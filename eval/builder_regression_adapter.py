"""回归标准检查适配：唯一允许既有h04题要求的最早月环比空值。"""
from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import builder_scoring_adapter as scoring

benchmark = scoring.benchmark
ORIGINAL_CHECK = benchmark.check_cases
EXCEPTION_ID = "nl2sql-h-04"


def assert_null_position(result: Any) -> None:
    positions = {(i, col) for i, row in enumerate(result.rows) for col, value in row.items() if value is None}
    if positions != {(0, "mom_diff")}:
        raise RuntimeError("h04唯一允许空值为第0行mom_diff；发现其他NULL或缺少预期NULL")


def check_regression(cases: list[dict[str, Any]], executor: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    exceptions = [c for c in cases if c["id"] == EXCEPTION_ID]
    if len(exceptions) != 1 or len({c["id"] for c in cases}) != len(cases):
        raise RuntimeError("回归例外用例数量或题号不合规")
    case = exceptions[0]
    if "最早的一个月没有上一个月，差额留空" not in case["question"]:
        raise RuntimeError("h04题干不再包含原先明确的NULL要求")
    gold, checks = ORIGINAL_CHECK([c for c in cases if c["id"] != EXCEPTION_ID], executor)
    first, second = executor.execute(case["gold_sql"]), executor.execute(case["gold_sql"])
    if first.error or second.error or not first.rows or not second.rows:
        raise RuntimeError("h04标准查询无法执行或结果为空")
    assert_null_position(first)
    assert_null_position(second)
    if not benchmark.compare_resultsets(benchmark.result_rows(first), benchmark.result_rows(second),
        order_matters=benchmark.gold_sql_has_order_by(case["gold_sql"])).ok:
        raise RuntimeError("h04标准结果重复执行不稳定")
    gold[EXCEPTION_ID] = first
    checks.append({"id": EXCEPTION_ID, "rows": first.row_count, "stable": True,
                   "intentional_null": {"row": 0, "column": "mom_diff", "count": 1},
                   "basis": "原冻结题干明确最早月没有上个月，环比差额留空；原SQL使用LAG"})
    return gold, checks


def offline_checks() -> None:
    good = benchmark.ExecutionResult(rows=[{"mom_diff": None}, {"mom_diff": 1}], columns=["mom_diff"])
    assert_null_position(good)
    for rows in [[{"mom_diff": 1}, {"mom_diff": None}], [{"amount": None}],
                 [{"mom_diff": None}, {"mom_diff": None}], [{"mom_diff": 1}]]:
        try:
            assert_null_position(benchmark.ExecutionResult(rows=rows))
        except RuntimeError:
            continue
        raise AssertionError("例外检查接受了错误NULL位置、列名、数量")


def main() -> int:
    if scoring.get_arg("--stage") != "regression":
        raise RuntimeError("此适配器只允许回归阶段")
    offline_checks()
    output = Path(scoring.get_arg("--run-dir"))
    benchmark.verify_sources(output)
    path = Path(__file__).resolve()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    lock = output / "regression_adapter_lock.json"
    if lock.exists():
        if benchmark.read(lock)["sha256"] != digest:
            raise RuntimeError("回归检查适配器冻结后改变")
    else:
        before = [r for r in benchmark.load_records(output) if r["phase"] == "regression"]
        if before:
            raise RuntimeError("首次适配前已有回归模型调用，不可隐式重跑")
        snapshot = output / "regression_adapter_snapshot.py"
        snapshot.write_bytes(path.read_bytes())
        benchmark.write_json(lock, {"time": datetime.now(timezone.utc).isoformat(),
            "path": str(path), "sha256": digest, "snapshot": str(snapshot.resolve()),
            "original_error": "标准结果有NULL，需明确口径后重新封存：nl2sql-h-04",
            "existing_regression_records": len(before), "existing_regression_model_calls": 0,
            "exception": {"id": EXCEPTION_ID, "row": 0, "column": "mom_diff", "count": 1},
            "basis": "原题明确最早月差额留空；仅检查允许位置，其他14题仍执行原非NULL检查",
            "solver_or_gold_changed": False, "original_freeze_verified": True})
    benchmark.check_cases = check_regression
    return scoring.main()


if __name__ == "__main__":
    raise SystemExit(main())
