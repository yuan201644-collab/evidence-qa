import json
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from types import SimpleNamespace

from app.tools.cross_source_demo import CrossSourceDemoTool

# 部分用例读取 eval/cross_source_heldout/ 下未入库的候选运行包（仅存在于运行机）。
# conftest.require_private_data 在数据缺失时（CI）跳过这些用例。
from conftest import require_private_data


TOOLS = Path(__file__).resolve().parents[1] / "eval" / "cross_source_heldout" / "tools"
sys.path.insert(0, str(TOOLS))

import run_architecture_comparison as runner  # noqa: E402
import architecture_arms as architecture_arms  # noqa: E402


def _write_package(root: Path, *, include_mapping_policy: bool = False) -> str:
    (root / "data" / "documents").mkdir(parents=True)
    (root / "data" / "structured").mkdir(parents=True)
    files = {
        "questions.jsonl": "\n".join(json.dumps({
            "case_id": f"N{i:02}", "work_order_id": f"WO-2026-{i:04}",
            "question": f"工单 WO-2026-{i:04} 的问题 {i}"
        }) for i in range(16)),
        "data/schema.sql": "-- schema",
        "data/load.sql": "-- load",
        "data/schema_meta.json": "{}",
        "data/schema_meta.descriptions.zh.json": "{}",
        "data/documents_manifest.json": json.dumps({"documents": [{
            "id": "C-001", "file": "C-001.md", "title": "维保服务协议", "category": "合同条款"
        }]}, ensure_ascii=False),
        "README.md": "运行包说明",
        "AUTHORING.md": "编制说明",
        "data/documents/C-001.md": "条款",
    }
    if include_mapping_policy:
        files["data/mapping_basis_policy.json"] = json.dumps({
            "policy_id": "test_runtime_policy",
            "version": "test-v1",
            "source": {"kind": "frozen_runtime_protocol"},
            "allowed": ["runtime_basis"],
            "denied": [],
        })
    for table in runner.TABLE_NAMES:
        files[f"data/structured/{table}.csv"] = "id\n"
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    entries = {
        path.relative_to(root).as_posix(): runner.sha256_file(path)
        for path in root.rglob("*") if path.is_file()
    }
    manifest = "".join(f"{digest}  {relative}\n" for relative, digest in sorted(entries.items()))
    manifest_path = root / "SHA256SUMS.txt"
    manifest_path.write_text(manifest, encoding="utf-8")
    return runner.sha256_file(manifest_path)


def test_runtime_verifier_accepts_only_frozen_allowlisted_package(tmp_path):
    expected = _write_package(tmp_path)
    entries = runner.verify_runtime(tmp_path, expected)
    assert "questions.jsonl" in entries
    assert len(runner.load_questions(tmp_path)) == 16
    _, doc_ids = runner.validate_document_manifest(tmp_path)
    assert doc_ids == {"C-001"}


def test_runtime_verifier_accepts_hashed_optional_mapping_policy(tmp_path):
    expected = _write_package(tmp_path, include_mapping_policy=True)
    entries = runner.verify_runtime(tmp_path, expected)
    assert "data/mapping_basis_policy.json" in entries


def test_runtime_verifier_rejects_unlisted_file_and_manifest_drift(tmp_path):
    expected = _write_package(tmp_path)
    (tmp_path / "gold.jsonl").write_text("secret", encoding="utf-8")
    with pytest.raises(RuntimeError, match="隔离保护|文件集合"):
        runner.verify_runtime(tmp_path, expected)
    (tmp_path / "gold.jsonl").unlink()
    (tmp_path / "data" / "documents" / "C-001.md").write_text("changed", encoding="utf-8")
    with pytest.raises(RuntimeError, match="哈希不匹配"):
        runner.verify_runtime(tmp_path, expected)


def test_document_manifest_accepts_explicit_cs08_contract_paths(tmp_path):
    _write_package(tmp_path)
    (tmp_path / "data" / "documents_manifest.json").write_text(
        json.dumps({"documents": [{
            "doc_id": "C-001", "contract_id": "C-001",
            "path": "documents/C-001.md", "title": "维保合同 C-001",
        }]}, ensure_ascii=False), encoding="utf-8"
    )
    _, doc_ids = runner.validate_document_manifest(tmp_path)
    assert doc_ids == {"C-001"}


