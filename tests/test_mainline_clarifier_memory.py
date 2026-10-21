from datetime import datetime
from types import SimpleNamespace

import app.memory as memory_module
from app.clarifier import SemanticClarifier
from app.memory import SessionMemory
from app.nl2sql.normalizer import normalize


def test_growth_rate_without_a_measure_asks_for_measure() -> None:
    decision = SemanticClarifier().decide("同比增长率是多少？")

    assert decision.status == "ask"
    assert decision.missing_slots == ("measure",)


def test_complete_year_over_year_question_does_not_repeat_time_clarification() -> None:
    question = "今年销售额相比去年增长幅度是多少？"
    normalized = normalize(question, time_anchor=datetime(2026, 6, 1))

    decision = SemanticClarifier().decide(normalized.normalized, normalized)

    assert decision.status == "continue", decision
    assert "销售额" in decision.intent.measures


def test_sales_growth_without_a_comparison_period_asks_for_period() -> None:
    decision = SemanticClarifier().decide("销售额增长率是多少？")

    assert decision.status == "ask"
    assert decision.missing_slots == ("comparison_period",)


def test_growth_with_only_one_explicit_year_still_asks_for_comparison_period() -> None:
    question = "2023 年销售额增长率是多少？"
    normalized = normalize(question)

    decision = SemanticClarifier().decide(normalized.normalized, normalized)

    assert decision.status == "ask"
    assert decision.missing_slots == ("comparison_period",)


def test_growth_with_two_explicit_years_has_a_comparison_period() -> None:
    question = "2023 年相比 2022 年销售额增长率是多少？"
    normalized = normalize(question)

    decision = SemanticClarifier().decide(normalized.normalized, normalized)

    assert decision.status == "continue", decision


def test_order_count_examples_are_resolved_as_invoice_measure_not_typo_ambiguity() -> None:
    for phrase in ("订单数", "订单量"):
        question = f"{phrase}增长率是多少？"
        normalized = normalize(question, time_anchor=datetime(2024, 6, 15))
        decision = SemanticClarifier().decide(normalized.normalized, normalized)

        assert any(
            entity.canonical == "invoice" and entity.via == "mention"
            for entity in normalized.entities
        ), normalized
        assert phrase in normalized.normalized
        assert not any("候选存在歧义" in item for item in normalized.unresolved)
        assert decision.status == "ask", decision
        assert decision.missing_slots == ("comparison_period",)


def test_growth_with_only_one_relative_period_still_asks_for_comparison_period() -> None:
    normalized = normalize("去年销售额增长率是多少？", time_anchor=datetime(2026, 6, 1))

    decision = SemanticClarifier().decide(normalized.normalized, normalized)

    assert decision.status == "ask"
    assert decision.missing_slots == ("comparison_period",)


def test_growth_with_explicit_and_relative_periods_has_a_comparison_period() -> None:
    question = "2023 年销售额增长率是多少？用户补充的对比期是前年。"
    normalized = normalize(question, time_anchor=datetime(2024, 6, 15))

    decision = SemanticClarifier().decide(normalized.normalized, normalized)

    assert decision.status == "continue", decision


def test_same_explicit_and_relative_period_does_not_count_as_a_comparison() -> None:
    question = "2023 年销售额增长率是多少？用户补充的对比期是去年。"
    normalized = normalize(question, time_anchor=datetime(2024, 6, 15))

    decision = SemanticClarifier().decide(normalized.normalized, normalized)

    assert decision.status == "ask"
    assert decision.missing_slots == ("comparison_period",)


def test_complete_non_growth_question_does_not_require_a_time_range() -> None:
    decision = SemanticClarifier().decide("列出所有客户名称")

    assert decision.status == "continue", decision


def test_unanchored_human_pronouns_ask_for_the_customer_or_employee() -> None:
    for question in (
        "他总共消费了多少？",
        "她总共消费了多少？",
        "他买过哪些曲目？",
        "那他总共消费了多少？",
    ):
        normalized = normalize(question)

        decision = SemanticClarifier().decide(normalized.normalized, normalized)

        assert decision.status == "ask", (question, decision)
        assert decision.missing_slots == ("person_reference",), (question, decision)
        assert decision.reason_code == "unresolved_person_reference", (question, decision)


def test_other_customers_does_not_trigger_person_pronoun_clarification() -> None:
    normalized = normalize("其他客户的消费总额是多少？")

    decision = SemanticClarifier().decide(normalized.normalized, normalized)

    assert decision.status == "continue", decision


