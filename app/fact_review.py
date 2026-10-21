"""Safe public projection of the request-scoped fact ledger.

This module is intentionally deterministic: it never reads the answer text or
calls a model, database, or file system. A fact is linked only to a uniquely
located reference already present in the final response. Source-specific
records remain candidates unless an explicit business checker supports them.
"""

from __future__ import annotations

import math
from typing import Any

from app.contracts import FactReview, FactReviewCheck, FactReviewItem, DocRef, RefT, SqlRef
from app.evidence_ledger import EvidenceLedger, FactPlan, FactRecord, FactTask, evidence_key
from app.fact_executor import FactPlanRun


_FACT_LABELS = {
    "work_order_facts": "工单基本信息与事件",
    "controlled_contract_mapping": "设备与合同关系",
    "contract_repair_limit": "合同修复时限",
    "repair_duration": "受理至服务恢复耗时",
    "first_response_duration": "受理至首次响应耗时",
    "first_response_verdict": "首次响应时限判断",
    "sla_verdict": "修复时限判断",
}

_REASON_TEXT = {
    "unsupported_dependencies": "前置事实未得到支持，本项未执行。",
    "handler_not_configured": "本项没有配置执行处理器。",
    "missing_contract_mapping": "没有可用的合同映射，因此文档检索未执行。",
    "document_retrieval_not_run_missing_mapping": "没有可用合同映射，无法检索合同原文。",
    "task_execution_failed": "本项处理未能完成。",
    "not_run_output_conflict": "任务终态记录不一致，本项不能判为完成。",
    "fact_record_missing": "缺少本项的事实记录。",
    "terminal_trace_conflict": "任务同时出现互相矛盾的终态记录。",
    "structured_query_failed": "结构化查询失败，未据此确认事实。",
    "tool_result_unavailable": "工具未返回可核验的结果。",
    "document_retrieval_empty": "合同文档检索没有返回可用内容。",
    "document_reference_missing": "检索结果没有形成可定位的合同原文引用。",
    "document_scope_excluded": "合同原文明确排除该设备。",
    "document_scope_unproven": "合同原文没有足够证据确认适用范围。",
    "document_sla_ambiguous": "合同原文存在多个修复时限，当前无法确定适用条款。",
    "document_sla_unstated": "合同原文明确未约定修复完成总时限。",
    "document_sla_unparsed": "已检索到合同原文，但未能可靠解析修复时限。",
    "document_sla_override_unresolved": "合同原文存在尚未消解的特别约定或优先条款。",
    "document_sla_scope_unproven": "修复时限条款未能证明适用于当前设备。",
    "fact_not_supported": "现有记录不足以支持该事实。",
    "source_fact_not_independently_verified": "本路线记录了实际查询/检索与证据定位，但未对答案语义作独立的逐项事实判定。",
    "query_execution_missing": "未找到本轮最终查询执行的终态记录。",
    "document_generation_missing": "未找到本轮文档回答生成的终态记录。",
    "cross_source_fact_plan_unavailable": "本轮执行了多个来源，但没有可用的关系核验计划；不能把两边的命中自动认作同一事实。",
}


def build_fact_review(
    plan: FactPlan,
    run: FactPlanRun,
    final_refs: list[RefT],
) -> FactReview | None:
    """Project one work-order fact plan; unknown mode fails closed."""
    if run.verification_mode not in {"strict", "ablation"}:
        return None

    items: list[FactReviewItem] = []
    for task_id in plan.validate():
        record = run.records.get(task_id)
        execution_status, terminal_code = _execution_status(task_id, record, run)
        code = _safe_reason_code(
            terminal_code or (record.diagnostic_code if record is not None else None)
        )
        if record is not None and record.state == "supported" and execution_status == "completed":
            code = None
        elif code is None and (record is None or record.state != "supported"):
            code = "fact_not_supported"

        evidence_indices, evidence_status = _map_evidence(
            record.evidence_refs if record is not None else (), final_refs
        )
        items.append(FactReviewItem(
            fact_id=task_id,
            label=_FACT_LABELS.get(task_id, "相关事实"),
            summary=_summary_for_fact(task_id, record, run),
            status=record.state if record is not None else "insufficient",
            execution_status=execution_status,
            evidence_link_status=evidence_status,
            checks=_checks_for_task(task_id, run),
            evidence_ref_indices=evidence_indices,
            reason_code=code,
            reason=_REASON_TEXT.get(code) if code else None,
        ))

    return FactReview(
        coverage="cross_source_work_order_sla",
        verification_mode=run.verification_mode,
        verification_scope="fact_dependencies_and_business_checks",
        scope_note="仅展示事实计划记录的事实状态与证据关联，不代表答案中的其他表述均已核验。",
        items=items,
    )


