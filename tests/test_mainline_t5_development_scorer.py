from eval.mainline.score_t5_development import _RawRunPredictor
from eval.runner import Prediction


def test_t5_scorer_uses_successful_execution_trace_not_last_candidate():
    predictor = _RawRunPredictor(
        [{
            "case_id": "T5-01",
            "http_status": 200,
            "elapsed_ms": 100.0,
            "response": {
                "sql": "SELECT * FROM MissingTable",
                "trace": [
                    {"tool": "nl2sql", "output": {
                        "sql": "SELECT customer_id FROM customer",
                        "row_count": 59,
                        "columns": ["customer_id"],
                    }},
                    {"tool": "nl2sql", "output": {
                        "sql": "SELECT * FROM MissingTable",
                        "row_count": 0,
                        "error": "relation does not exist",
                    }},
                ],
            },
            "error": None,
        }],
        Prediction,
    )

    prediction = predictor.predict_nl2sql({"id": "T5-01"})

    assert prediction.sql == "SELECT customer_id FROM customer"
    assert prediction.error is None


def test_t5_scorer_does_not_score_sql_without_successful_execution_trace():
    predictor = _RawRunPredictor(
        [{
            "case_id": "T5-02",
            "http_status": 200,
            "elapsed_ms": 100.0,
            "response": {
                "sql": "SELECT customer_id FROM customer",
                "trace": [{"tool": "nl2sql", "output": {
                    "sql": "SELECT customer_id FROM customer",
                    "row_count": 0,
                    "error": "execution failed",
                }}],
            },
            "error": None,
        }],
        Prediction,
    )

    prediction = predictor.predict_nl2sql({"id": "T5-02"})

    assert prediction.sql is None
    assert prediction.error == "未返回带实际执行痕迹的 SQL"
