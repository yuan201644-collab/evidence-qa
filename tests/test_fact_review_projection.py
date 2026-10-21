from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.contracts import DocRef, FactReviewItem, SqlRef, ToolResult, TraceStep
from app.evidence_ledger import FactPlan, FactRecord, FactTask
from app.fact_executor import FactPlanRun
from app.fact_review import (
    build_execution_fact_review,
    build_fact_review,
    build_route_fact_plan,
)


def _plan(*task_ids: str) -> FactPlan:
    plan = FactPlan()
    for task_id in task_ids:
        plan.add_task(FactTask(task_id, task_id))
    return plan


def _sql_ref(*, row_pk: str | None = "WO-1") -> SqlRef:
    return SqlRef(
        table="cross_source_demo.work_orders",
        column="work_order_id",
        row_pk=row_pk,
        snippet="工单 WO-1",
    )


def _doc_ref(*, offsets: bool = True, snippet: str = "修复限时 8 小时") -> DocRef:
    return DocRef(
        doc_id="HT-1",
        page=1,
        section_path="第二章 > 2.1",
        snippet=snippet,
        char_start=120 if offsets else None,
        char_end=140 if offsets else None,
    )


def _trace(tool: str, output: dict, *, task_id: str | None = None) -> TraceStep:
    return TraceStep(
        step_id=1,
        tool=tool,
        input={"task_id": task_id} if task_id else {},
        output=output,
        elapsed_ms=1,
        hit=[],
    )


def test_projection_maps_reordered_final_refs_and_only_uses_structured_metric_result():
    sql_ref = _sql_ref()
    doc_ref = _doc_ref()
    plan = _plan("work_order_facts", "sla_verdict")
    run = FactPlanRun(
        verification_mode="strict",
        records={
            "work_order_facts": FactRecord(
                "work_order_facts", "work_order_facts",
                {"work_order_id": "WO-1", "equipment_id": "EQ-1", "opened_at": "2026-10-14"},
                "supported", evidence_refs=(sql_ref,),
            ),
            # This raw answer-like value must never be copied into the public summary.
            "sla_verdict": FactRecord(
                "sla_verdict", "sla_verdict", "SECRET answer sentence", "supported",
                evidence_refs=(sql_ref, doc_ref),
            ),
        },
        trace=[_trace("cross_source_alignment", {
            "customer_scope_checked": True,
            "event_order_checked": True,
            "mapping_and_contract_windows_checked_separately": True,
            "metric_checks": [{
                "metric": "service_restored",
                "elapsed_seconds": 27600,
                "limit": 8,
                "unit": "hours",
                "within_limit": True,
            }],
        })],
    )

    review = build_fact_review(plan, run, [doc_ref, sql_ref])

    assert review is not None
    assert review.verification_mode == "strict"
    order_item, verdict_item = review.items
    assert order_item.evidence_ref_indices == [1]
    assert order_item.evidence_link_status == "complete"
    assert verdict_item.evidence_ref_indices == [1, 0]
    assert verdict_item.evidence_link_status == "complete"
    assert verdict_item.checks[0].result == "passed"
    assert verdict_item.summary == "修复耗时 7 小时40 分钟，合同限时 8 小时，符合合同限时。"
    assert "SECRET" not in review.model_dump_json()
    assert all(check.code != "customer_scope_checked" for check in verdict_item.checks)


def test_checked_flags_and_raw_mapping_basis_do_not_create_passed_check():
    plan = _plan("controlled_contract_mapping")
    run = FactPlanRun(
        verification_mode="strict",
        records={"controlled_contract_mapping": FactRecord(
            "controlled_contract_mapping", "controlled_contract_mapping",
            {"equipment_id": "EQ-1", "contract_id": "HT-1", "mapping_basis": "raw_unchecked_value"},
            "supported", evidence_refs=(_sql_ref(),),
        )},
        trace=[_trace("cross_source_alignment", {
            "customer_scope_checked": True,
            "event_order_checked": True,
            "mapping_and_contract_windows_checked_separately": True,
            "mapping_basis": "raw_unchecked_value",
        })],
    )

    review = build_fact_review(plan, run, [_sql_ref()])

    assert review is not None
    assert review.items[0].checks == []


