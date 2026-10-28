# -*- coding: utf-8 -*-
"""v19：把「拆事实 → 判事实 → 图边作证 → 合成答案」接成一条线。

为什么要有这个模块（而不是继续在 ledger 上打补丁）
-------------------------------------------------
前几轮的做法是「先让模型答，再拿账本去核/去纠」。这条路的两个失败已经测出来了：
  · 核验器能检出「借措辞写错数」，但回灌重答只能改对一半（v18a：M-08 改对、M-06 没改对）；
  · 结论槽能治「给了不用」，但只注入了单个文档会把多文档题锚死（v18c：M-01/M-02 只答一半）。

根因是同一个：**账本一次性给整段结论，模型自己去对号入座。** 对号入座失败的形态有两种
（选错文档、忽略整段），我们分别打了两块补丁，各自只治一半。

本模块改成**先把问题拆成一条条待确认事实，每条单独判定并带上证据块**，
模型拿到的不再是一段结论，而是一张已判定的表——它没有"对号入座"这步可做。

一条线的四个环节
----------------
    ① 拆   plan()    题干 → [(文档, 年度, 单位, 类别, 期望)]，一条子问一行
    ② 判   judge()   每条事实在 1031 条结构化记录里做集合运算 → 值 + 支持/无据
    ③ 证   evidence() 图上 `(项目)-[:source]->(块)` 反向取块名 —— 那 34,525 条边
    ④ 用   render()  拼成一张表注入引用首位；模型只需照表作答

与 KAG 的分工（对应最初的设想）
----------------------------
KAG 负责把问题拆成可检索的任务（现在做的是"检索任务"，本模块在其后再拆一层"待确认事实"）；
FEVA 侧负责判定每条事实成不成立；图的 source 边负责给每条事实配证据块。
判定不依赖模型，因此可复算、可审计。

边界
----
- 只在「聚合型问题」（问几个/几项/多少）上生效；条款题（B 层）无聚合意图，直接返回空，
  由原有链路处理，不干扰。
- 图上取不到证据块时，该行标 `证据: 未取到`，**不编造块名**。
"""
import json
import os
import re
import urllib.request

# 与 executor 保持同一套连接参数
_NEO4J = os.environ.get("KAG_SA_NEO4J", "http://release-openspg-neo4j:7474").rstrip("/")
_DB = os.environ.get("KAG_SA_DB", "edugov4armds")
_USER = os.environ.get("KAG_SA_USER", "neo4j:neo4j@openspg")

CLAIM_KIND = ("总数", "分类")


# ---------------------------------------------------------------- 图：证据块

def _runner(stmt, params=None, timeout=20):
    import base64
    url = "%s/db/%s/tx/commit" % (_NEO4J, _DB)
    payload = {"statements": [{"statement": stmt, "parameters": params or {}}]}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Basic " + base64.b64encode(_USER.encode()).decode()})
    d = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
    if d.get("errors"):
        raise RuntimeError(str(d["errors"][0])[:200])
    res = d["results"][0]
    cols = res["columns"]
    return [{c: row[i] for i, c in enumerate(cols)} for x in res["data"] for row in [x["row"]]]


def _org_candidates(org):
    """单位名的候选写法，按精确度递减。

    图上的节点名是 `中国石油大学(华东)`（半角括号），而题干与账本解析出来的是
    全角「（华东）」——v19 六题里 M-17 就是因此取到 0 个证据块。
    这里只做**字符归一**（全角→半角、去空白），不做同义词扩展：
    去括号的核心名作为最后兜底，因为「中国石油大学」确实会跨校区误配，
    但配上 `ch.name CONTAINS 文档名` 之后误差受限，且只在精确名零结果时才用。
    """
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


