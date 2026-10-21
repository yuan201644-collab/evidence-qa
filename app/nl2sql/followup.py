"""Deterministic interpretation and AST checks for database follow-ups.

This module does not infer hidden reasoning. It records a small, explicit edit to
the last successful SQL query and checks that the newly generated query honors it.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from sqlglot import exp, parse_one

#: 人称/指示指代：出现它，说明本轮在说上一轮结果里的那一行。
#: 「其」排除「其中」（那是集合内再筛，已由 memory 的追问标记处理）。
_PRONOUN_FOLLOWUP_RE = re.compile(r"(?<!其)(?:他们|她们|他|她)|这家|那家|这家|该供应商|该采购人|这个项目|那个项目")


_GROUP_RE = re.compile(
    r"按\s*(.+?)\s*(?:分组|分一下|分|拆开|拆分|拆|统计|汇总|呢|吗|[？?。！!，,；;]|$)"
)
_GROUP_PREFIX_RE = re.compile(r"(?:改成|改为|换成|换为|再|还|同时|另外|加上|并且|以及)")
_MEASURE_RE = re.compile(r"(?:改成|改为|换成|换为)\s*(?:看|统计|计算)?\s*(.+?)\s*(?:呢|吗|[？?。！!，,；;]|$)")


@dataclass(frozen=True)
class FollowupInstruction:
    """A bounded operation inferred from the user's latest turn."""

    operation: str
    target: str = ""
    previous_sql: str = ""
    previous_columns: tuple[str, ...] = ()
    previous_row_count: int | None = None
    #: 上一轮结果首行（有界）。用户的「他/这家/那家」指的就是它，
    #: 必须能作为明确取值落到下一轮 SQL 的 WHERE 里。
    selected_row: tuple[tuple[str, str], ...] = ()

    def as_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["selected_row"] = dict(self.selected_row)
        return payload


def resolve_followup(question: str, query_state: dict[str, Any] | None) -> FollowupInstruction | None:
    """Resolve only explicit, common query edits; leave other turns to the model."""
    state = query_state or {}
    previous_sql = str(state.get("sql") or "").strip()
    if not previous_sql:
        return None

    text = (question or "").strip()
    folded = text.casefold()
    operation = ""
    target = ""

    group_match = _GROUP_RE.search(text)
    if group_match:
        target = _GROUP_PREFIX_RE.sub("", group_match.group(1)).strip()
        if target:
            prefix = text[:group_match.start()]
            explicit_replacement = re.search(r"(?:改成|改为|换成|换为)", prefix)
            follows_with_then = re.search(r"(?:那|那么)\s*$", prefix)
            previous_has_group = False
            try:
                previous_query = parse_one(previous_sql)
                previous_has_group = (
                    isinstance(previous_query, exp.Select)
                    and bool(_group_expressions(previous_query))
                )
            except Exception:  # noqa: BLE001 - malformed prior SQL is rejected downstream
                pass
            operation = (
                "replace_group"
                if explicit_replacement or (follows_with_then and previous_has_group)
                else "add_group"
            )

    if not operation and any(term in folded for term in ("平均", "均值")):
        operation, target = "average_measure", text

    if not operation and any(term in text for term in ("最多", "最大", "最高", "最长", "最小", "最低", "最短")):
        operation, target = "rank_top", text

    if not operation:
        measure_match = _MEASURE_RE.search(text)
        if measure_match and any(term in measure_match.group(1) for term in ("张数", "数量", "金额", "总额", "消费", "时长", "播放")):
            operation, target = "replace_measure", measure_match.group(1).strip()

    if not operation:
        # 兜底：本轮用了人称指代（他/她/这家/那家/该供应商…），而上一轮的结果里
        # 确实只有/选中了一行 —— 那这句话一定是在追问那一行。
        # 2026-10-18 gmt-03 第 2 轮「他的项目一共花了多少钱？」既没有「改成」
        # 也没有「平均」，按操作词判定会返回 None，于是「他」所指的采购人
        # 从来没被写进 SQL，算出的是全库预算。
        if _PRONOUN_FOLLOWUP_RE.search(text) and state.get("first_row"):
            operation, target = "replace_measure", text

    if not operation:
        return None

    columns = state.get("columns") or ()
    if not isinstance(columns, (tuple, list)):
        columns = ()
    row_count = state.get("row_count")
    first_row = state.get("first_row")
    selected: tuple[tuple[str, str], ...] = ()
    if isinstance(first_row, dict):
        selected = tuple(
            (str(k)[:64], str(v)[:120]) for k, v in list(first_row.items())[:16]
        )
    return FollowupInstruction(
        operation=operation,
        target=target,
        previous_sql=previous_sql[:8000],
        previous_columns=tuple(str(item) for item in columns[:64]),
        previous_row_count=(int(row_count) if isinstance(row_count, int) else None),
        selected_row=selected,
    )


