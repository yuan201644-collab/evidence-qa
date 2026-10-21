"""跨源事实计划绑定的确定性边界测试。"""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from app.contracts import AskResponse, DocRef, SqlRef, TraceStep, ToolResult
from app.evidence_ledger import FactRecord
from app.cross_source_fact_plan import (
    _ScopedRagDocSearchTool,
    _dsn_with_search_path,
    _mapping_absence_trace,
    _document_validation_trace,
    _mapping_query_evidence,
    _mapping_stop_reason,
    _baseline_contract_document,
    _baseline_duration,
    _baseline_mapping,
    _handlers_for_tools,
    _make_verdict_handler,
    _make_first_response_duration_handler,
    _validate_contract_document,
    _validate_duration,
    _validate_mapping,
    _contract_candidate_refs,
    _mapping_row_refs,
    _event_absence_trace,
    _event_absence_filters_match,
    _make_repair_duration_handler,
    _repair_clause_targets_equipment,
    _contract_scope_matches_equipment,
    _explicitly_unspecified,
)
from app.fact_executor import FactRecord
from app.fact_executor import FactDecision, FactPlanExecutor, FactPlanRun
from app.orchestrator import Orchestrator
from app.fact_planner import build_work_order_sla_plan
from app.tools.base import BaseTool, ToolSpec
from app.tools.registry import ToolRegistry


def _joined_mapping_result() -> ToolResult:
    query = (
        "SELECT ce.equipment_id AS equipment_id, e.equipment_name AS equipment_name, "
        "e.customer_id AS equipment_customer_id, "
        "ce.contract_id AS contract_id, ce.valid_from AS mapping_valid_from, "
        "ce.valid_to AS mapping_valid_to, ce.mapping_basis AS mapping_basis, "
        "c.customer_id AS contract_customer_id, c.effective_from AS contract_effective_from, "
        "c.effective_to AS contract_effective_to, c.status AS contract_status "
        "FROM cross_source_demo.contract_equipment ce "
        "JOIN cross_source_demo.equipment e ON ce.equipment_id = e.equipment_id "
        "JOIN cross_source_demo.contracts c ON ce.contract_id = c.contract_id "
        "WHERE ce.equipment_id = 'DEV-001'"
    )
    return ToolResult(
        data={"rows": []},
        trace=[TraceStep(
            step_id=1,
            tool="nl2sql",
            input={},
            output={"sql": query, "row_count": 1},
            elapsed_ms=1.0,
            hit=[],
        )],
        refs=[],
    )


def test_isolated_dsn_sets_schema_search_path_without_plus_space_encoding() -> None:
    dsn = _dsn_with_search_path("postgresql://user:secret@127.0.0.1:55532/demo?connect_timeout=2")
    parsed = urlsplit(dsn)
    query = parse_qs(parsed.query)
    assert query["options"] == ["-c search_path=cross_source_demo,public"]
    assert query["connect_timeout"] == ["2"]
    assert "+" not in parsed.query

    custom = _dsn_with_search_path(
        "postgresql://user:secret@127.0.0.1:55532/demo",
        db_schema="cross_source_holdout_v01",
    )
    assert parse_qs(urlsplit(custom).query)["options"] == [
        "-c search_path=cross_source_holdout_v01,public"
    ]
    readonly = _dsn_with_search_path(
        "postgresql://user:secret@127.0.0.1:55532/demo",
        db_schema="architecture_2026q4_v01",
        extra_schema="cross_source_demo",
        read_only=True,
    )
    assert parse_qs(urlsplit(readonly).query)["options"] == [
        "-c search_path=architecture_2026q4_v01,cross_source_demo,public "
        "-c default_transaction_read_only=on"
    ]
    typed_extension = _dsn_with_search_path(
        "postgresql://user:secret@127.0.0.1:55532/demo",
        db_schema="architecture_2026q4_v02",
        extra_schema="architecture_2026q4_v02",
        extension_schema="cross_source_demo",
        read_only=True,
    )
    assert parse_qs(urlsplit(typed_extension).query)["options"] == [
        "-c search_path=architecture_2026q4_v02,cross_source_demo,public "
        "-c default_transaction_read_only=on"
    ]
    try:
        _dsn_with_search_path("postgresql://user:secret@localhost/demo", db_schema="Bad;drop")
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe database schema identifier must be rejected")


def test_fact_plan_executor_propagates_vector_extension_schema(monkeypatch) -> None:
    from urllib.parse import parse_qs, urlsplit

    import app.cross_source_fact_plan as fact_plan
    import app.nl2sql.executor as executor_module
    import app.nl2sql.pipeline as pipeline_module
    import app.nl2sql.schema as schema_module
    import app.tools.nl2sql as nl2sql_module

    captured = {}

    class FakeExecutor:
        def __init__(self, **kwargs):
            captured["dsn"] = kwargs["dsn"]
            captured["readonly"] = kwargs["readonly"]

    class FakePipeline:
        def __init__(self, *args, **kwargs):
            captured["pipeline_max_attempts"] = kwargs["max_attempts"]

    class FakeCatalog:
        @classmethod
        def from_file(cls, path):
            return cls()

    class FakeNl2SqlTool:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(executor_module, "SqlExecutor", FakeExecutor)
    monkeypatch.setattr(pipeline_module, "Nl2SqlPipeline", FakePipeline)
    monkeypatch.setattr(schema_module, "SchemaCatalog", FakeCatalog)
    monkeypatch.setattr(nl2sql_module, "Nl2SqlTool", FakeNl2SqlTool)
    monkeypatch.setattr(nl2sql_module, "_build_llm_client", lambda: object())
    monkeypatch.setattr(fact_plan, "_handlers_for_tools", lambda *args, **kwargs: object())
    monkeypatch.setenv("CROSS_SOURCE_FACT_PLAN_VECTOR_SCHEMA", "architecture_2026q4_v02")
    monkeypatch.setenv("CROSS_SOURCE_FACT_PLAN_READ_ONLY", "1")
    monkeypatch.setenv("PGVECTOR_EXTENSION_SCHEMA", "cross_source_demo")

    runtime = (Path(__file__).resolve().parents[1] / "eval" / "cross_source_heldout"
               / "architecture_2026Q4_v0.2_runtime")
    fact_plan.build_cross_source_fact_plan_executor(
        "postgresql://user:secret@localhost/cross_source_demo",
        schema_path=runtime / "data" / "schema_meta.json",
        documents_dir=runtime / "data" / "documents",
        db_schema="architecture_2026q4_v02",
    )
    options = parse_qs(urlsplit(captured["dsn"]).query)["options"][0]
    assert "search_path=architecture_2026q4_v02,cross_source_demo,public" in options
    assert "default_transaction_read_only=on" in options
    assert captured["readonly"] is True


def test_mapping_evidence_requires_executed_sql_with_both_explicit_key_joins() -> None:
    assert _mapping_query_evidence(_joined_mapping_result(), "DEV-001") is True
    assert _mapping_query_evidence(_joined_mapping_result(), "DEV-999") is False

    equivalent = _joined_mapping_result()
    equivalent_sql = equivalent.trace[0].output["sql"].replace(
        "WHERE ce.equipment_id", "WHERE e.equipment_id"
    )
    equivalent = equivalent.model_copy(update={
        "trace": [equivalent.trace[0].model_copy(update={
            "output": {"sql": equivalent_sql, "row_count": 1}
        })]
    })
    assert _mapping_query_evidence(equivalent, "DEV-001") is True

    or_predicate = equivalent_sql.replace(
        "WHERE e.equipment_id = 'DEV-001'",
        "WHERE e.equipment_id = 'DEV-001' OR e.equipment_id = 'DEV-999'",
    )
    assert _mapping_query_evidence(
        equivalent.model_copy(update={
            "trace": [equivalent.trace[0].model_copy(update={
                "output": {"sql": or_predicate, "row_count": 1}
            })]
        }), "DEV-001"
    ) is False

    bad = _joined_mapping_result()
    bad_sql_step = bad.trace[0].model_copy(update={
        "output": {"sql": "SELECT * FROM contract_equipment JOIN equipment ON true JOIN contracts ON true", "row_count": 1}
    })
    bad = bad.model_copy(update={"trace": [bad_sql_step]})
    assert _mapping_query_evidence(bad, "DEV-001") is False


def test_safe_stop_projection_keeps_supported_opened_at() -> None:
    plan = build_work_order_sla_plan("WO-2026-0001")
    run = FactPlanRun(records={
        "work_order_facts": FactRecord(
            fact_id="work", task_id="work_order_facts",
            value={"opened_at": "2026-09-18T09:00:00+08:00"}, state="supported",
        ),
        "controlled_contract_mapping": FactRecord(
            fact_id="mapping", task_id="controlled_contract_mapping",
            value="没有唯一映射", state="insufficient",
        ),
    })
    answer = Orchestrator._fact_plan_answer(plan, run)
    # 断言的是「已成立的事实不能因为后面早停就被丢掉」这个意图，
    # 不是某一个具体措辞。措辞必须是给人读的中文（2026-10-12 修：
    # 原实现直接拼内部字段名，答案以 `opened_at=2026-09-18T09:00:00+08:00；`
    # 开头，属 trace 键值对泄漏进答案）。
    assert answer.startswith("工单受理时间 2026-09-18 09:00:00+08:00；")
    assert "opened_at=" not in answer
    assert "无法判断" in answer


def test_safe_stop_projection_explains_customer_conflict_code() -> None:
    plan = build_work_order_sla_plan("WO-2026-0001")
    run = FactPlanRun(records={
        "work_order_facts": FactRecord(
            fact_id="work", task_id="work_order_facts",
            value={"opened_at": "2026-09-18T09:00:00+08:00"}, state="supported",
        ),
        "controlled_contract_mapping": FactRecord(
            fact_id="mapping", task_id="controlled_contract_mapping",
            value="没有唯一映射", state="insufficient",
        ),
    }, stop_reason_code="customer_scope_conflict")
    answer = Orchestrator._fact_plan_answer(plan, run)
    assert "设备客户与合同客户不一致" in answer


def test_safe_stop_projection_explains_inactive_contract_status() -> None:
    plan = build_work_order_sla_plan("WO-2026-0001")
    run = FactPlanRun(records={
        "work_order_facts": FactRecord(
            fact_id="work", task_id="work_order_facts",
            value={"opened_at": "2026-09-18T09:00:00+08:00"}, state="supported",
        ),
        "controlled_contract_mapping": FactRecord(
            fact_id="mapping", task_id="controlled_contract_mapping",
            value="合同状态为 suspended", state="insufficient",
        ),
    }, stop_reason_code="contract_status_inactive")
    answer = Orchestrator._fact_plan_answer(plan, run)
    assert "合同状态不是有效状态" in answer
    assert "有效期校验的合同映射" not in answer


def test_safe_stop_projection_explains_document_parse_failure_code() -> None:
    plan = build_work_order_sla_plan("WO-2026-0001")
    run = FactPlanRun(records={
        "work_order_facts": FactRecord(
            fact_id="work", task_id="work_order_facts",
            value={"opened_at": "2026-09-18T09:00:00+08:00"}, state="supported",
        ),
        "contract_repair_limit": FactRecord(
            fact_id="contract", task_id="contract_repair_limit",
            value="合同原文片段未显示修复完成时限。", state="insufficient",
            diagnostic_code="document_sla_unparsed",
        ),
    })
    answer = Orchestrator._fact_plan_answer(plan, run)
    assert "尚未可靠解析出修复完成时限" in answer


def test_safe_stop_projection_prioritizes_event_order_over_document_diagnostic() -> None:
    plan = build_work_order_sla_plan("WO-2026-0001")
    run = FactPlanRun(records={
        "work_order_facts": FactRecord(
            fact_id="work", task_id="work_order_facts",
            value={"opened_at": "2026-09-18T09:00:00+08:00"}, state="supported",
        ),
        "contract_repair_limit": FactRecord(
            fact_id="contract", task_id="contract_repair_limit",
            value="合同原文未约定修复完成总时限。", state="insufficient",
            diagnostic_code="document_sla_unstated",
        ),
        "repair_duration": FactRecord(
            fact_id="duration", task_id="repair_duration",
            value="受理事件晚于服务恢复事件。", state="contradicted",
        ),
    }, stop_reason_code="event_time_reversed")

    answer = Orchestrator._fact_plan_answer(plan, run)
    assert "时间顺序异常" in answer
    assert "未约定修复完成总时限" not in answer


