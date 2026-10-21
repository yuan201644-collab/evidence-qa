"""用 SQL 抽象语法树检查外键关联路径和显著的粒度风险。"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from app.nl2sql.planning_models import JoinPlan, SemanticCheckResult
from app.nl2sql.sql_scopes import (
    edge_table_key as _edge_table_key,
    immediate_nested_queries as _immediate_nested_queries,
    mandatory_conjuncts as _mandatory_conjuncts,
    resolve_identity as _resolve_identity,
    scope_columns as _scope_columns,
    table_alias as _table_alias,
    table_identity as _table_identity,
)


class SqlSemanticValidator:
    """只放行可由目录和当前 JOIN 条件共同确认的关系。"""

    def check(
        self,
        sql: str,
        join_plan: JoinPlan,
        *,
        dialect: str = "postgres",
        question: str = "",
        _outer_aliases: dict[str, str] | None = None,
    ) -> SemanticCheckResult:
        try:
            parsed = sqlglot.parse_one(sql, read=dialect)
        except Exception as exc:
            return SemanticCheckResult("unknown", "sql_ast_unavailable", "无法解析 SQL 结构：" + str(exc))
        if not isinstance(parsed, exp.Query):
            return SemanticCheckResult("unknown", "non_query_ast", "当前语句结构不在语义检查范围内")

        scale_issue = _growth_rate_scale_issue(parsed, question)
        if scale_issue is not None:
            return SemanticCheckResult("repair", scale_issue[0], scale_issue[1])

        where = parsed.args.get("where")
        if where is not None and _constant_predicate(where.this) is False:
            return SemanticCheckResult(
                "repair", "query_forces_empty_result",
                "WHERE 条件可静态证明恒假，不能将人为构造的空结果当作数据库无数据。",
            )
        if _requested_aggregate_missing(parsed, question):
            return SemanticCheckResult(
                "repair", "requested_aggregate_missing",
                "问题要求统计/聚合指标，但 SQL 没有对应的聚合表达式；不能用常量或 NULL 代替指标。",
            )

        joins = list(parsed.args.get("joins") or ())
        nested_queries = _immediate_nested_queries(parsed)
        if nested_queries:
            current_aliases = _scope_aliases(parsed, join_plan)
            visible_aliases = dict(_outer_aliases or {})
            visible_aliases.update(current_aliases)
            for nested in nested_queries:
                correlation_issue = _correlation_issue(nested, visible_aliases, join_plan)
                if correlation_issue is not None:
                    return SemanticCheckResult(
                        "repair", "nested_scope_" + correlation_issue[0],
                        "子查询跨作用域关联未通过校验：" + correlation_issue[1],
                    )
                nested_plan = _scope_join_plan(nested, join_plan)
                nested_result = self.check(
                    nested.sql(dialect=dialect), nested_plan,
                    dialect=dialect, question=question,
                    _outer_aliases=visible_aliases,
                )
                if nested_result.status != "pass":
                    return SemanticCheckResult(
                        nested_result.status,
                        "nested_scope_" + nested_result.reason_code,
                        "子查询关联校验未通过：" + nested_result.message,
                        {"nested_reason_code": nested_result.reason_code,
                         "nested_details": nested_result.details},
                    )
            if not joins:
                required_keys = _plan_table_keys(join_plan)
                from_clause = parsed.args.get("from_")
                base_table = from_clause.this if from_clause is not None else None
                if isinstance(base_table, exp.Table):
                    identity = _resolve_identity(base_table, required_keys)
                    if not identity:
                        return SemanticCheckResult(
                            "repair", "base_table_outside_link_result",
                            "SQL 的外层起始表未纳入定位结果：" + _table_identity(base_table),
                        )
                return SemanticCheckResult(
                    "pass", "nested_scopes_validated",
                    "外层对象及所有子查询关联均符合已声明的外键路径",
                    {"nested_scope_count": len(nested_queries)},
                )

        physical_tables = [node for node in parsed.find_all(exp.Table) if node.name]
        required_keys = _plan_table_keys(join_plan)
        if not joins:
            if not physical_tables and required_keys:
                return SemanticCheckResult(
                    "repair", "query_missing_source_table", "SQL 未查询表，但本题的定位计划要求读取业务数据",
                )
            # 白名单用 `allowed`（必用 + 可选），缺表判定用 `required`（仅必用）。
            # 两者混用会把“只查了一张可选表”的合法 SQL 判成缺表。
            allowed_keys = _allowed_table_keys(join_plan)
            used_keys = {
                identity
                for table in physical_tables
                if (identity := _resolve_identity(table, allowed_keys))
            }
            # 只有计划**真的点名了表**时才谈“计划外”。计划为空说明定位阶段没有
            # 拿到可信候选（例如全部候选都没过 0.5 分线），此时是“不做约束”，
            # 不是“什么都不许用”——按后者处理会把每一条正确 SQL 都判成越界。
            if allowed_keys:
                outside = [table for table in physical_tables
                           if not _resolve_identity(table, allowed_keys)]
                if outside:
                    return SemanticCheckResult(
                        "repair", "base_table_outside_link_result",
                        "SQL 使用了未纳入定位结果的表："
                        + ", ".join(_table_identity(table) for table in outside),
                    )
            missing_keys = sorted(required_keys - used_keys)
            if missing_keys:
                return SemanticCheckResult(
                    "repair", "query_missing_planned_tables",
                    "SQL 未使用定位/关联计划要求的业务表：" + ", ".join(missing_keys),
                    {"missing_tables": tuple(missing_keys)},
                )
            if len(physical_tables) <= 1:
                return SemanticCheckResult("pass", "single_table", "单表查询无需关联路径检查")
            return SemanticCheckResult("unknown", "implicit_multi_table", "多表查询未使用可核验的显式 JOIN")
        if join_plan.ambiguous:
            return SemanticCheckResult("unknown", "ambiguous_join_path", "候选外键路径存在歧义，停止自动执行")
        if join_plan.warnings:
            return SemanticCheckResult("unknown", "join_plan_incomplete", "候选表之间未能建立完整、唯一的外键路径")

        base_table = (parsed.args.get("from_").this
                      if parsed.args.get("from_") is not None else None)
        if not isinstance(base_table, exp.Table):
            return SemanticCheckResult("unknown", "unsupported_from_scope", "FROM 结构不在关联核验范围内")

        allowed_keys = _allowed_table_keys(join_plan)
        known_identities = set(allowed_keys)
        base_identity = _resolve_identity(base_table, known_identities)
        if not base_identity:
            return SemanticCheckResult("repair", "base_table_outside_link_result",
                                       "SQL 的起始表未纳入定位结果：" + _table_identity(base_table))
        alias_to_identity = {_table_alias(base_table): base_identity}
        seen_identities = {base_identity}
        seen_aliases = {_table_alias(base_table)}

        for join in joins:
            target = join.this
            if not isinstance(target, exp.Table):
                return SemanticCheckResult("unknown", "unsupported_join_target", "JOIN 目标不是普通数据表")
            target_identity = _resolve_identity(target, known_identities)
            target_alias = _table_alias(target)
            if not target_identity:
                return SemanticCheckResult(
                    "repair", "table_outside_link_result",
                    "SQL 使用了未纳入定位结果的表：" + _table_identity(target)
                )
            on = join.args.get("on")
            if on is None or join.args.get("using"):
                return SemanticCheckResult("unknown", "join_without_supported_on", "JOIN 缺少可核验的 ON 条件")
            conjuncts = _mandatory_conjuncts(on)
            if conjuncts is None:
                return SemanticCheckResult(
                    "repair", "join_condition_contains_or", "ON 条件含有 OR，不能确认外键约束必然成立"
                )
            condition_aliases = dict(alias_to_identity)
            condition_aliases[target_alias] = target_identity
            condition_aliases.setdefault(target.name.lower(), target_identity)
            equalities = _qualified_equalities(conjuncts, condition_aliases)
            if not _has_complete_edge(
                target_identity, target_alias, seen_aliases, seen_identities,
                equalities, join_plan,
            ):
                return SemanticCheckResult(
                    "repair", "join_condition_unverified",
                    "JOIN 条件未包含连接当前表与已连接表的完整外键列组",
                )
            alias_to_identity[target_alias] = target_identity
            alias_to_identity.setdefault(target.name.lower(), target_identity)
            seen_aliases.add(target_alias)
            seen_identities.add(target_identity)

        warnings = _grain_warnings(parsed, join_plan, physical_tables)
        if warnings:
            return SemanticCheckResult("unknown", "possible_grain_risk",
                                       "检测到可能的一对多汇总粒度风险，需人工确认",
                                       {"warnings": tuple(warnings)})
        return SemanticCheckResult("pass", "declared_fk_path", "每个 JOIN 均包含完整且必要的目录外键条件",
                                   {"tables": tuple(sorted(seen_identities)), "join_count": len(joins)})


def _mandatory_conjuncts(node: exp.Expression) -> list[exp.Expression] | None:
    """Return only predicates guaranteed by AND; any OR makes the join unsafe."""
    if isinstance(node, exp.Paren):
        return _mandatory_conjuncts(node.this)
    if isinstance(node, exp.Or):
        return None
    if isinstance(node, exp.And):
        left = _mandatory_conjuncts(node.this)
        right = _mandatory_conjuncts(node.expression)
        if left is None or right is None:
            return None
        return left + right
    return [node]


def _qualified_equalities(conjuncts, alias_to_identity):
    equalities = []
    for item in conjuncts:
        if not isinstance(item, exp.EQ):
            continue
        left, right = item.this, item.expression
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            continue
        left_alias = (left.table or "").lower()
        right_alias = (right.table or "").lower()
        if not left_alias or not right_alias:
            continue
        left_identity = alias_to_identity.get(left_alias)
        right_identity = alias_to_identity.get(right_alias)
        if left_identity is None or right_identity is None:
            continue
        equalities.append((left_identity, left.name.lower(), left_alias,
                           right_identity, right.name.lower(), right_alias))
    return equalities


def _has_complete_edge(target_identity, target_alias, seen_aliases, seen_identities, equalities, join_plan):
    for edge in join_plan.edges:
        left_identity = _edge_table_key(edge.left_schema, edge.left_table)
        right_identity = _edge_table_key(edge.right_schema, edge.right_table)
        orientations = []
        if target_identity == left_identity and right_identity in seen_identities:
            orientations.append((edge.left_columns, edge.right_columns, right_identity))
        if target_identity == right_identity and left_identity in seen_identities:
            orientations.append((edge.right_columns, edge.left_columns, left_identity))
        for target_columns, other_columns, other_identity in orientations:
            expected = list(zip(target_columns, other_columns))
            matched = all(
                any(
                    (a_identity == target_identity and a_column == target_column.lower()
                     and a_alias == target_alias and b_identity == other_identity
                     and b_column == other_column.lower() and b_alias in seen_aliases)
                    or (b_identity == target_identity and b_column == target_column.lower()
                        and b_alias == target_alias and a_identity == other_identity
                        and a_column == other_column.lower() and a_alias in seen_aliases)
                    for a_identity, a_column, a_alias, b_identity, b_column, b_alias in equalities
                )
                for target_column, other_column in expected
            )
            if matched and expected:
                return True
    return False


def _grain_warnings(parsed, join_plan: JoinPlan, physical_tables) -> list[str]:
    warnings = []
    parents = {
        _edge_table_key(edge.right_schema, edge.right_table)
        for edge in join_plan.edges if edge.cardinality == "many_to_one"
    }
    if not parents:
        return warnings
    alias_to_identity = {}
    for table in physical_tables:
        identity = _resolve_identity(table, parents | set(join_plan.tables))
        if not identity:
            identity = _table_identity(table)
        alias_to_identity[_table_alias(table)] = identity
        alias_to_identity.setdefault(table.name.lower(), identity)
    for aggregate in parsed.find_all(exp.Sum):
        parent_columns = set()
        for column in aggregate.find_all(exp.Column):
            qualifier = (column.table or "").lower()
            identity = alias_to_identity.get(qualifier) if qualifier else None
            if identity in parents:
                parent_columns.add(identity + "." + column.name.lower())
            elif not qualifier and parents.intersection(alias_to_identity.values()):
                parent_columns.add("unqualified." + column.name.lower())
        warnings.extend(
            f"SUM 表达式可能累计一对多关联中被重复展开的父表字段：{name}"
            for name in sorted(parent_columns)
        )
    return warnings


def _growth_rate_scale_issue(parsed: exp.Query, question: str) -> tuple[str, str] | None:
    """Require an explicit percentage scale for growth-rate questions."""
    if not any(term in (question or "") for term in ("增长率", "同比", "环比")):
        return None

    percent_aliases = []
    for item in parsed.expressions:
        if not isinstance(item, exp.Alias):
            continue
        alias = (item.alias or "").lower()
        if alias.endswith(("_percent", "_percentage")):
            percent_aliases.append(item)

    if not percent_aliases:
        return (
            "growth_rate_alias_not_percent",
            "增长率结果别名必须以 _percent 结尾，并按百分数表达。",
        )

    for item in percent_aliases:
        if _has_percent_multiplier(item.this):
            if any(isinstance(node, exp.Nullif) for node in item.this.walk()):
                return None
            return (
                "growth_rate_zero_denominator_unprotected",
                "增长率的对比期分母未使用 NULLIF 保护零值。",
            )

    return (
        "growth_rate_scale_not_percent",
        "增长率表达式未确认乘以 100；请按百分数计算，并将对比期为零的除数用 NULLIF 保护。",
    )


def _requested_aggregate_missing(parsed: exp.Query, question: str) -> bool:
    aggregation_terms = (
        "统计", "平均", "均值", "总额", "合计", "总计", "汇总", "数量", "个数",
        "订单数", "人数", "记录数", "占比", "比率", "拒收率",
    )
    if not any(term in (question or "") for term in aggregation_terms):
        return False
    return not any(isinstance(node, exp.AggFunc) for node in parsed.walk())


def _constant_predicate(node: exp.Expression) -> bool | None:
    """Evaluate only obvious boolean constants; unknown SQL expressions stay unknown."""
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Not):
        inner = _constant_predicate(node.this)
        return None if inner is None else not inner
    if isinstance(node, exp.EQ):
        left, right = node.this, node.expression
        if (isinstance(left, exp.Literal) and left.is_number
                and isinstance(right, exp.Literal) and right.is_number):
            try:
                return float(left.this) == float(right.this)
            except (TypeError, ValueError):
                return None
    if isinstance(node, exp.And):
        left = _constant_predicate(node.this)
        right = _constant_predicate(node.expression)
        if left is False or right is False:
            return False
        if left is True and right is True:
            return True
    if isinstance(node, exp.Or):
        left = _constant_predicate(node.this)
        right = _constant_predicate(node.expression)
        if left is True or right is True:
            return True
        if left is False and right is False:
            return False
    return None


def _plan_table_keys(join_plan: JoinPlan) -> set[str]:
    """校验器认可的**必用**表（缺了就 repair）。

    只认 `join_plan.tables`：它是“问题明确点名/由可靠路径推导出”的表集合。
    外键边的两个端点**不算必用** —— 边只说明“这些表可以按已声明外键关联”，
    并不表示每条 SQL 都必须同时用到它们（否则单表查询永远过不了）。
    """
    return {item.lower() for item in join_plan.tables}


def _allowed_table_keys(join_plan: JoinPlan) -> set[str]:
    """校验器**允许**出现的表 = 必用表 + 边端点 + 仅因字段归属入选的可选表。

    与 `_plan_table_keys` 严格区分：这里的表可以出现在 SQL 里，但**不要求**出现。
    若用必用表当白名单，只查某一张合法表的 SQL 会被误判
    `base_table_outside_link_result`。
    """
    keys = _plan_table_keys(join_plan)
    for edge in join_plan.edges:
        keys.add(_edge_table_key(edge.left_schema, edge.left_table))
        keys.add(_edge_table_key(edge.right_schema, edge.right_table))
    keys.update(item.lower() for item in join_plan.optional_tables)
    return keys


def _scope_join_plan(scope: exp.Query, join_plan: JoinPlan) -> JoinPlan:
    # 用 allow-list 解析表身份：可选表也要能映射到规范名，否则子查询里的
    # 可选表会被当成“未纳入定位结果”。
    known = _allowed_table_keys(join_plan)
    tables = []
    for table in scope.find_all(exp.Table):
        identity = _resolve_identity(table, known)
        if identity and identity not in tables:
            tables.append(identity)
    table_set = set(tables)
    edges = tuple(
        edge for edge in join_plan.edges
        if _edge_table_key(edge.left_schema, edge.left_table) in table_set
        and _edge_table_key(edge.right_schema, edge.right_table) in table_set
    )
    return JoinPlan(
        target_grain=join_plan.target_grain,
        tables=tuple(tables),
        edges=edges,
        ambiguous=join_plan.ambiguous,
        warnings=join_plan.warnings,
        evidence=tuple(item for item in join_plan.evidence if any(
            table in item for table in table_set
        )),
    )


def _scope_aliases(scope: exp.Query, join_plan: JoinPlan) -> dict[str, str]:
    aliases: dict[str, str] = {}
    # 同 `_scope_join_plan`：别名表也要覆盖可选表，容错 JOIN 的别名引用。
    known = _allowed_table_keys(join_plan)
    from_clause = scope.args.get("from_")
    base_table = from_clause.this if from_clause is not None else None
    if isinstance(base_table, exp.Table):
        identity = _resolve_identity(base_table, known)
        if identity:
            aliases[_table_alias(base_table)] = identity
            aliases.setdefault(base_table.name.lower(), identity)
    for join in scope.args.get("joins") or ():
        table = join.this
        if not isinstance(table, exp.Table):
            continue
        identity = _resolve_identity(table, known)
        if identity:
            aliases[_table_alias(table)] = identity
            aliases.setdefault(table.name.lower(), identity)
    return aliases


def _correlation_issue(
    scope: exp.Query,
    outer_aliases: dict[str, str],
    join_plan: JoinPlan,
) -> tuple[str, str] | None:
    """Require correlated predicates to match a complete planned foreign-key edge."""
    local_aliases = _scope_aliases(scope, join_plan)
    if not local_aliases or not outer_aliases:
        return None
    local_alias_names = set(local_aliases)
    outer_alias_names = set(outer_aliases) - local_alias_names
    if not outer_alias_names:
        return None

    visible_aliases = dict(outer_aliases)
    visible_aliases.update(local_aliases)
    seen_aliases = set(outer_alias_names)
    seen_identities = {outer_aliases[alias] for alias in outer_alias_names}

    predicates = []
    where = scope.args.get("where")
    if where is not None:
        predicates.append(where.this)
    having = scope.args.get("having")
    if having is not None:
        predicates.append(having.this)
    predicates.extend(
        join.args["on"] for join in (scope.args.get("joins") or ())
        if join.args.get("on") is not None
    )

    for predicate in predicates:
        predicate_aliases = {
            (column.table or "").lower()
            for column in _scope_columns(predicate)
        }
        if not (predicate_aliases & local_alias_names
                and predicate_aliases & outer_alias_names):
            continue

        conjuncts = _mandatory_conjuncts(predicate)
        if conjuncts is None:
            return "correlation_contains_or", "相关对象条件位于 OR 分支，不能确认外键约束必然成立。"
        equalities = _qualified_equalities(conjuncts, visible_aliases)
        correlated_equalities = []
        for conjunct in conjuncts:
            columns = _scope_columns(conjunct)
            aliases = {(column.table or "").lower() for column in columns}
            if not (aliases & local_alias_names and aliases & outer_alias_names):
                continue
            if isinstance(conjunct, exp.Paren):
                conjunct = conjunct.this
            if not isinstance(conjunct, exp.EQ):
                return (
                    "correlation_not_equality",
                    "跨作用域对象关联必须是明确的等值外键条件。",
                )
            left, right = conjunct.this, conjunct.expression
            if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
                return (
                    "correlation_not_equality",
                    "跨作用域对象关联必须是明确的等值外键条件。",
                )
            left_alias = (left.table or "").lower()
            right_alias = (right.table or "").lower()
            if not ((left_alias in local_alias_names and right_alias in outer_alias_names)
                    or (right_alias in local_alias_names and left_alias in outer_alias_names)):
                return (
                    "correlation_not_equality",
                    "跨作用域等值条件未直接比较本地表列与外层表列。",
                )
            correlated_equalities.append(conjunct)

        for equality in correlated_equalities:
            left_alias = (equality.this.table or "").lower()
            right_alias = (equality.expression.table or "").lower()
            local_alias = left_alias if left_alias in local_alias_names else right_alias
            local_identity = local_aliases[local_alias]
            if not _has_complete_edge(
                local_identity, local_alias, seen_aliases, seen_identities,
                equalities, join_plan,
            ):
                return (
                    "correlation_unverified",
                    "相关条件未包含本地表与外层表之间完整、已声明的外键列组。",
                )
    return None


def _has_percent_multiplier(node: exp.Expression) -> bool:
    """Accept common equivalent placements of the percentage multiplier."""
    current = node
    while isinstance(current, (exp.Paren, exp.Cast, exp.TryCast, exp.Round)):
        current = current.this

    def is_hundred(value: exp.Expression) -> bool:
        if not isinstance(value, exp.Literal) or not value.is_number:
            return False
        try:
            return float(value.this) == 100.0
        except (TypeError, ValueError):
            return False

    if isinstance(current, exp.Mul):
        for operand in (current.this, current.expression):
            if is_hundred(operand):
                other = current.expression if operand is current.this else current.this
                if any(isinstance(part, exp.Div) for part in other.walk()):
                    return True

    # `(current - prior) * 100 / NULLIF(prior, 0)` is algebraically
    # equivalent to `(current - prior) / NULLIF(prior, 0) * 100`.
    if isinstance(current, exp.Div):
        numerator = current.this
        while isinstance(numerator, (exp.Paren, exp.Cast, exp.TryCast, exp.Round)):
            numerator = numerator.this
        if isinstance(numerator, exp.Mul) and any(
            is_hundred(operand) for operand in (numerator.this, numerator.expression)
        ):
            return True
    return False


__all__ = ["SqlSemanticValidator"]
