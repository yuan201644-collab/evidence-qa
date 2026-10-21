"""Synthetic-only tests for sealed architecture A/B scoring."""

import copy
import hashlib
import json
import random
import sys
from pathlib import Path

import pytest

# 部分用例读取 eval/cross_source_heldout/ 下未入库的候选运行包（仅存在于运行机）。
# conftest.require_private_data 在数据缺失时（CI）跳过这些用例。
from conftest import require_private_data

TOOLS = Path(__file__).resolve().parents[1] / "eval" / "cross_source_heldout" / "tools"
sys.path.insert(0, str(TOOLS))

from score_architecture_comparison import (  # noqa: E402
    _strict_pass,
    CS14_SOURCE_HASH_PATHS,
    CS17_ADDITIONAL_EARLY_STOP_REASONS,
    CS17_CASE_GROUP_IDS,
    CS17_COVERAGE_SCHEMA,
    CS17_EARLY_STOP_REASON_BY_CASE,
    CS17_NULLABLE_METRIC_CASE_IDS,
    CS17_PACKAGE_ID,
    CS17_PROTOCOL_PATH,
    CS17_RUNTIME_PATH,
    CS17_SOURCE_HASH_PATHS,
    CS14_ARM_DESIGN,
    MATCHED_ARMS,
    CS15_ADDITIONAL_EARLY_STOP_REASONS,
    CS15_CASE_GROUP_IDS,
    CS15_CASE_GROUP_COUNTS,
    CS15_COVERAGE_SCHEMA,
    _cs15_case_groups,
    _cs17_case_groups,
    _validate_cs17_gold_groups,
    _validate_cs17_pre_run,
    _validate_cs17_schedule,
    _validate_cs17_source_manifest,
    _validate_cs15_gold_groups,
    _validate_cs15_pre_run,
    _validate_cs14_gold_groups,
    _validate_cs14_pre_run,
    _validate_gold_schema,
    _validate_pairs,
    score_run,
)
import score_architecture_comparison as scorer_module  # noqa: E402


ARMS = ("old_workflow", "new_fact_verification")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _current_cs14_source_manifest() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    return {path: _sha(root / path) for path in CS14_SOURCE_HASH_PATHS}


def _cs15_synthetic_fixture():
    questions = [{
        "case_id": f"C15-{index:02}",
        "question": f"查询工单 WO-2027-{index:04} 的服务恢复情况",
        "work_order_id": f"WO-2027-{index:04}",
    } for index in range(1, 33)]
    contract = {
        "schema": CS15_COVERAGE_SCHEMA,
        "total_questions": 32,
        "case_groups": {
            group: sorted(case_ids) for group, case_ids in CS15_CASE_GROUP_IDS.items()
        },
    }
    gold_rows = []
    for group, case_ids in contract["case_groups"].items():
        for case_id in case_ids:
            index = int(case_id.rsplit("-", 1)[1])
            work_order_id = f"WO-2027-{index:04}"
            row = {
                "case_id": case_id,
                "work_order_id": work_order_id,
                "expected_early_stop_reason": None,
                "expected_evidence_pks": [],
                "expected_doc_ids": [],
                "required_sql_refs": [{
                    "table": "cross_source_demo.work_orders",
                    "row_pk": work_order_id,
                    "required_columns": ["work_order_id", "opened_at"],
                    "required_values": {
                        "work_order_id": work_order_id,
                        "opened_at": "2027-01-01T09:00:00+08:00",
                    },
                }],
                "required_sql_snippets": [f"{work_order_id} opened_at=2027-01-01"],
                "required_doc_snippets": [],
                "absence_evidence_required": False,
                "required_absence_refs": [],
                "required_absence_checks": [],
            }
            if group in {"adjudicable_within_limit", "adjudicable_at_limit",
                         "adjudicable_over_limit", "metric_swap_control"}:
                if group == "adjudicable_at_limit":
                    elapsed, limit = 8 * 3600, 8
                elif group == "adjudicable_over_limit":
                    elapsed, limit = 9 * 3600, 8
                elif group == "metric_swap_control" and case_id == "C15-32":
                    elapsed, limit = 5 * 3600, 4
                else:
                    elapsed, limit = 1 * 3600, 8
                within = elapsed <= limit * 3600
                row.update({
                    "expected_status": "repair_within_limit" if within else "repair_over_limit",
                    "expected_retrieval_state": "clause_present_expected",
                    "expected_doc_ids": ["SYN-DOC-1"],
                    "required_doc_snippets": ["synthetic service restoration SLA"],
                    "required_metric": {
                        "start_event": "accepted",
                        "end_event": "service_restored",
                        "elapsed_seconds": elapsed,
                        "limit_hours": limit,
                        "within_limit": within,
                    },
                })
            elif group == "undetermined_after_required_retrieval":
                state = "bounded_not_found" if case_id == "C15-28" else "clause_present_expected"
                row.update({
                    "expected_status": "not_determined",
                    "expected_retrieval_state": state,
                })
                if state == "clause_present_expected":
                    row.update({
                        "expected_doc_ids": ["SYN-DOC-1"],
                        "required_doc_snippets": ["synthetic service restoration SLA"],
                    })
                if case_id in {f"C15-{index:02}" for index in range(24, 28)}:
                    row["required_metric"] = {
                        "start_event": "accepted",
                        "end_event": "service_restored",
                        "elapsed_seconds": None,
                        "limit_hours": None,
                        "within_limit": None,
                    }
                if state == "bounded_not_found":
                    row.update({
                        "expected_retrieval_attempts": 2,
                        "expected_retrieval_strategies": ["initial_query", "targeted_refinement"],
                        "expected_retrieval_statuses": ["no_match", "no_match"],
                        "expected_candidate_counts": [0, 0],
                    })
            else:
                reason_by_case = {
                    "C15-12": "mapping_unresolved",
                    "C15-13": "mapping_basis_unapproved",
                    "C15-14": "mapping_basis_unapproved",
                    "C15-15": "validity_window_not_cover",
                    "C15-16": "validity_window_not_cover",
                    "C15-17": "validity_window_not_cover",
                    "C15-18": "contract_status_inactive",
                    "C15-19": "contract_status_inactive",
                    "C15-20": "customer_scope_conflict",
                    "C15-21": "ambiguous_active_contracts",
                    "C15-22": "key_event_missing",
                    "C15-23": "event_time_reversed",
                    "C15-29": "key_event_missing",
                    "C15-30": "mapping_unresolved",
                }
                row.update({
                    "expected_status": "not_determined",
                    "expected_retrieval_state": "retrieval_not_required",
                })
                if case_id in reason_by_case:
                    row["expected_early_stop_reason"] = reason_by_case[case_id]
                if case_id in {"C15-12", "C15-30"}:
                    row.update({
                        "absence_evidence_required": True,
                        "required_absence_refs": [{
                            "table": "cross_source_demo.contract_equipment",
                            "column": "equipment_id",
                            "snippets": [f"0 mapping rows for {case_id}"],
                        }],
                        "required_absence_checks": [{
                            "table": "cross_source_demo.contract_equipment",
                            "filters": {"equipment_id": f"EQ-{case_id}"},
                        }],
                    })
                elif case_id in {"C15-22", "C15-29"}:
                    row.update({
                        "expected_missing_events": ["service_restored"],
                        "absence_evidence_required": True,
                        "required_absence_refs": [{
                            "table": "cross_source_demo.work_order_events",
                            "column": "event_type",
                            "snippets": [f"0 service_restored rows for {case_id}"],
                        }],
                        "required_absence_checks": [{
                            "table": "cross_source_demo.work_order_events",
                            "filters": {
                                "work_order_id": work_order_id,
                                "event_type": "service_restored",
                            },
                        }],
                    })
            gold_rows.append(row)
    return questions, contract, gold_rows


def _cs15_sealed_fixture(root: Path):
    runtime_dir = root / scorer_module.CS15_RUNTIME_PATH
    runtime_dir.mkdir(parents=True)
    run_dir = root / "sealed-cs15-run"
    run_dir.mkdir()
    questions, contract, gold_rows = _cs15_synthetic_fixture()
    questions_path = runtime_dir / "questions.jsonl"
    gold_path = root / "cs15-synthetic-gold.jsonl"
    _write_jsonl(questions_path, questions)
    _write_jsonl(gold_path, gold_rows)

    records = []
    for question in questions:
        for arm in scorer_module.MATCHED_ARMS:
            records.append({
                "case_id": question["case_id"],
                "arm": arm,
                "question": question["question"],
                "work_order_id": question["work_order_id"],
                "elapsed_ms": 10.0,
                "status": "completed",
                "response": {"answer": "synthetic", "refs": [], "trace": []},
            })
    raw_path = run_dir / "raw_responses.jsonl"
    _write_jsonl(raw_path, records)
    schedule_path = run_dir / "schedule.json"
    schedule_path.write_text("{}\n", encoding="utf-8")
    config = {
        "runtime_files": {"questions.jsonl": _sha(questions_path)},
        "runtime_package_id": scorer_module.CS15_PACKAGE_ID,
        "runtime_package_path": "eval/cross_source_heldout/2026Q4_fresh_cs15_v0.2_candidate",
        "protocol_path": scorer_module.CS15_PROTOCOL_PATH,
        "required_question_count": 32,
        "question_count": 32,
        "arm_pair": "matched_reference_fact",
        "arm_names": list(scorer_module.MATCHED_ARMS),
        "arm_design": scorer_module.CS14_ARM_DESIGN,
        "cs15_coverage_contract": contract,
        "gold_sha256": _sha(gold_path),
        "source_manifest": _current_cs14_source_manifest(),
    }
    pre_run_path = run_dir / "pre_run_manifest.json"
    pre_run_path.write_text(json.dumps({"status": "running", "config": config}, sort_keys=True) + "\n",
                            encoding="utf-8")
    manifest = {
        "status": "sealed",
        "postrun_integrity_issues": [],
        "record_count": len(records),
        "expected_record_count": len(records),
        "runtime_error_count": 0,
        "raw_responses_sha256": _sha(raw_path),
        "pre_run_manifest_sha256": _sha(pre_run_path),
        "schedule_sha256": _sha(schedule_path),
    }
    manifest_path = run_dir / "sealed_manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return run_dir, questions_path, gold_path, _sha(manifest_path), _sha(questions_path), _sha(gold_path)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _fixture(root: Path, *, error_pair: tuple[str, str] | None = None) -> tuple[Path, Path, Path, str]:
    run_dir = root / "sealed-run"
    run_dir.mkdir()
    questions = [
        {"case_id": "SYN-01", "question": "合成问题甲"},
        {"case_id": "SYN-02", "question": "合成问题乙"},
    ]
    for index, row in enumerate(questions, 1):
        row["work_order_id"] = f"WO-2026-{index:04}"
    gold = []
    for row in questions:
        question_index = int(row["case_id"][-2:])
        gold.append({
            "case_id": row["case_id"], "expected_status": "not_determined",
            "expected_retrieval_state": "retrieval_not_required",
            "expected_early_stop_reason": "validity_window_not_cover",
            "expected_evidence_pks": [f"HT-{question_index}|DEV-{question_index}"],
            "expected_doc_ids": [],
            "required_sql_snippets": [f"DEV-{question_index} 映射有效期未覆盖"],
            "required_doc_snippets": [],
            "absence_evidence_required": False,
            "required_absence_refs": [], "required_absence_checks": [],
        })
    records = []
    for case in questions:
        for arm in ARMS:
            pair = (case["case_id"], arm)
            common = {"case_id": case["case_id"], "arm": arm, "question": case["question"],
                      "work_order_id": case["work_order_id"], "elapsed_ms": 10.0}
            question_index = int(case["case_id"][-2:])
            evidence = {
                "kind": "sql", "table": "cross_source_demo.contract_equipment",
                "column": "equipment_id/contract_id/valid_from/valid_to",
                "row_pk": f"HT-{question_index}|DEV-{question_index}",
                "snippet": f"DEV-{question_index} 映射有效期未覆盖工单日",
            }
            if pair == error_pair:
                records.append({**common, "status": "error", "error_type": "SyntheticFailure"})
            else:
                status = "not_determined" if pair != ("SYN-02", "old_workflow") else "satisfied"
                records.append({**common, "status": "completed", "response": {
                    "answer": "合成回答", "refs": [evidence],
                    "trace": [{"tool": "cross_source_alignment", "output": {
                        "status": status, "stop_reason_code": "validity_window_not_cover",
                    }, "hit": [evidence]}],
                }})
    raw_path = run_dir / "raw_responses.jsonl"
    _write_jsonl(raw_path, records)
    questions_path, gold_path = root / "questions.jsonl", root / "gold.jsonl"
    _write_jsonl(questions_path, questions)
    _write_jsonl(gold_path, gold)
    schedule_path = run_dir / "schedule.json"
    schedule_path.write_text("{}\n", encoding="utf-8")
    pre_run_path = run_dir / "pre_run_manifest.json"
    pre_run_path.write_text(json.dumps({
        "status": "running",
        "config": {"runtime_files": {"questions.jsonl": _sha(questions_path)}},
    }, sort_keys=True) + "\n", encoding="utf-8")
    manifest = {
        "status": "sealed_with_runtime_errors" if error_pair else "sealed",
        "postrun_integrity_issues": [],
        "record_count": len(records), "expected_record_count": len(records),
        "runtime_error_count": 1 if error_pair else 0,
        "raw_responses_sha256": _sha(raw_path),
        "pre_run_manifest_sha256": _sha(pre_run_path),
        "schedule_sha256": _sha(schedule_path),
    }
    manifest_path = run_dir / "sealed_manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return (run_dir, questions_path, gold_path, _sha(manifest_path),
            _sha(questions_path), _sha(gold_path))


