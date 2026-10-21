from eval.mainline.score_t4_development import _coverage, _executed_sql


def test_scorer_uses_sql_from_successful_execution_not_last_response_candidate():
    response = {
        "sql": "SELECT * FROM MissingTable",
        "trace": [
            {"tool": "nl2sql", "output": {
                "sql": 'SELECT "Sales"."CurrencyRate"."AverageRate" FROM "Sales"."CurrencyRate"',
                "row_count": 12,
                "columns": ["averagerate"],
            }},
            {"tool": "nl2sql", "output": {
                "sql": "SELECT * FROM MissingTable",
                "row_count": 0,
                "error": "schema validation failed",
            }},
        ],
    }

    assert _executed_sql(response) == (
        True,
        'SELECT "Sales"."CurrencyRate"."AverageRate" FROM "Sales"."CurrencyRate"',
    )


def test_scorer_does_not_treat_failed_execution_as_success():
    response = {
        "sql": "SELECT * FROM MissingTable",
        "trace": [{"tool": "nl2sql", "output": {
            "sql": "SELECT * FROM MissingTable",
            "row_count": 0,
            "error": "relation does not exist",
        }}],
    }

    assert _executed_sql(response) == (False, None)


def test_linker_coverage_counts_foreign_key_relation_candidates():
    trace = [{
        "tool": "schema_linker",
        "output": {
            "selected_tables": [{"qualified_name": "Sales.SalesReason"}],
            "relation_tables": [{
                "qualified_name": "Sales.SalesOrderHeaderSalesReason",
                "relation_only": True,
            }],
        },
    }]

    coverage = _coverage(
        trace,
        {"sales.salesreason", "sales.salesorderheadersalesreason"},
        "schema_linker",
        "selected_tables",
    )

    assert coverage == {
        "covered": ["sales.salesorderheadersalesreason", "sales.salesreason"],
        "missing": [],
    }