def test_mapping_validator_checks_mapping_and_contract_windows_separately() -> None:
    result = _joined_mapping_result()
    facts = {
        "work_order_facts": FactRecord(
            fact_id="work",
            task_id="work_order_facts",
            value={"equipment_id": "DEV-001", "work_order_date": "2026-09-18"},
            state="supported",
        )
    }
    valid_row = {
        "equipment_id": "DEV-001",
        "equipment_name": "设备 A",
        "equipment_customer_id": "ORG-001",
        "contract_id": "HT-MNT-2026-001",
        "mapping_valid_from": "2026-09-01",
        "mapping_valid_to": "2026-12-31",
        "mapping_basis": "explicit_equipment_schedule",
        "contract_customer_id": "ORG-001",
        "contract_effective_from": "2026-09-01",
        "contract_effective_to": "2026-12-31",
        "contract_status": "active",
    }

    mapping_value, state, refs = _validate_mapping([valid_row], [], result, {}, facts)
    assert state == "supported"
    assert mapping_value["equipment_name"] == "设备 A"
    assert mapping_value["date_interval_policy"] == "closed"
    assert {ref.table for ref in refs} == {"equipment", "contract_equipment", "contracts"}

    endpoint_row = dict(
        valid_row,
        mapping_valid_from="2026-09-18",
        mapping_valid_to="2026-09-18",
        contract_effective_from="2026-09-18",
        contract_effective_to="2026-09-18",
    )
    endpoint_value, endpoint_state, _endpoint_refs = _validate_mapping(
        [endpoint_row], [], result, {}, facts
    )
    assert endpoint_state == "supported"
    assert endpoint_value["date_interval_policy"] == "closed"

    for missing_value in (None, "", "   ", "NULL", "unknown"):
        missing_customer = dict(valid_row, equipment_customer_id=missing_value,
                                contract_customer_id=missing_value)
        value, state, _refs = _validate_mapping([missing_customer], [], result, {}, facts)
        assert state == "insufficient"
        assert "没有唯一" in value

    attached_schedule = dict(valid_row, mapping_basis="attached_asset_schedule")
    _value, state, refs = _validate_mapping([attached_schedule], [], result, {}, facts)
    assert state == "supported"
    assert {ref.table for ref in refs} == {"equipment", "contract_equipment", "contracts"}

    for basis in ("signed_asset_register", "signed_acceptance_record"):
        _value, state, _refs = _validate_mapping([dict(valid_row, mapping_basis=basis)], [], result, {}, facts)
        assert state == "supported", basis

    expired_mapping = dict(valid_row, mapping_valid_to="2026-09-17")
    _value, state, _refs = _validate_mapping([expired_mapping], [], result, {}, facts)
    assert state == "insufficient"

    future_contract = dict(valid_row, contract_effective_from="2026-09-19")
    _value, state, _refs = _validate_mapping([future_contract], [], result, {}, facts)
    assert state == "insufficient"

    expired_contract = dict(valid_row, contract_effective_to="2026-09-17")
    value, state, evidence = _validate_mapping([expired_contract], [], result, {}, facts)
    assert state == "insufficient"
    assert "没有唯一" in value
    assert {ref.table for ref in evidence} == {"equipment", "contract_equipment", "contracts"}


def test_mapping_success_preserves_and_explains_excluded_competitors() -> None:
    valid = {
        "equipment_id": "DEV-001", "equipment_name": "设备 A",
        "equipment_customer_id": "ORG-001", "contract_id": "HT-CURRENT",
        "mapping_valid_from": "2026-09-01", "mapping_valid_to": "2026-12-31",
        "mapping_basis": "explicit_equipment_schedule", "contract_customer_id": "ORG-001",
        "contract_effective_from": "2026-09-01", "contract_effective_to": "2026-12-31",
        "contract_status": "active",
    }
    historical = dict(
        valid, contract_id="HT-HISTORICAL", mapping_valid_to="2026-09-17",
        contract_effective_to="2026-09-17",
    )
    result = _joined_mapping_result()
    sql_step = result.trace[0].model_copy(update={
        "output": {"sql": result.trace[0].output["sql"], "row_count": 2}
    })
    result = result.model_copy(update={"trace": [sql_step]})
    work_facts = {
        "work_order_facts": FactRecord(
            fact_id="work", task_id="work_order_facts",
            value={"equipment_id": "DEV-001", "work_order_date": "2026-09-18"},
            state="supported",
        )
    }

    mapping, state, refs = _validate_mapping(
        [valid, historical], [], result, {}, work_facts
    )
    assert state == "supported"
    assert mapping["contract_id"] == "HT-CURRENT"
    assert mapping["excluded_candidates"] == [{
        "contract_id": "HT-HISTORICAL",
        "reasons": [
            "设备—合同映射有效期未覆盖工单日期",
            "合同有效期未覆盖工单日期",
        ],
    }]
    assert {ref.row_pk for ref in refs if ref.table == "contract_equipment"} == {
        "HT-CURRENT|DEV-001", "HT-HISTORICAL|DEV-001",
    }

    mapping_refs = tuple(refs)
    clause_ref = DocRef(kind="doc", doc_id="HT-CURRENT", snippet="修复完成 8 小时内")
    duration_refs = (
        SqlRef(kind="sql", table="work_order_events", column="event_type", row_pk="WO-2026-0001|accepted", snippet="accepted"),
        SqlRef(kind="sql", table="work_order_events", column="event_type", row_pk="WO-2026-0001|service_restored", snippet="service_restored"),
    )
    final = _make_verdict_handler()(
        {"question": "是否满足修复时限？"},
        {
            "controlled_contract_mapping": FactRecord(
                "mapping", "controlled_contract_mapping", mapping, "supported",
                evidence_refs=mapping_refs,
            ),
            "contract_repair_limit": FactRecord(
                "clause", "contract_repair_limit", {"limit_hours": 8}, "supported",
                evidence_refs=(clause_ref,),
            ),
            "repair_duration": FactRecord(
                "duration", "repair_duration",
                {"work_order_id": "WO-2026-0001", "duration_seconds": 3600},
                "supported", evidence_refs=duration_refs,
            ),
        },
    )
    assert final.decision.state == "supported"
    assert "起始日和截止日均包含" in final.decision.value
    assert "HT-HISTORICAL" in final.decision.value
    assert "映射有效期未覆盖工单日期" in final.decision.value
    assert any(ref.row_pk == "HT-HISTORICAL|DEV-001" for ref in final.decision.evidence)
    assert final.tool_result.trace[0].output["mapping_interval_policy"] == "closed"


def test_ambiguous_contracts_hard_stop_before_document_retrieval() -> None:
    work_order_id = "WO-2026-8801"
    mapping_rows = [
        {
            "equipment_id": "DEV-001", "equipment_name": "设备 A",
            "equipment_customer_id": "ORG-001", "contract_id": "HT-A",
            "mapping_valid_from": "2026-09-01", "mapping_valid_to": "2026-12-31",
            "mapping_basis": "explicit_equipment_schedule", "contract_customer_id": "ORG-001",
            "contract_effective_from": "2026-09-01", "contract_effective_to": "2026-12-31",
            "contract_status": "active",
        },
        {
            "equipment_id": "DEV-001", "equipment_name": "设备 A",
            "equipment_customer_id": "ORG-001", "contract_id": "HT-B",
            "mapping_valid_from": "2026-09-01", "mapping_valid_to": "2026-12-31",
            "mapping_basis": "signed_asset_register", "contract_customer_id": "ORG-001",
            "contract_effective_from": "2026-09-01", "contract_effective_to": "2026-12-31",
            "contract_status": "active",
        },
    ]

    class FakeSqlTool:
        def run(self, payload):
            question = payload["question"]
            if "查询工单编号" in question:
                ref = SqlRef(
                    kind="sql", table="work_orders", column="work_order_id/equipment_id/opened_at",
                    row_pk=work_order_id, snippet="工单 WO-2026-8801 对应设备 DEV-001。",
                )
                sql = (
                    "SELECT work_order_id, equipment_id, opened_at FROM work_orders "
                    f"WHERE work_order_id = '{work_order_id}'"
                )
                return ToolResult(data={"rows": [{
                    "work_order_id": work_order_id,
                    "equipment_id": "DEV-001",
                    "opened_at": "2026-09-18T09:00:00+08:00",
                }]}, trace=[TraceStep(
                    step_id=1, tool="nl2sql", input=payload,
                    output={"sql": sql, "row_count": 1}, elapsed_ms=1.0, hit=[ref],
                )], refs=[ref])
            return _joined_mapping_result().model_copy(update={
                "data": {"rows": mapping_rows},
                "trace": [_joined_mapping_result().trace[0].model_copy(update={
                    "output": {
                        "sql": _joined_mapping_result().trace[0].output["sql"],
                        "row_count": 2,
                    }
                })],
            })

    class FakeDocTool:
        def __init__(self):
            self.doc_ids = []

        def run(self, payload):
            doc_id = payload["doc_id"]
            self.doc_ids.append(doc_id)
            hours = {"HT-A": 8, "HT-B": 4}[doc_id]
            ref = DocRef(
                kind="doc", doc_id=doc_id,
                snippet=f"| 修复完成 | service_restored | {hours} 小时内 |",
            )
            return ToolResult(
                data="检索到合同条款。",
                trace=[TraceStep(
                    step_id=1, tool="rerank", input={"doc_id": doc_id},
                    output={}, elapsed_ms=1.0, hit=[ref],
                )],
                refs=[ref],
            )

    plan = build_work_order_sla_plan(work_order_id)
    for question in (
        "请核验适用合同与修复时限。",
        f"{work_order_id}：请判断本工单是否存在唯一适用合同；若不存在，"
        "请给出足以证明候选合同不唯一的结构化记录。",
    ):
        doc_tool = FakeDocTool()
        executor = _handlers_for_tools(
            FakeSqlTool(), doc_tool, verification_enabled=True
        )
        run = executor.run(plan, {"work_order_id": work_order_id, "question": question})
        answer = Orchestrator._fact_plan_answer(plan, run)

        assert doc_tool.doc_ids == []
        assert not any(step.tool in {
            "hybrid_retrieve", "rerank", "cross_source_doc_retrieval",
            "cross_source_doc_validation", "llm_generate", "cross_source_multi_contract_review",
        } for step in run.trace)
        alignment = next(step for step in run.trace if step.tool == "cross_source_alignment")
        assert alignment.output["stop_reason_code"] == "ambiguous_active_contracts"
        assert "HT-A" in answer and "HT-B" in answer
        assert "映射有效期" in answer and "合同有效期" in answer
        assert "无法唯一确定适用合同" in answer
        assert {ref.row_pk for ref in run.refs if isinstance(ref, SqlRef)} >= {
            "HT-A|DEV-001", "HT-B|DEV-001",
        }


def test_mapping_refs_bind_machine_labels_to_their_declared_columns() -> None:
    row = {
        "equipment_id": "DEV-001", "equipment_name": "设备 A",
        "equipment_customer_id": "ORG-001", "contract_id": "HT-MNT-2026-001",
        "mapping_valid_from": "2026-09-01", "mapping_valid_to": "2026-12-31",
        "mapping_basis": "explicit_equipment_schedule", "contract_customer_id": "ORG-001",
        "contract_effective_from": "2026-09-01", "contract_effective_to": "2026-12-31",
        "contract_status": "active",
    }
    refs = _mapping_row_refs(row)
    expected = {
        "equipment": {
            "equipment_id": "DEV-001", "equipment_name": "设备 A", "customer_id": "ORG-001",
        },
        "contract_equipment": {
            "equipment_id": "DEV-001", "contract_id": "HT-MNT-2026-001",
            "valid_from": "2026-09-01", "valid_to": "2026-12-31",
            "mapping_basis": "explicit_equipment_schedule",
        },
        "contracts": {
            "contract_id": "HT-MNT-2026-001", "customer_id": "ORG-001",
            "effective_from": "2026-09-01", "effective_to": "2026-12-31", "status": "active",
        },
    }
    for ref in refs:
        labels = {}
        for item in ref.snippet.rsplit(" [", 1)[1][:-1].split("; "):
            column, value = item.split("=", 1)
            labels[column] = json.loads(value)
        assert set(labels) == set(ref.column.split("/"))
        assert labels == expected[ref.table]


