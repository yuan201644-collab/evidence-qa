"""跨轮条件档案：把"这一轮用户要什么"从字符串拼接收成可对账的结构。

## 要解决的真实问题

现在的多轮是**把两句原文拼起来**交给下游（`app/memory.py::_combine` →
`原句 + "\\n用户补充或修改：" + 本轮`）。这个做法在两个方向上都会出错：

    第 1 轮：2023年有多少客户？
    第 2 轮：只看巴西的呢？
    → 拼接结果里 2023 与巴西**同时存在**，但用户其实是**替换**了范围条件。

    第 1 轮：员工有多少？
    第 2 轮：改成按职位分组
    → 拼接文本里没有任何机制阻止下游把"员工"当成新的对象条件。

拼接文本无法表达"这一条是被替换的""这一条已经过期"。本模块提供
**可对账的结构**：逐条记录条件、它的来源轮次、以及它现在的处境
（in_force / dropped / stale）。

## 三条硬纪律

1. **不猜**。判定解析不出操作类型时记 `unknown`，合并策略退化为
   "旧的全留 + 新的全加"，并标记 `degraded=True`。绝不因为"猜不准"
   就悄悄丢掉旧条件 —— 那是乙类缺陷（用户说了、系统静默丢弃）。
2. **过期要显式**。换对象时（"那客户呢"）上一轮对象专属的条件标 `stale`
   而不是删除；显式否定（"不看巴西"）标 `dropped`。两者都不再进 SQL，
   但都留痕，让核验卡能说明"为什么这一轮的结果和上一轮不一样"。
3. **只在接续轮生效**。`merge()` 由调用方判断是否接续；非接续轮直接
   返回新档案。继承本身不是目的，"该继承时继承、该丢弃时声明"才是。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.nl2sql.planning_models import QueryRequirement, QueryRequirements

#: 条件在本轮里的处境。
ConditionState = Literal[
    "in_force",   # 本轮生效
    "dropped",    # 用户显式取消（"不看巴西了"）—— 不进 SQL，留痕
    "stale",      # 换对象后已不适用（"那客户呢"）—— 不进 SQL，留痕
]

#: 本轮相对上一轮做了哪种操作。取值刻意与母方案 §5.5 的八种操作对齐。
MergeOperation = Literal[
    "add",              # 增加一个条件（旧条件全留）
    "replace_value",    # 替换同类取值条件（"只看巴西"替掉"只看美国"）
    "replace_measure",  # 改指标
    "replace_object",   # 换对象
    "replace_group",    # 换分组维度
    "drop",             # 显式取消某条件
    "unknown",          # 判定不出 —— 退化为 add，且标记 degraded
]


#: 与对象无关的条件类别。换对象时它们**不该**跟着失效。
#:   - measure：统计口径（多少/平均/合计）换对象后通常仍然成立
#:     （「客户有多少」→「那员工呢」= 员工有多少，用户没改口径）。
#:   - grouping / ranking：同理，"按职位分组"换个对象往往还成立。
#: 反过来，time / value_filter / numeric_range / negation 这类**限定条件**
#: 一律挂在当时那个对象上 —— 「2023年有多少客户」换成员工后，
#: 2023 这个年份**不能**默默跟过去（Chinook 的员工没有 2023 相关列，
#: 跟过去会变成静默的条件错配）。
_OBJECT_INDEPENDENT_KINDS = frozenset({"measure", "grouping", "ranking"})


@dataclass(frozen=True)
class ArchivedCondition:
    """档案里的一条条件，带它从哪一轮来、现在是否生效。"""

    requirement: QueryRequirement
    turn: int
    state: ConditionState = "in_force"
    #: 加入这条条件时**在生效的对象键**（`object:<normalized>`）。
    #: 空串表示当时没有已知对象（或本条就是对象本身）。
    #: 用途：换对象时判断这条限定条件是不是"只能挂在旧对象上"。
    object_key: str = ""

    @property
    def key(self) -> str:
        """同类条件的归并键。取值条件按 (kind, normalized) 归并，
        这样"只看巴西"能替掉"只看美国"（同 kind，不同 normalized）。"""
        return f"{self.requirement.kind}:{self.requirement.normalized or self.requirement.surface}"

    @property
    def active(self) -> bool:
        return self.state == "in_force"

    def is_bound_to(self, object_key: str) -> bool:
        """这条条件是否绑在给定对象上（且它本身不是对象、也不与对象无关）。"""
        if not object_key or not self.object_key:
            return False
        if self.requirement.kind in _OBJECT_INDEPENDENT_KINDS:
            return False
        if self.requirement.kind in ("object", "cross_source_object"):
            return False
        return self.object_key == object_key


@dataclass(frozen=True)
class ConditionArchive:
    """一个会话的条件档案快照。不可变，合并产出新实例。"""

    turn: int = 0
    conditions: tuple[ArchivedCondition, ...] = ()
    #: 最近一轮被接续的原文，用于展示与调试。
    last_question: str = ""
    #: 合并时判定不出操作 → True。调用方据此决定是否降级处理。
    degraded: bool = False
    #: 本轮判定出的操作，供 trace 留痕。
    operation: MergeOperation = "add"

    # ── 读取 ────────────────────────────────────────────────────────────
    @property
    def active_conditions(self) -> tuple[ArchivedCondition, ...]:
        return tuple(item for item in self.conditions if item.active)

    @property
    def active_requirements(self) -> tuple[QueryRequirement, ...]:
        return tuple(item.requirement for item in self.active_conditions)

    @property
    def stale_conditions(self) -> tuple[ArchivedCondition, ...]:
        return tuple(item for item in self.conditions if item.state == "stale")

    @property
    def dropped_conditions(self) -> tuple[ArchivedCondition, ...]:
        return tuple(item for item in self.conditions if item.state == "dropped")

    @property
    def active_objects(self) -> tuple[str, ...]:
        """本轮生效的对象名（surface 原文）。澄清缺对象判定要用。"""
        return tuple(
            item.requirement.surface
            for item in self.active_conditions
            if item.requirement.kind in ("object", "cross_source_object")
        )

    def to_payload(self) -> dict[str, Any]:
        """给 trace 用。**只带可展示字段**，不含任何原始数据。"""
        return {
            "turn": self.turn,
            "operation": self.operation,
            "degraded": self.degraded,
            "conditions": [
                {
                    "kind": item.requirement.kind,
                    "surface": item.requirement.surface,
                    "normalized": item.requirement.normalized,
                    "turn": item.turn,
                    "state": item.state,
                }
                for item in self.conditions
            ],
        }


#: 显式取消的说法。命中即把对应条件标 dropped。
_DROP_PATTERNS = (
    "不看", "不要", "不限", "去掉", "去掉", "取消", "排除", "忽略", "不用",
)

#: 换对象的说法。命中且能提出新对象 → 旧对象专属条件标 stale。
_OBJECT_SWITCH_MARKERS = ("那", "换成", "改为", "改成", "看看")


def _is_drop_intent(text: str) -> bool:
    """本轮是否在显式取消某个条件。

    ⚠ 只认**明确的取消词**。「不看巴西」算，「巴西呢」不算 —— 后者
    更可能是**替换**（把国家条件从美国换成巴西），交给 `_merge_conditions`
    按同类键归并处理。这里判错会直接丢条件，所以宁可窄。
    """
    folded = text.strip()
    if not folded:
        return False
    return any(word in folded for word in _DROP_PATTERNS)


#: 范围收窄的说法。命中表示用户在本轮**重新圈定范围**而不是叠加条件。
_SCOPE_NARROWING_WORDS = ("只看", "只查", "只统计", "仅看", "仅查", "只限", "只看下")


def _is_scope_narrowing(text: str) -> bool:
    """本轮是否在**重新圈定范围**（「只看巴西的呢？」）。

    这类句子表达的是"把范围换成这个"，不是"在这个基础上再加一个条件"。
    实测踩过的坑：第 2 轮「那去年的呢？」把 `去年`（normalizer 锚成 `2025`）
    带进档案，第 3 轮「只看巴西的呢？」本意是换成只看巴西，却继承了时间
    条件 —— 答案里多出一段用户没问的时间口径说明，还与第 2 轮"没有时间
    依据"的诚实回答互相矛盾。
    """
    folded = (text or "").strip()
    if not folded:
        return False
    return any(word in folded for word in _SCOPE_NARROWING_WORDS)


def _is_limiting(requirement: QueryRequirement) -> bool:
    """这条条件是不是**限定范围**的（时间/取值/数值/否定）。

    与 `_OBJECT_INDEPENDENT_KINDS` 互补：统计口径（多少/平均）换范围时
    仍然成立；而时间与取值筛选**就是范围本身**，用户重新圈定范围时
    旧的要让位给新的。
    """
    return requirement.kind in ("time", "value_filter", "numeric_range", "negation")


def _switch_object(new_q: QueryRequirements, archive: ConditionArchive | None) -> bool:
    """本轮是否**换成了另一个**对象。

    ⚠ 判据是「换了」不是「提了」。只看本轮有没有 object 是错的：
    「不看巴西了，全部客户有多少？」重申的还是 customer —— 它跟上一轮
    是同一个对象，把它判成换对象会让 2023 这个年份条件被误标 stale
    （实测踩过：turn3 把旧对象和 time 一起标 stale，而用户根本没换对象）。

    所以要比对本轮对象键与档案里的对象键，**不同才算换**。
    """
    incoming_key = _object_key_of(new_q)
    if not incoming_key:
        return False
    old_key = _object_key_of(archive)
    return bool(old_key) and incoming_key != old_key


#: 相对时间词。它们与绝对年份表达的是**同一个条件**，只是粒度不同。
_RELATIVE_TIME_WORDS = ("去年", "今年", "明年", "上个月", "本月", "上周", "本季度", "今天", "至今")


def _is_time_requirement(requirement: QueryRequirement) -> bool:
    return requirement.kind == "time"


def _time_conflicts(old: QueryRequirement, new: QueryRequirement) -> bool:
    """两个时间要求是不是**同一件事的两种说法**。

    实测过的真实情形：第 2 轮用户说「那去年的呢？」，normalizer 把「去年」
    按当前日历锚成 `2025 年`；而档案里第 1 轮就存着 `去年` 这个相对词。
    若两条都留着，档案里同时存在「去年」与「2025 年」——下游会以为是
    两个时间条件叠加，而用户只提了一个。

    判据（任一成立即视为同一条件）：
      - 一条是相对词（去年/今年…），另一条不是 → 它们说的是同一段时间，
        保留本轮那条（它带 normalizer 的日历锚定，更具体可执行）。
      - 两条 normalized 完全相同。
    """
    if not (_is_time_requirement(old) and _is_time_requirement(new)):
        return False
    if old.normalized and old.normalized == new.normalized:
        return True
    old_relative = any(word in (old.surface or "") for word in _RELATIVE_TIME_WORDS)
    new_relative = any(word in (new.surface or "") for word in _RELATIVE_TIME_WORDS)
    # 一相对一绝对 → 同一个条件
    return old_relative != new_relative


def merge_condition_archive(
    archive: ConditionArchive | None,
    new_requirements: QueryRequirements,
    *,
    question: str = "",
    is_followup: bool = True,
) -> ConditionArchive:
    """把本轮清单并入档案，产出新档案。

    Args:
        archive: 上一轮档案；`None` 表示会话刚开始。
        new_requirements: 本轮的要求清单（由 `extract_requirements` 产出）。
        question: 本轮用户原话（判定取消/替换意图用）。
        is_followup: 本轮是否接续。False 时**丢弃旧档案**（新话题不留旧条件）。

    Returns:
        新档案。判定不出操作时 `degraded=True`，且**旧条件一条不丢**。
    """
    if archive is None or not is_followup:
        return _fresh_archive(new_requirements, turn=(archive.turn + 1 if archive else 1))

    turn = archive.turn + 1
    #: ⚠ **只把本轮用户真说过的条件当"新的"**。
    #: `extract_requirements` 的 `_augment_from_inherited` 会把上一轮仍生效的
    #: 条件也塞进同一份清单（标 `source="session"`、`span=(0,0)`），如果这里
    #: 照单全收，继承来的条件就会被当成"用户本轮又提了一次"，于是永远替换
    #: 不掉 —— 实测踩过：第 2 轮的 `去年`（被 normalizer 锚成 `2025`）以
    #: session 源混进第 3 轮，档案里它就成了"本轮新增"，`只看巴西的呢？`
    #: 想收窄范围也甩不掉它，答案里多出一段用户没问的时间口径说明。
    #: 真正的继承应当**只来自档案**（`archive.conditions`），不来自清单回灌。
    #:
    #: 判据用 `span == (0, 0)` 而不是 `source == "session"`：词表命中的取值
    #: 条件（「只看巴西」→ `Brazil`）也走 `source="session"`（`via=alias` 的
    #: 依据是会话词表而非本轮字面），但它的 span 指向用户真说出的「巴西」
    #: 二字 —— 它是本轮的新条件，必须进档案，否则换对象时它没有 object
    #: 绑定、下一轮既继承不了也判不了 stale（实测踩过：t2 的巴西条件整个
    #: 没入档，t3 的 SQL 把 Brazil 带进了员工查询）。span=(0,0) 只有
    #: `_augment_from_inherited` 会写，判它不会误伤词表条件。
    incoming = tuple(
        item for item in new_requirements.actionable
        if item.source != "session" or item.span != (0, 0)
    )
    inherited_only = tuple(
        item for item in new_requirements.actionable
        if item.source == "session" and item.span == (0, 0)
    )
    if not incoming:
        # 本轮没提出新条件（纯追问"那呢？"）→ 档案原样带过，轮次递增。
        return ConditionArchive(
            turn=turn,
            conditions=archive.conditions,
            last_question=question or archive.last_question,
            degraded=archive.degraded,
            operation=archive.operation,
        )

    drop_intent = _is_drop_intent(question)
    switching = _switch_object(new_requirements, archive)
    #: 本轮是否重新圈定范围（「只看巴西的呢？」）。见 `_is_scope_narrowing`。
    narrowing = _is_scope_narrowing(question)

    incoming_keys = {_merge_key(item) for item in incoming}
    kept: list[ArchivedCondition] = []
    stale_marks: list[ArchivedCondition] = []
    dropped_marks: list[ArchivedCondition] = []
    restated_keys: set[str] = set()

    #: 本轮在生效的对象键（换对象/新提对象时就是新的那个）。
    #: 本轮新增的限定条件都绑到它上面，供**下一轮**换对象时判定失效。
    current_object_key = _object_key_of(new_requirements) or _object_key_of(archive)
    #: 被换掉的那个旧对象键 —— 绑在它上面的限定条件要转 stale。
    old_object_key = _object_key_of(archive) if switching else ""

    for old in archive.conditions:
        req = old.requirement
        # 已经在历史里非生效的条件保持原状（stale 不会自己活过来）
        if not old.active:
            kept.append(old)
            continue

        # ① 显式取消：本轮取消词 + 提到了同类条件的 surface/normalized
        #    ⚠ **重述要优先于取消**。「不看巴西了，全部客户有多少？」里
        #    "客户"和"多少"都被本轮重述了 —— 它们是用户**重新指定**的，
        #    不是被取消的。曾经顺序反了，结果档案里显示 object/measure
        #    先 dropped 又被 add 回去（生效集合虽然对，但对账记录误导）。
        if drop_intent and _mentions(question, req) and not _is_restatement(question, req, ""):
            dropped_marks.append(ArchivedCondition(req, old.turn, "dropped", old.object_key))
            continue

        # ② 换对象：旧对象本体、以及**绑在旧对象上的限定条件**，一并标 stale。
        #    ⚠ 这里必须用 `is_bound_to` 而不是只判 kind —— 只判 kind 会让
        #    「2023年有多少客户？」里的 2023 在换成员工后默默跟过去
        #    （实测踩过：`time 2023` 跟进了员工查询）。
        if switching:
            if req.kind in ("object", "cross_source_object"):
                if not any(
                    item.kind in ("object", "cross_source_object") and item.surface == req.surface
                    for item in incoming
                ):
                    stale_marks.append(ArchivedCondition(req, old.turn, "stale", old.object_key))
                    continue
            elif old.is_bound_to(old_object_key) and not _is_restatement(question, req, ""):
                stale_marks.append(ArchivedCondition(req, old.turn, "stale", old.object_key))
                continue

        # ②b 时间条件的相对/绝对同一性：第 2 轮「去年」被 normalizer 锚成
        #     `2025 年`，与档案里的 `去年` 是同一个条件。两条都留会让下游
        #     以为是两个时间条件叠加。
        if any(_time_conflicts(req, item) for item in incoming):
            dropped_marks.append(ArchivedCondition(req, old.turn, "dropped", old.object_key))
            continue

        # ②c 重新圈定范围：本轮说了「只看X」，且 X 是**同类**限定条件
        #     （旧的是 time、新的是 value_filter 也算同"范围"）→ 旧的让位。
        #     判据刻意窄：只在本轮确实带来新的限定条件、且旧条**没被重述**
        #     时生效。用户若只是想加一个条件（没有"只"字）不会走到这里。
        if narrowing and _is_limiting(req) and not _is_restatement(question, req, ""):
            if any(_is_limiting(item) for item in incoming):
                dropped_marks.append(ArchivedCondition(req, old.turn, "dropped", old.object_key))
                continue

        # ③ 同类条件的两种情形，**必须分开处理**：
        #    重述（本轮又说了一遍同一个值）→ 旧条留着，本轮新条不重复加；
        #    换值（本轮说了同类的另一个值）→ 旧条标 dropped，本轮新条生效。
        #    判据见 `_is_restatement`；判错方向会静默丢条件，所以偏向"重述"。
        if _merge_key(req) in incoming_keys:
            if _is_restatement(question, req, _merge_key(req)):
                # 重述：旧的继续生效，且本轮不再往 added 里放重复项
                kept.append(old)
                restated_keys.add(_merge_key(req))
                continue
            dropped_marks.append(ArchivedCondition(req, old.turn, "dropped", old.object_key))
            continue

        kept.append(old)

    added = [
        ArchivedCondition(item, turn, "in_force", current_object_key)
        for item in incoming
        if not (
            item.kind in {c.requirement.kind for c in kept}
            and _is_restatement(question, item, _merge_key(item))
            and _merge_key(item) in restated_keys
        )
    ]

    operation = _decide_operation(
        archive, incoming, kept, stale_marks, dropped_marks, drop_intent, switching
    )
    degraded = operation == "unknown"

    conditions = tuple(kept) + tuple(stale_marks) + tuple(dropped_marks) + tuple(added)
    return ConditionArchive(
        turn=turn,
        conditions=conditions,
        last_question=question or archive.last_question,
        degraded=degraded,
        operation=operation,
    )


def _fresh_archive(requirements: QueryRequirements, *, turn: int) -> ConditionArchive:
    object_key = _object_key_of(requirements)
    return ConditionArchive(
        turn=turn,
        conditions=tuple(
            ArchivedCondition(item, turn, "in_force", object_key)
            for item in requirements.actionable
        ),
        last_question=requirements.original,
        degraded=False,
        operation="add",
    )


def _object_key_of(source: QueryRequirements | ConditionArchive | None) -> str:
    """取当前生效对象的归并键（`object:<normalized>`），没有则空串。

    从 `QueryRequirements` 与 `ConditionArchive` 都能取 —— 合并时前者是
    "本轮说的对象"，后者是"档案里有效的对象"，两者语义一致。
    """
    if source is None:
        return ""
    if isinstance(source, ConditionArchive):
        for item in source.active_conditions:
            if item.requirement.kind in ("object", "cross_source_object"):
                return f"object:{item.requirement.normalized or item.requirement.surface}"
        return ""
    for item in source.actionable:
        if item.kind in ("object", "cross_source_object"):
            return f"object:{item.normalized or item.surface}"
    return ""


def _merge_key(requirement: QueryRequirement) -> str:
    """同类条件的归并键。

    ⚠ **必须是 (kind, normalized)，不能只看 kind**：
      - 「只看巴西」→`Brazil`、「只看美国」→`USA`，键不同 → 两条共存是对的。
      - 但**重述同一条件**（「客户有多少」→「那客户呢」）键相同 ——
        此时旧条**不该被丢弃**，它是同一件事的另一次说法。

    所以调用方拿到「键相同」时不能直接判替换，必须再区分
    "重述" 与 "换值"。判据见 `_is_restatement`：**本轮原文里出现了旧条
    的 surface 或 normalized** 就是重述（用户又把同一个词说了一遍）。
    """
    return f"{requirement.kind}:{requirement.normalized or requirement.surface}"


def _is_restatement(question: str, old: QueryRequirement, incoming_key: str) -> bool:
    """本轮是**重述**了旧条件，还是**换成了**另一个同类值？

    - 重述：「客户有多少」→「那客户呢」——本轮原话说出了 customer/客户。
    - 换值：「只看美国」→「只看巴西」——本轮原话**没有**出现旧的 USA/美国。

    判错方向的代价不对称：把"换值"误判成"重述"会让旧值继续留在档案里
    （SQL 条件多余）；把"重述"误判成"换值"会把一个仍然有效的条件标成
    dropped（**静默丢条件**，即本轮要修的乙类缺陷）。故判据偏向后者：
    只要本轮原话提到了旧值，就按重述处理。
    """
    text = (question or "").casefold()
    if not text:
        # 拿不到原话时保守按**重述**处理（保条件，不丢）
        return True
    for token in (old.surface, old.normalized):
        token = (token or "").strip().casefold()
        if token and token in text:
            return True
    return False


def _mentions(question: str, requirement: QueryRequirement) -> bool:
    """取消意图里是否指向了这条条件。

    判据：原话里出现了这条条件的 surface 或 normalized。
    「不看巴西了」+ 条件 Brazil → 命中。
    """
    text = (question or "").casefold()
    if not text:
        return False
    for token in (requirement.surface, requirement.normalized):
        token = (token or "").strip().casefold()
        if token and token in text:
            return True
    return False


def _decide_operation(
    archive: ConditionArchive,
    incoming: tuple[QueryRequirement, ...],
    kept: list[ArchivedCondition],
    stale: list[ArchivedCondition],
    dropped: list[ArchivedCondition],
    drop_intent: bool,
    switching: bool,
) -> MergeOperation:
    """判定本轮是什么操作。**判不出就返回 unknown**，不硬猜一个近似值。"""
    if switching and stale:
        return "replace_object"
    if drop_intent and dropped:
        return "drop"
    # 有替换（旧条被 dropped）且本轮补了同类新条 → 替换类操作
    if dropped and incoming:
        if any(item.kind == "measure" for item in incoming):
            return "replace_measure"
        if any(item.kind == "grouping" for item in incoming):
            return "replace_group"
        if any(item.kind == "value_filter" for item in incoming):
            return "replace_value"
        return "replace_value"
    if incoming and kept:
        return "add"
    if incoming and not archive.conditions:
        return "add"
    if incoming:
        return "add"
    # 走到了这里：本轮有条件，但既没新增、也没归类掉任何旧条 —— 说明
    # 归并逻辑与判定逻辑对不上。**这种东西不能悄悄放过**，标 unknown 让人看见。
    return "unknown"


__all__ = [
    "ArchivedCondition",
    "ConditionArchive",
    "ConditionState",
    "MergeOperation",
    "merge_condition_archive",
]
