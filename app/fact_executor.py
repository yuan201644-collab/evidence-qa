"""私有事实计划执行器：编排任务处理器，并阻止未受支持事实继续传播。

本模块只提供调度与账本边界，不替任务决定如何查询或如何判定业务事实。
每个任务必须显式绑定处理器；处理器可以调用现有工具，也可以执行确定性校验。
处理器的原始输出默认只是候选，只有显式返回 supported 且关联本轮证据后，
依赖任务才会继续执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Callable, Literal, Mapping

from app.contracts import RefT, ToolResult, TraceStep
from app.evidence_ledger import (
    EvidenceLedger,
    FactPlan,
    FactRecord,
    FactState,
    evidence_key,
)
from app.progress import emit_progress, fail_active_progress
from app.tools.cross_source_policy import EARLY_STOP_ALLOWED_REASONS


@dataclass(frozen=True)
class FactDecision:
    """处理器对任务事实的显式判断；candidate 是默认且不满足依赖的状态。"""

    value: Any
    state: FactState = "candidate"
    evidence: tuple[RefT, ...] = ()
    stop_reason_code: str | None = None
    diagnostic_code: str | None = None

    def __post_init__(self) -> None:
        if (self.stop_reason_code is not None
                and self.stop_reason_code not in EARLY_STOP_ALLOWED_REASONS):
            raise ValueError("事实计划早停原因码不在冻结白名单中")
        if self.stop_reason_code is not None and self.state == "supported":
            raise ValueError("受支持事实不能同时声明安全早停")


@dataclass(frozen=True)
class FactTaskOutput:
    """单个任务的执行结果；工具调用记录可选，便于纯代码核验节点复用。"""

    decision: FactDecision
    tool_result: ToolResult | None = None
    not_run_reason_code: str | None = None
    not_run_reason: str | None = None
    not_run_dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.not_run_reason_code is not None and (
            self.tool_result is not None or self.decision.state == "supported"
        ):
            raise FactTaskOutputInvariantError


class FactTaskOutputInvariantError(ValueError):
    """拒绝把带工具结果或受支持结论的任务同时标记为未执行。"""

    diagnostic_code = "not_run_output_conflict"

    def __init__(self) -> None:
        super().__init__("未执行任务不能同时包含工具结果或 supported 决策")


FactTaskHandler = Callable[[Any, Mapping[str, FactRecord]], FactTaskOutput]
PayloadFactory = Callable[[Any, Mapping[str, FactRecord]], dict[str, Any]]
DecisionFactory = Callable[[ToolResult, Any, Mapping[str, FactRecord]], FactDecision]
ResultTransform = Callable[[ToolResult, FactDecision], ToolResult]


def tool_task_handler(
    tool: Any,
    payload_factory: PayloadFactory,
    decision_factory: DecisionFactory,
    result_transform: ResultTransform | None = None,
) -> FactTaskHandler:
    """把已有 ``BaseTool.run`` 工具包装成任务处理器。

    业务侧必须显式提供 payload 和事实核验函数；工具返回的命中或答案文本不会
    自动被提升为 supported。此适配器本身不依赖 NL2SQL/RAG 的具体实现。
    """

    def handle(context: Any, facts: Mapping[str, FactRecord]) -> FactTaskOutput:
        payload = payload_factory(context, facts)
        if not isinstance(payload, dict):
            raise TypeError("工具任务的 payload_factory 必须返回 dict")
        result = tool.run(payload)
        if not isinstance(result, ToolResult):
            raise TypeError("工具必须返回 ToolResult")
        decision = decision_factory(result, context, facts)
        if not isinstance(decision, FactDecision):
            raise TypeError("decision_factory 必须返回 FactDecision")
        if result_transform is not None:
            result = result_transform(result, decision)
            if not isinstance(result, ToolResult):
                raise TypeError("result_transform 必须返回 ToolResult")
        result = _attach_decision_evidence(result, decision.evidence)
        return FactTaskOutput(decision=decision, tool_result=result)

    return handle


def _attach_decision_evidence(result: ToolResult, evidence: tuple[RefT, ...]) -> ToolResult:
    """把确定性校验器从真实执行行派生的细粒度来源挂回原工具 trace。"""
    known = {evidence_key(ref) for ref in result.refs}
    for step in result.trace:
        for ref in step.hit or []:
            known.add(evidence_key(ref))
    additions = [ref for ref in evidence if evidence_key(ref) not in known]
    if not additions:
        return result

    # 若要补出 JOIN 每个表的逐行引用，必须将其绑定在包含已执行 SQL 的 trace 步，
    # 不能只把引用塞进最终列表。工具没有可识别的执行步骤时，宁可不伪造挂载点。
    trace = list(result.trace)
    target_index = next(
        (
            index for index in range(len(trace) - 1, -1, -1)
            if isinstance(trace[index].output.get("sql"), str)
            and "row_count" in trace[index].output
        ),
        None,
    )
    if target_index is None:
        return result
    target = trace[target_index]
    merged_hit = list(target.hit or [])
    seen = {evidence_key(ref) for ref in merged_hit}
    for ref in additions:
        key = evidence_key(ref)
        if key not in seen:
            merged_hit.append(ref)
            seen.add(key)
    trace[target_index] = target.model_copy(update={"hit": merged_hit})

    refs = list(result.refs)
    seen_refs = {evidence_key(ref) for ref in refs}
    for ref in additions:
        key = evidence_key(ref)
        if key not in seen_refs:
            refs.append(ref)
            seen_refs.add(key)
    return result.model_copy(update={"trace": trace, "refs": refs})


@dataclass
class FactPlanRun:
    """一次计划执行的结果；模式即使在提前停止时也显式保留。"""

    records: dict[str, FactRecord] = field(default_factory=dict)
    trace: list[TraceStep] = field(default_factory=list)
    refs: list[RefT] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    stop_reason_code: str | None = None
    stop_evidence: list[RefT] = field(default_factory=list)
    verification_mode: Literal["strict", "ablation"] | None = None


class FactPlanExecutor:
    """按拓扑顺序运行任务；依赖不受支持时安全早停。"""

    def __init__(
        self,
        handlers: Mapping[str, FactTaskHandler],
        *,
        stop_on_unsupported_dependencies: bool = True,
        verification_mode: Literal["strict", "ablation"] | None = None,
    ) -> None:
        self._handlers = dict(handlers)
        self._stop_on_unsupported_dependencies = stop_on_unsupported_dependencies
        self._verification_mode = verification_mode

    def run(self, plan: FactPlan, context: Any = None) -> FactPlanRun:
        ordered_task_ids = plan.validate()
        ledger = EvidenceLedger(plan=plan)
        run = FactPlanRun(verification_mode=self._verification_mode)

        for task_id in ordered_task_ids:
            task = plan.tasks[task_id]
            task_label = {
                "work_order_facts": "查询工单与关键事件",
                "controlled_contract_mapping": "核对设备与合同关系",
                "contract_repair_limit": "检索合同服务时限",
                "repair_duration": "计算事件耗时",
                "first_response_duration": "计算首次响应耗时",
                "first_response_verdict": "核对首次响应要求",
                "sla_verdict": "汇总事实与合同口径",
            }.get(task_id, "核验相关事实")
            unsupported = [
                dependency
                for dependency in task.depends_on
                if dependency not in run.records or run.records[dependency].state != "supported"
            ]
            if unsupported and self._stop_on_unsupported_dependencies:
                emit_progress(f"fact_{task_id}", task_label, "skipped")
                self._record_not_run(
                    run,
                    task_id,
                    reason_code="unsupported_dependencies",
                    reason="前置事实未受支持：" + "、".join(unsupported),
                    dependencies=tuple(unsupported),
                )
                record = FactRecord(
                    fact_id=task_id,
                    task_id=task_id,
                    value=None,
                    state="insufficient",
                )
                ledger.record_fact(record)
                run.records[task_id] = record
                continue

            handler = self._handlers.get(task_id)
            if handler is None:
                emit_progress(f"fact_{task_id}", task_label, "skipped")
                self._record_not_run(
                    run,
                    task_id,
                    reason_code="handler_not_configured",
                    reason="没有为任务配置执行处理器",
                )
                record = FactRecord(
                    fact_id=task_id,
                    task_id=task_id,
                    value=None,
                    state="insufficient",
                )
                ledger.record_fact(record)
                run.records[task_id] = record
                continue

            try:
                task_started = perf_counter()
                emit_progress(f"fact_{task_id}", task_label, "started")
                output = handler(context, dict(run.records))
                if not isinstance(output, FactTaskOutput):
                    raise TypeError("任务处理器必须返回 FactTaskOutput")
            except Exception as exc:
                fail_active_progress()
                run.skipped[task_id] = "任务处理失败：" + type(exc).__name__
                failure_output = {
                    "error_type": type(exc).__name__,
                    "sqlstate": getattr(exc, "sqlstate", None),
                }
                if isinstance(exc, FactTaskOutputInvariantError):
                    failure_output.update({
                        "status": "failed",
                        "diagnostic_code": exc.diagnostic_code,
                    })
                else:
                    failure_output.update({
                        "status": "failed",
                        "diagnostic_code": "task_execution_failed",
                    })
                run.trace.append(TraceStep(
                    step_id=len(run.trace) + 1,
                    tool="fact_task_failure",
                    input={"task_id": task_id},
                    output=failure_output,
                    elapsed_ms=(perf_counter() - task_started) * 1000.0,
                    hit=[],
                ))
                record = FactRecord(
                    fact_id=task_id,
                    task_id=task_id,
                    value=None,
                    state="insufficient",
                )
                ledger.record_fact(record)
                run.records[task_id] = record
                continue

            emit_progress(
                f"fact_{task_id}", task_label, "completed",
                elapsed_ms=(perf_counter() - task_started) * 1000.0,
            )

            if output.not_run_reason_code is not None:
                self._record_not_run(
                    run,
                    task_id,
                    reason_code=output.not_run_reason_code,
                    reason=output.not_run_reason or "处理器声明本任务未执行",
                    dependencies=output.not_run_dependencies,
                )

            tool_result = output.tool_result
            if tool_result is not None:
                for step in tool_result.trace:
                    run.trace.append(step.model_copy(update={"step_id": len(run.trace) + 1}))
                for step in tool_result.trace:
                    for ref in step.hit or []:
                        ledger.add_candidate(ref)
                for ref in tool_result.refs:
                    ledger.add_candidate(ref)

            # 决策只能关联本轮工具结果或其 trace 中真实出现的证据；不接受处理器
            # 凭空构造的来源对象。纯代码节点可以复用已记录的前置事实证据。
            evidence_ids: list[tuple[str, ...]] = []
            known_evidence = set(ledger.candidates)
            for ref in output.decision.evidence:
                key = evidence_key(ref)
                if key not in known_evidence:
                    continue
                if key not in evidence_ids:
                    evidence_ids.append(key)

            state = output.decision.state
            if (output.decision.stop_reason_code is not None
                    and evidence_ids and run.stop_reason_code is None):
                # Freeze the first cause that actually blocks a downstream
                # dependency; only ledger-registered evidence may support it.
                run.stop_reason_code = output.decision.stop_reason_code
                run.stop_evidence = [ledger.candidates[key] for key in evidence_ids]
            if state == "supported" and not evidence_ids:
                state = "insufficient"
                run.skipped[task_id] = "处理器未提供本轮可核验的来源证据"
            if (
                state == "supported"
                and unsupported
                and not self._stop_on_unsupported_dependencies
            ):
                # 消融路径可继续调用下游工具以测量“即使前置事实未支持仍继续”的行为，
                # 但不会把派生结果提升为 supported，维持账本的依赖不变量。
                state = "candidate"

            record = FactRecord(
                fact_id=task_id,
                task_id=task_id,
                value=output.decision.value,
                state=state,
                diagnostic_code=output.decision.diagnostic_code,
                evidence_ids=tuple(evidence_ids),
                    evidence_refs=tuple(
                        ledger.candidates[key] for key in evidence_ids
                    ),
            )
            ledger.record_fact(record)
            run.records[task_id] = record

        for record in run.records.values():
            if record.state != "candidate":
                ledger.select_for_response(record.evidence_refs)
        run.refs = ledger.response_refs()
        if run.stop_reason_code is not None:
            # This deterministic summary makes an actual supported early stop
            # observable to the evaluator. It is not an LLM-generated rationale.
            run.trace.append(TraceStep(
                step_id=len(run.trace) + 1,
                tool="cross_source_alignment",
                input={"stage": "fact_plan_early_stop"},
                output={
                    "status": "not_determined",
                    "stop_reason_code": run.stop_reason_code,
                },
                elapsed_ms=0.0,
                hit=list(run.stop_evidence),
            ))
        return run

    @staticmethod
    def _record_not_run(
        run: FactPlanRun,
        task_id: str,
        *,
        reason_code: str,
        reason: str,
        dependencies: tuple[str, ...] = (),
    ) -> None:
        """把未执行任务的确定性原因同时写入内部结果与可审计轨迹。"""
        run.skipped[task_id] = reason
        run.trace.append(TraceStep(
            step_id=len(run.trace) + 1,
            tool="fact_task_not_run",
            input={"task_id": task_id},
            output={
                "task_id": task_id,
                "status": "not_run",
                "reason_code": reason_code,
                "reason": reason,
                "unmet_dependencies": list(dependencies),
            },
            elapsed_ms=0.0,
            hit=[],
        ))


__all__ = [
    "FactDecision",
    "FactPlanExecutor",
    "FactPlanRun",
    "FactTaskOutput",
    "FactTaskHandler",
    "tool_task_handler",
]