def followup_prompt(instruction: dict[str, Any] | FollowupInstruction | None) -> str:
    """Render deterministic state for SQL generation, without chain-of-thought text."""
    if instruction is None:
        return ""
    if isinstance(instruction, FollowupInstruction):
        item = instruction.as_payload()
    else:
        item = dict(instruction)
    operation = str(item.get("operation") or "")
    target = str(item.get("target") or "")
    previous_sql = str(item.get("previous_sql") or "")
    rules = {
        "replace_group": "只替换上一条查询的分组维度；保留已有筛选条件、统计对象和统计口径。新的分组不得继续包含被替换的旧分组。",
        "add_group": "在上一条查询的分组维度上增加新维度；保留原分组、已有筛选条件、统计对象和统计口径。",
        "average_measure": "只把指标改为平均值；保留上一条查询的分组维度、对象范围和筛选条件。",
        "rank_top": "从上一条查询结果中按用户指定的指标排序，只返回排名第一项；结果必须同时展示排名对象和用于排序的指标值；保留对象范围、分组维度和筛选条件。",
        "replace_measure": "只替换上一条查询的统计指标；保留对象范围、已有分组和筛选条件。",
    }
    rule = rules.get(operation)
    if not rule:
        return ""
    lines = ["基于上一轮已执行成功的查询做一次明确修改。", "本轮修改：" + rule]
    if target:
        lines.append("用户本轮指定的目标：" + target)
    if previous_sql:
        lines.extend(["上一轮实际执行 SQL：", previous_sql])
    selected = item.get("selected_row")
    if isinstance(selected, dict) and selected:
        rendered = "、".join(f"{k}={v}" for k, v in list(selected.items())[:8] if str(v).strip())
        if rendered:
            lines.append(
                "上一轮结果里被选中的那一行（用户说的「他/这家/那家」就是它）："
                + rendered
                + "\n本轮若沿用它，必须把它写成明确的 WHERE 等值条件，不得省略。"
            )
    lines.append("不得丢弃上一轮已有 WHERE 筛选；若目标字段无法由库结构确定，应按既有澄清规则停止猜测。")
    return "\n".join(lines)


