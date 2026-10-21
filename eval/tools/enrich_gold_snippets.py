#!/usr/bin/env python
"""给 `eval/cases/rag.jsonl` 的 `gold_refs` 补上 `snippet`（引用原文片段）。

为什么必须补：
    评测口径（`docs/评测口径_v0.1.md` L200 / 3.3 节）要求单题通过的条件之一是
    「**有非空引用**（引用片段非空、且指向实际检索到的文档）」。而 `rag.jsonl`
    的 `gold_refs` 只有 `doc`（文件名）与 `anchor`（章节串），**没有片段正文**——于是：
      · 标准答案自己都拿不出一个非空片段，这条判据**无法被验证**；
      · oracle 自证（把标准答案当预测喂回去）必然判 `snippet_nonempty=False`，
        整条自证链变成假警报。
    补 `snippet` 之后，oracle 才能以**契约形状**（`doc_id`/`section_path`/`snippet`）
    回放标准答案，字段名一旦改错就会立刻显形。

片段从哪来、为什么不手写：
    语料是 `data/corpus/tools/build_corpus.py` 生成的，`DOCS_ALL` 里存着每份文档的
    **章节原文**（`sections` = [(层级, 文本)]）。本脚本按 `(doc, anchor)` 去那里逐字取，
    不重打一个字——手写片段等于往标准答案里塞未经核对的内容，比不补更糟。

派生样本（KB-002 / KB-004 / KB-015）是「目录混乱版」，正文与母本一致，
本脚本按 `derived_from` 回溯到母本取片段。

用法：
    python eval/tools/enrich_gold_snippets.py            # 干跑，只打印将要写入的片段
    python eval/tools/enrich_gold_snippets.py --write    # 实际写入 rag.jsonl
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAG_CASES = ROOT / "eval" / "cases" / "rag.jsonl"
BUILDER = ROOT / "data" / "corpus" / "tools" / "build_corpus.py"

# 派生样本 → 母本。目录混乱版的正文与母本一致，片段回溯到母本取。
DERIVED_FROM = {"KB-002": "KB-001", "KB-004": "KB-003", "KB-015": "KB-013"}

_STRIP = re.compile(r"[\s\u3000，。、；：！？,.;:!?（）()【】\[\]「」\"'`·\-—–_/]+")
_ID_RE = re.compile(r"^(KB-\d+)", re.I)


def norm(text: str) -> str:
    """去掉空白与标点后的小写形式 —— 与 `eval/rag_metrics.py::normalise()` 同一套规则。

    语料标题用的是全角空格 `\\u3000`（「第二章　质保条款」），而用例里写的是半角空格
    （「第二章 质保条款」）。不归一的话一条都匹配不上。
    """
    return _STRIP.sub("", (text or "").lower())


def load_builder():
    spec = importlib.util.spec_from_file_location("build_corpus", BUILDER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def doc_index(builder) -> dict[str, dict]:
    """`KB-XXX` → 文档定义（含派生件，指向母本）。"""
    idx: dict[str, dict] = {}
    for d in builder.DOCS_ALL:
        idx[d["id"].upper()] = d
    for derived, parent in DERIVED_FROM.items():
        if parent.upper() in idx:
            idx[derived.upper()] = idx[parent.upper()]
    return idx


def extract_snippet(sections: list, anchor: str) -> tuple[str, str]:
    """从 `sections` 里取 `anchor` 对应的原文片段。

    返回 `(片段, 命中方式)`。命中方式有：
      `heading` —— anchor 落在某个章节标题上（含「锚点是标题前缀」的情况，如
                   anchor「服务器巡检记录」对应标题「服务器巡检记录（扫描件）」）
      `para`    —— anchor 落在正文段落上（如 KB-012 的「二 故障响应」其实是
                   段落「二、故障响应：接到报修后 2 小时内派单…」的前缀）
      `miss`    —— 都没匹配上，**必须人工处理**，不静默给空片段

    取片段的范围：标题行 + 该标题下、下一个同级或更高级标题之前的全部正文段落。
    宁可多带一句上下文，也不要因为「只要那一句」而挑错句——片段是证据，
    漏句会让「引用非空」变成形式主义。
    """
    target = norm(anchor)
    if not target:
        return "", "miss"

    rows = [(int(lv), str(t)) for lv, t in sections]

    # 1) 标题匹配（含互相为前缀的情况）
    hidx = None
    for i, (lv, t) in enumerate(rows):
        if lv <= 0:
            continue
        h = norm(t)
        if h and (h == target or h.startswith(target) or target.startswith(h)):
            hidx = i
            break
    if hidx is not None:
        lv0 = rows[hidx][0]
        parts = [rows[hidx][1]]
        for lv, t in rows[hidx + 1 :]:
            if lv > 0 and lv <= lv0:
                break
            parts.append(t)
        return "\n".join(p for p in parts if p.strip()), "heading"

    # 2) 正文段落匹配。
    #    两个方向都要判：用例里的 anchor 既可能是段落的前缀（「二 故障响应」←
    #    「二、故障响应：接到报修后 2 小时内派单…」），也可能把段落写得更全。
    #    先判「段落以 anchor 开头」这个更精确的方向，再退回反向，避免短段落误配。
    bodies = [(lv, t) for lv, t in rows if lv <= 0]
    for lv, t in bodies:
        n = norm(t)
        if n and n.startswith(target):
            return t, "para"
    for lv, t in bodies:
        n = norm(t)
        if n and target.startswith(n):
            return t, "para"

    return "", "miss"


def main() -> int:
    ap = argparse.ArgumentParser(description="给 rag.jsonl 的 gold_refs 补 snippet")
    ap.add_argument("--write", action="store_true", help="实际写入（默认只干跑打印）")
    args = ap.parse_args()

    builder = load_builder()
    idx = doc_index(builder)

    lines = [l for l in RAG_CASES.read_text(encoding="utf-8").splitlines() if l.strip()]
    cases = [json.loads(l) for l in lines]

    problems: list[str] = []
    stats = {"heading": 0, "para": 0, "miss": 0}
    total_refs = 0

    for case in cases:
        print(f"\n=== {case['id']}  [{case.get('kind')}] ===")
        for j, ref in enumerate(case.get("gold_refs") or []):
            total_refs += 1
            doc, anchor = ref.get("doc", ""), ref.get("anchor", "")
            m = _ID_RE.match(doc)
            doc_id = m.group(1).upper() if m else ""
            d = idx.get(doc_id)
            if d is None:
                problems.append(f"{case['id']}#gold_refs[{j}]: 生成器里找不到文档 {doc_id}（{doc}）")
                print(f"  [{j}] MISS 文档未收录：{doc_id}")
                stats["miss"] += 1
                continue
            snippet, how = extract_snippet(d["sections"], anchor)
            stats[how] += 1
            if how == "miss":
                problems.append(f"{case['id']}#gold_refs[{j}]: 锚点匹配不上：{doc_id} / {anchor!r}")
                print(f"  [{j}] MISS 锚点未命中：{doc_id} / {anchor!r}")
                continue
            ref["snippet"] = snippet
            print(f"  [{j}] {how:<7} {doc_id} / {anchor!r}")
            print(f"       → {snippet[:120]}{'…' if len(snippet) > 120 else ''}")

    print("\n" + "=" * 70)
    print(f"引用条数 {total_refs}：标题命中 {stats['heading']} / 段落命中 {stats['para']} / 未命中 {stats['miss']}")

    if problems:
        print(f"\n需要人工处理 {len(problems)} 项（**不允许带着 miss 写入**）：")
        for p in problems:
            print("  -", p)
        return 1

    if not args.write:
        print("\n干跑结束，未写入。核对无误后加 --write。")
        return 0

    out = "\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n"
    RAG_CASES.write_text(out, encoding="utf-8")
    print(f"\n已写入 {RAG_CASES.relative_to(ROOT)}（{len(cases)} 条用例）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
