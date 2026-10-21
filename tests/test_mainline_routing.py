from app.orchestrator import route
from app.routing_signals import analyse


def test_relational_use_of_guanlian_routes_to_sql_not_graph() -> None:
    question = "按销售原因类型统计关联的不同订单数。"

    signals = analyse(question)
    plan = route(question)

    assert "关联" not in signals["connectors"]
    assert "原因" not in signals["doc_intent"]
    assert "统计" in signals["sql_intent"]
    assert "订单数" in signals["sql_intent"]
    assert [name for name, _ in plan.steps] == ["nl2sql"]


def test_explicit_multihop_question_still_routes_to_graph() -> None:
    plan = route("2024 年销售冠军是谁，并且他的成功方法是什么")

    assert [name for name, _ in plan.steps] == ["graph_reason"]