def validate_followup_sql(
    previous_sql: str,
    candidate_sql: str,
    instruction: dict[str, Any] | FollowupInstruction,
    *,
    dialect: str = "postgres",
) -> dict[str, Any]:
    """Check whether candidate SQL realizes the explicit follow-up edit."""
    item = instruction.as_payload() if isinstance(instruction, FollowupInstruction) else dict(instruction)
    operation = str(item.get("operation") or "")
    target = str(item.get("target") or "")
    try:
        previous = parse_one(previous_sql, read=dialect)
        candidate = parse_one(candidate_sql, read=dialect)
    except Exception as exc:  # noqa: BLE001 - report malformed state as a bounded validation fact
        return _failed("sql_parse_failed", "无法解析上一轮或本轮 SQL，不能验证追问修改。", error=str(exc)[:240])
    if not isinstance(previous, exp.Select) or not isinstance(candidate, exp.Select):
        return _failed("non_select_followup", "追问 SQL 必须是单条 SELECT 查询。")

    previous_groups = _group_expressions(previous)
    candidate_groups = _group_expressions(candidate)
    previous_keys = {_expression_key(node, dialect) for node in previous_groups}
    candidate_keys = {_expression_key(node, dialect) for node in candidate_groups}
    candidate_group_text = " ".join(_expression_key(node, dialect) for node in candidate_groups)
    projection_text = " ".join(_expression_key(node, dialect) for node in candidate.expressions)
    target_text = (candidate_group_text + " " + projection_text).casefold()
    failures: list[dict[str, str]] = []
    dimension_owner = _expected_dimension_owner(target, previous)

    if operation == "replace_group":
        # 主判据走结构：新维度必须真的出现在 GROUP BY 里；
        # `_target_found` 只作为**兜底**（中文词表能命中时才用它）。
        target_found = _has_new_group(previous_keys, candidate_keys, candidate_groups)
        if not target_found:
            target_found = _target_found(target, target_text)
        old_group_retained = bool(previous_keys & candidate_keys)
        old_is_target = _target_found(target, " ".join(previous_keys))
        if not candidate_groups or not target_found:
            failures.append({"reason_code": "replacement_dimension_missing", "message": "SQL 未按本轮指定的新维度分组。"})
        if old_group_retained and not old_is_target:
            failures.append({"reason_code": "old_dimension_retained", "message": "替换分组后，SQL 仍保留上一轮分组维度。"})
        if dimension_owner and not _group_contains_source(candidate, candidate_groups, dimension_owner):
            failures.append({
                "reason_code": "dimension_source_mismatch",
                "message": "本轮维度应取自上一轮已确认的「" + dimension_owner + "」数据对象，SQL 当前未能证明这一点。",
            })
    elif operation == "add_group":
        if not candidate_groups or not previous_keys.issubset(candidate_keys):
            failures.append({"reason_code": "previous_dimension_dropped", "message": "增加分组时，SQL 没有保留上一轮全部分组维度。"})
        if not (_has_new_group(previous_keys, candidate_keys, candidate_groups)
                or _target_found(target, candidate_group_text)):
            failures.append({"reason_code": "added_dimension_missing", "message": "SQL 未加入本轮指定的新分组维度。"})
        if dimension_owner and not _group_contains_source(candidate, candidate_groups, dimension_owner):
            failures.append({
                "reason_code": "dimension_source_mismatch",
                "message": "本轮维度应取自上一轮已确认的「" + dimension_owner + "」数据对象，SQL 当前未能证明这一点。",
            })
    elif operation == "average_measure":
        if not any(isinstance(node, exp.Avg) for node in candidate.walk()):
            failures.append({"reason_code": "average_missing", "message": "本轮要求平均值，但 SQL 没有 AVG 聚合。"})
        if previous_keys and not previous_keys.issubset(candidate_keys):
            failures.append({"reason_code": "previous_dimension_dropped", "message": "改为平均值时，SQL 没有保留上一轮分组维度。"})
    elif operation == "rank_top":
        order = candidate.args.get("order")
        limit = candidate.args.get("limit")
        limit_value = _limit_value(limit)
        order_items = list(order.expressions) if isinstance(order, exp.Order) else []
        descending = not any(term in target for term in ("最小", "最低", "最短"))
        sort_matches = bool(order_items) and any(
            bool(item.args.get("desc")) == descending for item in order_items
        )
        if not sort_matches:
            direction = "降序" if descending else "升序"
            failures.append({"reason_code": "ranking_order_missing", "message": "本轮排序方向应为" + direction + "。"})
        if limit_value != 1:
            failures.append({"reason_code": "top_one_limit_missing", "message": "本轮要求找出排名第一项，但 SQL 没有 LIMIT 1。"})
        if previous_keys and not previous_keys.issubset(candidate_keys):
            failures.append({"reason_code": "previous_dimension_dropped", "message": "取最大/最多项时，SQL 没有保留上一轮分组维度。"})
        order_text = " ".join(_expression_key(node, dialect) for node in order_items)
        explicit_rank_target = any(term in target for term in ("时长", "播放时间", "播放长度"))
        if explicit_rank_target:
            if not _target_found(target, order_text):
                failures.append({"reason_code": "ranking_measure_mismatch", "message": "排序字段未能对应本轮明确指定的指标。"})
            if not _ordered_metric_is_projected(candidate, order_items, dialect):
                failures.append({"reason_code": "ranking_measure_not_projected", "message": "SQL 按该指标排序，但没有把指标值放入结果列供用户核对。"})
        else:
            previous_metric_aliases, previous_metric_expressions = _aggregate_projections(previous, dialect)
            order_aliases = {
                node.name.casefold()
                for order_item in order_items
                for node in order_item.find_all(exp.Column)
                if node.name
            }
            order_expressions = {_expression_key(node, dialect) for node in order_items}
            if not previous_metric_aliases and not previous_metric_expressions:
                failures.append({"reason_code": "ranking_metric_unanchored", "message": "上一轮没有可沿用的统计指标，不能确定本轮排名口径。"})
            elif not (
                previous_metric_aliases & order_aliases
                or previous_metric_expressions & order_expressions
            ):
                failures.append({"reason_code": "ranking_measure_mismatch", "message": "排序字段与上一轮统计指标不一致。"})
    elif operation == "replace_measure":
        if any(term in target for term in ("张数", "数量")) and not any(
            isinstance(node, exp.Count) for node in candidate.walk()
        ):
            failures.append({"reason_code": "count_measure_missing", "message": "本轮要求数量指标，但 SQL 没有 COUNT 聚合。"})
        if previous_keys and not previous_keys.issubset(candidate_keys):
            failures.append({"reason_code": "previous_dimension_dropped", "message": "替换统计指标时，SQL 没有保留上一轮分组维度。"})

    # 比较时对**裸列**放宽表名，对**带表名的列**严格比较。
    #
    # 为什么：同一个条件在单表里会被补成 `projects.budget_level = 'central'`，
    # 在多表 JOIN 里裸列无法确定归属，就会保留成 `budget_level = 'central'`。
    # 两种写法语义完全相同，但字符串不同 —— 用字符串直接比会误判成
    # 「丢失上一轮筛选条件」，把一条正确的 SQL 打成错误（2026-10-18 实测
    # gmt-01 第 3 轮：`WHERE budget_level = 'central'` 明明还在）。
    #
    # 但**不能**无条件剥表名：`c.country = 'USA'` 换成 `e.country = 'USA'`
    # 是两个不同的条件（见 tests/test_nl2sql_followup.py
    # ::test_country_dimension_keeps_the_customer_owner_established_by_prior_filter）。
    # 所以保留两组键：严格键（原样）与宽松键（剥表名），
    # 本轮 SQL 只要**严格命中**或**其中一侧是裸列**就算条件还在。
    previous_filters = _where_conjuncts(previous, dialect)
    candidate_filters = _where_conjuncts(candidate, dialect)
    candidate_exact = set(candidate_filters)
    candidate_loose = {_unqualified_filter(item) for item in candidate_filters}
    dropped_filters = [
        condition for condition in previous_filters
        if condition not in candidate_exact
        and not (_is_unqualified(condition) and candidate_loose)
        and not (
            _unqualified_filter(condition) in candidate_loose
            and any(_unqualified_filter(condition) == _unqualified_filter(c)
                    and _is_unqualified(c) for c in candidate_filters)
        )
    ]
    if dropped_filters:
        failures.append({
            "reason_code": "previous_filter_dropped",
            "message": "SQL 丢失上一轮的筛选条件：" + "；".join(dropped_filters[:3]),
        })

    if failures:
        return {
            "ok": False,
            "reason_code": failures[0]["reason_code"],
            "message": " ".join(item["message"] for item in failures),
            "details": {"operation": operation, "target": target, "failures": failures},
        }
    return {
        "ok": True,
        "reason_code": "followup_constraints_satisfied",
        "message": "SQL 保留了必要上下文并落实本轮追问修改。",
        "details": {
            "operation": operation,
            "target": target,
            "previous_group_by": sorted(previous_keys),
            "current_group_by": sorted(candidate_keys),
            "preserved_filter_count": len(previous_filters),
        },
    }