def test_mapping_failure_reason_codes_are_row_derived_and_allowlisted() -> None:
    facts = {
        "work_order_facts": FactRecord(
            fact_id="work", task_id="work_order_facts",
            value={"equipment_id": "DEV-001", "work_order_date": "2026-09-18"},
            state="supported",
        )
    }
    row = {
        "equipment_id": "DEV-001", "equipment_customer_id": "ORG-001",
        "contract_id": "HT-001", "mapping_valid_from": "2026-09-01",
        "mapping_valid_to": "2026-12-31", "mapping_basis": "explicit_equipment_schedule",
        "contract_customer_id": "ORG-001", "contract_effective_from": "2026-09-01",
        "contract_effective_to": "2026-12-31", "contract_status": "active",
    }
    result = _joined_mapping_result()
    assert _mapping_stop_reason([row], result, facts) is None
    assert _mapping_stop_reason([dict(row, equipment_customer_id="ORG-002")], result, facts) == "customer_scope_conflict"
    assert _mapping_stop_reason([dict(row, mapping_basis="equipment_name_guess")], result, facts) == "mapping_basis_unapproved"
    assert _mapping_stop_reason([dict(row, mapping_valid_to="2026-09-17")], result, facts) == "validity_window_not_cover"
    assert _mapping_stop_reason([dict(row, contract_id="HT-002"), row], result, facts) == "ambiguous_active_contracts"
    assert _mapping_stop_reason([dict(row, contract_status="suspended")], result, facts) == "contract_status_inactive"
    # A zero-row result is not promoted to a stop without an executor-verified absence check.
    assert _mapping_stop_reason([], result, facts) is None

    zero_row_result = result.model_copy(update={"trace": [result.trace[0].model_copy(update={
        "output": {"sql": result.trace[0].output["sql"], "row_count": 0}
    })]})
    value, state, absence_refs = _validate_mapping([], [], zero_row_result, {}, facts)
    assert state == "insufficient"
    assert len(absence_refs) == 1
    assert absence_refs[0].table == "contract_equipment"
    assert absence_refs[0].row_pk is None
    assert _mapping_stop_reason([], zero_row_result, facts) == "mapping_unresolved"
    transformed = _mapping_absence_trace(
        zero_row_result, FactDecision(value, state, tuple(absence_refs), "mapping_unresolved")
    )
    assert transformed.refs == list(absence_refs)
    assert transformed.trace[-1].tool == "cross_source_absence_check"
    assert transformed.trace[-1].hit == list(absence_refs)


def test_unapproved_mapping_basis_records_policy_version_and_answer_detail() -> None:
    ref = SqlRef(
        kind="sql", table="contract_equipment", column="mapping_basis",
        row_pk="HT-001|DEV-001",
        snippet="设备 DEV-001 映射依据 equipment_name_guess。",
    )
    result = ToolResult(data={"rows": []}, trace=[], refs=[ref])
    decision = FactDecision(
        "没有唯一满足依据的合同映射。", "insufficient", (ref,),
        "mapping_basis_unapproved",
    )
    transformed = _mapping_absence_trace(result, decision)
    policy_step = next(
        step for step in transformed.trace if step.tool == "cross_source_policy_check"
    )
    assert policy_step.input["policy_id"] == "cross_source_mapping_basis"
    assert policy_step.input["version"] == "2026-10-06.v1"
    assert policy_step.output["observations"] == [{
        "mapping_basis": "equipment_name_guess", "status": "undefined",
    }]
    assert policy_step.hit == [ref]

    plan = build_work_order_sla_plan("WO-2026-0001")
    answer = Orchestrator._fact_plan_answer(
        plan,
        FactPlanRun(
            trace=[policy_step], stop_reason_code="mapping_basis_unapproved",
        ),
    )
    assert "equipment_name_guess" in answer
    assert "cross_source_mapping_basis v2026-10-06.v1" in answer


def test_event_absence_requires_exact_executed_sql_and_zero_rows() -> None:
    sql = (
        "SELECT work_order_id, event_type, event_at "
        "FROM cross_source_demo.work_order_events "
        "WHERE work_order_id = 'WO-2026-7001' AND event_type = 'accepted'"
    )
    assert _event_absence_filters_match(sql, "WO-2026-7001", "accepted")
    assert _event_absence_filters_match(
        "SELECT e.work_order_id, e.event_type FROM work_order_events AS e "
        "WHERE (e.work_order_id = 'WO-2026-7001' AND e.event_type = 'accepted') LIMIT 1000",
        "WO-2026-7001", "accepted",
    )
    assert not _event_absence_filters_match(
        sql + " LIMIT 0", "WO-2026-7001", "accepted",
    )
    assert not _event_absence_filters_match(
        sql + " OFFSET 1", "WO-2026-7001", "accepted",
    )
    assert not _event_absence_filters_match(
        sql.replace("event_type = 'accepted'", "event_type = 'service_restored'"),
        "WO-2026-7001", "accepted",
    )
    assert not _event_absence_filters_match(
        sql.replace(" AND event_type = 'accepted'", " OR event_type = 'accepted'"),
        "WO-2026-7001", "accepted",
    )

    result = ToolResult(data={"rows": [], "row_count": 0}, trace=[TraceStep(
        step_id=1, tool="nl2sql", input={},
        output={"sql": sql, "row_count": 0}, elapsed_ms=1.0, hit=[],
    )], refs=[])
    checked, ref = _event_absence_trace(result, "WO-2026-7001", "accepted")
    assert ref is not None and ref.row_pk is None
    assert checked.trace[-1].tool == "cross_source_absence_check"
    assert checked.trace[-1].output["filters"] == {
        "work_order_id": "WO-2026-7001", "event_type": "accepted",
    }
    assert checked.trace[-1].output["row_count"] == 0

    nonzero = result.model_copy(update={"data": {"rows": [{"event_type": "accepted"}], "row_count": 1},
                                      "trace": [result.trace[0].model_copy(update={
                                          "output": {"sql": sql, "row_count": 1}
                                      })]})
    _, ref = _event_absence_trace(nonzero, "WO-2026-7001", "accepted")
    assert ref is None


def test_repair_duration_handler_runs_exact_absence_query_for_missing_event() -> None:
    work_order_id = "WO-2026-7001"
    found_ref = SqlRef(
        kind="sql", table="work_order_events", column="work_order_id/event_type/event_at",
        row_pk=f"work_order_id={work_order_id}&event_type=service_restored",
        snippet="service_restored",
    )
    base_sql = (
        "SELECT work_order_id, event_type, event_at "
        "FROM cross_source_demo.work_order_events "
        f"WHERE work_order_id = '{work_order_id}'"
    )
    absence_sql = (
        "SELECT work_order_id, event_type, event_at "
        "FROM cross_source_demo.work_order_events "
        f"WHERE work_order_id = '{work_order_id}' AND event_type = 'accepted' LIMIT 1000"
    )

    class FakeSqlTool:
        def __init__(self):
            self.payloads = []

        def run(self, payload):
            self.payloads.append(payload)
            if len(self.payloads) == 1:
                return ToolResult(data={"rows": [{
                    "work_order_id": work_order_id,
                    "event_type": "service_restored",
                    "event_at": "2026-09-18T10:00:00+08:00",
                }]}, trace=[TraceStep(
                    step_id=1, tool="nl2sql", input=payload,
                    output={"sql": base_sql, "row_count": 1}, elapsed_ms=1.0,
                    hit=[found_ref],
                )], refs=[found_ref])
            return ToolResult(data={"rows": []}, trace=[TraceStep(
                step_id=1, tool="nl2sql", input=payload,
                output={"sql": absence_sql, "row_count": 0}, elapsed_ms=1.0, hit=[],
            )], refs=[])

    fake = FakeSqlTool()
    output = _make_repair_duration_handler(fake, verification_enabled=True)(
        {"work_order_id": work_order_id, "question": "核验修复时限"}, {}
    )
    assert len(fake.payloads) == 2
    assert "event_type = 'accepted'" in fake.payloads[1]["question"]
    assert output.decision.state == "insufficient"
    assert output.decision.evidence[-1].row_pk is None
    assert output.tool_result.trace[-1].tool == "cross_source_absence_check"
    assert output.tool_result.trace[-1].output["row_count"] == 0


def test_scoped_rag_adapter_records_actual_hybrid_retrieval_attempt() -> None:
    ref = DocRef(
        kind="doc", doc_id="HT-001", snippet="| 修复完成 | service_restored | 8小时内 |",
        section_path="服务级别", char_start=0, char_end=40,
    )

    class Chunk:
        text = ref.snippet

    class Candidate:
        chunk = Chunk()

    class FakeRag:
        def retrieve_and_rank(self, _question, *, doc_id):
            assert doc_id == "HT-001"
            return {"context_hits": [(Candidate(), ref)], "num_hits": 1, "retrieve_ms": 3.5}

        def retrieve_steps(self, _retrieved, *, step_id, doc_id):
            return [TraceStep(
                step_id=step_id, tool="retrieve", input={"doc_id": doc_id},
                output={"num_hits": 1}, elapsed_ms=3.5, hit=[ref],
            )]

        def generate(self, _question, _context_hits):
            return {"text": "8小时", "final_refs": [ref]}

        def generate_step(self, _question, _generated, *, step_id):
            return TraceStep(
                step_id=step_id, tool="generate", input={}, output={}, elapsed_ms=1.0, hit=[ref]
            )

    tool = _ScopedRagDocSearchTool(lambda: FakeRag())
    result = tool.run({"question": "HT-001 修复时限", "doc_id": "HT-001"})
    retrieval = next(step for step in result.trace if step.tool == "cross_source_doc_retrieval")
    assert retrieval.output["attempts"] == [{
        "attempt": 1, "strategy": "hybrid_bm25_vector",
        "status": "candidates_retrieved", "candidate_count": 1,
        "elapsed_ms": 3.5,
    }]
    assert retrieval.hit == []
    assert result.refs == [ref]


def test_scoped_rag_adapter_allows_at_most_one_targeted_clause_retry() -> None:
    first_ref = DocRef(kind="doc", doc_id="HT-002", snippet="适用范围：设备 EQ-002。")
    second_ref = DocRef(kind="doc", doc_id="HT-002", snippet="| 修复完成 | service_restored | 4小时内 |")

    class Chunk:
        def __init__(self, text):
            self.text = text

    class Candidate:
        def __init__(self, ref):
            self.chunk = Chunk(ref.snippet)

    class FakeRag:
        def __init__(self):
            self.calls = []

        def retrieve_and_rank(self, question, *, doc_id):
            self.calls.append(question)
            ref = first_ref if len(self.calls) == 1 else second_ref
            return {"context_hits": [(Candidate(ref), ref)], "num_hits": 1, "retrieve_ms": float(len(self.calls))}

        def retrieve_steps(self, _retrieved, *, step_id, doc_id):
            return [TraceStep(step_id=step_id, tool="retrieve", input={}, output={}, elapsed_ms=1.0, hit=[])]

        def generate(self, _question, context_hits):
            assert context_hits[0][1].snippet == second_ref.snippet
            return {"text": "4小时", "final_refs": [second_ref]}

        def generate_step(self, _question, _generated, *, step_id):
            return TraceStep(step_id=step_id, tool="generate", input={}, output={}, elapsed_ms=1.0, hit=[second_ref])

    fake = FakeRag()
    result = _ScopedRagDocSearchTool(lambda: fake).run({"question": "HT-002 修复时限", "doc_id": "HT-002"})
    retrieval = next(step for step in result.trace if step.tool == "cross_source_doc_retrieval")
    assert [item["strategy"] for item in retrieval.output["attempts"]] == [
        "hybrid_bm25_vector", "targeted_contract_clause_retry"
    ]
    assert [item["status"] for item in retrieval.output["attempts"]] == [
        "required_metric_not_found", "required_metric_found"
    ]
    assert len(fake.calls) == 2


