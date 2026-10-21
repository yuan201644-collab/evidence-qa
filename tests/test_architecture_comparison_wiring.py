from pathlib import Path
import os
import sys

from app import routing_signals
from app.orchestrator import route
from app.tools.cross_source_demo import CrossSourceDemoTool


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "eval" / "cross_source_heldout" / "architecture_2026Q4_v0.1_runtime"
TOOLS = ROOT / "eval" / "cross_source_heldout" / "tools"
sys.path.insert(0, str(TOOLS))

from architecture_arms import build_matched_reference_and_fact_arms, build_old_new_arms  # noqa: E402
from run_architecture_comparison import _configure_env  # noqa: E402


def test_old_and_new_arms_share_real_sql_rag_tools_but_differ_in_orchestration(monkeypatch):
    monkeypatch.setenv("LLM_MOCK", "0")
    original_environment = dict(os.environ)
    try:
        arms = build_old_new_arms(
            dsn="postgresql://user:pass@127.0.0.1/demo",
            schema_path=RUNTIME / "data" / "schema_meta.json",
            documents_dir=RUNTIME / "data" / "documents",
            db_schema="architecture_2026q4_v01",
            vector_schema="cross_source_demo",
        )
        question = "请查询工单 WO-2026-8401 的受理与修复完成时间，同时查找合同修复条款。"
        monkeypatch.setattr(routing_signals, "analyse", lambda _question: {
            "doc_score": 3, "sql_score": 2,
            "doc_terms": ["合同"], "sql_terms": ["工单"],
            "doc_intent": ["条款"], "sql_intent": ["查询"],
            "connectors": ["同时"], "doc_only": ["合同"], "sql_only": ["工单"],
        })
        old_plan = route(question, arms["old_workflow"].registry)
        new_plan = route(question, arms["new_fact_verification"].registry)
        assert not arms["old_workflow"].registry.has("graph_reason")
        assert not arms["new_fact_verification"].registry.has("graph_reason")
        assert [name for name, _ in old_plan.steps] == ["nl2sql", "doc_search"]
        assert old_plan.fact_plan is None
        assert new_plan.fact_plan is not None
    finally:
        os.environ.clear()
        os.environ.update(original_environment)
        routing_signals.reload_signals()


def test_comparison_runner_freezes_cross_source_query_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("CROSS_SOURCE_DEMO_TIME_BUDGET_MS", "29999")
    monkeypatch.setenv("CROSS_SOURCE_DEMO_DOC_ATTEMPT_LIMIT", "3")
    original_environment = dict(os.environ)
    try:
        _configure_env(
            tmp_path,
            "postgresql://user:pass@127.0.0.1/demo",
            "arch_holdout_test",
            "arch_holdout_test_vec",
            "2026-10-04",
            "cross_source_demo",
        )
        assert os.environ["CROSS_SOURCE_DEMO_TIME_BUDGET_MS"] == "5000"
        assert os.environ["CROSS_SOURCE_DEMO_DOC_ATTEMPT_LIMIT"] == "2"
        assert Path(os.environ["CROSS_SOURCE_MAPPING_POLICY_PATH"]) == (
            ROOT / "data" / "cross_source_demo" / "mapping_basis_policy.json"
        )
        tool = CrossSourceDemoTool(dsn="postgresql://unused")
        assert tool._time_budget_ms == 5000
        assert tool._doc_attempt_limit == 2
    finally:
        os.environ.clear()
        os.environ.update(original_environment)


def test_matched_reference_arm_uses_same_fact_template_but_disables_verification(monkeypatch):
    monkeypatch.setenv("LLM_MOCK", "0")
    original_environment = dict(os.environ)
    try:
        arms = build_matched_reference_and_fact_arms(
            dsn="postgresql://user:pass@127.0.0.1/demo",
            schema_path=RUNTIME / "data" / "schema_meta.json",
            documents_dir=RUNTIME / "data" / "documents",
            db_schema="architecture_2026q4_v01",
            vector_schema="cross_source_demo",
        )
        assert set(arms) == {"matched_reference_ablation", "new_fact_verification"}
        assert arms["matched_reference_ablation"].fact_plan_executor is not None
        assert arms["new_fact_verification"].fact_plan_executor is not None
        assert arms["matched_reference_ablation"].fact_plan_executor is not arms["new_fact_verification"].fact_plan_executor
        assert arms["matched_reference_ablation"].registry.get("nl2sql").spec.name == "nl2sql"
        assert arms["matched_reference_ablation"].registry.get("doc_search").spec.name == "doc_search"
    finally:
        os.environ.clear()
        os.environ.update(original_environment)
        routing_signals.reload_signals()


def test_comparison_runner_uses_runtime_mapping_policy_when_present(tmp_path):
    policy_path = tmp_path / "data" / "mapping_basis_policy.json"
    policy_path.parent.mkdir()
    policy_path.write_text('{"policy_id":"test","version":"v1","source":{},"allowed":["runtime_basis"],"denied":[]}', encoding="utf-8")
    original_environment = dict(os.environ)
    try:
        _configure_env(
            tmp_path,
            "postgresql://user:pass@127.0.0.1/demo",
            "arch_holdout_test",
            "arch_holdout_test_vec",
            "2026-10-04",
            "cross_source_demo",
        )
        assert Path(os.environ["CROSS_SOURCE_MAPPING_POLICY_PATH"]) == policy_path.resolve()
    finally:
        os.environ.clear()
        os.environ.update(original_environment)
        routing_signals.reload_signals()