def _group_expressions(query: exp.Select) -> list[exp.Expression]:
    group = query.args.get("group")
    if not isinstance(group, exp.Group):
        return []
    projections = list(query.expressions)
    aliases = {
        projection.alias.casefold(): projection.this
        for projection in projections
        if isinstance(projection, exp.Alias) and projection.alias
    }
    expanded: list[exp.Expression] = []
    for expression in group.expressions:
        if isinstance(expression, exp.Literal) and expression.is_int:
            try:
                projection = projections[int(expression.this) - 1]
            except (IndexError, TypeError, ValueError):
                expanded.append(expression)
                continue
            expanded.append(projection.this if isinstance(projection, exp.Alias) else projection)
        elif isinstance(expression, exp.Column) and not expression.table:
            expanded.append(aliases.get(expression.name.casefold(), expression))
        else:
            expanded.append(expression)
    return expanded


def _expression_key(node: exp.Expression, dialect: str) -> str:
    clone = node.copy()
    for column in clone.find_all(exp.Column):
        column.set("table", None)
        column.set("db", None)
        column.set("catalog", None)
    return re.sub(r"\s+", " ", clone.sql(dialect=dialect, normalize=True)).strip().casefold()


def _where_conjuncts(query: exp.Select, dialect: str) -> list[str]:
    where = query.args.get("where")
    if where is None:
        return []

    def flatten(node: exp.Expression) -> list[exp.Expression]:
        if isinstance(node, exp.And):
            return flatten(node.this) + flatten(node.expression)
        return [node]

    return sorted({_filter_expression_key(node, dialect, _source_table_map(query)) for node in flatten(where.this)})


