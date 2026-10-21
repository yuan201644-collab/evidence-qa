"""工单—合同跨源演示工具。

仅用于 data/cross_source_demo 的合成数据：固定只读 SQL 取事实，按已验证合同编号
在演示合同 Markdown 中执行 BM25 检索，再由代码对齐时限口径并生成确定性结论。
不使用模型生成 SQL 或结论，也不修改共享请求/响应契约。
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from datetime import date, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.contracts import DocRef, SqlRef, ToolResult, TraceStep
from app.fact_planner import WORK_ORDER_ID_SEARCH_RE
from app.rag.ingest.chunker import build_chunks
from app.rag.ingest.loader import extract_blocks
from app.rag.ingest.toc import build_tree
from app.rag.retrieval.retriever import Bm25Retriever
from app.tools.base import BaseTool, ToolSpec
from app.tools.cross_source_policy import (
    allowed_mapping_bases,
    is_active_contract_status,
    make_absence_check,
    mapping_basis_status,
)

_ROOT = Path(__file__).resolve().parents[2]
_DEMO_ROOT = _ROOT / "data" / "cross_source_demo"
_DOCS_DIR = _DEMO_ROOT / "documents"


def _machine_sql_field_suffix(fields: dict[str, Any]) -> str:
    """Append machine-readable field/value labels after the human citation text."""
    def render(value: Any) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "true" if value else "false"
        formatter = getattr(value, "isoformat", None)
        return str(formatter()) if callable(formatter) else str(value)

    return " [" + "; ".join(
        f"{column}={json.dumps(render(value), ensure_ascii=False)}"
        for column, value in fields.items()
    ) + "]"

_WORK_ORDER_SQL = """
SELECT wo.work_order_id, wo.equipment_id, wo.opened_at, wo.summary,
       e.equipment_id AS matched_equipment_id,
       e.equipment_name, e.customer_id,
       ce.contract_id AS mapped_contract_id,
       ce.valid_from AS mapping_valid_from, ce.valid_to AS mapping_valid_to,
       ce.mapping_basis,
       c.contract_id AS matched_contract_id,
       c.customer_id AS contract_customer_id,
       c.effective_from AS contract_effective_from,
       c.effective_to AS contract_effective_to, c.status AS contract_status,
       c.timezone AS contract_timezone
FROM cross_source_demo.work_orders AS wo
LEFT JOIN cross_source_demo.equipment AS e
  ON e.equipment_id = wo.equipment_id
LEFT JOIN cross_source_demo.contract_equipment AS ce
  ON ce.equipment_id = wo.equipment_id
LEFT JOIN cross_source_demo.contracts AS c
  ON c.contract_id = ce.contract_id