def build_route_fact_plan(
    tool_names: list[str],
    question: str,
    query_intent: Any = None,
) -> tuple[str, FactPlan] | None:
    """Plan source-specific facts before execution without changing tool routing."""
    sources = set(tool_names)
    if sources == {"nl2sql"}:
        plan = FactPlan()
        plan.add_task(FactTask(
            task_id="structured_query_target",
            target_fact=_query_target(question, query_intent),
            source_candidates=("nl2sql",),
        ))
        return "structured_query", plan

    if sources == {"doc_search"}:
        plan = FactPlan()
        plan.add_task(FactTask(
            task_id="document_answer_target",
            target_fact="从文档资料回答并定位支持原文：" + question[:240],
            source_candidates=("doc_search",),
        ))
        return "document_qa", plan

    if sources == {"graph_reason"}:
        # The existing graph_reason tool may return SQL and document refs in one
        # route. Until it emits an explicit relation-verification record, expose
        # the source evidence but keep the cross-source relation unverified.
        plan = FactPlan()
        plan.add_task(FactTask(
            task_id="graph_source_evidence",
            target_fact="本次跨源问题需要核对的来源事实：" + question[:240],
            source_candidates=("graph_reason",),
        ))
        plan.add_task(FactTask(
            task_id="cross_source_relation",
            target_fact="核验跨来源事实是否指向同一对象且适用条件、口径一致。",
            source_candidates=("cross_source_alignment",),
            depends_on=("graph_source_evidence",),
        ))
        return "cross_source_unplanned", plan

    if "nl2sql" in sources and "doc_search" in sources and "graph_reason" not in sources:
        plan = FactPlan()
        plan.add_task(FactTask(
            task_id="structured_source_fact",
            target_fact="结构化来源需要提供的事实：" + _query_target(question, query_intent),
            source_candidates=("nl2sql",),
        ))
        plan.add_task(FactTask(
            task_id="document_source_fact",
            target_fact="文档来源需要提供的事实：" + question[:240],
            source_candidates=("doc_search",),
        ))
        plan.add_task(FactTask(
            task_id="cross_source_relation",
            target_fact="核验两类来源是否指向同一对象且适用条件、口径一致。",
            source_candidates=("cross_source_alignment",),
            depends_on=("structured_source_fact", "document_source_fact"),
        ))
        return "cross_source_unplanned", plan

    return None


