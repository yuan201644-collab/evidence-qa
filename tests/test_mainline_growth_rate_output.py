from app.nl2sql.generator import SYSTEM_PROMPT
from app.nl2sql.pipeline import build_conclusion


def test_growth_rate_prompt_fixes_percent_scale_and_zero_denominator() -> None:
    assert "增长率、同比或环比" in SYSTEM_PROMPT
    assert "(本期值 - 对比期值) / NULLIF(对比期值, 0) * 100" in SYSTEM_PROMPT
    assert "别名以 `_percent` 结尾" in SYSTEM_PROMPT


def test_prompt_keeps_a_single_requested_name_as_one_result_column() -> None:
    assert "只要求一个\"姓名/名称\"列" in SYSTEM_PROMPT
    assert "应合并成一个结果列" in SYSTEM_PROMPT


def test_prompt_preserves_current_period_after_baseline_clarification() -> None:
    assert "必须将 X 作为增长率的对比期" in SYSTEM_PROMPT
    assert "保留原问题中已明确的本期" in SYSTEM_PROMPT


def test_prompt_distinguishes_record_count_from_amount_sum() -> None:
    assert "订单数/发票数/笔数" in SYSTEM_PROMPT
    assert "使用 COUNT(*)" in SYSTEM_PROMPT
    assert "不得误用金额求和" in SYSTEM_PROMPT


def test_percent_alias_is_rendered_with_percent_sign() -> None:
    conclusion = build_conclusion(
        [{"sales_growth_percent": -2.465469}],
        ["sales_growth_percent"],
    )

    assert conclusion == "查询结果：sales_growth_percent：-2.465469%。"


def test_non_percent_alias_keeps_raw_numeric_scale() -> None:
    conclusion = build_conclusion(
        [{"sales_growth_rate": -0.024655}],
        ["sales_growth_rate"],
    )

    assert conclusion == "查询结果：sales_growth_rate：-0.024655。"
