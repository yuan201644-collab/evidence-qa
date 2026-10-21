import sys
from pathlib import Path

import pytest

# eval/experiments/ 被 .gitignore 排除（实验中间产物，仅存在于运行机）。
# evaluate_task4_holdout_sql_v01 内部还会 import 同目录其他模块，只探测文件存在
# 不够 —— 直接尝试真实 import，失败就模块级 skip。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from eval.experiments.evont_next.evaluate_task4_holdout_sql_v01 import outputs
except ModuleNotFoundError:  # pragma: no cover - CI 环境
    pytest.skip("eval/experiments/evont_next 不在本 checkout 中（CI 环境）", allow_module_level=True)


def test_outputs_converts_column_keyed_trace_rows_to_ordered_values():
    record = {"trace": [{"tool": "validate_execute", "output": {
        "columns": ["name", "amount"],
        "rows": [{"name": "A", "amount": 3}, {"name": "B", "amount": 4}],
        "error": None,
    }}]}
    rows, error = outputs(record)
    assert rows == [["A", 3], ["B", 4]]
    assert error is None


def test_outputs_preserves_execution_failure():
    record = {"trace": [{"tool": "validate_execute", "output": {
        "columns": None, "rows": None, "error": "read-only guard rejected statement",
    }}]}
    rows, error = outputs(record)
    assert rows == []
    assert error == "read-only guard rejected statement"


def test_outputs_marks_missing_execution_trace_as_failure():
    rows, error = outputs({"error": "generation failed", "trace": []})
    assert rows == []
    assert error == "generation failed"