def test_question_schema_rejects_answer_or_group_leakage(tmp_path):
    _write_package(tmp_path)
    rows = [
        {"case_id": f"N{i:02}", "work_order_id": f"WO-2026-{i:04}",
         "question": f"工单 WO-2026-{i:04} 的问题 {i}",
         "expected_status": "supported"}
        for i in range(16)
    ]
    (tmp_path / "questions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="仅允许"):
        runner.load_questions(tmp_path)


@pytest.mark.parametrize(("work_order_id", "question"), [
    ("WO-2026-0001", "请查询工单 WO-2026-0002 的修复时限"),
    ("WO-2026-0001", "请查询工单 WO-2026-0001 和 WO-2026-0002"),
    ("WO-1", "请查询工单 WO-2026-0001"),
    ("WO-X-123", "请查询工单 WO-X-123 的修复时限"),
    ("WO-CRYOSTATION-87001", "请查询工单 WO-CRYOSTATION-87001 的修复时限"),
    ("WO-CRYO-8700123", "请查询工单 WO-CRYO-8700123 的修复时限"),
])
def test_question_loader_rejects_mismatched_or_invalid_work_order_ids(
    tmp_path, work_order_id, question
):
    _write_package(tmp_path)
    rows = [
        {"case_id": f"N{i:02}", "work_order_id": f"WO-2026-{i:04}",
         "question": f"请查询工单 WO-2026-{i:04} 的修复时限"}
        for i in range(16)
    ]
    rows[0]["work_order_id"] = work_order_id
    rows[0]["question"] = question
    (tmp_path / "questions.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="工单编号"):
        runner.load_questions(tmp_path)


def test_cs11_work_order_id_is_accepted_and_routes_to_explicit_fact_plan(tmp_path):
    _write_package(tmp_path)
    rows = [
        {"case_id": f"N{i:02}", "work_order_id": f"WO-2026-{i:04}",
         "question": f"请查询工单 WO-2026-{i:04} 的修复时限"}
        for i in range(16)
    ]
    rows[0] = {
        "case_id": "CS11-01",
        "work_order_id": "WO-CRYO-87001",
        "question": "请查询工单 WO-CRYO-87001 的修复时限",
    }
    (tmp_path / "questions.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8"
    )

    questions = runner.load_questions(tmp_path)
    runner.assert_questions_have_explicit_fact_routes(questions)
    assert questions[0]["work_order_id"] == "WO-CRYO-87001"


@require_private_data("cs14_candidate")
def test_cs14_route_preflight_uses_candidate_runtime_aliases():
    """候选运行包的 CS14 全套题目必须能穿过澄清闸门并进入显式事实计划路由。

    ⚠ 2026-10-16 修订：原版第一段断言「未 _configure_env 时必被澄清闸门拦下」
    （`pytest.raises(RuntimeError, match="生产澄清闸门")`）。该期望**建立在缺陷之上**：

    这 32 题里含「工单编号」这类词，`normalizer._pass_typo` 会从中切出二字碎片
    「单编」，拿去查错别字表命中三个目标，于是追加 unresolved 信号把整题拦下 ——
    而用户写的是完全正常的词。修复见 `app/nl2sql/normalizer.py`：把 `length < 3`
    的「二字片段不采纳」判断提到「报多目标歧义」之前（那正是该文件自己注释里
    写明的「二字模糊匹配会与复合词内部普通词撞车」）。

    修好后拦下行为消失，原断言不再成立。同时实测确认：`assert_questions_have_
    explicit_fact_routes` 的路由半边**对环境不具鉴别力** —— 它用的
    `_CrossSourceRouteRegistry` 是硬编码 `cross_source_demo` 的替身，题目又自带
    `work_order_id`，因此换 `SCHEMA_META_PATH`、甚至清空 schema 变量，它都照样通过。
    原版测试的鉴别力**全部**来自澄清闸门那半边。

    故本测试改为断言仍然为真、且真正值得保护的三件事：
      1. 候选运行包没有自带别名文件（用 `_configure_env` 的 pop 分支兜住继承别名）；
      2. `_configure_env` 确实移除了继承来的 `SCHEMA_META_ALIASES_PATH`；
      3. 全部 32 题穿过澄清闸门并进入 `cross_source_demo` 显式事实计划路由。
    """
    runtime = (
        runner.PROJECT_ROOT
        / "eval/cross_source_heldout/2026Q4_fresh_cs14_v0.3_candidate/runtime"
    )
    questions = runner.load_questions(runtime)
    assert len(questions) == 32

    # 前提：候选运行包不自带别名文件，所以 `_configure_env` 必须走 pop 分支，
    # 否则默认库的手工别名会漏进候选运行包的澄清闸门（该文件 942-947 行的用意）。
    assert not (runtime / "data" / "schema_meta.aliases.zh.json").is_file()

    with patch.dict(os.environ, {
        "SCHEMA_META_PATH": str(runner.PROJECT_ROOT / "infra/schema_meta.json"),
        "SCHEMA_META_DESCRIPTIONS_PATH": str(
            runner.PROJECT_ROOT / "infra/schema_meta.descriptions.zh.json"
        ),
        "SCHEMA_META_ALIASES_PATH": str(
            runner.PROJECT_ROOT / "infra/schema_meta.aliases.zh.json"
        ),
    }, clear=False):
        runner._configure_env(
            runtime,
            "postgresql://readonly:placeholder@127.0.0.1:5432/cross_source_demo",
            "cross_source_demo",
            "cross_source_demo",
            "2026-10-08",
            "public",
        )
        assert "SCHEMA_META_ALIASES_PATH" not in os.environ
        runner.assert_questions_have_explicit_fact_routes(questions)


def test_cs17_coverage_contract_is_exactly_bound_to_runtime_questions(tmp_path):
    groups = {
        "cohort_a": [f"ABYSS-CS17-{index:03}" for index in range(1, 17)],
        "cohort_b": [f"ABYSS-CS17-{index:03}" for index in range(17, 33)],
    }
    protocol = tmp_path / "protocol.md"
    contract_document = {"cs17_coverage_contract": {
        "schema": "cs17_coverage_contract_v1",
        "total_questions": 32,
        "case_groups": groups,
    }}
    protocol.write_text(
        "```json\n" + json.dumps(contract_document) + "\n```\n",
        encoding="utf-8",
    )
    questions = [{"case_id": case_id, "question": "q", "work_order_id": "WO-ABYSS-001"}
                 for group in groups.values() for case_id in group]
    (tmp_path / "cs17_coverage_contract.json").write_text(
        json.dumps(contract_document), encoding="utf-8"
    )
    coverage_rows = ["case_id,coverage_cohort,coverage_dimensions"]
    for group, case_ids in groups.items():
        coverage_rows.extend(f"{case_id},{group},dimension" for case_id in case_ids)
    coverage_path = tmp_path / "coverage.csv"
    coverage_path.write_text("\n".join(coverage_rows) + "\n", encoding="utf-8")
    assert runner._extract_cs17_coverage_contract(protocol, questions)["case_groups"] == groups
    coverage_path.write_text("case_id,coverage_cohort,coverage_dimensions\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="恰有 32 条"):
        runner._extract_cs17_coverage_contract(protocol, questions)
    coverage_path.write_text("\n".join(coverage_rows) + "\n", encoding="utf-8")

    extra_column_rows = list(coverage_rows)
    extra_column_rows[1] += ",unexpected"
    coverage_path.write_text("\n".join(extra_column_rows) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="缺列或额外列值"):
        runner._extract_cs17_coverage_contract(protocol, questions)
    missing_value_rows = list(coverage_rows)
    first_fields = missing_value_rows[1].split(",")
    missing_value_rows[1] = ",".join(first_fields[:2]) + ","
    coverage_path.write_text("\n".join(missing_value_rows) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="缺列或额外列值"):
        runner._extract_cs17_coverage_contract(protocol, questions)
    coverage_path.write_text("\n".join(coverage_rows) + "\n", encoding="utf-8")

    groups["cohort_b"][0] = groups["cohort_a"][0]
    protocol.write_text(
        "```json\n" + json.dumps({"cs17_coverage_contract": {
            "schema": "cs17_coverage_contract_v1",
            "total_questions": 32,
            "case_groups": groups,
        }}) + "\n```\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="精确匹配|互斥"):
        runner._extract_cs17_coverage_contract(protocol, questions)


def test_cs17_formal_scope_binds_exact_runtime_protocol_and_matched_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(runner, "CS17_RUNTIME_PATH", Path("candidate/runtime"))
    monkeypatch.setattr(runner, "CS17_MATCHED_PROTOCOL_PATH", Path("candidate/protocol.md"))
    runtime = tmp_path / "candidate" / "runtime"
    protocol = tmp_path / "candidate" / "protocol.md"
    runtime.mkdir(parents=True)
    protocol.parent.mkdir(parents=True, exist_ok=True)
    protocol.write_text("frozen", encoding="utf-8")
    profile = runner._cs17_profile_for_paths(runtime, protocol, "matched_reference_fact")
    assert profile["package_id"] == "2026Q4_fresh_cs17_v0.6_candidate"
    with pytest.raises(RuntimeError, match="同时指定"):
        runner._cs17_profile_for_paths(runtime, tmp_path / "other.md", "matched_reference_fact")
    with pytest.raises(RuntimeError, match="只允许"):
        runner._cs17_profile_for_paths(runtime, protocol, "historical_old_new")


@require_private_data("cs17_candidate")
def test_cs17_v05_public_non_gold_runner_preflight():
    runtime = runner.PROJECT_ROOT / runner.CS17_RUNTIME_PATH
    protocol = runner.PROJECT_ROOT / runner.CS17_MATCHED_PROTOCOL_PATH
    manifest_sha = runner.sha256_file(runtime / "SHA256SUMS.txt")
    files = runner.verify_runtime(runtime, manifest_sha)
    questions = runner.load_questions(runtime)
    meta, tables = runner._read_metadata(runtime, "abyss_cs17", "abyss_cs17_synthetic")
    _, doc_ids = runner.validate_document_manifest(runtime)
    runner.assert_questions_have_explicit_fact_routes(questions)
    contract = runner._extract_cs17_coverage_contract(protocol, questions)

    assert len(files) == 48
    assert len(questions) == 32
    assert {row["table"] for row in tables} == runner.TABLE_NAMES
    assert len(doc_ids) == 34
    assert {group: len(values) for group, values in contract["case_groups"].items()} == {
        "cohort_a": 16, "cohort_b": 16,
    }


def test_cs17_model_config_records_sent_parameters_without_guessing(monkeypatch):
    class FakeClient:
        model = "test-model-alias"
        base_url = "https://user:secret@api.example.invalid/v1?token=secret"
        timeout = 30.0
        max_retries = 2

    monkeypatch.setattr("app.rag.llm.OpenAILikeClient", FakeClient)
    monkeypatch.delenv("LLM_MODEL_REVISION", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "secret-provider-label")
    config = runner._safe_model_config()
    assert config["provider_identity"] == "api.example.invalid"
    assert config["api_base_identity"] == "https://api.example.invalid"
    assert "secret" not in json.dumps(config)
    assert "secret-provider-label" not in json.dumps(config)
    assert config["model"] == "test-model-alias"
    assert config["model_revision"] is None
    assert config["model_revision_status"] == "not_exposed_by_provider_or_configuration"
    assert config["request_parameters"] == {
        "temperature": {"sent": True, "value": 0.0},
        "top_p": {
            "sent": False, "value": None, "effective_value": "provider_default_unknown",
        },
        "rag_max_tokens": {
            "sent": False, "value": None, "effective_value": "provider_default_unknown",
        },
    }


def test_registered_config_comparison_is_json_type_strict():
    config = {"seed": 1, "nl2sql_parameters": {"max_attempts": 2}}
    runner._assert_registered_config_unchanged(config, json.loads(json.dumps(config)))
    for malformed in (
        {"seed": True, "nl2sql_parameters": {"max_attempts": 2}},
        {"seed": 1.0, "nl2sql_parameters": {"max_attempts": 2}},
    ):
        with pytest.raises(RuntimeError, match="最终运行配置.*未发送模型请求"):
            runner._assert_registered_config_unchanged(config, malformed)


def test_cs17_gate_evidence_binds_reviews_preflight_and_api_identity(tmp_path):
    config = {
        "runtime_manifest_sha256": "a" * 64,
        "gold_sha256": "b" * 64,
        "protocol_sha256": "c" * 64,
        "cs17_coverage_contract_sha256": "f" * 64,
        "cs17_coverage_csv_sha256": "0" * 64,
        "source_manifest": {"runner.py": "d" * 64},
        "database_and_index_snapshot": {"database": "cs17_test"},
        "model_config": {
            "provider_identity": "api.example.invalid",
            "api_base_identity": "https://api.example.invalid",
            "model": "test-model",
        },
    }
    api_identity = {
        "provider_identity": "api.example.invalid",
        "api_base_identity": "https://api.example.invalid",
        "model": "test-model",
    }
    bindings = {
        "runtime_manifest_sha256": config["runtime_manifest_sha256"],
        "gold_sha256": config["gold_sha256"],
        "protocol_sha256": config["protocol_sha256"],
        "cs17_coverage_contract_sha256": config["cs17_coverage_contract_sha256"],
        "cs17_coverage_csv_sha256": config["cs17_coverage_csv_sha256"],
        "source_manifest_sha256": runner.canonical_sha256(config["source_manifest"]),
        "database_snapshot_sha256": runner.canonical_sha256(
            config["database_and_index_snapshot"]
        ),
        "api_identity": api_identity,
    }
    artifact_bindings = {
        "gold_review": {
            "runtime_manifest_sha256": config["runtime_manifest_sha256"],
            "gold_sha256": config["gold_sha256"],
        },
        "code_review": {"source_manifest_sha256": bindings["source_manifest_sha256"]},
        "protocol_review": {
            "protocol_sha256": config["protocol_sha256"],
            "cs17_coverage_contract_sha256": config["cs17_coverage_contract_sha256"],
            "cs17_coverage_csv_sha256": config["cs17_coverage_csv_sha256"],
        },
        "database_preflight": {
            "database_snapshot_sha256": bindings["database_snapshot_sha256"]
        },
        "api_smoke": {"api_identity": api_identity},
    }
    independent = {"gold_review", "code_review", "protocol_review"}
    artifacts = {}
    for name, artifact_binding in artifact_bindings.items():
        report = tmp_path / f"{name}.md"
        report.write_text(f"verified: {name}\n", encoding="utf-8")
        artifacts[name] = {
            "status": "pass",
            "path": str(report),
            "sha256": runner.sha256_file(report),
            "independent": name in independent,
            "bindings": artifact_binding,
        }
    evidence_path = tmp_path / "gate-evidence.json"
    evidence_path.write_text(json.dumps({
        "schema": "cs17_pre_run_gate_v1",
        "status": "ready_for_freeze",
        "bindings": bindings,
        "artifacts": artifacts,
    }, sort_keys=True), encoding="utf-8")
    evidence_hash = runner.sha256_file(evidence_path)
    assert runner._validate_cs17_gate_evidence(str(evidence_path), evidence_hash, config) == (
        str(evidence_path.resolve()), evidence_hash
    )

    artifacts["api_smoke"]["bindings"] = {
        "api_identity": {**api_identity, "model": "other-model"}
    }
    evidence_path.write_text(json.dumps({
        "schema": "cs17_pre_run_gate_v1",
        "status": "ready_for_freeze",
        "bindings": bindings,
        "artifacts": artifacts,
    }, sort_keys=True), encoding="utf-8")
    changed_evidence_hash = runner.sha256_file(evidence_path)
    with pytest.raises(RuntimeError, match="凭据无效：api_smoke"):
        runner._validate_cs17_gate_evidence(str(evidence_path), changed_evidence_hash, config)

    artifacts["api_smoke"]["bindings"] = {"api_identity": api_identity}
    evidence_path.write_text(json.dumps({
        "schema": "cs17_pre_run_gate_v1",
        "status": "ready_for_freeze",
        "bindings": bindings,
        "artifacts": artifacts,
    }, sort_keys=True), encoding="utf-8")
    valid_manifest_hash = runner.sha256_file(evidence_path)
    Path(artifacts["gold_review"]["path"]).write_text("tampered review report\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="报告 SHA-256 不匹配：gold_review"):
        runner._validate_cs17_gate_evidence(str(evidence_path), valid_manifest_hash, config)


def test_cs17_freeze_registration_requires_external_gate_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "load_nl2sql_env", lambda: None)
    monkeypatch.setattr(runner, "load_rag_env", lambda: None)
    monkeypatch.setattr(runner, "prepare_config", lambda _args: ({
        "runtime_package_id": runner.CS17_PACKAGE_ID,
    }, [], ""))
    registration_path = tmp_path / "registration.json"
    args = SimpleNamespace(freeze_registration=str(registration_path))
    with pytest.raises(RuntimeError, match="必须提供工作区外 gate-evidence"):
        runner.freeze_registration(args)
    assert not registration_path.exists()


def test_cross_source_demo_recognizes_cs11_work_order_id_without_database(monkeypatch):
    monkeypatch.delenv("CROSS_SOURCE_DEMO_DSN", raising=False)
    result = CrossSourceDemoTool(dsn="").run({
        "question": "请查询工单 WO-CRYO-87001 的修复时限",
    })
    assert result.trace[0].output["status"] == "demo_database_not_configured"


def test_explicit_route_preflight_rejects_generic_rag_fallthrough():
    class NoDemoRegistry:
        @staticmethod
        def has(_name):
            return False

        @staticmethod
        def names():
            return ["doc_search"]

    questions = [{
        "case_id": "CS11-01",
        "work_order_id": "WO-CRYO-87001",
        "question": "请查询工单 WO-CRYO-87001 的修复时限",
    }]
    with pytest.raises(RuntimeError, match="显式 cross_source_demo 事实计划路由"):
        runner.assert_questions_have_explicit_fact_routes(questions, NoDemoRegistry())


def test_schedule_contains_each_question_once_with_both_arms():
    questions = [{"case_id": f"N{i:02}", "work_order_id": f"WO-2026-{i:04}", "question": "Q"}
                 for i in range(16)]
    first = runner.randomized_schedule(questions, seed=20261004)
    assert first == runner.randomized_schedule(questions, seed=20261004)
    assert {row["case_id"] for row in first} == {row["case_id"] for row in questions}
    assert all(set(row["arms"]) == {"old_workflow", "new_fact_verification"} for row in first)


def test_matched_reference_schedule_and_protocol_are_explicit():
    questions = [{"case_id": "N01", "work_order_id": "WO-2026-0001", "question": "Q"}]
    schedule = runner.randomized_schedule(
        questions, seed=20261006,
        arms=("matched_reference_ablation", "new_fact_verification"),
    )
    assert set(schedule[0]["arms"]) == {
        "matched_reference_ablation", "new_fact_verification"
    }
    runner._require_current_protocol(
        Path("跨源架构事实核验匹配对照预注册_v0.1.md"), "matched_reference_fact"
    )
    runner._require_current_protocol(
        Path("跨源事实核验架构实验预注册_cs08_v0.1.md"), "matched_reference_fact"
    )
    runner._require_current_protocol(
        Path("跨源事实核验架构实验预注册_cs09_v0.1.md"), "matched_reference_fact"
    )
    runner._require_current_protocol(
        runner.PROJECT_ROOT / runner.CS11_MATCHED_PROTOCOL_PATH,
        "matched_reference_fact",
    )
    runner._require_current_protocol(
        runner.PROJECT_ROOT / runner.CS12_MATCHED_PROTOCOL_PATH,
        "matched_reference_fact",
    )
    runner._require_current_protocol(
        runner.PROJECT_ROOT / runner.CS13_MATCHED_PROTOCOL_PATH,
        "matched_reference_fact",
    )
    runner._require_current_protocol(
        runner.PROJECT_ROOT / runner.CS14_MATCHED_PROTOCOL_PATH,
        "matched_reference_fact",
    )
    with pytest.raises(RuntimeError, match="只接受冻结协议"):
        runner._require_current_protocol(Path("somewhere_else/protocol.md"), "matched_reference_fact")
    with pytest.raises(RuntimeError, match="事实核验匹配对照"):
        runner._require_current_protocol(
            Path("跨源问答新旧流程对照预注册_v0.7.md"), "matched_reference_fact"
        )


@pytest.mark.parametrize("question_count", [15, 16])
def test_question_count_matches_preregistered_minimum(tmp_path, question_count):
    _write_package(tmp_path)
    rows = [
        {"case_id": f"N{i:02}", "work_order_id": f"WO-2026-{i:04}",
         "question": f"工单 WO-2026-{i:04} 的问题 {i}"}
        for i in range(question_count)
    ]
    (tmp_path / "questions.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8"
    )
    if question_count == 15:
        with pytest.raises(RuntimeError, match="16–64"):
            runner.load_questions(tmp_path)
    else:
        assert len(runner.load_questions(tmp_path)) == 16


def test_matched_reference_arm_pair_freezes_validator_mode_as_only_treatment(tmp_path, monkeypatch):
    schema_path = tmp_path / "schema_meta.json"
    schema_path.write_text("{}", encoding="utf-8")
    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "DOC-1.md").write_text("contract", encoding="utf-8")
    calls = []

    class Registry:
        @staticmethod
        def get(name):
            return SimpleNamespace(spec=SimpleNamespace(name=name))

        @staticmethod
        def has(_name):
            return False

    def fake_build_default_orchestrator(**kwargs):
        mode = kwargs.get("fact_plan_validator_mode")
        calls.append(mode)
        executor = None if mode is None else SimpleNamespace(
            verification_enabled=(mode == "strict")
        )
        return SimpleNamespace(
            fact_plan_executor=executor,
            fact_plan_validator_mode=mode,
            registry=Registry(),
        )

    monkeypatch.setattr(architecture_arms, "_dsn_with_search_path", lambda *args, **kwargs: "readonly")
    monkeypatch.setattr(architecture_arms, "build_default_orchestrator", fake_build_default_orchestrator)
    # The arm builder intentionally configures process-wide runtime variables;
    # keep this synthetic contract test from contaminating subsequent test modules.
    original_environment = dict(os.environ)
    with patch.dict(os.environ, {}, clear=False):
        arms = architecture_arms.build_matched_reference_and_fact_arms(
            dsn="postgresql://unused",
            schema_path=schema_path,
            documents_dir=docs,
            db_schema="test_schema",
            vector_schema="test_vectors",
        )
        reference = arms["matched_reference_ablation"]
        treatment = arms["new_fact_verification"]
        assert reference.fact_plan_validator_mode == "ablation"
        assert treatment.fact_plan_validator_mode == "strict"
        assert reference.fact_plan_executor.verification_enabled is False
        assert treatment.fact_plan_executor.verification_enabled is True
        assert reference.registry.get("nl2sql").spec.name == treatment.registry.get("nl2sql").spec.name
        assert reference.registry.get("doc_search").spec.name == treatment.registry.get("doc_search").spec.name
        assert calls == [None, "strict", "ablation"]
    assert dict(os.environ) == original_environment


def _cs14_question_rows(count: int = 32) -> list[dict[str, str]]:
    return [{
        "case_id": f"CS14-{index:02}",
        "work_order_id": f"WO-2026-{index:04}",
        "question": f"请查询工单 WO-2026-{index:04} 的修复时限",
    } for index in range(1, count + 1)]


def _cs15_question_rows(count: int = 32) -> list[dict[str, str]]:
    return [{
        "case_id": f"C15-{index:02}",
        "work_order_id": f"WO-2026-{index:04}",
        "question": f"请核实工单 WO-2026-{index:04} 的服务恢复时限",
    } for index in range(1, count + 1)]


def _cs15_coverage_contract(questions: list[dict[str, str]]) -> dict:
    ids = [row["case_id"] for row in questions]
    return {
        "schema": runner.CS15_COVERAGE_SCHEMA,
        "total_questions": 32,
        "case_groups": {
            "adjudicable_within_limit": ids[0:5],
            "adjudicable_at_limit": ids[5:8],
            "adjudicable_over_limit": ids[8:11],
            "blocked_before_retrieval": ids[11:23],
            "undetermined_after_required_retrieval": ids[23:28],
            "absence_dependent": ids[28:30],
            "metric_swap_control": ids[30:32],
        },
    }


def _cs14_coverage_contract(questions: list[dict[str, str]]) -> dict:
    ids = [row["case_id"] for row in questions]
    return {
        "schema": "cs14_coverage_contract_v1",
        "total_questions": 32,
        "case_groups": {
            "adjudicable_sla_under": ids[0:4],
            "adjudicable_sla_equal": ids[4:8],
            "adjudicable_sla_over": ids[8:12],
            "boundary": ids[12:32],
        },
        "confidence_level": 0.95,
        "mcnemar_alpha": 0.05,
    }


def test_cs14_exact_runtime_path_requires_exactly_32_questions(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "PROJECT_ROOT", tmp_path)
    runtime_dir = tmp_path / runner.CS14_RUNTIME_PATH
    runtime_dir.mkdir(parents=True)
    questions = _cs14_question_rows()
    path = runtime_dir / "questions.jsonl"
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in questions), encoding="utf-8")
    assert len(runner.load_questions(runtime_dir)) == 32
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in questions[:-1]), encoding="utf-8")
    with pytest.raises(RuntimeError, match="CS14.*32"):
        runner.load_questions(runtime_dir)


