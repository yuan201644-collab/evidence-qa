from types import SimpleNamespace

import pytest

from app.nl2sql.pipeline import Nl2SqlPipeline, build_conclusion
from app.nl2sql.schema import ColumnMeta, SchemaCatalog, TableMeta


class _Client:
    def __init__(self, sql: str) -> None:
        self.sql = sql

    def chat(self, _system: str, _user: str) -> str:
        return self.sql


class _Executor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def execute(self, sql: str) -> SimpleNamespace:
        self.sql = sql
        return SimpleNamespace(
            rows=self.rows,
            columns=["customer_id"],
            row_count=len(self.rows),
            error=None,
            sqlstate=None,
        )


class _ExpandingExecutor:
    def __init__(self, *, expanded_rows: int, fail_expansion: bool = False) -> None:
        self.expanded_rows = expanded_rows
        self.fail_expansion = fail_expansion
        self.calls: list[str] = []

    def execute(self, sql: str, *, max_result_rows=None, max_result_bytes=None):
        self.calls.append(sql)
        if len(self.calls) == 1:
            rows = [{"customer_id": value} for value in (1, 2)]
            return SimpleNamespace(
                rows=rows,
                columns=["customer_id"],
                row_count=len(rows),
                error=None,
                sqlstate=None,
                result_truncated=False,
            )
        if self.fail_expansion:
            return SimpleNamespace(
                rows=[], columns=["customer_id"], row_count=0,
                error="SQL 执行失败：timeout", sqlstate="57014", result_truncated=False,
            )
        rows = [{"customer_id": value} for value in range(1, self.expanded_rows + 1)]
        return SimpleNamespace(
            rows=rows,
            columns=["customer_id"],
            row_count=len(rows),
            error=None,
            sqlstate=None,
            result_truncated=False,
        )


def _run(
    sql: str,
    *,
    limit: int,
    rows: list[dict],
    executor=None,
    large_limit: int = 0,
):
    catalog = SchemaCatalog(
        [
            TableMeta(
                name="customer",
                schema="public",
                primary_key=("customer_id",),
                columns=(
                    ColumnMeta(
                        "customer_id", "integer", nullable=False, is_primary_key=True
                    ),
                ),
            )
        ]
    )
    pipeline = Nl2SqlPipeline(
        _Client(sql),
        catalog=catalog,
        executor=executor or _Executor(rows),
        auto_limit=limit,
        large_result_max_rows=large_limit,
        semantic_planning_enabled=False,
    )
    return pipeline.run("查询 customer 表")


@pytest.mark.parametrize(
    ("sql", "limit", "rows", "expected_warning"),
    [
        (
            "SELECT customer_id FROM customer",
            2,
            [{"customer_id": 1}, {"customer_id": 2}],
            True,
        ),
        (
            "SELECT customer_id FROM customer",
            2,
            [{"customer_id": 1}],
            False,
        ),
        (
            "SELECT customer_id FROM customer LIMIT 2",
            2,
            [{"customer_id": 1}, {"customer_id": 2}],
            False,
        ),
        (
            "SELECT customer_id FROM customer",
            1,
            [{"customer_id": 1}],
            True,
        ),
    ],
)
def test_possible_truncation_is_reported_only_when_auto_limit_was_reached(
    sql: str,
    limit: int,
    rows: list[dict],
    expected_warning: bool,
) -> None:
    outcome = _run(sql, limit=limit, rows=rows)

    assert outcome.error is None
    assert outcome.result_limit_reached is expected_warning
    execution_output = next(
        step.output
        for step in reversed(outcome.trace)
        if step.tool == "nl2sql" and "row_count" in step.output
    )
    if " LIMIT " not in f" {sql.upper()} ":
        assert execution_output["automatic_limit"] == limit
        assert execution_output["possible_truncation"] is expected_warning
    else:
        assert "automatic_limit" not in execution_output
        assert "possible_truncation" not in execution_output

    conclusion = build_conclusion(
        outcome.rows,
        outcome.columns,
        automatic_limit=outcome.automatic_limit,
        result_limit_reached=outcome.result_limit_reached,
    )
    if expected_warning:
        assert "不能视为完整结果" in conclusion
        assert "当前接口不提供续页或导出" not in conclusion
    else:
        assert "不能视为完整结果" not in conclusion


def test_capped_query_is_revalidated_and_expanded_with_a_hard_limit() -> None:
    executor = _ExpandingExecutor(expanded_rows=4)
    outcome = _run(
        "SELECT customer_id FROM customer",
        limit=2,
        rows=[],
        executor=executor,
        large_limit=5,
    )

    assert outcome.error is None
    assert len(executor.calls) == 2
    assert "LIMIT 2" in executor.calls[0].upper()
    assert "LIMIT 6" in executor.calls[1].upper()
    assert outcome.sql == executor.calls[1]
    assert outcome.row_count == 4
    assert outcome.result_limit_reached is False
    assert outcome.automatic_limit == 5
    materialization = outcome.trace[-1]
    assert materialization.output["purpose"] == "bounded_result_materialization"
    assert materialization.output["row_count"] == 4
    probe = next(step for step in outcome.trace if "row_count" in step.output and not step.output.get("purpose"))
    assert probe.hit is None


def test_large_result_hard_row_limit_marks_partial_output() -> None:
    executor = _ExpandingExecutor(expanded_rows=6)
    outcome = _run(
        "SELECT customer_id FROM customer",
        limit=2,
        rows=[],
        executor=executor,
        large_limit=5,
    )

    assert outcome.error is None
    assert outcome.row_count == 5
    assert outcome.result_limit_reached is True
    assert outcome.automatic_limit == 5
    assert "不能视为完整结果" in build_conclusion(
        outcome.rows,
        outcome.columns,
        automatic_limit=outcome.automatic_limit,
        result_limit_reached=outcome.result_limit_reached,
    )


def test_failed_large_materialization_keeps_the_initial_partial_result() -> None:
    executor = _ExpandingExecutor(expanded_rows=0, fail_expansion=True)
    outcome = _run(
        "SELECT customer_id FROM customer",
        limit=2,
        rows=[],
        executor=executor,
        large_limit=5,
    )

    assert outcome.error is None
    assert outcome.row_count == 2
    assert outcome.result_limit_reached is True
    assert "SQL 执行失败" in outcome.trace[-1].output["error"]
