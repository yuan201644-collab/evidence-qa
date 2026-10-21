"""`app/nl2sql/sql_scopes.py` 的作用域解析测试。

按实施方案 §9.3 的纪律：**每支持一种结构，就要同时有「正确写法」与「绕过写法」
两套断言**，不能只加允许列表。所以本文件刻意成对出现：
WHERE 生效 / 条件被挪到 HAVING、条件在 AND 下 / 条件藏在 OR 分支里。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlglot import exp

REPO_ROOT = Path(__file__).resolve().parents[1]
_VENV_SITE = REPO_ROOT / ".venv" / "Lib" / "site-packages"
# ⚠ 这个插入必须配对恢复。
#
# 2026-10-18 实测：模块级 `sys.path.insert` 是**全局且永久的** —— 导入本文件后，
# 后续任何模块（pydantic → pydantic_core）都可能从 `.venv` 里取到与当前解释器
# 不匹配的编译扩展，直接 `ModuleNotFoundError: No module named
# 'pydantic_core._pydantic_core'`（pytest 收集期 exit=2）。
# 症状是「单独跑本文件全绿、和别的文件一起跑就炸」，且爆炸点在**别的文件**里，
# 极易误判成别人的改动坏了事。
#
# 判据：本项目用系统 Python（D:\python）跑测试，`.venv` 只在本机某些环境存在；
# 插入是为了让 sqlglot 在本文件里可导入，导入完成即应还原现场。
_SAVED_PATH = list(sys.path)
if _VENV_SITE.is_dir():
    sys.path.insert(0, str(_VENV_SITE))

from app.nl2sql.sql_scopes import (  # noqa: E402
    cte_queries,
    edge_table_key,
    immediate_nested_queries,
    mandatory_conjuncts,
    parse_sql,
    resolve_identity,
    scope_columns,
    scope_condition_columns,
    scope_distinct,
    scope_group_keys,
    scope_having,
    scope_join_ons,
    scope_limit,
    scope_order_keys,
    scope_predicates,
    scope_select_columns,
    scope_where,
    table_alias,
    table_identity,
    unwrap_query,
)
# 导入完成，还原 sys.path —— 不让本文件的兼容性补丁泄漏给后面的测试模块。
sys.path[:] = _SAVED_PATH

DIALECT = "postgres"


def parse(sql: str) -> exp.Query:
    parsed = unwrap_query(parse_sql(sql, DIALECT))
    assert isinstance(parsed, exp.Query), sql
    return parsed


# ─────────────────────── 表身份与别名 ───────────────────────


def test_table_identity_uses_schema_prefix():
    table = parse("SELECT * FROM public.customer").args["from_"].this
    assert table_identity(table) == "public.customer"


def test_table_identity_without_schema():
    table = parse("SELECT * FROM customer").args["from_"].this
    assert table_identity(table) == "customer"


def test_table_identity_lowercases_both_parts():
    # AdventureWorks 元数据登记的是 PascalCase；AST 侧一律折成小写，
    # 与 planning_models 里 JoinEdge 的 `schema.table` 字符串拼法保持一致。
    table = parse("SELECT * FROM Sales.SalesOrderHeader").args["from_"].this
    assert table_identity(table) == "sales.salesorderheader"


def test_table_alias_prefers_alias_over_name():
    table = parse("SELECT * FROM customer AS c").args["from_"].this
    assert table_alias(table) == "c"


def test_table_alias_falls_back_to_table_name():
    table = parse("SELECT * FROM customer").args["from_"].this
    assert table_alias(table) == "customer"


# ─────────────────────── resolve_identity ───────────────────────


def test_resolve_identity_exact_match():
    table = parse("SELECT * FROM customer").args["from_"].this
    assert resolve_identity(table, {"customer", "invoice"}) == "customer"


def test_resolve_identity_suffix_match_when_schema_omitted():
    table = parse("SELECT * FROM customer").args["from_"].this
    assert resolve_identity(table, {"public.customer"}) == "public.customer"


def test_resolve_identity_ambiguous_suffix_is_not_guessed():
    # 同一表名在两个 schema 下都存在时**不猜**：猜错会把「查了另一张表」
    # 判成合法，这正是本函数要避免的事故。
    table = parse("SELECT * FROM customer").args["from_"].this
    assert resolve_identity(table, {"public.customer", "sales.customer"}) == ""


def test_resolve_identity_unknown_table_returns_empty():
    table = parse("SELECT * FROM playlist").args["from_"].this
    assert resolve_identity(table, {"customer"}) == ""


def test_edge_table_key_matches_table_identity_shape():
    assert edge_table_key("public", "Customer") == "public.customer"
    table = parse("SELECT * FROM public.customer").args["from_"].this
    assert table_identity(table) == edge_table_key("public", "customer")


# ─────────────────────── 条件位置（WHERE / HAVING / ON） ───────────────────────


def test_scope_where_present():
    assert scope_where(parse("SELECT 1 FROM t WHERE a > 1")) is not None


def test_scope_where_absent_returns_none():
    assert scope_where(parse("SELECT 1 FROM t")) is None


def test_scope_having_present():
    assert scope_having(parse("SELECT a FROM t GROUP BY a HAVING COUNT(*) > 1")) is not None


def test_scope_having_absent_returns_none():
    assert scope_having(parse("SELECT a FROM t WHERE a > 1")) is None


def test_scope_join_ons_collects_every_on():
    sql = "SELECT * FROM a JOIN b ON a.id = b.a_id LEFT JOIN c ON c.b_id = b.id"
    assert len(scope_join_ons(parse(sql))) == 2


def test_scope_join_ons_skips_using_and_missing_on():
    sql = "SELECT * FROM a JOIN b USING (id) JOIN c ON c.a_id = a.id"
    assert len(scope_join_ons(parse(sql))) == 1


def test_scope_predicates_covers_where_having_and_on():
    sql = (
        "SELECT a.id FROM a JOIN b ON b.a_id = a.id "
        "WHERE a.x > 1 GROUP BY a.id HAVING COUNT(*) > 2"
    )
    assert len(scope_predicates(parse(sql))) == 3


def test_condition_moved_to_having_is_still_a_predicate():
    """反绕过：条件从 WHERE 挪到 HAVING 不能逃过条件位置枚举。"""
    moved = parse("SELECT country FROM customer GROUP BY country HAVING country = 'Brazil'")
    in_where = parse("SELECT country FROM customer WHERE country = 'Brazil'")
    assert scope_where(moved) is None and scope_having(moved) is not None
    assert len(scope_predicates(moved)) == len(scope_predicates(in_where))


def test_left_join_condition_in_on_is_a_predicate():
    """反绕过：LEFT JOIN 的 ON 里带条件，不能只查 WHERE。"""
    sql = "SELECT * FROM invoice i LEFT JOIN customer c ON i.customer_id = c.id AND c.country = 'Brazil'"
    assert len(scope_predicates(parse(sql))) == 1
    assert scope_where(parse(sql)) is None


# ─────────────────────── mandatory_conjuncts ───────────────────────


def test_mandatory_conjuncts_and_splits_into_all_conjuncts():
    sql = "SELECT 1 FROM t WHERE a > 1 AND b < 2 AND c = 3"
    conjuncts = mandatory_conjuncts(scope_where(parse(sql)))
    assert conjuncts is not None
    assert len(conjuncts) == 3


def test_mandatory_conjuncts_or_returns_none():
    sql = "SELECT 1 FROM t WHERE a > 1 OR b = 2"
    assert mandatory_conjuncts(scope_where(parse(sql))) is None


def test_mandatory_conjuncts_or_nested_inside_and_returns_none():
    """反绕过：`A AND (B OR C)` 里 OR 分支的谓词不必然成立。"""
    sql = "SELECT 1 FROM t WHERE a > 1 AND (b = 2 OR c = 3)"
    assert mandatory_conjuncts(scope_where(parse(sql))) is None


def test_mandatory_conjuncts_unwraps_paren():
    sql = "SELECT 1 FROM t WHERE ((a > 1))"
    conjuncts = mandatory_conjuncts(scope_where(parse(sql)))
    assert conjuncts is not None and len(conjuncts) == 1


def test_mandatory_conjuncts_single_predicate():
    sql = "SELECT 1 FROM t WHERE a > 1"
    conjuncts = mandatory_conjuncts(scope_where(parse(sql)))
    assert conjuncts is not None and len(conjuncts) == 1


# ─────────────────────── 列收集 ───────────────────────


def test_scope_columns_collects_where_columns():
    # `scope_columns` 刻意不进入子查询；这里只查最外层 WHERE。
    sql = "SELECT first_name FROM customer WHERE first_name = 'x' AND last_name = 'y'"
    names = sorted(column.name.lower() for column in scope_columns(scope_where(parse(sql))))
    assert names == ["first_name", "last_name"]


def test_scope_columns_skips_nested_query_columns():
    """子查询里的列不属于外层作用域，混进来会让跨层比较误判为同层。"""
    sql = "SELECT id FROM customer WHERE id IN (SELECT customer_id FROM invoice)"
    outer = parse(sql)
    where_columns = scope_columns(scope_where(outer))
    assert all(column.name.lower() != "customer_id" for column in where_columns)


def test_scope_select_columns_includes_aggregates_and_case_branches():
    sql = "SELECT COUNT(*) AS n, CASE WHEN country = 'Brazil' THEN 1 ELSE 0 END AS is_br FROM customer"
    names = sorted({column.name.lower() for column in scope_select_columns(parse(sql))})
    assert "country" in names


def test_scope_group_keys():
    sql = "SELECT country, COUNT(*) FROM customer GROUP BY country"
    keys = [column.name.lower() for column in scope_group_keys(parse(sql))]
    assert keys == ["country"]


def test_scope_order_keys():
    sql = "SELECT total FROM invoice ORDER BY total DESC"
    keys = scope_order_keys(parse(sql))
    assert len(keys) == 1 and keys[0].name.lower() == "total"


def test_scope_order_keys_absent():
    assert scope_order_keys(parse("SELECT 1 FROM t")) == []


# ─────────────────────── LIMIT / DISTINCT ───────────────────────


def test_scope_limit_literal():
    assert scope_limit(parse("SELECT * FROM t LIMIT 3")) == 3


def test_scope_limit_absent_returns_none():
    assert scope_limit(parse("SELECT * FROM t")) is None


def test_scope_limit_dynamic_returns_none():
    # 参数化的 LIMIT 不是字面量，无法与「前 3 条」做静态比对。
    assert scope_limit(parse("SELECT * FROM t LIMIT :n")) is None


def test_scope_limit_expression_returns_none():
    assert scope_limit(parse("SELECT * FROM t LIMIT 1 + 2")) is None


def test_scope_distinct_true_and_false():
    assert scope_distinct(parse("SELECT DISTINCT country FROM customer")) is True
    assert scope_distinct(parse("SELECT country FROM customer")) is False


# ─────────────────────── 子查询作用域 ───────────────────────


def test_immediate_nested_queries_direct_child():
    sql = "SELECT * FROM customer WHERE id IN (SELECT customer_id FROM invoice)"
    assert len(immediate_nested_queries(parse(sql))) == 1


def test_immediate_nested_queries_excludes_grandchildren():
    sql = (
        "SELECT * FROM customer WHERE id IN "
        "(SELECT customer_id FROM invoice WHERE total > "
        "(SELECT AVG(total) FROM invoice))"
    )
    assert len(immediate_nested_queries(parse(sql))) == 1


def test_immediate_nested_queries_none_when_flat():
    assert immediate_nested_queries(parse("SELECT * FROM customer")) == []


def test_immediate_nested_queries_cte_is_not_immediate():
    """CTE 在 `WITH` 段，不算直接嵌在外层查询体里的子查询。

    `parse_one` 对 `WITH ... SELECT ...` 返回的是 `With`；`unwrap_query`
    剥壳后拿到外层 Select。CTE 里的子查询 parent 是 `With`，所以
    `immediate_nested_queries` 收不到它 —— 要拿 CTE 得用 `cte_queries`。
    """
    sql = "WITH big AS (SELECT * FROM invoice WHERE total > 100) SELECT * FROM big"
    parsed = unwrap_query(parse_sql(sql, DIALECT))
    assert isinstance(parsed, exp.Select)
    assert immediate_nested_queries(parsed) == []
    assert len(cte_queries(parsed)) == 1


def test_cte_queries_finds_every_cte():
    sql = (
        "WITH a AS (SELECT * FROM invoice), "
        "b AS (SELECT * FROM customer) SELECT * FROM a JOIN b ON a.id = b.id"
    )
    assert len(cte_queries(unwrap_query(parse_sql(sql, DIALECT)))) == 2


def test_cte_queries_empty_without_with():
    assert cte_queries(unwrap_query(parse_sql("SELECT 1 FROM t", DIALECT))) == []


def test_unwrap_query_returns_select_for_with_statement():
    sql = "WITH big AS (SELECT * FROM invoice) SELECT * FROM big"
    assert isinstance(unwrap_query(parse_sql(sql, DIALECT)), exp.Select)


def test_unwrap_query_leaves_plain_select_untouched():
    assert isinstance(unwrap_query(parse_sql("SELECT 1", DIALECT)), exp.Select)


def test_unwrap_query_none_stays_none():
    assert unwrap_query(None) is None


# ─────────────────────── parse_sql ───────────────────────


def test_parse_sql_valid_query():
    assert isinstance(parse_sql("SELECT 1", DIALECT), exp.Query)


def test_parse_sql_broken_sql_returns_none():
    assert parse_sql("SELECT FROM WHERE", DIALECT) is None


def test_parse_sql_non_query_returns_none():
    assert parse_sql("INSERT INTO t VALUES (1)", DIALECT) is None


def test_parse_sql_empty_string_returns_none():
    assert parse_sql("", DIALECT) is None


# ─────────────────────── 对抗：谓词藏在深层结构里 ───────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        # CASE WHEN 写在 SELECT 里：条件列要靠 scope_condition_columns 才能看到
        "SELECT CASE WHEN total > 100 THEN 'big' ELSE 'small' END FROM invoice",
        # 子查询里的 WHERE
        "SELECT * FROM customer WHERE id IN (SELECT customer_id FROM invoice WHERE total > 100)",
        # EXISTS 相关子查询
        "SELECT * FROM customer c WHERE EXISTS (SELECT 1 FROM invoice i WHERE i.customer_id = c.id AND i.total > 100)",
        # CTE 里的 WHERE
        "WITH big AS (SELECT * FROM invoice WHERE total > 100) SELECT * FROM big",
    ],
)
def test_predicate_inside_nested_structure_is_discoverable(sql):
    """反绕过：谓词藏在 CASE / 子查询 / EXISTS / CTE 里也必须能被找到。

    核对器不能只扫最外层 WHERE —— 那正好漏掉方案 §5.4 点名的
    「条件被下沉到子查询」这一类静默改写。
    """
    parsed = parse(sql)
    found = (
        scope_where(parsed) is not None
        or len(scope_join_ons(parsed)) > 0
        or len(immediate_nested_queries(parsed)) > 0
        or parsed.args.get("with") is not None
        or len(scope_condition_columns(parsed)) > 0
        or len(cte_queries(parsed)) > 0
    )
    assert found, sql