def test_cs14_formal_scope_binds_runtime_protocol_and_matched_arms(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "PROJECT_ROOT", tmp_path.resolve())
    runtime = tmp_path / runner.CS14_RUNTIME_PATH
    protocol = tmp_path / runner.CS14_MATCHED_PROTOCOL_PATH
    assert runner._validate_cs14_formal_scope(runtime, protocol, "matched_reference_fact")
    with pytest.raises(RuntimeError, match="只允许"):
        runner._validate_cs14_formal_scope(runtime, protocol, "historical_old_new")
    with pytest.raises(RuntimeError, match="同时指定"):
        runner._validate_cs14_formal_scope(runtime, tmp_path / "other.md", "matched_reference_fact")


def test_cs14_formal_paths_select_v02_and_reject_v01_protocol(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "PROJECT_ROOT", tmp_path.resolve())
    assert runner.CS14_PACKAGE_ID == "2026Q4_fresh_cs14_v0.2_candidate"
    assert runner.CS14_RUNTIME_PATH.as_posix() == (
        "eval/cross_source_heldout/2026Q4_fresh_cs14_v0.2_candidate/runtime"
    )
    assert runner.CS14_MATCHED_PROTOCOL_PATH.as_posix() == (
        "eval/cross_source_heldout/2026Q4_fresh_cs14_v0.2_candidate/protocol.md"
    )
    assert runner.CS14_BUILD_VALIDATE_PATH.as_posix() == (
        "eval/cross_source_heldout/2026Q4_fresh_cs14_v0.2_candidate/build_validate.py"
    )
    runner._require_current_protocol(
        tmp_path / runner.CS14_MATCHED_PROTOCOL_PATH, "matched_reference_fact"
    )
    legacy_protocol = tmp_path / (
        "eval/cross_source_heldout/2026Q4_fresh_cs14_v0.1_candidate/protocol.md"
    )
    with pytest.raises(RuntimeError, match="只接受冻结协议"):
        runner._require_current_protocol(legacy_protocol, "matched_reference_fact")


