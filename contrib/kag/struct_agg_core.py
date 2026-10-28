# -*- coding: utf-8 -*-
"""结构化聚合检索的核心逻辑（不依赖 kag 包，可离线单测）。

职责：把「自然语言的计数/列举型问题」翻译成对知识图谱的确定性聚合，
输出一段可直接复算的证据文本。

设计约束
  · 只读：生成的 Cypher 只有 MATCH / RETURN，绝不写入；
  · 确定性：同一问题 + 同一图版本 → 逐字相同的结果，不用 LLM 生成查询；
  · 可开关：由调用方决定是否启用，本模块不读环境变量。

调用方式
    core = StructAggCore(runner)          # runner(stmt, params) -> List[dict]
    text = core.build_evidence(query)     # 无法定位文档时返回 None
"""
import re
import threading
import time

PROVINCES = [
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
    "广东", "广西", "海南", "重庆", "四川", "贵州", "云南", "西藏", "陕西",
    "甘肃", "青海", "宁夏", "新疆",
]

# 类别归并规则：按顺序取第一条命中的规则，一个原始取值只归到一个规范类别
CAT_RULES = [
    ("杰出青年", ("杰出青年",)),
    ("优秀青年", ("优秀青年",)),
    ("规划", ("规划",)),
    ("重大", ("重大",)),
    ("重点", ("重点",)),
    ("青年", ("青年",)),
    ("基础", ("基础",)),
    ("应用", ("应用",)),
    ("研究报告", ("研究报告",)),
    ("论文", ("论文",)),
    ("著作", ("著作",)),
    ("自筹", ("自筹",)),
    ("资助", ("资助",)),
]

# 问题里出现的类别词 → 规范类别（长词优先）
QUERY_CAT_HINTS = [
    ("杰出青年", ("杰出青年",)),
    ("优秀青年", ("优秀青年",)),
    ("研究报告", ("研究报告", "咨询报告")),
    ("重点", ("重点",)),
    ("重大", ("重大",)),
    ("青年", ("青年",)),
    ("规划", ("规划",)),
    ("基础", ("基础",)),
    ("应用", ("应用",)),
    ("论文", ("论文",)),
]

ORG_SUFFIX = ("大学", "学院", "学校", "研究院", "研究所", "研究中心", "中心",
              "医院", "教育厅", "教育局", "委员会", "集团", "公司", "中学", "小学")

MAX_DOCS = 4          # 候选文档超过这个数就判定为「无法定位」，不出证据
MAX_LIST = 15         # 明细清单最多列这么多条


def norm(v):
    """图里的名字普遍被双引号包裹，统一剥掉。"""
    if v is None:
        return ""
    s = str(v).strip()
    while len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1].strip()
    return s


def canon_category(raw):
    s = norm(raw)
    for key, pats in CAT_RULES:
        for p in pats:
            if p in s:
                return key
    return s or "(未标注类别)"


def looks_like_org(name):
    n = norm(name)
    if not n or len(n) < 4 or len(n) > 30:
        return False
    return any(n.endswith(s) or (s in n and len(n) <= 20) for s in ORG_SUFFIX)


# 聚合意图词表：只有命中这些词，才认为问题要的是「计数/取值集合/定位某一条」，
# 而不是「办法条款怎么规定的」。用于门控模式下抑制误触发。
COUNT_WORDS = (
    "多少", "几项", "几个", "几条", "几家", "合计", "总计", "总数", "共计",
    "增加", "减少", "最多", "最少", "相差", "差额",
)
VALUESET_WORDS = ("取值", "哪些类别", "类别名称", "哪几类", "几类")
WHICH_WORDS = ("哪一项", "哪几项", "哪一个项目", "哪几个项目")


def has_agg_intent(query):
    """判断问题是否具有聚合/取值/定位意图（保守判定，宁可漏判不可误判）。"""
    q = norm(query)
    if any(w in q for w in VALUESET_WORDS):
        return True
    if any(w in q for w in WHICH_WORDS):
        return True
    return any(w in q for w in COUNT_WORDS)