def _cs14_fixture(root: Path, *, v03: bool = False) -> tuple[Path, Path, Path, str, str, str]:
    """Build a fully synthetic CS14-shaped bundle; it contains no candidate data."""
    runtime_package_path = (
        scorer_module.CS14_V03_RUNTIME_PATH.rsplit("/", 1)[0]
        if v03 else scorer_module.CS14_RUNTIME_PATH.rsplit("/", 1)[0]
    )
    package = root / runtime_package_path
    runtime_dir = package / "runtime"
    runtime_dir.mkdir(parents=True)
    run_dir = root / "sealed-cs14-run"
    run_dir.mkdir()

    questions = [
        {
            "case_id": f"CS14-SYN-{index:02}",
            "question": f"合成 CS14 问题 {index}",
            "work_order_id": f"WO-2026-{index:04}",
        }
        for index in range(1, 33)
    ]
    if v03:
        groups = {
            "adjudicable_sla_under": [row["case_id"] for row in questions[0:13]],
            "adjudicable_sla_equal": [row["case_id"] for row in questions[13:15]],
            "adjudicable_sla_over": [row["case_id"] for row in questions[15:16]],
            "mapping_blocked_before_retrieval": [row["case_id"] for row in questions[16:23]],
            "event_blocked_before_retrieval": [row["case_id"] for row in questions[23:29]],
            "undetermined_after_required_retrieval": [row["case_id"] for row in questions[29:30]],
            "insufficient_document_evidence": [row["case_id"] for row in questions[30:32]],
        }
        schema = scorer_module.CS14_V03_COVERAGE_SCHEMA
    else:
        groups = {
            "adjudicable_sla_under": [row["case_id"] for row in questions[0:4]],
            "adjudicable_sla_equal": [row["case_id"] for row in questions[4:8]],
            "adjudicable_sla_over": [row["case_id"] for row in questions[8:12]],
            "boundary": [row["case_id"] for row in questions[12:32]],
        }
        schema = scorer_module.CS14_COVERAGE_SCHEMA
    contract = {
        "schema": schema,
        "total_questions": 32,
        "case_groups": groups,
        "confidence_level": 0.95,
        "mcnemar_alpha": 0.05,
    }
    questions_path = runtime_dir / "questions.jsonl"
    _write_jsonl(questions_path, questions)
    gold_rows: list[dict] = []
    for group, case_ids in groups.items():
        for case_id in case_ids:
            row: dict = {
                "case_id": case_id,
                "work_order_id": f"WO-2026-{int(case_id.rsplit('-', 1)[1]):04}",
                "expected_status": "not_determined",
                "expected_retrieval_state": "retrieval_not_required",
                "expected_early_stop_reason": "validity_window_not_cover",
                "expected_evidence_pks": [],
                "expected_doc_ids": [],
                "required_sql_snippets": [f"{case_id} opened_at=2026-10-01"],
                "required_doc_snippets": [],
                "absence_evidence_required": False,
                "required_absence_refs": [],
                "required_absence_checks": [],
            }
            adjudicable_group = group in {
                "adjudicable_sla_under", "adjudicable_sla_equal", "adjudicable_sla_over",
            }
            if adjudicable_group:
                elapsed_by_group = {
                    "adjudicable_sla_under": 3 * 3600,
                    "adjudicable_sla_equal": 4 * 3600,
                    "adjudicable_sla_over": 5 * 3600,
                }
                within_limit = group != "adjudicable_sla_over"
                row.update({
                    "expected_status": "repair_within_limit" if within_limit else "repair_over_limit",
                    "expected_retrieval_state": "clause_present_expected",
                    "expected_early_stop_reason": None,
                    "expected_doc_ids": ["SYN-DOC-1"],
                    "required_doc_snippets": ["合成修复时限 4 小时"],
                    "required_metric": {
                        "start_event": "accepted",
                        "end_event": "service_restored",
                        "elapsed_seconds": elapsed_by_group[group],
                        "limit_hours": 4,
                        "within_limit": within_limit,
                    },
                })
            elif v03:
                if group in {"mapping_blocked_before_retrieval", "event_blocked_before_retrieval"}:
                    row["required_metric"] = None
                    if group == "event_blocked_before_retrieval":
                        row.update({
                            "expected_early_stop_reason": "key_event_missing",
                            "expected_missing_events": ["accepted"],
                            "absence_evidence_required": True,
                            "required_absence_refs": [{
                                "table": "cross_source_demo.work_order_events",
                                "column": "event_type",
                                "snippets": ["0 accepted rows for synthetic work order"],
                            }],
                            "required_absence_checks": [{
                                "table": "cross_source_demo.work_order_events",
                                "filters": {
                                    "work_order_id": row["work_order_id"],
                                    "event_type": "accepted",
                                },
                            }],
                        })
                elif group == "undetermined_after_required_retrieval":
                    row.update({
                        "expected_retrieval_state": "bounded_not_found",
                        "expected_early_stop_reason": None,
                        "expected_retrieval_attempts": 2,
                        "expected_retrieval_strategies": ["exact", "semantic"],
                        "expected_retrieval_statuses": ["bounded_not_found", "bounded_not_found"],
                        "expected_candidate_counts": [1, 0],
                    })
                else:
                    row.update({
                        "expected_retrieval_state": "clause_present_expected",
                        "expected_early_stop_reason": None,
                        "expected_doc_ids": ["SYN-DOC-1"],
                        "required_doc_snippets": ["合成的不完整合同证据"],
                        "required_metric": {
                            "start_event": "accepted", "end_event": "service_restored",
                            "elapsed_seconds": 3600, "limit_hours": None,
                            "within_limit": None,
                        },
                    })
            gold_rows.append(row)
    gold_path = root / "cs14-gold.jsonl"
    _write_jsonl(gold_path, gold_rows)

    records: list[dict] = []
    for index, question in enumerate(questions, 1):
        for arm in scorer_module.MATCHED_ARMS:
            # Outcomes are synthetic and patched into compare() by the test.
            if index == 1:
                values = (True, True)
            elif index == 2:
                values = (arm == scorer_module.MATCHED_ARMS[1],) * 2
            elif index == 3:
                values = (arm == scorer_module.MATCHED_ARMS[0],) * 2
            else:
                values = (False, False)
            records.append({
                "case_id": question["case_id"],
                "arm": arm,
                "question": question["question"],
                "work_order_id": question["work_order_id"],
                "elapsed_ms": 12.0,
                "status": "completed",
                "response": {
                    "answer": "合成响应",
                    "synthetic_metrics": {
                        "correctness_pass": values[0],
                        "citation_completeness_pass": values[1],
                        "trace_compliance_pass": values[0] and values[1],
                    },
                },
            })
    raw_path = run_dir / "raw_responses.jsonl"
    _write_jsonl(raw_path, records)
    schedule_path = run_dir / "schedule.json"
    schedule_path.write_text("{}\n", encoding="utf-8")
    pre_run_path = run_dir / "pre_run_manifest.json"
    config = {
        "runtime_files": {"questions.jsonl": _sha(questions_path)},
        "runtime_package_id": scorer_module.CS14_V03_PACKAGE_ID if v03 else scorer_module.CS14_PACKAGE_ID,
        "runtime_package_path": runtime_package_path,
        "protocol_path": scorer_module.CS14_V03_PROTOCOL_PATH if v03 else scorer_module.CS14_PROTOCOL_PATH,
        "required_question_count": 32,
        "question_count": 32,
        "arm_pair": "matched_reference_fact",
        "arm_names": list(scorer_module.MATCHED_ARMS),
        "arm_design": scorer_module.CS14_ARM_DESIGN,
        "cs14_coverage_contract": contract,
        "gold_sha256": _sha(gold_path),
        "source_manifest": _current_cs14_source_manifest(),
    }
    pre_run_path.write_text(json.dumps({"status": "running", "config": config}, sort_keys=True) + "\n",
                            encoding="utf-8")
    manifest = {
        "status": "sealed",
        "postrun_integrity_issues": [],
        "record_count": len(records),
        "expected_record_count": len(records),
        "runtime_error_count": 0,
        "raw_responses_sha256": _sha(raw_path),
        "pre_run_manifest_sha256": _sha(pre_run_path),
        "schedule_sha256": _sha(schedule_path),
    }
    manifest_path = run_dir / "sealed_manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return (
        run_dir,
        questions_path,
        gold_path,
        _sha(manifest_path),
        _sha(questions_path),
        _sha(gold_path),
    )


