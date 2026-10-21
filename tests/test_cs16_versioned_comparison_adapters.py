import csv
import json
import sys
from pathlib import Path

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "eval" / "cross_source_heldout" / "tools"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(TOOLS))

# run_cs16_architecture_comparison.py / score_cs16_architecture_comparison.py 位于
# eval/cross_source_heldout/tools/（该目录被 .gitignore 排除，仅存在于运行机）。
# CI checkout 后文件缺失会让 import 直接 ModuleNotFoundError，把整个 pytest
# 的 collection 阶段炸掉，掩盖其他测试的真实结果。这里先探测再跳过：
# 本机文件在 → 照常跑；CI 文件不在 → skip 而不是 error。
if not (TOOLS / "run_cs16_architecture_comparison.py").exists():
    pytest.skip("eval/cross_source_heldout/tools 下的 cs16 脚本不在本 checkout 中（CI 环境）", allow_module_level=True)

import run_cs16_architecture_comparison as runner  # noqa: E402
import score_cs16_architecture_comparison as scorer  # noqa: E402


def test_cs16_runner_and_scorer_bind_the_versioned_mapping_policy_protocol():
    expected = "eval/cross_source_heldout/2026Q4_fresh_cs16_v0.2_candidate/protocol.md"
    assert runner.CS16_MATCHED_PROTOCOL_PATH.as_posix() == expected
    assert scorer.CS16_PROTOCOL_PATH == expected
    assert runner.CS16_PACKAGE_ID == "2026Q4_fresh_cs16_v0.2_candidate"
    assert scorer.CS16_PACKAGE_ID == "2026Q4_fresh_cs16_v0.2_candidate"


def _questions(count: int = 32):
    return [{"case_id": f"CS16-{index:02}", "question": f"问题 {index}",
             "work_order_id": f"WO-2026-{1600 + index}"} for index in range(1, count + 1)]


def _computable_gold_rows():
    return [{
        "case_id": f"CS16-{index:02}",
        "expected_status": "repair_within_limit",
        "expected_retrieval_state": "clause_present_expected",
        "expected_metric_state": "computable",
        "required_metric": {
            "start_event": "accepted", "end_event": "service_restored",
            "elapsed_seconds": 3600, "limit_hours": 6, "within_limit": True,
        },
    } for index in range(1, 33)]


def test_runner_binds_only_exact_cs16_runtime_protocol_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "PROJECT_ROOT", tmp_path)
    runtime = tmp_path / runner.CS16_RUNTIME_PATH
    protocol = tmp_path / runner.CS16_MATCHED_PROTOCOL_PATH
    runtime.mkdir(parents=True)
    protocol.parent.mkdir(parents=True, exist_ok=True)
    protocol.write_text("protocol", encoding="utf-8")

    profile = runner._cs16_profile_for_paths(runtime, protocol, "matched_reference_fact")
    assert profile["package_id"] == runner.CS16_PACKAGE_ID
    with pytest.raises(RuntimeError, match="同一|唯一匹配"):
        runner._cs16_profile_for_paths(runtime, tmp_path / "other.md", "matched_reference_fact")
    with pytest.raises(RuntimeError, match="只允许"):
        runner._cs16_profile_for_paths(runtime, protocol, "historical_old_new")


def test_runner_live_model_preflight_requires_a_real_nonempty_response(monkeypatch):
    captured = {}

    class SuccessfulClient:
        model = "deepseek-v4.1-flash"

        def __init__(self, *, max_retries):
            captured["max_retries"] = max_retries

        def chat(self, system, user):
            captured["prompts"] = (system, user)
            return "OK"

    monkeypatch.setattr(runner, "_safe_model_config", lambda: {
        "model": "deepseek-v4.1-flash",
        "api_base_identity": "https://example.invalid/v1",
    })
    result = runner._validate_live_model_endpoint(SuccessfulClient)

    assert captured["max_retries"] == 0
    assert result["status"] == "passed"
    assert result["nonempty_text_response"] is True
    assert result["response_content_recorded"] is False
    assert "OK" not in json.dumps(result)


