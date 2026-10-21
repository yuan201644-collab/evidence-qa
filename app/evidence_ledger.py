"""编排层私有的证据账本原型。

本模块刻意不进入公共 Pydantic 契约。它区分工具/检索命中、最终响应引用，
以及由专用核验器显式建立的事实—证据关系。命中或被引用都不会自动变成
“事实已验证”。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from app.contracts import DocRef, RefT, SqlRef, TraceStep, collect_refs


FactState = Literal["candidate", "supported", "contradicted", "insufficient", "ambiguous"]


@dataclass(frozen=True)
class FactTask:
    """一个待确认的原子事实及其显式依赖。"""

    task_id: str
    target_fact: str
    source_candidates: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class FactRecord:
    """工具/核验器记录的事实值；只有显式校验后才能标记为 supported。"""

    fact_id: str
    task_id: str
    value: Any
    state: FactState
    evidence_ids: tuple[tuple[str, ...], ...] = ()
    evidence_refs: tuple[RefT, ...] = ()
    diagnostic_code: str | None = None


@dataclass
class FactPlan:
    """编排层私有的事实任务依赖图。"""

    tasks: dict[str, FactTask] = field(default_factory=dict)

    def add_task(self, task: FactTask) -> None:
        if not task.task_id.strip() or not task.target_fact.strip():
            raise ValueError("task_id 与 target_fact 不能为空")
        if task.task_id in self.tasks:
            raise ValueError(f"重复 task_id: {task.task_id}")
        if task.task_id in task.depends_on:
            raise ValueError("任务不能依赖自身")
        self.tasks[task.task_id] = task

    def validate(self) -> list[str]:
        """校验依赖都存在且无环；返回可执行的拓扑顺序。"""
        for task in self.tasks.values():
            missing = [dependency for dependency in task.depends_on if dependency not in self.tasks]
            if missing:
                raise ValueError(f"任务 {task.task_id} 依赖不存在的任务: {missing}")

        ordered: list[str] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError(f"事实任务依赖存在循环: {task_id}")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in self.tasks[task_id].depends_on:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)
            ordered.append(task_id)

        for task_id in self.tasks:
            visit(task_id)
        return ordered


def evidence_key(ref: RefT) -> tuple[str, ...]:
    """生成本轮账本定位键；无行主键时以 result_id 区分独立 SQL 结果。"""
    if isinstance(ref, SqlRef):
        if ref.row_pk:
            return ("sql", ref.table, ref.column or "", ref.row_pk)
        if ref.result_id:
            return ("sql_result", ref.result_id, ref.table, ref.column or "")
        # 不把两个缺少行/结果定位符的表级引用折叠成同一条证据。
        # 这个私有键只在本次进程内使用；它不能用于响应或跨请求持久化。
        return ("sql_unlocated", str(id(ref)))
    if isinstance(ref, DocRef):
        if ref.char_start is not None or ref.char_end is not None:
            return (
                "doc",
                ref.doc_id,
                str(ref.char_start if ref.char_start is not None else ""),
                str(ref.char_end if ref.char_end is not None else ""),
            )
        return ("doc", ref.doc_id, str(ref.page or ""), ref.section_path or "")
    return ("other", repr(ref))


@dataclass(frozen=True)
class ClaimEvidenceLink:
    """某项答案主张明确采用的证据键；不包含也不生成主张文本。"""

    claim_id: str
    evidence_ids: tuple[tuple[str, ...], ...]


@dataclass
class EvidenceLedger:
    """一次问答中的候选来源、最终引用和显式主张绑定。"""

    candidates: dict[tuple[str, ...], RefT] = field(default_factory=dict)
    response_evidence_ids: list[tuple[str, ...]] = field(default_factory=list)
    claim_links: dict[str, ClaimEvidenceLink] = field(default_factory=dict)
    plan: FactPlan = field(default_factory=FactPlan)
    facts: dict[str, FactRecord] = field(default_factory=dict)

    def add_task(self, task: FactTask) -> None:
        """添加任务；计划完整性和无环性由 validate_plan 统一检查。"""
        self.plan.add_task(task)

    def validate_plan(self) -> list[str]:
        """校验依赖都存在且无环；返回可执行的拓扑顺序。"""
        return self.plan.validate()

    def record_fact(self, fact: FactRecord) -> None:
        """登记事实；supported 必须有候选来源且所有前置任务已受支持。"""
        if fact.state not in {"candidate", "supported", "contradicted", "insufficient", "ambiguous"}:
            raise ValueError(f"未知事实状态: {fact.state}")
        if fact.fact_id in self.facts:
            raise ValueError(f"重复 fact_id: {fact.fact_id}")
        if fact.task_id not in self.plan.tasks:
            raise ValueError(f"事实引用未知任务: {fact.task_id}")
        unknown_evidence = [item for item in fact.evidence_ids if item not in self.candidates]
        if unknown_evidence:
            raise ValueError("事实引用了本轮未记录的候选证据")
        unknown_refs = [
            ref for ref in fact.evidence_refs if evidence_key(ref) not in self.candidates
        ]
        if unknown_refs:
            raise ValueError("事实包含本轮未记录的候选证据对象")
        if fact.state == "supported":
            if not fact.evidence_ids:
                raise ValueError("supported 事实必须至少有一条来源证据")
            dependencies = self.plan.tasks[fact.task_id].depends_on
            unsupported_dependencies = [
                task_id
                for task_id in dependencies
                if not any(
                    record.task_id == task_id and record.state == "supported"
                    for record in self.facts.values()
                )
            ]
            if unsupported_dependencies:
                raise ValueError(f"前置事实未受支持，不能确认当前事实: {unsupported_dependencies}")
        self.facts[fact.fact_id] = fact

    def add_candidate(self, ref: RefT) -> tuple[str, ...]:
        key = evidence_key(ref)
        existing = self.candidates.get(key)
        # 同一来源定位可能先以展示用短摘要出现，随后由局部验证器提供完整原文。
        # 保留定位键不变，但采用更完整片段，避免账本选回被截断的展示摘要。
        if existing is None or len(ref.snippet) > len(existing.snippet):
            self.candidates[key] = ref
        return key

    def select_for_response(self, refs: Iterable[RefT]) -> None:
        """登记最终响应引用；保留顺序并按现有定位规则去重。"""
        seen = set(self.response_evidence_ids)
        for ref in refs:
            key = self.add_candidate(ref)
            if key not in seen:
                seen.add(key)
                self.response_evidence_ids.append(key)

    def bind_claim(self, claim_id: str, evidence_ids: Iterable[tuple[str, ...]]) -> None:
        """只允许主张绑定到本轮已选入最终响应的证据。"""
        if not claim_id.strip():
            raise ValueError("claim_id 不能为空")
        ids = tuple(dict.fromkeys(evidence_ids))
        if not ids:
            raise ValueError("主张至少需要一条显式证据")
        selected = set(self.response_evidence_ids)
        unknown = [evidence_id for evidence_id in ids if evidence_id not in selected]
        if unknown:
            raise ValueError("主张不能绑定到未进入最终响应的候选证据")
        self.claim_links[claim_id] = ClaimEvidenceLink(claim_id, ids)

    def response_refs(self) -> list[RefT]:
        """按最终响应顺序返回引用。"""
        return [self.candidates[key] for key in self.response_evidence_ids]


def ledger_for_response(
    tool_refs: Iterable[RefT], trace: list[TraceStep]
) -> EvidenceLedger:
    """按现有响应规则构造内部账本，不把候选命中误记为主张支持。"""
    ledger = EvidenceLedger()
    for step in trace:
        for ref in step.hit or []:
            ledger.add_candidate(ref)

    chosen_tool_refs = list(tool_refs)
    if chosen_tool_refs:
        # 与当前 Orchestrator._collect_final_refs 相同：工具 refs 优先，
        # 因为 RAG 工具可在这里提供经过引用解析后的较窄集合。
        ledger.select_for_response(chosen_tool_refs)
    else:
        # 保持既有兜底行为：工具没有给 refs 时，从 trace 命中汇总。
        ledger.select_for_response(collect_refs(trace))
    return ledger