def test_matched_ablation_selects_first_mapping_without_applicability_checks() -> None:
    facts = {
        "work_order_facts": FactRecord(
            fact_id="work", task_id="work_order_facts",
            value={"equipment_id": "DEV-001", "work_order_date": "2026-09-18"},
            state="supported",
        )
    }
    rows = [{
        "equipment_id": "DEV-001", "equipment_customer_id": "ORG-OTHER",
        "contract_id": "HT-EXPIRED", "mapping_valid_from": "2025-01-01",
        "mapping_valid_to": "2025-12-31", "mapping_basis": "equipment_name_guess",
        "contract_customer_id": "ORG-MISMATCH", "contract_effective_from": "2024-01-01",
        "contract_effective_to": "2025-12-31", "contract_status": "suspended",
    }]
    refs = [
        SqlRef(kind="sql", table=table, column="*", row_pk="1", snippet="candidate")
        for table in ("equipment", "contract_equipment", "contracts")
    ]
    value, state, evidence = _baseline_mapping(rows, refs, _joined_mapping_result(), {}, facts)
    assert state == "supported"
    assert value["contract_id"] == "HT-EXPIRED"
    assert len(evidence) == 3


def test_matched_ablation_keeps_partial_real_sql_refs_instead_of_false_early_stop() -> None:
    facts = {
        "work_order_facts": FactRecord(
            fact_id="work", task_id="work_order_facts",
            value={"equipment_id": "DEV-001", "work_order_date": "2026-09-18"},
            state="supported",
        )
    }
    rows = [{
        "equipment_id": "DEV-001", "equipment_customer_id": "ORG-001",
        "contract_id": "HT-001", "mapping_valid_from": "2026-01-01",
        "mapping_valid_to": "2026-12-31", "mapping_basis": "field_email_note",
        "contract_customer_id": "ORG-999", "contract_effective_from": "2026-01-01",
        "contract_effective_to": "2026-12-31", "contract_status": "suspended",
    }]
    refs = [SqlRef(kind="sql", table="contract_equipment", column="contract_id",
                   row_pk="HT-001|DEV-001", snippet="实际 SQL 行级引用")]
    value, state, evidence = _baseline_mapping(rows, refs, _joined_mapping_result(), {}, facts)
    assert state == "supported"
    assert value["contract_id"] == "HT-001"
    assert evidence == refs


def test_matched_ablation_uses_first_clause_while_strict_marks_conflict_ambiguous() -> None:
    doc_ref = DocRef(
        kind="doc", doc_id="HT-MNT-2026-001", page=1,
        snippet=(
            "本合同适用于设备编号 DEV-001。\n"
            "service_restored 时间减 accepted 时间计算修复耗时。\n"
            "| 修复完成 | service_restored | 8 小时内 |\n"
            "| 修复完成 | service_restored | 4 小时内 |"
        ),
    )
    result = ToolResult(data="检索到合同条款。", trace=[], refs=[doc_ref])
    mapping = FactRecord(
        "mapping", "controlled_contract_mapping",
        {"contract_id": "HT-MNT-2026-001", "equipment_id": "DEV-001"}, "supported",
    )
    facts = {"controlled_contract_mapping": mapping}
    context = {"question": "工单是否按修复时限完成？"}
    assert _validate_contract_document(result, context, facts).state == "ambiguous"
    baseline = _baseline_contract_document(result, context, facts)
    assert baseline.state == "supported"
    assert baseline.value["limit_hours"] == 8


def test_matched_ablation_parses_first_response_descriptor_table() -> None:
    doc_ref = DocRef(
        kind="doc", doc_id="HT-001", page=1,
        snippet=(
            "本合同适用于设备编号 DEV-001。\n"
            "| 修复完成 | service_restored | 8 小时内 |\n"
            "| 首次响应 | first_response | 30 分钟内 |"
        ),
    )
    result = ToolResult(data="检索到合同条款。", trace=[], refs=[doc_ref])
    mapping = FactRecord(
        "mapping", "controlled_contract_mapping",
        {"contract_id": "HT-001", "equipment_id": "DEV-001"}, "supported",
    )
    decision = _baseline_contract_document(
        result,
        {"question": "请同时报告首次响应和修复完成耗时。"},
        {"controlled_contract_mapping": mapping},
    )
    assert decision.state == "supported"
    assert decision.value["first_response_limit_minutes"] == 30


def test_matched_ablation_computes_negative_duration_where_strict_stops() -> None:
    rows = [
        {"work_order_id": "WO-2026-0001", "event_type": "accepted", "event_at": "2026-09-18T09:00:00+08:00"},
        {"work_order_id": "WO-2026-0001", "event_type": "service_restored", "event_at": "2026-09-18T08:00:00+08:00"},
    ]
    refs = [
        SqlRef(kind="sql", table="work_order_events", column="event_type",
               row_pk=f"work_order_id={row['work_order_id']}&event_type={row['event_type']}",
               snippet=row["event_type"])
        for row in rows
    ]
    context = {"work_order_id": "WO-2026-0001"}
    baseline_value, baseline_state, _ = _baseline_duration(rows, refs, None, context, {})
    _strict_value, strict_state, _ = _validate_duration(rows, refs, None, context, {})
    assert baseline_state == "supported"
    assert baseline_value["duration_seconds"] == -3600
    assert strict_state == "contradicted"


def test_strict_and_ablation_share_valid_case_tool_payloads_and_rag_candidates() -> None:
    class SqlTool:
        def __init__(self):
            self.payloads = []

        def run(self, payload):
            self.payloads.append(payload)
            question = payload["question"]
            if "查询工单编号" in question:
                rows = [{
                    "work_order_id": "WO-2026-7001", "equipment_id": "DEV-001",
                    "opened_at": "2026-09-18T09:00:00+08:00",
                }]
                ref = SqlRef(kind="sql", table="work_orders", column="*", row_pk="WO-2026-7001", snippet="WO-2026-7001 DEV-001")
                sql = "SELECT work_order_id, equipment_id, opened_at FROM work_orders WHERE work_order_id = 'WO-2026-7001'"
            elif "查询设备编号" in question:
                rows = [{
                    "equipment_id": "DEV-001", "equipment_customer_id": "ORG-001",
                    "contract_id": "HT-001", "mapping_valid_from": "2026-01-01",
                    "mapping_valid_to": "2026-12-31", "mapping_basis": "explicit_equipment_schedule",
                    "contract_customer_id": "ORG-001", "contract_effective_from": "2026-01-01",
                    "contract_effective_to": "2026-12-31", "contract_status": "active",
                }]
                refs = [SqlRef(kind="sql", table=t, column="*", row_pk="1", snippet="row") for t in ("equipment", "contract_equipment", "contracts")]
                sql = _joined_mapping_result().trace[0].output["sql"].replace("DEV-001", "DEV-001").replace("cross_source_demo.", "")
                return ToolResult(
                    data={"rows": rows},
                    trace=[TraceStep(step_id=1, tool="nl2sql", input=payload, output={"sql": sql, "row_count": 1}, elapsed_ms=1, hit=refs)],
                    refs=refs,
                )
            else:
                rows = [
                    {"work_order_id": "WO-2026-7001", "event_type": "accepted", "event_at": "2026-09-18T09:00:00+08:00"},
                    {"work_order_id": "WO-2026-7001", "event_type": "service_restored", "event_at": "2026-09-18T10:00:00+08:00"},
                ]
                refs = [SqlRef(kind="sql", table="work_order_events", column="event_type", row_pk=f"work_order_id={row['work_order_id']}&event_type={row['event_type']}", snippet=f"{row['event_type']} {row['event_at']}") for row in rows]
                sql = "SELECT work_order_id, event_type, event_at FROM work_order_events WHERE work_order_id = 'WO-2026-7001'"
                return ToolResult(data={"rows": rows}, trace=[TraceStep(step_id=1, tool="nl2sql", input=payload, output={"sql": sql, "row_count": len(rows)}, elapsed_ms=1, hit=refs)], refs=refs)
            return ToolResult(data={"rows": rows}, trace=[TraceStep(step_id=1, tool="nl2sql", input=payload, output={"sql": sql, "row_count": len(rows)}, elapsed_ms=1, hit=[ref])], refs=[ref])

    class DocTool:
        def __init__(self):
            self.payloads = []

        def run(self, payload):
            self.payloads.append(payload)
            ref = DocRef(
                kind="doc", doc_id="HT-001", page=1,
                snippet=(
                    "本合同适用于设备编号 DEV-001。修复完成耗时按 service_restored 时间减 accepted 时间计算。\n"
                    "| 修复完成 | `service_restored`（服务恢复） | 8 小时内 |"
                ),
            )
            return ToolResult(
                data="检索到合同条款。",
                trace=[TraceStep(step_id=1, tool="rerank", input=payload, output={}, elapsed_ms=1, hit=[ref])],
                refs=[ref],
            )

    paired = []
    for verification_enabled in (True, False):
        sql_tool, doc_tool = SqlTool(), DocTool()
        executor = _handlers_for_tools(sql_tool, doc_tool, verification_enabled=verification_enabled)
        run = executor.run(
                build_work_order_sla_plan("WO-2026-7001", include_first_response=True),
                {"work_order_id": "WO-2026-7001", "question": "工单 WO-2026-7001 的首次响应和修复是否满足时限？"},
        )
        paired.append((sql_tool.payloads, doc_tool.payloads, run))

    strict_sql, strict_doc, strict_run = paired[0]
    ablated_sql, ablated_doc, ablated_run = paired[1]
    assert strict_sql == ablated_sql
    assert strict_doc == ablated_doc, {
        "strict_records": {key: (record.state, record.value) for key, record in paired[0][2].records.items()},
        "strict_not_run": paired[0][2].skipped,
        "strict_trace": [(step.tool, step.output) for step in paired[0][2].trace],
        "strict_sql": strict_sql,
    }
    assert strict_run.records["contract_repair_limit"].state == "supported", (
        strict_doc, strict_run.records["contract_repair_limit"].value,
        strict_run.skipped,
    )
    assert strict_run.records["sla_verdict"].state == "supported", {
        key: (record.state, record.value) for key, record in strict_run.records.items()
    }
    assert "首次响应无法判断" in strict_run.records["sla_verdict"].value
    assert "首次响应无法判断" in ablated_run.records["sla_verdict"].value
    assert ablated_run.records["sla_verdict"].state == "supported", {
        key: record.state for key, record in ablated_run.records.items()
    }
    final_alignment = strict_run.trace[-1].output
    assert final_alignment["metric_checks"] == [{
        "metric": "service_restored",
        "start_event": "accepted",
        "end_event": "service_restored",
        "elapsed_seconds": 3600.0,
        "limit": 8,
        "unit": "hours",
        "within_limit": True,
    }]


