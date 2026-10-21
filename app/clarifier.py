"""Deterministic semantic checks for clearly underspecified query patterns."""

from __future__ import annotations

import re
from datetime import date, timedelta

from app.nl2sql.planning_models import ClarificationDecision, QueryIntent
from app.nl2sql.normalizer import NormalizeResult
from app.query_requirements import (
    QueryRequirements,
    _has_preceding_clause_content,
    extract_requirements,
)


_GROWTH_TERMS = ("增长率", "增长幅度", "增长情况", "增幅")
_MEASURE_HINTS = (
    "销售额", "销售金额", "收入", "营收", "客户数", "订单数", "订单量", "数量",
    "利润", "成本", "金额", "发票数", "销售额同比",
)
_COMPARISON_MARKERS = ("同比", "环比")
_HUMAN_PRONOUN_RE = re.compile(r"(?<![A-Za-z0-9_])(?<!其)(?:他们|她们|他|她)")
_PERSON_LABEL_RE = r"(?i:客户|顾客|用户|员工|customer|client|employee|user)"
_LATIN_PERSON_NAME_RE = re.compile(
    r"[A-Z][^\W\d_]+(?:['’\-][^\W\d_]+)*(?:\s+"
    r"[A-Z][^\W\d_]+(?:['’\-][^\W\d_]+)*)+"
)
_LABELED_LATIN_PERSON_RE = re.compile(
    _PERSON_LABEL_RE + r"\s*[:：]?\s*" + _LATIN_PERSON_NAME_RE.pattern
)
_LABELED_CHINESE_PERSON_RE = re.compile(
    r"(?:客户|顾客|用户|员工)\s*[:：]?\s*"
    r"[\u4e00-\u9fff]{2,4}(?=(?:的|是谁|先生|女士|[，,。！？\s]|$))"
)
#: 上一轮结果里**任何**已具名的单值实体（`buyer_name：清华大学`、`supplier_name：…`）。
#: 2026-10-18 gmt-03 第 2 轮实测：第 1 轮答出「buyer_name：清华大学」，
#: 第 2 轮「他的项目一共花了多少钱？」被 `_has_unresolved_person_reference` 拦下，
#: 回了「请补充这位**客户或员工**的姓名/编号」—— 库里根本没有客户或员工，
#: 用户问的是采购人。根因是上面两条正则把锚点**绑死在"客户/员工"这类标签**上。
#: 人称代词的锚点判据只该问「上文有没有一个明确的单值对象」，
#: 不该问「那个对象是不是人」—— 「他」在中文问数里指的是上一轮选中的那行记录。
_NAMED_RESULT_VALUE_RE = re.compile(
    r"[A-Za-z_][A-Za-z_0-9]*\s*[:：]\s*[\u4e00-\u9fff]{2,}|"   # buyer_name：清华大学
    r"[A-Za-z_][A-Za-z_0-9]*\s*[:：]\s*[A-Z][\w'’\-\s]{1,}"      # supplier_name：Acme
)
_EXPLICIT_PERIOD_RE = re.compile(
    r"20\d{2}(?:\s*年(?:\s*\d{1,2}\s*月)?|[-/]\d{1,2}(?:[-/]\d{1,2})?)?"
)
# 时间项的粗粒度日历锚定：把清单里的 normalized（'2023'/'2023-Q1'/'去年'/
# '2023-03'）归约成可比的年份区间，用于判断两个时间项是否真的落在不同的
# 日历范围上。锚定不了（无年份数字）返回 None，视为"粒度未知"——两个未知
# 不构成比较口径，保持追问（保守，不因解析不出而放行）。
_CALENDAR_YEAR_RE = re.compile(r"(20\d{2})")


def _anchor_time_span(normalized: str) -> str | None:
    match = _CALENDAR_YEAR_RE.search(normalized or "")
    return match.group(1) if match else None


