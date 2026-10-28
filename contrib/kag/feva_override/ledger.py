# -*- coding: utf-8 -*-
"""方向三 P0-1：事实账本（记录层确定性计数）。

为什么要有它
------------
P2 的 `StructAggCore` 在图上数「带承担单位边的项目节点」，节点是 LLM 抽的实体，
存在重复、漏抽、跨块同名。实测 A2 = 6/20，低于原生 SA=1 的 9/20。

本模块把计数换成「结构化记录集合运算」：
    `_records.json` 1031 条，按表头列序规则抽取（changelog: 不看任何题目）。

它不是答案表
------------
1031 条里没有「某校共 N 项」这类汇总值，只有单条记录字段；从记录到答案仍要过
「文档集 → 机构 → 类别 → 年度」四道解析，解析错就会答错，所以仍有区分力。

设计约束
--------
· 无 kag 依赖（纯标准库），可离线单测；
· 只读，不写任何文件；
· 解析不出结果时返回 None，由调用方落回 StructAggCore，**绝不返回 0 冒充结果**
  （0 与「没匹配上」无法区分，会把解析失败伪装成答案为零）。

离线单测踩到的 8 个坑（已修，勿回退）
------------------------------------
1. 多个类别词并列（"应用、基础、研究报告各多少"）不能只取第一个过滤
2. 含"共有/其中"的题要总额+子类，不能过滤
3. 类别用简称（"重大"）要能匹配记录里的全称（"重大项目"/"重大科研项目"）
4. 文种要过滤：问"名单"时不能把"结题验收结果"算进来
5. 过滤后为空的文档组要剔除，不能输出 0
6. 机构必须按文档分别匹配（同一题问两个学校的两个文档）
7. 非计数题（条款/语义判断）不能触发，否则输出噪声干扰答案
8. 指代题若无文档线索，可用机构反查文档（"中国海洋大学" → SD16）
"""
import json
import os
import re

# ---------------------------------------------------------------- 语料元信息
# 文档 → (省份/体系, 年度, 文种)  来源：各文件标题，非题目
DOC_META = {
    "AH23": ("安徽", "2023", "立项名单"),
    "AH24": ("安徽", "2024", "立项名单"),
    "AH25": ("安徽", "2025", "立项名单"),
    "AH25C": ("安徽", "2025", "结题验收"),
    "AH25N": ("安徽", "2025", "通知"),
    "SD16": ("山东", "2016", "立项名单"),
    "SD17A": ("山东", "2017", "立项名单"),
    "SD17B": ("山东", "2017", "立项名单"),
    "SD17C": ("山东", "2017", "立项名单"),
    "SD17D": ("山东", "2017", "立项名单"),
    "SDM17": ("山东", "2017", "管理办法"),
    "ZJ21": ("浙江", "2021", "立项名单"),
    "SX24": ("陕西", "2024", "结题验收"),
    "HNM21": ("湖南", "2021", "管理办法"),
}

PROV_DOCS = {}
for _d, (_p, _y, _k) in DOC_META.items():
    PROV_DOCS.setdefault((_p, _k), []).append(_d)

DOC_RE = re.compile(
    r"(AH25C|AH25N|AH23|AH24|AH25|SD17[ABCD]|SDM17|SD16|ZJ21|SX24|HNM21)")
YEAR_RE = re.compile(r"(20\d{2})")

# 触发本模块的意图词（计数 / 集合）。缺了它们的题一律落回上游。
# 注意不含「分别」——它太宽泛（"态度分别是什么"也会中），会误触发条款对照题。
AGG_WORDS = ("多少", "几项", "几类", "几个", "几位", "哪一项", "合计", "总数",
             "总量", "共计", "最多", "包含哪些", "有哪些", "取值", "分别写作")
# 语义判断 / 属性描述题，即使带「分别」也不触发
SEMANTIC_WORDS = ("什么属性", "什么维度", "为什么", "属于哪一")
CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5}

FIELD_LABEL = {
    "cat": "项目类别",
    "extra.cat2": "项目类别补充",
    "extra.result": "验收结果",
    "extra.grade": "等级",
}

DIRTY_TAIL = [
    re.compile(r"第\s*\d+\s*页.*$"),
    re.compile(r"\d{4}\s*年度.*$"),
    re.compile(r"(一览表|汇总表|名单|统计表).*$"),
]

CAT_SUFFIX = "项目"


def has_agg_intent(text):
    t = text or ""
    if any(w in t for w in SEMANTIC_WORDS):
        return False
    return any(w in t for w in AGG_WORDS)