def test_scores_matched_pairs_and_keeps_sealed_inputs_unchanged(tmp_path):
    run_dir, questions, gold, anchor, questions_anchor, gold_anchor = _fixture(tmp_path)
    before = {p.name: _sha(p) for p in run_dir.iterdir()}
    result = score_run(run_dir, questions, gold, anchor, questions_anchor, gold_anchor)
    assert result["case_count"] == 2
    assert result["schema_version"] == "1.3"
    assert result["score_status"] == "valid"
    assert result["arms"]["old_workflow"]["correctness_pass"] == {"passed": 1, "total": 2, "rate": 0.5}
    assert result["arms"]["new_fact_verification"]["correctness_pass"]["passed"] == 2
    assert result["paired_transitions"]["correctness_pass"] == {
        "old_pass__new_pass": 1, "old_pass__new_fail": 0,
        "old_fail__new_pass": 1, "old_fail__new_fail": 0,
    }
    assert result["primary_outcome"] is None
    assert result["automatic_diagnostic_outcome"] == "strict_pass"
    assert result["arms"]["old_workflow"]["strict_pass"] == {
        "passed": 1, "total": 2, "rate": 0.5,
    }
    assert result["paired_transitions"]["strict_pass"] == {
        "old_pass__new_pass": 1, "old_pass__new_fail": 0,
        "old_fail__new_pass": 1, "old_fail__new_fail": 0,
    }
    assert result["arms"]["old_workflow"]["elapsed_ms"]["median"] == 10.0
    assert before == {p.name: _sha(p) for p in run_dir.iterdir()}


def test_strict_pass_requires_both_correctness_and_citation_completeness():
    assert _strict_pass({
        "correctness_pass": True, "citation_completeness_pass": True,
    }) is True
    assert _strict_pass({
        "correctness_pass": False, "citation_completeness_pass": True,
    }) is False
    assert _strict_pass({
        "correctness_pass": True, "citation_completeness_pass": False,
    }) is False
    assert _strict_pass({
        "correctness_pass": False, "citation_completeness_pass": False,
    }) is False


def test_machine_score_marks_natural_language_status_as_unavailable():
    scored = scorer_module._score_one(
        {"case_id": "SYN-01", "question": "问题"},
        {"expected_status": "repair_within_limit"},
        {"arm": "old_workflow", "status": "completed", "elapsed_ms": 10,
         "response": {"answer": "该工单满足合同限时。", "refs": [], "trace": []}},
    )
    assert scored["status_comparison_available"] is False
    assert scored["strict_pass"] is False
    # Human semantic correctness is deliberately not produced by this automatic scorer.
    assert "human_correctness" not in scored


def test_cs14_pre_run_contract_selects_v02_and_rejects_v01_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer_module, "PROJECT_ROOT", tmp_path.resolve())
    questions = [
        {"case_id": f"CS14-SYN-{index:02}", "question": "Q", "work_order_id": f"WO-2026-{index:04}"}
        for index in range(1, 33)
    ]
    groups = {
        "adjudicable_sla_under": [row["case_id"] for row in questions[0:4]],
        "adjudicable_sla_equal": [row["case_id"] for row in questions[4:8]],
        "adjudicable_sla_over": [row["case_id"] for row in questions[8:12]],
        "boundary": [row["case_id"] for row in questions[12:32]],
    }
    contract = {
        "schema": scorer_module.CS14_COVERAGE_SCHEMA,
        "total_questions": 32,
        "case_groups": groups,
        "confidence_level": 0.95,
        "mcnemar_alpha": 0.05,
    }
    config = {
        "runtime_package_id": scorer_module.CS14_PACKAGE_ID,
        "runtime_package_path": scorer_module.CS14_RUNTIME_PATH.rsplit("/", 1)[0],
        "protocol_path": scorer_module.CS14_PROTOCOL_PATH,
        "required_question_count": 32,
        "question_count": 32,
        "arm_pair": "matched_reference_fact",
        "arm_names": list(scorer_module.MATCHED_ARMS),
        "arm_design": scorer_module.CS14_ARM_DESIGN,
        "cs14_coverage_contract": contract,
        "gold_sha256": "a" * 64,
    }
    v02_questions_path = tmp_path / scorer_module.CS14_RUNTIME_PATH / "questions.jsonl"
    assert scorer_module.CS14_PACKAGE_ID == "2026Q4_fresh_cs14_v0.2_candidate"
    assert scorer_module.CS14_PROTOCOL_PATH.endswith("2026Q4_fresh_cs14_v0.2_candidate/protocol.md")
    assert _validate_cs14_pre_run(config, questions, v02_questions_path)

    legacy_config = {
        **config,
        "runtime_package_id": "2026Q4_fresh_cs14_v0.1_candidate",
        "runtime_package_path": "eval/cross_source_heldout/2026Q4_fresh_cs14_v0.1_candidate",
        "protocol_path": "eval/cross_source_heldout/2026Q4_fresh_cs14_v0.1_candidate/protocol.md",
    }
    with pytest.raises(ValueError, match="32 题/匹配臂设计契约"):
        _validate_cs14_pre_run(legacy_config, questions, v02_questions_path)


def test_cs15_pre_run_requires_exact_v02_package_and_fixed_seven_groups(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer_module, "PROJECT_ROOT", tmp_path.resolve())
    questions, contract, _gold = _cs15_synthetic_fixture()
    questions_path = tmp_path / scorer_module.CS15_RUNTIME_PATH / "questions.jsonl"
    config = {
        "runtime_package_id": scorer_module.CS15_PACKAGE_ID,
        "runtime_package_path": "eval/cross_source_heldout/2026Q4_fresh_cs15_v0.2_candidate",
        "protocol_path": scorer_module.CS15_PROTOCOL_PATH,
        "required_question_count": 32,
        "question_count": 32,
        "arm_pair": "matched_reference_fact",
        "arm_names": list(scorer_module.MATCHED_ARMS),
        "arm_design": scorer_module.CS14_ARM_DESIGN,
        "cs15_coverage_contract": contract,
        "gold_sha256": "f" * 64,
    }
    assert _validate_cs15_pre_run(config, questions, questions_path)
    assert CS15_ADDITIONAL_EARLY_STOP_REASONS == {
        "mapping_unresolved", "contract_status_inactive",
    }

    invalid = json.loads(json.dumps(contract))
    invalid["case_groups"]["metric_swap_control"][0] = "C15-30"
    with pytest.raises(ValueError, match="固定 ID"):
        _cs15_case_groups(invalid, {row["case_id"] for row in questions})


def test_cs15_v05_pre_run_binds_exact_candidate_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer_module, "PROJECT_ROOT", tmp_path.resolve())
    questions, contract, _gold = _cs15_synthetic_fixture()
    questions_path = tmp_path / scorer_module.CS15_V05_RUNTIME_PATH / "questions.jsonl"
    config = {
        "runtime_package_id": scorer_module.CS15_V05_PACKAGE_ID,
        "runtime_package_path": "eval/cross_source_heldout/2026Q4_fresh_cs15_v0.5_candidate",
        "protocol_path": scorer_module.CS15_V05_PROTOCOL_PATH,
        "required_question_count": 32,
        "question_count": 32,
        "arm_pair": "matched_reference_fact",
        "arm_names": list(scorer_module.MATCHED_ARMS),
        "arm_design": scorer_module.CS14_ARM_DESIGN,
        "cs15_coverage_contract": contract,
        "gold_sha256": "a" * 64,
    }
    assert _validate_cs15_pre_run(config, questions, questions_path)

    wrong_version = json.loads(json.dumps(config))
    wrong_version["runtime_package_id"] = scorer_module.CS15_PACKAGE_ID
    with pytest.raises(ValueError, match="32 题/匹配臂设计契约"):
        _validate_cs15_pre_run(wrong_version, questions, questions_path)


def test_cs15_v06_pre_run_binds_exact_candidate_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer_module, "PROJECT_ROOT", tmp_path.resolve())
    questions, contract, _gold = _cs15_synthetic_fixture()
    questions_path = tmp_path / scorer_module.CS15_V06_RUNTIME_PATH / "questions.jsonl"
    config = {
        "runtime_package_id": scorer_module.CS15_V06_PACKAGE_ID,
        "runtime_package_path": "eval/cross_source_heldout/2026Q4_fresh_cs15_v0.6_candidate",
        "protocol_path": scorer_module.CS15_V06_PROTOCOL_PATH,
        "required_question_count": 32,
        "question_count": 32,
        "arm_pair": "matched_reference_fact",
        "arm_names": list(scorer_module.MATCHED_ARMS),
        "arm_design": scorer_module.CS14_ARM_DESIGN,
        "cs15_coverage_contract": contract,
        "gold_sha256": "b" * 64,
    }
    assert _validate_cs15_pre_run(config, questions, questions_path)


def test_cs15_gold_groups_enforce_retrieval_and_metric_semantics():
    questions, contract, gold_rows = _cs15_synthetic_fixture()
    _validate_cs15_gold_groups(gold_rows, questions, contract)

    wrong_retrieval = json.loads(json.dumps(gold_rows))
    next(row for row in wrong_retrieval if row["case_id"] == "C15-28")[
        "expected_retrieval_state"
    ] = "clause_present_expected"
    with pytest.raises(ValueError, match="C15-28.*bounded_not_found"):
        _validate_cs15_gold_groups(wrong_retrieval, questions, contract)

    wrong_metric_status = json.loads(json.dumps(gold_rows))
    next(row for row in wrong_metric_status if row["case_id"] == "C15-11")[
        "expected_status"
    ] = "repair_within_limit"
    with pytest.raises(ValueError, match="分类与 SLA 指标/业务状态不一致"):
        _validate_cs15_gold_groups(wrong_metric_status, questions, contract)

    wrong_stop_reason = json.loads(json.dumps(gold_rows))
    next(row for row in wrong_stop_reason if row["case_id"] == "C15-18")[
        "expected_early_stop_reason"
    ] = "mapping_unresolved"
    with pytest.raises(ValueError, match="阻断原因与覆盖维度不一致"):
        _validate_cs15_gold_groups(wrong_stop_reason, questions, contract)

    missing_absence_requirement = json.loads(json.dumps(gold_rows))
    next(row for row in missing_absence_requirement if row["case_id"] == "C15-29")[
        "absence_evidence_required"
    ] = False
    with pytest.raises(ValueError, match="absence evidence"):
        _validate_cs15_gold_groups(missing_absence_requirement, questions, contract)

    wrong_nullable = json.loads(json.dumps(gold_rows))
    next(row for row in wrong_nullable if row["case_id"] == "C15-24")[
        "required_metric"
    ] = {"elapsed_seconds": 0, "limit_hours": 8}
    with pytest.raises(ValueError, match="受限空指标"):
        _validate_cs15_gold_groups(wrong_nullable, questions, contract)