def test_runner_live_model_preflight_fails_closed_and_scrubs_provider_errors():
    class UnauthorizedClient:
        def __init__(self, *, max_retries):
            assert max_retries == 0

        def chat(self, system, user):
            raise RuntimeError("HTTP 401 Unauthorized; secret=do-not-leak")

    with pytest.raises(RuntimeError, match="鉴权/连通性预检失败") as error:
        runner._validate_live_model_endpoint(UnauthorizedClient)
    assert "do-not-leak" not in str(error.value)


def test_runner_live_model_preflight_rejects_empty_model_response(monkeypatch):
    class EmptyClient:
        def __init__(self, *, max_retries):
            pass

        def chat(self, system, user):
            return "  "

    with pytest.raises(RuntimeError, match="未返回可用文本"):
        runner._validate_live_model_endpoint(EmptyClient)


def test_runner_accepts_only_complete_answer_free_coverage_matrix(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(runner, "CS16_COVERAGE_PATH", Path("coverage.csv"))
    path = tmp_path / "coverage.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=["case_id", "coverage_cohort", "coverage_dimensions"]
        )
        writer.writeheader()
        writer.writerows({
            "case_id": row["case_id"],
            "coverage_cohort": "cohort_a" if index <= 16 else "cohort_b",
            "coverage_dimensions": "cross_source",
        } for index, row in enumerate(_questions(), 1))
    contract = runner._extract_cs16_coverage_matrix(path, _questions())
    assert contract["total_questions"] == 32
    assert contract["cohort_counts"] == {"cohort_a": 16, "cohort_b": 16}
    assert len(contract["coverage_matrix_sha256"]) == 64

    with path.open("a", encoding="utf-8", newline="") as stream:
        stream.write("CS16-01,cohort_a,duplicate\n")
    with pytest.raises(RuntimeError, match="恰好且唯一"):
        runner._extract_cs16_coverage_matrix(path, _questions())

    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=["case_id", "coverage_cohort", "coverage_dimensions"]
        )
        writer.writeheader()
        writer.writerows({
            "case_id": row["case_id"],
            "coverage_cohort": "cohort_a" if index <= 15 else "cohort_b",
            "coverage_dimensions": "cross_source",
        } for index, row in enumerate(_questions(), 1))
    with pytest.raises(RuntimeError, match="16/16"):
        runner._extract_cs16_coverage_matrix(path, _questions())

    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=["case_id", "coverage_cohort", "coverage_dimensions"]
        )
        writer.writeheader()
        writer.writerows({
            "case_id": row["case_id"],
            "coverage_cohort": "invalid" if index == 1 else (
                "cohort_a" if index <= 16 else "cohort_b"
            ),
            "coverage_dimensions": "cross_source",
        } for index, row in enumerate(_questions(), 1))
    with pytest.raises(RuntimeError, match="空字段或结构错误"):
        runner._extract_cs16_coverage_matrix(path, _questions())


def test_scorer_rejects_coverage_hash_drift_and_bad_cohort_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(scorer, "CS16_COVERAGE_PATH", "candidate/coverage.csv")
    path = tmp_path / "candidate" / "coverage.csv"
    path.parent.mkdir()

    def write_rows(first_cohort_count: int):
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream, fieldnames=["case_id", "coverage_cohort", "coverage_dimensions"]
            )
            writer.writeheader()
            writer.writerows({
                "case_id": row["case_id"],
                "coverage_cohort": "cohort_a" if index <= first_cohort_count else "cohort_b",
                "coverage_dimensions": "cross_source",
            } for index, row in enumerate(_questions(), 1))

    write_rows(15)
    config = {
        "cs16_coverage_matrix": {
            "schema": scorer.CS16_COVERAGE_SCHEMA,
            "total_questions": 32,
            "cohort_counts": scorer.CS16_COVERAGE_COHORT_COUNTS,
            "coverage_matrix_path": "candidate/coverage.csv",
            "coverage_matrix_sha256": scorer.sha256_file(path),
        }
    }
    with pytest.raises(ValueError, match="恰好覆盖"):
        scorer._validate_cs16_coverage_matrix(config, _questions())

    write_rows(16)
    config["cs16_coverage_matrix"]["coverage_matrix_sha256"] = scorer.sha256_file(path)
    scorer._validate_cs16_coverage_matrix(config, _questions())
    with path.open("a", encoding="utf-8", newline="") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="摘要不一致"):
        scorer._validate_cs16_coverage_matrix(config, _questions())