def test_cs14_v03_exact_paths_and_coverage_contract_v2(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "PROJECT_ROOT", tmp_path.resolve())
    runtime = tmp_path / runner.CS14_V03_RUNTIME_PATH
    protocol = tmp_path / runner.CS14_V03_MATCHED_PROTOCOL_PATH
    assert runner.CS14_V03_PACKAGE_ID == "2026Q4_fresh_cs14_v0.3_candidate"
    assert runner._validate_cs14_formal_scope(runtime, protocol, "matched_reference_fact")
    with pytest.raises(RuntimeError, match="同时指定"):
        runner._validate_cs14_formal_scope(
            runtime, tmp_path / runner.CS14_MATCHED_PROTOCOL_PATH, "matched_reference_fact"
        )

    questions = _cs14_question_rows()
    ids = [row["case_id"] for row in questions]
    contract = {
        "schema": runner.CS14_V03_COVERAGE_SCHEMA,
        "total_questions": 32,
        "case_groups": {
            "adjudicable_sla_under": ids[0:13],
            "adjudicable_sla_equal": ids[13:15],
            "adjudicable_sla_over": ids[15:16],
            "mapping_blocked_before_retrieval": ids[16:23],
            "event_blocked_before_retrieval": ids[23:29],
            "undetermined_after_required_retrieval": ids[29:30],
            "insufficient_document_evidence": ids[30:32],
        },
        "confidence_level": 0.95,
        "mcnemar_alpha": 0.05,
    }
    protocol.parent.mkdir(parents=True)
    protocol.write_text(
        "```json\n" + json.dumps({"cs14_coverage_contract": contract}) + "\n```\n",
        encoding="utf-8",
    )
    assert runner._extract_cs14_coverage_contract(
        protocol, questions, contract_version=2
    ) == contract
    with pytest.raises(RuntimeError, match="不匹配"):
        runner._extract_cs14_coverage_contract(protocol, questions, contract_version=1)