def test_cs15_score_run_accepts_only_synthetic_seven_group_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer_module, "PROJECT_ROOT", tmp_path.resolve())
    monkeypatch.setattr(scorer_module, "compare", lambda *_args: {
        "correctness_pass": True,
        "citation_completeness_pass": True,
        "trace_compliance_pass": True,
        "status_comparison_available": True,
    })
    run_dir, questions, gold, seal_sha, questions_sha, gold_sha = _cs15_sealed_fixture(tmp_path)

    result = score_run(run_dir, questions, gold, seal_sha, questions_sha, gold_sha)

    assert result["score_status"] == "valid"
    assert result["case_count"] == 32
    coverage = result["cs15_coverage_validation"]
    assert coverage["gold_crosscheck"] == "pass"
    assert coverage["nullable_metric_case_ids"] == ["C15-24", "C15-25", "C15-26", "C15-27"]
    assert coverage["bounded_not_found_case_ids"] == ["C15-28"]


def test_cs14_emits_automatic_diagnostics_without_claiming_primary_inference(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer_module, "PROJECT_ROOT", tmp_path.resolve())

    def synthetic_compare(_case, _gold, response, _elapsed_ms):
        return response["synthetic_metrics"]

    monkeypatch.setattr(scorer_module, "compare", synthetic_compare)
    run_dir, questions, gold, anchor, questions_anchor, gold_anchor = _cs14_fixture(tmp_path)
    result = score_run(run_dir, questions, gold, anchor, questions_anchor, gold_anchor)

    assert result["schema_version"] == "1.3"
    assert result["score_status"] == "valid"
    assert result["case_count"] == 32
    assert result["primary_outcome"] is None
    assert result["automatic_diagnostic_outcome"] == "strict_pass"
    assert "primary_inference" not in result
    assert result["cs14_coverage_validation"]["gold_crosscheck"] == "pass"
    assert "不得含 required_metric" in result[
        "cs14_coverage_validation"]["boundary_metric_semantics"]


def test_cs14_v03_scores_v2_groups_with_narrow_nullable_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer_module, "PROJECT_ROOT", tmp_path.resolve())
    monkeypatch.setattr(scorer_module, "compare", lambda _case, _gold, response, _elapsed: {
        **response["synthetic_metrics"],
    })
    run_dir, questions, gold, anchor, questions_anchor, gold_anchor = _cs14_fixture(
        tmp_path, v03=True
    )

    result = score_run(run_dir, questions, gold, anchor, questions_anchor, gold_anchor)

    groups = result["cs14_coverage_validation"]["case_groups"]
    assert result["cs14_coverage_validation"]["schema"] == "cs14_coverage_contract_v2"
    assert {name: len(case_ids) for name, case_ids in groups.items()} == {
        "adjudicable_sla_under": 13,
        "adjudicable_sla_equal": 2,
        "adjudicable_sla_over": 1,
        "mapping_blocked_before_retrieval": 7,
        "event_blocked_before_retrieval": 6,
        "undetermined_after_required_retrieval": 1,
        "insufficient_document_evidence": 2,
    }
    assert result["score_status"] == "valid"
    assert "v0.3" in result["cs14_coverage_validation"]["indeterminate_metric_semantics"]


def test_cs14_v03_rejects_nullable_metrics_outside_indeterminate_groups(tmp_path):
    run_dir, questions_path, gold_path, _anchor, _questions_anchor, _gold_anchor = _cs14_fixture(
        tmp_path, v03=True
    )
    questions = scorer_module.read_jsonl(questions_path)
    gold_rows = scorer_module.read_jsonl(gold_path)
    pre_run = json.loads((run_dir / "pre_run_manifest.json").read_text(encoding="utf-8"))
    contract = pre_run["config"]["cs14_coverage_contract"]
    groups = contract["case_groups"]

    under_row = next(row for row in gold_rows if row["case_id"] == groups["adjudicable_sla_under"][0])
    under_row["required_metric"] = None
    with pytest.raises(ValueError, match="缺少完整可判 SLA 指标"):
        _validate_cs14_gold_groups(gold_rows, questions, contract)

    under_row["required_metric"] = {
        "start_event": "accepted", "end_event": "service_restored",
        "elapsed_seconds": 3 * 3600, "limit_hours": 4, "within_limit": True,
    }
    boundary = next(
        row for row in gold_rows
        if row["case_id"] == groups["insufficient_document_evidence"][0]
    )
    boundary["required_metric"] = {"elapsed_seconds": 3600}
    with pytest.raises(ValueError, match="只能缺省、为 null"):
        _validate_cs14_gold_groups(gold_rows, questions, contract)


def test_cs14_automatic_diagnostic_reports_counts_without_inference(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer_module, "PROJECT_ROOT", tmp_path.resolve())
    monkeypatch.setattr(scorer_module, "compare", lambda *_args: {
        "correctness_pass": False,
        "citation_completeness_pass": False,
        "trace_compliance_pass": False,
    })
    run_dir, questions, gold, anchor, questions_anchor, gold_anchor = _cs14_fixture(tmp_path)
    result = score_run(run_dir, questions, gold, anchor, questions_anchor, gold_anchor)
    assert "primary_inference" not in result
    assert result["paired_transitions"]["strict_pass"] == {
        "old_pass__new_pass": 0,
        "old_pass__new_fail": 0,
        "old_fail__new_pass": 0,
        "old_fail__new_fail": 32,
    }


def test_cs14_gold_groups_are_cross_checked_against_protocol_ids():
    questions = [
        {"case_id": f"CS14-SYN-{index:02}", "question": "Q", "work_order_id": f"WO-2026-{index:04}"}
        for index in range(1, 33)
    ]
    groups = {
        "adjudicable_sla_under": [row["case_id"] for row in questions[0:4]],
        "adjudicable_sla_equal": [row["case_id"] for row in questions[4:8]],
        "adjudicable_sla_over": [row["case_id"] for row in questions[8:12]],
        "boundary": [row["case_id"] for row in questions[12:32]],
    }
    contract = {
        "schema": scorer_module.CS14_COVERAGE_SCHEMA,
        "total_questions": 32,
        "case_groups": groups,
        "confidence_level": 0.95,
        "mcnemar_alpha": 0.05,
    }
    gold_rows = []
    for group, case_ids in groups.items():
        for case_id in case_ids:
            row = {
                "case_id": case_id,
                "expected_status": "not_determined",
                "expected_retrieval_state": "retrieval_not_required",
                "expected_early_stop_reason": "validity_window_not_cover",
            }
            if group != "boundary":
                over = group == "adjudicable_sla_over"
                elapsed = {
                    "adjudicable_sla_under": 3,
                    "adjudicable_sla_equal": 4,
                    "adjudicable_sla_over": 5,
                }[group] * 3600
                row.update({
                    "expected_status": "repair_over_limit" if over else "repair_within_limit",
                    "required_metric": {
                        "start_event": "accepted", "end_event": "service_restored",
                        "elapsed_seconds": elapsed, "limit_hours": 4, "within_limit": not over,
                    },
                })
            gold_rows.append(row)
    _validate_cs14_gold_groups(gold_rows, questions, contract)
    boundary_row = next(row for row in gold_rows if row["case_id"] == groups["boundary"][0])
    assert boundary_row["expected_status"] == "not_determined"
    assert boundary_row["expected_retrieval_state"] == "retrieval_not_required"
    assert "required_metric" not in boundary_row

    boundary_row["expected_status"] = "repair_within_limit"
    with pytest.raises(ValueError, match="boundary 分类"):
        _validate_cs14_gold_groups(gold_rows, questions, contract)


def _simple_cs14_gold_group_fixture():
    questions = [
        {"case_id": f"CS14-SYN-{index:02}", "question": "Q", "work_order_id": f"WO-{index:04}"}
        for index in range(1, 33)
    ]
    groups = {
        "adjudicable_sla_under": [row["case_id"] for row in questions[0:4]],
        "adjudicable_sla_equal": [row["case_id"] for row in questions[4:8]],
        "adjudicable_sla_over": [row["case_id"] for row in questions[8:12]],
        "boundary": [row["case_id"] for row in questions[12:32]],
    }
    contract = {
        "schema": scorer_module.CS14_COVERAGE_SCHEMA,
        "total_questions": 32,
        "case_groups": groups,
        "confidence_level": 0.95,
        "mcnemar_alpha": 0.05,
    }
    gold_rows = []
    for group, case_ids in groups.items():
        for case_id in case_ids:
            row = {
                "case_id": case_id,
                "expected_status": "not_determined",
                "expected_retrieval_state": "retrieval_not_required",
            }
            if group != "boundary":
                is_over = group == "adjudicable_sla_over"
                elapsed = {
                    "adjudicable_sla_under": 3,
                    "adjudicable_sla_equal": 4,
                    "adjudicable_sla_over": 5,
                }[group] * 3600
                row.update({
                    "expected_status": "repair_over_limit" if is_over else "repair_within_limit",
                    "required_metric": {
                        "start_event": "accepted", "end_event": "service_restored",
                        "elapsed_seconds": elapsed, "limit_hours": 4, "within_limit": not is_over,
                    },
                })
            gold_rows.append(row)
    return questions, contract, gold_rows, groups


@pytest.mark.parametrize("retrieval_state", [None, "clause_present_expected", "bounded_not_found"])
def test_cs14_boundary_requires_retrieval_not_required(retrieval_state):
    questions, contract, gold_rows, groups = _simple_cs14_gold_group_fixture()
    boundary_row = next(row for row in gold_rows if row["case_id"] == groups["boundary"][0])
    if retrieval_state is None:
        boundary_row.pop("expected_retrieval_state")
    else:
        boundary_row["expected_retrieval_state"] = retrieval_state
    with pytest.raises(ValueError, match="必须 retrieval_not_required"):
        _validate_cs14_gold_groups(gold_rows, questions, contract)


@pytest.mark.parametrize("metric", [
    {"start_event": "accepted", "end_event": "service_restored",
     "elapsed_seconds": 3600, "limit_hours": 4, "within_limit": True},
    {"elapsed_seconds": 3600},
    None,
])
def test_cs14_boundary_rejects_present_full_partial_or_null_metric(metric):
    questions, contract, gold_rows, groups = _simple_cs14_gold_group_fixture()
    boundary_row = next(row for row in gold_rows if row["case_id"] == groups["boundary"][0])
    boundary_row["required_metric"] = metric
    with pytest.raises(ValueError, match="不得包含 required_metric"):
        _validate_cs14_gold_groups(gold_rows, questions, contract)