def _has_comparison_period(
    question: str,
    normalized: NormalizeResult | None,
    requirements: QueryRequirements | None = None,
) -> bool:
    """Return whether a growth query names a comparison convention or both periods."""
    if any(marker in question for marker in _COMPARISON_MARKERS):
        return True

    periods = {
        (item.start, item.end) for item in (normalized.times if normalized else ())
    }
    # S2 修复（SC-11 误挡）：要求清单里的时间项已经把「2023年第一季度」解析成
    # 「2023-Q1」这类离散粒度（app/query_requirements.py 的 _ABSOLUTE_TIME_RE）。
    # 两个不同的时间项 = 比较口径已给足，不该再追问。判定条件收紧为
    # 「unresolved=False 的 time 项」——未解析的指代（如「那段时期」）不算数。
    # ⚠ 判据是**解析后的日历区间**，不是字面 normalized 字符串，两个坑：
    # ①「2023-Q1」与「2023-Q2」只取年份会坍缩成同一年 —— 绝对时间项的
    #   normalized 本身已带离散粒度，直接用字面值当锚点（SC-11）。
    # ②「去年」这类相对词在清单上是独立 token，但日历上可能与本期同一年
    #   （锚定 2024-06-15 时去年=2023）——同一年不算比较口径
    #   （tests/test_mainline_clarifier_memory.py::test_same_explicit_and_relative_period）。
    #   相对词的日历锚定要借助 normalizer 的 TimeExpression（start 是真日历
    #   日期），清单项本身不带日历信息。
    if requirements is not None:
        req_times = [
            item for item in requirements.of_kind("time")
            if item.actionable and item.normalized
        ]
        if len(req_times) >= 2:
            # normalizer 把相对词解析成日历区间；清单 surface 与 TimeExpression
            # 的原文对齐后能拿到真日历锚点。
            cal_by_surface: dict[str, str] = {}
            for t in (normalized.times if normalized else ()):
                surface = getattr(t, "surface", None) or getattr(t, "text", "")
                start = getattr(t, "start", None)
                if surface and start:
                    cal_by_surface.setdefault(str(surface), str(start))
            anchors: set[str] = set()
            for item in req_times:
                if item.normalized in {"去年", "今年", "明年"}:
                    # 相对词的日历锚点统一归约到**年份**（与绝对项可比，
                    # 避免把 '2023-01-01' 与 '2023' 当成两个不同区间）。
                    start = cal_by_surface.get(item.surface)
                    anchor = str(start)[:4] if start else None
                else:
                    anchor = item.normalized  # 绝对项字面即粒度（2023 / 2023-Q1）
                if anchor is None:
                    anchors = set()  # 锚定不了的不放行（保守）
                    break
                anchors.add(anchor)
            if len(anchors) >= 2:
                return True
    for match in _EXPLICIT_PERIOD_RE.finditer(question):
        numbers = [int(value) for value in re.findall(r"\d+", match.group(0))]
        if not numbers:
            continue
        year = numbers[0]
        try:
            if len(numbers) >= 3:
                start = date(year, numbers[1], numbers[2])
                end = start + timedelta(days=1)
            elif len(numbers) == 2:
                start = date(year, numbers[1], 1)
                end = date(year + (numbers[1] == 12), 1 if numbers[1] == 12 else numbers[1] + 1, 1)
            else:
                start, end = date(year, 1, 1), date(year + 1, 1, 1)
        except ValueError:
            continue
        periods.add((start.isoformat(), end.isoformat()))
    return len(periods) >= 2


def _has_unresolved_person_reference(question: str) -> bool:
    """Return whether the current turn uses 他/她 without a named person in context.

    Session memory carries the user's earlier question text, not an inferred LLM
    summary. A human pronoun is safe to continue only when that text explicitly
    names a customer/employee; a generic mention of the customer table is not an
    identity anchor.

    锚定方式二：**同句内的先行名词短语**（2026-10-16 补）
    -----------------------------------------------------
    与 `app/query_requirements._deixis_is_anchored` 的补丁同根因：中文问句里
    「A 是谁？他 B」是**陈述与追问**的自然衔接，先行语就在同句前半。

        「机房巡视检查记录表的记录人是谁？按员工手册的考勤规定，**他**的上班时间是从几点到几点？」

    这题的「他」指「记录人」，答案用户已经写在同句里。原判据只认
    「上下文里有没有 `客户:张三` 这种带标签的姓名」，于是判未解析 →
    追问「请补充这位客户或员工的姓名/编号」——而用户根本不是在问某个人，
    是在问文档里写的那个记录人。实测 RAG 套件误伤 1 题（rag-mh-16）。

    复用 `_has_preceding_clause_content` 而不是另写一套：两处的判据**必须同源**，
    否则「它」放行了而「他」没放行，同一种句式给出两种行为。
    """
    seam = "用户补充或修改："
    if seam in question:
        context, current_turn = question.rsplit(seam, 1)
    else:
        context, current_turn = "", question

    match = _HUMAN_PRONOUN_RE.search(current_turn)
    if match is None:
        return False

    if (
        _LABELED_LATIN_PERSON_RE.search(context) is not None
        or _LABELED_CHINESE_PERSON_RE.search(context) is not None
        # 上一轮只要答出过一个具名单值，「他」就有明确所指（标签不限）。
        or _NAMED_RESULT_VALUE_RE.search(context) is not None
    ):
        return False

    # 同句先行语：指代词前面已有实质内容，说明"他"有明确所指。
    return not _has_preceding_clause_content(current_turn, match.start())