def build_execution_fact_review(
    coverage: str,
    plan: FactPlan,
    runs: list[Any],
    final_refs: list[RefT],
) -> FactReview:
    """Project real source traces into the shared ledger without claiming semantic support."""
    order = plan.validate()
    ledger = EvidenceLedger(plan=plan)
    for run in runs:
        for step in run.result.trace:
            for ref in step.hit or []:
                ledger.add_candidate(ref)
    ledger.select_for_response(final_refs)

    run_by_tool: dict[str, list[Any]] = {}
    for run in runs:
        run_by_tool.setdefault(run.tool_name, []).append(run)

    for task_id in order:
        task = plan.tasks[task_id]
        if task_id == "cross_source_relation":
            relation_refs = [
                ref
                for source_run in runs
                if source_run.tool_name in {"nl2sql", "doc_search", "graph_reason"}
                for ref in source_run.result.refs
            ]
            evidence_ids = tuple(dict.fromkeys(
                ledger.add_candidate(ref) for ref in relation_refs
            ))
            ledger.record_fact(FactRecord(
                fact_id=task_id,
                task_id=task_id,
                value={"target": task.target_fact},
                state="insufficient",
                evidence_ids=evidence_ids,
                evidence_refs=tuple(relation_refs),
                diagnostic_code="cross_source_fact_plan_unavailable",
            ))
            continue
        tool_name = task.source_candidates[0] if task.source_candidates else ""
        source_runs = run_by_tool.get(tool_name, [])
        refs = [ref for source_run in source_runs for ref in source_run.result.refs]
        evidence_ids = tuple(dict.fromkeys(ledger.add_candidate(ref) for ref in refs))
        ledger.record_fact(FactRecord(
            fact_id=task_id,
            task_id=task_id,
            value={"target": task.target_fact},
            state="candidate",
            evidence_ids=evidence_ids,
            evidence_refs=tuple(refs),
            diagnostic_code="source_fact_not_independently_verified",
        ))

    items: list[FactReviewItem] = []
    for task_id in order:
        task = plan.tasks[task_id]
        record = ledger.facts[task_id]
        if task_id == "cross_source_relation":
            execution_status, code = "not_run", "cross_source_fact_plan_unavailable"
            evidence_indices, evidence_status = _map_evidence(record.evidence_refs, final_refs)
            checks = [FactReviewCheck(code="cross_source_relation_check", result="not_run")]
        else:
            tool_name = task.source_candidates[0] if task.source_candidates else ""
            source_runs = run_by_tool.get(tool_name, [])
            execution_status, code = _source_execution_status(tool_name, source_runs)
            evidence_indices, evidence_status = _map_evidence(record.evidence_refs, final_refs)
            checks = _source_checks(tool_name, source_runs, evidence_status)
        items.append(FactReviewItem(
            fact_id=task_id,
            label={
                "structured_query_target": "本次数据查询目标",
                "document_answer_target": "本次文档回答目标",
                "structured_source_fact": "结构化来源事实",
                "document_source_fact": "文档来源事实",
                "graph_source_evidence": "跨源候选证据",
                "cross_source_relation": "跨源关系与口径",
            }.get(task_id, "本次问题事实"),
            summary=task.target_fact,
            status=record.state,
            execution_status=execution_status,
            evidence_link_status=evidence_status,
            checks=checks,
            evidence_ref_indices=evidence_indices,
            reason_code=code or record.diagnostic_code,
            reason=_REASON_TEXT.get(code or record.diagnostic_code or ""),
        ))

    scope_and_note = {
        "structured_query": (
            "query_execution_and_evidence",
            "记录查询目标、只读校验、语义关联检查（如实际执行）、执行终态、结果完整性和引用定位；尚未独立证明查询结果完整满足自然语言意图。",
        ),
        "document_qa": (
            "document_retrieval_and_citation",
            "记录检索、生成和引用定位；检索命中或存在引用本身不证明原文支持答案中的每项事实。",
        ),
        "cross_source_unplanned": (
            "cross_source_execution_only",
            "记录本轮跨源候选证据与引用；本轮没有通用关系核验计划，因此不会把来源并列展示成关系已核验。",
        ),
    }[coverage]
    return FactReview(
        coverage=coverage,
        verification_mode="source_specific",
        verification_scope=scope_and_note[0],
        scope_note=scope_and_note[1],
        items=items,
    )


def _query_target(question: str, query_intent: Any) -> str:
    if query_intent is None:
        return "按已澄清的问题目标执行只读查询：" + question[:240]
    groups = (
        ("对象", getattr(query_intent, "objects", ())),
        ("指标", getattr(query_intent, "measures", ())),
        ("时间", getattr(query_intent, "time_expressions", ())),
        ("维度", getattr(query_intent, "dimensions", ())),
        ("筛选", getattr(query_intent, "filters", ())),
    )
    parts = [f"{label}：{'、'.join(str(item)[:80] for item in values[:8])}" for label, values in groups if values]
    grain = getattr(query_intent, "target_grain", None)
    if grain:
        parts.append("粒度：" + str(grain)[:80])
    parts.append("原问题：" + question[:180])
    return "；".join(parts)[:480]