def test_cs14_rejects_gold_substitution_against_pre_run_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer_module, "PROJECT_ROOT", tmp_path.resolve())
    run_dir, questions, gold, _anchor, questions_anchor, original_gold_anchor = _cs14_fixture(tmp_path)
    gold.write_bytes(gold.read_bytes() + b"\n")
    substituted_gold_anchor = _sha(gold)

    with pytest.raises(ValueError, match="Gold 文件与外部 SHA-256 锚点"):
        score_run(
            run_dir, questions, gold, _anchor, questions_anchor, original_gold_anchor
        )

    original_read_jsonl = scorer_module.read_jsonl

    def reject_gold_read(path):
        if Path(path).resolve() == gold.resolve():
            raise AssertionError("CS14 Gold digest mismatch must fail before reading Gold")
        return original_read_jsonl(path)

    monkeypatch.setattr(scorer_module, "read_jsonl", reject_gold_read)
    with pytest.raises(ValueError, match="与运行前外部预注册摘要不一致"):
        score_run(
            run_dir, questions, gold, _anchor, questions_anchor, substituted_gold_anchor
        )


def test_cs14_post_run_only_gold_registration_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer_module, "PROJECT_ROOT", tmp_path.resolve())
    run_dir, questions, gold, _anchor, questions_anchor, gold_anchor = _cs14_fixture(tmp_path)
    pre_run_path = run_dir / "pre_run_manifest.json"
    pre_run = json.loads(pre_run_path.read_text(encoding="utf-8"))
    pre_run["config"].pop("gold_sha256")
    pre_run_path.write_text(json.dumps(pre_run, sort_keys=True) + "\n", encoding="utf-8")
    manifest_path = run_dir / "sealed_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pre_run_manifest_sha256"] = _sha(pre_run_path)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    original_read_jsonl = scorer_module.read_jsonl

    def reject_gold_read(path):
        if Path(path).resolve() == gold.resolve():
            raise AssertionError("post-run-only registration must fail before reading Gold")
        return original_read_jsonl(path)

    monkeypatch.setattr(scorer_module, "read_jsonl", reject_gold_read)
    with pytest.raises(ValueError, match="运行前封存缺少有效的外部预注册 Gold SHA-256"):
        score_run(
            run_dir, questions, gold, _sha(manifest_path), questions_anchor, gold_anchor
        )


@pytest.mark.parametrize("source_path", CS14_SOURCE_HASH_PATHS)
def test_cs14_source_hash_mismatch_aborts_before_scoring(tmp_path, monkeypatch, source_path):
    monkeypatch.setattr(scorer_module, "PROJECT_ROOT", tmp_path.resolve())
    run_dir, questions, gold, _anchor, questions_anchor, gold_anchor = _cs14_fixture(tmp_path)
    pre_run_path = run_dir / "pre_run_manifest.json"
    pre_run = json.loads(pre_run_path.read_text(encoding="utf-8"))
    pre_run["config"]["source_manifest"][source_path] = "0" * 64
    pre_run_path.write_text(json.dumps(pre_run, sort_keys=True) + "\n", encoding="utf-8")
    manifest_path = run_dir / "sealed_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pre_run_manifest_sha256"] = _sha(pre_run_path)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    original_read_jsonl = scorer_module.read_jsonl

    def reject_gold_read(path):
        if Path(path).resolve() == gold.resolve():
            raise AssertionError("Gold must not be read after source hash mismatch")
        return original_read_jsonl(path)

    monkeypatch.setattr(scorer_module, "read_jsonl", reject_gold_read)

    with pytest.raises(ValueError, match="当前源码与运行前封存哈希不一致"):
        score_run(run_dir, questions, gold, _sha(manifest_path), questions_anchor, gold_anchor)


def test_comparator_exception_fails_closed_without_writing_score_file(tmp_path, monkeypatch):
    run_dir, questions, gold, anchor, questions_anchor, gold_anchor = _fixture(tmp_path)

    def broken_compare(*_args, **_kwargs):
        raise RuntimeError("synthetic comparator defect")

    monkeypatch.setattr(scorer_module, "compare", broken_compare)
    output = tmp_path / "external-score.json"
    monkeypatch.setattr(sys, "argv", [
        "score_architecture_comparison.py",
        "--run-dir", str(run_dir),
        "--questions", str(questions),
        "--gold", str(gold),
        "--sealed-manifest-sha256", anchor,
        "--questions-sha256", questions_anchor,
        "--gold-sha256", gold_anchor,
        "--output", str(output),
    ])
    with pytest.raises(RuntimeError, match="synthetic comparator defect"):
        scorer_module.main()
    assert not output.exists()


def test_validate_pairs_accepts_matched_reference_ablation_arm():
    questions = [{"case_id": "SYN-M", "question": "问题", "work_order_id": "WO-2026-0001"}]
    gold = [{"case_id": "SYN-M"}]
    records = [
        {"case_id": "SYN-M", "arm": "matched_reference_ablation",
         "question": "问题", "work_order_id": "WO-2026-0001"},
        {"case_id": "SYN-M", "arm": "new_fact_verification",
         "question": "问题", "work_order_id": "WO-2026-0001"},
    ]
    _cases, _gold, pairs = _validate_pairs(questions, gold, records)
    assert set(arm for _case, arm in pairs) == {
        "matched_reference_ablation", "new_fact_verification"
    }


def test_runtime_error_is_retained_and_fails_all_three_metrics(tmp_path):
    run_dir, questions, gold, anchor, questions_anchor, gold_anchor = _fixture(
        tmp_path, error_pair=("SYN-01", "old_workflow")
    )
    result = score_run(run_dir, questions, gold, anchor, questions_anchor, gold_anchor)
    failed = next(row for row in result["items"] if row["case_id"] == "SYN-01" and row["arm"] == "old_workflow")
    assert failed["run_status"] == "error"
    assert failed["error_type"] == "SyntheticFailure"
    assert not any(failed[name] for name in (
        "correctness_pass", "citation_completeness_pass", "trace_compliance_pass",
    ))
    assert result["arms"]["old_workflow"]["runtime_errors"] == 1


def test_external_seal_anchor_is_mandatory_and_checked(tmp_path):
    run_dir, questions, gold, anchor, questions_anchor, gold_anchor = _fixture(tmp_path)
    with pytest.raises(ValueError, match="锚点"):
        score_run(run_dir, questions, gold, "0" * 64, questions_anchor, gold_anchor)
    with pytest.raises(ValueError, match="64 位"):
        score_run(run_dir, questions, gold, "not-a-digest", questions_anchor, gold_anchor)


def test_raw_response_tampering_fails_seal_verification(tmp_path):
    run_dir, questions, gold, anchor, questions_anchor, gold_anchor = _fixture(tmp_path)
    with (run_dir / "raw_responses.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("{}\n")
    with pytest.raises(ValueError, match="原始回答文件哈希"):
        score_run(run_dir, questions, gold, anchor, questions_anchor, gold_anchor)


def test_incomplete_or_duplicate_pairs_are_rejected(tmp_path):
    run_dir, questions, gold, anchor, questions_anchor, gold_anchor = _fixture(tmp_path)
    raw_path = run_dir / "raw_responses.jsonl"
    records = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]
    records.pop()
    _write_jsonl(raw_path, records)
    manifest_path = run_dir / "sealed_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["record_count"] = len(records)
    manifest["expected_record_count"] = len(records)
    manifest["runtime_error_count"] = 0
    manifest["raw_responses_sha256"] = _sha(raw_path)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    new_anchor = _sha(manifest_path)
    with pytest.raises(ValueError, match="配对不完整"):
        score_run(run_dir, questions, gold, new_anchor, questions_anchor, gold_anchor)

    records.append(records[0])
    _write_jsonl(raw_path, records)
    manifest["record_count"] = len(records)
    manifest["expected_record_count"] = len(records)
    manifest["runtime_error_count"] = 0
    manifest["raw_responses_sha256"] = _sha(raw_path)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="重复 case/arm"):
        score_run(run_dir, questions, gold, _sha(manifest_path), questions_anchor, gold_anchor)



def test_raw_work_order_id_must_match_question_registration(tmp_path):
    run_dir, questions, gold, _anchor, questions_anchor, gold_anchor = _fixture(tmp_path)
    raw_path = run_dir / "raw_responses.jsonl"
    records = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]
    records[0]["work_order_id"] = "WO-2026-9999"
    _write_jsonl(raw_path, records)
    manifest_path = run_dir / "sealed_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["raw_responses_sha256"] = _sha(raw_path)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="工单编号"):
        score_run(run_dir, questions, gold, _sha(manifest_path), questions_anchor, gold_anchor)


def test_gold_and_questions_must_match_exactly(tmp_path):
    run_dir, questions, gold, anchor, questions_anchor, gold_anchor = _fixture(tmp_path)
    valid_gold = json.loads(gold.read_text(encoding="utf-8").splitlines()[0])
    altered_gold = {**valid_gold, "review_fixture_marker": "altered"}
    _write_jsonl(gold, [
        altered_gold,
        {**altered_gold, "case_id": "SYN-02", "review_fixture_marker": "second"},
    ])
    with pytest.raises(ValueError, match="Gold 文件与外部 SHA-256 锚点"):
        score_run(run_dir, questions, gold, anchor, questions_anchor, gold_anchor)
    changed_gold_anchor = _sha(gold)
    rescored = score_run(run_dir, questions, gold, anchor, questions_anchor, changed_gold_anchor)
    assert rescored["input_anchors"]["gold_sha256"] == changed_gold_anchor

    _write_jsonl(gold, [{**altered_gold, "case_id": "SYN-99"}])
    changed_gold_anchor = _sha(gold)
    with pytest.raises(ValueError, match="完全一致"):
        score_run(run_dir, questions, gold, anchor, questions_anchor, changed_gold_anchor)


@pytest.mark.parametrize("patch", [
    {"expected_retrieval_state": None},
    {"absence_evidence_required": True,
     "required_absence_refs": [{"table": "t", "column": "id", "snippets": ["id=x"]}]},
    {"absence_evidence_required": True,
     "required_absence_checks": [{"table": "t", "filters": {"id": "x"}}]},
    {"absence_evidence_required": True},
    {"expected_retrieval_state": "retrieval_not_required", "expected_doc_ids": ["DOC-1"]},
    {"expected_retrieval_state": "retrieval_not_required", "required_doc_snippets": ["合同原文"]},
    {"expected_early_stop_reason": "mapping_unresolved"},
    {"expected_early_stop_reason": "equipment_master_missing"},
    {"expected_retrieval_state": "clause_present_expected", "expected_doc_ids": [],
     "required_doc_snippets": []},
    {"required_ref_snippets": ["合同原文"]},
    {"expected_retrieval_state": "bounded_not_found", "expected_retrieval_attempts": 1},
])
def test_malformed_gold_is_rejected_as_a_batch_before_scoring(tmp_path, patch):
    run_dir, questions, gold, anchor, questions_anchor, _gold_anchor = _fixture(tmp_path)
    rows = [json.loads(line) for line in gold.read_text(encoding="utf-8").splitlines()]
    rows[0].update(patch)
    _write_jsonl(gold, rows)
    with pytest.raises(ValueError, match="Gold 预验证失败，整批拒绝评分"):
        score_run(run_dir, questions, gold, anchor, questions_anchor, _sha(gold))


