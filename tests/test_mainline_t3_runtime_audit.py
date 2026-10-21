from decimal import Decimal

from eval.mainline.audit_t3_runtime_replay import answer_contains_value, executed_sql, value_text


def test_value_text_matches_application_decimal_precision():
    assert value_text(Decimal("-2.465468896")) == "-2.465469"
    assert value_text(Decimal("481.450000")) == "481.45"


def test_answer_numeric_match_uses_numeric_boundaries():
    assert answer_contains_value("客户数：59位", 59)
    assert not answer_contains_value("159 位客户", 59)
    assert answer_contains_value("增长率：-2.465469%", Decimal("-2.465468896"))


def test_executed_sql_uses_last_successful_execution_trace():
    response = {
        "trace": [
            {"tool": "nl2sql", "output": {"row_count": 1, "columns": ["x"], "sql": "SELECT 1"}},
            {"tool": "nl2sql", "output": {"error": "failed", "sql": "SELECT 2"}},
            {"tool": "nl2sql", "output": {"row_count": 1, "columns": ["x"], "sql": "SELECT 3"}},
        ]
    }
    assert executed_sql(response)["sql"] == "SELECT 3"


def test_executed_sql_ignores_candidate_without_result_metadata():
    assert executed_sql({"trace": [{"tool": "nl2sql", "output": {"sql": "SELECT 1"}}]}) is None