def test_strict_stops_before_contract_retrieval_for_missing_or_reversed_events() -> None:
    work_order_id = "WO-2026-7010"

    class SqlTool:
        def __init__(self, scenario):
            self.scenario = scenario
            self.payloads = []

        def run(self, payload):
            self.payloads.append(payload)
            question = payload["question"]
            if "查询工单编号" in question:
                rows = [{
                    "work_order_id": work_order_id, "equipment_id": "DEV-010",
                    "opened_at": "2026-09-18T09:00:00+08:00",
                }]
                sql = f"SELECT work_order_id, equipment_id, opened_at FROM work_orders WHERE work_order_id = '{work_order_id}'"
                refs = [SqlRef(kind="sql", table="work_orders", column="*", row_pk=work_order_id, snippet=work_order_id)]
            elif "查询设备编号" in question:
                rows = [{
                    "equipment_id": "DEV-010", "equipment_customer_id": "ORG-010",
                    "contract_id": "HT-010", "mapping_valid_from": "2026-01-01",
                    "mapping_valid_to": "2026-12-31", "mapping_basis": "explicit_equipment_schedule",
                    "contract_customer_id": "ORG-010", "contract_effective_from": "2026-01-01",
                    "contract_effective_to": "2026-12-31", "contract_status": "active",
                }]
                sql = _joined_mapping_result().trace[0].output["sql"].replace("DEV-001", "DEV-010").replace("cross_source_demo.", "")
                refs = [SqlRef(kind="sql", table=table, column="*", row_pk="1", snippet="row")
                        for table in ("equipment", "contract_equipment", "contracts")]
            elif "且 event_type = '" in question:
                event_type = question.split("且 event_type = '", 1)[1].split("'", 1)[0]
                rows, refs = [], []
                sql = (
                    "SELECT work_order_id, event_type, event_at FROM work_order_events "
                    f"WHERE work_order_id = '{work_order_id}' AND event_type = '{event_type}'"
                )
            else:
                rows = [{
                    "work_order_id": work_order_id, "event_type": "accepted",
                    "event_at": "2026-09-18T09:00:00+08:00",
                }]
                if self.scenario == "reversed":
                    rows.append({
                        "work_order_id": work_order_id, "event_type": "service_restored",
                        "event_at": "2026-09-18T08:00:00+08:00",
                    })
                refs = [SqlRef(
                    kind="sql", table="work_order_events", column="event_type",
                    row_pk=f"work_order_id={row['work_order_id']}&event_type={row['event_type']}",
                    snippet=f"{row['event_type']} {row['event_at']}",
                ) for row in rows]
                sql = f"SELECT work_order_id, event_type, event_at FROM work_order_events WHERE work_order_id = '{work_order_id}'"
            return ToolResult(
                data={"rows": rows},
                trace=[TraceStep(step_id=1, tool="nl2sql", input=payload,
                                 output={"sql": sql, "row_count": len(rows)}, elapsed_ms=1, hit=refs)],
                refs=refs,
            )

    class DocTool:
        def __init__(self):
            self.payloads = []

        def run(self, payload):
            self.payloads.append(payload)
            ref = DocRef(
                kind="doc", doc_id="HT-010", page=1,
                snippet=("本合同适用于设备编号 DEV-010。修复耗时按 service_restored 时间减 accepted 时间。\n"
                         "| 修复完成 | `service_restored` | 8 小时内 |"),
            )
            return ToolResult(
                data="检索到合同条款。",
                trace=[TraceStep(step_id=1, tool="rerank", input=payload, output={}, elapsed_ms=1, hit=[ref])],
                refs=[ref],
            )

    for scenario in ("missing", "reversed"):
        runs = {}
        for verification_enabled in (True, False):
            sql, doc = SqlTool(scenario), DocTool()
            executor = _handlers_for_tools(sql, doc, verification_enabled=verification_enabled)
            run = executor.run(
                build_work_order_sla_plan(work_order_id),
                {"work_order_id": work_order_id, "question": f"工单 {work_order_id} 是否满足修复时限？"},
            )
            runs[verification_enabled] = (sql, doc, run)

        strict_sql, strict_doc, strict_run = runs[True]
        ablated_sql, ablated_doc, ablated_run = runs[False]
        assert not strict_doc.payloads, scenario
        assert len(ablated_doc.payloads) == 1, scenario
        if scenario == "missing":
            assert strict_run.stop_reason_code == "key_event_missing"
            assert any(step.tool == "cross_source_absence_check" for step in strict_run.trace)
        else:
            assert strict_run.stop_reason_code == "event_time_reversed"
        assert ablated_doc.payloads, scenario


def test_ablation_missing_mapping_records_insufficient_without_downstream_typeerror() -> None:
    class SqlTool:
        def run(self, payload):
            question = payload["question"]
            if "work_order_id" in question and "work_order_events" not in question:
                rows = [{
                    "work_order_id": "WO-2026-7002",
                    "equipment_id": "DEV-MISSING",
                    "opened_at": "2026-09-18T09:00:00+08:00",
                }]
                refs = [SqlRef(kind="sql", table="work_orders", column="work_order_id",
                               row_pk="WO-2026-7002", snippet="work order")]
                sql = "SELECT work_order_id, equipment_id, opened_at FROM work_orders WHERE work_order_id = 'WO-2026-7002'"
            elif "设备编号" in question and "work_order_events" not in question:
                rows, refs = [], []
                sql = _joined_mapping_result().trace[0].output["sql"].replace(
                    "DEV-001", "DEV-MISSING"
                )
            else:
                rows = [
                    {"work_order_id": "WO-2026-7002", "event_type": "accepted",
                     "event_at": "2026-09-18T09:00:00+08:00"},
                    {"work_order_id": "WO-2026-7002", "event_type": "service_restored",
                     "event_at": "2026-09-18T10:00:00+08:00"},
                ]
                refs = [SqlRef(kind="sql", table="work_order_events", column="event_type",
                               row_pk=f"{row['event_type']}", snippet=row["event_type"])
                        for row in rows]
                sql = "SELECT work_order_id, event_type, event_at FROM work_order_events WHERE work_order_id = 'WO-2026-7002'"
            return ToolResult(
                data={"rows": rows},
                trace=[TraceStep(step_id=1, tool="nl2sql", input=payload,
                                 output={"sql": sql, "row_count": len(rows)}, elapsed_ms=1, hit=refs)],
                refs=refs,
            )

    class DocTool:
        def run(self, _payload):
            raise AssertionError("缺少映射时不应调用合同文档工具")

    plan = build_work_order_sla_plan("WO-2026-7002")
    context = {"work_order_id": "WO-2026-7002", "question": "请判断修复是否满足时限。"}
    for verification_enabled in (False, True):
        executor = _handlers_for_tools(
            SqlTool(), DocTool(), verification_enabled=verification_enabled
        )
        run = executor.run(plan, context)
        assert run.records["controlled_contract_mapping"].state == "insufficient"
        assert run.records["contract_repair_limit"].state == "insufficient"
        assert not any(step.tool == "fact_task_failure" for step in run.trace)
        assert not any(step.tool == "cross_source_doc_retrieval" for step in run.trace)

        absence = next(step for step in run.trace if step.tool == "cross_source_absence_check")
        assert absence.output == {
            "table": "contract_equipment",
            "filters": {"equipment_id": "DEV-MISSING"},
            "row_count": 0,
            "derived_from_executed_query": True,
        }
        assert absence.hit and absence.hit[0].column == "equipment_id"
        assert absence.hit[0].row_pk is None
        assert absence.hit[0] in run.refs

        doc_task = next(
            step for step in run.trace
            if step.tool == "fact_task_not_run"
            and step.output.get("task_id") == "contract_repair_limit"
        )
        assert doc_task.output["status"] == "not_run"
        assert doc_task.output["unmet_dependencies"] == (
            ["controlled_contract_mapping", "repair_duration"]
            if verification_enabled else ["controlled_contract_mapping"]
        )
        assert doc_task.output["reason_code"] == (
            "unsupported_dependencies" if verification_enabled
            else "missing_contract_mapping"
        )


def test_missing_first_response_does_not_block_supported_repair_metric() -> None:
    doc_ref = DocRef(
        kind="doc", doc_id="HT-MNT-2026-001", page=1,
        snippet=(
            "本合同适用于设备 DEV-001。修复耗时按 service_restored 时间减 accepted 时间。\n"
            "| 修复完成 | `service_restored` | 8 小时内 |"
        ),
    )
    mapping_value = {
        "contract_id": "HT-MNT-2026-001", "equipment_id": "DEV-001",
        "mapping_basis": "explicit_equipment_schedule",
    }
    mapping_ref = SqlRef(kind="sql", table="contract_equipment", column="contract_id", row_pk="HT-MNT-2026-001|DEV-001", snippet="DEV-001 映射到 HT-MNT-2026-001")
    clause = _validate_contract_document(
        ToolResult(data="找到修复条款。", trace=[], refs=[doc_ref]),
        {"question": "首次响应和修复两项是否达标？"},
        {"controlled_contract_mapping": FactRecord("m", "controlled_contract_mapping", mapping_value, "supported")},
    )
    assert clause.state == "supported"
    assert clause.value["first_response_limit_minutes"] is None

    event_rows = [
        {"work_order_id": "WO-2026-9001", "event_type": "accepted", "event_at": "2026-09-18T09:00:00+08:00"},
        {"work_order_id": "WO-2026-9001", "event_type": "service_restored", "event_at": "2026-09-18T10:00:00+08:00"},
    ]
    event_refs = [
        SqlRef(kind="sql", table="work_order_events", column="event_type",
               row_pk=f"work_order_id={row['work_order_id']}&event_type={row['event_type']}",
               snippet=f"{row['event_type']} {row['event_at']}")
        for row in event_rows
    ]
    duration_value, duration_state, duration_evidence = _validate_duration(
        event_rows, event_refs, ToolResult(data={}, trace=[], refs=[]),
        {"work_order_id": "WO-2026-9001", "question": "首次响应和修复两项是否达标？"}, {},
    )
    assert duration_state == "supported"
    assert duration_value["first_response_status"] == "insufficient"

    facts = {
        "controlled_contract_mapping": FactRecord("m", "controlled_contract_mapping", mapping_value, "supported", evidence_refs=(mapping_ref,)),
        "contract_repair_limit": FactRecord("c", "contract_repair_limit", clause.value, "supported", evidence_refs=clause.evidence),
        "repair_duration": FactRecord("d", "repair_duration", duration_value, "supported", evidence_refs=tuple(duration_evidence)),
        "first_response_duration": FactRecord("fr", "first_response_duration", "首次响应事件缺失或重复，不能计算首次响应耗时。", "insufficient"),
        "first_response_verdict": FactRecord("frv", "first_response_verdict", "首次响应条款或事件耗时未受支持，无法判断。", "insufficient"),
    }
    final = _make_verdict_handler()(
        {"question": "首次响应和修复两项是否达标？"}, facts
    )
    assert final.decision.state == "supported"
    assert "修复时限" in final.decision.value
    assert "首次响应无法判断" in final.decision.value


def test_natural_chinese_sla_rows_support_hours_and_minutes_without_outer_pipes() -> None:
    doc_ref = DocRef(
        kind="doc",
        doc_id="CT-NJ-714",
        page=1,
        snippet=(
            "适用范围 | 响应修复联合询问泵 | 本合同约定的设备维保服务\n"
                "首次响应 | 响应修复联合询问泵报修 | 1小时内\n"
                "修复完成 | 响应修复联合询问泵故障修复 | 4小时内"
        ),
    )
    mapping = FactRecord(
        "mapping",
        "controlled_contract_mapping",
        {"contract_id": "CT-NJ-714", "equipment_id": "EQ-P-114",
         "equipment_name": "响应修复联合询问泵"},
        "supported",
    )
    decision = _validate_contract_document(
        ToolResult(data="已检索合同原文。", trace=[], refs=[doc_ref]),
        {"question": "首次响应和修复是否满足时限？"},
        {"controlled_contract_mapping": mapping},
    )
    assert decision.state == "supported"
    assert decision.value["limit_hours"] == 4
    assert decision.value["first_response_limit_minutes"] == 60
    assert decision.value["first_response_clause_status"] == "supported"


def test_contract_prose_scope_and_sla_sentences_are_parsed_deterministically() -> None:
    doc_ref = DocRef(
        kind="doc",
        doc_id="AG-ICE-7301",
        page=1,
        snippet=(
            "## 适用范围\n"
            "本保障书适用于客户 CUST-GLACIER-01 的设备清册所列转运舱。"
            "设备 EQ-ICE-4701 双温区药品转运舱 CT-47 列入本书清册，服务期为 2026-01-01 至 2027-06-30。\n"
            "## 服务级别\n"
            "时长从工单受理事件 `accepted` 起算。首次响应以 `first_response` 为终点，目标为 12 分钟内；"
            "修复完成以 `service_restored` 为终点，目标为 3 小时内。"
        ),
    )
    mapping = FactRecord(
        "mapping",
        "controlled_contract_mapping",
        {"contract_id": "AG-ICE-7301", "equipment_id": "EQ-ICE-4701",
         "equipment_name": "双温区药品转运舱 CT-47"},
        "supported",
    )
    decision = _validate_contract_document(
        ToolResult(data="已检索合同原文。", trace=[], refs=[doc_ref]),
        {"question": "首次响应和修复是否满足时限？"},
        {"controlled_contract_mapping": mapping},
    )
    assert decision.state == "supported"
    assert decision.value["limit_hours"] == 3
    assert decision.value["first_response_limit_minutes"] == 12


def test_zero_mapping_trace_is_emitted_only_from_exact_executed_query_metadata() -> None:
    result = _joined_mapping_result()
    sql_step = result.trace[0].model_copy(update={
        "output": {"sql": result.trace[0].output["sql"], "row_count": 0}
    })
    result = result.model_copy(update={"trace": [sql_step]})
    transformed = _mapping_absence_trace(
        result, FactDecision("未找到设备编号对应的合同映射。", "insufficient")
    )
    assert transformed.trace[-1].tool == "cross_source_absence_check"
    assert transformed.trace[-1].output == {
        "table": "contract_equipment",
        "filters": {"equipment_id": "DEV-001"},
        "row_count": 0,
        "derived_from_executed_query": True,
    }

    nonzero = result.model_copy(update={"trace": [result.trace[0].model_copy(update={
        "output": {"sql": result.trace[0].output["sql"], "row_count": 1}
    })]})
    decision = FactDecision("未找到设备编号对应的合同映射。", "insufficient")
    assert _mapping_absence_trace(nonzero, decision) is nonzero
    unsupported_decision = FactDecision("没有唯一满足依据、客户和双有效期校验的合同映射。", "insufficient")
    assert _mapping_absence_trace(result, unsupported_decision) is result