def _unqualified_filter(condition: str) -> str:
    """把条件表达式里的 `表名.` 前缀剥掉，用于跨轮比较筛选条件是否还在。

    `projects.budget_level = 'central'` → `budget_level = 'central'`。
    只剥标识符前的限定前缀，不碰字符串字面量里的点（`'a.b'` 原样保留）。
    """
    return re.sub(r"(?<![A-Za-z0-9_'\"])[A-Za-z_][A-Za-z0-9_]*\.(?=[A-Za-z_])", "", condition)


def _is_unqualified(condition: str) -> bool:
    """条件里是否**没有任何**带表名的列（即全部是裸列）。

    只对「裸列 vs 带表名」这一对放宽比较；两侧都带表名时表名是语义的一部分。
    """
    return not re.search(r"(?<![A-Za-z0-9_'\"])[A-Za-z_][A-Za-z0-9_]*\.(?=[A-Za-z_])", condition)


def _source_table_map(query: exp.Select) -> dict[str, str]:
    result: dict[str, str] = {}
    from_clause = query.args.get("from_")
    sources = []
    if isinstance(from_clause, exp.From):
        sources.append(from_clause.this)
        sources.extend(from_clause.expressions)
    sources.extend(join.this for join in query.args.get("joins") or [])
    for source in sources:
        if isinstance(source, exp.Table):
            result[source.alias_or_name.casefold()] = source.name.casefold()
            result[source.name.casefold()] = source.name.casefold()
    return result


def _filter_expression_key(
    node: exp.Expression, dialect: str, source_map: dict[str, str]
) -> str:
    clone = node.copy()
    sources = set(source_map.values())
    single_source = next(iter(sources)) if len(sources) == 1 else None
    for column in clone.find_all(exp.Column):
        source = source_map.get(column.table.casefold()) if column.table else single_source
        if source:
            column.set("table", source)
        column.set("db", None)
        column.set("catalog", None)
    return re.sub(r"\s+", " ", clone.sql(dialect=dialect, normalize=True)).strip().casefold()


def _expected_dimension_owner(target: str, previous: exp.Select) -> str | None:
    text = (target or "").casefold()
    if any(term in text for term in ("开票", "账单", "billing")):
        return "invoice"
    if any(term in text for term in ("客户", "顾客", "customer")):
        return "customer"
    if any(term in text for term in ("员工", "客服", "employee")):
        return "employee"
    if "国家" not in text and "country" not in text:
        return None

    where = previous.args.get("where")
    if where is not None:
        source_map = _source_table_map(previous)
        owners = {
            source_map.get(column.table.casefold())
            for column in where.find_all(exp.Column)
            if column.name.casefold() in {"country", "billing_country"} and column.table
        }
        owners.discard(None)
        if len(owners) == 1:
            return next(iter(owners))
    return None


def _aggregate_projections(query: exp.Select, dialect: str) -> tuple[set[str], set[str]]:
    aliases: set[str] = set()
    expressions: set[str] = set()
    for projection in query.expressions:
        expression = projection.this if isinstance(projection, exp.Alias) else projection
        if not any(isinstance(node, exp.AggFunc) for node in expression.walk()):
            continue
        if isinstance(projection, exp.Alias) and projection.alias:
            aliases.add(projection.alias.casefold())
        expressions.add(_expression_key(expression, dialect))
    return aliases, expressions


