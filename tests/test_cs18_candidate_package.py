"""Runner-side contracts for the frozen CS18 v0.6 candidate (no external calls)."""

import json
import shutil
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "eval" / "cross_source_heldout" / "tools"
sys.path.insert(0, str(TOOLS))

# validate_cs18_candidate.py 位于 eval/cross_source_heldout/tools/（.gitignore 排除，
# 仅存在于运行机）。CI checkout 后缺失会让 import 在 collection 阶段直接炸掉，
# 用探测 + 模块级 skip 代替（run_architecture_comparison.py 已入库，无需探测）。
if not (TOOLS / "validate_cs18_candidate.py").exists():
    pytest.skip("eval/cross_source_heldout/tools 下的 cs18 脚本不在本 checkout 中（CI 环境）", allow_module_level=True)

import run_architecture_comparison as runner  # noqa: E402
import validate_cs18_candidate as validator  # noqa: E402


def _copy_coverage_artifacts(destination: Path) -> Path:
    package = ROOT / runner.CS18_PACKAGE_ROOT
    destination.mkdir(parents=True)
    for name in ("protocol.md", "coverage.csv", "cs18_coverage_contract.json"):
        shutil.copyfile(package / name, destination / name)
    return destination / "protocol.md"


def test_cs18_v05_coverage_artifacts_match_64_questions():
    runtime = ROOT / runner.CS18_RUNTIME_PATH
    questions = runner.load_questions(runtime, allow_cs08_count=True)
    protocol = ROOT / runner.CS18_PROTOCOL_PATH

    contract, artifacts = runner._validate_cs18_coverage_artifacts(protocol, questions)

    assert len(questions) == 64
    assert contract["total_questions"] == 64
    assert sum(contract["coverage_dimension_counts"].values()) == 64
    assert len(artifacts["contract_sha256"]) == 64
    assert len(artifacts["coverage_csv_sha256"]) == 64


def test_cs18_questions_reach_the_explicit_cross_source_route():
    questions = runner.load_questions(
        ROOT / runner.CS18_RUNTIME_PATH, allow_cs08_count=True
    )

    runner.assert_questions_have_explicit_fact_routes(questions)


def test_cs18_coverage_rejects_wrong_subcategory_quota(tmp_path):
    protocol = _copy_coverage_artifacts(tmp_path / "package")
    contract_path = protocol.parent / "cs18_coverage_contract.json"
    payload = json.loads(contract_path.read_text(encoding="utf-8"))
    payload["cs18_coverage_contract"]["case_groups"]["computable_001_024"].pop()
    contract_path.write_text(json.dumps(payload), encoding="utf-8")
    questions = runner.load_questions(
        ROOT / runner.CS18_RUNTIME_PATH, allow_cs08_count=True
    )

    with pytest.raises(RuntimeError, match="case_groups|coverage contract"):
        runner._validate_cs18_coverage_artifacts(protocol, questions)


def test_cs18_formal_profile_requires_exact_paths_and_matched_arms():
    profile = runner._cs18_profile_for_paths(
        ROOT / runner.CS18_RUNTIME_PATH,
        ROOT / runner.CS18_PROTOCOL_PATH,
        "matched_reference_fact",
    )
    assert profile["package_id"] == runner.CS18_PACKAGE_ID

    with pytest.raises(RuntimeError, match="matched_reference_ablation"):
        runner._cs18_profile_for_paths(
            ROOT / runner.CS18_RUNTIME_PATH,
            ROOT / runner.CS18_PROTOCOL_PATH,
            "historical_old_new",
        )

    with pytest.raises(RuntimeError, match="同时指定"):
        runner._cs18_profile_for_paths(
            ROOT / runner.CS18_RUNTIME_PATH,
            ROOT / runner.CS17_MATCHED_PROTOCOL_PATH,
            "matched_reference_fact",
        )


def test_cs18_gold_anchor_is_required_without_reading_gold():
    assert runner._cs18_gold_digest(
        type("Args", (), {"gold_sha256": "A" * 64})(), True
    ) == "a" * 64
    with pytest.raises(RuntimeError, match="Gold manifest"):
        runner._cs18_gold_digest(type("Args", (), {"gold_sha256": None})(), True)


def test_cs18_prerun_and_postrun_source_manifest_scopes_match():
    before = runner._source_manifest_for_package(runner.CS18_PACKAGE_ID)
    after = runner._source_manifest_for_package(runner.CS18_PACKAGE_ID)
    baseline = runner._source_manifest_for_package(None)

    assert before == after
    assert set(before) - set(baseline) == set(runner.CS18_SOURCE_HASH_PATHS) - set(baseline)


def test_cs18_validator_reports_the_runtime_hash_expected_by_runner():
    candidate_root = ROOT / "eval" / "cross_source_heldout" / "2026Q4_fresh_cs18_v0.6_candidate"
    digests = validator.runtime_manifest_digests(candidate_root)

    assert digests["runtime_manifest_sha256"] == runner.sha256_file(
        candidate_root / "runtime" / "SHA256SUMS.txt"
    )
    assert digests["runtime_inventory_sha256"] == runner.sha256_file(
        candidate_root / "runtime_manifest.json"
    )

