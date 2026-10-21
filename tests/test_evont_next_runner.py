from __future__ import annotations

import sys
from pathlib import Path

import pytest

# eval/experiments/ 被 .gitignore 排除（实验中间产物，仅存在于运行机）。
# evont_next_runner 内部还会 import 同目录其他模块，只探测文件存在不够 ——
# 直接尝试真实 import，失败就模块级 skip。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from eval.experiments.evont_next.evont_metrics import (
        boot_ci,
        extract_sql,
        mcnemar_p,
        parse_linking_prediction,
        paired_stats,
        score_linking_prediction,
        schema_scope,
    )
    from eval.experiments.evont_next import evont_next_runner
except ModuleNotFoundError:  # pragma: no cover - CI 环境
    pytest.skip("eval/experiments/evont_next 不在本 checkout 中（CI 环境）", allow_module_level=True)


def test_extract_sql_keeps_with_preamble() -> None:
    sql = 'WITH purchase AS (SELECT 1 AS id) SELECT id FROM purchase;'
    assert extract_sql("```sql\n" + sql + "\n```") == sql.removesuffix(";")


def test_mcnemar_is_two_sided_exact() -> None:
    assert mcnemar_p(5, 0) == 0.0625
    assert mcnemar_p(0, 0) is None


def test_bootstrap_is_reproducible() -> None:
    assert boot_ci([0.0, 0.5, 1.0], seed=17) == boot_ci([0.0, 0.5, 1.0], seed=17)


def test_paired_stats_uses_questions_as_units() -> None:
    rows = []
    for repeat, value in ((1, 0.0), (2, 1.0)):
        rows.extend([
            {"id": "q1", "repeat": repeat, "arm": "A", "table_recall": value,
             "field_recall": value, "executed": True},
            {"id": "q1", "repeat": repeat, "arm": "B", "table_recall": 1.0,
             "field_recall": 1.0, "executed": True},
        ])
    result = paired_stats(rows, "A", "B")
    assert result["pairs"] == 1
    assert result["field_recall_mean_diff"] == 0.5


def test_paired_stats_keeps_failed_or_missing_arm_as_zero() -> None:
    rows = [
        {"id": "q1", "arm": "A", "table_recall": None, "field_recall": None,
         "completed": False},
        {"id": "q1", "arm": "C", "table_recall": 1.0, "field_recall": 0.5,
         "completed": True},
    ]
    result = paired_stats(rows, "A", "C", expected_ids=["q1", "q2"])
    assert result["pairs"] == 2
    assert result["mean_field_recall_left"] == 0.0
    assert result["mean_field_recall_right"] == 0.25
    assert result["field_recall_mean_diff"] == 0.25


def test_c_arm_exposes_only_retrieved_schema_subset() -> None:
    all_tables = {"public.wanted", "public.other"}
    assert schema_scope("C", all_tables, {"public.wanted", "unknown.table"}) == {"public.wanted"}
    assert schema_scope("A", all_tables) == all_tables
    assert schema_scope("B", all_tables) == all_tables


def test_linking_score_penalizes_irrelevant_candidates_and_tracks_rank() -> None:
    case = {"required_tables": ["Sales.Header", "Sales.Detail"],
            "required_columns": ["Sales.Header.Date", "Sales.Detail.Amount"]}
    prediction = {"tables": ["Sales.Detail", "Sales.Header", "Other.Table"],
                  "fields": ["Sales.Detail.Amount", "Other.Table.Noise", "Sales.Header.Date"]}
    result = score_linking_prediction(prediction, case)
    assert result["table_precision"] == 0.6667
    assert result["table_recall"] == 1.0
    assert result["table_recall_at_1"] == 0.5
    assert result["field_precision"] == 0.6667
    assert result["field_recall"] == 1.0