def _source_execution_status(tool_name: str, runs: list[Any]) -> tuple[str, str | None]:
    if not runs:
        return "not_run", None
    trace = [step for run in runs for step in run.result.trace]
    if tool_name == "nl2sql":
        final_data = next((run.result.data for run in reversed(runs)
                           if isinstance(run.result.data, dict)), None)
        if final_data is not None:
            if final_data.get("need_clarify") is True:
                return "completed", "query_execution_missing"
            if final_data.get("error"):
                return "completed", "structured_query_failed"
            if isinstance(final_data.get("rows"), list):
                return "completed", None
        executions = [step for step in trace if "row_count" in step.output]
        if executions and executions[-1].output.get("error"):
            return "completed", "structured_query_failed"
        if not executions:
            return "completed", "query_execution_missing"
        return "completed", None
    if tool_name == "doc_search":
        generated = [
            step for step in trace
            if step.tool == "llm_generate"
            and isinstance(step.output.get("answer_len"), int)
            and not step.output.get("error")
        ]
        return ("completed", None) if generated else ("completed", "document_generation_missing")
    return "completed", None


def _requirement_coverage_check(
    runs: list[Any],
    final_sql: str | None,
) -> FactReviewCheck | None:
    """把 S2 要求覆盖结论投影成一条核验卡检查项。

    **为什么单独一条而不是混进 query_execution**：执行成功与要求落实是两件事。
    `2023年有多少客户？` 会照样执行成功并返回全量客户数 —— 把两者合成一条，
    这条 SQL 会被判成"通过"，正是 S2 要消灭的静默丢弃。

    取值口径（S2 计划 §12.1）：
      pass    → passed（全部落实）
      repair  → failed（有明确"要求被丢掉"的证据）
      unknown → unknown（看懂了 SQL 但证不出是否满足，如实标黄）
      缺席    → 返回 None，**不加这条检查**（闸门 off 或没跑核对）。
                造一条 not_run 会让人误以为"核对了但没执行"，与"根本没核对"混淆。

    `detail` 只放 repair/unknown 的具体原因：用户要看的是**哪一条**条件没落实，
    不是"未通过"三个字。
    """
    coverage = None
    for run in reversed(runs):
        data = getattr(run.result, "data", None)
        if isinstance(data, dict) and isinstance(data.get("requirement_coverage"), dict):
            coverage = data["requirement_coverage"]
            break
    if coverage is None:
        # 回退到 trace：闸门段即使 data 没透传，trace 里也有这一步。
        for run in reversed(runs):
            for step in reversed(getattr(run.result, "trace", []) or []):
                if step.tool != "sql_requirement_validator":
                    continue
                if final_sql and step.input.get("sql") != final_sql:
                    continue
                coverage = {
                    "status": step.output.get("status"),
                    "checks": step.output.get("checks") or [],
                }
                break
            if coverage is not None:
                break
    if coverage is None:
        return None

    status = coverage.get("status")
    result = {"pass": "passed", "repair": "failed", "unknown": "unknown"}.get(status)
    if result is None:
        return None

    detail = None
    if result != "passed":
        messages = []
        for item in coverage.get("checks") or []:
            if not isinstance(item, dict):
                continue
            if item.get("outcome") not in {"repair", "unknown"} or not item.get("message"):
                continue
            message = item["message"]
            # S2-F F5：继承条（上一轮带过来的条件）显式标注来源。
            # 用户看到「巴西」没落实时，要知道它不是本轮新提的、
            # 而是从上一轮延续的——否则会以为是系统凭空冒出来的条件。
            if item.get("inherited"):
                message += "（该条件来自上一轮对话的延续）"
            messages.append(message)
        if messages:
            detail = "；".join(messages[:3])
            if len(messages) > 3:
                detail += f"；等 {len(messages)} 项"
    return FactReviewCheck(code="sql_requirement_coverage", result=result, detail=detail)


