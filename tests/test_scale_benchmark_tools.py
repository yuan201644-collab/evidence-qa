from datetime import date
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.cases.scale_structured_v01.score_scale_run import (
    linear_percentile,
    semantic_rows,
    table_for_id,
    validate_select,
)


def test_linear_percentile_uses_interpolation_for_ten_samples() -> None:
    assert linear_percentile(list(range(1, 11)), 0.95) == 9.55


@pytest.mark.parametrize(
    ("case_id", "expected"),
    [
        ("S1K-Q01", ("sales_scale_1000", 1_000)),
        ("S10K-Q10", ("sales_scale_10000", 10_000)),
        ("S100K-Q03", ("sales_scale_100000", 100_000)),
    ],
)
def test_table_for_case_id(case_id: str, expected: tuple[str, int]) -> None:
    assert table_for_id(case_id) == expected


def test_table_for_case_id_rejects_unknown_tier() -> None:
    with pytest.raises(ValueError):
        table_for_id("S1M-Q01")


def test_validate_select_allows_only_the_expected_qualified_scale_table() -> None:
    validate_select(
        "SELECT COUNT(*) FROM scale.sales_scale_1000 LIMIT 1000",
        "sales_scale_1000",
    )


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM public.track",
        "SELECT * FROM scale.sales_scale_10000",
        "SELECT * FROM sales_scale_1000",
        "SELECT 1; SELECT 2",
        "DELETE FROM scale.sales_scale_1000",
    ],
)
def test_validate_select_rejects_other_tables_or_non_selects(statement: str) -> None:
    with pytest.raises(ValueError):
        validate_select(statement, "sales_scale_1000")


def test_semantic_month_comparison_normalizes_date_or_month_text_only() -> None:
    columns = ["month", "total_amount"]
    actual = semantic_rows("S1K-Q08", columns, [(date(2024, 1, 1), 12.5)])
    reference = semantic_rows("S1K-Q08", columns, [("2024-01", 12.5)])
    assert actual == reference == [("2024-01", 12.5)]


def test_semantic_month_comparison_does_not_normalize_other_queries() -> None:
    columns = ["month", "total_amount"]
    rows = [(date(2024, 1, 1), 12.5)]
    assert semantic_rows("S1K-Q03", columns, rows) == rows
