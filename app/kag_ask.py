# -*- coding: utf-8 -*-
"""KAG 直连链路：一次提问 → KAG 规划检索生成 → 本地事实表 → 契约化响应。

这个模块是「拆 → 判 → 证 → 用」四段在**应用侧**的落地，与容器内
`feva_override/refinject.py` 的分工如下：

| 环节 | 容器内（模型看到的） | 应用侧（界面上看到的） |
|---|---|---|
| 拆 | `facts.plan/query` 复用账本解析器 | 同一份 `facts.build()` |
| 判 | 1031 条结构化记录做集合运算 | 同一份算法，逐字一致 |
| 证 | 走 Neo4j 的 `(项目)-[:source]->(块)` 边 | **走本次响应里的检索块**（见下） |
| 用 | 渲染成表插进 `refer_data` 首位 | 渲染成 `fact_review` 卡片 |

为什么证据不查图库：容器里那条 source 边路径是对的，但应用侧没有 Neo4j 连接，
而 KAG 的 SSE 已经把每个召回块的**正文**下发了（实测 93/93 个节点带
`properties.content`）。用本次请求真正用到的块当证据，比回查全库更贴近
「模型当时看到了什么」，也少一层跨机依赖。

两条路径的**算法**必须一致，否则界面上的表会和模型当时看到的表对不上。
所以这里不重写判定逻辑，只把 `facts.evidence` 换成一个数据源不同的同名函数。
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import threading
from pathlib import Path

from . import kag_client

_HERE = Path(__file__).resolve().parent
_CONTRIB = _HERE.parent / "contrib" / "kag" / "feva_override"

_TL = threading.local()
_LOCK = threading.Lock()
_MODULES: dict | None = None


# ---------------------------------------------------------------- 加载事实层

def _load_modules() -> dict:
    """按文件路径加载 facts/ledger。

    为什么不 `import feva_override`：那个包的 `__init__` 在容器里会注册
    KAG 覆盖模块，应用侧不该触发它；而且 `contrib/` 不在 `sys.path` 里。
    按路径加载可以保证「用哪份代码」是显式的，不受 PYTHONPATH 影响。
    """
    global _MODULES
    if _MODULES is not None:
        return _MODULES
    with _LOCK:
        if _MODULES is not None:
            return _MODULES
        if not (_CONTRIB / "facts.py").exists():
            raise RuntimeError("事实层未找到：%s" % _CONTRIB)

        def _load(name: str, filename: str):
            spec = importlib.util.spec_from_file_location(name, str(_CONTRIB / filename))
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            return mod

        ledger_mod = _load("_feva_ledger", "ledger.py")
        facts_mod = _load("_feva_facts", "facts.py")
        records = _CONTRIB / "data" / "_records.json"
        ledger = ledger_mod.Ledger(str(records) if records.exists() else None)

        # 证据源改成「本次响应里的检索块」。
        # 容器内仍走 Neo4j（那边没有 SSE 上下文），两边入口不同、判定逻辑相同。
        if os.environ.get("FEVA_EVIDENCE_MODE", "sse") != "neo4j":
            facts_mod.evidence = _evidence_from_response

        _MODULES = {"ledger": ledger_mod, "facts": facts_mod,
                    "ledger_obj": ledger, "records": records}
        return _MODULES


def ledger():
    return _load_modules()["ledger_obj"]


def facts():
    return _load_modules()["facts"]


def _records() -> list:
    return getattr(_load_modules()["ledger_obj"], "records", []) or []


# ---------------------------------------------------------------- 证：块正文

def _norm(text: str) -> str:
    import unicodedata
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def _org_candidates(org: str) -> list[str]:
    """单位名的候选写法，与 facts._org_candidates 保持同一套归一规则。"""
    if not org:
        return []
    import unicodedata
    s = unicodedata.normalize("NFKC", str(org))
    out = [s, re.sub(r"\s+", "", s)]
    core = re.split(r"[\(（]", s)[0].strip()
    if core and core not in out:
        out.append(core)
    seen, uniq = set(), []
    for x in out:
        if x and x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def _evidence_from_response(doc, org=None, limit=12):
    """同名替换 `facts.evidence`：从本次响应召回块里取该文档的块。

    与图库版的差别只有数据源，筛选规则一致：先按文档前缀锁定块，
    再按单位名做一次软过滤 —— 过滤后为空就退回不过滤，因为一个块里
    可能混着多个单位的记录，硬过滤会把正确块误杀。
    """
    res = getattr(_TL, "res", None)
    if res is None:
        return []
    blocks = res.chunks_of(doc, limit=max(limit, 24))
    if not blocks:
        return []
    if org:
        cands = [_norm(x) for x in _org_candidates(org)]
        hit = [c for c in blocks if any(x and x in _norm(c.content) for x in cands)]
        if hit:
            blocks = hit
    return [c.name for c in blocks][:limit]


# ---------------------------------------------------------------- 拆 + 判

def build_fact_rows(question: str, res: kag_client.KagResult | None):
    """跑一次「拆 → 判 → 证」，返回 (rows, 表格文本)。

    `res` 为 None 时（例如只做离线预览）证据列全空，判定结果不变。
    """
    mod = _load_modules()
    _TL.res = res
    try:
        rows, text = mod["facts"].build(question, mod["ledger_obj"], with_evidence=True)
    except Exception:
        return [], ""
    finally:
        _TL.res = None
    return rows, text


# ---------------------------------------------------------------- 契约组装

_DOC_HEAD_RE = re.compile(r"^([A-Z]{2}\d{2}[A-Z]?)（([^）]*)）")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _plain(value: str) -> str:
    """去掉渲染用的 markdown 强调，只留值本身。"""
    s = _BOLD_RE.sub(r"\1", str(value or ""))
    return s.strip()


def _doc_title(doc: str) -> str:
    meta = getattr(_load_modules()["ledger"], "DOC_META", {}).get(doc)
    if not meta:
        return doc
    prov, year, kind = meta
    return "%s %s 年度 %s（%s）" % (prov, year, kind, doc)


def _criterion(label: str) -> tuple[str, str]:
    """从行标签里拆出「口径」与「事实名」。

    行标签的形状是 `AH24（安徽 2024 年度 立项名单）· 滁州学院 · 重点科研类`：
    前一段是口径（哪份文档、哪年、什么文种），后面是这条具体在问什么。
    拆开是为了让界面不必把口径重复显示两遍。
    """
    m = _DOC_HEAD_RE.match(label or "")
    if not m:
        return "", label or ""
    head = m.group(0)
    rest = (label or "")[len(head):].lstrip(" ·-—")
    return head, (rest or label or "")


def _refs_and_index(res: kag_client.KagResult):
    """把召回块转成 DocRef 列表，并给出「块名 → refs 下标」的映射。

    引用表按服务端返回顺序（即相关性顺序）；同一份文档的多个块各占一条，
    因为用户点开一条要看到的是那一块原文，不是整份文档。
    """
    refs, index = [], {}
    for c in res.chunks:
        snippet = (c.content or "").strip()
        if not snippet:
            snippet = "块 %s（服务端未返回正文）" % c.name
        refs.append({
            "kind": "doc",
            "doc_id": c.doc,
            "doc_title": _doc_title(c.doc),
            "section_path": c.name,
            "snippet": snippet[:600],
        })
        index[c.name] = len(refs) - 1
    return refs, index


def _fact_review(rows, res, ref_index: dict):
    """把事实表的每一行投影成 FactReviewItem。"""
    if not rows:
        return None
    items = []
    # 派生行（「上述 N 校合计」「A − B」这类由前面几行算出来的行）在表里标的是
    # 「同上」，本身没有独立证据块。契约上不能让它显示成「证据未关联」——
    # 那会被误读成这条算不出依据。继承**它实际依据的那几行**才是真实情形：
    # 差量行「A − B」同时依赖 A 和 B 两行，所以这里累积而不是只记上一行。
    acc_chunks: list[str] = []
    acc_idx: list[int] = []
    for i, r in enumerate(rows, 1):
        blocks = [b for b in (r.get("blocks") or []) if b]
        label = str(r.get("label") or "")
        criterion, _short = _criterion(label)
        verdict = r.get("verdict")
        supported = verdict == "supported"
        idx = [ref_index[b] for b in blocks if b in ref_index]
        chunk = blocks[0] if blocks else None
        if idx:
            for b in blocks:
                if b not in acc_chunks:
                    acc_chunks.append(b)
            for x in idx:
                if x not in acc_idx:
                    acc_idx.append(x)
        elif r.get("same_block") and acc_idx:
            chunk, idx = (acc_chunks[0] if acc_chunks else None), list(acc_idx)
        items.append({
            "fact_id": "F%02d" % i,
            "label": label,
            # 不再填 summary：`label` 已经是「口径 + 事实」的完整描述，
            # 而 criterion 又把口径单独列了一列，再放一句「去掉口径的事实名」
            # 会让界面出现两行几乎一样的话。空着比重复强。
            "summary": None,
            "status": "supported" if supported else "insufficient",
            "execution_status": "completed",
            "evidence_link_status": "complete" if idx else "unavailable",
            "checks": [{
                "code": "structured_set_operation",
                "result": "passed" if supported else "unknown",
                "detail": None if supported else "该口径在结构化记录中没有对应项",
            }],
            "evidence_ref_indices": idx,
            "reason_code": None if idx else "evidence_not_retrieved",
            "reason": None if idx else "本次检索未召回该文档的块",
            # 以下三个字段是本项目为方向三补的：契约原来只有「判没判」，
            # 没有「判出什么值」和「凭哪一块」。
            "value": _plain(r.get("value")),
            "criterion": criterion,
            "evidence_chunk": chunk,
        })
    return {
        "schema_version": "2",
        "coverage": "document_qa",
        "verification_mode": "strict",
        "verification_scope": "document_retrieval_and_citation",
        "scope_note": (
            "本卡片是事实层的投影，不是答案的逐句核验：每一行由 %d 条结构化记录做集合运算判定，"
            "证据块取自本次请求实际召回的块。表的覆盖范围仅限于可拆成「某文档 · 某单位 · 某类别」"
            "的计数类子问；表外的推理过程未经核验。" % len(_records())
        ),
        "items": items,
    }


def _trace(res: kag_client.KagResult, n_rows: int) -> list[dict]:
    """把 KAG 的三段执行过程投影成调用链，供界面的「执行过程」抽屉展示。"""
    return [
        {"step_id": 1, "tool": "kag_plan", "input": {},
         "output": {"planner": "DomainLFStaticPlanningPromptDAG",
                    "thinking": True}, "elapsed_ms": 0, "hit": None},
        {"step_id": 2, "tool": "kag_retrieve", "input": {},
         "output": {"num_hits": len(res.chunks), "num_docs": len(res.docs),
                    "docs": res.docs}, "elapsed_ms": 0, "hit": None},
        {"step_id": 3, "tool": "llm_generate", "input": {},
         "output": {"answer_len": len(res.answer), "fact_rows": n_rows},
         "elapsed_ms": int(res.elapsed * 1000), "hit": None},
    ]


def _stage(publish, stage_id: str, label: str, status: str, elapsed_ms: int | None = None):
    if publish is None:
        return
    try:
        publish({"type": "stage", "id": "%s-%d" % (stage_id, elapsed_ms or 0),
                 "stage_id": stage_id, "label": label, "status": status,
                 "elapsed_ms": elapsed_ms})
    except Exception:
        pass


# ---------------------------------------------------------------- 对外入口

def answer(question: str, session_id: str = "", progress_callback=None) -> dict:
    """跑通一次完整提问，返回符合 AskResponse 契约的 dict。

    失败时不编造答案：`answer` 里写明失败原因，`refs` / `fact_review` 留空，
    前端会当成一次正常的「没答上来」，而不是假装成功。
    """
    client = kag_client.KagClient(
        base=os.environ.get("KAG_BASE", "http://10.166.33.23:8887"),
        account=os.environ.get("KAG_ACCOUNT", "openspg"),
        password=os.environ.get("KAG_PASSWORD", "openspgadmin"),
        app_id=int(os.environ.get("KAG_APP_ID", "5")),
        timeout=int(os.environ.get("KAG_TIMEOUT", "420")),
    )

    _stage(progress_callback, "kag_plan", "KAG 解析问题并规划检索", "started")
    res = client.ask(question, thinking=True)
    _stage(progress_callback, "kag_plan", "KAG 解析问题并规划检索", "completed")

    if not res.ok:
        _stage(progress_callback, "kag_retrieve", "KAG 检索语料", "failed")
        return {
            "answer": "本次未取得有效回答。%s" % (res.error or "服务端返回了空答案或兜底文案。"),
            "trace": _trace(res, 0), "refs": [], "sql": None,
            "need_clarify": False, "fact_review": None,
        }

    _stage(progress_callback, "kag_retrieve", "KAG 检索语料", "completed",
           int(res.elapsed * 1000))

    _stage(progress_callback, "fact_table", "生成待确认事实表", "started")
    rows, _text = build_fact_rows(question, res)
    _stage(progress_callback, "fact_table", "生成待确认事实表",
           "completed" if rows else "skipped")

    refs, ref_index = _refs_and_index(res)
    review = _fact_review(rows, res, ref_index)
    _stage(progress_callback, "fact_evidence", "为每条事实挂原文块",
           "completed" if review else "skipped")

    return {
        "answer": res.answer,
        "trace": _trace(res, len(rows)),
        "refs": refs,
        "sql": None,
        "need_clarify": False,
        "fact_review": review,
    }


def fact_table(question: str) -> dict:
    """事实表页用的离线接口：不调 KAG，只跑拆/判。

    证据列留空并显式标注，因为「哪一块」取决于当时那次检索——
    没有检索就声称有证据，正好是这一版要消灭的那类假溯源。
    """
    rows, text = build_fact_rows(question, None)
    items = []
    for i, r in enumerate(rows, 1):
        criterion, short = _criterion(str(r.get("label") or ""))
        items.append({
            "fact_id": "F%02d" % i,
            "label": r.get("label"),
            "criterion": criterion,
            "fact": short,
            "value": _plain(r.get("value")),
            "status": "supported" if r.get("verdict") == "supported" else "insufficient",
        })
    return {"question": question, "rows": items, "table": text}


def graph_stats() -> dict:
    """图的规模底数，供界面标注「证据来自这张图」。

    数字是 2026-10-28 在 `edugov4armds` 库实测的静态快照，不是实时查询：
    应用侧不连图库，写死一个带测量时间的快照，比给一个连不上的实时接口诚实。
    """
    return {
        "measured_at": "2026-10-28",
        "database": "edugov4armds",
        "source_edges": 34525,
        "business_edges": 7177,
        "relation_types": 326,
        "承担单位": 1070,
        "项目类别": 1049,
        "负责人": 1106,
        "note": "source 边 = (项目)-[:source]->(块)，即事实表每个证据格的来源",
    }