class SemanticClarifier:
    """Ask for only the missing dimension in a narrow, explicitly supported pattern."""

    #: 追问话术里"例如……"用到的兜底对象名。**只在调用方没给出当前库的真实对象时**
    #: 才会出现，所以它必须是**跨库都成立**的说法，不能默认成某一套库的表。
    #:
    #: 2026-10-18 修：原先这两处话术写死「（如客户、发票或员工）」，
    #: 那是 chinook 的业务对象。政采库（askdata_gov）上追问时照样说
    #: 「客户、发票或员工」—— 库里一张都没有，等于把用户往错方向引。
    #: 现在改成由调用方按当前 schema 传入真实对象名（`object_labels`），
    #: 没传时用下面这组中性说法兜住。
    _FALLBACK_OBJECT_LABELS: tuple[str, ...] = ()

    def _object_examples(self, object_labels: tuple[str, ...] | None) -> str:
        """把对象名列表拼成话术里的「（如 A、B 或 C）」，最多取 3 个。"""
        labels = tuple(
            label for label in (object_labels or self._FALLBACK_OBJECT_LABELS) if label
        )
        if not labels:
            return ""
        picked = labels[:3]
        if len(picked) == 1:
            body = picked[0]
        else:
            body = "、".join(picked[:-1]) + " 或 " + picked[-1]
        return "（如" + body + "）"

    def decide(
        self,
        question: str,
        normalized: NormalizeResult | None = None,
        requirements: QueryRequirements | None = None,
        inherited: QueryRequirements | None = None,
        object_labels: tuple[str, ...] | None = None,
    ) -> ClarificationDecision:
        """Decide whether this turn needs a clarifying question.

        Args:
            question: 本轮原话。
            normalized: 要素识别结果，沿用既有分支。
            requirements: 要求清单（`app/query_requirements.py`）。`None` 时**现场补抽一次** ——
                这样老调用点不传也能拿到新检查；显式传 `QueryRequirements()`（空清单）
                可以关掉基于清单的分支，测试用。
            inherited: 从上一轮条件档案继承来的要求（S2-F/F3）。接续轮才有值。
                **只用于"缺对象"判定**：用户第 1 轮说了「员工」，第 2 轮
                「男的有多少？」不必再问一次要统计什么。非接续轮传 `None`。
            object_labels: 当前业务库里**真实存在**的业务对象说法，用于追问话术的
                「例如……」。不传则不举例子（话术仍然完整可读）。**只能是当前 schema
                里的对象** —— 举一个库里没有的例子比不举更糟。
        """
        text = (normalized.original if normalized else question or "").strip()
        intent = self._intent(text, normalized)

        if len(text) < 4:
            return ClarificationDecision(
                status="ask",
                question="请补充要查询的对象或指标。",
                missing_slots=("object_or_measure",),
                reason_code="question_too_short",
                intent=intent,
            )

        # 接续轮里 `inherited` 非空，说明上一轮已经确定了统计对象，
        # 「他的项目……」的「他」指的就是它 —— 这本身就是最强锚点。
        # 2026-10-18 gmt-03 第 2 轮：历史里存的是**问题原文**，不含上一轮的
        # 答案（`buyer_name：清华大学`），所以光靠 `_NAMED_RESULT_VALUE_RE`
        # 扫文本扫不到；档案才是那条信息的真正载体。
        if _has_unresolved_person_reference(text) and (inherited is None or not inherited.actionable):
            return ClarificationDecision(
                status="ask",
                question="请补充这位客户或员工的姓名/编号，我再继续查询。",
                missing_slots=("person_reference",),
                reason_code="unresolved_person_reference",
                intent=intent,
            )

        # 清单在增长率分支之前就要有：_has_comparison_period 要用它判断
        # 「两个不同的已解析时间项 = 比较口径已给足」（SC-11 误挡修复）。
        clist = requirements if requirements is not None else extract_requirements(text, normalized)

        if any(term in text for term in _GROWTH_TERMS):
            missing: list[str] = []
            if not any(hint in text for hint in _MEASURE_HINTS):
                missing.append("measure")
            if not _has_comparison_period(text, normalized, clist):
                missing.append("comparison_period")
            if missing:
                prompts = []
                if "measure" in missing:
                    prompts.append("需要比较哪项指标（如销售额、客户数或订单数）")
                if "comparison_period" in missing:
                    prompts.append("比较时间范围是什么（如同比、环比或具体年份）")
                return ClarificationDecision(
                    status="ask",
                    question="请补充" + "，以及".join(prompts) + "。",
                    missing_slots=tuple(missing),
                    reason_code="growth_query_missing_dimensions",
                    intent=intent,
                )
            # 增长率问句已经自己核过"指标 + 比较口径"两个维度，且都齐了。
            # 就此打住 —— 再往下走会被通用检查用"没提到业务对象"追问一遍，
            # 而「销售额同比增长率是多少？」本来就没打算限定在某张表上。
            return ClarificationDecision(status="continue", intent=intent)

        unresolved = tuple((normalized.unresolved if normalized else ())[:3])
        # ⚠ 只认「语义候选存在歧义」，**不认「疑似错别字但存在多个改写目标」**。
        #
        # 两种告警来自 normalizer 的两个不同 pass，可信度差一个量级：
        #
        #   「语义候选存在歧义」（normalizer.py:868，C 层 semantic）——
        #       候选是**回库校验过**的真实体，两边分数接近才报。
        #       这是真歧义，该追问。
        #
        #   「疑似错别字但存在多个改写目标」（normalizer.py:586，B 层 fuzzy）——
        #       来自 2 字滑窗的编辑距离命中，**normalizer 自己都不据此改写**
        #       （见 :589-594 的注释：两字模糊匹配会撞上复合词里的普通字）。
        #       把它当追问理由等于把 normalizer 主动丢弃的低置信信号
        #       升级成硬阻断。实测误伤 5 题：RAG 题里的「单机」「单位」「单集」
        #       被当成「单价/单据/单曲」的错别字候选，于是
        #       「单机 GPU 显存是多少？」被追问「你指的是哪个对象或指标」。
        #
        # 两个模块对 `unresolved` 的理解原本是错配的：
        #   normalizer 的意图是「我注意到了，但我不敢改」；
        #   澄清器原先按「我搞不清，必须问用户」处理。
        # 错别字提示只该留给用户当参考，不该阻断问答 —— 用户问的是
        # 「显存是多少」，追问「单机指什么」是答非所问。
        if any("候选存在歧义" in item for item in unresolved):
            return ClarificationDecision(
                status="ask",
                question="问题中的业务词存在多个可能含义，请说明你指的是哪个对象或指标。",
                missing_slots=("ambiguous_business_term",),
                reason_code="normalizer_ambiguity",
                intent=intent,
            )

        decision = self._decide_from_requirements(
            clist, intent, inherited, object_labels=object_labels
        )
        if decision is not None:
            return decision

        return ClarificationDecision(status="continue", intent=intent)

    def _decide_from_requirements(
        self,
        clist: QueryRequirements,
        intent: QueryIntent,
        inherited: QueryRequirements | None = None,
        object_labels: tuple[str, ...] | None = None,
    ) -> ClarificationDecision | None:
        """基于要求清单的补充检查。**只负责"加追问"，不改已有判定。**

        放在最后一道，是因为它比上面几条更宽：上面每条都针对一个显式句型，
        这里针对"清单缺了一块"。顺序反了会把既有话术顶掉（既有话术已经在
        T5/多轮回归里被验证过），所以宁可排在后面、只在前面都放行时才生效。

        `inherited` 是上一轮条件档案里仍生效的要求（S2-F/F3）。它**只**参与
        "有没有对象"这一件事，不改任何其它判定：

            「公司有多少员工？」→「男的有多少？」
            第 2 轮本轮清单里只有 measure + value_filter，没有 object。
            若只看本轮，"没有对象"成立，会追问"要统计什么" —— 而用户
            上一轮已经说了是员工。加进继承后才不会误问。

        返回 `None` 表示这套清单没有发现需要追问的地方。
        """
        # 「本轮 ∪ 继承」判定对象是否存在。继承条在本轮清单里被标
        # `source="session"`（见 `app/query_requirements.py::_augment_from_inherited`），
        # 因此这里把两边的对象类要求并起来看。
        has_object = clist.has_object or self._inherited_has_object(inherited)

        # ⚠ 未解析指代检查**必须排在 document_scope 豁免之前**。
        # 早先版本先判 document_scope 就 return None，导致「这份合同怎么规定？」
        # 「该合同的质保期是多少？」里已经提取出的 unresolved 对象被整份丢掉，
        # 直接放行到下游。指代没落实时，无论问的是不是文档，都该先追问 ——
        # 全新会话里「这份/该」没有先行词，放行等于让系统自己挑一份文档。
        #
        # ⚠ 这一条**只看本轮**，不看继承：指代（"它""这份"）没有先行词是
        # 本轮的问题，上一轮的对象不能替它当先行词 —— 那样「它的质保期是多久？」
        # 会被上一轮的"客户"顶包，追问就失效了。
        unresolved_objects = tuple(
            item for item in clist.of_kind("object") if item.unresolved
        )
        if unresolved_objects:
            # 「它」/「这份」—— 有指代但没落实。与"根本没提对象"是两种毛病，
            # 话术也不同：这里要用户点明"它"是谁，而不是重问要查什么。
            return ClarificationDecision(
                status="ask",
                question="请说明「" + unresolved_objects[0].surface + "」指的是哪个对象或哪份文档。",
                missing_slots=("resolved_reference",),
                reason_code="unresolved_reference",
                intent=intent,
            )

        if clist.of_kind("document_scope") or clist.of_kind("cross_source_object"):
            # 文档问句与跨源问句的对象不在业务表里，这里不适用"缺对象"判定。
            # 判据用清单里的 `document_scope`（"合同/条款/规定"），
            # 而不是 `scope`（那是**用户显式声明的数据源**，两者不是一回事）。
            #
            # 注意豁免的只是**下面两条**（ranking_without_object / measure_without_object）：
            # 「合同里怎么规定的？」没有业务对象是正常的，不该追问。
            # 而指代未落实是另一回事，上面已经拦掉了。
            return None

        if clist.of_kind("ranking") and not has_object:
            # 「帮我看下最多的那个。」「订单里金额最高的那张」—— 要排序却没说要排谁。
            # **排在 `has_measure` 之前**：这类问句里"金额/数量"也会命中 measure，
            # 但用户真正要做的是"取前几名"，问"要统计什么对象"答非所问。
            # 也**不能**随口报候选表名 —— "那"字句里用户心里多半已有对象，
            # 报错表名比不报更糟。SC-05 就是这个场景。
            return ClarificationDecision(
                status="ask",
                question=(
                    "请补充要对哪个业务对象排序或取前几名"
                    + self._object_examples(object_labels)
                    + "。"
                ),
                missing_slots=("object",),
                reason_code="ranking_without_object",
                intent=intent,
                options=tuple(object_labels or ()),
            )

        if clist.has_measure and not has_object and not clist.has_located_measure:
            # 「有多少位？」—— 要一个数却没说要数什么。
            # 这是 SC-02/SC-05，也是问数里最容易"猜错还答得很像"的一类。
            #
            # ⚠ `has_located_measure` 这半个条件是 2026-10-17 补的，起因是政采题集
            # gov-m-07「按采购方式分组，各自的中标金额平均值是多少」被拦：
            # 那句话里一个表名都没提（"中标金额"是 `awards.award_yuan` 的列说明，
            # 不是表），于是按"缺对象"追问，可用户其实已经点名要算哪个字段了。
            # 判据落在**列说明**上而不是再加一张词表，理由见 `_locus_of_measure`。
            return ClarificationDecision(
                status="ask",
                question=(
                    "请补充要统计的业务对象"
                    + self._object_examples(object_labels)
                    + "。"
                ),
                missing_slots=("object",),
                reason_code="measure_without_object",
                intent=intent,
                options=tuple(object_labels or ()),
            )

        if self._relative_period_on_person(clist):
            # SC-08：「去年有多少员工？」—— 「去年」落在人身上有两个口径
            # （去年在职 vs 去年新入职），employee 表只有 hire_date，
            # 静默选一个就是乙类丢弃口径。SC-09（「去年新入职员工」）带了
            # 「新入职」这个明确语义词，不会进这条分支。
            # 判据刻意收窄到"人 + 计数"：发票/客户的时间过滤语义唯一
            # （按开票/注册日期），追问反而误伤。
            return ClarificationDecision(
                status="ask",
                question="「去年」的口径请确认：统计去年内新入职的员工，还是去年期间在职的员工？",
                missing_slots=("time_semantics",),
                reason_code="time_semantics_ambiguous",
                intent=intent,
            )

        return None

    @staticmethod
    def _inherited_has_object(inherited: QueryRequirements | None) -> bool:
        """继承清单里是否有**可用的**对象要求（S2-F/F3）。

        只认 `actionable` 的条目：继承来的对象若本身还带指代未解析
        （`unresolved=True`），它替不了本轮的对象 —— 否则「它的质保期是多久？」
        会被上一轮某个同样没落实的对象顶包。

        用 `source` 区分继承条不是必需的（调用方已经把其它来源排除干净），
        这里靠 `actionable` 就够，避免把判定绑在字段取值上。
        """
        if inherited is None:
            return False
        return any(item.actionable for item in inherited.of_kind("object", "cross_source_object"))

    @staticmethod
    def _relative_period_on_person(clist: QueryRequirements) -> bool:
        """SC-08 判定：裸相对年份 + 人 + 计数，且没有明确语义词。"""
        objects = {item.surface for item in clist.of_kind("object") if item.actionable}
        person_terms = {"员工", "雇员", "职工", "职员", "employee"}
        if not objects & person_terms:
            return False
        if not clist.of_kind("measure"):
            return False
        # 有明确语义词（新入职/在职/离职/入职）就不追问 —— 那是 SC-09。
        original = clist.original or ""
        for marker in ("新入职", "在职", "离职", "入职", "招聘"):
            if marker in original:
                return False
        # 有绝对锚点（"2024年的员工"）也不追问：年份语义唯一。
        relative = tuple(
            item for item in clist.of_kind("time")
            if item.normalized in {"去年", "今年", "明年"}
        )
        absolute = tuple(
            item for item in clist.of_kind("time")
            if item.normalized not in {"去年", "今年", "明年"}
            and item.actionable and item.normalized
        )
        return bool(relative) and not absolute

    @staticmethod
    def _intent(question: str, normalized: NormalizeResult | None) -> QueryIntent:
        entities = tuple(dict.fromkeys(item.canonical for item in (normalized.entities if normalized else [])))
        times = tuple(item.surface for item in (normalized.times if normalized else []))
        explicit_measures = tuple(term for term in _MEASURE_HINTS if term in question)
        measures = tuple(dict.fromkeys(explicit_measures + tuple(value for value in entities if value in _MEASURE_HINTS or any(
            term in value for term in ("amount", "total", "price", "quantity", "revenue", "count")
        ))))
        objects = tuple(value for value in entities if value not in measures)
        target_grain = next((
            item.canonical for item in (normalized.entities if normalized else [])
            if item.kind == "table"
        ), None)
        return QueryIntent(
            objects=objects,
            measures=measures,
            time_expressions=times,
            target_grain=target_grain,
            source_question=question,
            confidence=0.5 if entities or times else 0.0,
        )


__all__ = ["SemanticClarifier"]