def test_linking_group_summary_uses_macro_recall_and_accepts_linking_records() -> None:
    rows = [
        {"table_recall": 1.0, "field_recall": 0.0, "table_precision": 1.0,
         "field_precision": 0.0, "latency_ms": 10, "usage": {"total_tokens": 5}, "calls": [],
         "completed": True},
        {"table_recall": 0.0, "field_recall": 1.0, "table_precision": 0.0,
         "field_precision": 1.0, "latency_ms": 20, "usage": {"total_tokens": 7}, "calls": [],
         "completed": True},
    ]
    result = evont_next_runner.score_group(rows)
    assert result["mean_field_recall"] == 0.5
    assert result["completed"] == 2


def test_linking_group_summary_counts_missing_metrics_as_zero() -> None:
    result = evont_next_runner.score_group([
        {"field_recall": 1.0, "table_recall": 1.0, "latency_ms": 1,
         "usage": {}, "calls": [], "completed": True},
        {"field_recall": None, "table_recall": None, "latency_ms": 1,
         "usage": {}, "calls": [], "completed": False},
    ])
    assert result["mean_field_recall"] == 0.5


def test_linking_json_parser_deduplicates_without_dropping_unknowns() -> None:
    value = parse_linking_prediction('```json\n{"tables":["A.T","A.T","X.Y"],"fields":[]}\n```')
    assert value == {"tables": ["A.T", "X.Y"], "fields": []}


def test_linking_json_parser_rejects_candidates_over_contract_limit() -> None:
    import json
    import pytest

    body = json.dumps({"tables": [f"A.T{i}" for i in range(11)], "fields": []})
    with pytest.raises(ValueError, match="数量上限"):
        parse_linking_prediction(body)


def test_linking_solver_uses_retrieved_schema_only_for_c(monkeypatch) -> None:
    raw = {"database": "test", "dialect": "postgres", "tables": [
        {"schema": "public", "table": "Orders", "description": "orders",
         "columns": [{"name": "OrderID", "type": "integer"}, {"name": "Total", "type": "numeric"}],
         "foreign_keys": []},
        {"schema": "public", "table": "Unrelated", "description": "other data",
         "columns": [{"name": "Noise", "type": "text"}], "foreign_keys": []},
    ]}
    entries = [{"id": "K1", "title": "orders", "aliases": ["orders"], "tables": ["public.Orders"],
                "description": "order total", "mapping": {}, "constraints": []}]
    case = {"id": "q1", "question": "orders total", "required_tables": ["public.Orders"],
            "required_columns": ["public.Orders.Total"]}

    class FakeClient:
        def __init__(self, responses):
            self.responses = iter(responses)
            self.calls = []

        def chat(self, _system, _user):
            result = next(self.responses)
            self.calls.append({"usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4}})
            return result

    client = FakeClient([
        '{"query":"orders","tables":["public.Orders"]}',
        '{"tables":["public.Orders"],"fields":["public.Orders.Total"]}',
    ])
    monkeypatch.setattr(evont_next_runner, "MeasuredClient", lambda **_kwargs: client)
    row = evont_next_runner.solve_task4_linking(case, "C", 1, raw, entries, object(), "test")
    assert row["completed"] is True
    assert row["table_recall"] == 1.0
    assert row["field_recall"] == 1.0
    assert row["visible_schema_tables"] == ["public.Orders"]
    assert row["visible_schema_field_count"] == 2
    assert len(row["calls"]) == 2


def test_linking_batch_records_arm_exceptions_as_zero_without_dropping_pairs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(evont_next_runner, "solve_task4_linking",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic")))
    case = {"id": "q1", "question": "example", "required_tables": ["A.T"],
            "required_columns": ["A.T.C"]}
    rows = evont_next_runner.run_batch([case], ("A", "B", "C"), {"tables": []},
                                      {"A": [], "B": [], "C": []}, tmp_path,
                                      object(), "phase", "linking")
    assert len(rows) == 3
    assert all(row["field_recall"] == 0.0 and not row["completed"] for row in rows)
    summary = __import__("json").loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["paired"]["A_to_C"]["pairs"] == 1
    assert summary["groups"]["A"]["mean_field_recall"] == 0.0