def test_evidence_mapping_fails_closed_for_missing_or_colliding_locators():
    plan = _plan("repair_duration")
    valid_sql = _sql_ref()
    no_pk_sql = _sql_ref(row_pk=None)
    no_offset_doc = _doc_ref(offsets=False)
    record = FactRecord(
        "repair_duration", "repair_duration",
        {"duration_seconds": 60}, "supported",
        evidence_refs=(valid_sql, no_pk_sql, no_offset_doc),
    )
    run = FactPlanRun(verification_mode="strict", records={"repair_duration": record})

    partial = build_fact_review(plan, run, [valid_sql, no_pk_sql, no_offset_doc])
    assert partial is not None
    assert partial.items[0].evidence_link_status == "partial"
    assert partial.items[0].evidence_ref_indices == [0]

    duplicate_final = build_fact_review(plan, run, [valid_sql, valid_sql.model_copy(update={"snippet": "different"})])
    assert duplicate_final is not None
    assert duplicate_final.items[0].evidence_link_status == "unavailable"
    assert duplicate_final.items[0].evidence_ref_indices == []


def test_execution_status_comes_from_task_terminal_trace_not_skipped_dict():
    plan = _plan("normal", "not_run", "failed", "missing")
    run = FactPlanRun(
        verification_mode="ablation",
        records={
            "normal": FactRecord("normal", "normal", None, "insufficient"),
            "not_run": FactRecord("not_run", "not_run", None, "insufficient"),
            "failed": FactRecord("failed", "failed", None, "insufficient"),
        },
        skipped={"normal": "this text must not determine terminal state"},
        trace=[
            _trace("fact_task_not_run", {"reason_code": "unsupported_dependencies"}, task_id="not_run"),
            _trace("fact_task_failure", {"status": "failed", "diagnostic_code": "task_execution_failed"}, task_id="failed"),
        ],
    )

    review = build_fact_review(plan, run, [])

    assert review is not None
    assert [item.execution_status for item in review.items] == ["completed", "not_run", "failed", "failed"]
    assert review.items[1].reason_code == "unsupported_dependencies"
    assert review.items[2].reason_code == "task_execution_failed"
    assert review.items[3].reason_code == "fact_record_missing"
    assert review.items[0].reason is not None


def test_unknown_mode_and_missing_evidence_cannot_create_strict_review():
    plan = _plan("repair_duration")
    record = FactRecord("repair_duration", "repair_duration", "not structured", "supported")
    assert build_fact_review(plan, FactPlanRun(records={"repair_duration": record}), []) is None

    review = build_fact_review(
        plan,
        FactPlanRun(verification_mode="strict", records={"repair_duration": record}),
        [],
    )
    assert review is not None
    assert review.items[0].evidence_link_status == "unavailable"
    assert review.items[0].evidence_ref_indices == []
    assert review.items[0].summary is None


def test_fact_review_contract_rejects_negative_reference_indices():
    with pytest.raises(ValidationError):
        FactReviewItem(
            fact_id="repair_duration",
            label="耗时",
            status="supported",
            execution_status="completed",
            evidence_link_status="complete",
            evidence_ref_indices=[-1],
        )


