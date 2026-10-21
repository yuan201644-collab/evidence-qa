from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# score_cs15_v06_supplemental.py 位于 eval/reports/ 下的盲审报告目录（.gitignore 排除，
# 仅存在于运行机）。CI checkout 后缺失会让 import 在 collection 阶段直接炸掉，
# 用探测 + 模块级 skip 代替。
_blind_review_dir = PROJECT_ROOT / "eval" / "reports" / "cross_source" / "architecture_cs15_v06_blind_review_20261009"
if not (_blind_review_dir / "score_cs15_v06_supplemental.py").exists():
    pytest.skip("eval/reports 下的 cs15 盲审脚本不在本 checkout 中（CI 环境）", allow_module_level=True)

from eval.reports.cross_source.architecture_cs15_v06_blind_review_20261009.score_cs15_v06_supplemental import (
    EXPECTED_STRATEGIES,
    _v06_supplemental_gold_validation,
)


def _c15_28(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "case_id": "C15-28",
        "expected_retrieval_state": "bounded_not_found",
        "expected_retrieval_attempts": 2,
        "expected_retrieval_strategies": EXPECTED_STRATEGIES.copy(),
    }
    row.update(overrides)
    return row


def test_v06_schema_adapter_only_passes_placeholders_to_schema_delegate() -> None:
    gold = [_c15_28()]
    original = copy.deepcopy(gold)
    received: list[dict[str, object]] = []

    def delegate(rows: list[dict[str, object]], **_: object) -> None:
        received.extend(rows)

    _v06_supplemental_gold_validation(gold, delegate)

    assert gold == original
    assert received[0]["expected_retrieval_statuses"] == ["schema_only_not_scored"] * 2
    assert received[0]["expected_candidate_counts"] == [0, 0]
    assert "expected_retrieval_statuses" not in gold[0]
    assert "expected_candidate_counts" not in gold[0]


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_retrieval_attempts": 1},
        {"expected_retrieval_strategies": ["hybrid_bm25_vector", "other"]},
        {"expected_retrieval_state": "clause_present_expected"},
        {"expected_candidate_counts": [0, 0]},
        {"expected_retrieval_statuses": ["hit", "miss"]},
    ],
)
def test_v06_schema_adapter_rejects_unregistered_c15_28_criteria(
    overrides: dict[str, object],
) -> None:
    called = False

    def delegate(_: list[dict[str, object]], **__: object) -> None:
        nonlocal called
        called = True

    with pytest.raises(ValueError):
        _v06_supplemental_gold_validation([_c15_28(**overrides)], delegate)
    assert not called


def test_v06_schema_adapter_requires_exactly_one_c15_28() -> None:
    with pytest.raises(ValueError, match="恰有一条 C15-28"):
        _v06_supplemental_gold_validation([], lambda *_args, **_kwargs: None)