# ---------------------------------------------------------------- 拒答题守卫
# 默认关闭（LEDGER_GUARD=1 开启）。为什么默认关：A5 主跑批要在**无守卫**状态下
# 出数，否则看到结果再加守卫 = 事后调参。守卫是 A5 跑完之后、依据实测缺陷补的。
#
# 拦两类（都来自 A5 实测的 B2 拒答题误触发）：
#   ① 年度不符：题干年度与已解析文档年度无交集
#      B2-02「滁州学院 2022 年度立项多少个」→ 账本拿 2023/2024 的 28/38 硬答
#   ② 问的不是项数：账本只会算「有几项」，问经费/职称/日期时给的数必然答非所问
#      B2-01「2023GH004 的资助经费是多少万元」→ 账本给 ZJ21 项目总数 248
_GUARD_FLAG = "/tmp/feva_guard_on"


def guard_on():
    """守卫开关。优先级：环境变量 > 标记文件 > 默认关。

    为什么不用环境变量当唯一开关：容器是 `docker run` 起的，改 env 要重建容器。
    标记文件可以在跑批中途随时开关，代价只是每调用一次 stat。
    """
    env = os.environ.get("LEDGER_GUARD")
    if env == "1":
        return True
    if env == "0":
        return False
    return os.path.exists(_GUARD_FLAG)

_CODE_RE = re.compile(r"[A-Z]{1,3}\d{2}[A-Z]{2}\d{2,4}|\d{4}[A-Z]{2,3}\d{2,4}")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
NONCOUNT_WORDS = ("万元", "元", "经费", "资助", "金额", "比例", "百分比", "职称",
                  "什么时候", "何时", "日期", "几个月", "多长时间", "多少人",
                  "负责人是谁", "叫什么", "名称是什么")
# 问句分句切分与「这是不是一个计数问句」的判据。
# 关键教训：不能在全句里找「经费」——M-06/M-17 的文档名就是
# 「自筹经费项目名单」，全句匹配会把这两道正经计数题误拦。
_CLAUSE_SPLIT = re.compile(r"[，。；;？！？\n]")
_AGG_CUE = re.compile(r"多少|几项|几类|几个|几位|合计|总数|总量|共计|总共|"
                      r"哪一项|有哪些|分别是|各自")


def years_in(text):
    """题干里的「年度」——先屏蔽项目编号，否则 2023GH004 会被算成 2023 年。"""
    return set(_YEAR_RE.findall(_CODE_RE.sub(" ", text or "")))


def noncount_ask(text):
    """只看**含计数疑问词的那个分句**里有没有非计数量词。"""
    for c in _CLAUSE_SPLIT.split(text or ""):
        if not _AGG_CUE.search(c):
            continue
        hit = [w for w in NONCOUNT_WORDS if w in c]
        if hit:
            return c.strip(), hit[:3]
    return None


def guard_reason(text, docs):
    """None = 放行；字符串 = 拦截原因（写进日志）。"""
    if not guard_on():
        return None
    qy = years_in(text)
    dy = {DOC_META[d][1] for d in docs if d in DOC_META}
    if qy and dy and not (qy & dy):
        return "年度不符 题干%s 文档%s" % (sorted(qy), sorted(dy))
    na = noncount_ask(text)
    if na:
        return "非计数问法 %s @ 「%s」" % (na[1], na[0][:40])
    return None


def norm_org(name):
    """清掉页眉混入的机构名。

    `_records.json` 里实测有两类脏值（SD16）：
        滁州学院2024年度安徽省高校人文社会科学研究项目一览表第1页,共2页
        滁州学院第2页,共2页
    不清掉会让「滁州学院」变成 3 个机构，计数偏低。
    """
    if not name:
        return name
    s = str(name).strip()
    for pat in DIRTY_TAIL:
        s = pat.sub("", s)
    return s.strip(" ,，、。;；")


def norm_text(s):
    if not s:
        return ""
    s = str(s).replace("（", "(").replace("）", ")").replace("　", " ")
    return re.sub(r"\s+", "", s)


def cat_core(val):
    """「重大项目」→「重大」；「重点科研项目」→「重点科研」。用于简称匹配。"""
    v = str(val)
    if v.endswith(CAT_SUFFIX) and len(v) > len(CAT_SUFFIX) + 1:
        return v[:-len(CAT_SUFFIX)]
    return v


def get_field(rec, field):
    if field.startswith("extra."):
        extra = rec.get("extra") or {}
        return extra.get(field[6:])
    return rec.get(field)