def test_cs15_exact_paths_require_matched_arms_and_32_questions(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "PROJECT_ROOT", tmp_path.resolve())
    runtime = tmp_path / runner.CS15_RUNTIME_PATH
    protocol = tmp_path / runner.CS15_MATCHED_PROTOCOL_PATH
    assert runner.CS15_PACKAGE_ID == "2026Q4_fresh_cs15_v0.2_candidate"
    assert runner.CS15_RUNTIME_PATH.as_posix() == (
        "eval/cross_source_heldout/2026Q4_fresh_cs15_v0.2_candidate/runtime"
    )
    assert runner.CS15_MATCHED_PROTOCOL_PATH.as_posix() == (
        "eval/cross_source_heldout/2026Q4_fresh_cs15_v0.2_candidate/protocol.md"
    )
    assert runner._validate_cs15_formal_scope(runtime, protocol, "matched_reference_fact")
    with pytest.raises(RuntimeError, match="只允许"):
        runner._validate_cs15_formal_scope(runtime, protocol, "historical_old_new")
    with pytest.raises(RuntimeError, match="同时指定"):
        runner._validate_cs15_formal_scope(runtime, tmp_path / "other.md", "matched_reference_fact")

    v05_runtime = tmp_path / runner.CS15_V05_RUNTIME_PATH
    v05_protocol = tmp_path / runner.CS15_V05_MATCHED_PROTOCOL_PATH
    assert runner.CS15_V05_PACKAGE_ID == "2026Q4_fresh_cs15_v0.5_candidate"
    assert runner._validate_cs15_formal_scope(
        v05_runtime, v05_protocol, "matched_reference_fact"
    )
    with pytest.raises(RuntimeError, match="同一版本"):
        runner._validate_cs15_formal_scope(
            v05_runtime, protocol, "matched_reference_fact"
        )
    v06_runtime = tmp_path / runner.CS15_V06_RUNTIME_PATH
    v06_protocol = tmp_path / runner.CS15_V06_MATCHED_PROTOCOL_PATH
    assert runner.CS15_V06_PACKAGE_ID == "2026Q4_fresh_cs15_v0.6_candidate"
    assert runner._validate_cs15_formal_scope(
        v06_runtime, v06_protocol, "matched_reference_fact"
    )

    runtime.mkdir(parents=True)
    questions = _cs15_question_rows()
    question_path = runtime / "questions.jsonl"
    question_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in questions), encoding="utf-8"
    )
    assert len(runner.load_questions(runtime)) == 32
    question_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in questions[:-1]), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="CS14/CS15.*32"):
        runner.load_questions(runtime)

    v05_runtime.mkdir(parents=True)
    (v05_runtime / "questions.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in questions), encoding="utf-8"
    )
    assert len(runner.load_questions(v05_runtime)) == 32

    v06_runtime.mkdir(parents=True)
    (v06_runtime / "questions.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in questions), encoding="utf-8"
    )
    assert len(runner.load_questions(v06_runtime)) == 32