def test_generic_customer_context_does_not_resolve_a_person_pronoun() -> None:
    question = "平台上一共有多少位客户？\n用户补充或修改：他总共消费了多少？"
    normalized = normalize(question)

    decision = SemanticClarifier().decide(normalized.normalized, normalized)

    assert decision.status == "ask", decision
    assert decision.reason_code == "unresolved_person_reference"


def test_named_person_in_context_resolves_a_followup_pronoun() -> None:
    question = "客户 Frank Harris 是谁？\n用户补充或修改：他总共消费了多少？"
    normalized = normalize(question)

    decision = SemanticClarifier().decide(normalized.normalized, normalized)

    assert decision.status == "continue", decision


def test_named_chinese_person_in_context_resolves_a_followup_pronoun() -> None:
    question = "客户张三是谁？\n用户补充或修改：他总共消费了多少？"
    normalized = normalize(question)

    decision = SemanticClarifier().decide(normalized.normalized, normalized)

    assert decision.status == "continue", decision


def test_pending_clarification_is_isolated_and_can_be_cancelled() -> None:
    memory = SessionMemory(max_sessions=4, ttl_seconds=1800)
    memory.begin_clarification("session-a", "同比增长率是多少？", ("measure",))

    other_question, other_action = memory.prepare_question("session-b", "销售额")
    resumed, resumed_action = memory.prepare_question("session-a", "销售额")

    assert (other_question, other_action) == ("销售额", "")
    assert resumed_action == "pending_clarification_resumed"
    assert "同比增长率是多少？" in resumed and "销售额" in resumed

    memory.begin_clarification("session-a", "同比增长率是多少？", ("measure",))
    cancelled, action = memory.prepare_question("session-a", "取消")
    assert (cancelled, action) == ("", "pending_cancelled")


def test_pending_clarification_can_accumulate_multiple_incomplete_replies() -> None:
    memory = SessionMemory(max_sessions=4, ttl_seconds=1800)
    memory.begin_clarification(
        "session-a", "增长率是多少？", ("measure", "comparison_period")
    )

    first, first_action = memory.prepare_question("session-a", "销售额")
    assert first_action == "pending_clarification_resumed"
    assert "增长率是多少？" in first and "销售额" in first

    memory.begin_clarification("session-a", first, ("comparison_period",))
    second, second_action = memory.prepare_question(
        "session-a", "2023 年相比 2022 年"
    )
    assert second_action == "pending_clarification_resumed"
    assert "增长率是多少？" in second
    assert "销售额" in second
    assert "2023 年相比 2022 年" in second


def test_comparison_period_reply_is_labeled_as_baseline_not_current_period() -> None:
    memory = SessionMemory(max_sessions=4, ttl_seconds=1800)
    memory.begin_clarification(
        "session-a", "2023 年销售额增长率是多少？", ("comparison_period",)
    )

    combined, action = memory.prepare_question("session-a", "2022 年")

    assert action == "pending_clarification_resumed"
    assert "2023 年销售额增长率是多少？" in combined
    assert "用户明确补充的增长率比较周期（对比期/基期）：2022 年" in combined


def test_new_question_replaces_pending_clarification_without_merging() -> None:
    memory = SessionMemory(max_sessions=4, ttl_seconds=1800)
    memory.begin_clarification("session-a", "增长率是多少？", ("measure",))

    question, action = memory.prepare_question(
        "session-a", "新问题：平台上一共有多少位客户？"
    )

    assert question == "新问题：平台上一共有多少位客户？"
    assert action == "pending_replaced_by_new_question"
    state = memory.pending("session-a")
    assert state is not None
    assert state.pending_question == ""
    assert state.pending_slots == ()


def test_session_memory_expires_state_after_configured_ttl(monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(
        memory_module,
        "time",
        SimpleNamespace(monotonic=lambda: now[0]),
    )
    memory = SessionMemory(max_sessions=4, ttl_seconds=60)
    memory.begin_clarification("session-a", "增长率是多少？", ("measure",))

    now[0] += 59
    assert memory.pending("session-a") is not None

    now[0] += 2
    assert memory.pending("session-a") is None


def test_session_memory_capacity_evicts_least_recently_used_session() -> None:
    memory = SessionMemory(max_sessions=2, ttl_seconds=1800)
    memory.begin_clarification("session-a", "问题 A", ("slot",))
    memory.begin_clarification("session-b", "问题 B", ("slot",))
    assert memory.pending("session-a") is not None  # A becomes most recently used.

    memory.begin_clarification("session-c", "问题 C", ("slot",))

    assert memory.pending("session-a") is not None
    assert memory.pending("session-b") is None
    assert memory.pending("session-c") is not None