def test_route_fact_plans_cover_sql_document_and_cross_source_without_rerouting():
    intent = SimpleNamespace(
        objects=("发票",), measures=("数量",), time_expressions=("2023 年",),
        dimensions=(), filters=(), target_grain="年度",
    )
    sql_route = build_route_fact_plan(["nl2sql"], "2023 年有多少张发票", intent)
    document_route = build_route_fact_plan(["doc_search"], "合同的质保期多久")
    cross_route = build_route_fact_plan(["nl2sql", "doc_search"], "工单时长是否符合合同")
    graph_route = build_route_fact_plan(["graph_reason"], "数据库与文档如何关联")

    assert sql_route is not None and sql_route[0] == "structured_query"
    assert sql_route[1].validate() == ["structured_query_target"]
    assert "指标：数量" in sql_route[1].tasks["structured_query_target"].target_fact
    assert document_route is not None and document_route[0] == "document_qa"
    assert cross_route is not None and cross_route[0] == "cross_source_unplanned"
    assert cross_route[1].validate() == [
        "structured_source_fact", "document_source_fact", "cross_source_relation"
    ]
    assert graph_route is not None and graph_route[0] == "cross_source_unplanned"


def test_source_specific_sql_review_links_aggregate_result_without_claiming_support():
    route = build_route_fact_plan(["nl2sql"], "总发票金额是多少")
    assert route is not None
    coverage, plan = route
    ref = SqlRef(
        table="invoices", column="total", result_id="query-result-1",
        snippet="本次查询总额：1234.5",
    )
    trace = [
        TraceStep(
            step_id=1, tool="nl2sql", input={"sql": "SELECT SUM(total) FROM invoices"},
            output={"ok": True}, elapsed_ms=1,
        ),
        TraceStep(
            step_id=2, tool="nl2sql", input={"sql": "SELECT SUM(total) FROM invoices"},
            output={"row_count": 1, "columns": ["sum"], "possible_truncation": False},
            elapsed_ms=1, hit=[ref],
        ),
    ]
    run = SimpleNamespace(
        tool_name="nl2sql",
        result=ToolResult(data={"rows": [{"sum": 1234.5}]}, trace=trace, refs=[ref]),
    )

    review = build_execution_fact_review(coverage, plan, [run], [ref])
    item = review.items[0]

    assert review.coverage == "structured_query"
    assert review.verification_mode == "source_specific"
    assert item.status == "candidate"
    assert item.execution_status == "completed"
    assert item.evidence_link_status == "complete"
    assert item.evidence_ref_indices == [0]
    assert next(check for check in item.checks if check.code == "query_execution").result == "passed"
    assert next(check for check in item.checks if check.code == "semantic_fact_support").result == "not_run"


def test_source_specific_sql_checks_follow_the_execution_that_produced_final_results():
    route = build_route_fact_plan(["nl2sql"], "查询发票金额")
    assert route is not None
    coverage, plan = route
    final_ref = SqlRef(
        table="invoices", column="total", result_id="query-result-final",
        snippet="本次查询总额：1234.5",
    )
    trace = [
        TraceStep(
            step_id=1, tool="nl2sql", input={"sql": "SELECT total FROM invoices"},
            output={"ok": True}, elapsed_ms=1,
        ),
        TraceStep(
            step_id=2, tool="sql_semantic_validator",
            input={"sql": "SELECT total FROM invoices"},
            output={"status": "pass"}, elapsed_ms=1,
        ),
        TraceStep(
            step_id=3, tool="nl2sql", input={"sql": "SELECT total FROM invoices"},
            output={"row_count": 100, "possible_truncation": True}, elapsed_ms=1,
            hit=[final_ref],
        ),
        # A later, failed bounded-expansion candidate did not produce the
        # response result. Its checks must not overwrite the executed query.
        TraceStep(
            step_id=4, tool="nl2sql", input={"sql": "SELECT total, id FROM invoices"},
            output={"ok": False, "purpose": "bounded_result_materialization"}, elapsed_ms=1,
        ),
        TraceStep(
            step_id=5, tool="sql_semantic_validator",
            input={"sql": "SELECT total, id FROM invoices"},
            output={"status": "blocked"}, elapsed_ms=1,
        ),
    ]
    run = SimpleNamespace(
        tool_name="nl2sql",
        result=ToolResult(data={
            "rows": [{"total": 1234.5}],
            "sql": "SELECT total FROM invoices",
            "possible_truncation": True,
        }, trace=trace, refs=[final_ref]),
    )

    review = build_execution_fact_review(coverage, plan, [run], [final_ref])
    checks = {check.code: check.result for check in review.items[0].checks}

    assert checks["readonly_sql_validation"] == "passed"
    assert checks["query_relation_validation"] == "passed"
    assert checks["query_result_completeness"] == "failed"