def evidence(doc, org=None, limit=12):
    """一条事实的证据块：走 `(项目)-[:source]->(块)` 这条边。

    这是最初设想里「图里那些实体指回原文块的边负责当证据」的落地。
    先用 `承担单位` 边锚定项目，再走 `source` 边到块——直接按 source 取会把
    知识单元（KU）节点也算成项目，导致块数虚高（struct_agg_core 里踩过）。
    """
    pat = "%s_v2" % doc
    try:
        if not org:
            rows = _runner(
                "MATCH (p)-[:`承担单位`]->(o) MATCH (p)-[:source]->(ch) "
                "WHERE ch.name CONTAINS $pat "
                "RETURN DISTINCT ch.name AS n LIMIT $lim",
                {"pat": pat, "lim": limit})
            return [r["n"] for r in rows if r.get("n")]
        for cand in _org_candidates(org):
            rows = _runner(
                "MATCH (p)-[:`承担单位`]->(o) MATCH (p)-[:source]->(ch) "
                "WHERE ch.name CONTAINS $pat AND o.name CONTAINS $org "
                "RETURN DISTINCT ch.name AS n LIMIT $lim",
                {"pat": pat, "org": cand, "lim": limit})
            got = [r["n"] for r in rows if r.get("n")]
            if got:
                return got
        return []
    except Exception:
        return []


# ---------------------------------------------------------------- 拆：题干 → 事实

def _years(text):
    return re.findall(r"(20\d{2})\s*年度?", text or "")


def plan(question, ledger):
    """把问题拆成若干条待确认事实。

    拆的依据是**账本已有的解析器**（文档集 / 单位 / 类别 / 年度），不额外调模型：
    多调一次 LLM 会让这条线不可复算，也违背"判定不依赖模型"的定位。

    输出：[{doc, year, org, cats, label}]
      · 多文档题会拆成多行（M-01「2023 与 2024」→ 两行）——这正是 v18c 翻车的地方，
        一行一个文档，模型不会再只答一半。
    """
    if ledger is None or not question:
        return []
    try:
        if not ledger.__class__.__module__ and False:
            pass
        from_agg = getattr(ledger, "has_agg_intent", None)
        if callable(from_agg) and not from_agg(question):
            return []
    except Exception:
        pass

    try:
        docs, _how = ledger.resolve_docs(question)
    except Exception:
        docs = []
    if not docs:
        try:
            orgs0 = ledger.parse_orgs(question)
            docs = ledger.reverse_docs_by_org(orgs0) if orgs0 else []
        except Exception:
            docs = []
    if not docs:
        return []

    try:
        orgs = ledger.parse_orgs(question)
    except Exception:
        orgs = []
    try:
        cats = [c for _f, _d, _t, c in ledger.parse_cats(question, docs)]
    except Exception:
        cats = []

    years = _years(question)
    # 年度与文档一一对应时（题目按年列举），把年份挂到对应文档上
    doc_year = {}
    meta = getattr(ledger, "DOC_META", {}) or {}
    for d in docs:
        m = meta.get(d)
        if m and len(m) > 1:
            doc_year[d] = str(m[1])

    out = []
    for d in docs:
        org_hit = []
        try:
            recs = ledger.by_doc.get(d, [])
            org_hit = [o for o in orgs if any(r["org_n"] == o for r in recs)]
        except Exception:
            pass
        out.append({
            "doc": d,
            "year": doc_year.get(d) or (years[0] if len(years) == 1 else ""),
            "org": org_hit[0] if org_hit else None,
            "cats": list(cats),
            "label": _label(ledger, d, org_hit, cats),
        })
    return out


def _label(ledger, doc, orgs, cats):
    meta = (getattr(ledger, "DOC_META", {}) or {}).get(doc) or ("", "", "")
    who = "、".join(orgs) if orgs else "全部单位"
    cat = "、".join(cats) if cats else "全部类别"
    return "%s（%s %s 年度 %s）· %s · %s" % (doc, meta[0], meta[1], meta[2], who, cat)


# ---------------------------------------------------------------- 判：记录层集合运算

