from app.nl2sql.planning_models import JoinEdge, JoinPlan
from app.nl2sql.semantic_validator import SqlSemanticValidator


def _invoice_line_plan() -> JoinPlan:
    edge = JoinEdge(
        left_schema="sales",
        left_table="invoice_line",
        left_columns=("invoice_id",),
        right_schema="sales",
        right_table="invoice",
        right_columns=("id",),
        source="test_fixture.foreign_keys",
        cardinality="many_to_one",
    )
    return JoinPlan(
        tables=("sales.invoice_line", "sales.invoice"),
        edges=(edge,),
    )


def test_declared_foreign_key_join_with_aliases_passes() -> None:
    sql = (
        "SELECT i.id FROM sales.invoice_line AS il "
        "JOIN sales.invoice AS i ON il.invoice_id = i.id"
    )

    result = SqlSemanticValidator().check(sql, _invoice_line_plan())

    assert result.status == "pass", result


def test_join_with_parenthesized_foreign_key_and_filter_passes() -> None:
    sql = (
        "SELECT i.id FROM sales.invoice_line il "
        "JOIN sales.invoice i ON (il.invoice_id = i.id) AND i.status = 'open'"
    )

    result = SqlSemanticValidator().check(sql, _invoice_line_plan())

    assert result.status == "pass", result


def test_or_cannot_weaken_a_valid_foreign_key_condition() -> None:
    sql = (
        "SELECT i.id FROM sales.invoice_line il "
        "JOIN sales.invoice i ON il.invoice_id = i.id OR TRUE"
    )

    result = SqlSemanticValidator().check(sql, _invoice_line_plan())

    assert result.status == "repair"
    assert result.reason_code == "join_condition_contains_or"


def _composite_plan() -> JoinPlan:
    edge = JoinEdge(
        left_schema="sales",
        left_table="order_line",
        left_columns=("tenant_id", "order_id"),
        right_schema="sales",
        right_table="orders",
        right_columns=("tenant_id", "id"),
        source="test_fixture.foreign_keys",
        cardinality="many_to_one",
    )
    return JoinPlan(tables=("sales.order_line", "sales.orders"), edges=(edge,))


def test_all_columns_of_a_composite_foreign_key_are_required() -> None:
    accepted = (
        "SELECT o.id FROM sales.order_line l JOIN sales.orders o "
        "ON l.tenant_id = o.tenant_id AND l.order_id = o.id"
    )
    incomplete = (
        "SELECT o.id FROM sales.order_line l JOIN sales.orders o "
        "ON l.tenant_id = o.tenant_id"
    )

    assert SqlSemanticValidator().check(accepted, _composite_plan()).status == "pass"
    rejected = SqlSemanticValidator().check(incomplete, _composite_plan())
    assert rejected.status == "repair"
    assert rejected.reason_code == "join_condition_unverified"


def test_self_join_is_checked_using_table_instances() -> None:
    edge = JoinEdge(
        left_schema="hr",
        left_table="employee",
        left_columns=("manager_id",),
        right_schema="hr",
        right_table="employee",
        right_columns=("id",),
        source="test_fixture.foreign_keys",
        cardinality="many_to_one",
    )
    plan = JoinPlan(tables=("hr.employee",), edges=(edge,))
    valid = "SELECT e.id FROM hr.employee e JOIN hr.employee m ON e.manager_id = m.id"
    invalid = "SELECT e.id FROM hr.employee e JOIN hr.employee m ON TRUE"

    assert SqlSemanticValidator().check(valid, plan).status == "pass"
    assert SqlSemanticValidator().check(invalid, plan).status != "pass"


def test_parent_sum_through_alias_is_flagged_but_child_sum_is_not() -> None:
    parent_sum = (
        "SELECT SUM(i.total) FROM sales.invoice_line il "
        "JOIN sales.invoice i ON il.invoice_id = i.id"
    )
    child_sum = (
        "SELECT SUM(il.quantity) FROM sales.invoice_line il "
        "JOIN sales.invoice i ON il.invoice_id = i.id"
    )

    parent_result = SqlSemanticValidator().check(parent_sum, _invoice_line_plan())
    child_result = SqlSemanticValidator().check(child_sum, _invoice_line_plan())

    assert parent_result.reason_code == "possible_grain_risk"
    assert child_result.status == "pass", child_result