def test_contract_clause_is_parsed_from_reranked_source_even_if_llm_rejects() -> None:
    doc_ref = DocRef(
        kind="doc",
        doc_id="HT-MNT-2026-001",
        snippet=(
            "本合同只适用于设备编号 DEV-001、DEV-002。\n"
            "修复完成耗时計算為 service_restored 时间减 accepted 时间。\n"
            "| 修复完成 | `service_restored`（服务恢复） | 8 小时内 |"
        ),
        page=1,
    )
    result = ToolResult(
        data="根据现有资料无法回答。",
        trace=[
            TraceStep(step_id=1, tool="rerank", input={}, output={}, elapsed_ms=1, hit=[doc_ref]),
            TraceStep(step_id=2, tool="llm_generate", input={}, output={"rejected": True}, elapsed_ms=1, hit=[doc_ref]),
        ],
        refs=[],
    )
    mapping = FactRecord(
        "mapping", "controlled_contract_mapping",
        {"contract_id": "HT-MNT-2026-001", "equipment_id": "DEV-001"},
        "supported",
    )

    decision = _validate_contract_document(result, {}, {"controlled_contract_mapping": mapping})
    transformed = _document_validation_trace(result, decision)

    assert decision.state == "supported"
    assert decision.value == {
        "limit_hours": 8,
        "first_response_limit_minutes": None,
        "first_response_clause_status": "insufficient",
        "contract_id": "HT-MNT-2026-001",
    }
    assert decision.evidence == (doc_ref,)
    assert transformed.trace[-1].tool == "cross_source_doc_validation"
    assert transformed.trace[-1].output["answer_text_used_as_fact"] is False


def test_contract_scope_matches_exact_master_equipment_name_without_internal_id() -> None:
    doc_ref = DocRef(
        kind="doc",
        doc_id="DH-C-8207",
        snippet=(
            "本协议适用于新港换热站一次网循环泵，供热单位 HEAT-07。\n"
            "| 修复完成 | 新港换热站一次网循环泵恢复运行 | 6小时内 |"
        ),
        page=1,
    )
    result = ToolResult(
        data="合同正文已检索。",
        trace=[TraceStep(step_id=1, tool="rerank", input={}, output={}, elapsed_ms=1, hit=[doc_ref])],
        refs=[doc_ref],
    )
    mapping = FactRecord(
        "mapping",
        "controlled_contract_mapping",
        {
            "contract_id": "DH-C-8207",
            "equipment_id": "HT-8207",
            "equipment_name": "新港换热站一次网循环泵",
        },
        "supported",
    )

    decision = _validate_contract_document(result, {}, {"controlled_contract_mapping": mapping})

    assert decision.state == "supported"
    assert decision.value["limit_hours"] == 6
    assert decision.evidence == (doc_ref,)


def test_verified_mapping_does_not_require_document_to_repeat_equipment_identifier() -> None:
    doc_ref = DocRef(
        kind="doc",
        doc_id="HT-NEV-401",
        snippet=(
            "本合同提供充电网络设备维护服务，所有适用设备均执行以下服务时限。\n"
            "| 修复完成 | service_restored | 6小时内 |"
        ),
        page=1,
    )
    result = ToolResult(
        data="合同正文已检索。",
        trace=[TraceStep(step_id=1, tool="rerank", input={}, output={}, elapsed_ms=1, hit=[doc_ref])],
        refs=[doc_ref],
    )
    mapping = FactRecord(
        "mapping",
        "controlled_contract_mapping",
        {
            "contract_id": "HT-NEV-401",
            "equipment_id": "DEV-NEV-401",
            "mapping_relation_verified": True,
        },
        "supported",
    )

    decision = _validate_contract_document(result, {}, {"controlled_contract_mapping": mapping})

    assert decision.state == "supported"
    assert decision.value["limit_hours"] == 6


def test_verified_mapping_still_stops_on_explicit_document_exclusion() -> None:
    doc_ref = DocRef(
        kind="doc",
        doc_id="HT-NEV-401",
        snippet=(
            "本合同不适用于设备 DEV-NEV-401。\n"
            "| 修复完成 | service_restored | 6小时内 |"
        ),
        page=1,
    )
    result = ToolResult(
        data="合同正文已检索。",
        trace=[TraceStep(step_id=1, tool="rerank", input={}, output={}, elapsed_ms=1, hit=[doc_ref])],
        refs=[doc_ref],
    )
    mapping = FactRecord(
        "mapping",
        "controlled_contract_mapping",
        {
            "contract_id": "HT-NEV-401",
            "equipment_id": "DEV-NEV-401",
            "mapping_relation_verified": True,
        },
        "supported",
    )

    decision = _validate_contract_document(result, {}, {"controlled_contract_mapping": mapping})

    assert decision.state == "insufficient"
    assert "明确排除" in decision.value


def test_contract_scope_does_not_accept_near_match_to_equipment_name() -> None:
    doc_ref = DocRef(
        kind="doc",
        doc_id="DH-C-8207",
        snippet=(
            "本协议适用于新港换热站二次网循环泵，供热单位 HEAT-07。\n"
            "| 修复完成 | 恢复热力站运行 | 6小时内 |"
        ),
        page=1,
    )
    result = ToolResult(
        data="合同正文已检索。",
        trace=[TraceStep(step_id=1, tool="rerank", input={}, output={}, elapsed_ms=1, hit=[doc_ref])],
        refs=[doc_ref],
    )
    mapping = FactRecord(
        "mapping",
        "controlled_contract_mapping",
        {
            "contract_id": "DH-C-8207",
            "equipment_id": "HT-8207",
            "equipment_name": "新港换热站一次网循环泵",
        },
        "supported",
    )

    decision = _validate_contract_document(result, {}, {"controlled_contract_mapping": mapping})

    assert decision.state == "insufficient"
    assert decision.evidence == (doc_ref,)


def test_contract_scope_does_not_accept_equipment_name_outside_scope_clause() -> None:
    doc_ref = DocRef(
        kind="doc",
        doc_id="DH-C-8207",
        snippet=(
            "背景：新港换热站一次网循环泵曾由其他协议维护。\n"
            "本协议适用于南区二次网增压泵。\n"
            "| 修复完成 | 恢复热力站运行 | 6小时内 |"
        ),
        page=1,
    )
    result = ToolResult(
        data="合同正文已检索。",
        trace=[TraceStep(step_id=1, tool="rerank", input={}, output={}, elapsed_ms=1, hit=[doc_ref])],
        refs=[doc_ref],
    )
    mapping = FactRecord(
        "mapping",
        "controlled_contract_mapping",
        {
            "contract_id": "DH-C-8207",
            "equipment_id": "HT-8207",
            "equipment_name": "新港换热站一次网循环泵",
        },
        "supported",
    )

    decision = _validate_contract_document(result, {}, {"controlled_contract_mapping": mapping})

    assert decision.state == "insufficient"
    assert decision.evidence == (doc_ref,)


def test_contract_scope_rejects_internal_id_only_outside_scope_sentence() -> None:
    doc_ref = DocRef(
        kind="doc",
        doc_id="DH-C-8207",
        snippet=(
            "附件提到设备编号 HT-8207，但该信息不构成适用范围。\n"
            "本协议适用于南区二次网增压泵。\n"
            "| 修复完成 | 恢复热力站运行 | 6小时内 |"
        ),
        page=1,
    )
    result = ToolResult(
        data="合同正文已检索。",
        trace=[TraceStep(step_id=1, tool="rerank", input={}, output={}, elapsed_ms=1, hit=[doc_ref])],
        refs=[doc_ref],
    )
    mapping = FactRecord(
        "mapping",
        "controlled_contract_mapping",
        {"contract_id": "DH-C-8207", "equipment_id": "HT-8207"},
        "supported",
    )

    decision = _validate_contract_document(result, {}, {"controlled_contract_mapping": mapping})

    assert decision.state == "insufficient"


def test_contract_scope_conflicting_exclusion_fails_closed() -> None:
    doc_ref = DocRef(
        kind="doc",
        doc_id="DH-C-8207",
        snippet=(
            "本协议适用于新港换热站一次网循环泵，供水单位 WU-07。\n"
            "特别约定：本协议不适用于新港换热站一次网循环泵。\n"
            "| 修复完成 | 恢复热力站运行 | 6小时内 |"
        ),
        page=1,
    )
    result = ToolResult(
        data="合同正文已检索。",
        trace=[TraceStep(step_id=1, tool="rerank", input={}, output={}, elapsed_ms=1, hit=[doc_ref])],
        refs=[doc_ref],
    )
    mapping = FactRecord(
        "mapping",
        "controlled_contract_mapping",
        {
            "contract_id": "DH-C-8207",
            "equipment_id": "HT-8207",
            "equipment_name": "新港换热站一次网循环泵",
        },
        "supported",
    )

    decision = _validate_contract_document(result, {}, {"controlled_contract_mapping": mapping})

    assert decision.state == "insufficient"
    assert decision.evidence == (doc_ref,)


def test_contract_scope_rejects_non_constitutive_scope_mention() -> None:
    assert not _contract_scope_matches_equipment(
        "适用范围：设备编号 HT-8207，但该信息不构成适用范围。",
        "HT-8207",
        "",
    )


def test_contract_scope_name_is_exact_and_confined_to_its_clause() -> None:
    name = "新港换热站一次网循环泵"
    assert not _contract_scope_matches_equipment(f"本合同适用于旧{name}。", "HT-8207", name)
    assert not _contract_scope_matches_equipment(
        f"本合同适用于南区水泵；另一协议适用于{name}。", "HT-8207", name
    )
    assert not _contract_scope_matches_equipment(
        f"本合同并非适用于{name}。", "HT-8207", name
    )
    assert not _contract_scope_matches_equipment(
        "本合同不属于适用范围：设备编号 HT-8207", "HT-8207", ""
    )
    assert not _contract_scope_matches_equipment(
        "本合同非适用范围：设备编号 HT-8207", "HT-8207", ""
    )
    assert not _contract_scope_matches_equipment(
        "本合同并非属于适用范围：设备编号 HT-8207", "HT-8207", ""
    )
    assert not _contract_scope_matches_equipment(
        "本合同不纳入适用范围：设备编号 HT-8207", "HT-8207", ""
    )
    assert not _contract_scope_matches_equipment(
        "本合同适用于南区泵，设备编号 HT-8207。", "HT-8207", ""
    )
    assert not _contract_scope_matches_equipment(
        f"本合同适用于南区泵，另有设备清单列出{name}。", "HT-8207", name
    )


def test_negated_unspecified_phrase_is_not_reported_as_explicit_no_limit() -> None:
    doc_ref = DocRef(
        kind="doc",
        doc_id="DH-C-8210",
        snippet=(
            "本协议适用于文苑换热站循环泵。\n"
            "本协议并非未约定服务恢复总时限，具体时限以附件为准。"
        ),
        page=1,
    )
    result = ToolResult(
        data="合同正文已检索。",
        trace=[TraceStep(step_id=1, tool="rerank", input={}, output={}, elapsed_ms=1, hit=[doc_ref])],
        refs=[doc_ref],
    )
    mapping = FactRecord(
        "mapping",
        "controlled_contract_mapping",
        {
            "contract_id": "DH-C-8210",
            "equipment_id": "HT-8210",
            "equipment_name": "文苑换热站循环泵",
        },
        "supported",
    )

    decision = _validate_contract_document(result, {}, {"controlled_contract_mapping": mapping})

    assert decision.state == "insufficient"
    assert decision.value == "合同原文片段未显示修复完成时限。"
    assert decision.diagnostic_code == "document_sla_unparsed"