WHERE wo.work_order_id = %s
ORDER BY ce.contract_id
""".strip()

_EVENTS_SQL = """
SELECT work_order_id, event_type, event_at
FROM cross_source_demo.work_order_events
WHERE work_order_id = %s
ORDER BY event_type
""".strip()

_LIMIT_PATTERNS: dict[str, tuple[str, str]] = {
    "first_response": (
        "首次响应",
        r"\|\s*首次响应\s*\|(?:\s*[^|\n]+\s*\|)?\s*(\d+)\s*分钟内\s*\|",
    ),
    "onsite_arrival": (
        "现场到达",
        r"\|\s*现场到达\s*\|(?:\s*[^|\n]+\s*\|)?\s*(\d+)\s*小时内\s*\|",
    ),
    "service_restored": (
        "修复完成",
        r"\|\s*修复完成\s*\|(?:\s*[^|\n]+\s*\|)?\s*(\d+)\s*小时内\s*\|",
    ),
}
_EXPLICITLY_UNSPECIFIED_REPAIR_SLA = re.compile(
    r"(?:不约定|未约定|不规定|未规定|不设定|未设定)[^。；\n]{0,16}"
    r"(?:服务恢复|修复完成)[^。；\n]{0,8}(?:总)?(?:时限|时间)"
    r"|(?:服务恢复|修复完成)[^。；\n]{0,8}(?:总)?(?:时限|时间)"
    r"[^。；\n]{0,16}(?:不约定|未约定|不规定|未规定|不设定|未设定)"
)


class _BudgetExceeded(TimeoutError):
    """当前演示工具的单次处理时限已耗尽。"""

    def __init__(self, message: str, attempts: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.attempts = attempts or []


class _DocumentEvidenceUnavailable(LookupError):
    """有限次定向检索后仍没有找到可核验的修复时限条款。"""

    def __init__(self, attempts: list[dict[str, Any]]) -> None:
        super().__init__("required repair SLA was not found")
        self.attempts = attempts


class _ConflictingContractLimit(LookupError):
    """Distinct limits remain unresolved after checking explicit scope and precedence."""

    def __init__(self, attempts: list[dict[str, Any]], ref: DocRef) -> None:
        super().__init__("conflicting repair limits could not be resolved")
        self.attempts = attempts
        self.ref = ref


class _RunBudget:
    """跨 SQL 和文档检索共用的单次处理预算。"""

    def __init__(self, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms
        self._deadline = time.perf_counter() + timeout_ms / 1000

    def remaining_ms(self) -> int:
        remaining = int((self._deadline - time.perf_counter()) * 1000)
        if remaining <= 0:
            raise _BudgetExceeded("cross-source demo time budget exhausted")
        return remaining


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


class CrossSourceDemoTool(BaseTool):
    """隔离的演示工具：PostgreSQL 只读事实 + 合同文档检索 + 代码核验。"""

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or os.environ.get("CROSS_SOURCE_DEMO_DSN", "").strip()
        # 初始工程建议值：5s 高于已观察到的单次 SQL / BM25 阶段耗时；
        # 文档最多两次不同查询策略（初查 + 一次定向修复），不是赛事指标。
        self._time_budget_ms = _bounded_env_int(
            "CROSS_SOURCE_DEMO_TIME_BUDGET_MS", default=5000, minimum=1000, maximum=30000
        )
        self._doc_attempt_limit = _bounded_env_int(
            "CROSS_SOURCE_DEMO_DOC_ATTEMPT_LIMIT", default=2, minimum=1, maximum=3
        )
        self._retriever: Bm25Retriever | None = None
        self._retriever_lock = threading.Lock()

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="cross_source_demo",
            capabilities=("structured_table", "unstructured_doc"),
            description=(
                "仅处理独立跨源演示数据中的工单编号问题；按工单/设备/合同编号关联"
                "PostgreSQL只读记录与对应Markdown合同条款。"
            ),
            input_schema={
                "question": "包含 WO-2026-0001 或 WO-CRYO-87001 形式工单编号的问题",
                "hint": "路由识别到的工单编号",
            },
            returns="确定性核验结论、SQL与文档证据、每步处理记录",
            cost_hint="中",
        )

    def readiness(self) -> tuple[bool, str]:
        """Check the isolated demo route without generating an answer or exposing its DSN."""
        if not self._dsn:
            return False, "demo_database_not_configured"

        documents_dir = Path(
            os.environ.get("CROSS_SOURCE_DEMO_DOCS_DIR", str(_DOCS_DIR))
        )
        if not documents_dir.is_dir() or not any(documents_dir.glob("*.md")):
            return False, "demo_documents_unavailable"

        try:
            # Probe the same read-only SQL path as a real request using a fixed,
            # shipped sample. Keep the probe bounded and never return DB errors.
            rows, events = self._load_records(
                "WO-2026-0001",
                _RunBudget(min(self._time_budget_ms, 3000)),
            )
        except Exception:  # noqa: BLE001 - readiness must not reveal connection details
            return False, "demo_database_unavailable"

        if not rows:
            return False, "demo_seed_work_order_missing"
        if not events:
            return False, "demo_seed_events_missing"
        return True, "ready"

    def probe_demo_case_readiness(
        self,
        work_order_id: str = "WO-2026-0001",
        contract_id: str = "HT-MNT-2026-001",
    ) -> bool:
        """Check the isolated homepage sample without writes or model calls."""
        docs_dir = Path(
            os.environ.get("CROSS_SOURCE_DEMO_DOCS_DIR", str(_DOCS_DIR))
        ).expanduser().resolve()
        if not self._dsn or not (docs_dir / f"{contract_id}.md").is_file():
            return False

        try:
            rows, events = self._load_records(work_order_id, _RunBudget(2500))
            if not rows or rows[0].get("matched_equipment_id") is None:
                return False

            if any(
                row.get("mapped_contract_id")
                and row.get("contract_customer_id")
                and row.get("customer_id") != row.get("contract_customer_id")
                for row in rows
            ):
                return False
            if any(
                row.get("mapped_contract_id")
                and mapping_basis_status(row.get("mapping_basis")) != "allowed"
                for row in rows
            ):
                return False

            work_date = self._work_order_date(rows[0])
            applicable = self._applicable_contracts(rows, work_date)
            if len(applicable) != 1 or applicable[0].get("mapped_contract_id") != contract_id:
                return False

            accepted = [event for event in events if event.get("event_type") == "accepted"]
            restored = [event for event in events if event.get("event_type") == "service_restored"]
            if len(accepted) != 1 or len(restored) != 1:
                return False
            accepted_at = accepted[0].get("event_at")
            restored_at = restored[0].get("event_at")
            if accepted_at is None or restored_at is None or restored_at < accepted_at:
                return False

            candidates = self._ensure_doc_retriever().retrieve(
                contract_id + " 修复完成 服务恢复时限 受理时刻",
                top_k=8,
                doc_id=contract_id,
            )
            pattern = _LIMIT_PATTERNS["service_restored"][1]
            limits = {
                int(match.group(1))
                for candidate, _ref in candidates
                for match in re.finditer(pattern, candidate.chunk.text)
            }
            return limits == {8}
        except Exception:  # noqa: BLE001 - expose only a safe availability state
            return False

    def run(self, payload: dict[str, Any]) -> ToolResult:
        question = payload.get("question", "")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("cross_source_demo 需要非空 question")
        match = WORK_ORDER_ID_SEARCH_RE.search(question)
        if match is None:
            match = WORK_ORDER_ID_SEARCH_RE.search(str(payload.get("hint", "")))
        if match is None:
            return self._failure(
                "问题中没有可识别的演示工单编号，例如 WO-2026-0001 或 WO-CRYO-87001。",
                "missing_work_order_id",
            )
        work_order_id = match.group(1).upper()

        if not self._dsn:
            return self._failure(
                "演示数据库尚未配置；请设置 CROSS_SOURCE_DEMO_DSN 并载入独立演示数据。",
                "demo_database_not_configured",
            )

        budget = _RunBudget(self._time_budget_ms)
        sql_started = time.perf_counter()
        try:
            rows, events = self._load_records(work_order_id, budget)
        except Exception as exc:  # 数据库/连接故障不伪装成空结果
            elapsed_ms = (time.perf_counter() - sql_started) * 1000.0
            try:
                budget.remaining_ms()
                budget_expired = False
            except _BudgetExceeded:
                budget_expired = True
            is_statement_timeout = type(exc).__name__ == "QueryCanceled"
            failed_state = (
                "budget_exhausted"
                if isinstance(exc, _BudgetExceeded) or budget_expired or is_statement_timeout
                else "failed"
            )
            step = TraceStep(
                step_id=1,
                tool="cross_source_sql",
                input={"work_order_id": work_order_id, "query": _WORK_ORDER_SQL},
                output={
                    "status": failed_state,
                    "error_type": type(exc).__name__,
                    "time_budget_ms": self._time_budget_ms,
                    "model_calls": 0,
                },
                elapsed_ms=elapsed_ms,
                hit=[],
            )
            return ToolResult(
                data=(
                    "本次核验达到总时限，已停止后续查询。"
                    if failed_state == "budget_exhausted"
                    else "独立演示数据库查询失败，当前无法判断该工单；请检查演示数据库和连接配置。"
                ),
                trace=[step],
                refs=[],
            )

        sql_elapsed = (time.perf_counter() - sql_started) * 1000.0
        if not rows:
            absence_check = make_absence_check(
                "cross_source_demo.work_orders",
                {"work_order_id": work_order_id},
                0,
            )
            absence_ref = SqlRef(
                kind="sql",
                table="cross_source_demo.work_orders",
                column="work_order_id",
                row_pk=None,
                snippet=(
                    f"按 work_order_id={work_order_id} 查询工单表未返回记录；row_count=0。"
                    "这只能说明当前数据库中没有匹配记录，不能证明业务上未发生。"
                ),
            )
            step = TraceStep(
                step_id=1,
                tool="cross_source_sql",
                input={
                    "work_order_id": work_order_id,
                    "query": _WORK_ORDER_SQL,
                    "filters": {"work_order_id": work_order_id},
                },
                output={
                    "status": "not_found",
                    "row_count": 0,
                    "absence_checks": [absence_check],
                },
                elapsed_ms=sql_elapsed,
                hit=[absence_ref],
            )
            return ToolResult(
                data=(
                    f"没有在演示数据库中找到工单 {work_order_id}；按该编号查询返回 0 行。"
                    "这只能说明当前数据库没有匹配记录，不能证明业务上未发生该工单。"
                ),
                trace=[step],
                refs=[absence_ref],
            )

        base = rows[0]
        absence_checks = self._query_absence_checks(base, rows, events)
        if base.get("matched_equipment_id") is None:
            refs = self._missing_equipment_refs(base)
            sql_step = TraceStep(
                step_id=1,
                tool="cross_source_sql",
                input={"work_order_id": work_order_id, "query": _WORK_ORDER_SQL},
                output={
                    "status": "missing_reference_data",
                    "row_count": len(rows),
                    "equipment_record_found": False,
                    "event_types": sorted({event["event_type"] for event in events}),
                    "absence_checks": absence_checks,
                    "time_budget_ms": self._time_budget_ms,
                    "model_calls": 0,
                },
                elapsed_ms=sql_elapsed,
                hit=refs,
            )
            alignment = TraceStep(
                step_id=2,
                tool="cross_source_alignment",
                input={"work_order_id": work_order_id, "equipment_id": base["equipment_id"]},
                output={
                    "status": "missing_reference_data",
                    "stop_reason_code": "equipment_master_missing",
                    "reason": f"工单存在，但设备主数据中未找到设备 {base['equipment_id']}，无法核验合同关系。",
                },
                elapsed_ms=0.0,
                hit=refs,
            )
            return ToolResult(
                data=(
                    f"已找到工单 {work_order_id}，但设备主数据中没有设备 "
                    f"{base['equipment_id']}；无法核验合同适用性和修复时限。"
                ),
                trace=[sql_step, alignment],
                refs=refs,
            )

        sql_refs = self._sql_refs(base, rows, events)
        mapping_refs = [
            ref for ref in sql_refs
            if ref.table != "cross_source_demo.work_order_events"
        ]
        sql_step = TraceStep(
            step_id=1,
            tool="cross_source_sql",
            input={"work_order_id": work_order_id, "query": _WORK_ORDER_SQL},
            output={
                "work_order_id": work_order_id,
                "equipment_id": base["equipment_id"],
                "mapping_candidates": len({r["mapped_contract_id"] for r in rows if r["mapped_contract_id"]}),
                "event_types": [event["event_type"] for event in events],
                "absence_checks": absence_checks,
                "time_budget_ms": self._time_budget_ms,
                "document_attempt_limit": self._doc_attempt_limit,
                "model_calls": 0,
            },
            elapsed_ms=sql_elapsed,
            hit=sql_refs,
        )

        event_map = {row["event_type"]: row["event_at"] for row in events}
        scope_conflicts = [
            row for row in rows
            if row["mapped_contract_id"]
            and row["contract_customer_id"]
            and row["customer_id"] != row["contract_customer_id"]
        ]
        if scope_conflicts:
            row = scope_conflicts[0]
            reason = (
                f"设备客户 {row['customer_id']} 与候选合同 {row['mapped_contract_id']} "
                f"的客户 {row['contract_customer_id']} 不一致；来源关系冲突，停止判断。"
            )
            validation = TraceStep(
                step_id=2,
                tool="cross_source_alignment",
                input={
                    "work_order_id": work_order_id,
                    "equipment_id": base["equipment_id"],
                    "equipment_customer_id": row["customer_id"],
                    "contract_id": row["mapped_contract_id"],
                    "contract_customer_id": row["contract_customer_id"],
                },
                output={
                    "status": "customer_scope_conflict",
                    "stop_reason_code": "customer_scope_conflict",
                    "reason": reason,
                },
                elapsed_ms=0.0,
                hit=mapping_refs,
            )
            selected_sql_step = sql_step.model_copy(update={"hit": mapping_refs})
            return ToolResult(
                data=f"无法判断工单 {work_order_id} 的合同适用性：{reason}",
                trace=[selected_sql_step, validation],
                refs=mapping_refs,
            )

        unverified_mappings = [
            row for row in rows
            if row["mapped_contract_id"]
            and mapping_basis_status(row["mapping_basis"]) != "allowed"
        ]
        if unverified_mappings:
            row = unverified_mappings[0]
            approved_mapping_bases = sorted(allowed_mapping_bases())
            reason = (
                f"候选映射 {row['mapped_contract_id']}—{base['equipment_id']} 的依据 "
                f"{row['mapping_basis']!r} 未列入本演示的受控映射依据清单 "
                f"（仅允许：{', '.join(approved_mapping_bases)}）；停止判断。"
            )
            validation = TraceStep(
                step_id=2,
                tool="cross_source_alignment",
                input={"work_order_id": work_order_id, "equipment_id": base["equipment_id"]},
                output={
                    "status": "mapping_basis_not_allowed",
                    "stop_reason_code": "mapping_basis_unapproved",
                    "observed_mapping_basis": row["mapping_basis"],
                    "approved_mapping_bases": approved_mapping_bases,
                    "reason": reason,
                },
                elapsed_ms=0.0,
                hit=mapping_refs,
            )
            selected_sql_step = sql_step.model_copy(update={"hit": mapping_refs})
            return ToolResult(
                data=f"无法判断工单 {work_order_id} 的合同适用性：{reason}",
                trace=[selected_sql_step, validation],
                refs=mapping_refs,
            )

        work_date = self._work_order_date(base)
        applicable = self._applicable_contracts(rows, work_date)
        if len(applicable) == 0:
            mapped = [r for r in rows if r["mapped_contract_id"]]
            if mapped:
                validity_checks = self._contract_validity_checks(mapped, work_date)
                reasons = []
                for check in validity_checks:
                    failed = []
                    if not check["contract_record_found"]:
                        failed.append(
                            f"合同主表未找到合同 {check['contract_id']} 的匹配记录，"
                            "无法核实该合同是否适用"
                        )
                    elif not check["contract_active"]:
                        failed.append(
                            f"合同状态为 {check['contract_status'] or '未提供'}，不作为有效适用合同"
                        )
                    if not check["mapping_covers_work_date"]:
                        failed.append(
                            "设备—合同映射有效期"
                            f"{check['mapping_valid_from'] or '未提供'} 至 "
                            f"{check['mapping_valid_to'] or '未提供'}不覆盖工单日期 {work_date}"
                        )
                    if check["contract_record_found"] and not check["contract_covers_work_date"]:
                        failed.append(
                            "合同有效期"
                            f"{check['contract_effective_from'] or '未提供'} 至 "
                            f"{check['contract_effective_to'] or '未提供'}不覆盖工单日期 {work_date}"
                        )
                    reasons.append(f"候选合同 {check['contract_id']}：" + "；".join(failed))
                reason = "没有候选合同同时满足映射和合同两种有效期核验。" + "；".join(reasons)
                state = "no_active_contract"
                stop_reason_code = None
                if any(not item["contract_record_found"] for item in validity_checks):
                    stop_reason_code = "mapping_unresolved"
                elif any(
                    not item["mapping_covers_work_date"]
                    or not item["contract_covers_work_date"]
                    for item in validity_checks
                ):
                    stop_reason_code = "validity_window_not_cover"
                checks_by_id = {item["contract_id"]: item for item in validity_checks}
                contextual_rows = {
                    row["mapped_contract_id"]: row
                    for row in mapped
                    if row["mapped_contract_id"] in checks_by_id
                    and checks_by_id[row["mapped_contract_id"]]["mapping_covers_work_date"]
                    and checks_by_id[row["mapped_contract_id"]]["contract_covers_work_date"]
                    and not checks_by_id[row["mapped_contract_id"]]["contract_active"]
                }
                if len(contextual_rows) == 1:
                    return self._inactive_contract_context_result(
                        work_order_id=work_order_id,
                        question=question,
                        contract_row=next(iter(contextual_rows.values())),
                        base=base,
                        reason=reason,
                        validity_checks=validity_checks,
                        sql_step=sql_step,
                        sql_refs=sql_refs,
                        budget=budget,
                    )
            else:
                validity_checks = []
                mapping_refs.append(
                    SqlRef(
                        kind="sql",
                        table="cross_source_demo.contract_equipment",
                        column="contract_id/equipment_id",
                        row_pk=None,
                        snippet=(
                            f"按设备编号 {base['equipment_id']} 查询合同设备映射未返回记录；"
                            f"过滤条件 equipment_id={base['equipment_id']}，mapping_candidates=0；"
                            "不能按设备名称推断合同关系。"
                        ),
                    )
                )
                reason = (
                    f"工单设备编号为 {base['equipment_id']}，但映射表中没有该设备对应的合同；"
                    "不能按设备名称猜测适用合同。"
                )
                state = "missing_object_mapping"
                stop_reason_code = "mapping_unresolved"
            answer = f"无法判断工单 {work_order_id} 是否满足合同修复时限：{reason}"
            answer += self._requested_validity_summary(question, validity_checks)
            validation = TraceStep(
                step_id=2,
                tool="cross_source_alignment",
                input={"work_order_id": work_order_id, "work_date": str(work_date)},
                output={
                    "status": state,
                    "reason": reason,
                    "validity_checks": validity_checks,
                    **({"stop_reason_code": stop_reason_code} if stop_reason_code else {}),
                },
                elapsed_ms=0.0,
                hit=mapping_refs,
            )
            selected_sql_step = sql_step.model_copy(update={"hit": mapping_refs})
            return ToolResult(data=answer, trace=[selected_sql_step, validation], refs=mapping_refs)

        if len(applicable) > 1:
            contract_ids = sorted({r["mapped_contract_id"] for r in applicable})
            answer = (
                f"无法唯一判断工单 {work_order_id} 的合同修复时限：该设备在工单日期对应多个有效合同"
                f"（{'、'.join(contract_ids)}），需要先确认适用合同。"
            )
            validation = TraceStep(
                step_id=2,
                tool="cross_source_alignment",
                input={"work_order_id": work_order_id},
                output={
                    "status": "ambiguous_active_contracts",
                    "stop_reason_code": "ambiguous_active_contracts",
                    "contract_ids": contract_ids,
                },
                elapsed_ms=0.0,
                hit=mapping_refs,
            )
            selected_sql_step = sql_step.model_copy(update={"hit": mapping_refs})
            return ToolResult(data=answer, trace=[selected_sql_step, validation], refs=mapping_refs)

        contract = applicable[0]
        contract_id = contract["mapped_contract_id"]
        doc_started = time.perf_counter()
        try:
            doc_ref, limits, doc_hit_count, attempts = self._retrieve_contract_limits(
                contract_id, budget, base["equipment_name"]
            )
        except _BudgetExceeded as exc:
            doc_elapsed = (time.perf_counter() - doc_started) * 1000.0
            doc_step = TraceStep(
                step_id=2,
                tool="cross_source_doc_retrieval",
                input={"contract_id": contract_id, "attempt_limit": self._doc_attempt_limit},
                output={
                    "status": "budget_exhausted",
                    "error_type": type(exc).__name__,
                    "attempts": exc.attempts,
                    "time_budget_ms": self._time_budget_ms,
                },
                elapsed_ms=doc_elapsed,
                hit=[],
            )
            answer = f"已找到工单 {work_order_id} 的有效合同 {contract_id}，但本次核验达到总时限；因此不判断是否达标。"
            validation = TraceStep(
                step_id=3,
                tool="cross_source_alignment",
                input={"work_order_id": work_order_id, "contract_id": contract_id},
                output={"status": "budget_exhausted"},
                elapsed_ms=0.0,
                hit=mapping_refs,
            )
            selected_sql_step = sql_step.model_copy(update={"hit": mapping_refs})
            return ToolResult(
                data=answer,
                trace=[selected_sql_step, doc_step, validation],
                refs=mapping_refs,
            )
        except _ConflictingContractLimit as exc:
            doc_elapsed = (time.perf_counter() - doc_started) * 1000.0
            doc_step = TraceStep(
                step_id=2,
                tool="cross_source_doc_retrieval",
                input={"contract_id": contract_id, "attempt_limit": self._doc_attempt_limit},
                output={
                    "status": "conflicting_contract_limits",
                    "attempts": exc.attempts,
                },
                elapsed_ms=doc_elapsed,
                hit=[exc.ref],
            )
            validation = TraceStep(
                step_id=3,
                tool="cross_source_alignment",
                input={"work_order_id": work_order_id, "contract_id": contract_id},
                output={
                    "status": "insufficient_contract_evidence",
                    "reason": "conflicting_repair_limits_unresolved",
                },
                elapsed_ms=0.0,
                hit=[*sql_refs, exc.ref],
            )
            return ToolResult(
                data=(
                    f"工单 {work_order_id}（设备 {base['equipment_id']}）：合同 {contract_id} "
                    "存在多个适用修复时限且无法根据明确的适用范围和优先级消解，不能判断是否达标。"
                ),
                trace=[sql_step, doc_step, validation],
                refs=[*sql_refs, exc.ref],
            )
        except Exception as exc:
            doc_elapsed = (time.perf_counter() - doc_started) * 1000.0
            is_missing_evidence = isinstance(exc, _DocumentEvidenceUnavailable)
            attempts = exc.attempts if is_missing_evidence else []
            doc_status = "insufficient_document_evidence" if is_missing_evidence else "failed"
            failure_state = doc_status
            doc_step = TraceStep(
                step_id=2,
                tool="cross_source_doc_retrieval",
                input={"contract_id": contract_id, "attempt_limit": self._doc_attempt_limit},
                output={
                    "status": doc_status,
                    "error_type": type(exc).__name__,
                    "attempts": attempts,
                },
                elapsed_ms=doc_elapsed,
                hit=[],
            )
            answer = (
                f"已找到工单 {work_order_id} 的有效合同 {contract_id}，但"
                + (
                    f"在 {self._doc_attempt_limit} 次定向检索内未取得可核验的合同条款；因此不判断是否达标。"
                    if is_missing_evidence
                    else "文档检索失败；因此不判断是否达标。"
                )
            )
            if is_missing_evidence:
                answer += self._requested_validity_summary(
                    question, self._contract_validity_checks(rows, work_date)
                )
                answer += self._requested_event_interval_summary(question, event_map)
                answer += "\n本次检索未取得可引用的合同原句；这表示检索未命中，不表示合同没有相关条款。"
            validation = TraceStep(
                step_id=3,
                tool="cross_source_alignment",
                input={"work_order_id": work_order_id, "contract_id": contract_id},
                output={"status": failure_state},
                elapsed_ms=0.0,
                hit=mapping_refs,
            )
            selected_sql_step = sql_step.model_copy(update={"hit": mapping_refs})
            return ToolResult(
                data=answer,
                trace=[selected_sql_step, doc_step, validation],
                refs=mapping_refs,
            )

        doc_elapsed = (time.perf_counter() - doc_started) * 1000.0
        doc_step = TraceStep(
            step_id=2,
            tool="cross_source_doc_retrieval",
            input={"contract_id": contract_id},
            output={"status": "retrieved", "doc_id": contract_id, "hit_count": doc_hit_count,
                    "limits": limits, "attempts": attempts,
                    "repair_limit_status": (
                        "explicitly_not_defined"
                        if any(item.get("status") == "required_metric_absence_explicit" for item in attempts)
                        else "found"
                    ),
                    "attempt_limit": self._doc_attempt_limit},
            elapsed_ms=doc_elapsed,
            hit=[doc_ref],
        )

        explicitly_unspecified = (
            "service_restored" not in limits
            and any(item.get("status") == "required_metric_absence_explicit" for item in attempts)
        )
        if explicitly_unspecified:
            conclusion = (
                f"合同 {contract_id} 原文明确未约定服务恢复总时限；"
                "因此不进行修复耗时达标比较，也不能判断是否超时。"
            )
            answer = f"工单 {work_order_id}（设备 {base['equipment_id']}）：{conclusion}"
            answer += self._requested_fact_summary(question, base, contract, doc_ref)
            validation = TraceStep(
                step_id=3,
                tool="cross_source_alignment",
                input={"work_order_id": work_order_id, "contract_id": contract_id},
                output={
                    "status": "insufficient_contract_evidence",
                    "reason": "contract_explicitly_omits_service_restored_limit",
                    "metric_checks": [],
                    "missing_events": [],
                },
                elapsed_ms=0.0,
                hit=[*sql_refs, doc_ref],
            )
            return ToolResult(
                data=answer,
                trace=[sql_step, doc_step, validation],
                refs=[*sql_refs, doc_ref],
            )

        comparisons: list[str] = []
        statuses: list[dict[str, Any]] = []
        invalid_metrics: list[dict[str, Any]] = []
        event_names = {
            "first_response": ("accepted", "first_response"),
            "onsite_arrival": ("accepted", "onsite_arrival"),
            "service_restored": ("accepted", "service_restored"),
        }
        for event_type, (start_name, end_name) in event_names.items():
            limit_value = limits.get(event_type)
            start_at = event_map.get(start_name)
            end_at = event_map.get(end_name)
            if limit_value is None or start_at is None or end_at is None:
                continue
            elapsed = end_at - start_at
            if elapsed < timedelta(0):
                invalid_metrics.append({
                    "metric": event_type,
                    "reason": "end_event_before_start_event",
                    "evidence_ids": [
                        work_order_id + "|" + start_name,
                        work_order_id + "|" + end_name,
                        "doc:" + contract_id,
                    ],
                })
                comparisons.append(f"{self._limit_label(event_type)}事件时间顺序异常，未判断")
                continue
            unit, limit = limit_value
            limit_delta = timedelta(minutes=limit) if unit == "minutes" else timedelta(hours=limit)
            within = elapsed <= limit_delta
            over = elapsed - limit_delta
            state = "满足" if within else "超时 " + self._format_duration(over)
            comparisons.append(
                f"{self._limit_label(event_type)} {self._format_duration(elapsed)}"
                f"（要求不超过 {limit}{'分钟' if unit == 'minutes' else '小时'}，{state}）"
            )
            statuses.append(
                {"metric": event_type, "elapsed_seconds": elapsed.total_seconds(),
                 "limit": limit, "unit": unit, "within_limit": within,
                 "start_event": start_name, "end_event": end_name,
                 "evidence_ids": [
                     work_order_id + "|" + start_name,
                     work_order_id + "|" + end_name,
                     "doc:" + contract_id,
                 ]}
            )

        missing_events = [name for name in ("accepted", "service_restored") if name not in event_map]
        absence_refs = self._missing_event_refs(work_order_id, events, missing_events)
        if missing_events:
            labels = {"accepted": "受理（accepted）", "service_restored": "服务恢复（service_restored）"}
            conclusion = "缺少" + "、".join(labels[name] for name in missing_events)
            conclusion += "事件，无法计算修复耗时，不能判断修复时限是否达标。"
            if "accepted" in missing_events:
                conclusion += "当前数据库查询未返回 accepted 记录，只能说明本数据中缺少该记录，不能证明业务事件没有发生。"
            if "service_restored" in missing_events:
                conclusion += "当前数据库查询未返回 service_restored 记录，只能说明本数据中缺少该记录，不能证明服务在现实中尚未恢复。"
            status = "missing_required_event"
        elif not any(item["metric"] == "service_restored" for item in statuses):
            conclusion = "工单事件时间顺序异常，无法可靠判断修复时限。"
            status = "invalid_event_order"
        else:
            repair = next(item for item in statuses if item["metric"] == "service_restored")
            conclusion = "；".join(comparisons) if comparisons else "没有取得可比较的服务时限。"
            conclusion = "合同 " + contract_id + " 的适用条款已按工单日期核对；" + conclusion + "。"
            status = "repair_within_limit" if repair["within_limit"] else "repair_over_limit"

        answer = f"工单 {work_order_id}（设备 {base['equipment_id']}）：{conclusion}"
        if missing_events:
            answer += self._requested_event_interval_summary(question, event_map)
        if "假设" in question and "题设" in question:
            answer += "（按题设假设条件计算）"
        answer += self._requested_fact_summary(question, base, contract, doc_ref)
        final_refs = [*sql_refs, *absence_refs, doc_ref]
        validation = TraceStep(
            step_id=3,
            tool="cross_source_alignment",
            input={"work_order_id": work_order_id, "contract_id": contract_id},
            output={
                "status": status,
                **({"stop_reason_code": "key_event_missing"} if missing_events else {}),
                **({"stop_reason_code": "event_time_reversed"}
                   if status == "invalid_event_order" else {}),
                "metric_checks": statuses,
                "invalid_metrics": invalid_metrics,
                "missing_events": missing_events,
                "absence_checks": absence_checks,
            },
            elapsed_ms=0.0,
            hit=final_refs,
        )
        return ToolResult(data=answer, trace=[sql_step, doc_step, validation], refs=final_refs)

    @staticmethod
    def _requested_fact_summary(
        question: str,
        base: dict[str, Any],
        contract: dict[str, Any],
        doc_ref: DocRef,
    ) -> str:
        """Surface asked-for source facts already retrieved, without inferring new facts."""
        relational_terms = (
            "有效期", "有效", "适用", "范围", "站点", "映射", "映射期", "合同期",
            "客户", "合同状态", "状态字段", "纳入服务", "覆盖工单日期"
        )
        asks_relations = any(term in question for term in relational_terms)
        asks_scope_conclusion = any(
            term in question
            for term in ("服务范围", "纳入服务", "适用范围", "在范围内", "是否适用")
        )
        facts: list[str] = []
        if asks_relations:
            equipment_name = str(base["equipment_name"])
            site_match = re.fullmatch(r"(.+?)[（(]([^（）()]+)[）)]", equipment_name)
            equipment = (
                f"设备 {base['equipment_id']}（{site_match.group(1)}），站点 {site_match.group(2)}"
                if site_match
                else f"设备 {base['equipment_id']}（{equipment_name}）"
            )
            work_date = CrossSourceDemoTool._work_order_date(base)
            mapping_window = (
                f"设备—合同映射有效期 {contract['mapping_valid_from']} 至 "
                f"{contract['mapping_valid_to']}"
            )
            contract_window = (
                f"合同 {contract['mapped_contract_id']} 有效期 "
                f"{contract['contract_effective_from']} 至 {contract['contract_effective_to']}，"
                f"状态 {contract['contract_status']}"
            )
            facts.append(
                f"结构化核对：{equipment}；工单日期 {work_date}；"
                f"{mapping_window}；映射期覆盖工单日期：是；"
                f"{contract_window}；合同期覆盖工单日期：是。"
            )
            if asks_scope_conclusion:
                facts.append("据此，结构化映射和有效期均覆盖该工单日期，设备属于该有效合同的适用范围。")

        detail_terms = (
            "复测", "校准", "采样", "标准液", "读数", "最高值", "允许差", "差是多少"
        )
        scope_terms = ("服务范围", "纳入服务", "适用范围", "服务期", "覆盖")
        asks_details = any(term in question for term in detail_terms)
        asks_scope = any(term in question for term in scope_terms)
        if asks_details or asks_scope:
            equipment_id = str(base["equipment_id"])
            document_terms = ("复测", "校准", "采样", "标准液", "读数") if asks_details else ()
            scope_document_terms = ("服务范围", "纳入服务", "适用范围", "服务期", "覆盖") if asks_scope else ()
            relevant_lines = [
                line.strip()
                for line in doc_ref.snippet.splitlines()
                if line.strip()
                and equipment_id in line
                and any(term in line for term in (*document_terms, *scope_document_terms))
            ]
            if relevant_lines:
                facts.append("文档中的相关条款：" + " ".join(relevant_lines))

        if any(term in question for term in ("原句", "原文", "引用原文")):
            equipment_id = str(base["equipment_id"])
            quote_lines = [
                line.strip()
                for line in doc_ref.snippet.splitlines()
                if line.strip()
                and (
                    ("|" in line and any(term in line for term in ("首次响应", "现场到达", "修复完成")))
                    or equipment_id in line
                )
            ]
            if quote_lines:
                facts.append("合同文档原句：" + "；".join(f"“{line}”" for line in quote_lines))

        return "\n" + "\n".join(facts) if facts else ""

    @staticmethod
    def _requested_validity_summary(
        question: str, validity_checks: list[dict[str, Any]]
    ) -> str:
        """按题目所问分别报告映射期、合同期和状态；缺失记录不能伪装成日期不覆盖。"""
        requested_terms = (
            "有效期", "映射", "排期映射", "设备排期", "合同期", "日期范围",
            "日期覆盖", "覆盖工单日期", "工单日期", "工单当天", "状态字段", "合同状态"
        )
        if not validity_checks or not any(term in question for term in requested_terms):
            return ""

        def coverage(check: dict[str, Any], kind: str) -> str:
            if kind == "mapping":
                start = check.get("mapping_valid_from")
                end = check.get("mapping_valid_to")
                covers = check["mapping_covers_work_date"]
                missing_reason = "映射有效期字段缺失"
            else:
                if not check["contract_record_found"]:
                    return "无法核实（合同主记录缺失）"
                start = check.get("contract_effective_from")
                end = check.get("contract_effective_to")
                covers = check["contract_covers_work_date"]
                missing_reason = "合同有效期字段缺失"
            if start is None or end is None:
                return f"无法核实（{missing_reason}）"
            return "是" if covers else "否"

        windows = [
            f"合同 {check['contract_id']}：映射有效期 "
            f"{check.get('mapping_valid_from') or '未提供'} 至 "
            f"{check.get('mapping_valid_to') or '未提供'}；"
            f"映射期覆盖工单日期={coverage(check, 'mapping')}；"
            + (
                "合同有效期无法核实（合同主记录缺失）；"
                if not check["contract_record_found"]
                else (
                    f"合同有效期{check.get('contract_effective_from') or '未提供'} 至 "
                    f"{check.get('contract_effective_to') or '未提供'}；"
                )
            )
            + f"合同期覆盖工单日期={coverage(check, 'contract')}；"
            f"合同状态={check.get('contract_status') or '未提供'}"
            for check in validity_checks
        ]
        applicability = []
        for check in validity_checks:
            if (
                not check["contract_record_found"]
                or check.get("mapping_valid_from") is None
                or check.get("mapping_valid_to") is None
                or check.get("contract_effective_from") is None
                or check.get("contract_effective_to") is None
                or check.get("contract_status") is None
            ):
                state = "无法确认"
            else:
                state = (
                    "是"
                    if check["mapping_covers_work_date"]
                    and check["contract_covers_work_date"]
                    and check["contract_active"]
                    else "否"
                )
            applicability.append(f"合同 {check['contract_id']} 是否已核实同时满足两种日期窗口且状态有效：{state}")
        return (
            "\n有效期逐项判断：" + "；".join(windows) + "。"
            + "\n适用性直接结论：" + "；".join(applicability) + "。"
        )

    @staticmethod
    def _requested_event_interval_summary(
        question: str, event_map: dict[str, Any]
    ) -> str:
        """披露可从现有事件时间计算的间隔，并避免把非合同区间误作 SLA。"""
        asked_time = any(
            term in question
            for term in ("耗时", "用了多久", "间隔", "时长", "分别用了多久")
        )
        if not asked_time:
            return ""

        parts: list[str] = []
        if "accepted" in event_map:
            labels = {
                "first_response": "首次响应",
                "onsite_arrival": "现场到达",
                "service_restored": "修复完成",
            }
            for event_type, label in labels.items():
                end_at = event_map.get(event_type)
                if end_at is None:
                    continue
                elapsed = end_at - event_map["accepted"]
                if elapsed < timedelta(0):
                    parts.append(f"{label}事件早于受理事件，时间顺序异常")
                else:
                    parts.append(f"受理至{label}的记录间隔为 {CrossSourceDemoTool._format_duration(elapsed)}")
        elif event_map.get("first_response") is not None and event_map.get("service_restored") is not None:
            elapsed = event_map["service_restored"] - event_map["first_response"]
            if elapsed >= timedelta(0):
                parts.append(
                    "现有记录中 first_response 至 service_restored 的间隔为 "
                    f"{CrossSourceDemoTool._format_duration(elapsed)}；这不是以 accepted 为起点的合同修复耗时"
                )
        if not parts:
            return "\n当前事件记录不足以计算题目所问的时间间隔。"
        return "\n可计算的事件间隔（仅为记录时间差，需按合同定义判断是否可作 SLA）：" + "；".join(parts) + "。"

    @staticmethod
    def _work_order_date(base: dict[str, Any]) -> date:
        """Resolve a timestamp to the contract's local calendar date for validity checks."""
        opened_at = base["opened_at"]
        timezone_name = base.get("contract_timezone")
        if opened_at.tzinfo is None or not timezone_name:
            return opened_at.date()
        return opened_at.astimezone(
            CrossSourceDemoTool._parse_contract_timezone(str(timezone_name))
        ).date()

    @staticmethod
    def _parse_contract_timezone(timezone_name: str) -> tzinfo:
        """Accept IANA time zones and explicit UTC offsets from contract rows."""
        normalized = timezone_name.strip()
        offset_match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", normalized)
        if offset_match is None:
            return ZoneInfo(normalized)

        hours = int(offset_match.group(2))
        minutes = int(offset_match.group(3))
        if hours > 14 or minutes > 59 or (hours == 14 and minutes != 0):
            raise ValueError(f"contract timezone offset is out of range: {timezone_name}")
        offset = timedelta(hours=hours, minutes=minutes)
        if offset_match.group(1) == "-":
            offset = -offset
        return timezone(offset, name=normalized)

    @staticmethod
    def _missing_event_refs(
        work_order_id: str, events: list[dict[str, Any]], missing_events: list[str]
    ) -> list[SqlRef]:
        """仅在完整事件查询成功后说明缺项；不存在的事件不能伪造记录主键。"""
        present = sorted({row["event_type"] for row in events})
        absent = [name for name in missing_events if name not in present]
        if not absent:
            return []
        # 一个完整查询对应一条零结果引用，避免公共引用去重时合并掉第二个缺项。
        absent_text = "；".join(f"event_type={name} 未返回记录" for name in absent)
        return [
            SqlRef(
                kind="sql", table="cross_source_demo.work_order_events",
                column="event_type/event_at", row_pk=None,
                snippet=(
                    f"按 work_order_id={work_order_id} 查询全部事件成功；"
                    f"返回 event_types={present}；{absent_text}。"
                    "此处表示记录缺失，不证明业务事件没有发生。"
                ),
            )
        ]

    def _load_records(
        self, work_order_id: str, budget: _RunBudget
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not self._dsn:
            raise RuntimeError("CROSS_SOURCE_DEMO_DSN is empty")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("psycopg is unavailable") from exc

        connect_timeout = max(1, math.ceil(budget.remaining_ms() / 1000))
        with psycopg.connect(self._dsn, connect_timeout=connect_timeout) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                # 所有后续查询都处于 PostgreSQL 只读事务；用户输入只作为参数值传入。
                cur.execute("SET TRANSACTION READ ONLY")
                self._set_statement_timeout(cur, budget)
                cur.execute("SELECT current_database() AS database_name")
                database_name = cur.fetchone()["database_name"]
                if database_name != "cross_source_demo":
                    raise RuntimeError(
                        "CROSS_SOURCE_DEMO_DSN 必须指向独立 cross_source_demo 数据库"
                    )
                self._set_statement_timeout(cur, budget)
                cur.execute(_WORK_ORDER_SQL, (work_order_id,))
                rows = list(cur.fetchall())
                self._set_statement_timeout(cur, budget)
                cur.execute(_EVENTS_SQL, (work_order_id,))
                events = list(cur.fetchall())
        return rows, events

    @staticmethod
    def _set_statement_timeout(cursor: Any, budget: _RunBudget) -> None:
        timeout_value = f"{budget.remaining_ms()}ms"
        cursor.execute("SELECT set_config('statement_timeout', %s, true)", (timeout_value,))

    @staticmethod
    def _contract_validity_checks(
        rows: list[dict[str, Any]], work_date: date
    ) -> list[dict[str, Any]]:
        """Separately evaluate the equipment mapping and contract date windows."""
        checks = []
        seen: set[str] = set()
        for row in rows:
            contract_id = row["mapped_contract_id"]
            if not contract_id or contract_id in seen:
                continue
            seen.add(contract_id)
            mapping_from = row["mapping_valid_from"]
            mapping_to = row["mapping_valid_to"]
            contract_from = row["contract_effective_from"]
            contract_to = row["contract_effective_to"]
            checks.append({
                "contract_id": contract_id,
                "contract_record_found": row.get("matched_contract_id") == contract_id,
                "contract_status": row.get("contract_status"),
                "contract_active": is_active_contract_status(row.get("contract_status")),
                "mapping_valid_from": str(mapping_from) if mapping_from is not None else None,
                "mapping_valid_to": str(mapping_to) if mapping_to is not None else None,
                "contract_effective_from": str(contract_from) if contract_from is not None else None,
                "contract_effective_to": str(contract_to) if contract_to is not None else None,
                "mapping_covers_work_date": (
                mapping_from is not None and mapping_to is not None
                and mapping_from <= work_date <= mapping_to
                ),
                "contract_covers_work_date": (
                    contract_from is not None and contract_to is not None
                    and contract_from <= work_date <= contract_to
                ),
            })
        return checks

    @staticmethod
    def _applicable_contracts(rows: list[dict[str, Any]], work_date: date) -> list[dict[str, Any]]:
        checks = {
            check["contract_id"]: check
            for check in CrossSourceDemoTool._contract_validity_checks(rows, work_date)
        }
        applicable = []
        seen: set[str] = set()
        for row in rows:
            contract_id = row["mapped_contract_id"]
            check = checks.get(contract_id)
            if not contract_id or contract_id in seen or check is None:
                continue
            seen.add(contract_id)
            if (
                check["contract_active"]
                and check["mapping_covers_work_date"]
                and check["contract_covers_work_date"]
            ):
                applicable.append(row)
        return applicable

    def _inactive_contract_context_result(
        self,
        *,
        work_order_id: str,
        question: str,
        contract_row: dict[str, Any],
        base: dict[str, Any],
        reason: str,
        validity_checks: list[dict[str, Any]],
        sql_step: TraceStep,
        sql_refs: list[SqlRef],
        budget: _RunBudget,
    ) -> ToolResult:
        """Retrieve the inactive contract clause for trace context, never to apply its SLA."""
        contract_id = contract_row["mapped_contract_id"]
        status = contract_row.get("contract_status") or "未提供"
        doc_started = time.perf_counter()
        doc_ref: DocRef | None = None
        limits: dict[str, tuple[str, int]] = {}
        attempts: list[dict[str, Any]] = []
        hit_count = 0
        doc_status = "retrieved_context_only"
        error_type: str | None = None
        try:
            doc_ref, limits, hit_count, attempts = self._retrieve_contract_limits(
                contract_id, budget, base["equipment_name"]
            )
        except _BudgetExceeded as exc:
            doc_status = "budget_exhausted"
            attempts = exc.attempts
            error_type = type(exc).__name__
        except _DocumentEvidenceUnavailable as exc:
            doc_status = "insufficient_document_evidence"
            attempts = exc.attempts
            error_type = type(exc).__name__
        except _ConflictingContractLimit as exc:
            doc_status = "conflicting_contract_limits"
            attempts = exc.attempts
            doc_ref = exc.ref
            error_type = type(exc).__name__
        except Exception as exc:
            doc_status = "failed"
            error_type = type(exc).__name__

        doc_elapsed = (time.perf_counter() - doc_started) * 1000.0
        doc_hit = [doc_ref] if doc_ref is not None else []
        doc_step = TraceStep(
            step_id=2,
            tool="cross_source_doc_retrieval",
            input={"contract_id": contract_id, "purpose": "inactive_contract_context"},
            output={
                "status": doc_status,
                "doc_id": contract_id if doc_ref is not None else None,
                "hit_count": hit_count,
                "limits_context_only": limits,
                "attempts": attempts,
                "error_type": error_type,
            },
            elapsed_ms=doc_elapsed,
            hit=doc_hit,
        )
        repair_limit = limits.get("service_restored")
        if repair_limit is None:
            omission_was_explicit = any(
                attempt.get("status") == "required_metric_absence_explicit"
                for attempt in attempts
            )
            clause_context = (
                "合同文档明确未约定服务恢复总时限（仅作条款背景）"
                if omission_was_explicit else "未取得可引用的修复时限条款"
            )
        else:
            unit, value = repair_limit
            unit_label = "分钟" if unit == "minutes" else "小时"
            clause_context = f"合同文档列明修复完成时限为 {value}{unit_label}（仅作条款背景）"
        answer = (
            f"无法判断工单 {work_order_id} 是否满足有效合同的修复时限：合同 {contract_id} "
            f"状态为 {status}，不属于有效适用合同；{clause_context}，不据此判定达标或超时。"
        )
        answer += self._requested_validity_summary(question, validity_checks)
        if doc_ref is not None:
            answer += self._requested_fact_summary(question, base, contract_row, doc_ref)
        refs = [*sql_refs, *doc_hit]
        validation = TraceStep(
            step_id=3,
            tool="cross_source_alignment",
            input={
                "work_order_id": work_order_id,
                "contract_id": contract_id,
                "contract_status": status,
                "document_role": "context_only_not_applicable_sla",
            },
            output={
                "status": "no_active_contract",
                "reason": reason,
                "validity_checks": validity_checks,
                "document_used_for_sla_calculation": False,
                "metric_checks": [],
            },
            elapsed_ms=0.0,
            hit=refs,
        )
        return ToolResult(data=answer, trace=[sql_step, doc_step, validation], refs=refs)

    def _retrieve_contract_limits(
        self, contract_id: str, budget: _RunBudget, equipment_name: str = ""
    ) -> tuple[DocRef, dict[str, tuple[str, int]], int, list[dict[str, Any]]]:
        budget.remaining_ms()
        retriever = self._ensure_doc_retriever()
        try:
            budget.remaining_ms()
        except _BudgetExceeded as exc:
            raise _BudgetExceeded(str(exc)) from exc
        query_strategies = (
            (
                "primary_sla_query",
                contract_id + " 服务级别 首次响应 现场到达 修复完成 受理 服务恢复时限",
            ),
            (
                "targeted_repair_sla_retry",
                contract_id + " 修复完成 服务恢复 受理时刻 修复时限",
            ),
            (
                "targeted_event_definition_retry",
                contract_id + " 服务恢复事件 起止时间 修复完成时限",
            ),
        )
        attempts: list[dict[str, Any]] = []
        for attempt_number, (strategy, query) in enumerate(
            query_strategies[:self._doc_attempt_limit], start=1
        ):
            try:
                budget.remaining_ms()
            except _BudgetExceeded as exc:
                raise _BudgetExceeded(str(exc), attempts) from exc
            attempt_started = time.perf_counter()
            candidates = retriever.retrieve(query, top_k=8, doc_id=contract_id)
            try:
                budget.remaining_ms()
            except _BudgetExceeded as exc:
                attempts.append({
                    "attempt": attempt_number,
                    "strategy": strategy,
                    "status": "budget_exhausted_during_retrieval",
                    "candidate_count": len(candidates),
                    "elapsed_ms": (time.perf_counter() - attempt_started) * 1000.0,
                })
                raise _BudgetExceeded(str(exc), attempts) from exc
            for candidate, ref in candidates:
                text = candidate.chunk.text
                limits: dict[str, tuple[str, int]] = {}
                for event_type, (_label, pattern) in _LIMIT_PATTERNS.items():
                    matches = list(re.finditer(pattern, text))
                    if not matches:
                        continue
                    unit = "minutes" if event_type == "first_response" else "hours"
                    if event_type == "service_restored" and len({m.group(1) for m in matches}) > 1:
                        resolved = self._preferred_special_repair_limit(
                            text, matches, equipment_name
                        )
                        if resolved is None:
                            attempts.append({
                                "attempt": attempt_number,
                                "strategy": strategy,
                                "status": "conflicting_repair_limits_unresolved",
                                "candidate_count": len(candidates),
                                "elapsed_ms": (time.perf_counter() - attempt_started) * 1000.0,
                            })
                            conflict_ref = self._repair_limit_ref(candidate.chunk, ref)
                            raise _ConflictingContractLimit(attempts, conflict_ref)
                        limits[event_type] = (unit, resolved)
                    else:
                        limits[event_type] = (unit, int(matches[0].group(1)))
                if "service_restored" not in limits:
                    if _EXPLICITLY_UNSPECIFIED_REPAIR_SLA.search(text):
                        attempts.append({
                            "attempt": attempt_number,
                            "strategy": strategy,
                            "status": "required_metric_absence_explicit",
                            "candidate_count": len(candidates),
                            "elapsed_ms": (time.perf_counter() - attempt_started) * 1000.0,
                        })
                        focused_ref = self._repair_limit_ref(candidate.chunk, ref)
                        return focused_ref, limits, len(candidates), attempts
                    continue
                attempts.append({
                    "attempt": attempt_number,
                    "strategy": strategy,
                    "status": "required_metric_found",
                    "candidate_count": len(candidates),
                    "elapsed_ms": (time.perf_counter() - attempt_started) * 1000.0,
                })
                focused_ref = self._repair_limit_ref(candidate.chunk, ref)
                return focused_ref, limits, len(candidates), attempts
            attempts.append({
                "attempt": attempt_number,
                "strategy": strategy,
                "status": "required_metric_not_found",
                "candidate_count": len(candidates),
                "elapsed_ms": (time.perf_counter() - attempt_started) * 1000.0,
            })
        raise _DocumentEvidenceUnavailable(attempts)

    @staticmethod
    def _repair_limit_ref(chunk: Any, ref: DocRef) -> DocRef:
        """Keep all retrieved service-limit rows and their surrounding rule text visible."""
        text = getattr(chunk, "text", "")
        chunk_start = getattr(chunk, "char_start", None)
        chunk_end = getattr(chunk, "char_end", None)
        if re.search(_LIMIT_PATTERNS["service_restored"][1], text):
            updates: dict[str, Any] = {"snippet": text}
            excerpt_start = 0
            excerpt_end = len(text)
        else:
            omission = _EXPLICITLY_UNSPECIFIED_REPAIR_SLA.search(text)
            if omission is None:
                return ref
            excerpt_start = max(0, omission.start() - 100)
            excerpt_end = min(len(text), omission.end() + 100)
            updates = {"snippet": text[excerpt_start:excerpt_end]}
        if (
            isinstance(chunk_start, int)
            and isinstance(chunk_end, int)
            and chunk_end - chunk_start == len(text)
            and ref.char_start == chunk_start
            and ref.char_end == chunk_end
        ):
            updates["char_start"] = chunk_start + excerpt_start
            updates["char_end"] = chunk_start + excerpt_end
        return ref.model_copy(update=updates)

    @staticmethod
    def _preferred_special_repair_limit(
        text: str, matches: list[re.Match[str]], equipment_name: str
    ) -> int | None:
        """Resolve a distinct special SLA only when precedence and equipment scope are explicit."""
        if not re.search(r"特别约定[^。\n]{0,40}优先于[^。\n]{0,40}标准服务时限", text):
            return None
        special_rows: list[tuple[str, int]] = []
        active_heading = ""
        for line in text.splitlines():
            heading_match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
            if heading_match:
                active_heading = heading_match.group(1)
            row_match = re.search(_LIMIT_PATTERNS["service_restored"][1], line)
            if row_match and "特别约定" in active_heading:
                special_rows.append((active_heading, int(row_match.group(1))))
        if len(special_rows) != 1:
            return None
        heading, special_limit = special_rows[0]
        scope_match = re.search(r"[（(]([^）)]+)[）)]", heading)
        if not scope_match or not equipment_name.strip():
            return None
        scope = re.sub(r"(?:设备|一体机|终端|装置)$", "", scope_match.group(1).strip())
        equipment = re.sub(r"(?:设备|一体机|终端|装置)$", "", equipment_name.strip())
        common_length = 0
        for start in range(len(scope)):
            for end in range(start + 1, len(scope) + 1):
                if len(scope[start:end]) > common_length and scope[start:end] in equipment:
                    common_length = len(scope[start:end])
        if common_length < 3:
            return None
        distinct_values = {int(match.group(1)) for match in matches}
        return special_limit if special_limit in distinct_values else None

    def _ensure_doc_retriever(self) -> Bm25Retriever:
        if self._retriever is None:
            with self._retriever_lock:
                if self._retriever is None:
                    docs_dir = Path(
                        os.environ.get("CROSS_SOURCE_DEMO_DOCS_DIR", str(_DOCS_DIR))
                    ).expanduser().resolve()
                    paths = sorted(str(path) for path in docs_dir.glob("*.md"))
                    if not paths:
                        raise FileNotFoundError("演示合同文档目录为空：" + str(docs_dir))
                    chunks = []
                    for path in paths:
                        doc_id = Path(path).stem
                        blocks = extract_blocks(path)
                        sections = build_tree(path)
                        chunks.extend(build_chunks(blocks, sections, doc_id))
                    if not chunks:
                        raise ValueError("演示合同文档没有生成可检索切片")
                    self._retriever = Bm25Retriever(chunks)
        return self._retriever

    @staticmethod
    def _sql_refs(
        base: dict[str, Any], rows: list[dict[str, Any]], events: list[dict[str, Any]]
    ) -> list[SqlRef]:
        work_order_id = base["work_order_id"]
        work_order_fields = {
            "work_order_id": work_order_id,
            "equipment_id": base["equipment_id"],
            "opened_at": base["opened_at"],
        }
        equipment_fields = {
            "equipment_id": base["equipment_id"],
            "equipment_name": base["equipment_name"],
            "customer_id": base["customer_id"],
        }
        refs = [
            SqlRef(kind="sql", table="cross_source_demo.work_orders", column="/".join(work_order_fields),
                   row_pk=work_order_id,
                   snippet=(f"工单 {work_order_id} 对应设备 {base['equipment_id']}；开单时间 {base['opened_at']} "
                            f"（不替代 accepted 受理事件）。"
                            + _machine_sql_field_suffix(work_order_fields))),
            SqlRef(kind="sql", table="cross_source_demo.equipment", column="/".join(equipment_fields),
                   row_pk=base["equipment_id"],
                   snippet=(f"设备编号 {base['equipment_id']}，名称 {base['equipment_name']}，"
                            f"所属客户 {base['customer_id']}；名称不作为关联键。"
                            + _machine_sql_field_suffix(equipment_fields))),
        ]
        unique = {
            row["mapped_contract_id"]: row for row in rows if row["mapped_contract_id"]
        }
        for contract_id, row in sorted(unique.items()):
            mapping_fields = {
                "equipment_id": base["equipment_id"],
                "contract_id": contract_id,
                "valid_from": row.get("mapping_valid_from"),
                "valid_to": row.get("mapping_valid_to"),
                "mapping_basis": row.get("mapping_basis"),
            }
            refs.append(
                SqlRef(kind="sql", table="cross_source_demo.contract_equipment", column="/".join(mapping_fields),
                       row_pk=contract_id + "|" + base["equipment_id"],
                       snippet=(f"明确映射：合同 {contract_id} — 设备 {base['equipment_id']}；"
                                f"映射有效期 {row['mapping_valid_from']} 至 {row['mapping_valid_to']}；"
                                f"依据 {row['mapping_basis']}。"
                                + _machine_sql_field_suffix(mapping_fields)))
            )
            contract_found = row.get("matched_contract_id") == contract_id
            contract_fields = {
                "contract_id": contract_id,
                "customer_id": row.get("contract_customer_id"),
                "effective_from": row.get("contract_effective_from"),
                "effective_to": row.get("contract_effective_to"),
                "status": row.get("contract_status"),
            }
            refs.append(
                SqlRef(kind="sql", table="cross_source_demo.contracts",
                       column=("/".join(contract_fields) if contract_found else "contract_id"),
                       row_pk=(contract_id if contract_found else None),
                       snippet=(
                           f"合同 {contract_id} 客户 {row['contract_customer_id']}；有效期 "
                           f"{row['contract_effective_from']} 至 "
                           f"{row['contract_effective_to']}；状态字段 {row['contract_status']}。"
                           + _machine_sql_field_suffix(contract_fields)
                           if contract_found else (
                               f"映射指向合同 {contract_id}；按 contract_id={contract_id} 查询合同主表未返回匹配记录；"
                               "客户、有效期与状态字段均缺失，不能据此证明合同适用。"
                           )
                       ))
            )
        for event in events:
            event_fields = {
                "work_order_id": event["work_order_id"],
                "event_type": event["event_type"],
                "event_at": event["event_at"],
            }
            refs.append(
                SqlRef(kind="sql", table="cross_source_demo.work_order_events", column="/".join(event_fields),
                       row_pk=work_order_id + "|" + event["event_type"],
                       snippet=(f"工单 {work_order_id} 的 {event['event_type']} 事件时间为 "
                                f"{event['event_at']}。"
                                + _machine_sql_field_suffix(event_fields)))
            )
        return refs

    @staticmethod
    def _missing_equipment_refs(base: dict[str, Any]) -> list[SqlRef]:
        work_order_id = base["work_order_id"]
        equipment_id = base["equipment_id"]
        return [
            SqlRef(
                kind="sql",
                table="cross_source_demo.work_orders",
                column="work_order_id/equipment_id",
                row_pk=work_order_id,
                snippet=f"已查询到工单 {work_order_id}，其设备编号为 {equipment_id}。",
            ),
            SqlRef(
                kind="sql",
                table="cross_source_demo.equipment",
                column="equipment_id",
                row_pk=None,
                snippet=(
                    f"按 equipment_id={equipment_id} 查询设备主数据未返回记录（0 行）；"
                    "工单记录存在，不能将设备主数据缺失误判为工单不存在。"
                ),
            ),
        ]

    @staticmethod
    def _query_absence_checks(
        base: dict[str, Any],
        rows: list[dict[str, Any]],
        events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Record zero-row facts from the completed SQL result sets, never from prose."""
        checks: list[dict[str, Any]] = []
        equipment_id = str(base["equipment_id"])
        equipment_rows = [
            row for row in rows if row.get("matched_equipment_id") == equipment_id
        ]
        if not equipment_rows:
            checks.append(make_absence_check(
                "cross_source_demo.equipment", {"equipment_id": equipment_id}, 0
            ))

        mapping_rows = [row for row in rows if row.get("mapped_contract_id")]
        if not mapping_rows:
            checks.append(make_absence_check(
                "cross_source_demo.contract_equipment", {"equipment_id": equipment_id}, 0
            ))

        contract_ids = sorted({
            str(row["mapped_contract_id"])
            for row in rows if row.get("mapped_contract_id")
        })
        for contract_id in contract_ids:
            contract_rows = [
                row for row in rows if row.get("matched_contract_id") == contract_id
            ]
            if not contract_rows:
                checks.append(make_absence_check(
                    "cross_source_demo.contracts", {"contract_id": contract_id}, 0
                ))

        present_events = {str(event["event_type"]) for event in events}
        for required_event in ("accepted", "service_restored"):
            if required_event not in present_events:
                checks.append(make_absence_check(
                    "cross_source_demo.work_order_events",
                    {"work_order_id": str(base["work_order_id"]),
                     "event_type": required_event},
                    sum(1 for event in events
                        if event.get("work_order_id") == base["work_order_id"]
                        and event.get("event_type") == required_event),
                ))
        return checks

    @staticmethod
    def _limit_label(event_type: str) -> str:
        return _LIMIT_PATTERNS[event_type][0]

    @staticmethod
    def _format_duration(value: timedelta) -> str:
        total_minutes = int(value.total_seconds() // 60)
        hours, minutes = divmod(total_minutes, 60)
        if hours and minutes:
            return f"{hours}小时{minutes}分钟"
        if hours:
            return f"{hours}小时"
        return f"{minutes}分钟"

    @staticmethod
    def _failure(message: str, status: str) -> ToolResult:
        return ToolResult(
            data=message,
            trace=[TraceStep(
                step_id=1,
                tool="cross_source_demo",
                input={},
                output={"status": status},
                elapsed_ms=0.0,
                hit=[],
            )],
            refs=[],
        )