def test_cs15_coverage_contract_requires_exact_partition_and_v08_descriptive_scope(tmp_path):
    questions = _cs15_question_rows()
    protocol = tmp_path / "protocol.md"
    contract = _cs15_coverage_contract(questions)
    protocol.write_text(
        "```json\n" + json.dumps({"cs15_coverage_contract": contract}) + "\n```\n",
        encoding="utf-8",
    )
    assert runner._extract_cs15_coverage_contract(protocol, questions) == contract

    invalid = json.loads(json.dumps({"cs15_coverage_contract": contract}))
    invalid["cs15_coverage_contract"]["confidence_level"] = 0.95
    invalid["cs15_coverage_contract"]["mcnemar_alpha"] = 0.05
    protocol.write_text("```json\n" + json.dumps(invalid) + "\n```\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="v0.8 口径冲突"):
        runner._extract_cs15_coverage_contract(protocol, questions)

    invalid = json.loads(json.dumps({"cs15_coverage_contract": contract}))
    groups = invalid["cs15_coverage_contract"]["case_groups"]
    groups["absence_dependent"][0] = groups["blocked_before_retrieval"][0]
    protocol.write_text("```json\n" + json.dumps(invalid) + "\n```\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="必须含 2 个互异非空 case_id"):
        runner._extract_cs15_coverage_contract(protocol, questions)


def test_cs15_protocol_allowlist_is_exact_and_gold_digest_is_external_only(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "PROJECT_ROOT", tmp_path.resolve())
    for protocol_path in (
        runner.CS15_MATCHED_PROTOCOL_PATH,
        runner.CS15_V05_MATCHED_PROTOCOL_PATH,
        runner.CS15_V06_MATCHED_PROTOCOL_PATH,
    ):
        runner._require_current_protocol(
            tmp_path / protocol_path, "matched_reference_fact"
        )
    with pytest.raises(RuntimeError, match="只接受冻结协议"):
        runner._require_current_protocol(tmp_path / "arbitrary" / "protocol.md", "matched_reference_fact")
    legacy_protocol = tmp_path / (
        "eval/cross_source_heldout/2026Q4_fresh_cs15_v0.1_candidate/protocol.md"
    )
    with pytest.raises(RuntimeError, match="只接受冻结协议"):
        runner._require_current_protocol(legacy_protocol, "matched_reference_fact")

    digest = "D" * 64
    assert runner._cs15_gold_digest(SimpleNamespace(gold_sha256=digest), True) == digest.lower()
    assert runner._cs15_gold_digest(SimpleNamespace(), False) is None
    with pytest.raises(RuntimeError, match="--gold-sha256"):
        runner._cs15_gold_digest(SimpleNamespace(), True)


def test_cs14_requires_external_gold_digest_without_opening_gold():
    digest = "A" * 64
    args = SimpleNamespace(gold_sha256=digest)
    assert runner._cs14_gold_digest(args, True) == digest.lower()
    assert runner._cs14_gold_digest(args, False) is None
    with pytest.raises(RuntimeError, match="--gold-sha256"):
        runner._cs14_gold_digest(SimpleNamespace(), True)
    with pytest.raises(RuntimeError, match="--gold-sha256"):
        runner._cs14_gold_digest(SimpleNamespace(gold_sha256="not-a-digest"), True)


def test_freeze_registration_binds_gold_digest_in_external_registration(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(runner, "PROJECT_ROOT", workspace.resolve())
    monkeypatch.setattr(runner, "load_nl2sql_env", lambda: None)
    monkeypatch.setattr(runner, "load_rag_env", lambda: None)
    questions = _cs14_question_rows()
    gold_digest = "b" * 64
    config = {"gold_sha256": gold_digest, "question_count": 32}
    monkeypatch.setattr(runner, "prepare_config", lambda _args: (config, questions, "dsn"))
    args = SimpleNamespace(
        freeze_registration=str(tmp_path / "custodian" / "registration.json"),
        protocol_sha256="a" * 64,
        runtime_manifest_sha256="c" * 64,
        seed=7,
        warmup_question="previously exposed question",
        arm_pair="matched_reference_fact",
    )

    path = runner.freeze_registration(args)
    registration = json.loads(path.read_text(encoding="utf-8"))
    assert path.is_relative_to(tmp_path / "custodian")
    assert registration["gold_sha256"] == gold_digest
    assert registration["config"]["gold_sha256"] == gold_digest


def test_cs14_protocol_json_contract_requires_partition_of_all_questions(tmp_path):
    questions = _cs14_question_rows()
    protocol = tmp_path / "protocol.md"
    body = {"cs14_coverage_contract": _cs14_coverage_contract(questions)}
    protocol.write_text(
        "Pre-registration\n```json\n" + json.dumps(body, ensure_ascii=False) + "\n```\n",
        encoding="utf-8",
    )
    assert runner._extract_cs14_coverage_contract(protocol, questions) == body["cs14_coverage_contract"]

    invalid = json.loads(json.dumps(body))
    invalid["cs14_coverage_contract"]["case_groups"]["adjudicable_sla_over"][0] = (
        invalid["cs14_coverage_contract"]["case_groups"]["adjudicable_sla_under"][0]
    )
    protocol.write_text(
        "```json\n" + json.dumps(invalid, ensure_ascii=False) + "\n```",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="互斥"):
        runner._extract_cs14_coverage_contract(protocol, questions)


def test_cs08_protocol_allows_its_preregistered_twelve_question_suite(tmp_path):
    _write_package(tmp_path)
    rows = [
        {"case_id": f"N{i:02}", "work_order_id": f"WO-2026-{i:04}",
         "question": f"工单 WO-2026-{i:04} 的问题 {i}"}
        for i in range(12)
    ]
    (tmp_path / "questions.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8"
    )
    assert len(runner.load_questions(tmp_path, allow_cs08_count=True)) == 12
    with pytest.raises(RuntimeError, match="16–64"):
        runner.load_questions(tmp_path)


def test_cs09_protocol_allows_its_preregistered_twelve_question_suite(tmp_path):
    _write_package(tmp_path)
    rows = [
        {"case_id": f"N{i:02}", "work_order_id": f"WO-2026-{i:04}",
         "question": f"工单 WO-2026-{i:04} 的问题 {i}"}
        for i in range(12)
    ]
    (tmp_path / "questions.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8"
    )
    assert len(runner.load_questions(tmp_path, allow_cs08_count=True)) == 12


def test_question_loader_derives_only_redundant_work_order_id_from_question_text(tmp_path):
    _write_package(tmp_path)
    rows = [
        {"case_id": f"N{i:02}", "question": f"工单 WO-2026-{i:04} 的问题 {i}"}
        for i in range(16)
    ]
    (tmp_path / "questions.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8"
    )
    loaded = runner.load_questions(tmp_path)
    assert len(loaded) == 16
    assert loaded[0]["work_order_id"] == "WO-2026-0000"


def test_formal_runner_only_accepts_registered_v07_protocol():
    with pytest.raises(RuntimeError, match="只接受冻结协议"):
        runner._require_current_protocol(Path("跨源问答新旧流程对照预注册_v0.2.md"))
    with pytest.raises(RuntimeError, match="只接受冻结协议"):
        runner._require_current_protocol(Path("跨源问答新旧流程对照预注册_v0.4.md"))
    with pytest.raises(RuntimeError, match="v0.7"):
        runner._require_current_protocol(Path("跨源问答新旧流程对照预注册_v0.5.md"))
    with pytest.raises(RuntimeError, match="v0.7"):
        runner._require_current_protocol(Path("跨源问答新旧流程对照预注册_v0.6.md"))
    runner._require_current_protocol(Path("跨源问答新旧流程对照预注册_v0.7.md"))


@require_private_data("cs14_candidate")
def test_source_manifest_includes_default_baseline_and_blind_scoring_tools():
    manifest = runner._source_manifest()
    assert "eval/cross_source_reference_baseline.py" in manifest
    assert "eval/cross_source_heldout/tools/score_architecture_blind_review.py" in manifest
    assert "eval/cross_source_heldout/tools/prepare_architecture_blind_review.py" in manifest
    assert "docs/architecture/跨源问答新旧流程对照预注册_v0.4.md" in manifest
    assert "tests/test_architecture_comparison_wiring.py" in manifest
    assert "eval/cross_source_heldout/2026Q4_fresh_cs11_v0.2_candidate/protocol.md" in manifest
    assert "eval/cross_source_heldout/2026Q4_fresh_cs11_v0.2_candidate/build_validate.py" in manifest
    assert "eval/cross_source_heldout/2026Q4_fresh_cs12_v0.1_candidate/protocol.md" in manifest
    assert "eval/cross_source_heldout/2026Q4_fresh_cs12_v0.1_candidate/build_validate.py" in manifest
    assert "eval/cross_source_heldout/2026Q4_fresh_cs13_v0.1_candidate/protocol.md" in manifest
    assert "eval/cross_source_heldout/2026Q4_fresh_cs13_v0.1_candidate/build_validate.py" in manifest
    assert "eval/cross_source_heldout/tools/load_cs11.py" in manifest
    assert "eval/cross_source_heldout/tools/prepare_cs11_index.py" in manifest
    assert "eval/cross_source_heldout/tools/load_cs15.py" in manifest
    assert "eval/cross_source_heldout/tools/prepare_cs15_index.py" in manifest
    assert "eval/cross_source_heldout/tools/prepare_cs15_readonly.py" in manifest
    assert "eval/cross_source_heldout/tools/probe_cs15_v06_index.py" in manifest


def test_formal_runner_requires_protocol_path(monkeypatch):
    required = [
        "--runtime-dir", "runtime", "--runtime-manifest-sha256", "0" * 64,
        "--protocol-sha256", "0" * 64, "--database", "db", "--db-schema", "schema",
        "--vector-schema", "vector", "--time-anchor", "2026-10-04", "--run-root", "runs",
        "--warmup-question", "known exposed warmup",
    ]
    monkeypatch.setattr(sys, "argv", ["run_architecture_comparison.py", *required])
    with pytest.raises(SystemExit) as error:
        runner.parse_args()
    assert error.value.code == 2


def test_exposed_runtime_csvs_can_be_parsed_and_snapshotted():
    runtime = (Path(__file__).resolve().parents[1] / "eval" / "cross_source_heldout"
               / "architecture_2026Q4_v0.1_runtime")
    meta = json.loads((runtime / "data" / "schema_meta.json").read_text(encoding="utf-8"))
    snapshot = runner.runtime_csv_snapshot(runtime, meta["tables"])
    assert snapshot["equipment"]["row_count"] == 12
    assert snapshot["work_order_events"]["row_count"] == 28


def test_timestamptz_snapshot_canonicalizes_equivalent_offsets():
    csv_value = runner._parse_csv_value(
        "2026-10-08T08:00:00+08:00",
        {"type": "TIMESTAMPTZ", "name": "event_at"},
    )
    database_value = runner._iso(datetime.fromisoformat("2026-10-08T00:00:00+00:00"))
    assert csv_value == database_value == "2026-10-08T00:00:00+00:00"
