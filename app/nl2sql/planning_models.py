"""Private typed records used by the question-planning pipeline.

These records are internal implementation details. The HTTP contract in
``app/contracts.py`` stays unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ClarificationStatus = Literal["continue", "ask", "out_of_scope"]
SemanticCheckStatus = Literal["pass", "repair", "unknown"]

#: 一条用户要求属于哪一类。取值刻意与「实现怎么查」解耦 ——
#: 这里记的是**用户要什么**，不是「SQL 里应该出现什么」。
#: 同一条要求可以由不同的 SQL 写法满足（等价形式），判定属于后续阶段的事。
RequirementKind = Literal[
    "object",              # 查哪一类实体（客户 / 员工 / 发票…）
    "measure",             # 算什么指标（数量 / 合计 / 平均…）
    "time",                # 时间范围
    "value_filter",        # 取值筛选（国家、类别、编号…）
    "numeric_range",       # 数值比较（大于 100、不小于 100…）
    "negation",            # 否定（不在…、除了…）
    "grouping",            # 分组维度
    "ranking",             # 排序与前 N 条
    "distinct_grain",      # 去重粒度
    "document_scope",      # 文档范围
    "cross_source_object",  # 跨源对象（工单编号、合同编号…）
    # ── 以下两类为 S2 新增（`requirement_id` 与它们配套） ──────────
    "value_scope",         # 条件作用域：筛选/分组/排名/去重分别落在哪个层级
    "join_grain",          # 关联粒度：按哪张表的粒度汇总，避免一对多重复展开
]

#: 一条要求的核对结论。**刻意不复用 `SemanticCheckStatus`**：
#: 那套是给「SQL 结构是否合法」用的；这里是「用户的要求有没有被 SQL 落实」。
#: 两者都可能取 `unknown`，但含义不同 —— 前者是"看不懂 SQL"，
#: 后者是"看懂了，但证不出用户这条要求被满足"。
RequirementOutcome = Literal["pass", "repair", "unknown"]


@dataclass(frozen=True)
class QueryRequirement:
    """用户原话里的一条明确要求，带得回原文的出处。

    **为什么 `span` 与 `surface` 必须同时存在**：`surface` 说明"用户说了什么"，
    `span` 说明"这句话在原话的哪个位置"。只留 `surface` 的话，一旦改写环节
    把某个词换掉，就再也说不清这条要求是从原文的哪一处来的；而
    「不能只从改写结果提取」（实施方案 §5.1 第 1 条）正是针对这一点。
    """

    kind: RequirementKind
    surface: str
    span: tuple[int, int]
    normalized: str = ""
    #: 有实质约束信号、但**当前解析不了**。它不等于"没有这条要求"：
    #: 实施方案 §5.1 第 7 条明确「没有匹配到规则不等于已经证明问题没有条件」。
    #: 置位后由澄清环节决定追问还是停止，绝不静默丢弃。
    unresolved: bool = False
    #: 这条要求的来源，决定它可不可信：
    #:   literal —— 用户本轮原话里字面写着（可信）
    #:   session —— 从已确认的会话条件继承（可信，但须能回到确认动作）
    #:   default —— 系统补的默认值（**不可冒充用户提供**，见 §5.1 第 2 条）
    source: str = "literal"
    #: 跨模块稳定标识。**用原文 span 生成**而不是序号：
    #: 序号在"补抽/合并清单"时会漂移，span 只要原文不变就唯一。
    #: 空串表示"未编号"（旧调用方构造的实例），核对器遇到空串时
    #: 用 `span` 现场补一个，不阻断。
    requirement_id: str = ""
    #: 这条要求是否**必须**被满足。False 表示"最好满足，但不满足也不算错"
    #: （例如系统从会话上下文补的提示性要求）。核对器对 `required=False`
    #: 的要求不产 `repair` —— 否则一次提示会打坏整条 SQL。
    required: bool = True
    #: 时间要求的**确定区间**，左闭右开 `(start, end)`，ISO 日期字符串。
    #:
    #: 只有 `kind == "time"` 且要素识别能算出日历边界时才有值；其余情况为 None。
    #: 单独开这个字段而不是复用 `normalized`，是因为 `normalized` 对相对时间
    #: 存的是**原词**（「去年」），而原词无从比对 SQL 里的日期字面量 —— 核对器
    #: 2026-10-17 前正因此只能判「无法静态证明等价」并放行，写错区间的 SQL 拦不住。
    #: 默认 None 保证老调用点不传也照常工作，核对器遇 None 退回原有判据。
    time_range: tuple[str, str] | None = None

    @property
    def actionable(self) -> bool:
        """是否是一条可用于执行的要求。未解析的不算。"""
        return not self.unresolved

    @property
    def id(self) -> str:
        """对外统一取标识。没编号就按 span 现算一个（同轮内可复现）。"""
        if self.requirement_id:
            return self.requirement_id
        return f"{self.kind}:{self.span[0]}-{self.span[1]}"


@dataclass(frozen=True)
class QueryRequirements:
    """一轮提问的完整要求清单 —— 提取器与判定方共用的同一份快照。

    实施方案 §5.1 的表格要求「每轮使用同一份快照，避免提取器与执行器
    各自理解」。所以本类型一经构造即不可变，下游只读。
    """

    version: str
    original: str
    normalized_question: str
    #: 本轮明确的来源范围（逻辑数据源身份）。空串表示"未声明，用默认问数库"。
    #: 实施方案 §5.2 明确：界面上"存在三个来源"**不**等于用户选了三个来源，
    #: 不能据此把单库问答悄悄变成跨库合计。
    scope: str = ""
    requirements: tuple[QueryRequirement, ...] = ()
    #: 提取到的、但无法归类的原话片段。留给澄清与留痕，不允许丢掉。
    unresolved_spans: tuple[str, ...] = ()
    #: 题面里的指标说法能否落到**某个具体列**上（口径见 `app/query_requirements.py`
    #: 的 `_locus_of_measure`）。
    #:
    #: 这个标志是为了把两件看起来一样、实则不同的事分开：
    #:   - 「有多少位？」—— 指标是"数量"，但数什么完全没说，必须追问；
    #:   - 「各地市中标金额的平均值是多少？」—— 指标已经点名到 `awards.award_yuan`，
    #:     一追问就是误伤。
    #:
    #: 与 `has_object` **刻意是两个判据**：`has_object` 看的是"题面有没有提到某张表
    #: 对应的业务对象"（客户/发票/项目），而用户完全可以在不提表名的情况下
    #: 直接点名要算哪个字段。默认 False 保证老调用点行为不变。
    measure_locus: bool = False

    def of_kind(self, *kinds: RequirementKind) -> tuple[QueryRequirement, ...]:
        """按类别取要求。判定方读清单的唯一入口，避免各自遍历出错。"""
        wanted = set(kinds)
        return tuple(item for item in self.requirements if item.kind in wanted)

    @property
    def actionable(self) -> tuple[QueryRequirement, ...]:
        return tuple(item for item in self.requirements if item.actionable)

    @property
    def has_object(self) -> bool:
        return bool(self.of_kind("object", "cross_source_object"))

    @property
    def has_measure(self) -> bool:
        return bool(self.of_kind("measure"))

    @property
    def has_located_measure(self) -> bool:
        """指标是否已经落到具体的列上。

        澄清器用它区分「要一个数但没说要数什么」与「已经点名了要算哪个字段」。
        与 `has_object` 分开：前者说的是指标有落点，后者说的是题面提到某张表。
        """
        return self.measure_locus

    @property
    def time(self) -> tuple[QueryRequirement, ...]:
        return self.of_kind("time")


@dataclass(frozen=True)
class QueryIntent:
    """A bounded interpretation of what the user asks the database to return."""

    objects: tuple[str, ...] = ()
    measures: tuple[str, ...] = ()
    time_expressions: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    filters: tuple[str, ...] = ()
    target_grain: str | None = None
    source_question: str = ""
    confidence: float = 0.0


@dataclass(frozen=True)
class ClarificationDecision:
    status: ClarificationStatus
    question: str = ""
    missing_slots: tuple[str, ...] = ()
    reason_code: str = ""
    intent: QueryIntent = field(default_factory=QueryIntent)
    #: 本轮的要求清单快照。`None` 表示"本轮未经要求提取"（旧调用方、假件测试），
    #: 与"提取过但没提到任何要求"（空清单）是两回事，不能混为一谈。
    requirements: QueryRequirements | None = None
    #: 澄清时**建议给出的可选项**。实施方案 §5.3 要求「优先给数据实际支持的选项」——
    #: 让用户从系统真能算的口径里选，而不是自由填一个系统算不出来的词。
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class LinkCandidate:
    """One table or column candidate and the evidence that made it relevant."""

    object_type: Literal["table", "column"]
    schema: str
    table: str
    column: str | None
    score: float
    score_parts: dict[str, float] = field(default_factory=dict)
    matched_terms: tuple[str, ...] = ()
    relation_only: bool = False

    @property
    def qualified_name(self) -> str:
        if self.column:
            return f"{self.schema}.{self.table}.{self.column}"
        return f"{self.schema}.{self.table}"


@dataclass(frozen=True)
class LinkResult:
    selected_tables: tuple[LinkCandidate, ...] = ()
    selected_columns: tuple[LinkCandidate, ...] = ()
    relation_tables: tuple[LinkCandidate, ...] = ()
    ambiguous_groups: tuple[tuple[str, ...], ...] = ()
    degraded: bool = False
    degradation_reason: str = ""
    metadata_version: str = ""


@dataclass(frozen=True)
class JoinEdge:
    left_schema: str
    left_table: str
    left_columns: tuple[str, ...]
    right_schema: str
    right_table: str
    right_columns: tuple[str, ...]
    source: str
    join_type: Literal["inner", "left"] = "inner"
    cardinality: Literal["many_to_one", "one_to_many", "one_to_one", "unknown"] = "unknown"


@dataclass(frozen=True)
class JoinPlan:
    target_grain: str | None = None
    tables: tuple[str, ...] = ()
    edges: tuple[JoinEdge, ...] = ()
    measure_expressions: tuple[str, ...] = ()
    filters: tuple[str, ...] = ()
    candidate_paths: tuple[tuple[str, ...], ...] = ()
    ambiguous: bool = False
    warnings: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    #: 只靠“某张表拥有命中的列”（`owner_of_column`）进入定位结果的表。
    #:
    #: **语义与 `tables` 不同**：`tables` 是校验器要求的必用表（缺了就 repair），
    #: 而本字段只是“允许出现”的候选。字段归属本身**不构成业务关联依据**
    #: （见 `linker.py` 的注释），所以这类表既不能被当成必用表，也不该被丢掉。
    #: 当一张表同时出现在两者中时，以 `tables` 的必用语义为准。
    optional_tables: tuple[str, ...] = ()


@dataclass(frozen=True)
class SemanticCheckResult:
    status: SemanticCheckStatus
    reason_code: str = ""
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# S2 要求覆盖核对：从「用户要什么」到「SQL 有没有做到」的中间层
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedRequirement:
    """一条要求绑定到具体表/列之后的**可判定形态**。

    提取器只知道"用户说了 2023 年"；核对器要回答"这个 2023 落在哪张表的
    哪个列上、用什么运算符、闭区间还是开区间"。这一步做不出来（例如库里
    根本没有客户注册时间列）时，`resolution` 为空，核对结论只能是 `unknown`
    或 `repair` —— **绝不能因为没有列就默认通过**。

    ⚠ 本类型是**绑定**，不是"证明 SQL 做到了"。做到没有要看
    `RequirementCheck`（在 SQL 树上逐项核对）。
    """

    requirement_id: str
    #: 绑定时依据的是哪一条要求（保留原文 span，便于回查）。
    requirement: QueryRequirement | None = None
    #: 绑定依据的来源标识，便于在 trace 里说清"凭什么绑到这张表"。
    source_id: str = ""
    #: 候选表（按可信度降序）。空元组表示"没有候选"。
    table_candidates: tuple[str, ...] = ()
    #: 最终选定的列 `schema.table.column`。空串 = 没绑上。
    column: str = ""
    #: 归一化后的运算符，如 `>`、`>=`、`=`、`!=`、`between`。
    operator: str = ""
    #: 归一化后的比较值（字符串形式，便于 trace 展示）。
    normalized_value: str = ""
    #: 单位（元/天/年…），空串表示无量纲。
    unit: str = ""
    #: 时间类要求的闭区间 `(start, end)`，两者都可为空串。
    time_bounds: tuple[str, str] = ()
    #: 汇总粒度（按哪张表 / 哪个键汇总）。用于一对多风险判定。
    target_grain: str = ""
    #: 绑定结论：`bound` / `unbound` / `ambiguous`。
    #: `ambiguous` 表示多张表都能满足，需要调用方按业务规则挑或降级。
    resolution: str = "unbound"
    #: 绑定不上的**具体原因**。空串表示没有额外说明。
    reason_code: str = ""


@dataclass(frozen=True)
class RequirementCheck:
    """一条要求在 SQL 上的核对结论。"""

    requirement_id: str
    outcome: RequirementOutcome
    reason_code: str = ""
    message: str = ""
    #: SQL 里承载这条要求的表达式片段（`sql()` 还原文本），便于人工核对。
    sql_scope: str = ""
    #: 绑定信息。`unbound` 时也会带上，方便说明"想绑但绑不上"。
    resolved: ResolvedRequirement | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RequirementCoverage:
    """一轮 SQL 的全部要求核对结果。

    `status` 的聚合顺序**刻意固定**：任一 `repair` → repair；否则任一
    `unknown` → unknown；否则 pass。调换顺序会让"有 repair 但也有 unknown"
    的情况显示成 unknown，从而把该拦的问题降级成"再看看"。
    """

    version: str
    checks: tuple[RequirementCheck, ...] = ()

    def of_outcome(self, *outcomes: RequirementOutcome) -> tuple[RequirementCheck, ...]:
        wanted = set(outcomes)
        return tuple(item for item in self.checks if item.outcome in wanted)

    @property
    def status(self) -> RequirementOutcome:
        if any(item.outcome == "repair" for item in self.checks):
            return "repair"
        if any(item.outcome == "unknown" for item in self.checks):
            return "unknown"
        return "pass"

    @property
    def unknown(self) -> tuple[RequirementCheck, ...]:
        return self.of_outcome("unknown")

    @property
    def repaired(self) -> tuple[RequirementCheck, ...]:
        return self.of_outcome("repair")
