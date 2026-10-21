"""Offline contract tests for the CS18 v0.6 sealed-run scorer."""

import copy
import csv
import hashlib
import json
import random
import sys
from collections import Counter
from pathlib import Path

import pytest

# 本文件所有用例都绑定 2026Q4_fresh_cs18_v0.6_candidate 运行包 —— 该目录被
# .gitignore 排除，仅存在于跑过 CS18 实验的机器上。CI 缺数据时整文件跳过。
from conftest import require_private_data

pytestmark = require_private_data("cs18_candidate")

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "eval" / "cross_source_heldout" / "tools"
sys.path.insert(0, str(TOOLS))

import score_architecture_comparison as scorer  # noqa: E402


PACKAGE = ROOT / scorer.CS18_PACKAGE_ROOT
RUNTIME = PACKAGE / "runtime"
GOLD_PATH = ROOT / "eval" / "cross_source_heldout" / "2026Q4_fresh_cs18_v0.6_gold" / "gold.jsonl"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _package_contract() -> dict:
    wrapped = json.loads((PACKAGE / "cs18_coverage_contract.json").read_text(encoding="utf-8"))
    raw = wrapped["cs18_coverage_contract"]
    with (PACKAGE / "coverage.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    dimensions = {row["case_id"]: row["coverage_dimension"] for row in rows}
    return {
        "schema": raw["schema"],
        "total_questions": raw["total_questions"],
        "case_groups": raw["case_groups"],
        "coverage_dimensions": dimensions,
        "coverage_dimension_counts": dict(Counter(dimensions.values())),
    }


def _source_manifest() -> dict[str, str]:
    paths = {
        relative: _sha(ROOT / relative)
        for relative in scorer.CS18_SOURCE_HASH_PATHS
    }
    app_paths = [
        path for path in (ROOT / "app").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    ]
    paths.update({path.relative_to(ROOT).as_posix(): _sha(path) for path in app_paths})
    return paths


def _cs18_config(tmp_path: Path) -> dict:
    questions = _read_jsonl(RUNTIME / "questions.jsonl")
    source_manifest = _source_manifest()
    database_snapshot = {"snapshot": "synthetic-unit-fixture"}
    model_config = {
        "provider_identity": "offline-test-provider",
        "api_base_identity": "https://offline.example.invalid/v1",
        "model": "offline-test-model",
    }
    config = {
        "runtime_package_id": scorer.CS18_PACKAGE_ID,
        "runtime_package_path": scorer.CS18_PACKAGE_ROOT,
        "protocol_path": scorer.CS18_PROTOCOL_PATH,
        "protocol_sha256": _sha(PACKAGE / "protocol.md"),
        "runtime_manifest_sha256": _sha(RUNTIME / "SHA256SUMS.txt"),
        "runtime_files": {
            line.split("  ", 1)[1]: line.split("  ", 1)[0]
            for line in (RUNTIME / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
        },
        "required_question_count": 64,
        "question_count": 64,
        "runtime_directory_name": "runtime",
        "arm_pair": "matched_reference_fact",
        "arm_names": list(scorer.MATCHED_ARMS),
        "arm_design": scorer.CS14_ARM_DESIGN,
        "seed": 20261009,
        "time_anchor": "2026-10-09",
        "gold_sha256": _sha(GOLD_PATH),
        "cs18_coverage_contract": _package_contract(),
        "cs18_coverage_contract_sha256": _sha(PACKAGE / "cs18_coverage_contract.json"),
        "cs18_coverage_csv_sha256": _sha(PACKAGE / "coverage.csv"),
        "source_manifest": source_manifest,
        "database_and_index_snapshot": database_snapshot,
        "model_config": model_config,
    }
    api_identity = {
        "provider_identity": model_config["provider_identity"],
        "api_base_identity": model_config["api_base_identity"],
        "model": model_config["model"],
    }
    expected_bindings = {
        "runtime_manifest_sha256": config["runtime_manifest_sha256"],
        "gold_sha256": config["gold_sha256"],
        "protocol_sha256": config["protocol_sha256"],
        "cs18_coverage_contract_sha256": config["cs18_coverage_contract_sha256"],
        "cs18_coverage_csv_sha256": config["cs18_coverage_csv_sha256"],
        "source_manifest_sha256": scorer._canonical_sha256(source_manifest),
        "database_snapshot_sha256": scorer._canonical_sha256(database_snapshot),
        "api_identity": api_identity,
    }
    artifact_bindings = {
        "gold_review": {
            "runtime_manifest_sha256": config["runtime_manifest_sha256"],
            "gold_sha256": config["gold_sha256"],
        },
        "code_review": {"source_manifest_sha256": expected_bindings["source_manifest_sha256"]},
        "protocol_review": {
            "protocol_sha256": config["protocol_sha256"],
            "cs18_coverage_contract_sha256": config["cs18_coverage_contract_sha256"],
            "cs18_coverage_csv_sha256": config["cs18_coverage_csv_sha256"],
        },
        "database_preflight": {
            "database_snapshot_sha256": expected_bindings["database_snapshot_sha256"],
        },
        "api_smoke": {"api_identity": api_identity},
    }
    artifacts = {}
    for name, binding in artifact_bindings.items():
        report_path = tmp_path / f"{name}.report.json"
        report_path.write_text(json.dumps({"artifact": name}), encoding="utf-8")
        artifacts[name] = {
            "status": "pass",
            "path": str(report_path.resolve()),
            "sha256": _sha(report_path),
            "independent": name in {"gold_review", "code_review", "protocol_review"},
            "bindings": binding,
        }
    evidence_path = tmp_path / "cs18_gate_evidence.json"
    evidence_path.write_text(json.dumps({
        "schema": "cs18_pre_run_gate_v1",
        "status": "ready_for_freeze",
        "bindings": expected_bindings,
        "artifacts": artifacts,
    }, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    config["gate_evidence_path"] = str(evidence_path.resolve())
    config["gate_evidence_sha256"] = _sha(evidence_path)
    return config


def test_cs18_v06_package_gold_and_prerun_bindings_pass_offline(tmp_path):
    questions = _read_jsonl(RUNTIME / "questions.jsonl")
    gold_rows = _read_jsonl(GOLD_PATH)
    config = _cs18_config(tmp_path)

    assert scorer._validate_cs18_pre_run(config, questions, RUNTIME / "questions.jsonl")
    scorer._validate_gold_schema(
        gold_rows,
        nullable_metric_case_ids=scorer.CS18_NULLABLE_METRIC_CASE_IDS,
        additional_early_stop_reasons=scorer.CS18_ADDITIONAL_EARLY_STOP_REASONS,
    )
    scorer._validate_cs18_gold_groups(gold_rows, questions, config["cs18_coverage_contract"])
    assert scorer.CS18_PACKAGE_ID.endswith("v0.6_candidate")


def test_cs18_v06_coverage_ids_and_dimensions_are_exact():
    questions = _read_jsonl(RUNTIME / "questions.jsonl")
    contract = _package_contract()
    groups = scorer._cs18_case_groups(contract, {row["case_id"] for row in questions})

    assert {key: len(values) for key, values in groups.items()} == scorer.CS18_CASE_GROUP_COUNTS
    assert len(scorer.CS18_COVERAGE_DIMENSION_BY_CASE) == 64

    bad_group = copy.deepcopy(contract)
    bad_group["case_groups"]["structural_block_025_044"].remove("PSR-CS18-025")
    with pytest.raises(ValueError, match="case_groups|64 个 case_id"):
        scorer._cs18_case_groups(bad_group, {row["case_id"] for row in questions})

    bad_dimension = copy.deepcopy(contract)
    bad_dimension["coverage_dimensions"]["PSR-CS18-043"] = "structural_contract_inactive"
    with pytest.raises(ValueError, match="子类别"):
        scorer._cs18_case_groups(bad_dimension, {row["case_id"] for row in questions})


@pytest.mark.parametrize(
    ("case_id", "field", "value"),
    [
        ("PSR-CS18-043", "expected_metric_state", "prerequisite_blocked"),
        ("PSR-CS18-057", "expected_metric_state", "sla_not_stated"),
        ("PSR-CS18-061", "expected_retrieval_state", "retrieval_not_required"),
    ],
)
def test_cs18_gold_state_mapping_rejects_semantic_drift(case_id, field, value):
    questions = _read_jsonl(RUNTIME / "questions.jsonl")
    rows = _read_jsonl(GOLD_PATH)
    target = next(row for row in rows if row["case_id"] == case_id)
    target[field] = value

    with pytest.raises(ValueError, match="status/metric/retrieval mapping"):
        scorer._validate_cs18_gold_groups(rows, questions, _package_contract())


def test_cs18_gold_metric_arithmetic_and_dimension_must_agree():
    questions = _read_jsonl(RUNTIME / "questions.jsonl")
    rows = _read_jsonl(GOLD_PATH)
    target = next(row for row in rows if row["case_id"] == "PSR-CS18-011")
    target["required_metric"]["elapsed_seconds"] -= 1
    target["required_metric"]["within_limit"] = True

    with pytest.raises(ValueError, match="sla_equal|指标与冻结"):
        scorer._validate_cs18_gold_groups(rows, questions, _package_contract())


def test_cs18_mapping_absence_gold_snippets_match_runtime_refs():
    from app.contracts import ToolResult, TraceStep
    from app.cross_source_fact_plan import _mapping_absence_ref

    rows = _read_jsonl(GOLD_PATH)
    sql_template = (
        "SELECT ce.equipment_id, ce.contract_id "
        "FROM psr_cs18.contract_equipment AS ce "
        "JOIN psr_cs18.equipment AS e ON ce.equipment_id = e.equipment_id "
        "JOIN psr_cs18.contracts AS c ON ce.contract_id = c.contract_id "
        "WHERE ce.equipment_id = '{equipment_id}'"
    )
    for case_id, equipment_id in (
        ("PSR-CS18-033", "PSR-EQ-5333"),
        ("PSR-CS18-034", "PSR-EQ-5334"),
    ):
        result = ToolResult(
            data=[],
            trace=[TraceStep(
                step_id=1,
                tool="cross_source_sql",
                output={
                    "sql": sql_template.format(equipment_id=equipment_id),
                    "row_count": 0,
                },
                elapsed_ms=0.0,
            )],
        )
        actual_ref = _mapping_absence_ref(result, equipment_id)
        gold = next(row for row in rows if row["case_id"] == case_id)

        assert actual_ref is not None
        assert gold["required_absence_refs"][0]["snippets"] == [actual_ref.snippet]


def test_cs18_prerun_rejects_protocol_and_gate_binding_drift(tmp_path):
    questions = _read_jsonl(RUNTIME / "questions.jsonl")
    config = _cs18_config(tmp_path)
    bad_protocol = copy.deepcopy(config)
    bad_protocol["protocol_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="protocol_sha256"):
        scorer._validate_cs18_pre_run(bad_protocol, questions, RUNTIME / "questions.jsonl")

    bad_coverage = copy.deepcopy(config)
    bad_coverage["cs18_coverage_csv_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="cs18_coverage_csv_sha256"):
        scorer._validate_cs18_pre_run(bad_coverage, questions, RUNTIME / "questions.jsonl")

    bad_source = copy.deepcopy(config)
    bad_source["source_manifest"][scorer.CS18_SOURCE_HASH_PATHS[0]] = "0" * 64
    with pytest.raises(ValueError, match="当前源码/协议与运行前封存哈希"):
        scorer._validate_cs18_pre_run(bad_source, questions, RUNTIME / "questions.jsonl")

    bad_gate = copy.deepcopy(config)
    gate_path = Path(bad_gate["gate_evidence_path"])
    evidence = json.loads(gate_path.read_text(encoding="utf-8"))
    evidence["bindings"]["gold_sha256"] = "0" * 64
    gate_path.write_text(json.dumps(evidence), encoding="utf-8")
    bad_gate["gate_evidence_sha256"] = _sha(gate_path)
    with pytest.raises(ValueError, match="gate-evidence 与当前题包"):
        scorer._validate_cs18_pre_run(bad_gate, questions, RUNTIME / "questions.jsonl")


def test_cs18_v05_is_not_accepted_as_the_current_formal_profile():
    legacy_questions = ROOT / "eval" / "cross_source_heldout" / (
        "2026Q4_fresh_cs18_v0.5_candidate/runtime/questions.jsonl"
    )
    with pytest.raises(ValueError, match="只接受 v0.6"):
        scorer._cs18_profile(
            {"runtime_package_id": "2026Q4_fresh_cs18_v0.5_candidate"}, legacy_questions,
        )


def test_cs18_schedule_is_recomputed_from_registered_seed(tmp_path):
    questions = _read_jsonl(RUNTIME / "questions.jsonl")
    seed = 20261009
    rng = random.Random(seed)
    pairs = []
    for question in questions:
        arms = list(scorer.MATCHED_ARMS)
        rng.shuffle(arms)
        pairs.append({"case_id": question["case_id"], "arms": arms})
    rng.shuffle(pairs)
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(json.dumps({"seed": seed, "pairs": pairs}), encoding="utf-8")

    scorer._validate_cs18_schedule(schedule_path, {"seed": seed}, questions)
    with pytest.raises(ValueError, match="配对顺序"):
        schedule_path.write_text(json.dumps({"seed": seed, "pairs": pairs[:-1]}), encoding="utf-8")
        scorer._validate_cs18_schedule(schedule_path, {"seed": seed}, questions)


def test_cs18_automatic_scores_are_labeled_diagnostic_only():
    semantics = scorer._cs18_coverage_validation(_package_contract())

    assert semantics["primary_outcome_computed"] is False
    assert semantics["automatic_scores"] == "diagnostic_only"
    assert semantics["population_inference"] == "not_supported_for_purposive_synthetic_cases"
    assert scorer.CS18_SCORING_SEMANTICS["primary_outcome"] == (
        "human_blinded_review_and_adjudication"
    )