def test_constant_only_sql_is_rejected_when_query_plan_requires_a_source_table() -> None:
    plan = JoinPlan(tables=("sales.invoice",))

    result = SqlSemanticValidator().check("SELECT CAST(NULL AS DECIMAL) AS sales_growth_rate", plan)

    assert result.status == "repair"
    assert result.reason_code == "query_missing_source_table"


def test_single_table_sql_cannot_drop_a_table_from_multi_table_plan() -> None:
    plan = JoinPlan(tables=("sales.employee", "sales.customer", "sales.invoice"))
    sql = "SELECT employee_id FROM sales.employee"

    result = SqlSemanticValidator().check(sql, plan)

    assert result.status == "repair"
    assert result.reason_code == "query_missing_planned_tables"
    assert set(result.details["missing_tables"]) == {"sales.customer", "sales.invoice"}


def test_growth_rate_ratio_without_percent_alias_is_rejected() -> None:
    plan = JoinPlan(tables=("sales.invoice",))
    sql = "SELECT (current_total - prior_total) / NULLIF(prior_total, 0) AS sales_growth_rate FROM sales.invoice"

    result = SqlSemanticValidator().check(sql, plan, question="销售额同比增长率是多少？")

    assert result.status == "repair"
    assert result.reason_code == "growth_rate_alias_not_percent"


def test_growth_rate_percent_alias_without_scale_is_rejected() -> None:
    plan = JoinPlan(tables=("sales.invoice",))
    sql = "SELECT (current_total - prior_total) / NULLIF(prior_total, 0) AS sales_growth_percent FROM sales.invoice"

    result = SqlSemanticValidator().check(sql, plan, question="销售额同比增长率是多少？")

    assert result.status == "repair"
    assert result.reason_code == "growth_rate_scale_not_percent"


def test_growth_rate_percent_scale_with_nullif_is_accepted() -> None:
    plan = JoinPlan(tables=("sales.invoice",))
    sql = (
        "SELECT ((current_total - prior_total) / NULLIF(prior_total, 0) * 100) "
        "AS sales_growth_percent FROM sales.invoice"
    )

    result = SqlSemanticValidator().check(sql, plan, question="销售额同比增长率是多少？")

    assert result.status == "pass", result


def test_growth_rate_percent_scale_accepts_multiplier_in_numerator() -> None:
    plan = JoinPlan(tables=("sales.invoice",))
    sql = (
        "SELECT ((current_total - prior_total) * 100.0 / NULLIF(prior_total, 0)) "
        "AS sales_growth_percent FROM sales.invoice"
    )

    result = SqlSemanticValidator().check(sql, plan, question="发票数同比增长率是多少？")

    assert result.status == "pass", result


def test_growth_rate_percent_scale_without_nullif_is_rejected() -> None:
    plan = JoinPlan(tables=("sales.invoice",))
    sql = "SELECT ((current_total - prior_total) / prior_total * 100) AS sales_growth_percent FROM sales.invoice"

    result = SqlSemanticValidator().check(sql, plan, question="销售额同比增长率是多少？")

    assert result.status == "repair"
    assert result.reason_code == "growth_rate_zero_denominator_unprotected"


def _customer_genre_purchase_plan() -> JoinPlan:
    edges = (
        JoinEdge("public", "invoice", ("customer_id",), "public", "customer",
                 ("customer_id",), "test_fixture.foreign_keys"),
        JoinEdge("public", "invoice_line", ("invoice_id",), "public", "invoice",
                 ("invoice_id",), "test_fixture.foreign_keys"),
        JoinEdge("public", "invoice_line", ("track_id",), "public", "track",
                 ("track_id",), "test_fixture.foreign_keys"),
        JoinEdge("public", "track", ("genre_id",), "public", "genre",
                 ("genre_id",), "test_fixture.foreign_keys"),
    )
    return JoinPlan(
        tables=("public.customer", "public.invoice", "public.invoice_line",
                "public.track", "public.genre"),
        edges=edges,
    )