def test_source_specific_sql_clarification_does_not_claim_query_execution():
    route = build_route_fact_plan(["nl2sql"], "查询发票金额")
    assert route is not None
    coverage, plan = route
    run = SimpleNamespace(
        tool_name="nl2sql",
        result=ToolResult(
            data={"rows": [], "sql": None, "need_clarify": True},
            trace=[],
            refs=[],
        ),
    )

    review = build_execution_fact_review(coverage, plan, [run], [])
    item = review.items[0]

    assert item.execution_status == "completed"
    assert item.reason_code == "query_execution_missing"
    assert next(check for check in item.checks if check.code == "query_execution").result == "not_run"
    assert next(check for check in item.checks if check.code == "query_result_completeness").result == "not_run"


def test_source_specific_rag_review_aggregates_multihop_retrieval_counts():
    route = build_route_fact_plan(["doc_search"], "合同约定的维修时限是多少")
    assert route is not None
    coverage, plan = route
    ref = _doc_ref()
    trace = [
        TraceStep(step_id=1, tool="hybrid_retrieve", input={}, output={"num_hits": 0}, elapsed_ms=1),
        TraceStep(step_id=2, tool="hybrid_retrieve", input={}, output={"num_hits": 1}, elapsed_ms=1, hit=[ref]),
        TraceStep(step_id=3, tool="llm_generate", input={}, output={"answer_len": 24}, elapsed_ms=1, hit=[ref]),
    ]
    run = SimpleNamespace(
        tool_name="doc_search",
        result=ToolResult(data="合同约定维修时限。", trace=trace, refs=[ref]),
    )

    review = build_execution_fact_review(coverage, plan, [run], [ref])
    item = review.items[0]

    assert review.coverage == "document_qa"
    assert item.status == "candidate"
    assert item.evidence_ref_indices == [0]
    assert next(check for check in item.checks if check.code == "document_retrieval").result == "passed"
    assert next(check for check in item.checks if check.code == "semantic_fact_support").result == "not_run"


def test_unplanned_cross_source_review_links_candidates_but_never_passes_relation():
    route = build_route_fact_plan(["nl2sql", "doc_search"], "工单修复时间是否符合合同限时")
    assert route is not None
    coverage, plan = route
    sql_ref = _sql_ref()
    doc_ref = _doc_ref()
    sql_run = SimpleNamespace(
        tool_name="nl2sql",
        result=ToolResult(data={"rows": []}, trace=[
            TraceStep(step_id=1, tool="nl2sql", input={}, output={"row_count": 1}, elapsed_ms=1, hit=[sql_ref]),
        ], refs=[sql_ref]),
    )
    doc_run = SimpleNamespace(
        tool_name="doc_search",
        result=ToolResult(data="限时为 8 小时。", trace=[
            TraceStep(step_id=1, tool="llm_generate", input={}, output={"answer_len": 8}, elapsed_ms=1, hit=[doc_ref]),
        ], refs=[doc_ref]),
    )

    review = build_execution_fact_review(coverage, plan, [sql_run, doc_run], [sql_ref, doc_ref])
    relation = review.items[-1]

    assert review.verification_mode == "source_specific"
    assert relation.status == "insufficient"
    assert relation.execution_status == "not_run"
    assert relation.evidence_link_status == "complete"
    assert relation.evidence_ref_indices == [0, 1]
    assert relation.checks[0].result == "not_run"