def _source_checks(
    tool_name: str,
    runs: list[Any],
    evidence_status: str,
) -> list[FactReviewCheck]:
    trace = [step for run in runs for step in run.result.trace]
    checks: list[FactReviewCheck] = []
    if tool_name == "nl2sql":
        execution_indices = [
            index for index, step in enumerate(trace) if "row_count" in step.output
        ]
        result_data = next((run.result.data for run in reversed(runs)
                            if isinstance(run.result.data, dict)), None)
        result_sql = result_data.get("sql") if isinstance(result_data, dict) else None
        if not isinstance(result_sql, str) or not result_sql.strip():
            result_sql = None
        matching_execution_indices = [
            index for index in execution_indices
            if (trace[index].input.get("sql") or trace[index].output.get("sql")) == result_sql
        ] if result_sql is not None else []
        execution_index = (
            matching_execution_indices[-1] if matching_execution_indices
            else execution_indices[-1] if execution_indices else None
        )
        final_execution = trace[execution_index] if execution_index is not None else None
        final_sql = result_sql
        if final_execution is not None:
            candidate_sql = final_execution.input.get("sql") or final_execution.output.get("sql")
            if final_sql is None and isinstance(candidate_sql, str) and candidate_sql.strip():
                final_sql = candidate_sql

        validation_candidates = [
            (index, step)
            for index, step in enumerate(trace)
            if step.tool == "nl2sql" and isinstance(step.output.get("ok"), bool)
        ]
        if execution_index is not None:
            validation_candidates = [
                (index, step) for index, step in validation_candidates
                if index < execution_index
            ]
        validation = validation_candidates[-1][1] if validation_candidates else None
        checks.append(FactReviewCheck(
            code="readonly_sql_validation",
            result=("passed" if validation.output["ok"] else "failed") if validation else "not_run",
        ))
        semantic_steps = [
            step for index, step in enumerate(trace)
            if step.tool == "sql_semantic_validator"
            and (execution_index is None or index < execution_index)
        ]
        if final_sql is not None:
            semantic_steps = [
                step for step in semantic_steps if step.input.get("sql") == final_sql
            ]
        semantic_status = semantic_steps[-1].output.get("status") if semantic_steps else None
        checks.append(FactReviewCheck(
            code="query_relation_validation",
            result="passed" if semantic_status == "pass" else (
                "failed" if semantic_status in {"repair", "blocked", "fail"} else "not_run"
            ),
        ))
        result_data_error = bool(result_data.get("error")) if isinstance(result_data, dict) else False
        if isinstance(result_data, dict) and result_data.get("need_clarify") is True:
            query_result = "not_run"
        elif result_data_error:
            query_result = (
                "failed" if final_execution is not None and final_execution.output.get("error")
                else "not_run"
            )
        elif isinstance(result_data, dict) and isinstance(result_data.get("rows"), list):
            query_result = "passed"
        elif final_execution is not None:
            query_result = "failed" if final_execution.output.get("error") else "passed"
        else:
            query_result = "not_run"
        checks.append(FactReviewCheck(code="query_execution", result=query_result))

        # S2 要求覆盖核对：SQL 真的执行了，不代表用户说的条件都落实了。
        # 这一项与执行结果无关 —— 执行成功但条件被丢掉，正是要暴露的病症。
        coverage_check = _requirement_coverage_check(runs, final_sql)
        if coverage_check is not None:
            checks.append(coverage_check)

        truncated = result_data.get("possible_truncation") if isinstance(result_data, dict) else None
        if not isinstance(truncated, bool) and final_execution is not None:
            truncated = final_execution.output.get("possible_truncation")
        completeness_result = (
            "failed" if truncated is True else "passed" if truncated is False else "unknown"
        ) if query_result != "not_run" else "not_run"
        checks.append(FactReviewCheck(
            code="query_result_completeness",
            result=completeness_result,
        ))
        checks.append(FactReviewCheck(
            code="semantic_fact_support",
            result="not_run",
        ))
    elif tool_name == "doc_search":
        retrieval = [step for step in trace if step.tool == "hybrid_retrieve"]
        if retrieval:
            hit_counts = [step.output.get("num_hits") for step in retrieval]
            known_counts = [count for count in hit_counts if isinstance(count, int) and not isinstance(count, bool)]
            if any(count > 0 for count in known_counts):
                result = "passed"
            elif known_counts and len(known_counts) == len(hit_counts):
                result = "failed"
            else:
                result = "unknown"
        else:
            result = "not_run"
        checks.append(FactReviewCheck(code="document_retrieval", result=result))
        generated = any(
            step.tool == "llm_generate"
            and isinstance(step.output.get("answer_len"), int)
            and not step.output.get("error")
            for step in trace
        )
        checks.append(FactReviewCheck(
            code="answer_generation",
            result="passed" if generated else "not_run",
        ))
        checks.append(FactReviewCheck(
            code="citation_reference_linkage",
            result="passed" if evidence_status == "complete" else (
                "unknown" if evidence_status == "partial" else (
                    "failed" if generated else "not_run"
                )
            ),
        ))
        checks.append(FactReviewCheck(code="semantic_fact_support", result="not_run"))
    return checks