def judge(claim, ledger):
    """对一条事实做判定。返回 {n, verdict, by_cat, ids}。

    verdict:
      supported   —— 记录里能算出来（n > 0）
      empty       —— 能定位到文档与单位，但集合为空
      unresolved  —— 连文档都定位不到（不上表）
    """
    doc = claim.get("doc")
    org = claim.get("org")
    cats = claim.get("cats") or []
    try:
        recs = ledger.by_doc.get(doc, [])
    except Exception:
        return {"verdict": "unresolved", "n": 0}
    if not recs:
        return {"verdict": "unresolved", "n": 0}

    pool = recs
    if org:
        pool = [r for r in pool if r["org_n"] == org]
    by_cat = {}
    for r in pool:
        c = str(r.get("cat") or "").strip() or "未标注"
        by_cat[c] = by_cat.get(c, 0) + 1

    # 类别过滤只影响"该类别有多少"，不影响总数判定
    n = len(pool)
    if cats:
        n_cat = sum(by_cat.get(c, 0) for c in cats)
        return {"verdict": "supported" if n_cat else "empty", "n": n_cat,
                "total": n, "by_cat": by_cat,
                "ids": [r.get("id") for r in pool][:20]}
    return {"verdict": "supported" if n else "empty", "n": n,
            "total": n, "by_cat": by_cat,
            "ids": [r.get("id") for r in pool][:20]}


# ---------------------------------------------------------------- 用：渲染成表