def test_nested_exists_checks_local_joins_and_outer_foreign_key_correlation() -> None:
    sql = """
        SELECT c.customer_id
        FROM public.customer c
        WHERE EXISTS (
            SELECT 1
            FROM public.invoice i
            JOIN public.invoice_line il ON il.invoice_id = i.invoice_id
            JOIN public.track t ON t.track_id = il.track_id
            JOIN public.genre g ON g.genre_id = t.genre_id
            WHERE i.customer_id = c.customer_id AND g.name = 'Rock'
        )
    """

    result = SqlSemanticValidator().check(sql, _customer_genre_purchase_plan())

    assert result.status == "pass", result
    assert result.reason_code == "nested_scopes_validated"


def test_nested_exists_rejects_unplanned_outer_correlation() -> None:
    sql = """
        SELECT c.customer_id
        FROM public.customer c
        WHERE EXISTS (
            SELECT 1
            FROM public.invoice i
            WHERE i.billing_address_id = c.customer_id
        )
    """

    result = SqlSemanticValidator().check(sql, _customer_genre_purchase_plan())

    assert result.status == "repair"
    assert result.reason_code == "nested_scope_correlation_unverified"


def test_nested_exists_rejects_outer_reference_without_equality() -> None:
    sql = """
        SELECT c.customer_id FROM public.customer c
        WHERE EXISTS (
            SELECT 1 FROM public.invoice i
            WHERE i.customer_id > c.customer_id
        )
    """

    result = SqlSemanticValidator().check(sql, _customer_genre_purchase_plan())

    assert result.status == "repair"
    assert result.reason_code == "nested_scope_correlation_not_equality"


def test_nested_exists_rejects_outer_correlation_weakened_by_or() -> None:
    sql = """
        SELECT c.customer_id FROM public.customer c
        WHERE EXISTS (
            SELECT 1 FROM public.invoice i
            WHERE i.customer_id = c.customer_id OR i.invoice_id > 0
        )
    """

    result = SqlSemanticValidator().check(sql, _customer_genre_purchase_plan())

    assert result.status == "repair"
    assert result.reason_code == "nested_scope_correlation_contains_or"


def test_nested_exists_requires_every_composite_correlation_column() -> None:
    edge = JoinEdge(
        "sales", "order_line", ("tenant_id", "order_id"),
        "sales", "orders", ("tenant_id", "id"),
        "test_fixture.foreign_keys",
    )
    plan = JoinPlan(tables=("sales.order_line", "sales.orders"), edges=(edge,))
    complete = """
        SELECT o.id FROM sales.orders o
        WHERE EXISTS (
            SELECT 1 FROM sales.order_line l
            WHERE l.tenant_id = o.tenant_id AND l.order_id = o.id
        )
    """
    incomplete = """
        SELECT o.id FROM sales.orders o
        WHERE EXISTS (
            SELECT 1 FROM sales.order_line l WHERE l.order_id = o.id
        )
    """

    accepted = SqlSemanticValidator().check(complete, plan)
    rejected = SqlSemanticValidator().check(incomplete, plan)

    assert accepted.status == "pass", accepted
    assert rejected.status == "repair"
    assert rejected.reason_code == "nested_scope_correlation_unverified"


def test_aggregate_request_cannot_be_filled_with_null_constant() -> None:
    plan = JoinPlan(tables=("sales.document",))
    sql = "SELECT status, NULL::numeric AS rejected_ratio FROM sales.document"

    result = SqlSemanticValidator().check(sql, plan, question="按采购状态统计明细行拒收率")

    assert result.status == "repair"
    assert result.reason_code == "requested_aggregate_missing"


def test_explicitly_false_filter_cannot_be_reported_as_no_data() -> None:
    plan = JoinPlan(tables=("sales.document",))
    sql = "SELECT COUNT(*) AS record_count FROM sales.document WHERE 1 = 0"

    result = SqlSemanticValidator().check(sql, plan, question="统计记录数")

    assert result.status == "repair"
    assert result.reason_code == "query_forces_empty_result"
