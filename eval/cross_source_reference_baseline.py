"""阶段 B 记录复原的参考算法 A；不是历史阶段 B 源码。

仅用于与当前跨源演示候选进行可复现的合成数据配对实验。它刻意不实现阶段 C
新增的客户范围/映射依据拦截、多合同歧义拒判、定向重试和合并时间预算。
复原假设见 docs/architecture/阶段B参考基线复原假设_v0.1.md。
"""

from __future__ import annotations

import os
import re
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.rag.ingest.chunker import build_chunks  # noqa: E402
from app.rag.ingest.loader import extract_blocks  # noqa: E402
from app.rag.ingest.toc import build_tree  # noqa: E402
from app.rag.retrieval.retriever import Bm25Retriever  # noqa: E402
from app.tools.cross_source_demo import (  # noqa: E402
    CrossSourceDemoTool,
    _EVENTS_SQL,
    _LIMIT_PATTERNS,
    _WORK_ORDER_SQL,
)

_WORK_ORDER_RE = re.compile(r"\b(WO-\d{4}-\d{4})\b", re.IGNORECASE)


class ReferenceBaselineError(RuntimeError):
    """参考基线无法读取同一份实验数据或检索语料。"""


class StageBReferenceBaseline:
    """按阶段 B 文档复原的最小单轮检索算法。"""

    def __init__(self, dsn: str | None = None, docs_dir: Path | None = None) -> None:
        self._dsn = dsn or os.environ.get("CROSS_SOURCE_DEMO_DSN", "").strip()
        configured_docs = os.environ.get("CROSS_SOURCE_DEMO_DOCS_DIR", "").strip()
        if docs_dir is not None:
            self._docs_dir = docs_dir
        elif configured_docs:
            self._docs_dir = Path(configured_docs)
        else:
            self._docs_dir = ROOT / "data" / "cross_source_demo" / "documents"
        self._retriever: Bm25Retriever | None = None

    def run(self, question: str) -> dict[str, Any]:
        match = _WORK_ORDER_RE.search(question)
        if match is None:
            return self._result("missing_work_order_id", "问题中没有可识别的工单编号。")
        if not self._dsn:
            raise ReferenceBaselineError("请设置 CROSS_SOURCE_DEMO_DSN，连接隔离留出数据库")

        started = time.perf_counter()
        rows, events = self._read_rows(match.group(1).upper())
        sql_ms = (time.perf_counter() - started) * 1000
        if not rows:
            return self._result("not_found", f"没有找到工单 {match.group(1).upper()}。", sql_ms=sql_ms)

        base = rows[0]
        if base.get("matched_equipment_id") is None:
            sql_refs = [
                ref.model_dump()
                for ref in CrossSourceDemoTool._missing_equipment_refs(base)
            ]
            absence_checks = CrossSourceDemoTool._query_absence_checks(base, rows, events)
            return self._result(
                "missing_reference_data",
                f"已找到工单 {base['work_order_id']}，但设备主数据中没有设备 "
                f"{base['equipment_id']}；无法核验合同适用性和修复时限。",
                sql_ms=sql_ms,
                sql_refs=sql_refs,
                sql_output={"absence_checks": absence_checks},
                stop_reason_code="equipment_master_missing",
            )
        sql_refs = self._sql_refs(base, rows, events)
        absence_checks = CrossSourceDemoTool._query_absence_checks(base, rows, events)
        event_map = {row["event_type"]: row["event_at"] for row in events}
        work_date = base["opened_at"].date()
        applicable = [
            row for row in rows
            if row["mapped_contract_id"]
            and row["mapping_valid_from"] is not None
            and row["mapping_valid_to"] is not None
            and row["contract_effective_from"] is not None
            and row["contract_effective_to"] is not None
            and row["mapping_valid_from"] <= work_date <= row["mapping_valid_to"]
            and row["contract_effective_from"] <= work_date <= row["contract_effective_to"]
        ]
        # 复原假设：旧基线在多份有效合同中按 SQL 的合同编号顺序取第一项。
        if not applicable:
            has_mapping = any(row["mapped_contract_id"] for row in rows)
            state = "no_active_contract" if has_mapping else "missing_object_mapping"
            if not has_mapping:
                explanation = "按设备编号没有找到合同映射。"
            elif any(
                row["mapped_contract_id"]
                and (row["contract_effective_from"] is None or row["contract_effective_to"] is None)
                for row in rows
            ):
                explanation = "存在编号映射，但对应合同记录或有效期信息缺失，无法核实合同是否适用。"
            elif any(
                row["mapped_contract_id"]
                and (row["mapping_valid_from"] is None or row["mapping_valid_to"] is None)
                for row in rows
            ):
                explanation = "存在编号映射，但映射有效期信息缺失，无法核实合同关系是否适用。"
            else:
                explanation = "存在编号映射但工单日期不在合同有效期内。"
            contract_unresolved = any(
                row.get("mapped_contract_id") and row.get("matched_contract_id") != row.get("mapped_contract_id")
                for row in rows
            )
            stop_reason_code = (
                "mapping_unresolved" if not has_mapping or contract_unresolved
                else "validity_window_not_cover"
            )
            return self._result(
                state, f"工单 {base['work_order_id']} 无法判断：{explanation}", sql_ms, sql_refs,
                sql_output={"absence_checks": absence_checks}, stop_reason_code=stop_reason_code,
            )

        selected = sorted(applicable, key=lambda row: row["mapped_contract_id"])[0]
        contract_id = selected["mapped_contract_id"]
        doc_started = time.perf_counter()
        limits, doc_ref, retrieval_attempt = self._retrieve_once(contract_id)
        doc_ms = (time.perf_counter() - doc_started) * 1000
        if limits is None or "service_restored" not in limits:
            return self._result(
                "insufficient_document_evidence",
                f"已找到合同 {contract_id}，但首轮检索未取到可核验的修复时限条款；不作达标判断。",
                sql_ms, sql_refs, doc_ms=doc_ms, contract_id=contract_id, doc_ref=doc_ref,
                sql_output={"absence_checks": absence_checks}, retrieval_attempts=[retrieval_attempt],
            )

        accepted = event_map.get("accepted")
        restored = event_map.get("service_restored")
        if accepted is None or restored is None:
            missing_events = [name for name in ("accepted", "service_restored") if name not in event_map]
            labels = {"accepted": "受理（accepted）", "service_restored": "服务恢复（service_restored）"}
            sql_refs.extend(ref.model_dump() for ref in CrossSourceDemoTool._missing_event_refs(
                base["work_order_id"], events, missing_events
            ))
            return self._result(
                "missing_required_event",
                f"工单 {base['work_order_id']} 缺少" + "、".join(labels[name] for name in missing_events)
                + "事件，无法计算修复时长。",
                sql_ms, sql_refs, doc_ms, contract_id, doc_ref,
                missing_events=missing_events,
                sql_output={"absence_checks": absence_checks},
                stop_reason_code="key_event_missing",
                retrieval_attempts=[retrieval_attempt],
            )

        elapsed = restored - accepted
        unit, limit = limits["service_restored"]
        limit_delta = timedelta(minutes=limit) if unit == "minutes" else timedelta(hours=limit)
        within = elapsed <= limit_delta
        # 复原假设：A 不专门拦截倒序时间，按负时差直接比较；不据此声称历史文案一致。
        status = "repair_within_limit" if within else "repair_over_limit"
        elapsed_text = self._format_duration(elapsed)
        verdict = "满足" if within else "超时"
        answer = (
            f"工单 {base['work_order_id']}（设备 {base['equipment_id']}）：按合同 {contract_id}，"
            f"从受理到服务恢复为 {elapsed_text}，修复完成要求不超过 {limit}"
            f"{'分钟' if unit == 'minutes' else '小时'}，{verdict}。"
        )
        metric = {
            "metric": "service_restored", "elapsed_seconds": elapsed.total_seconds(),
            "limit": limit, "unit": unit, "within_limit": within,
            "evidence_ids": [base["work_order_id"] + "|accepted", base["work_order_id"] + "|service_restored", "doc:" + contract_id],
        }
        return self._result(
            status, answer, sql_ms, sql_refs, doc_ms, contract_id, doc_ref, metric,
            sql_output={"absence_checks": absence_checks},
            retrieval_attempts=[retrieval_attempt],
        )

    def _read_rows(self, work_order_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise ReferenceBaselineError("缺少 psycopg；按项目依赖安装后重试") from exc
        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SELECT current_database() AS name")
                    if cur.fetchone()["name"] != "cross_source_demo":
                        raise ReferenceBaselineError("拒绝连接数据库名不是 cross_source_demo 的目标")
                    cur.execute(_WORK_ORDER_SQL, (work_order_id,))
                    rows = list(cur.fetchall())
                    cur.execute(_EVENTS_SQL, (work_order_id,))
                    events = list(cur.fetchall())
        except ReferenceBaselineError:
            raise
        except Exception as exc:
            raise ReferenceBaselineError(f"基线只读查询失败：{type(exc).__name__}: {exc}") from exc
        return rows, events

    def _retrieve_once(
        self, contract_id: str
    ) -> tuple[dict[str, tuple[str, int]] | None, dict[str, Any] | None, dict[str, Any]]:
        retriever = self._ensure_retriever()
        query = contract_id + " 服务级别 首次响应 现场到达 修复完成 受理 服务恢复时限"
        candidates = retriever.retrieve(query, top_k=8, doc_id=contract_id)
        for candidate, ref in candidates:
            limits: dict[str, tuple[str, int]] = {}
            for metric, (_label, pattern) in _LIMIT_PATTERNS.items():
                found = re.search(pattern, candidate.chunk.text)
                if found:
                    unit = "minutes" if metric == "first_response" else "hours"
                    limits[metric] = (unit, int(found.group(1)))
            if "service_restored" in limits:
                return limits, ref.model_dump(), {
                    "attempt": 1, "strategy": "primary_sla_query",
                    "status": "required_metric_found", "candidate_count": len(candidates),
                }
        return None, None, {
            "attempt": 1, "strategy": "primary_sla_query",
            "status": "required_metric_not_found", "candidate_count": len(candidates),
        }

    def _ensure_retriever(self) -> Bm25Retriever:
        if self._retriever is None:
            paths = sorted(self._docs_dir.glob("*.md"))
            if not paths:
                raise ReferenceBaselineError(f"合同文档目录为空：{self._docs_dir}")
            chunks = []
            for path in paths:
                doc_id = path.stem
                blocks = extract_blocks(str(path))
                chunks.extend(build_chunks(blocks, build_tree(str(path)), doc_id))
            self._retriever = Bm25Retriever(chunks)
        return self._retriever

    @staticmethod
    def _sql_refs(base: dict[str, Any], rows: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        refs = [
            ref.model_dump()
            for ref in CrossSourceDemoTool._sql_refs(base, rows, events)
        ]
        missing_contracts = {
            row["mapped_contract_id"] for row in rows
            if row["mapped_contract_id"]
            and (row["contract_effective_from"] is None or row["contract_effective_to"] is None)
        }
        for ref in refs:
            if ref.get("table") == "cross_source_demo.contracts" and ref.get("row_pk") in missing_contracts:
                contract_id = ref["row_pk"]
                ref["row_pk"] = None
                ref["snippet"] = (
                    f"映射指向合同 {contract_id}；按 contract_id={contract_id} "
                    "查询合同主表未返回匹配记录；客户、有效期与状态字段均缺失，"
                    "不能据此证明合同适用。"
                )
        if not any(row["mapped_contract_id"] for row in rows):
            refs.append({
                "kind": "sql", "table": "cross_source_demo.contract_equipment",
                "column": "contract_id/equipment_id", "row_pk": None,
                "snippet": f"按设备编号 {base['equipment_id']} 查询合同映射未返回记录；"
                           "mapping_candidates=0；不能按设备名称推断合同关系",
            })
        return refs

    @staticmethod
    def _result(
        status: str, answer: str, sql_ms: float = 0.0, sql_refs: list[dict[str, Any]] | None = None,
        doc_ms: float = 0.0, contract_id: str | None = None, doc_ref: dict[str, Any] | None = None,
        metric: dict[str, Any] | None = None,
        missing_events: list[str] | None = None,
        sql_output: dict[str, Any] | None = None,
        stop_reason_code: str | None = None,
        retrieval_attempts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        sql_refs = sql_refs or []
        trace = [{"tool": "cross_source_sql", "output": {"status": "queried", **(sql_output or {})}, "elapsed_ms": sql_ms, "hit": sql_refs}]
        refs = list(sql_refs)
        if contract_id:
            trace.append({"tool": "cross_source_doc_retrieval", "output": {
                "status": "retrieved" if doc_ref else "required_metric_not_found",
                "doc_id": contract_id if doc_ref else None,
                "attempts": retrieval_attempts or [],
            }, "elapsed_ms": doc_ms, "hit": [doc_ref] if doc_ref else []})
            if doc_ref:
                refs.append(doc_ref)
        alignment_output = {"status": status, "metric_checks": [metric] if metric else [], "missing_events": missing_events or []}
        if stop_reason_code:
            alignment_output["stop_reason_code"] = stop_reason_code
        trace.append({"tool": "cross_source_alignment", "output": alignment_output, "elapsed_ms": 0.0, "hit": refs})
        return {"answer": answer, "trace": trace, "refs": refs}

    @staticmethod
    def _format_duration(value: timedelta) -> str:
        total_seconds = int(value.total_seconds())
        sign = "-" if total_seconds < 0 else ""
        total_minutes = abs(total_seconds) // 60
        hours, minutes = divmod(total_minutes, 60)
        if hours and minutes:
            return f"{sign}{hours}小时{minutes}分钟"
        if hours:
            return f"{sign}{hours}小时"
        return f"{sign}{minutes}分钟"