def _execution_status(
    task_id: str,
    record: FactRecord | None,
    run: FactPlanRun,
) -> tuple[str, str | None]:
    not_run_codes: list[str] = []
    failure_codes: list[str] = []
    for step in run.trace:
        if step.input.get("task_id") != task_id:
            continue
        if step.tool == "fact_task_not_run":
            value = step.output.get("reason_code")
            if isinstance(value, str):
                not_run_codes.append(value)
        elif step.tool == "fact_task_failure":
            value = step.output.get("diagnostic_code")
            if isinstance(value, str):
                failure_codes.append(value)

    if not_run_codes and failure_codes:
        return "failed", "terminal_trace_conflict"
    if failure_codes:
        return "failed", failure_codes[-1]
    if not_run_codes:
        return "not_run", not_run_codes[-1]
    if record is None:
        return "failed", "fact_record_missing"
    return "completed", None


def _safe_reason_code(code: str | None) -> str | None:
    if code in _REASON_TEXT:
        return code
    return None


def _checks_for_task(task_id: str, run: FactPlanRun) -> list[FactReviewCheck]:
    checks: list[FactReviewCheck] = []
    if task_id == "controlled_contract_mapping":
        for step in run.trace:
            if step.tool != "cross_source_policy_check":
                continue
            observations = step.output.get("observations")
            if not isinstance(observations, list) or not observations:
                continue
            statuses = [
                item.get("status") for item in observations if isinstance(item, dict)
            ]
            if len(statuses) != len(observations):
                continue
            if all(status == "allowed" for status in statuses):
                result = "passed"
            elif any(status in {"denied", "undefined"} for status in statuses):
                result = "failed"
            else:
                result = "unknown"
            checks.append(FactReviewCheck(code="mapping_basis_policy", result=result))
            break

    metric_by_task = {
        "sla_verdict": "service_restored",
        "first_response_verdict": "first_response",
    }
    target_metric = metric_by_task.get(task_id)
    if target_metric:
        for step in run.trace:
            raw_checks = step.output.get("metric_checks")
            if not isinstance(raw_checks, list):
                continue
            for metric in raw_checks:
                if not isinstance(metric, dict) or metric.get("metric") != target_metric:
                    continue
                within_limit = metric.get("within_limit")
                if isinstance(within_limit, bool):
                    checks.append(FactReviewCheck(
                        code=f"{target_metric}_within_limit",
                        result="passed" if within_limit else "failed",
                    ))
                break
    return checks


def _summary_for_fact(task_id: str, record: FactRecord | None, run: FactPlanRun) -> str | None:
    if record is None:
        return None
    value = record.value
    if task_id == "work_order_facts" and isinstance(value, dict):
        order_id, equipment_id, opened_at = (
            _safe_display(value.get("work_order_id")),
            _safe_display(value.get("equipment_id")),
            _safe_display(value.get("opened_at")),
        )
        if order_id and equipment_id and opened_at:
            return f"工单 {order_id} 对应设备 {equipment_id}，开单时间 {opened_at}。"
    elif task_id == "controlled_contract_mapping" and isinstance(value, dict):
        contract_id = _safe_display(value.get("contract_id"))
        equipment_id = _safe_display(value.get("equipment_id"))
        if contract_id and equipment_id:
            return f"设备 {equipment_id} 对应合同 {contract_id}。"
        candidates = value.get("candidate_contracts")
        if isinstance(candidates, list) and candidates:
            return f"存在 {len(candidates)} 份适用合同候选，未能确定唯一关系。"
    elif task_id == "contract_repair_limit" and isinstance(value, dict):
        limit = _finite_number(value.get("limit_hours"))
        contract_id = _safe_display(value.get("contract_id"))
        if limit is not None:
            contract = f"合同 {contract_id} 的" if contract_id else ""
            return f"{contract}修复时限为 {_number_text(limit)} 小时。"
    elif task_id in {"repair_duration", "first_response_duration"} and isinstance(value, dict):
        seconds = _finite_number(value.get("duration_seconds"))
        if seconds is not None:
            label = "受理至服务恢复" if task_id == "repair_duration" else "受理至首次响应"
            return f"{label}耗时 {_duration_text(seconds)}。"
    elif task_id == "first_response_verdict" and isinstance(value, dict):
        seconds = _finite_number(value.get("duration_seconds"))
        limit = _finite_number(value.get("limit_minutes"))
        verdict = value.get("verdict")
        if seconds is not None and limit is not None and verdict in {"满足", "超时"}:
            return f"首次响应耗时 {_duration_text(seconds)}，时限 { _number_text(limit) } 分钟，判定{verdict}。"
    elif task_id == "sla_verdict":
        metric = _find_metric(run, "service_restored")
        if metric is not None:
            elapsed = _finite_number(metric.get("elapsed_seconds"))
            limit = _finite_number(metric.get("limit"))
            unit = metric.get("unit")
            within = metric.get("within_limit")
            if elapsed is not None and limit is not None and unit == "hours" and isinstance(within, bool):
                verdict = "符合" if within else "超过"
                return f"修复耗时 {_duration_text(elapsed)}，合同限时 {_number_text(limit)} 小时，{verdict}合同限时。"
    return None