def test_bounded_not_found_requires_exactly_two_fully_described_attempts(tmp_path):
    run_dir, questions, gold, anchor, questions_anchor, _gold_anchor = _fixture(tmp_path)
    rows = [json.loads(line) for line in gold.read_text(encoding="utf-8").splitlines()]
    rows[0].update({
        "expected_retrieval_state": "bounded_not_found",
        "expected_early_stop_reason": None,
        "expected_doc_ids": [],
        "required_sql_snippets": [],
        "required_doc_snippets": [],
        "expected_retrieval_attempts": 2,
        "expected_retrieval_strategies": ["exact", "semantic"],
        "expected_retrieval_statuses": ["no_candidates", "no_candidates"],
        "expected_candidate_counts": [0, 0],
    })
    _write_jsonl(gold, rows)
    assert score_run(run_dir, questions, gold, anchor, questions_anchor, _sha(gold))["case_count"] == 2
    rows[0].pop("expected_candidate_counts")
    _write_jsonl(gold, rows)
    with pytest.raises(ValueError, match="expected_candidate_counts 必须逐次声明两项"):
        score_run(run_dir, questions, gold, anchor, questions_anchor, _sha(gold))


def _valid_clause_gold() -> dict:
    return {
        "case_id": "CLAUSE-01",
        "expected_status": "repair_within_limit",
        "expected_retrieval_state": "clause_present_expected",
        "expected_early_stop_reason": None,
        "expected_doc_ids": ["HT-1"],
        "required_sql_snippets": ["WO-1 opened_at=2026-10-01"],
        "required_doc_snippets": ["修复时限 4 小时"],
        "required_metric": {
            "start_event": "accepted",
            "end_event": "service_restored",
            "elapsed_seconds": 3600,
            "limit_hours": 4,
            "within_limit": True,
        },
        "absence_evidence_required": False,
        "required_absence_refs": [],
        "required_absence_checks": [],
    }


def test_clause_gold_requires_complete_metric_and_work_order_date_evidence():
    valid = _valid_clause_gold()
    _validate_gold_schema([valid])

    missing_metric = {**valid}
    missing_metric.pop("required_metric")
    with pytest.raises(ValueError, match="完整 required_metric"):
        _validate_gold_schema([missing_metric])

    missing_opened_at = {**valid, "required_sql_snippets": ["WO-1 equipment=DEV-1"]}
    with pytest.raises(ValueError, match="opened_at 日期"):
        _validate_gold_schema([missing_opened_at])

    contradictory = {**valid, "required_metric": {**valid["required_metric"], "within_limit": False}}
    with pytest.raises(ValueError, match="expected_status 与 required_metric"):
        _validate_gold_schema([contradictory])


def test_structured_sql_refs_can_replace_legacy_snippets_without_weakening_old_gold():
    required_ref = {
        "table": "cross_source_demo.work_order_events",
        "row_pk": "WO-1|accepted",
        "required_columns": ["work_order_id", "event_type", "opened_at"],
        "required_values": {
            "work_order_id": "WO-1", "event_type": "accepted", "opened_at": "2026-10-01",
        },
    }
    structured = _valid_clause_gold()
    structured.pop("required_sql_snippets")
    structured["required_sql_refs"] = [required_ref]
    _validate_gold_schema([structured])

    combined = _valid_clause_gold()
    combined["required_sql_refs"] = [required_ref]
    _validate_gold_schema([combined])

    legacy_missing = _valid_clause_gold()
    legacy_missing.pop("required_sql_snippets")
    with pytest.raises(ValueError, match="required_sql_snippets"):
        _validate_gold_schema([legacy_missing])


@pytest.mark.parametrize("bad_ref", [
    {"table": "cross_source_demo.work_order_events", "row_pk": "", "required_columns": ["opened_at"],
     "required_values": {"opened_at": "2026-10-01"}},
    {"table": "cross_source_demo.work_order_events", "row_pk": "WO-1|accepted", "required_columns": [],
     "required_values": {"opened_at": "2026-10-01"}},
    {"table": "cross_source_demo.work_order_events", "row_pk": "WO-1|accepted", "required_columns": ["opened_at"],
     "required_values": {}},
])
def test_structured_sql_refs_reject_malformed_requirements(bad_ref):
    gold = _valid_clause_gold()
    gold.pop("required_sql_snippets")
    gold["required_sql_refs"] = [bad_ref]
    with pytest.raises(ValueError, match="required_sql_refs"):
        _validate_gold_schema([gold])


def test_key_event_missing_early_stop_gold_requires_query_backed_absence_evidence():
    gold = {
        "case_id": "MISSING-EVENT-01",
        "expected_status": "not_determined",
        "expected_retrieval_state": "retrieval_not_required",
        "expected_early_stop_reason": "key_event_missing",
        "expected_missing_events": ["accepted"],
        "expected_evidence_pks": [],
        "expected_doc_ids": [],
        "required_sql_snippets": [],
        "required_doc_snippets": [],
        "absence_evidence_required": True,
        "required_absence_refs": [{
            "table": "cross_source_demo.work_order_events",
            "column": "event_type",
            "snippets": ["0 accepted rows for WO-2026-0001"],
        }],
        "required_absence_checks": [{
            "table": "cross_source_demo.work_order_events",
            "filters": {"work_order_id": "WO-2026-0001", "event_type": "accepted"},
        }],
    }

    _validate_gold_schema([gold])

    missing_set = {**gold}
    missing_set.pop("expected_missing_events")
    with pytest.raises(ValueError, match="必须声明 expected_missing_events"):
        _validate_gold_schema([missing_set])

    wrong_filter = {**gold, "expected_missing_events": ["service_restored"]}
    with pytest.raises(ValueError, match="过滤条件必须覆盖每个缺失事件"):
        _validate_gold_schema([wrong_filter])

    ungrounded_policy = {**gold, "absence_evidence_required": False,
                         "required_absence_refs": [], "required_absence_checks": [],
                         "required_sql_snippets": ["some positive row"]}
    with pytest.raises(ValueError, match="必须要求可核验的负面证据"):
        _validate_gold_schema([ungrounded_policy])

    ungrounded = {**gold, "absence_evidence_required": False,
                  "required_absence_refs": [], "required_absence_checks": []}
    with pytest.raises(ValueError, match="行级证据早停题"):
        _validate_gold_schema([ungrounded])


def test_contract_status_inactive_reason_is_narrowly_added_for_v03():
    gold = {
        "case_id": "CS14-Q4F03-018",
        "expected_status": "not_determined",
        "expected_retrieval_state": "retrieval_not_required",
        "expected_early_stop_reason": "contract_status_inactive",
        "expected_evidence_pks": ["WO-QF-018", "CT-Q4F03-018|EQ-Q4F03-018"],
        "expected_doc_ids": [],
        "required_sql_snippets": ["opened_at"],
        "required_doc_snippets": [],
        "absence_evidence_required": False,
        "required_absence_refs": [],
        "required_absence_checks": [],
    }
    with pytest.raises(ValueError, match="v0.6 本轮限定范围"):
        _validate_gold_schema([gold])
    _validate_gold_schema(
        [gold], additional_early_stop_reasons={"contract_status_inactive"}
    )


@pytest.mark.parametrize("field,value", [
    ("elapsed_seconds", float("nan")),
    ("elapsed_seconds", float("inf")),
    ("elapsed_seconds", float("-inf")),
    ("limit_hours", float("nan")),
    ("limit_hours", float("inf")),
    ("limit_hours", float("-inf")),
])
def test_clause_gold_rejects_non_finite_metric_values(field, value):
    gold = _valid_clause_gold()
    gold["required_metric"][field] = value
    with pytest.raises(ValueError, match="有限数值"):
        _validate_gold_schema([gold])


def _cs17_fixture():
    contract = {
        "schema": CS17_COVERAGE_SCHEMA,
        "total_questions": 32,
        "case_groups": {
            group: sorted(case_ids) for group, case_ids in CS17_CASE_GROUP_IDS.items()
        },
    }
    questions = [{
        "case_id": case_id,
        "work_order_id": f"WO-ABYSS-{index:03}",
        "question": f"WO-ABYSS-{index:03} 的恢复时限",
    } for index, case_id in enumerate(
        sorted(set().union(*CS17_CASE_GROUP_IDS.values())), start=1
    )]
    gold_rows = []
    for question in questions:
        case_id = question["case_id"]
        row = {
            "case_id": case_id,
            "expected_status": "repair_within_limit",
            "expected_metric_state": "computable",
            "expected_retrieval_state": "clause_present_expected",
            "expected_early_stop_reason": None,
            "expected_doc_ids": ["ABYSS-CT-SYN"],
            "required_sql_refs": [{
                "table": "work_orders",
                "row_pk": question["work_order_id"],
                "required_columns": ["work_order_id", "opened_at"],
                "required_values": {"work_order_id": question["work_order_id"]},
            }],
            "required_sql_snippets": [],
            "required_doc_snippets": ["恢复时限 6 小时"],
            "required_absence_refs": [],
            "required_absence_checks": [],
            "absence_evidence_required": False,
            "required_metric": {
                "start_event": "accepted", "end_event": "service_restored",
                "elapsed_seconds": 3600, "limit_hours": 6, "within_limit": True,
            },
        }
        row["required_sql_refs"].extend([
            {
                "table": "work_order_events",
                "row_pk": f"{question['work_order_id']}|{event_type}",
                "required_columns": ["work_order_id", "event_type", "event_at"],
                "required_values": {
                    "work_order_id": question["work_order_id"],
                    "event_type": event_type,
                    "event_at": "2026-10-01T00:00:00Z",
                },
            }
            for event_type in ("accepted", "service_restored")
        ])
        if case_id in CS17_EARLY_STOP_REASON_BY_CASE:
            reason = CS17_EARLY_STOP_REASON_BY_CASE[case_id]
            metric_state = (
                "event_missing" if reason == "key_event_missing"
                else "event_reversed" if reason == "event_time_reversed"
                else "prerequisite_blocked"
            )
            row.update({
                "expected_status": "not_determined",
                "expected_metric_state": metric_state,
                "expected_retrieval_state": "retrieval_not_required",
                "expected_early_stop_reason": reason,
                "expected_doc_ids": [],
                "required_doc_snippets": [],
            })
            row.pop("required_metric")
            if metric_state == "event_missing":
                missing_event = "accepted" if case_id == "ABYSS-CS17-016" else "service_restored"
                row["expected_missing_events"] = [missing_event]
                row["required_sql_refs"] = [
                    ref for ref in row["required_sql_refs"]
                    if ref.get("table") != "work_order_events"
                    or ref.get("required_values", {}).get("event_type") != missing_event
                ]
            elif metric_state == "prerequisite_blocked":
                row["required_sql_refs"] = [
                    ref for ref in row["required_sql_refs"]
                    if ref.get("table") != "work_order_events"
                ]
            if case_id in {"ABYSS-CS17-007", "ABYSS-CS17-015", "ABYSS-CS17-016"}:
                table = "work_order_events" if case_id in {
                    "ABYSS-CS17-015", "ABYSS-CS17-016"
                } else "contract_equipment"
                filters = ({"work_order_id": question["work_order_id"], "event_type": "missing"}
                           if table == "work_order_events"
                           else {"equipment_id": f"EQ-{case_id}"})
                row.update({
                    "absence_evidence_required": True,
                    "required_absence_refs": [{"table": table, "column": "event_type", "snippets": ["0 rows"]}],
                    "required_absence_checks": [{"table": table, "filters": filters}],
                })
        elif case_id in CS17_NULLABLE_METRIC_CASE_IDS:
            metric_state = "sla_not_stated" if case_id in {
                "ABYSS-CS17-018", "ABYSS-CS17-023"
            } else "sla_unresolved"
            row.update({
                "expected_status": "not_determined",
                "expected_metric_state": metric_state,
                "required_doc_snippets": ["合同条款冲突或未约定恢复时限"],
            })
            row.pop("required_metric")
            row["required_sql_refs"] = [
                ref for ref in row["required_sql_refs"]
                if ref.get("table") != "work_order_events"
            ]
        gold_rows.append(row)
    return questions, contract, gold_rows