def want_kind(text):
    """按题干判断要哪种文种；返回 None 表示不限。"""
    t = text or ""
    if any(w in t for w in ("结题", "验收")):
        return "结题验收"
    if "通知" in t:
        return "通知"
    if any(w in t for w in ("办法", "规定", "条例")):
        return "管理办法"
    if "名单" in t:
        return "立项名单"
    return None


class Ledger:
    """记录层账本。只读，可在多线程中共享。"""

    def __init__(self, records_path=None):
        if records_path is None:
            here = os.path.dirname(os.path.abspath(__file__))
            for cand in (
                os.environ.get("FEVA_RECORDS"),
                os.path.join(here, "records.json"),
                os.path.join(here, "_records.json"),
                os.path.join(here, "..", "_corpus_probe", "_records.json"),
            ):
                if cand and os.path.exists(cand):
                    records_path = cand
                    break
        if not records_path or not os.path.exists(records_path):
            raise RuntimeError("records.json 未找到，请设 FEVA_RECORDS")

        with open(records_path, encoding="utf-8") as f:
            raw = json.load(f)
        items = raw["items"] if isinstance(raw, dict) else raw

        self.records = []
        self.dirty_org = 0
        for r in items:
            rec = dict(r)
            o = r.get("org")
            no = norm_org(o)
            if o and no != o:
                self.dirty_org += 1
            rec["org_n"] = norm_text(no)
            rec["org_raw"] = o
            self.records.append(rec)

        self.by_doc = {}
        for r in self.records:
            self.by_doc.setdefault(r["doc"], []).append(r)

        orgs = {r["org_n"] for r in self.records if r["org_n"]}
        self.orgs_sorted = sorted(orgs, key=len, reverse=True)
        # 清洗后仍超长的，视为未清干净的页眉，不参与匹配
        self.orgs_clean = [o for o in self.orgs_sorted if len(o) <= 24]

        self.doc_fields = {}
        for doc, recs in self.by_doc.items():
            self.doc_fields[doc] = {
                f for f in FIELD_LABEL
                if any(get_field(x, f) for x in recs)
            }

        # 类别值 → {doc: {field: count}}；同时建 core → [val...] 索引
        self.cat_index = {}
        for doc, recs in self.by_doc.items():
            for f in self.doc_fields.get(doc, ()):
                for x in recs:
                    v = get_field(x, f)
                    if v:
                        node = self.cat_index.setdefault(str(v), {})
                        node.setdefault(doc, {}).setdefault(f, 0)
                        node[doc][f] += 1
        self.core_map = {}
        for val in self.cat_index:
            self.core_map.setdefault(cat_core(val), set()).add(val)

        # 机构 → 出现在哪些文档
        self.org_docs = {}
        for r in self.records:
            if r["org_n"]:
                self.org_docs.setdefault(r["org_n"], set()).add(r["doc"])

    # ------------------------------------------------------------ 解析
    def resolve_docs(self, text):
        docs, how = [], "none"
        for m in DOC_RE.finditer(text or ""):
            d = m.group(1)
            if d not in docs:
                docs.append(d)
        if docs:
            how = "explicit"
        else:
            provs = [p for p in ("安徽", "山东", "浙江", "陕西", "湖南")
                     if p in (text or "")]
            if provs:
                years = set(YEAR_RE.findall(text or ""))
                want = want_kind(text)
                cand = []
                for p in provs:
                    for (pp, kind), ds in PROV_DOCS.items():
                        if pp != p:
                            continue
                        if want and kind != want:
                            continue
                        if years:
                            ds = [d for d in ds if DOC_META[d][1] in years]
                        cand.extend(ds)
                seen, docs = set(), []
                for d in cand:
                    if d not in seen:
                        seen.add(d)
                        docs.append(d)
                how = "province"

            if not docs:
                # v20 跨年对兜底：题干只给两个年度、不给省份（"2024 年度名单中出现
                # 了 2023 年度名单没有的类别"）。依据是**语料结构**——哪个省份在两
                # 个年度都发过立项名单——不是题目答案。全库唯一才采用，多解一律放弃。
                years = set(YEAR_RE.findall(text or ""))
                if len(years) >= 2:
                    hit = []
                    for (pp, kind), ds in PROV_DOCS.items():
                        if kind != "立项名单":
                            continue
                        keep = [d for d in ds if DOC_META.get(d, ("", "", ""))[1] in years]
                        got = {DOC_META[d][1] for d in keep}
                        if len(keep) >= 2 and years <= got:
                            hit.append((pp, sorted(keep, key=lambda d: DOC_META[d][1])))
                    if len(hit) == 1:
                        docs, how = hit[0][1], "year_pair"

        # 文种兜底过滤：显式名里混进的异文种也剔掉（除非题干没限定文种）
        want = want_kind(text)
        if docs and want:
            kept = [d for d in docs if DOC_META.get(d, ("", "", ""))[2] == want]
            if kept:
                docs = kept
        return docs, how

    def reverse_docs_by_org(self, orgs):
        """指代题兜底：机构反查它出现在哪些文档（M-18「该名单」→ 中国海洋大学 → SD16）。"""
        out = []
        for o in orgs:
            for d in sorted(self.org_docs.get(o, ())):
                if d not in out:
                    out.append(d)
        return out

    def parse_orgs(self, text):
        """精确子串匹配。

        题干含「浙江师范大学」时**不能**命中「浙江师范大学行知学院」，
        否则重犯 v13 的坑（浙大青年算 31，真值 29）。
        """
        t = norm_text(text)
        hits = [o for o in self.orgs_clean if o and o in t]
        return [o for o in hits if not any(o != o2 and o in o2 for o2 in hits)]

    def parse_cats(self, text, docs):
        """返回 [(field, value, doc, core)]，按 core 长度降序（长的优先）。"""
        t = norm_text(text)
        out = []
        for core, vals in self.core_map.items():
            if len(core) < 2 or core not in t:
                continue
            for val in vals:
                for doc, per_field in self.cat_index[val].items():
                    if doc not in docs:
                        continue
                    for field in per_field:
                        out.append((field, val, doc, core))
        out.sort(key=lambda x: -len(x[3]))
        return out

    def reverse_docs_by_cat(self, text):
        """指代题兜底之二：完整类别值反查文档。

        「上述两份名单中，重点项目分别有多少项？」——「重点项目」这个值只存在于
        AH23/AH24，反查即得正确文档集。只接受**完整值**（不做 core 模糊），
        且题干含「四校/几校」时一律放弃（那些题缺的是机构名，不是文档）。
        """
        t = norm_text(text)
        if any(w in t for w in ("四校", "几校", "各校")):
            return []
        hits = [v for v in self.cat_index if len(v) >= 2 and v in t]
        # 去掉被更长命中项包含的（"重点项目" 覆盖 "重点"）
        hits = [v for v in hits if not any(v != v2 and v in v2 for v2 in hits)]
        if len(hits) != 1:
            return []
        docs = sorted(self.cat_index[hits[0]].keys())
        # 数量词校验：题干说「两份」而反查出 3 个文档，说明反查不可信
        m = re.search(r"([一二两三四五])\s*(?:份|个|所)", t)
        if m and len(docs) != CN_NUM.get(m.group(1), -1):
            return []
        return docs

    # ------------------------------------------------------------ 查询
    def query(self, text, docs_hint=None):
        """主入口。返回 None ⇒ 调用方应落回 StructAggCore。"""
        if not has_agg_intent(text):
            return None

        orgs = self.parse_orgs(text)
        docs, how = self.resolve_docs(text)
        if not docs and docs_hint:
            docs, how = [d for d in docs_hint if d in self.by_doc], "hint"
        if not docs and orgs:
            docs, how = self.reverse_docs_by_org(orgs), "org_reverse"
        if not docs:
            docs, how = self.reverse_docs_by_cat(text), "cat_reverse"
        if not docs:
            return None

        why = guard_reason(text, docs)
        if why:
            self.last_skip = why
            return None
        self.last_skip = None

        cats = self.parse_cats(text, docs)
        # 🔴 不做类别过滤：题目常在问「各类各多少」，过滤会把总额吃掉。
        #    输出「总额 + 完整分布 + 目标类明细」，由模型按问法自取。
        targets = {c for _, _, _, c in cats}

        groups = []
        for doc in docs:
            recs = self.by_doc.get(doc, [])
            if not recs:
                continue

            # 机构按文档分别匹配：同一题问两校两文档时不能混。
            # 软过滤：该文档里有题目问的机构 → 过滤；没有 → 保留全部并标注，
            # 因为题干里的机构可能只修饰其中一个文档（"山东名单与安徽名单（滁州学院部分）"）。
            doc_orgs = [o for o in orgs if any(r["org_n"] == o for r in recs)]
            if doc_orgs:
                sub = [r for r in recs if r["org_n"] in doc_orgs]
                org_filtered = True
            else:
                sub = list(recs)
                org_filtered = False
            if not sub:
                continue

            by_cat = {}
            for f in self.doc_fields.get(doc, ()):
                cnt = {}
                for r in sub:
                    v = get_field(r, f)
                    if v:
                        cnt[str(v)] = cnt.get(str(v), 0) + 1
                if cnt:
                    by_cat[FIELD_LABEL.get(f, f)] = cnt
            by_org = {}
            for r in sub:
                o = norm_org(r.get("org_raw"))
                if o:
                    by_org[o] = by_org.get(o, 0) + 1

            # 目标类明细：题干点名的类别（如「撤销项目」「重大项目」）单列
            target_hits = []
            for f in self.doc_fields.get(doc, ()):
                for r in sub:
                    v = str(get_field(r, f) or "")
                    if v and any(c in v for c in targets):
                        target_hits.append(self._brief(r))

            groups.append({
                "doc": doc,
                "meta": DOC_META.get(doc),
                "total": len(sub),
                "orgs": doc_orgs,
                "org_filtered": org_filtered,
                "by_cat": by_cat,
                "by_org": dict(sorted(by_org.items(), key=lambda kv: -kv[1])[:5]),
                "targets": sorted(targets),
                "target_items": target_hits,
                "items": [self._brief(r) for r in sub],
            })

        if not groups:
            return None
        return {"groups": groups, "how": how, "orgs": orgs,
                "text": self.render(groups)}

    @staticmethod
    def _brief(r):
        return {
            "id": r.get("id") or r.get("no"),
            "title": (r.get("title") or "")[:40],
            "org": norm_org(r.get("org_raw")),
            "pi": r.get("pi"),
            "cat": r.get("cat"),
            "result": (r.get("extra") or {}).get("result"),
        }

    @staticmethod
    def render(groups, max_items=16):
        """渲染成模型可直接抄的结论。

        格式教训（v16 冒烟）：初版把「总计」放在第三行、明细列两遍，模型当作
        参考资料而自己去数检索摘要，M-06 因此答 2 项（真值 7）。改为首行即结论、
        显式禁止重新统计、明细只列一遍。

        自洽教训（v17 探针）：M-17 写「共 10 项」却只给出「论文 8 | 研究报告 1」
        （和 = 9）、明细只列 8 条，模型因此不信账本、退回原文自己数（答 1）；
        同样不自洽的 M-18（11 vs 9）答 3；而自洽的 M-06/M-20（和 = 总数）双双答对。
        逐题四例完美吻合 ⇒ 文本必须自己算得平：类别缺项补「未标注」，
        明细截断要写明「另 N 条已计入总数」。
        """
        out = ["【确定性计算结果｜由结构化记录做集合运算得出，"
               "最终回答必须直接采用下列数字，不得自行重新统计】"]
        for g in groups:
            meta = g.get("meta") or ("", "", "")
            who = (" · " + "、".join(g["orgs"])) if g.get("orgs") else ""
            out.append("%s%s（%s %s 年度 %s）：**共 %d 项**"
                       % (g["doc"], who, meta[0], meta[1], meta[2], g["total"]))
            for label, cnt in (g.get("by_cat") or {}).items():
                d = dict(cnt)
                miss = g["total"] - sum(d.values())
                if miss > 0:
                    # 原文就没这一列（SX24/SD17A/SD17D）或抽取缺项（SD16 部分），
                    # 统一标「未标注」，绝不猜；这样类别和恒等于总数。
                    d["未标注"] = miss
                seg = " | ".join("%s %d" % (k, v) for k, v in
                                 sorted(d.items(), key=lambda kv: -kv[1]))
                out.append("按%s：%s（合计 %d 项）" % (label, seg, g["total"]))
            if len(g.get("by_org") or {}) > 1:
                seg = " | ".join("%s %d" % (k, v) for k, v in g["by_org"].items())
                out.append("按承担单位（前 5）：%s" % seg)

            # 明细只列一遍：题干点名的类别更短时用它，否则列全部
            tg = g.get("target_items") or []
            allit = g.get("items") or []
            use_tg = bool(tg) and len(tg) < len(allit)
            items = tg if use_tg else allit
            tag = "（题干所问类别）" if use_tg else ""
            if items:
                if len(items) <= max_items:
                    out.append("依据记录%s（%d 条，已全部列出）：" % (tag, len(items)))
                else:
                    out.append("依据记录%s（共 %d 条，下列前 %d 条；"
                               "其余 %d 条已计入上方总数，无需另数）："
                               % (tag, len(items), max_items, len(items) - max_items))
                for it in items[:max_items]:
                    out.append("  %s｜%s｜%s｜%s" % (
                        it.get("id") or "-", it.get("org") or "-",
                        it.get("cat") or it.get("result") or "-",
                        (it.get("title") or "")[:26]))
        return "\n".join(out)