def test_explicit_no_limit_phrase_is_preserved_with_document_evidence() -> None:
    doc_ref = DocRef(
        kind="doc",
        doc_id="DH-C-8210",
        snippet=(
            "本协议适用于文苑换热站循环泵。\n"
            "本协议修复完成总时限未约定。"
        ),
        page=1,
    )
    result = ToolResult(
        data="合同正文已检索。",
        trace=[TraceStep(step_id=1, tool="rerank", input={}, output={}, elapsed_ms=1, hit=[doc_ref])],
        refs=[doc_ref],
    )
    mapping = FactRecord(
        "mapping",
        "controlled_contract_mapping",
        {
            "contract_id": "DH-C-8210",
            "equipment_id": "HT-8210",
            "equipment_name": "文苑换热站循环泵",
        },
        "supported",
    )

    decision = _validate_contract_document(result, {}, {"controlled_contract_mapping": mapping})

    assert decision.state == "insufficient"
    assert decision.value == "合同原文明确未约定修复完成时限。"
    assert decision.diagnostic_code == "document_sla_unstated"
    assert decision.evidence == (doc_ref,)


def test_plain_repair_sla_unspecified_wording_is_recognized_without_double_negation() -> None:
    assert _explicitly_unspecified("本合同未约定泵站压力表故障修复时限。")
    assert not _explicitly_unspecified("本合同并非未约定泵站压力表故障修复时限。")


def test_contract_override_language_fails_closed_instead_of_using_one_threshold() -> None:
    doc_ref = DocRef(
        kind="doc", doc_id="HT-MNT-2026-001", page=1,
        snippet=(
            "本合同适用于设备 A。\n"
            "发生重大故障时按重大故障标准执行；特别约定优先于本条，设备 A 的修复完成时限以补充协议为准。\n"
            "| 修复完成 | service_restored | 8 小时内 |"
        ),
    )
    result = ToolResult(
        data="已检索合同片段。",
        trace=[TraceStep(step_id=1, tool="rerank", input={}, output={}, elapsed_ms=1, hit=[doc_ref])],
        refs=[doc_ref],
    )
    mapping = FactRecord(
        "mapping", "controlled_contract_mapping",
        {"contract_id": "HT-MNT-2026-001", "equipment_id": "DEV-001", "equipment_name": "设备 A"},
        "supported",
    )
    decision = _validate_contract_document(result, {}, {"controlled_contract_mapping": mapping})
    assert decision.state == "ambiguous"
    assert "优先约定" in decision.value


def test_unsigned_supplement_conflict_reports_both_limits_and_fails_closed() -> None:
    doc_ref = DocRef(
        kind="doc", doc_id="CT-Q4F03-024", page=1,
        snippet=(
            "签署正文写明服务恢复须在受理后 8 小时内完成；未签署的补充稿写 4 小时。"
            "该补充稿没有双方签章，档案中的两个时限尚未消解，不能将草稿视为已生效变更。"
            "适用范围：设备 EQ-Q4F03-024（赤湾输送机）。"
        ),
    )
    result = ToolResult(
        data="检索到合同条款。",
        trace=[TraceStep(step_id=1, tool="rerank", input={}, output={}, elapsed_ms=1, hit=[doc_ref])],
        refs=[doc_ref],
    )
    mapping = FactRecord(
        "mapping", "controlled_contract_mapping",
        {
            "contract_id": "CT-Q4F03-024", "equipment_id": "EQ-Q4F03-024",
            "equipment_name": "赤湾输送机", "mapping_relation_verified": True,
        },
        "supported",
    )

    decision = _validate_contract_document(
        result, {}, {"controlled_contract_mapping": mapping}
    )

    assert decision.state == "ambiguous"
    assert decision.diagnostic_code == "document_sla_override_unresolved"
    assert decision.value["signed_limit_hours"] == 8
    assert decision.value["unsigned_draft_limit_hours"] == [4]
    plan = build_work_order_sla_plan("WO-2026-0024")
    answer = Orchestrator._fact_plan_answer(plan, FactPlanRun(records={
        "contract_repair_limit": FactRecord(
            "limit", "contract_repair_limit", decision.value, decision.state,
            evidence_refs=decision.evidence, diagnostic_code=decision.diagnostic_code,
        ),
    }))
    assert "签署正文约定修复时限 8 小时" in answer
    assert "未签署补充稿写 4 小时" in answer
    assert "不能将未签稿视为已生效变更" in answer


def test_orchestrator_response_preserves_q24_conflict_and_verified_event_absence_refs() -> None:
    work_order_id = "WO-QF-024"
    equipment_id = "EQ-Q4F03-024"
    contract_id = "CT-Q4F03-024"
    contract_text = (
        "# 云岫园区运维合同 CT-Q4F03-024\n"
        "客户编号：CUST-Q4F03-024\n"
        "设备档案：EQ-Q4F03-024（赤湾输送机）\n"
        "签署归档批次：Q4-F03-024-SIGNED\n"
        "背景记录：合同附件由设施组按编号核对后归档。\n"
        "## 第 2 条 > 服务恢复时限\n"
        "签署正文写明服务恢复须在受理后 8 小时内完成；未签署的补充稿写 4 小时。"
        "该补充稿没有双方签章，档案中的两个时限尚未消解，不能将草稿视为已生效变更。\n"
        "## 第 5 条 > 适用范围\n"
        "仅适用于合同编号与设备稳定编号均匹配，且合同状态及日期范围有效的情形。"
        "设备显示名称仅供阅读，不能替代设备编号。"
    )

    class RouteMarker(BaseTool):
        @property
        def spec(self) -> ToolSpec:
            return ToolSpec("cross_source_demo", (), "测试中仅用于触发演示链路路由。")

        def run(self, _payload):
            raise AssertionError("事实计划执行器应处理本测试问题")

    class FakeSqlTool:
        def __init__(self, missing_event: str | None = None):
            self.missing_event = missing_event

        @staticmethod
        def _result(payload, sql, rows, refs=()):
            trace = TraceStep(
                step_id=1, tool="nl2sql", input=payload,
                output={"sql": sql, "row_count": len(rows)}, elapsed_ms=1.0,
                hit=list(refs),
            )
            return ToolResult(data={"rows": rows}, trace=[trace], refs=list(refs))

        def run(self, payload):
            question = payload["question"]
            if "查询工单编号" in question:
                rows = [{
                    "work_order_id": work_order_id,
                    "equipment_id": equipment_id,
                    "opened_at": "2026-11-24T00:00:00+00:00",
                }]
                ref = SqlRef(
                    kind="sql", table="work_orders",
                    column="work_order_id/equipment_id/opened_at", row_pk=work_order_id,
                    snippet=(f"工单 {work_order_id} 对应设备 {equipment_id}，"
                             "开单时间 2026-11-24T00:00:00+00:00。"),
                )
                sql = (
                    "SELECT work_order_id, equipment_id, opened_at FROM work_orders "
                    f"WHERE work_order_id = '{work_order_id}'"
                )
                return self._result(payload, sql, rows, [ref])

            if "查询设备编号" in question:
                rows = [{
                    "equipment_id": equipment_id, "equipment_name": "赤湾输送机",
                    "equipment_customer_id": "CUST-Q4F03-024", "contract_id": contract_id,
                    "mapping_valid_from": "2026-01-01", "mapping_valid_to": "2027-12-31",
                    "mapping_basis": "signed_acceptance_record",
                    "contract_customer_id": "CUST-Q4F03-024",
                    "contract_effective_from": "2026-01-01", "contract_effective_to": "2027-12-31",
                    "contract_status": "active",
                }]
                sql = (
                    "SELECT ce.equipment_id, e.equipment_name, e.customer_id AS equipment_customer_id, "
                    "ce.contract_id, ce.valid_from AS mapping_valid_from, ce.valid_to AS mapping_valid_to, "
                    "ce.mapping_basis, c.customer_id AS contract_customer_id, "
                    "c.effective_from AS contract_effective_from, c.effective_to AS contract_effective_to, "
                    "c.status AS contract_status FROM contract_equipment ce "
                    "INNER JOIN equipment e ON e.equipment_id = ce.equipment_id "
                    "INNER JOIN contracts c ON c.contract_id = ce.contract_id "
                    f"WHERE ce.equipment_id = '{equipment_id}'"
                )
                return self._result(payload, sql, rows)

            if "只查询 work_order_events 表" in question:
                event_type = question.split("event_type = '", 1)[1].split("'", 1)[0]
                rows = [] if event_type == self.missing_event else [{
                    "work_order_id": work_order_id,
                    "event_type": event_type,
                    "event_at": "2026-11-24T01:00:00+00:00",
                }]
                sql = (
                    "SELECT work_order_id, event_type, event_at FROM work_order_events "
                    f"WHERE work_order_id = '{work_order_id}' AND event_type = '{event_type}' LIMIT 1000"
                )
                refs = [] if not rows else [SqlRef(
                    kind="sql", table="work_order_events",
                    column="work_order_id/event_type/event_at",
                    row_pk=f"work_order_id={work_order_id}&event_type={event_type}",
                    snippet=f"{work_order_id} 的 {event_type} 时间为 2026-11-24T01:00:00+00:00。",
                )]
                return self._result(payload, sql, rows, refs)

            events = [
                {"work_order_id": work_order_id, "event_type": "accepted",
                 "event_at": "2026-11-24T01:00:00+00:00"},
            ]
            if self.missing_event != "service_restored":
                events.append({
                    "work_order_id": work_order_id, "event_type": "service_restored",
                    "event_at": "2026-11-24T01:00:00+00:00",
                })
            refs = [SqlRef(
                kind="sql", table="work_order_events",
                column="work_order_id/event_type/event_at",
                row_pk=f"work_order_id={row['work_order_id']}&event_type={row['event_type']}",
                snippet=f"{row['event_type']} {row['event_at']}",
            ) for row in events]
            sql = (
                "SELECT work_order_id, event_type, event_at FROM work_order_events "
                f"WHERE work_order_id = '{work_order_id}' ORDER BY event_at, event_type"
            )
            return self._result(payload, sql, events, refs)

    class FakeDocTool:
        def __init__(self):
            self.calls = 0

        def run(self, _payload):
            self.calls += 1
            ref = DocRef(kind="doc", doc_id=contract_id, page=1, snippet=contract_text)
            return ToolResult(
                data="检索到合同原文。",
                trace=[TraceStep(
                    step_id=1, tool="rerank", input={}, output={}, elapsed_ms=1.0, hit=[ref],
                )],
                refs=[ref],
            )

    def ask_with(missing_event: str | None):
        registry = ToolRegistry()
        registry.register(RouteMarker())
        docs = FakeDocTool()
        executor = _handlers_for_tools(
            FakeSqlTool(missing_event), docs, verification_enabled=True,
        )
        orchestrator = Orchestrator(registry, fact_plan_executor=executor)
        response = orchestrator.answer(
            f"工单 {work_order_id} 是否满足修复时限？",
            session_id=f"cs14-regression-{missing_event or 'complete'}",
        )
        return response, docs

    conflict_response, conflict_docs = ask_with(None)
    assert conflict_docs.calls == 1, (
        conflict_response.answer,
        [(step.tool, step.output) for step in conflict_response.trace],
    )
    assert "签署正文约定修复时限 8 小时" in conflict_response.answer
    assert "未签署补充稿写 4 小时" in conflict_response.answer
    assert "不能将未签稿视为已生效变更" in conflict_response.answer
    assert any(
        ref.kind == "doc" and ref.doc_id == contract_id
        and "8 小时内完成" in ref.snippet and "未签署的补充稿写 4 小时" in ref.snippet
        for ref in conflict_response.refs
    )
    conflict_round_trip = AskResponse.model_validate_json(conflict_response.model_dump_json())
    assert conflict_round_trip.answer == conflict_response.answer

    missing_response, missing_docs = ask_with("service_restored")
    assert missing_docs.calls == 0
    absence_step = next(
        step for step in missing_response.trace if step.tool == "cross_source_absence_check"
    )
    assert absence_step.output == {
        "table": "work_order_events",
        "filters": {"work_order_id": work_order_id, "event_type": "service_restored"},
        "row_count": 0,
        "derived_from_executed_query": True,
    }
    assert absence_step.hit
    assert absence_step.hit[0] in missing_response.refs
    assert "无法判断" in missing_response.answer
    missing_round_trip = AskResponse.model_validate_json(missing_response.model_dump_json())
    assert missing_round_trip.answer == missing_response.answer
    assert {ref.model_dump_json() for ref in missing_response.refs} == {
        ref.model_dump_json()
        for step in missing_response.trace for ref in (step.hit or [])
    }