def _find_metric(run: FactPlanRun, metric_name: str) -> dict[str, Any] | None:
    for step in run.trace:
        metrics = step.output.get("metric_checks")
        if not isinstance(metrics, list):
            continue
        for metric in metrics:
            if isinstance(metric, dict) and metric.get("metric") == metric_name:
                return metric
    return None


def _safe_display(value: Any) -> str | None:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text[:120] if text else None


def _finite_number(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = float(value)
    return number if math.isfinite(number) and abs(number) <= 10**12 else None


def _number_text(value: float) -> str:
    return f"{value:g}"


def _duration_text(seconds: float) -> str:
    if seconds < 0:
        return f"{_number_text(seconds)} 秒"
    total_minutes = int(seconds // 60)
    remainder_seconds = seconds - total_minutes * 60
    hours, minutes = divmod(total_minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} 小时")
    if minutes:
        parts.append(f"{minutes} 分钟")
    if remainder_seconds or not parts:
        parts.append(f"{_number_text(remainder_seconds)} 秒")
    return "".join(parts)


def _locator(ref: RefT) -> tuple[str, ...] | None:
    """Only return locators whose source identity can be proven without guessing."""
    if isinstance(ref, SqlRef):
        if not ref.table.strip():
            return None
        if ref.row_pk and ref.row_pk.strip():
            return ("sql", ref.table, ref.column or "", ref.row_pk)
        if ref.result_id and ref.result_id.strip():
            return ("sql_result", ref.result_id, ref.table, ref.column or "")
        return None
    if isinstance(ref, DocRef):
        if (ref.char_start is None or ref.char_end is None
                or ref.char_end <= ref.char_start):
            return None
        return ("doc", ref.doc_id, str(ref.page or ""), str(ref.char_start), str(ref.char_end))
    return None


def _map_evidence(
    evidence_refs: tuple[RefT, ...] | list[RefT],
    final_refs: list[RefT],
) -> tuple[list[int], str]:
    if not evidence_refs:
        return [], "unavailable"

    targets: dict[tuple[str, ...], list[int]] = {}
    for index, ref in enumerate(final_refs):
        locator = _locator(ref)
        if locator is not None:
            targets.setdefault(locator, []).append(index)

    indices: list[int] = []
    unresolved = 0
    seen_locators: set[tuple[str, ...]] = set()
    for ref in evidence_refs:
        locator = _locator(ref)
        if locator is None:
            unresolved += 1
            continue
        if locator in seen_locators:
            continue
        seen_locators.add(locator)
        matches = targets.get(locator, [])
        if len(matches) != 1 or matches[0] in indices:
            unresolved += 1
            continue
        indices.append(matches[0])

    if not indices:
        return [], "unavailable"
    if unresolved:
        return indices, "partial"
    return indices, "complete"


__all__ = [
    "build_execution_fact_review",
    "build_fact_review",
    "build_route_fact_plan",
]