def _ordered_metric_is_projected(
    query: exp.Select, order_items: list[exp.Expression], dialect: str
) -> bool:
    aliases = {
        projection.alias.casefold()
        for projection in query.expressions
        if isinstance(projection, exp.Alias) and projection.alias
    }
    expressions = {
        _expression_key(projection.this if isinstance(projection, exp.Alias) else projection, dialect)
        for projection in query.expressions
    }
    for order_item in order_items:
        ordered_expression = order_item.this if isinstance(order_item, exp.Ordered) else order_item
        if isinstance(ordered_expression, exp.Column) and not ordered_expression.table:
            if ordered_expression.name.casefold() in aliases:
                return True
        if _expression_key(ordered_expression, dialect) in expressions:
            return True
    return False


def _group_contains_source(
    query: exp.Select, groups: list[exp.Expression], expected_owner: str
) -> bool:
    source_map = _source_table_map(query)
    sources = set(source_map.values())
    for group in groups:
        for column in group.find_all(exp.Column):
            owner = source_map.get(column.table.casefold()) if column.table else None
            if owner:
                if owner == expected_owner:
                    return True
            elif len(sources) == 1:
                if next(iter(sources)) == expected_owner:
                    return True
    return False


def _has_new_group(previous_keys: set[str], candidate_keys: set[str], candidate_groups) -> bool:
    """本轮 GROUP BY 是否出现了上一轮没有的维度。

    这是 `add_group` / `replace_group` 的**主判据**，替代 `_target_found` 的
    中文词表匹配。

    为什么换：`_target_found` 用一张硬编码词表（月/城市/国家/员工/客户/
    发票/时长）去猜 target 的语义。政采库的维度是「地区」「采购方式」
    「预算级次」—— 一个都不在表里，于是无论 SQL 怎么写都判失败
    （2026-10-18 实测 gmt-01 第 3 轮：SQL 已按 `region_name` 分组，
    仍报「未加入本轮指定的新分组维度」）。

    结构判据不会有这个问题：分组维度到底加没加，看 GROUP BY 的表达式集合
    差集即可，与用户用的是哪个中文词无关。它也不宽松 —— 没加就是没加。
    """
    if not candidate_groups:
        return False
    return bool(candidate_keys - previous_keys)


def _target_found(target: str, structure: str) -> bool:
    text = (target or "").casefold()
    structure = structure.casefold()
    patterns: tuple[str, ...]
    if any(word in text for word in ("月", "月份", "按月")):
        patterns = ("invoice_month", "date_trunc", "strftime", "to_char", "extract(month", "month(")
    elif "城市" in text or "city" in text:
        patterns = ("city",)
    elif "国家" in text or "country" in text:
        patterns = ("country",)
    elif "员工" in text or "employee" in text or "客服" in text:
        patterns = ("employee", "support_rep", "first_name", "last_name")
    elif "客户" in text or "顾客" in text or "customer" in text:
        patterns = ("customer",)
    elif "发票" in text or "invoice" in text:
        patterns = ("invoice",)
    elif "时长" in text or "播放时间" in text:
        patterns = ("duration", "milliseconds", "length")
    else:
        words = [word.casefold() for word in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", text) if len(word) > 2]
        patterns = tuple(words)
    if any(word in text for word in ("月", "月份", "按月")):
        return any(pattern in structure for pattern in patterns) or bool(
            re.search(r"(?:date_trunc|extract)\s*\([^)]*month", structure)
        )
    return any(pattern in structure for pattern in patterns)


def _limit_value(limit: exp.Expression | None) -> int | None:
    if not isinstance(limit, exp.Limit):
        return None
    value = limit.expression
    if isinstance(value, exp.Literal) and value.is_int:
        try:
            return int(value.this)
        except (TypeError, ValueError):
            return None
    return None


def _failed(reason_code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"ok": False, "reason_code": reason_code, "message": message, "details": details}


__all__ = ["FollowupInstruction", "resolve_followup", "followup_prompt", "validate_followup_sql"]