def test_cs17_coverage_and_gold_state_contract_are_enforced():
    questions, contract, gold_rows = _cs17_fixture()
    groups = _cs17_case_groups(contract, {row["case_id"] for row in questions})
    assert {key: len(values) for key, values in groups.items()} == {"cohort_a": 16, "cohort_b": 16}
    _validate_cs17_gold_groups(gold_rows, questions, contract)

    inconsistent_absence = [dict(row) for row in gold_rows]
    next(row for row in inconsistent_absence if row["case_id"] == "ABYSS-CS17-001")[
        "absence_evidence_required"
    ] = True
    with pytest.raises(ValueError, match="absence_evidence_required"):
        _validate_cs17_gold_groups(inconsistent_absence, questions, contract)

    refs_without_checks = [dict(row) for row in gold_rows]
    next(row for row in refs_without_checks if row["case_id"] == "ABYSS-CS17-001")[
        "required_absence_refs"
    ] = [{"table": "work_order_events", "column": "event_type", "snippets": ["0 rows"]}]
    with pytest.raises(ValueError, match="absence_evidence_required"):
        _validate_cs17_gold_groups(refs_without_checks, questions, contract)

    checks_without_refs = [dict(row) for row in gold_rows]
    next(row for row in checks_without_refs if row["case_id"] == "ABYSS-CS17-001")[
        "required_absence_checks"
    ] = [{"table": "work_order_events", "filters": {"event_type": "accepted"}}]
    with pytest.raises(ValueError, match="absence_evidence_required"):
        _validate_cs17_gold_groups(checks_without_refs, questions, contract)

    incorrect = [dict(row) for row in gold_rows]
    blocked = next(row for row in incorrect if row["case_id"] == "ABYSS-CS17-007")
    blocked["expected_early_stop_reason"] = "validity_window_not_cover"
    with pytest.raises(ValueError, match="早停原因码"):
        _validate_cs17_gold_groups(incorrect, questions, contract)

    wrong_metric_state = [dict(row) for row in gold_rows]
    next(row for row in wrong_metric_state if row["case_id"] == "ABYSS-CS17-018")[
        "expected_metric_state"
    ] = "sla_unresolved"
    with pytest.raises(ValueError, match="expected_metric_state 与冻结题型"):
        _validate_cs17_gold_groups(wrong_metric_state, questions, contract)

    wrong_metric_math = [dict(row) for row in gold_rows]
    next(row for row in wrong_metric_math if row["case_id"] == "ABYSS-CS17-001")[
        "required_metric"
    ] = {
        "start_event": "accepted", "end_event": "service_restored",
        "elapsed_seconds": 8 * 3600, "limit_hours": 6, "within_limit": True,
    }
    with pytest.raises(ValueError, match="指标/检索/早停字段矛盾"):
        _validate_cs17_gold_groups(wrong_metric_math, questions, contract)

    extra_metric_field = [
        {**row, "required_metric": dict(row["required_metric"])}
        if row.get("required_metric") is not None
        else dict(row)
        for row in gold_rows
    ]
    next(row for row in extra_metric_field if row["case_id"] == "ABYSS-CS17-001")[
        "required_metric"
    ]["unregistered_note"] = "must be rejected"
    with pytest.raises(ValueError, match="指标/检索/早停字段矛盾"):
        _validate_cs17_gold_groups(extra_metric_field, questions, contract)

    early_case_as_computable = [dict(row) for row in gold_rows]
    early_case = next(row for row in early_case_as_computable if row["case_id"] == "ABYSS-CS17-007")
    early_case.update({
        "expected_metric_state": "computable",
        "expected_status": "repair_within_limit",
        "expected_retrieval_state": "clause_present_expected",
        "expected_early_stop_reason": None,
        "required_metric": {
            "start_event": "accepted", "end_event": "service_restored",
            "elapsed_seconds": 3600, "limit_hours": 6, "within_limit": True,
        },
    })
    with pytest.raises(ValueError, match="expected_metric_state 与冻结题型"):
        _validate_cs17_gold_groups(early_case_as_computable, questions, contract)

    first_response_evidence = [dict(row) for row in gold_rows]
    next(row for row in first_response_evidence if row["case_id"] == "ABYSS-CS17-001")[
        "required_doc_snippets"
    ] = ["首次响应时限 2 小时"]
    with pytest.raises(ValueError, match="不得要求首次响应条款"):
        _validate_cs17_gold_groups(first_response_evidence, questions, contract)

    nonempty_sql_snippets = [dict(row) for row in gold_rows]
    next(row for row in nonempty_sql_snippets if row["case_id"] == "ABYSS-CS17-001")[
        "required_sql_snippets"
    ] = ["SELECT * FROM work_orders"]
    with pytest.raises(ValueError, match="required_sql_snippets 必须为空"):
        _validate_cs17_gold_groups(nonempty_sql_snippets, questions, contract)

    irrelevant_event_ref = copy.deepcopy(gold_rows)
    blocked_row = next(row for row in irrelevant_event_ref if row["case_id"] == "ABYSS-CS17-007")
    blocked_row["required_sql_refs"].append({
        "table": "work_order_events", "row_pk": "WO-ABYSS-007|accepted",
        "required_columns": ["work_order_id", "event_type", "event_at"],
        "required_values": {"work_order_id": "WO-ABYSS-007", "event_type": "accepted"},
    })
    with pytest.raises(ValueError, match="不得强制引用无关事件"):
        _validate_cs17_gold_groups(irrelevant_event_ref, questions, contract)

    missing_computable_event = copy.deepcopy(gold_rows)
    computed_row = next(row for row in missing_computable_event if row["case_id"] == "ABYSS-CS17-001")
    computed_row["required_sql_refs"] = [
        ref for ref in computed_row["required_sql_refs"]
        if ref.get("table") != "work_order_events"
        or ref.get("required_values", {}).get("event_type") != "service_restored"
    ]
    with pytest.raises(ValueError, match="必须引用两个相关事件端点"):
        _validate_cs17_gold_groups(missing_computable_event, questions, contract)

    requires_missing_event = copy.deepcopy(gold_rows)
    missing_row = next(row for row in requires_missing_event if row["case_id"] == "ABYSS-CS17-015")
    missing_row["required_sql_refs"].append({
        "table": "work_order_events", "row_pk": "WO-ABYSS-015|service_restored",
        "required_columns": ["work_order_id", "event_type", "event_at"],
        "required_values": {
            "work_order_id": "WO-ABYSS-015", "event_type": "service_restored",
        },
    })
    with pytest.raises(ValueError, match="只可引用实际存在的相关事件端点"):
        _validate_cs17_gold_groups(requires_missing_event, questions, contract)

    wrong_missing_event_type = copy.deepcopy(gold_rows)
    missing_row = next(row for row in wrong_missing_event_type if row["case_id"] == "ABYSS-CS17-015")
    missing_row["expected_missing_events"] = ["unregistered_event"]
    with pytest.raises(ValueError, match="只可引用实际存在的相关事件端点"):
        _validate_cs17_gold_groups(wrong_missing_event_type, questions, contract)

    missing_present_endpoint = copy.deepcopy(gold_rows)
    missing_row = next(row for row in missing_present_endpoint if row["case_id"] == "ABYSS-CS17-015")
    missing_row["required_sql_refs"] = [
        ref for ref in missing_row["required_sql_refs"]
        if ref.get("table") != "work_order_events"
    ]
    with pytest.raises(ValueError, match="只可引用实际存在的相关事件端点"):
        _validate_cs17_gold_groups(missing_present_endpoint, questions, contract)