class StructAggCore:
    """词表 + 解析 + 聚合 + 渲染。进程内复用，词表懒加载并缓存。"""

    def __init__(self, runner, vocab_ttl=1800):
        self.runner = runner                # (stmt, params) -> List[dict]
        self.ttl = vocab_ttl
        self._lock = threading.Lock()
        self._vocab = None
        self._ts = 0.0
        self.last_trace = []                # 供离线调试看命中了什么
        self.last_sources = []              # [(chunk_name, snippet)] 证据的真实来源块

    # ---------------- 词表 ----------------
    def vocab(self):
        with self._lock:
            if self._vocab is not None and time.time() - self._ts < self.ttl:
                return self._vocab
            self._vocab = self._build_vocab()
            self._ts = time.time()
            return self._vocab

    def _build_vocab(self):
        docs = {}
        rows = self.runner(
            "MATCH (c:`edugov4armds.Chunk`) WHERE c.name CONTAINS '_v2' "
            "AND NOT c.name CONTAINS '_split_' "
            "RETURN c.name AS n, c.content AS body", {})
        for r in rows:
            n = norm(r["n"])
            m = re.match(r"^([A-Za-z0-9]+)_v2$", n)
            if not m:
                continue
            doc = m.group(1)
            # 平台写入的 content 里换行是字面量 "\n"（反斜杠 + n），不是真换行，两种都试
            body = (r.get("body") or "").replace("\\n", "\n")
            tm = re.search(r"《[^》]{4,140}》", body)
            title = tm.group(0) if tm else ""
            years = sorted(set(re.findall(r"(?:19|20)\d{2}", title.split("（")[0] + " " + doc)))
            docs[doc] = {"title": title, "years": years,
                         "prov": next((p for p in PROVINCES if p in title), "")}

        org2doc = {}
        rows = self.runner(
            "MATCH (p)-[:`承担单位`]->(o) MATCH (p)-[:source]->(ch) "
            "RETURN DISTINCT o.name AS org, ch.name AS ch", {})
        raw_orgs = set()
        for r in rows:
            org = norm(r["org"])
            if not looks_like_org(org):
                continue
            raw_orgs.add(org)
            m = re.match(r"^([A-Za-z0-9]+)_v2", norm(r["ch"]))
            if m:
                org2doc.setdefault(org, set()).add(m.group(1))
        # 清干净的机构名（部分记录的 org 字段被拼进了页脚，如「滁州学院…第1页,共2页」）
        clean = [o for o in raw_orgs
                 if len(o) <= 20 and "页" not in o and "一览表" not in o and "共" not in o]
        clean.sort(key=len, reverse=True)

        cats = set()
        for r in self.runner("MATCH (p)-[:`项目类别`]->(c) RETURN DISTINCT c.name AS c", {}):
            cats.add(norm(r["c"]))
        org2doc = {k: v for k, v in org2doc.items()}
        return {"docs": docs, "org2doc": org2doc, "cats": cats,
                "clean_orgs": clean, "orgs_sorted": clean}

    def canon_org(self, raw):
        """把带页脚噪声的单位名归到干净的机构名；认不出就原样返回。"""
        n = norm(raw)
        if not n:
            return ""
        for cand in self.vocab()["clean_orgs"]:
            if len(cand) >= 4 and n.startswith(cand):
                return cand
        return n

    # ---------------- 解析 ----------------
    def resolve(self, query):
        v = self.vocab()
        docs = v["docs"]
        q = query or ""

        provs = {p for p in PROVINCES if p in q and any(d["prov"] == p for d in docs.values())}
        years = set(re.findall(r"(?:19|20)\d{2}", q))
        orgs = []
        rest = q
        for name in v["orgs_sorted"]:               # 长名优先，避免「浙江大学」吃掉「浙江师范大学」
            if name and name in rest:
                orgs.append(name)
                rest = rest.replace(name, "\u0000" * len(name))
        cats = []
        for key, pats in QUERY_CAT_HINTS:
            if any(p in q for p in pats) and key not in cats:
                cats.append(key)

        cand = set(docs)
        if provs:
            cand &= {d for d, m in docs.items() if m["prov"] in provs}
        if years:
            cand &= {d for d, m in docs.items() if set(m["years"]) & years}
        if orgs:
            hit = set()
            for o in orgs:
                hit |= v["org2doc"].get(o, set())
            cand &= hit

        # 文档类型提示：名单 / 结题验收 是两类不同文档，别混
        if "结题" in q or "验收" in q:
            narrowed = {d for d in cand if "结题" in docs[d]["title"] or "验收" in docs[d]["title"]}
            if narrowed:
                cand = narrowed
        elif "名单" in q:
            narrowed = {d for d in cand if "名单" in docs[d]["title"]}
            if narrowed:
                cand = narrowed

        self.last_trace = {"provs": sorted(provs), "years": sorted(years),
                           "orgs": orgs, "cats": cats, "docs": sorted(cand)}
        if not cand or len(cand) > MAX_DOCS:
            return None
        return {"docs": sorted(cand), "orgs": orgs, "cats": cats,
                "want_ids": ("编号" in q), "want_org_top": bool(
                    re.search(r"(最多|哪一所|哪个单位|哪个学校|排名|前几)", q)),
                "want_result": ("撤销" in q or "验收结果" in q or "结果" in q)}

    # ---------------- 聚合 ----------------
    def _agg(self, doc):
        """返回该文档的项目级聚合（按项目名去重，类别归一后每个项目每类只计一次）。

        项目口径：图里带「承担单位」边的节点。source 边同时连着知识单元（KU）节点
        （名字形如 `"J16LB74 负责人 刘运新 "`），直接按 source 取会把 KU 当成项目，
        导致总数虚高。所以先用承担单位边锚定项目，再按 source 归到文档。
        """
        pat = {"d": doc + "_v2"}
        rows = self.runner(
            "MATCH (p)-[:`承担单位`]->(o) MATCH (p)-[:source]->(ch) "
            "WHERE ch.name CONTAINS $d RETURN DISTINCT p.name AS p", pat)
        projects = {norm(r["p"]) for r in rows if norm(r["p"])}
        if not projects:                      # 兜底：退化为 source 直连，但滤掉 KU 名
            rows = self.runner(
                "MATCH (p)-[:source]->(ch) WHERE ch.name CONTAINS $d "
                "RETURN DISTINCT p.name AS p", pat)
            projects = {norm(r["p"]) for r in rows
                        if norm(r["p"]) and not re.search(r"\s\S+\s", norm(r["p"]))}

        cat_of = {}
        rows = self.runner(
            "MATCH (p)-[:`项目类别`]->(c) MATCH (p)-[:source]->(ch) "
            "WHERE ch.name CONTAINS $d RETURN DISTINCT p.name AS p, c.name AS c", pat)
        for r in rows:
            p = norm(r["p"])
            if p in projects:
                cat_of.setdefault(p, set()).add(canon_category(r["c"]))

        org_of = {}
        rows = self.runner(
            "MATCH (p)-[:`承担单位`]->(o) MATCH (p)-[:source]->(ch) "
            "WHERE ch.name CONTAINS $d RETURN DISTINCT p.name AS p, o.name AS o", pat)
        for r in rows:
            p = norm(r["p"])
            o = self.canon_org(norm(r["o"]))
            if p in projects and looks_like_org(o):
                org_of.setdefault(p, set()).add(o)

        res_of = {}
        rows = self.runner(
            "MATCH (p)-[:`验收结果`]->(r) MATCH (p)-[:source]->(ch) "
            "WHERE ch.name CONTAINS $d RETURN DISTINCT p.name AS p, r.name AS r", pat)
        for r in rows:
            p = norm(r["p"])
            if p in projects:
                res_of.setdefault(p, set()).add(norm(r["r"]))

        id_of = {}
        rows = self.runner(
            "MATCH (p)-[:`项目编号`]->(i) MATCH (p)-[:source]->(ch) "
            "WHERE ch.name CONTAINS $d RETURN DISTINCT p.name AS p, i.name AS i", pat)
        for r in rows:
            p = norm(r["p"])
            if p in projects:
                id_of.setdefault(p, set()).add(norm(r["i"]))
        pi_of = {}
        rows = self.runner(
            "MATCH (p)-[:`负责人`]->(i) MATCH (p)-[:source]->(ch) "
            "WHERE ch.name CONTAINS $d RETURN DISTINCT p.name AS p, i.name AS i", pat)
        for r in rows:
            p = norm(r["p"])
            if p in projects:
                pi_of.setdefault(p, set()).add(norm(r["i"]))
        return {"projects": projects, "cat_of": cat_of, "org_of": org_of,
                "res_of": res_of, "id_of": id_of, "pi_of": pi_of}

    # ---------------- 渲染 ----------------
    def build_evidence(self, query, require_intent=False):
        if require_intent and not has_agg_intent(query):
            self.last_trace = {"gated": True, "provs": [], "years": [],
                               "orgs": [], "cats": [], "docs": []}
            return None
        ctx = self.resolve(query)
        if not ctx:
            return None
        v = self.vocab()
        docs = ctx["docs"]
        orgs = ctx["orgs"]
        parts = []
        parts.append("【结构化聚合证据｜知识图谱确定性计数，同一图版本可逐项复算】")
        parts.append("文档范围：" + "；".join(
            "%s（%s）" % (d, v["docs"][d]["title"].strip("《》") or d) for d in docs))

        grand = {}
        for doc in docs:
            a = self._agg(doc)
            grand[doc] = a

        # 总体计数（单位限定 / 不限）
        if orgs:
            parts.append("单位限定：" + "、".join(orgs))
        for doc in docs:
            a = grand[doc]
            projs = a["projects"]
            if orgs:
                sub = {p for p in projs if a["org_of"].get(p, set()) & set(orgs)}
            else:
                sub = projs
            if not sub:
                parts.append("%s：单位限定下 0 项" % doc)
                continue
            if orgs:
                line = "%s（限定 %s）共 %d 项" % (doc, "、".join(orgs), len(sub))
            else:
                line = "%s 共 %d 项" % (doc, len(sub))
            if ctx["cats"]:
                detail = []
                for key in ctx["cats"]:
                    n = sum(1 for p in sub if key in a["cat_of"].get(p, set()))
                    detail.append("%s %d" % (key, n))
                line += "；其中 " + "、".join(detail)
            # 类别全貌（未限定类别时给出前几类，便于模型自行核对）
            if not ctx["cats"]:
                cnt = {}
                for p in sub:
                    for c in a["cat_of"].get(p, set()) or {"(未标注类别)"}:
                        cnt[c] = cnt.get(c, 0) + 1
                top = sorted(cnt.items(), key=lambda x: -x[1])[:8]
                if top:
                    line += "；按类别：" + "、".join("%s %d" % (c, n) for c, n in top)
            parts.append(line)

        # 多单位：逐单位 × 类别
        if len(orgs) > 1:
            for doc in docs:
                a = grand[doc]
                rows = []
                for o in orgs:
                    sub = {p for p in a["projects"] if o in a["org_of"].get(p, set())}
                    if not sub:
                        continue
                    if ctx["cats"]:
                        d = "、".join("%s %d" % (k, sum(1 for p in sub
                                                       if k in a["cat_of"].get(p, set())))
                                     for k in ctx["cats"])
                        rows.append("%s 共 %d（%s）" % (o, len(sub), d))
                    else:
                        rows.append("%s 共 %d" % (o, len(sub)))
                if rows:
                    parts.append("%s 分单位：%s" % (doc, "；".join(rows)))

        # 承担单位排名（问「最多/哪一所」时）
        if ctx["want_org_top"] and not orgs:
            for doc in docs:
                a = grand[doc]
                cnt = {}
                for p in a["projects"]:
                    for o in a["org_of"].get(p, set()):
                        cnt[o] = cnt.get(o, 0) + 1
                top = sorted(cnt.items(), key=lambda x: (-x[1], x[0]))[:5]
                if top:
                    parts.append("%s 承担单位项目数 TOP：%s" % (
                        doc, "、".join("%s %d" % (o, n) for o, n in top)))

        # 验收结果
        if ctx["want_result"]:
            for doc in docs:
                a = grand[doc]
                cnt = {}
                for p in a["projects"]:
                    for r in a["res_of"].get(p, set()):
                        cnt[r] = cnt.get(r, 0) + 1
                if cnt:
                    parts.append("%s 验收结果分布：%s" % (
                        doc, "、".join("%s %d" % (k, n) for k, n in
                                       sorted(cnt.items(), key=lambda x: -x[1]))))
                    for p in sorted(a["projects"]):
                        rs = a["res_of"].get(p, set())
                        if rs and any(("撤销" in r or "不通过" in r) for r in rs):
                            od = "、".join(sorted(a["org_of"].get(p, set()))) or "未标注"
                            pid = "、".join(sorted(a["pi_of"].get(p, set()))) or "未标注"
                            parts.append("  · %s｜验收结果 %s｜承担单位 %s｜负责人 %s" % (
                                p, "/".join(sorted(rs)), od, pid))

        # 编号清单
        if ctx["want_ids"]:
            for doc in docs:
                a = grand[doc]
                for p in sorted(a["projects"]):
                    if orgs and not (a["org_of"].get(p, set()) & set(orgs)):
                        continue
                    if ctx["cats"] and not any(k in a["cat_of"].get(p, set())
                                               for k in ctx["cats"]):
                        continue
                    ids = sorted(a["id_of"].get(p, set())) or ["(图中未挂编号)"]
                    parts.append("  · %s｜编号 %s｜类别 %s" % (
                        p, "/".join(ids),
                        "/".join(sorted(a["cat_of"].get(p, set()))) or "未标注"))
                    if len(parts) > 60:
                        break

        if orgs:
            parts.append(
                "（以上数字为知识图谱按单位「%s」逐条计数、可逐项复算的结果，"
                "请直接采用，不要从检索到的原文片段重新统计。）" % "、".join(orgs))
        else:
            parts.append(
                "（以上数字为知识图谱逐条计数、可逐项复算的结果，"
                "请直接采用，不要从检索到的原文片段重新统计。）")

        text = "\n".join(parts)
        self.last_sources = self.source_refs(docs, orgs)
        return text[:4000]

    # ---------------- 引用锚点 ----------------
    def source_refs(self, docs, orgs, limit=3):
        """返回证据所依据的真实源块 (chunk_name, snippet)。

        目的：结构化证据本身是「算出来的」，没有文档归属，平台不会回填出处；
        把真正被计数的那几个块一并带上，出处才能落到真实文档上（可追溯）。
        """
        out = []
        for d in docs:
            try:
                if orgs:
                    rows = self.runner(
                        "MATCH (p)-[:`承担单位`]->(o) "
                        "WHERE any(x IN $orgs WHERE o.name CONTAINS x) "
                        "MATCH (p)-[:source]->(ch) "
                        "WHERE ch.name CONTAINS $d AND ch.name CONTAINS '_split' "
                        "RETURN DISTINCT ch.name AS n LIMIT $k",
                        {"orgs": orgs, "d": d + "_v2", "k": limit})
                else:
                    rows = self.runner(
                        "MATCH (ch) WHERE ch.name CONTAINS $d "
                        "AND ch.name CONTAINS '_split' RETURN ch.name AS n LIMIT $k",
                        {"d": d + "_v2", "k": limit})
            except Exception:
                rows = []
            names = [r["n"] for r in rows if r.get("n")]
            if not names:
                continue
            try:
                body = self.runner(
                    "MATCH (ch) WHERE ch.name IN $names "
                    "RETURN ch.name AS n, ch.content AS c", {"names": names})
            except Exception:
                body = []
            got = {b["n"]: (b.get("c") or "") for b in body}
            for nm in names:
                out.append((nm, got.get(nm, "")[:400]))
            if len(out) >= limit:
                break
        return out[:limit]