def test_device_specific_repair_clause_precedes_generic_baseline_in_real_chunks(tmp_path) -> None:
    from app.rag.ingest.chunker import build_chunks
    from app.rag.ingest.loader import extract_blocks
    from app.rag.ingest.toc import build_tree

    path = tmp_path / "tiered_contract.md"
    path.write_text(
        "# Contract CT-TIERED\n\n"
        "## 2 Scope\n"
        "本合同只适用于设备编号 EQ-1101、EQ-1103。\n\n"
        "## 3 Service SLA\n"
        "| 指标 | 终点事件 | 时限 |\n"
        "|---|---|---:|\n"
        "| 修复完成 | `service_restored` | 4 小时内 |\n\n"
        "## 4 Annex\n"
        "### 4.1 Device tier\n"
        "EQ-1103 自动扶梯F1 的服务恢复，自工单受理时刻起 8 小时内完成。\n",
        encoding="utf-8",
    )
    chunks = build_chunks(extract_blocks(path), build_tree(path), "CT-TIERED")
    assert any("4 小时内" in chunk.text for chunk in chunks)
    assert any("8 小时内" in chunk.text for chunk in chunks)

    refs = [
        DocRef(
            kind="doc", doc_id="CT-TIERED", page=chunk.page,
            section_path=" / ".join(chunk.section_path or []),
            snippet=chunk.text, char_start=chunk.char_start, char_end=chunk.char_end,
        )
        for chunk in chunks
    ]
    result = ToolResult(
        data="检索到合同条款。",
        trace=[TraceStep(
            step_id=1, tool="rerank", input={}, output={}, elapsed_ms=1.0, hit=refs,
        )],
        refs=refs,
    )

    for equipment_id, equipment_name, expected_limit in (
        ("EQ-1101", "站台门单元A侧", 4),
        ("EQ-1103", "自动扶梯F1", 8),
    ):
        mapping = FactRecord(
            "mapping", "controlled_contract_mapping",
            {
                "contract_id": "CT-TIERED", "equipment_id": equipment_id,
                "equipment_name": equipment_name, "mapping_relation_verified": True,
            },
            "supported",
        )
        decision = _validate_contract_document(
            result, {}, {"controlled_contract_mapping": mapping}
        )
        assert decision.state == "supported"
        assert decision.value["limit_hours"] == expected_limit


def test_conflicting_generic_repair_clauses_still_fail_closed() -> None:
    doc_ref = DocRef(
        kind="doc", doc_id="CT-CONFLICT",
        snippet=(
            "本合同只适用于设备编号 EQ-1101。\n"
            "| 修复完成 | service_restored | 5 小时内 |\n"
            "| 修复完成 | service_restored | 9 小时内 |"
        ),
    )
    result = ToolResult(
        data="检索到合同条款。",
        trace=[TraceStep(
            step_id=1, tool="rerank", input={}, output={}, elapsed_ms=1.0, hit=[doc_ref],
        )],
        refs=[doc_ref],
    )
    mapping = FactRecord(
        "mapping", "controlled_contract_mapping",
        {
            "contract_id": "CT-CONFLICT", "equipment_id": "EQ-1101",
            "equipment_name": "站台门单元A侧", "mapping_relation_verified": True,
        },
        "supported",
    )

    decision = _validate_contract_document(
        result, {}, {"controlled_contract_mapping": mapping}
    )

    assert decision.state == "ambiguous"
    assert decision.diagnostic_code == "document_sla_ambiguous"


def test_single_sla_threshold_for_another_device_does_not_apply_to_scoped_device() -> None:
    doc_ref = DocRef(
        kind="doc", doc_id="HT-MNT-2026-001", page=1,
        snippet=(
            "适用范围 | 设备 A | 本合同约定的维保服务\n"
            "| 修复完成 | 设备 B 故障修复 | 8 小时内 |"
        ),
    )
    result = ToolResult(
        data="检索到合同条款。",
        trace=[TraceStep(step_id=1, tool="rerank", input={}, output={}, elapsed_ms=1, hit=[doc_ref])],
        refs=[doc_ref],
    )
    mapping = FactRecord(
        "mapping", "controlled_contract_mapping",
        {"contract_id": "HT-MNT-2026-001", "equipment_id": "DEV-A", "equipment_name": "设备 A"},
        "supported",
    )
    decision = _validate_contract_document(result, {}, {"controlled_contract_mapping": mapping})
    assert decision.state == "insufficient"
    assert "未明确关联" in decision.value


def test_asset_descriptor_matching_rejects_prefix_ids_and_similar_names() -> None:
    assert _repair_clause_targets_equipment(
        "DEV-001 service_restored", "DEV-001", "设备 A"
    )
    assert not _repair_clause_targets_equipment(
        "DEV-0010 service_restored", "DEV-001", "设备 A"
    )
    assert not _repair_clause_targets_equipment(
        "DEV-001_extra service_restored", "DEV-001", "设备 A"
    )
    assert not _repair_clause_targets_equipment(
        "dev-999 service_restored", "DEV-001", "设备 A"
    )
    assert not _repair_clause_targets_equipment(
        "设备 AB故障修复", "DEV-001", "设备 A"
    )
    assert _repair_clause_targets_equipment(
        "设备 A故障修复", "DEV-001", "设备 A"
    )


def test_rerank_miss_cannot_fall_back_to_generated_answer_references() -> None:
    doc_ref = DocRef(
        kind="doc", doc_id="HT-MNT-2026-001", page=1,
        snippet="适用范围 | 设备 A |\n| 修复完成 | service_restored | 8 小时内 |",
    )
    non_candidate = DocRef(
        kind="doc", doc_id="OTHER-CONTRACT", page=1, snippet="其他合同候选。",
    )
    result = ToolResult(
        data="生成器仍然引用了目标合同。",
        trace=[TraceStep(step_id=1, tool="rerank", input={}, output={}, elapsed_ms=1, hit=[non_candidate])],
        refs=[doc_ref],
    )
    assert _contract_candidate_refs(result, "HT-MNT-2026-001") == []


def test_only_final_rerank_candidates_can_support_contract_facts() -> None:
    first = DocRef(kind="doc", doc_id="HT-MNT-2026-001", page=1, snippet="old candidate")
    final = DocRef(kind="doc", doc_id="OTHER-CONTRACT", page=1, snippet="final candidate")
    result = ToolResult(
        data="检索到合同条款。",
        trace=[
            TraceStep(step_id=1, tool="rerank", input={}, output={}, elapsed_ms=1, hit=[first]),
            TraceStep(step_id=2, tool="rerank", input={}, output={}, elapsed_ms=1, hit=[final]),
        ],
        refs=[first],
    )
    assert _contract_candidate_refs(result, "HT-MNT-2026-001") == []


def test_first_response_limit_for_another_device_is_not_supported() -> None:
    doc_ref = DocRef(
        kind="doc", doc_id="HT-MNT-2026-001", page=1,
        snippet=(
            "本合同适用于设备 A。\n"
            "| 首次响应 | DEV-999 | 30 分钟内 |\n"
            "| 修复完成 | service_restored | 8 小时内 |"
        ),
    )
    result = ToolResult(
        data="已检索合同片段。",
        trace=[TraceStep(step_id=1, tool="rerank", input={}, output={}, elapsed_ms=1, hit=[doc_ref])],
        refs=[doc_ref],
    )
    mapping = FactRecord(
        "mapping", "controlled_contract_mapping",
        {"contract_id": "HT-MNT-2026-001", "equipment_id": "DEV-001", "equipment_name": "设备 A"},
        "supported",
    )
    decision = _validate_contract_document(
        result, {"question": "首次响应和修复是否满足？"},
        {"controlled_contract_mapping": mapping},
    )
    assert decision.state == "supported"
    assert decision.value["first_response_limit_minutes"] is None
    assert decision.value["first_response_clause_status"] == "insufficient"


def test_requested_first_response_metric_is_grounded_in_clause_and_event_rows() -> None:
    doc_ref = DocRef(
        kind="doc",
        doc_id="HT-MNT-2026-001",
        snippet=(
            "本合同只适用于设备编号 DEV-001。\n"
            "以下时限均以受理事件时间为起点。\n"
            "| 首次响应 | `first_response` | 30 分钟内 |\n"
            "| 修复完成 | `service_restored` | 8 小时内 |\n"
            "修复完成耗时计算为 service_restored 时间减 accepted 时间。"
        ),
        page=1,
    )
    doc_result = ToolResult(
        data="根据条款，首次响应和修复完成有不同的时限。",
        trace=[TraceStep(step_id=1, tool="rerank", input={}, output={}, elapsed_ms=1, hit=[doc_ref])],
        refs=[doc_ref],
    )
    mapping = FactRecord(
        "mapping", "controlled_contract_mapping",
        {"contract_id": "HT-MNT-2026-001", "equipment_id": "DEV-001"},
        "supported",
    )
    clause = _validate_contract_document(
        doc_result, {"question": "首次响应和修复是否满足？"},
        {"controlled_contract_mapping": mapping},
    )
    assert clause.state == "supported"
    assert clause.value["limit_hours"] == 8
    assert clause.value["first_response_limit_minutes"] == 30

    rows = [
        {"work_order_id": "WO-2026-0002", "event_type": "accepted", "event_at": "2026-09-19T10:00:00+08:00"},
        {"work_order_id": "WO-2026-0002", "event_type": "first_response", "event_at": "2026-09-19T10:20:00+08:00"},
        {"work_order_id": "WO-2026-0002", "event_type": "service_restored", "event_at": "2026-09-19T18:30:00+08:00"},
    ]
    refs = [
        SqlRef(kind="sql", table="work_order_events", column="event_type", row_pk=f"work_order_id={row['work_order_id']}&event_type={row['event_type']}", snippet=f"{row['event_type']} {row['event_at']}")
        for row in rows
    ]
    repair_value, state, evidence = _validate_duration(
        rows, refs, ToolResult(data={}, trace=[], refs=[]),
        {"work_order_id": "WO-2026-0002", "question": "首次响应和修复是否满足？"}, {},
    )
    assert state == "supported"
    assert repair_value["first_response_duration_seconds"] == 1200
    assert len(evidence) == 3

    derived = _make_first_response_duration_handler()(
        {},
        {"repair_duration": FactRecord(
            "duration", "repair_duration", repair_value, "supported",
            evidence_refs=tuple(evidence),
        )},
    )
    assert derived.decision.state == "supported"
    assert derived.decision.value["duration_seconds"] == 1200


def test_duration_requires_citations_for_both_boundary_events() -> None:
    rows = [
        {"work_order_id": "WO-2026-0001", "event_type": "accepted", "event_at": "2026-09-18T09:00:00+08:00"},
        {"work_order_id": "WO-2026-0001", "event_type": "service_restored", "event_at": "2026-09-18T16:40:00+08:00"},
    ]
    accepted = SqlRef(kind="sql", table="work_order_events", column="event_type", row_pk="work_order_id=WO-2026-0001&event_type=accepted", snippet="accepted")
    restored = SqlRef(kind="sql", table="work_order_events", column="event_type", row_pk="work_order_id=WO-2026-0001&event_type=service_restored", snippet="service_restored")

    _value, state, _refs = _validate_duration(rows, [accepted], ToolResult(data={}, trace=[], refs=[]), {"work_order_id": "WO-2026-0001"}, {})
    assert state == "insufficient"

    value, state, refs = _validate_duration(rows, [accepted, restored], ToolResult(data={}, trace=[], refs=[]), {"work_order_id": "WO-2026-0001"}, {})
    assert state == "supported"
    assert value["duration_seconds"] == 27600
    assert len(refs) == 2

    misleading_accepted = accepted.model_copy(update={
        "row_pk": "work_order_id=WO-2026-0001&event_type=not_accepted",
        "snippet": "not_accepted (accepted)",
    })
    _value, state, _refs = _validate_duration(
        rows, [misleading_accepted, restored], ToolResult(data={}, trace=[], refs=[]),
        {"work_order_id": "WO-2026-0001"}, {},
    )
    assert state == "insufficient"

    wrong_work_order = accepted.model_copy(update={
        "row_pk": "work_order_id=WO-2026-0099&event_type=accepted",
    })
    _value, state, _refs = _validate_duration(
        rows, [wrong_work_order, restored], ToolResult(data={}, trace=[], refs=[]),
        {"work_order_id": "WO-2026-0001"}, {},
    )
    assert state == "insufficient"