@require_private_data("cs17_candidate")
def test_cs17_prerun_profile_requires_exact_paths_groups_and_gold_anchor(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(scorer_module, "CS17_RUNTIME_PATH", "candidate/runtime")
    monkeypatch.setattr(scorer_module, "CS17_PROTOCOL_PATH", "candidate/protocol.md")
    monkeypatch.setattr(scorer_module, "CS17_COVERAGE_CONTRACT_PATH", "candidate/cs17_coverage_contract.json")
    monkeypatch.setattr(scorer_module, "CS17_COVERAGE_CSV_PATH", "candidate/coverage.csv")
    runtime = tmp_path / "candidate" / "runtime"
    runtime.mkdir(parents=True)
    questions, contract, _ = _cs17_fixture()
    questions_path = runtime / "questions.jsonl"
    _write_jsonl(questions_path, questions)
    protocol_path = tmp_path / "candidate" / "protocol.md"
    coverage_json_path = tmp_path / "candidate" / "cs17_coverage_contract.json"
    coverage_csv_path = tmp_path / "candidate" / "coverage.csv"
    protocol_path.write_text("# frozen test protocol\n", encoding="utf-8")
    coverage_json_path.write_text(
        json.dumps({"cs17_coverage_contract": contract}, sort_keys=True), encoding="utf-8"
    )
    coverage_csv_path.write_text("case_id,coverage_cohort,coverage_dimensions\n", encoding="utf-8")
    extra_runtime_path = runtime / "data" / "load.sql"
    extra_runtime_path.parent.mkdir(parents=True)
    extra_runtime_path.write_text("-- immutable import script\n", encoding="utf-8")
    source_schema_meta = (
        Path(__file__).resolve().parents[1] / CS17_RUNTIME_PATH / "data" / "schema_meta.json"
    )
    copied_schema_meta = runtime / "data" / "schema_meta.json"
    copied_schema_meta.write_bytes(source_schema_meta.read_bytes())
    runtime_entries = {
        "questions.jsonl": _sha(questions_path),
        "data/load.sql": _sha(extra_runtime_path),
        "data/schema_meta.json": _sha(copied_schema_meta),
    }
    runtime_manifest_path = runtime / "SHA256SUMS.txt"
    runtime_manifest_path.write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(runtime_entries.items())),
        encoding="utf-8",
    )
    table_names = {
        item["table"] for item in json.loads(copied_schema_meta.read_text(encoding="utf-8"))["tables"]
    }
    snapshot_tables = {
        name: {"row_count": 1, "sha256": "b" * 64, "runtime_csv_sha256": "b" * 64}
        for name in table_names
    }
    csv_snapshot = {
        name: {"row_count": 1, "sha256": "b" * 64} for name in table_names
    }
    config = {
        "runtime_package_id": CS17_PACKAGE_ID,
        "runtime_package_path": "candidate",
        "protocol_path": "candidate/protocol.md",
        "protocol_sha256": _sha(protocol_path),
        "runtime_manifest_sha256": _sha(runtime_manifest_path),
        "runtime_files": runtime_entries,
        "cs17_coverage_contract_sha256": _sha(coverage_json_path),
        "cs17_coverage_csv_sha256": _sha(coverage_csv_path),
        "required_question_count": 32,
        "question_count": 32,
        "database_identity": "cs17_test_db",
        "db_schema": "abyss_cs17",
        "vector_schema": "abyss_cs17_vectors",
        "vector_extension_schema": "public",
        "database_and_index_snapshot": {
            "database": "cs17_test_db",
            "database_role": "cs17_readonly",
            "write_privilege_check": {
                "status": "pass",
                "relations": {
                    **{f"abyss_cs17.{name}": False for name in table_names},
                    "abyss_cs17_vectors.chunk_vectors": False,
                    "abyss_cs17_vectors.doc_meta": False,
                },
                "schema_create_rights": {
                    "abyss_cs17": False,
                    "abyss_cs17_vectors": False,
                    "public": False,
                },
            },
            "schema": "abyss_cs17",
            "tables": snapshot_tables,
            "runtime_csv_snapshot": csv_snapshot,
            "rag_index": {"doc_count": 34, "chunk_count": 1, "sha256": "c" * 64},
        },
        "arm_pair": "matched_reference_fact",
        "arm_names": list(MATCHED_ARMS),
        "arm_design": CS14_ARM_DESIGN,
        "gold_sha256": "a" * 64,
        "gate_evidence_path": "C:/external/cs17-gate.json",
        "gate_evidence_sha256": "e" * 64,
        "cs17_coverage_contract": contract,
        "seed": 20261009,
        "time_anchor": "2026-10-09",
        "model_config": {
            "provider_identity": "api.example.invalid",
            "model": "test-model",
            "model_revision": None,
            "model_revision_status": "not_exposed_by_provider_or_configuration",
            "api_base_identity": "https://api.example.invalid/v1",
            "timeout_seconds": 30.0,
            "max_retries": 2,
            "temperature": 0.0,
            "request_parameters": {
                "temperature": {"sent": True, "value": 0.0},
                "top_p": {
                    "sent": False, "value": None,
                    "effective_value": "provider_default_unknown",
                },
                "rag_max_tokens": {
                    "sent": False, "value": None,
                    "effective_value": "provider_default_unknown",
                },
            },
        },
        "nl2sql_parameters": {
            "max_tokens": 16000, "max_attempts": 2, "max_refs": 10, "readonly": True,
        },
    }
    assert _validate_cs17_pre_run(config, questions, questions_path)
    missing_database_snapshot = dict(config)
    missing_database_snapshot.pop("database_and_index_snapshot")
    with pytest.raises(ValueError, match="database_and_index_snapshot 缺失"):
        _validate_cs17_pre_run(missing_database_snapshot, questions, questions_path)
    writable_snapshot = json.loads(json.dumps(config))
    writable_snapshot["database_and_index_snapshot"]["write_privilege_check"][
        "relations"]["abyss_cs17.work_orders"] = True
    with pytest.raises(ValueError, match="未完整证明目标表和 schema 只读"):
        _validate_cs17_pre_run(writable_snapshot, questions, questions_path)
    omitted_privilege = json.loads(json.dumps(config))
    omitted_privilege["database_and_index_snapshot"]["write_privilege_check"][
        "relations"].pop("abyss_cs17.work_orders")
    with pytest.raises(ValueError, match="未完整证明目标表和 schema 只读"):
        _validate_cs17_pre_run(omitted_privilege, questions, questions_path)
    missing_seed = dict(config)
    missing_seed.pop("seed")
    with pytest.raises(ValueError, match="显式整数 seed"):
        _validate_cs17_pre_run(missing_seed, questions, questions_path)
    missing_gate_evidence = dict(config)
    missing_gate_evidence.pop("gate_evidence_sha256")
    with pytest.raises(ValueError, match="外部 gate-evidence"):
        _validate_cs17_pre_run(missing_gate_evidence, questions, questions_path)
    coerced_question_count = dict(config)
    coerced_question_count["question_count"] = 32.0
    with pytest.raises(ValueError, match="32 题/匹配臂设计契约"):
        _validate_cs17_pre_run(coerced_question_count, questions, questions_path)
    missing_runtime_anchor = dict(config)
    missing_runtime_anchor.pop("runtime_manifest_sha256")
    with pytest.raises(ValueError, match="runtime_manifest_sha256"):
        _validate_cs17_pre_run(missing_runtime_anchor, questions, questions_path)
    bad_runtime_member_hash = json.loads(json.dumps(config))
    bad_runtime_member_hash["runtime_files"]["data/load.sql"] = "0" * 64
    with pytest.raises(ValueError, match="runtime_files 与完整冻结清单"):
        _validate_cs17_pre_run(bad_runtime_member_hash, questions, questions_path)
    extra_runtime_path.write_text("-- changed import script\n", encoding="utf-8")
    with pytest.raises(ValueError, match="runtime 成员摘要不匹配"):
        _validate_cs17_pre_run(config, questions, questions_path)
    extra_runtime_path.write_text("-- immutable import script\n", encoding="utf-8")
    guessed_top_p = json.loads(json.dumps(config))
    guessed_top_p["model_config"]["request_parameters"]["top_p"] = {
        "sent": False, "value": 1.0, "effective_value": 1.0,
    }
    with pytest.raises(ValueError, match="top_p 必须记录为未发送"):
        _validate_cs17_pre_run(guessed_top_p, questions, questions_path)
    coerced_temperature_flag = json.loads(json.dumps(config))
    coerced_temperature_flag["model_config"]["request_parameters"]["temperature"][
        "sent"] = 1
    with pytest.raises(ValueError, match="temperature 必须记录为实际发送"):
        _validate_cs17_pre_run(coerced_temperature_flag, questions, questions_path)
    config["gold_sha256"] = "invalid"
    with pytest.raises(ValueError, match="Gold SHA-256"):
        _validate_cs17_pre_run(config, questions, questions_path)


def test_cs17_schedule_is_recomputed_from_registered_seed(tmp_path):
    questions, _, _ = _cs17_fixture()
    seed = 20261009
    rng = random.Random(seed)
    expected_pairs = []
    for question in questions:
        arms = list(MATCHED_ARMS)
        rng.shuffle(arms)
        expected_pairs.append({"case_id": question["case_id"], "arms": arms})
    rng.shuffle(expected_pairs)
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(
        json.dumps({"seed": seed, "pairs": expected_pairs}, sort_keys=True), encoding="utf-8"
    )
    _validate_cs17_schedule(schedule_path, {"seed": seed}, questions)

    with pytest.raises(ValueError, match="schedule seed 与 pre-run config 不一致"):
        _validate_cs17_schedule(schedule_path, {"seed": seed + 1}, questions)

    for malformed_seed in (True, float(seed)):
        schedule_path.write_text(
            json.dumps({"seed": malformed_seed, "pairs": expected_pairs}, sort_keys=True),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="schedule seed 与 pre-run config 不一致"):
            _validate_cs17_schedule(schedule_path, {"seed": seed}, questions)

    altered_pairs = list(expected_pairs)
    altered_pairs[0] = {
        **altered_pairs[0],
        "arms": list(reversed(altered_pairs[0]["arms"])),
    }
    schedule_path.write_text(
        json.dumps({"seed": seed, "pairs": altered_pairs}, sort_keys=True), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="schedule 配对顺序与冻结 seed 重算结果不一致"):
        _validate_cs17_schedule(schedule_path, {"seed": seed}, questions)


@require_private_data("cs17_candidate")
def test_cs17_source_manifest_pins_blind_review_and_arm_code():
    project_root = Path(__file__).resolve().parents[1]
    manifest = {relative: _sha(project_root / relative) for relative in CS17_SOURCE_HASH_PATHS}
    app_paths = [
        path for path in (project_root / "app").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    ]
    manifest.update({path.relative_to(project_root).as_posix(): _sha(path) for path in app_paths})
    _validate_cs17_source_manifest({"source_manifest": manifest})
    missing_app_path = dict(manifest)
    del missing_app_path["app/tools/cross_source_policy.py"]
    with pytest.raises(ValueError, match="app 文件集合与当前应用代码不一致"):
        _validate_cs17_source_manifest({"source_manifest": missing_app_path})
    extra_app_path = {**manifest, "app/not_a_real_module.py": "0" * 64}
    with pytest.raises(ValueError, match="app 文件集合与当前应用代码不一致"):
        _validate_cs17_source_manifest({"source_manifest": extra_app_path})
    manifest["eval/cross_source_heldout/tools/score_architecture_blind_review.py"] = "0" * 64
    with pytest.raises(ValueError, match="当前源码与运行前封存哈希不一致"):
        _validate_cs17_source_manifest({"source_manifest": manifest})

    manifest["eval/cross_source_heldout/tools/score_architecture_blind_review.py"] = _sha(
        project_root / "eval/cross_source_heldout/tools/score_architecture_blind_review.py"
    )
    manifest["app/tools/cross_source_policy.py"] = "0" * 64
    with pytest.raises(ValueError, match="当前应用代码与运行前封存哈希不一致"):
        _validate_cs17_source_manifest({"source_manifest": manifest})