def render(rows, max_blocks=3):
    """渲染成一张「已判定的待确认事实表」。

    v19b 改了粒度：**一个子问一行**（总数 / 每个所问类别 / 每所学校 / 编号），
    而不是一个文档一行。原因是 v19 全量实测出来的：
      · M-06 表里只有「7 项」，题目问「应用/基础/研究报告各多少」→ 模型只能自己数，数成 5；
      · M-07 表里把「论文 3」放在最前、总数 7 塞进括号 → 模型直接把 3 当总数；
      · M-12 表里只给「合计 29」→ 模型自己编出四校 18/9/9/8（合计 44），再凑出 29。
    共同点是：**表给的信息量小于题目问的信息量**，模型必然去检索块里自己补，
    一补就错。所以表必须把题目问到的每个数都单独成行。
    """
    if not rows:
        return ""
    out = ["【待确认事实表｜每条已由结构化记录判定，可直接采用，勿再自行清点】",
           "| # | 事实 | 判定 | 值 | 证据块 |",
           "|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        blk = r.get("blocks") or []
        blk_txt = "、".join(blk[:max_blocks]) if blk else ("同上" if r.get("same_block") else "未取到")
        mark = "✅支持" if r.get("verdict") == "supported" else "⚠️无据"
        out.append("| %d | %s | %s | %s | %s |" % (i, r["label"], mark, r["value"], blk_txt))
    out.append("")
    out.append("规则：上表每一行都已判定，答案中的每个数字都必须能对应到某一行，"
               "并注明其对应的行名；不得把这些数字相加、相减或自行重新清点。"
               "表中没有的数，就是语料里没有。")
    return "\n".join(out)


def _cat(it):
    return str(it.get("cat") or "").strip() or "未标注"


def _hit(cat, targets):
    return any(t and t in cat for t in targets)


def _row(label, value, verdict="supported", blocks=None, same_block=False):
    return {"label": label, "value": value, "verdict": verdict,
            "blocks": blocks or [], "same_block": same_block}


def _cat_counts(g):
    """把 group 的 by_cat 压成 {类别: 数量}；合计保证等于 total。"""
    out = {}
    for _label, cnt in (g.get("by_cat") or {}).items():
        for k, v in (cnt or {}).items():
            out[k] = out.get(k, 0) + int(v)
    miss = int(g.get("total") or 0) - sum(out.values())
    if miss > 0:
        out["未标注"] = out.get("未标注", 0) + miss
    return out


_DIFFSET_RE = re.compile(r"(出现|新增|多出)[^？?。]{0,20}(没有|不含|未出现)|没有的(项目)?类别")
_DIFFNUM_RE = re.compile(r"(增加|减少|多|少)了?\s*多少|相差多少|差多少")


def _year_ops(question, groups, with_evidence=True):
    """跨年差集 / 差量（v20）。

    为什么单独一类：M-03「2024 出现了 2023 没有的类别」、M-05「2024 比 2023 增加
    多少项」问的都是**两份名单之间的关系**，不是单份名单内的计数。按单文档渲染的
    事实表给不出这个数，模型只能落回检索，而这两题恰好零召回。

    方向由题干语序定：**先出现的年度 = A（被问的一方）**，后出现的 = B（参照系）。
      · 差集：cats(A) − cats(B)，逐类给数量
      · 差量：total(A) − total(B)，带符号
    只在文档数恰好 2、且两份都带年度时生效；否则原样返回空，走通用路径。
    """
    q = question or ""
    if len(groups) < 2:
        return []
    gs = []
    for g in groups:
        meta = g.get("meta") or ("", "", "")
        if not meta[1]:
            return []
        i = q.find(meta[1])
        gs.append((i if i >= 0 else 10 ** 6, g))
    gs.sort(key=lambda x: x[0])
    if len(gs) != 2 or gs[0][1]["doc"] == gs[1][1]["doc"]:
        return []
    ga, gb = gs[0][1], gs[1][1]
    ma, mb = ga.get("meta"), gb.get("meta")
    head_a = "%s（%s %s 年度）" % (ga["doc"], ma[0] or "-", ma[1] or "-")
    head_b = "%s（%s %s 年度）" % (gb["doc"], mb[0] or "-", mb[1] or "-")

    def _ev(doc):
        return evidence(doc) if with_evidence else []

    rows = []
    if _DIFFSET_RE.search(q) and re.search(r"类别|哪几类|几类", q):
        ca, cb = _cat_counts(ga), _cat_counts(gb)
        new = {k: v for k, v in ca.items() if k not in cb}
        if not new:
            return []
        for k, v in sorted(new.items(), key=lambda kv: -kv[1]):
            rows.append(_row("%s 有、%s 没有的类别 · %s" % (head_a, head_b, k),
                             "**%d 项**" % v, "supported", _ev(ga["doc"])))
        rows.append(_row("%s 有、%s 没有的类别 · 合计 %d 类" % (head_a, head_b, len(new)),
                         "**%d 项**" % sum(new.values()), "supported",
                         blocks=[], same_block=True))
        return rows
    if _DIFFNUM_RE.search(q) and re.search(r"总数|合计|共|项数", q):
        ta, tb = int(ga.get("total") or 0), int(gb.get("total") or 0)
        if not ta or not tb:
            return []
        diff = ta - tb
        rows.append(_row("%s 项目总数" % head_a, "**%d 项**" % ta, "supported", _ev(ga["doc"])))
        rows.append(_row("%s 项目总数" % head_b, "**%d 项**" % tb, "supported", _ev(gb["doc"])))
        rows.append(_row("%s 项目总数 − %s 项目总数" % (head_a, head_b),
                         "**%+d 项**" % diff, "supported", blocks=[], same_block=True))
        return rows
    return []


def build(question, ledger, with_evidence=True):
    """完整链路：拆 → 判 → 证。返回 (rows, 表格文本)。

    实现取舍：**不重写文档解析**，直接复用 `ledger.query()` 的 groups。
    第一版自己写了一套 plan()（resolve_docs + parse_orgs），离线一测 M-02 就掉队——
    账本 query 里还有 `reverse_docs_by_cat` 等兜底，重写等于漏掉它们。

    拆的粒度（v19b）：
      单位唯一 → 「合计」+「每个所问类别」+（问编号时）「每个类别的编号」
      单位多个 → 「每所学校 × 所问类别」+「合计」+（问编号时）各校编号
    一条子问一行，模型不需要再做任何对号入座或补算。
    """
    if ledger is None or not question:
        return [], ""
    try:
        res = ledger.query(question)
    except Exception:
        res = None
    if not res or not res.get("groups"):
        return [], ""

    # v20：跨年差集 / 差量优先——它们问的是两份名单之间的关系，不是单份的计数
    ops = _year_ops(question, res["groups"], with_evidence=with_evidence)
    if ops:
        return ops, render(ops)

    ask_ids = bool(re.search(r"编号", question or ""))
    rows = []
    for g in res["groups"]:
        meta = g.get("meta") or ("", "", "")
        items = [it for it in (g.get("items") or []) if isinstance(it, dict)]
        total = int(g.get("total") or len(items) or 0)
        orgs = [o for o in (g.get("orgs") or []) if o]
        targets = [t for t in (g.get("targets") or []) if t]
        head = "%s（%s %s 年度 %s）" % (
            g["doc"], meta[0] or "-", meta[1] or "-", meta[2] or "-")
        blocks = evidence(g["doc"], orgs[0] if len(orgs) == 1 else None) if with_evidence else []

        if not items:
            rows.append(_row(head + " · " + ("、".join(orgs) if orgs else "全部单位") + " · 合计",
                             "**%d 项**" % total, "supported" if total else "empty", blocks))
            continue

        if len(orgs) >= 2:
            # 多单位：先逐校给数（带类别口径），再给合计
            cat_txt = "、".join(targets) if targets else "全类别"
            for o in orgs:
                sub = [it for it in items if it.get("org") == o]
                v = sum(1 for it in sub if _hit(_cat(it), targets)) if targets else len(sub)
                rows.append(_row("%s · %s · %s" % (head, o, cat_txt), "**%d 项**" % v,
                                 "supported" if v else "empty", blocks, same_block=True))
                if ask_ids and targets:
                    ids = [it.get("id") for it in sub if _hit(_cat(it), targets)]
                    if ids:
                        rows.append(_row("%s · %s · %s类项目编号" % (head, o, cat_txt),
                                         "、".join(str(x) for x in ids if x),
                                         blocks=[], same_block=True))
            tot = sum(1 for it in items if _hit(_cat(it), targets)) if targets else total
            rows.append(_row("%s · 上述 %d 校 %s 合计" % (head, len(orgs), cat_txt),
                             "**%d 项**" % tot, "supported" if tot else "empty",
                             blocks=[], same_block=True))
        else:
            who = orgs[0] if orgs else "全部单位"
            rows.append(_row("%s · %s · 全类别合计" % (head, who), "**%d 项**" % total,
                             "supported" if total else "empty", blocks))
            for t in targets:
                sub = [it for it in items if _hit(_cat(it), [t])]
                rows.append(_row("%s · %s · %s类" % (head, who, t), "**%d 项**" % len(sub),
                                 "supported" if sub else "empty", blocks=[], same_block=True))
                if ask_ids and sub:
                    ids = [it.get("id") for it in sub if it.get("id")]
                    if ids:
                        rows.append(_row("%s · %s · %s类项目编号" % (head, who, t),
                                         "、".join(str(x) for x in ids),
                                         blocks=[], same_block=True))
            if targets:
                rest = [it for it in items if not _hit(_cat(it), targets)]
                if rest:
                    detail = {}
                    for it in rest:
                        detail[_cat(it)] = detail.get(_cat(it), 0) + 1
                    rows.append(_row("%s · %s · 其余类别" % (head, who),
                                     "**%d 项**（%s）" % (len(rest), " ".join(
                                         "%s%d" % (k, v) for k, v in
                                         sorted(detail.items(), key=lambda kv: -kv[1]))),
                                     blocks=[], same_block=True))
    if not rows:
        return [], ""
    return rows, render(rows)


def _flat_cats(g):
    """把 by_cat 的 {字段: {值: 数量}} 压成 {值: 数量}，并保证合计等于 total。"""
    out = {}
    for _label, cnt in (g.get("by_cat") or {}).items():
        for k, v in (cnt or {}).items():
            out[k] = out.get(k, 0) + int(v)
    miss = (g.get("total") or 0) - sum(out.values())
    if miss > 0:
        out["未标注"] = out.get("未标注", 0) + miss
    return out


if __name__ == "__main__":
    # 离线自测：只验「拆」与「判」，证据块需要图，离线留空
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("led", os.path.join(here, "ledger.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    root = os.path.dirname(os.path.dirname(here))
    L = m.Ledger(os.path.join(root, "_corpus_probe", "_records.json"))
    qb = json.load(open(os.path.join(root, "qbank_v4.json"), encoding="utf-8"))["questions"]
    for qid in ("M-01", "M-02", "M-17", "M-18", "M-20", "B-05"):
        x = next((i for i in qb if i["qid"] == qid), None)
        if not x:
            continue
        rows, txt = build(x["text"], L, with_evidence=False)
        print("=" * 70)
        print(qid, "gold:", str(x.get("gold"))[:60])
        print(txt if txt else "(本模块不处理该题)")