def test_scorer_cs16_pre_run_requires_hashes_and_exact_questions_path(tmp_path, monkeypatch):
    monkeypatch.setattr(scorer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(scorer, "CS16_RUNTIME_PATH", "candidate/runtime")
    monkeypatch.setattr(scorer, "CS16_PROTOCOL_PATH", "candidate/protocol.md")
    monkeypatch.setattr(scorer, "CS16_COVERAGE_PATH", "candidate/coverage.csv")
    questions_path = tmp_path / "candidate" / "runtime" / "questions.jsonl"
    questions_path.parent.mkdir(parents=True)
    questions_path.write_text("", encoding="utf-8")
    config = {
        "runtime_package_id": scorer.CS16_PACKAGE_ID,
        "runtime_package_path": "candidate",
        "protocol_path": "candidate/protocol.md",
        "required_question_count": 32,
        "question_count": 32,
        "arm_pair": "matched_reference_fact",
        "arm_names": list(scorer.MATCHED_ARMS),
        "arm_design": scorer.CS14_ARM_DESIGN,
        "gold_sha256": "a" * 64,
        "cs16_coverage_matrix": {
            "schema": scorer.CS16_COVERAGE_SCHEMA,
            "total_questions": 32,
            "cohort_counts": scorer.CS16_COVERAGE_COHORT_COUNTS,
            "coverage_matrix_path": "candidate/coverage.csv",
            "coverage_matrix_sha256": "b" * 64,
        },
    }
    assert scorer._validate_cs16_pre_run(config, _questions(), questions_path)
    assert scorer._cs14_profile(config, questions_path) is None
    with pytest.raises(ValueError, match="路径不匹配"):
        scorer._validate_cs16_pre_run(config, _questions(), tmp_path / "elsewhere.jsonl")


def test_cs16_question_projection_derives_exact_single_work_order_id_without_mutation():
    source = [
        {"case_id": "CS16-01", "question": "判断工单 WO-2026-1601 是否在合同修复时限内完成？"},
        {"case_id": "CS16-02", "question": "查询 WO-2026-1602 的服务恢复事件。"},
    ]
    before = json.loads(json.dumps(source))

    projected = scorer._project_cs16_question_work_order_ids(source)

    assert source == before
    assert [row["work_order_id"] for row in projected] == [
        "WO-2026-1601", "WO-2026-1602",
    ]
    assert all(row["case_id"] == source[index]["case_id"] for index, row in enumerate(projected))


def test_cs16_question_projection_fails_closed_on_zero_multiple_or_conflicting_ids():
    with pytest.raises(ValueError, match="恰好包含一个工单编号"):
        scorer._project_cs16_question_work_order_ids([
            {"case_id": "CS16-01", "question": "没有工单编号"},
        ])
    with pytest.raises(ValueError, match="恰好包含一个工单编号"):
        scorer._project_cs16_question_work_order_ids([
            {"case_id": "CS16-01", "question": "WO-2026-1601 和 WO-2026-1602"},
        ])
    with pytest.raises(ValueError, match="显式字段不一致"):
        scorer._project_cs16_question_work_order_ids([
            {
                "case_id": "CS16-01", "question": "WO-2026-1601",
                "work_order_id": "WO-2026-9999",
            },
        ])


def test_cs16_schema_projection_is_non_scoring_and_does_not_mutate_gold():
    source = [{
        "case_id": "CS16-01",
        "expected_status": "repair_within_limit",
        "expected_retrieval_state": "clause_present_expected",
        "expected_metric_state": "computable",
        "expected_doc_ids": [],
        "required_sql_refs": [],
        "required_doc_snippets": ["条款原文"],
        "required_metric": {
            "start_event": "accepted", "end_event": "service_restored",
            "elapsed_seconds": 3600, "limit_hours": 6, "within_limit": True,
        },
    }]
    before = json.loads(json.dumps(source))
    projected = scorer._cs16_schema_projection(source)
    assert source == before
    assert projected[0]["absence_evidence_required"] is False
    assert projected[0]["required_sql_snippets"] == []
    assert "expected_retrieval_statuses" not in source[0]
    assert "expected_candidate_counts" not in source[0]

    bad = [{**source[0], "expected_candidate_counts": [1, 2]}]
    with pytest.raises(ValueError, match="未登记字段"):
        scorer._cs16_schema_projection(bad)


def test_cs16_projection_passes_legacy_shape_gate_without_promoting_placeholders():
    rows = [{
        "case_id": "CS16-01",
        "expected_status": "repair_within_limit",
        "expected_retrieval_state": "clause_present_expected",
        "expected_metric_state": "computable",
        "expected_doc_ids": ["HT-CS16-001"],
        "required_sql_refs": [{
            "table": "fresh_cs16_v01.work_orders",
            "row_pk": "WO-2026-1601",
            "required_columns": ["work_order_id", "opened_at"],
            "required_values": {"work_order_id": "WO-2026-1601", "opened_at": "2026-10-01"},
        }],
        "required_doc_snippets": ["修复时限为 6 小时"],
        "required_metric": {
            "start_event": "accepted", "end_event": "service_restored",
            "elapsed_seconds": 3600, "limit_hours": 6, "within_limit": True,
        },
    }, {
        "case_id": "CS16-02",
        "expected_status": "not_determined",
        "expected_retrieval_state": "retrieval_not_required",
        "expected_metric_state": "prerequisite_blocked",
        "expected_early_stop_reason": "equipment_master_missing",
        "expected_doc_ids": [], "required_sql_refs": [{
            "table": "fresh_cs16_v01.work_orders", "row_pk": "WO-2026-1603",
            "required_columns": ["work_order_id", "opened_at"],
            "required_values": {"work_order_id": "WO-2026-1603", "opened_at": "2026-10-03"},
        }],
        "required_doc_snippets": [],
    }]
    projected = scorer._cs16_schema_projection(rows)
    scorer._validate_gold_schema(
        projected, additional_early_stop_reasons=scorer.CS16_ADDITIONAL_EARLY_STOP_REASONS,
    )
    assert all("expected_candidate_counts" not in row for row in rows)
    assert all("expected_retrieval_statuses" not in row for row in rows)


def test_cs16_document_unresolved_states_may_omit_metric_but_must_not_supply_one():
    rows = [{
        "case_id": "CS16-01",
        "expected_status": "not_determined",
        "expected_retrieval_state": "clause_present_expected",
        "expected_metric_state": "sla_not_stated",
        "expected_doc_ids": ["HT-CS16-001"],
        "required_sql_refs": [{
            "table": "fresh_cs16_v01.work_orders", "row_pk": "WO-2026-1601",
            "required_columns": ["work_order_id", "opened_at"],
            "required_values": {"work_order_id": "WO-2026-1601", "opened_at": "2026-10-01"},
        }],
        "required_doc_snippets": ["本合同未约定服务恢复时限"],
    }, {
        "case_id": "CS16-02",
        "expected_status": "not_determined",
        "expected_retrieval_state": "clause_present_expected",
        "expected_metric_state": "sla_unresolved",
        "expected_doc_ids": ["HT-CS16-002"],
        "required_sql_refs": [{
            "table": "fresh_cs16_v01.work_orders", "row_pk": "WO-2026-1602",
            "required_columns": ["work_order_id", "opened_at"],
            "required_values": {"work_order_id": "WO-2026-1602", "opened_at": "2026-10-01"},
        }],
        "required_doc_snippets": ["适用范围存在未消解冲突"],
    }]
    scorer._validate_gold_schema(
        scorer._cs16_schema_projection(rows),
        additional_early_stop_reasons=scorer.CS16_ADDITIONAL_EARLY_STOP_REASONS,
    )

    with_metric = [{**rows[0], "required_metric": {
        "start_event": "accepted", "end_event": "service_restored",
        "elapsed_seconds": 3600, "limit_hours": 6, "within_limit": True,
    }}]
    with pytest.raises(ValueError, match="必须省略 required_metric"):
        scorer._validate_gold_schema(
            scorer._cs16_schema_projection(with_metric),
            additional_early_stop_reasons=scorer.CS16_ADDITIONAL_EARLY_STOP_REASONS,
        )

    computable_without_metric = [{**rows[0],
        "expected_status": "repair_within_limit",
        "expected_metric_state": "computable",
        "required_doc_snippets": ["修复时限为 6 小时"],
    }]
    with pytest.raises(ValueError, match="必须显式声明完整 required_metric"):
        scorer._validate_gold_schema(
            scorer._cs16_schema_projection(computable_without_metric),
            additional_early_stop_reasons=scorer.CS16_ADDITIONAL_EARLY_STOP_REASONS,
        )


def test_cs16_scoring_projection_derives_absence_flag_only_from_paired_evidence_fields():
    rows = [{
        "case_id": "CS16-01",
        "required_absence_refs": [{"table": "work_order_events"}],
        "required_absence_checks": [{"table": "work_order_events", "filters": {"event_type": "accepted"}}],
    }, {"case_id": "CS16-02"}]
    scored = scorer._cs16_scoring_projection(rows)
    assert scored["CS16-01"]["absence_evidence_required"] is True
    assert scored["CS16-02"]["absence_evidence_required"] is False
    assert "expected_candidate_counts" not in scored["CS16-01"]
    assert "expected_retrieval_statuses" not in scored["CS16-01"]
    assert "absence_evidence_required" not in rows[0]

    with pytest.raises(ValueError, match="同时包含"):
        scorer._cs16_scoring_projection([{
            "case_id": "CS16-03", "required_absence_refs": [{"table": "x"}],
        }])


def test_cs16_scored_gold_does_not_receive_schema_only_placeholders(monkeypatch):
    original = {
        "case_id": "CS16-01",
        "expected_status": "not_determined",
        "expected_retrieval_state": "retrieval_not_required",
        "required_absence_refs": [{"table": "work_order_events"}],
        "required_absence_checks": [{"table": "work_order_events", "filters": {"event_type": "accepted"}}],
    }
    captured = {}

    def fake_compare(case, gold, response, elapsed_seconds):
        captured.update(gold)
        return {metric: True for metric in scorer.METRICS} | {"status_comparison_available": True}

    monkeypatch.setattr(scorer, "compare", fake_compare)
    score_gold = scorer._cs16_scoring_projection([original])["CS16-01"]
    scorer._score_one(
        {"case_id": "CS16-01", "question": "q"}, score_gold,
        {"arm": "new_fact_verification", "status": "completed", "elapsed_ms": 1,
         "response": {"trace": [], "refs": []}},
    )
    assert captured["absence_evidence_required"] is True
    assert "expected_candidate_counts" not in captured
    assert "expected_retrieval_statuses" not in captured
    assert "required_sql_snippets" not in captured


def test_cs16_gold_ids_and_retrieval_state_contract():
    rows = [{
        "case_id": f"CS16-{index:02}",
        "expected_status": "repair_within_limit",
        "expected_retrieval_state": "clause_present_expected",
        "expected_metric_state": "computable",
        "required_metric": {
            "start_event": "accepted", "end_event": "service_restored",
            "elapsed_seconds": 3600, "limit_hours": 6, "within_limit": True,
        },
    } for index in range(1, 33)]
    scorer._validate_cs16_gold_rows(rows, _questions())
    with pytest.raises(ValueError, match="一一对应"):
        scorer._validate_cs16_gold_rows(rows[:-1], _questions())
    rows[0]["expected_retrieval_state"] = "bounded_not_found"
    rows[0]["expected_status"] = "not_determined"
    rows[0]["expected_metric_state"] = "prerequisite_blocked"
    rows[0].pop("required_metric")
    rows[0]["expected_early_stop_reason"] = "mapping_unresolved"
    with pytest.raises(ValueError, match="不包含 bounded_not_found"):
        scorer._validate_cs16_gold_rows(rows, _questions())


def test_cs16_metric_state_controls_metric_presence_and_state_semantics():
    base = {
        "case_id": "CS16-01",
        "expected_status": "not_determined",
        "expected_retrieval_state": "retrieval_not_required",
        "expected_metric_state": "event_missing",
        "expected_early_stop_reason": "key_event_missing",
        "expected_missing_events": ["accepted"],
        "required_doc_snippets": [],
        "expected_doc_ids": [],
    }
    good_rows = _computable_gold_rows()
    good_rows[0] = base
    scorer._validate_cs16_gold_rows(good_rows, _questions())

    bad_metric = [dict(row) for row in good_rows]
    bad_metric[0] = {**base, "required_metric": None}
    with pytest.raises(ValueError, match="省略 required_metric"):
        scorer._validate_cs16_gold_rows(bad_metric, _questions())

    bad_missing_event = [dict(row) for row in good_rows]
    bad_missing_event[0] = {**base, "expected_missing_events": []}
    with pytest.raises(ValueError, match="缺失事件状态字段不一致"):
        scorer._validate_cs16_gold_rows(bad_missing_event, _questions())


def test_cs16_noncomputable_document_metric_passes_only_explicit_nullable_gate():
    row = {
        "case_id": "CS16-01",
        "expected_status": "not_determined",
        "expected_retrieval_state": "clause_present_expected",
        "expected_metric_state": "sla_not_stated",
        "expected_doc_ids": ["CT-CS16-001"],
        "required_sql_refs": [{
            "table": "fresh_cs16_v01.work_orders",
            "row_pk": "WO-2026-1601",
            "required_columns": ["work_order_id", "opened_at"],
            "required_values": {"work_order_id": "WO-2026-1601", "opened_at": "2026-10-01"},
        }],
        "required_doc_snippets": ["合同未约定服务恢复时限"],
    }
    projected = scorer._cs16_schema_projection([row])
    scorer._validate_gold_schema(
        projected,
        nullable_metric_case_ids={"CS16-01"},
        additional_early_stop_reasons=scorer.CS16_ADDITIONAL_EARLY_STOP_REASONS,
    )
    rows = _computable_gold_rows()
    rows[0] = row
    scorer._validate_cs16_gold_rows(rows, _questions())

    with pytest.raises(ValueError, match="不可计算指标必须"):
        scorer._validate_cs16_gold_rows(
            [{**row, "required_metric": {}}] + rows[1:], _questions()
        )


def test_cs16_absence_gold_binds_zero_count_and_complete_predicate_to_reference():
    row = {
        "case_id": "CS16-01",
        "expected_status": "not_determined",
        "expected_retrieval_state": "retrieval_not_required",
        "expected_metric_state": "event_missing",
        "expected_early_stop_reason": "key_event_missing",
        "expected_missing_events": ["accepted"],
        "required_absence_checks": [{
            "table": "fresh_cs16_v01.work_order_events",
            "filters": {"work_order_id": "WO-2026-1601", "event_type": "accepted"},
            "expected_row_count": 0,
        }],
        "required_absence_refs": [{
            "table": "fresh_cs16_v01.work_order_events",
            "column": "event_type",
            "filters": {"work_order_id": "WO-2026-1601", "event_type": "accepted"},
            "snippets": [
                "work_order_id=WO-2026-1601 event_type=accepted; 0 行"
            ],
        }],
    }
    rows = _computable_gold_rows()
    rows[0] = row
    scorer._validate_cs16_gold_rows(rows, _questions())

    bad_ref = [dict(item) for item in rows]
    bad_ref[0] = json.loads(json.dumps(row))
    bad_ref[0]["required_absence_refs"][0]["filters"]["event_type"] = "service_restored"
    bad_ref[0]["required_absence_refs"][0]["snippets"] = [
        "work_order_id=WO-2026-1601 event_type=service_restored; 0 行"
    ]
    with pytest.raises(ValueError, match="一一对应"):
        scorer._validate_cs16_gold_rows(bad_ref, _questions())

    bad_count = [dict(item) for item in rows]
    bad_count[0] = json.loads(json.dumps(row))
    bad_count[0]["required_absence_checks"][0]["expected_row_count"] = 1
    with pytest.raises(ValueError, match="期望 0 行"):
        scorer._validate_cs16_gold_rows(bad_count, _questions())
