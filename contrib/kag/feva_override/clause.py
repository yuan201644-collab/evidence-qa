# -*- coding: utf-8 -*-
"""v18b：条款级脚注（把溯源从「哪份文件」下沉到「哪一条」）。

要解决的问题
------------
K08：三级出处审计里「条款号」一级**全配置 0.000** —— 只能答到文档级。
但块里其实是有条号的（`SDM17_v2` 27 条正文、`HNM21_v2` 28 条），
v16 的离线脚本已经能把 B-05 定位到「第二十一条」，只是**没接进 run**。

与 v16 离线版的区别（为什么重写而不是直接搬）
--------------------------------------------
v16 版依赖 `chunk_span.SpanIndex` + 本地语料快照（`build/_lf_snap_local`），
用于把块名回切到原文区间。但那两样**只在本地有、容器里没有**，
搬进 run 就得连带部署快照，体积大且容易和线上语料不一致。

本版改成**只用生成时手里的 `refer_data`**：每个引用条目自带 `content`（块正文）
与 `document_name`，条号就在正文里。零额外依赖，容器里直接可跑。

判定链（与 v16 同口径）
-----------------------
    答案要点句 --3-gram 覆盖--> 引用块内最相似句 --向前找--> 最近的「第X条」
约束：前向覆盖 ≥0.40、反向覆盖 ≥0.30（挡长句靠常见词凑分）、
      命中句与条号距离 ≤200 字，超限不硬给。

只输出**能落到具体块**的脚注。落不到的宁可不给——给错条号比不给更糟。
"""
import re

ARTICLE_RE = re.compile(r"第[一二三四五六七八九十百零〇]{1,6}条")
TITLE_RE = re.compile(r"《([^》]{4,60})》")

MIN_COVER = 0.60      # 要点 → 句子 的前向覆盖率
MIN_BACK = 0.30       # 句子 → 要点 的反向覆盖率（防长句靠常见词凑分）
MAX_GAP = 200
MAX_ITEMS = 3
MAX_REFS = 8          # 只看前 8 个引用块，越靠前越相关

# 阈值为什么从 0.40 提到 0.60（2026-10-28 实测）
# ------------------------------------------------
# B 层 5 题首测挂出 4 条脚注，人工对 gold 核：
#   B-05 相似度 0.73 → 「第二十一条」**正确**（gold 就是 6 个月内申请 + 外单位专家）
#   B-03 相似度 0.88 → 第八条（未核）
#   B-01 相似度 0.42 → 「第二条」**错误**（gold 指第六条：B 类项目经费由负责人筹集）
#   B-04 相似度 0.45 → 第二十七条（未核）
# ⇒ 0.4–0.5 区间出错的概率明显偏高。**给错条号比不给更糟**（读者会当真），
#   所以宁可少给：0.60 会挡掉 0.42/0.45 两条低置信，保留 0.73/0.88。
#   该阈值只经 4 例观察得出，样本很小，需在下一轮用更多 B/D 题复核。


def _grams(s, n=3):
    s = re.sub(r"[\s，。、；：？！「」『』（）()《》“”\"'·—\-]", "", s or "")
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


def _cover(a, b):
    if not a:
        return 0.0
    return len(a & b) / float(len(a))


def _sentences(text):
    parts = re.split(r"[。；！？\n]", text or "")
    return [p.strip() for p in parts if p and p.strip()]


def _article_before(text, pos):
    """命中句所属条号。

    先看命中句本身是否以「第X条」开头——中文条款常写成「第二十一条  正文…」，
    此时往前找会误取上一条（v16 实测 B-05 因此从第二十一条退到第二十条）。
    """
    k = pos
    while k < len(text) and text[k] in " \u3000\t":
        k += 1
    m = ARTICLE_RE.match(text, k)
    if m:
        return m.group(0), 0

    best = (None, -1)
    for mm in ARTICLE_RE.finditer(text, 0, pos + 1):
        best = (mm.group(0), pos - mm.end())
    if best[0] is None:
        for mm in ARTICLE_RE.finditer(text, pos):
            if mm.start() - pos <= MAX_GAP:
                return mm.group(0), mm.start() - pos
            break
        return None, -1
    return best if best[1] <= MAX_GAP else (None, best[1])


def locate(answer, refer_data):
    """返回 [{ref_id, doc, article, score, exact}]，按相关度排序。

    只在「答案里有实质要点句」时工作；答案解析不出要点（例如纯拒答）时返回空。
    """
    refs = [r for r in (refer_data or []) if isinstance(r, dict)]
    # 合成条目（chunk:9_*）是账本结论，不是原文块，不能当条款出处
    refs = [r for r in refs if not str(r.get("id") or "").startswith("chunk:9_")][:MAX_REFS]
    claims = [s for s in _sentences(answer) if len(s) >= 10]
    if not refs or not claims:
        return []

    titles = [m.group(1) for m in TITLE_RE.finditer(answer or "")]
    claim_g = [_grams(c) for c in claims]

    out = []
    for r in refs:
        body = str(r.get("content") or "")
        if not body:
            continue
        # 注：曾用「答案点了《某办法》，块里必须出现该名称」作硬门槛，
        # 实测会把正确出处全部挡掉——500 字切块通常不含文件标题，
        # 标题只在首块出现。所以书名号只用于**加分**，不作过滤：
        # 真正的判别力来自下面的双向 3-gram 覆盖。

        best = None
        for sent in _sentences(body):
            sg = _grams(sent)
            if not sg:
                continue
            for cg in claim_g:
                fwd = _cover(cg, sg)
                if fwd < MIN_COVER:
                    continue
                if _cover(sg, cg) < MIN_BACK:
                    continue
                if best is None or fwd > best[0]:
                    best = (fwd, sent)
                break
        if not best:
            continue

        sc, sent = best
        pos = body.find(sent)
        art, gap = _article_before(body, max(0, pos))
        if not art:
            continue
        out.append({
            "ref_id": r.get("id"),
            "doc": r.get("document_name"),
            "article": art,
            "score": round(sc, 3),
            "exact": gap == 0,
        })

    out.sort(key=lambda x: (-x["score"], 0 if x["exact"] else 1))
    # 同一 (文档, 条号) 只留一条
    seen, uniq = set(), []
    for x in out:
        k = (x["doc"], x["article"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(x)
    return uniq[:MAX_ITEMS]


def render_footnotes(items):
    """渲染成追加在答案末尾的一段。刻意标成「出处·条款级」，不混进正文。"""
    if not items:
        return ""
    lines = ["", "【出处·条款级】"]
    for x in items:
        lines.append("- %s %s（块 %s，相似度 %.2f）"
                     % (x.get("doc") or "-", x.get("article"), x.get("ref_id") or "-",
                        x.get("score") or 0.0))
    return "\n".join(lines)


def append_to(answer, refer_data):
    """主入口：算不出就原样返回，绝不改动答案正文。"""
    try:
        items = locate(answer, refer_data)
    except Exception:
        return answer
    fn = render_footnotes(items)
    if not fn:
        return answer
    return (answer or "").rstrip() + fn
