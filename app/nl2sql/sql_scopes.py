"""SQL 抽象语法树的作用域与表身份解析 —— 语义校验器与要求覆盖核对器共用。

**为什么单独一个模块**：`semantic_validator.py` 与 `requirement_validator.py`
都要回答同一组问题——「这个 Column 属于哪张表」「这个 Table 的规范身份是什么」
「这个子查询能看到哪些别名」。两处各自复制一份的话，作用域语义会悄悄漂移
（例如一边认 `schema.table` 规范名、另一边只认裸表名），于是同一个 SQL 在两套
校验下结论不同。所以这里只保留**纯函数**，两边都从本模块 import。

**刻意不做的事**：本模块不认识「要求」「能力」，也不做任何 IO 与模型调用。
它只把 AST 翻成可判定的结构。所有业务判定留给调用方。
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp


# ────────────────────────────── 表身份 ──────────────────────────────


def table_identity(node: exp.Table) -> str:
    """表的规范身份 `schema.table`（无 schema 时只有 `table`），一律小写。

    小写是刻意的：PostgreSQL 会把未加引号的标识符折成小写，而 `AdventureWorks`
    的元数据登记的是 PascalCase（如 `SalesOrderHeader`）。**不在这里做大小写归一**，
    交给 `resolve_identity` 与调用方按需处理——本模块只负责如实反映 AST 原文。
    """
    name = (node.name or "").lower()
    schema = (node.db or "").lower()
    return f"{schema}.{name}" if schema else name


def table_alias(node: exp.Table) -> str:
    """表的引用名：有别名用别名，否则用表名。一律小写。"""
    return (node.alias_or_name or node.name or "").lower()


def resolve_identity(node: exp.Table, known_identities: set[str]) -> str:
    """把 AST 里的表节点解析成 `known_identities` 中的规范身份，失败返回空串。

    **两条匹配规则**（顺序不可调换）：
    1. 精确匹配 `schema.table` 或裸 `table`；
    2. 表节点没带 schema 前缀时，用表名在候选集里找**唯一**后缀匹配
       （`public.customer` 与 `sales.customer` 同时在候选里时不猜，返回空串）。
    """
    identity = table_identity(node)
    if identity in known_identities:
        return identity
    if not node.db:
        matches = [candidate for candidate in known_identities
                   if candidate.rsplit(".", 1)[-1] == node.name.lower()]
        if len(matches) == 1:
            return matches[0]
    return ""


def edge_table_key(schema: str, table: str) -> str:
    """外键边端点与表身份用同一拼法，保证两边的字符串可直接比较。"""
    return f"{schema}.{table}".lower()


# ────────────────────────────── 作用域 ──────────────────────────────


def immediate_nested_queries(parsed: exp.Query) -> list[exp.Query]:
    """只取**直接**嵌在 `parsed` 查询体里的子查询，不含更深层、不含 CTE。

    `parsed.find_all(exp.Query)` 会把孙辈与 CTE 也带出来；这里沿 `parent`
    往上找到第一个 `Query`，只有它正是 `parsed` 时才算直接嵌套。语义校验器
    靠这个区分「本层要自己核的子查询」与「留给下一层递归的」。

    **CTE 刻意不算**：sqlglot 会把 `WITH x AS (...) SELECT ...` 的 CTE 挂成
    `CTE → With → 外层 Select`，沿 parent 链第一个 `Query` 正是外层 Select。
    若不加 `With` 豁免，CTE 会被误判成「外层的直接子查询」——它其实是独立的
    查询体，得由 `cte_queries` 单独枚举。
    """
    nested = []
    for node in parsed.find_all(exp.Query):
        if node is parsed:
            continue
        parent = node.parent
        while parent is not None and not isinstance(parent, exp.Query):
            if isinstance(parent, exp.With):
                break
            parent = parent.parent
        if parent is parsed:
            nested.append(node)
    return nested


def cte_queries(parsed: exp.Query) -> list[exp.Query]:
    """`WITH` 段里每个 CTE 的查询体（外层 SELECT 不算）。"""
    queries: list[exp.Query] = []
    for node in parsed.walk():
        if isinstance(node, exp.CTE):
            inner = node.this
            if isinstance(inner, exp.Query):
                queries.append(inner)
    return queries


def scope_columns(expression: exp.Expression) -> list[exp.Column]:
    """收集表达式里的 Column 节点，**不进入子查询**。

    这是刻意的：外层 `WHERE` 里的 `x IN (SELECT y FROM t)` 中的 `y` 属于
    子查询作用域，若一并收进来，跨作用域判定会把不同层的列当成同层比较。
    """
    columns = []
    pending = [expression]
    while pending:
        node = pending.pop()
        if isinstance(node, exp.Query):
            continue
        if isinstance(node, exp.Column):
            columns.append(node)
        pending.extend(node.iter_expressions())
    return columns


def mandatory_conjuncts(node: exp.Expression) -> list[exp.Expression] | None:
    """只返回**由 AND 保证成立**的谓词；出现 OR 即返回 None。

    「条件真的生效」要求它不被任何 OR 分支绕过。OR 里的谓词可能在某个分支为假，
    不能算必然成立，所以整条判定作废（None）而非挑出可用部分——挑出来会得出
    「条件已覆盖」的错误结论。
    """
    if isinstance(node, exp.Paren):
        return mandatory_conjuncts(node.this)
    if isinstance(node, exp.Or):
        return None
    if isinstance(node, exp.And):
        left = mandatory_conjuncts(node.this)
        right = mandatory_conjuncts(node.expression)
        if left is None or right is None:
            return None
        return left + right
    return [node]


def parse_sql(sql: str, dialect: str) -> exp.Query | None:
    """解析 SQL；解析失败或不是查询语句时返回 None（不抛异常）。

    调用方需要区分「解析不了」（`unknown`，如实报告）与「能解析但有问题」
    （`repair`），所以这里不抛异常、也不返回哨兵字符串。
    """
    try:
        parsed = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        return None
    if not isinstance(parsed, exp.Query):
        return None
    return parsed


def unwrap_query(parsed: exp.Query) -> exp.Query:
    """把 `WITH ... SELECT ...` 的 With 壳剥掉，返回里面真正的查询体。

    sqlglot 对 `WITH x AS (...) SELECT ...` 返回的是 `With(this=Select(...))`
    而不是 Select。不剥的话，`scope_where` / `immediate_nested_queries`
    这些按 Select 结构读 `args['where']` 的辅助函数在带 CTE 的 SQL 上会
    全部返回空 —— 那正好漏掉 CTE 里外两层的全部条件。
    """
    while isinstance(parsed, exp.With) and isinstance(parsed.this, exp.Query):
        parsed = parsed.this
    return parsed


def scope_where(scope: exp.Query):
    """`scope` 的 WHERE 表达式，没有则 None。"""
    where = scope.args.get("where")
    return where.this if where is not None else None


def scope_having(scope: exp.Query):
    """`scope` 的 HAVING 表达式，没有则 None。"""
    having = scope.args.get("having")
    return having.this if having is not None else None


def scope_join_ons(scope: exp.Query) -> list[exp.Expression]:
    """`scope` 里每个 JOIN 的 ON 表达式（无 ON 或 USING 连接的跳过）。"""
    ons = []
    for join in scope.args.get("joins") or ():
        on = join.args.get("on")
        if on is None or join.args.get("using"):
            continue
        ons.append(on)
    return ons


def scope_select_columns(scope: exp.Query) -> list[exp.Column]:
    """`scope` 的 SELECT 列表里的列（含聚合、CASE 内部的列）。

    要求覆盖核对器要判断「用户要的国家出现在结果里」，必须看 SELECT；
    只看 WHERE 会把「按国家分组」漏掉。
    """
    columns: list[exp.Column] = []
    for expression in scope.expressions:
        columns.extend(scope_columns(expression))
    return columns


def scope_condition_columns(scope: exp.Query) -> list[exp.Column]:
    """本层 SELECT 表达式**之外**的条件列。

    `scope_columns` 刻意不进入子查询，所以 CASE WHEN 这种写在 SELECT 里的
    条件它收不到。而「条件是否被落实」恰恰要看到这些列 —— 少了它，
    「金额超过 100 的发票」只写在 CASE 分支里时会被判成没有条件。
    """
    columns: list[exp.Column] = []
    for expression in scope.expressions:
        columns.extend(_case_condition_columns(expression))
    return columns


def _case_condition_columns(node: exp.Expression) -> list[exp.Column]:
    """只收 CASE 判定分支（`ifs`）与默认值里的列，不收 `then`/`else` 的结果列。

    `then` 是「满足条件时返回什么」，`ifs` 才是「条件是什么」。
    混进来会把 `CASE WHEN a > 1 THEN b ELSE c END` 里的 b/c 当成条件列。
    """
    columns: list[exp.Column] = []
    pending = [node]
    while pending:
        current = pending.pop()
        if isinstance(current, exp.Query):
            continue
        if isinstance(current, exp.Case):
            for branch in current.args.get("ifs") or ():
                pending.append(branch.this)
            if current.args.get("default") is not None:
                pending.append(current.args["default"])
            continue
        if isinstance(current, exp.Column):
            columns.append(current)
        pending.extend(current.iter_expressions())
    return columns


def scope_group_keys(scope: exp.Query) -> list[exp.Column]:
    """GROUP BY 的分组列。"""
    columns: list[exp.Column] = []
    for expression in scope.args.get("group") or ():
        columns.extend(scope_columns(expression))
    return columns


def scope_order_keys(scope: exp.Query) -> list[exp.Expression]:
    """ORDER BY 的排序表达式（可能带 DESC/ASC，取 `this`）。"""
    return [item.this for item in scope.args.get("order") or ()]


def scope_limit(scope: exp.Query) -> int | None:
    """LIMIT 的字面量取值；动态 LIMIT（参数/表达式）返回 None。"""
    limit = scope.args.get("limit")
    if limit is None:
        return None
    value = limit.expression if isinstance(limit, exp.Limit) else limit
    if isinstance(value, exp.Literal) and value.is_number:
        try:
            return int(float(value.this))
        except (TypeError, ValueError):
            return None
    return None


def scope_distinct(scope: exp.Query) -> bool:
    """SELECT 是否带 DISTINCT。"""
    return bool(scope.args.get("distinct"))


def scope_predicates(scope: exp.Query) -> list[exp.Expression]:
    """本层全部可能承载条件的表达式：WHERE + HAVING + 每个 JOIN 的 ON。

    按实施方案 §5.4：条件可能出现在这些位置，只查 WHERE 会漏掉
    「条件被挪到 HAVING」和「LEFT JOIN 的 ON 里」两类情况。
    """
    predicates = []
    where = scope_where(scope)
    if where is not None:
        predicates.append(where)
    having = scope_having(scope)
    if having is not None:
        predicates.append(having)
    predicates.extend(scope_join_ons(scope))
    return predicates


__all__ = [
    "table_identity",
    "table_alias",
    "resolve_identity",
    "edge_table_key",
    "immediate_nested_queries",
    "cte_queries",
    "scope_columns",
    "mandatory_conjuncts",
    "parse_sql",
    "unwrap_query",
    "scope_where",
    "scope_having",
    "scope_join_ons",
    "scope_select_columns",
    "scope_condition_columns",
    "scope_group_keys",
    "scope_order_keys",
    "scope_limit",
    "scope_distinct",
    "scope_predicates",
]
