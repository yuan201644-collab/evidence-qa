"""隔离跨源样例的事实计划节点绑定。

仅供 ``CROSS_SOURCE_FACT_PLAN_ENABLED=1`` 的显式试验路径使用，不修改默认
NL2SQL/RAG 工具、默认路由或公共响应契约。NL2SQL/RAG 产出仅是候选；本模块
对结果结构、对象编号、双有效期、客户、条款原文与事件时间执行确定性核验。
"""

from __future__ import annotations

import os
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from app.contracts import DocRef, RefT, SqlRef, ToolResult, TraceStep
from app.evidence_ledger import FactRecord, FactState, evidence_key
from app.fact_executor import (
    FactDecision,
    FactPlanExecutor,
    FactTaskOutput,
    _attach_decision_evidence,
    tool_task_handler,
)
from app.fact_planner import WORK_ORDER_ID_RE
from app.tools.cross_source_policy import (
    is_active_contract_status,
    load_mapping_basis_policy,
    mapping_basis_status,
)
from app.tools.base import CAPABILITY_DOC, BaseTool, ToolSpec


_ROOT = Path(__file__).resolve().parents[1]
_DATA = _ROOT / "data" / "cross_source_demo"
_REPAIR_LIMIT_RE = re.compile(
    r"(?m)^\s*\|?\s*修复完成\s*\|(?:\s*([^|\n]+)\s*\|)?\s*(\d+)\s*小时内\s*\|?\s*$"
)
_REPAIR_LIMIT_PROSE_RE = re.compile(
    r"(?m)(?:修复完成|服务恢复)(?P<descriptor>[^。；;|\n]{0,120}?)"
    r"(?P<limit>\d+)\s*小时内"
)
_REPAIR_EQUIPMENT_ID_RE = re.compile(
    r"(?<![\w-])(?:EQ|DEV)-[A-Z0-9]+(?:-[A-Z0-9]+)*(?![\w-])", re.IGNORECASE
)
_FIRST_RESPONSE_LIMIT_RE = re.compile(
    r"(?m)^\s*\|?\s*首次响应\s*\|\s*"
    r"(?:`?first_response`?\s*\|\s*|([^|\n]+)\s*\|\s*)"
    r"(\d+)\s*(分钟|小时)内\s*\|?\s*$"
)
_FIRST_RESPONSE_LIMIT_PROSE_RE = re.compile(
    r"(?m)首次响应(?P<descriptor>[^。；;|\n]{0,120}?)"
    r"(?P<limit>\d+)\s*(?P<unit>分钟|小时)内"
)
_UNSPECIFIED_RE = re.compile(
    r"(?:不约定|未约定|不规定|未规定|不设定|未设定)[^。；\n]{0,16}"
    r"(?:服务恢复|修复|维修)[^。；\n]{0,8}(?:总)?(?:时限|时间)"
    r"|(?:服务恢复|修复|维修)[^。；\n]{0,8}(?:总)?(?:时限|时间)"
    r"[^。；\n]{0,16}(?:不约定|未约定|不规定|未规定|不设定|未设定)"
)
_SIGNED_REPAIR_LIMIT_RE = re.compile(
    r"(?:签署正文|已签署正文|正式签署正文)[^。；;\n]{0,100}?"
    r"(?P<limit>\d+)\s*小时内"
)
_UNSIGNED_SUPPLEMENT_LIMIT_RE = re.compile(
    r"(?:未签署|未签|未盖章|未生效|草稿|补充稿)[^。；;\n]{0,80}?"
    r"(?P<limit>\d+)\s*小时"
)
_NEGATED_UNSPECIFIED_RE = re.compile(r"(?:并非|并不是|并不)\s*(?:不约定|未约定|不规定|未规定|不设定|未设定)")
_SCOPE_MARKER_RE = re.compile(
    r"(?:适用于|适用范围\s*[:：|]|适用设备(?:范围)?\s*[:：|])"
)
_SCOPE_HEADING_RE = re.compile(
    r"^\s*#{1,6}\s*(?:适用范围|设备范围|适用设备(?:范围)?|设备清册|服务对象与期限)\s*$"
)
_SCOPE_EXCLUSION_RE = re.compile(
    r"(?:不适用|并非\s*适用|并不是\s*适用|并不\s*适用|不包括|不含|"
    r"除.{0,40}外|不在.{0,12}(?:适用)?范围|不构成适用范围|"
    r"不属于适用范围|并非属于适用范围|不纳入适用范围|不具备适用范围|非适用(?:设备)?范围|"
    r"(?:其他|另一)(?:合同|协议)[^。；;]{0,24}适用于)"
)
_SLA_OVERRIDE_RE = re.compile(
    r"(?:特别约定|补充约定|优先于|另行约定|另有约定|变更为|变更后|"
    r"替代上列|替代前述)"
    r"|(?:不适用于|并不适用于)"
    r"|(?:时限|期限|时间|标准|条款|要求|指标|服务|修复|响应|到达|级别)"
    r"[^。；;\n]{0,12}以[^。；;\n]{0,20}为准"
    r"|(?:重大故障|特别故障|特殊故障|严重故障|一级故障)[^。；;\n]{0,20}(?:执行|按|采用|适用)"
)
# 「以＜事件/编号/记录…＞为准」是在说明**怎么度量**（用哪个事件算耗时、
# 以编号还是名称辨识设备），不是时限的优先约定。这类片段要排除，
# 否则合同里一句「设备名称不是唯一标识，以设备编号为准」就会让
# 「修复完成时限是几小时」这个问题永远判不出来。
#
# ⚠️ 白名单**刻意不含「附件」「协议」**：指向附件/补充协议的「以…为准」
# 恰恰是新时限所在，属于必须 fail closed 的 override。
_SLA_DEFINITION_RE = re.compile(
    r"以[^。；;\n]{0,20}(?:事件|编号|记录|日志|系统|字段|清单|字典|口径)"
    r"[^。；;\n]{0,6}为准"
)
def build_cross_source_fact_plan_executor(
    dsn: str,
    *,
    schema_path: str | Path | None = None,
    documents_dir: str | Path | None = None,
    db_schema: str = "cross_source_demo",
    verification_enabled: bool = True,
) -> FactPlanExecutor:
    """为独立演示库装配真实 NL2SQL 与 RAG 工具；两者均延迟到任务执行时工作。"""
    if not dsn.strip():
        raise ValueError("事实计划试验必须提供独立数据库连接串")

    from app.nl2sql.pipeline import Nl2SqlPipeline
    from app.nl2sql.schema import SchemaCatalog
    from app.nl2sql.executor import SqlExecutor
    from app.tools.nl2sql import Nl2SqlTool, _build_llm_client

    schema_file = Path(schema_path or (_DATA / "schema_meta.json")).resolve()
    docs_dir = Path(documents_dir or (_DATA / "documents")).resolve()
    if not schema_file.is_file():
        raise FileNotFoundError("隔离 NL2SQL 结构目录不存在：" + str(schema_file))
    document_paths = sorted(str(path) for path in docs_dir.glob("*.md"))
    if not document_paths:
        raise FileNotFoundError("隔离合同文档目录为空：" + str(docs_dir))

    vector_schema = os.environ.get("CROSS_SOURCE_FACT_PLAN_VECTOR_SCHEMA", "public").strip()
    extension_schema = os.environ.get("PGVECTOR_EXTENSION_SCHEMA", "public").strip()
    isolated_dsn = _dsn_with_search_path(
        dsn,
        db_schema=db_schema,
        extra_schema=vector_schema,
        read_only=os.environ.get("CROSS_SOURCE_FACT_PLAN_READ_ONLY", "1").strip() == "1",
        extension_schema=extension_schema,
    )
    pipeline = Nl2SqlPipeline(
        _build_llm_client(),
        catalog=SchemaCatalog.from_file(schema_file),
        executor=SqlExecutor(dsn=isolated_dsn, readonly=True),
        dialect="postgres",
        # 隔离原型每个节点最多生成两次；不改变默认 NL2SQL 后端的重试预算。
        max_attempts=2,
        # 时长节点需同时引用 accepted 与 service_restored，不截断事件行级来源。
        max_refs=10,
    )
    nl2sql_tool = Nl2SqlTool(pipeline=pipeline)

    def build_rag_tool():
        from app.rag.answer import build_rag_tool_from_corpus

        # 明确限制到演示合同 Markdown；索引写入的 DSN 也必须是隔离库。
        read_only_index = os.environ.get(
            "CROSS_SOURCE_FACT_PLAN_RAG_READ_ONLY", "1"
        ).strip() == "1"
        return build_rag_tool_from_corpus(
            dsn=isolated_dsn, paths=document_paths, read_only_index=read_only_index
        )

    doc_search_tool = _ScopedRagDocSearchTool(build_rag_tool)
    return _handlers_for_tools(
        nl2sql_tool, doc_search_tool, verification_enabled=verification_enabled
    )


class _ScopedRagDocSearchTool(BaseTool):
    """事实计划专用 RAG 适配器，按已核验合同编号过滤，并保留完整切片证据。"""

    def __init__(self, tool_factory):
        import threading

        self._tool_factory = tool_factory
        self._rag_tool = None
        self._lock = threading.Lock()

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="doc_search",
            capabilities=(CAPABILITY_DOC,),
            description="仅在已核验的合同文档内检索原文证据。",
            input_schema={"question": "检索问题", "doc_id": "合同编号"},
            returns="带真实检索、重排、生成轨迹及完整切片偏移的文档证据",
            cost_hint="慢",
        )

    def _ensure_tool(self):
        if self._rag_tool is None:
            with self._lock:
                if self._rag_tool is None:
                    self._rag_tool = self._tool_factory()
        return self._rag_tool

    def run(self, payload: dict[str, Any]) -> ToolResult:
        question = payload.get("question")
        doc_id = payload.get("doc_id")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("合同文档检索需要非空问题")
        if not isinstance(doc_id, str) or not doc_id.strip():
            raise ValueError("合同文档检索必须先有结构化核验的合同编号")

        rag_tool = self._ensure_tool()

        def required_markers(text: str) -> bool:
            compact = text.replace(" ", "")
            repair_supported = bool(
                _REPAIR_LIMIT_RE.search(text) or _REPAIR_LIMIT_PROSE_RE.search(text)
            ) or any(marker in compact for marker in ("修复完成总时限未约定", "未约定修复完成"))
            if "首次响应" not in question and "first_response" not in question:
                return repair_supported
            response_supported = bool(
                _FIRST_RESPONSE_LIMIT_RE.search(text) or _FIRST_RESPONSE_LIMIT_PROSE_RE.search(text)
            )
            return repair_supported and response_supported

        def expand_context(retrieved: dict[str, Any]) -> dict[str, Any]:
            # DocRef 为展示而截成 200 字，但确定性核验需要防止编号刚好被切片边界
            # 分开。候选本身来自真实 RAG 检索与重排；此处把引用扩回其准确源切片，
            # 并保留原 char_start/char_end，不从答案文本或本地未命中的文档补证据。
            full_context_hits = []
            for candidate, ref in retrieved["context_hits"]:
                expanded_ref = ref.model_copy(update={"snippet": candidate.chunk.text})
                full_context_hits.append((candidate, expanded_ref))
            retrieved["context_hits"] = full_context_hits
            return retrieved

        attempts: list[dict[str, Any]] = []
        retrieved = expand_context(rag_tool.retrieve_and_rank(question, doc_id=doc_id))
        total_retrieval_ms = float(retrieved.get("retrieve_ms", 0.0))
        first_has_required = bool(retrieved["num_hits"]) and required_markers(
            "\n".join(candidate.chunk.text for candidate, _ in retrieved["context_hits"])
        )
        attempts.append({
            "attempt": 1,
            "strategy": "hybrid_bm25_vector",
            "status": (
                "candidates_retrieved" if first_has_required
                else "required_metric_not_found" if retrieved["num_hits"]
                else "no_candidates"
            ),
            "candidate_count": int(retrieved["num_hits"]),
            "elapsed_ms": total_retrieval_ms,
        })

        # 仅允许一次明确改变策略的补查；候选为空或缺少用户请求的指标时
        # 才触发。第二次仍限制到同一已核验合同编号，不能回退到全库检索。
        if not first_has_required:
            retry_question = (
                f"合同 {doc_id} 条款关键词定向补查：修复完成 service_restored、"
                "首次响应 first_response、适用设备范围、未约定。"
            )
            retry = expand_context(rag_tool.retrieve_and_rank(retry_question, doc_id=doc_id))
            retry_elapsed_ms = float(retry.get("retrieve_ms", 0.0))
            total_retrieval_ms += retry_elapsed_ms
            retry_has_required = bool(retry["num_hits"]) and required_markers(
                "\n".join(candidate.chunk.text for candidate, _ in retry["context_hits"])
            )
            attempts.append({
                "attempt": 2,
                "strategy": "targeted_contract_clause_retry",
                "status": "required_metric_found" if retry_has_required else "required_metric_not_found",
                "candidate_count": int(retry["num_hits"]),
                "elapsed_ms": retry_elapsed_ms,
            })
            if retry["num_hits"]:
                retrieved = retry

        trace = rag_tool.retrieve_steps(retrieved, step_id=1, doc_id=doc_id)
        # Keep a stable private trace projection for the experiment scorer. The
        # attempts are derived from actual hybrid retrieval calls, not from the
        # generated answer; detailed steps remain those of the selected candidate set.
        trace.append(TraceStep(
            step_id=len(trace) + 1,
            tool="cross_source_doc_retrieval",
            input={"doc_id": doc_id, "strategy": "hybrid_bm25_vector"},
            output={
                "status": attempts[-1]["status"],
                "doc_id": doc_id,
                "attempts": attempts,
            },
            elapsed_ms=total_retrieval_ms,
            hit=[],
        ))
        generated = rag_tool.generate(question, retrieved["context_hits"])
        trace.append(rag_tool.generate_step(question, generated, step_id=len(trace) + 1))
        return ToolResult(
            data=generated["text"],
            trace=trace,
            refs=generated["final_refs"],
        )


def _handlers_for_tools(
    nl2sql_tool: Any, doc_search_tool: Any, *, verification_enabled: bool = True
) -> FactPlanExecutor:
    def sql_payload(question_builder):
        return lambda context, facts: {
            "question": question_builder(context, facts),
            "hint": "只查询隔离演示库；按给定编号精确筛选，不得按名称推断关系。",
        }

    def checked_sql_result(result: ToolResult, context, facts, validator):
        rows = _rows(result)
        if rows is None:
            return FactDecision(
                _tool_failure(result), "insufficient", diagnostic_code=_tool_failure_code(result)
            )
        value, state, evidence = validator(rows, result.refs, result, context, facts)
        stop_reason_code = None
        if verification_enabled and state != "supported" and validator is _validate_mapping:
            stop_reason_code = _mapping_stop_reason(rows, result, facts)
        elif verification_enabled and state != "supported" and validator is _validate_duration:
            if state == "contradicted" and "时间倒序" in str(value):
                stop_reason_code = "event_time_reversed"
        return FactDecision(value, state, tuple(evidence), stop_reason_code)

    contract_tool_handler = tool_task_handler(
        doc_search_tool,
        lambda context, facts: {
            "question": _contract_question(
                facts["controlled_contract_mapping"].value,
                include_first_response=_requests_first_response(context),
            ),
            "doc_id": facts["controlled_contract_mapping"].value["contract_id"],
        },
        lambda result, context, facts: (
            _validate_contract_document(result, context, facts)
            if verification_enabled
            else _baseline_contract_document(result, context, facts)
        ),
        _document_validation_trace,
    )

    def contract_repair_handler(context, facts):
        """Avoid an ablation-only downstream crash when mapping is absent.

        The relaxed reference arm intentionally continues independent event
        queries after a mapping miss, but it still cannot construct a contract
        document request without a contract id.  Record a bounded insufficient
        fact instead of calling the document tool with fabricated input.
        """
        mapping = facts.get("controlled_contract_mapping")
        value = mapping.value if mapping is not None else None
        if not isinstance(value, dict) or not str(value.get("contract_id", "")).strip():
            return FactTaskOutput(
                FactDecision(
                    "没有可用于检索合同的映射候选。",
                    "insufficient",
                    diagnostic_code="document_retrieval_not_run_missing_mapping",
                ),
                None,
                not_run_reason_code="missing_contract_mapping",
                not_run_reason="合同映射未提供可用合同编号，文档检索未执行。",
                not_run_dependencies=("controlled_contract_mapping",),
            )
        return contract_tool_handler(context, facts)

    def mapping_handler(context, facts):
        """核验映射；发现多个适用合同后立即停止，不再检索候选合同原文。"""
        payload = sql_payload(_mapping_question)(context, facts)
        result = nl2sql_tool.run(payload)
        if not isinstance(result, ToolResult):
            raise TypeError("NL2SQL 工具必须返回 ToolResult")
        rows = _rows(result)
        if rows is None:
            return FactTaskOutput(FactDecision(
                _tool_failure(result), "insufficient", diagnostic_code=_tool_failure_code(result)
            ), result)

        validator = _validate_mapping if verification_enabled else _baseline_mapping
        value, state, evidence = validator(rows, result.refs, result, context, facts)
        stop_reason_code = None
        if verification_enabled and state != "supported":
            stop_reason_code = _mapping_stop_reason(rows, result, facts)
        decision = FactDecision(value, state, tuple(evidence), stop_reason_code)
        result = _mapping_absence_trace(result, decision)
        result = _attach_decision_evidence(result, decision.evidence)
        return FactTaskOutput(decision, result)

    handlers = {
        "work_order_facts": tool_task_handler(
            nl2sql_tool,
            sql_payload(_work_order_question),
            lambda result, context, facts: checked_sql_result(
                result, context, facts, _validate_work_order
            ),
        ),
        "controlled_contract_mapping": mapping_handler,
        "contract_repair_limit": contract_repair_handler,
        "repair_duration": _make_repair_duration_handler(
            nl2sql_tool, verification_enabled=verification_enabled
        ),
        "first_response_duration": _make_first_response_duration_handler(
            verification_enabled=verification_enabled
        ),
        "first_response_verdict": _make_first_response_verdict_handler(
            verification_enabled=verification_enabled
        ),
        "sla_verdict": _make_verdict_handler(verification_enabled=verification_enabled),
    }
    return FactPlanExecutor(
        handlers,
        stop_on_unsupported_dependencies=verification_enabled,
        verification_mode="strict" if verification_enabled else "ablation",
    )


def _work_order_question(context: Any, _facts: Mapping[str, FactRecord]) -> str:
    work_order_id = _work_order_id(context)
    return (
        f"查询工单编号 {work_order_id}，精确筛选 work_order_id，返回 work_order_id、"
        "equipment_id、opened_at 三列。只查工单表的该条记录，不要按设备名称关联。"
    )


def _mapping_question(context: Any, facts: Mapping[str, FactRecord]) -> str:
    work = facts["work_order_facts"].value
    return (
        f"查询设备编号 {work['equipment_id']} 的全部合同设备映射候选，并连接设备主表与合同主表；"
        f"工单日期是 {work['work_order_date']}。按 equipment_id 精确关联，返回每条候选的 "
        "equipment_id、equipment_name、equipment_customer_id、contract_id、mapping_valid_from、mapping_valid_to、"
        "mapping_basis、contract_customer_id、contract_effective_from、contract_effective_to、"
        "contract_status。必须列出全部候选，不要预先按有效期、状态、依据过滤，不要只取第一条，"
        "也不要用设备名称作关联键。三张表必须使用 INNER JOIN；不要使用 LEFT/RIGHT/FULL OUTER JOIN，"
        "否则无法证明设备、映射和合同记录的完整对应关系。"
    )


def _events_question(context: Any, facts: Mapping[str, FactRecord]) -> str:
    work_order_id = _work_order_id(context)
    return (
        f"查询工单 {work_order_id} 的 work_order_events，按 work_order_id 精确筛选，"
        "返回 work_order_id、event_type、event_at 三列并列出该工单全部事件；不要自行计算时长。"
    )


def _event_absence_question(work_order_id: str, event_type: str) -> str:
    return (
        f"只查询 work_order_events 表，精确筛选 work_order_id = '{work_order_id}' "
        f"且 event_type = '{event_type}'，返回 work_order_id、event_type、event_at。"
        "必须执行数据库查询，不要依据此前结果或文字描述推断行数。"
    )


def _contract_question(mapping: dict[str, Any], *, include_first_response: bool = False) -> str:
    question = (
        f"只检索合同 {mapping['contract_id']} 的原文，确认设备 {mapping['equipment_id']} "
        "适用的修复完成 service_restored 时限、起止事件定义、适用设备范围，以及是否存在"
        "优先级更高的特别约定。必须引用原文；若片段未明确，不要猜测。"
    )
    if include_first_response:
        question += "同时核验首次响应 first_response 时限与适用范围。"
    return question


# ISO 8601 的「日期T时间」分隔符；与 app/orchestrator._ISO_T_RE 同口径。
_ISO_T_RE = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})")


def _readable_duration(seconds: float) -> str:
    """把秒数写成「X 小时 Y 分钟」的人话。

    **为什么不能直接 `{hours:g}`**：27600 秒 ÷ 3600 = 7.666666666666667，
    直接格式化会印出「7.66667 小时」—— 一个浮点残渣，人读起来是机器噪声，
    而演示题面期望的表述是「7小时40分钟」。整点（如 8 小时）仍写成「8 小时」，
    不拖一个「0 分钟」的尾巴。
    """
    total_seconds = int(round(float(seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours} 小时 {minutes} 分钟"
    if hours:
        return f"{hours} 小时"
    if minutes:
        return f"{minutes} 分钟"
    return f"{total_seconds} 秒"


def _readable_time(value: Any) -> str:
    """ISO 8601 → 用户可读：去掉日期与时间之间的 `T`，保留时区后缀。

    与 `app/orchestrator._readable_time` 同一口径，两处必须一起改：
    这里负责正常路径（`sla_verdict` 受支持时的答案），orchestrator 那边
    负责早停路径（只保留已成立事实 + 阻塞原因）。同一个 `opened_at`
    在两处出现，输出形态必须一致，否则同一份事实会随走哪条分支而变形。
    """
    text = value if isinstance(value, str) else str(value)
    return _ISO_T_RE.sub(r"\1 \2", text)


def _requests_first_response(context: Any) -> bool:
    if not isinstance(context, dict):
        return False
    return _explicitly_requests_metric(str(context.get("question", "")), "首次响应")


def _explicitly_requests_metric(question: str, metric: str) -> bool:
    """匹配明确的指标请求；紧邻的“不需要/不核验”等否定不触发额外分支。"""
    for match in re.finditer(re.escape(metric), question):
        prefix = question[max(0, match.start() - 12):match.start()]
        suffix = question[match.end():match.end() + 12]
        if re.search(r"(?:不需要|无需|无须|不用|不必|不要|不再|不核验|不检查|不判断|不讨论|不统计|不关注|不考虑)\s*(?:对|核验|检查|判断|查询|计算|统计|讨论|分析|关注|提及)?\s*$", prefix):
            continue
        if re.match(r"\s*(?:无需|不需要|不用|不必|不核验|不检查|不判断|不讨论|不统计|不关注|不考虑)", suffix):
            continue
        return True
    return False


def _work_order_id(context: Any) -> str:
    if not isinstance(context, dict):
        raise ValueError("事实计划上下文必须是字典")
    work_order_id = str(context.get("work_order_id", "")).strip().upper()
    if not WORK_ORDER_ID_RE.fullmatch(work_order_id):
        raise ValueError("事实计划上下文缺少有效工单编号")
    return work_order_id


def _rows(result: ToolResult) -> list[dict[str, Any]] | None:
    if not isinstance(result.data, dict) or result.data.get("error"):
        return None
    rows = result.data.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return None
    return rows


def _event_absence_filters_match(sql: str, work_order_id: str, event_type: str) -> bool:
    """Require one direct, bounded SELECT with exactly the declared event filters."""
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        return False
    if not isinstance(tree, exp.Select):
        return False
    forbidden = (exp.Join, exp.Group, exp.Having, exp.Offset, exp.Distinct,
                 exp.Subquery, exp.With, exp.Order, exp.AggFunc, exp.Window)
    if any(isinstance(node, forbidden) for node in tree.walk()):
        return False
    # The NL2SQL execution pipeline appends a positive LIMIT (normally 1000)
    # to protect the database. A positive limit cannot turn a non-empty exact
    # result into zero rows; LIMIT 0 can, so it must never support absence.
    limit = tree.args.get("limit")
    if limit is not None:
        limit_value = limit.expression
        if (not isinstance(limit_value, exp.Literal)
                or not re.fullmatch(r"\d+", str(limit_value.this))
                or int(limit_value.this) <= 0):
            return False
    from_clause = tree.args.get("from_")
    source = from_clause.this if isinstance(from_clause, exp.From) else None
    if (not isinstance(source, exp.Table) or from_clause.expressions
            or source.name.casefold() != "work_order_events"
            or len(list(tree.find_all(exp.Table))) != 1):
        return False
    where = tree.args.get("where")
    if where is None or any(isinstance(node, (exp.Or, exp.Not)) for node in where.walk()):
        return False

    def flatten(expression: Any) -> list[Any]:
        if isinstance(expression, exp.Paren):
            return flatten(expression.this)
        if isinstance(expression, exp.And):
            return flatten(expression.this) + flatten(expression.expression)
        return [expression]

    predicates: dict[str, str] = {}
    for predicate in flatten(where.this):
        if not isinstance(predicate, exp.EQ):
            return False
        left, right = predicate.this, predicate.expression
        if not isinstance(left, exp.Column) and isinstance(right, exp.Column):
            left, right = right, left
        if (not isinstance(left, exp.Column)
                or (left.table and left.table.casefold() not in {
                    (source.alias_or_name or source.name).casefold(), source.name.casefold()
                })
                or not isinstance(right, exp.Literal) or not right.is_string):
            return False
        column = left.name.casefold()
        if column in predicates:
            return False
        predicates[column] = str(right.this)
    return predicates == {"work_order_id": work_order_id, "event_type": event_type}


def _event_absence_trace(
    result: ToolResult, work_order_id: str, event_type: str,
) -> tuple[ToolResult, SqlRef | None]:
    """Attach negative evidence only after validating the executed SQL and row count."""
    if not isinstance(result.data, dict) or result.data.get("error"):
        return result, None
    rows = result.data.get("rows")
    if not isinstance(rows, list):
        return result, None
    executed = next((
        step for step in reversed(result.trace)
        if isinstance(step.output.get("sql"), str)
        and type(step.output.get("row_count")) is int
        and not step.output.get("error")
    ), None)
    if (executed is None
            or executed.output["row_count"] != len(rows)
            or rows
            or not _event_absence_filters_match(
                executed.output["sql"], work_order_id, event_type
            )):
        return result, None
    table = "work_order_events"
    filters = {"work_order_id": work_order_id, "event_type": event_type}
    absence_ref = SqlRef(
        kind="sql",
        table=table,
        column="work_order_id/event_type",
        row_pk=None,
        snippet=(
            f"按 {table}.work_order_id={work_order_id} 且 event_type={event_type} "
            "精确查询，数据库实际返回 0 行。"
        ),
    )
    check = TraceStep(
        step_id=len(result.trace) + 1,
        tool="cross_source_absence_check",
        input={"table": table, "filters": filters},
        output={
            "table": table,
            "filters": filters,
            "row_count": executed.output["row_count"],
            "derived_from_executed_query": True,
        },
        elapsed_ms=0.0,
        hit=[absence_ref],
    )
    refs = [*result.refs]
    if evidence_key(absence_ref) not in {evidence_key(ref) for ref in refs}:
        refs.append(absence_ref)
    return result.model_copy(update={"trace": [*result.trace, check], "refs": refs}), absence_ref


def _merge_tool_results(results: list[ToolResult]) -> ToolResult:
    if not results:
        raise ValueError("至少需要一个真实工具结果")
    refs: list[RefT] = []
    seen: set[tuple[str, ...]] = set()
    traces: list[TraceStep] = []
    for result in results:
        traces.extend(result.trace)
        for ref in result.refs:
            key = evidence_key(ref)
            if key not in seen:
                refs.append(ref)
                seen.add(key)
    return results[0].model_copy(update={"trace": traces, "refs": refs})


def _make_repair_duration_handler(nl2sql_tool: Any, *, verification_enabled: bool):
    """Query all events, then issue exact zero-row checks for missing SLA events."""
    def handle(context: Any, facts: Mapping[str, FactRecord]) -> FactTaskOutput:
        work_order_id = _work_order_id(context)
        base = nl2sql_tool.run({
            "question": _events_question(context, facts),
            "hint": "只查询隔离演示库；按给定编号精确筛选。",
        })
        if not isinstance(base, ToolResult):
            raise TypeError("NL2SQL 工具必须返回 ToolResult")
        rows = _rows(base)
        if rows is None:
            return FactTaskOutput(FactDecision(
                _tool_failure(base), "insufficient", diagnostic_code=_tool_failure_code(base)
            ), base)
        validator = _validate_duration if verification_enabled else _baseline_duration
        value, state, evidence = validator(rows, base.refs, base, context, facts)
        merged = [base]
        evidence = list(evidence)
        event_counts = {
            event_type: sum(
                str(row.get("work_order_id", "")).upper() == work_order_id
                and row.get("event_type") == event_type
                for row in rows
            )
            for event_type in ("accepted", "service_restored")
        }
        verified_absence_types: set[str] = set()
        for event_type, count in event_counts.items():
            if count != 0:
                continue
            absence_result = nl2sql_tool.run({
                "question": _event_absence_question(work_order_id, event_type),
                "hint": "这是单独的精确负证据查询；只接受数据库实际返回的行数。",
            })
            if not isinstance(absence_result, ToolResult):
                raise TypeError("NL2SQL 工具必须返回 ToolResult")
            checked, absence_ref = _event_absence_trace(
                absence_result, work_order_id, event_type
            )
            merged.append(checked)
            if absence_ref is not None:
                evidence.append(absence_ref)
                verified_absence_types.add(event_type)
        stop_reason_code = None
        missing_required_events = {
            event_type for event_type in ("accepted", "service_restored")
            if event_counts[event_type] == 0
        }
        if (verification_enabled and missing_required_events
                and missing_required_events.issubset(verified_absence_types)):
            stop_reason_code = "key_event_missing"
        elif verification_enabled and state == "contradicted" and "时间倒序" in str(value):
            stop_reason_code = "event_time_reversed"
        return FactTaskOutput(
            FactDecision(value, state, tuple(evidence), stop_reason_code),
            _merge_tool_results(merged),
        )

    return handle


def _tool_failure(result: ToolResult) -> str:
    if isinstance(result.data, dict) and result.data.get("error"):
        return "结构化查询失败，未据此确认事实。"
    return "结构化查询未返回可核验的结果行。"


def _tool_failure_code(result: ToolResult) -> str:
    """Return a stable, non-sensitive diagnostic category for a failed tool result."""
    if isinstance(result.data, dict) and result.data.get("error"):
        return "structured_query_failed"
    return "tool_result_unavailable"


def _table_evidence(refs: list[RefT], required_tables: set[str]) -> list[RefT] | None:
    selected: list[RefT] = []
    hit_tables: set[str] = set()
    for ref in refs:
        if not isinstance(ref, SqlRef):
            continue
        table = ref.table.lower()
        selected_name = table.split(".")[-1]
        if selected_name in required_tables:
            selected.append(ref)
            hit_tables.add(selected_name)
    return selected if required_tables.issubset(hit_tables) else None


def _validate_work_order(rows, refs, _result, context, _facts):
    work_order_id = _work_order_id(context)
    if len(rows) != 1 or str(rows[0].get("work_order_id", "")).upper() != work_order_id:
        return "工单编号查询结果缺失或不唯一。", "insufficient", ()
    row = rows[0]
    equipment_id = row.get("equipment_id")
    opened_at = _datetime(row.get("opened_at"))
    evidence = _table_evidence(refs, {"work_orders"})
    if not equipment_id or opened_at is None or evidence is None:
        return "工单记录字段或行级来源证据不足。", "insufficient", ()
    return {
        "work_order_id": work_order_id,
        "equipment_id": str(equipment_id),
        "opened_at": opened_at.isoformat(),
        "work_order_date": opened_at.date().isoformat(),
    }, "supported", evidence


def _validate_mapping(rows, refs, result, _context, facts):
    work = facts["work_order_facts"].value
    work_date = date.fromisoformat(work["work_order_date"])
    # 不信任 SQL 生成器提前“筛选成唯一”：零条、多条或证据表缺失一律不支持。
    if not _mapping_query_evidence(result, work["equipment_id"]):
        return "映射查询未能证明按设备编号精确检索及明确连接关系。", "insufficient", ()
    if not rows:
        absence_ref = _mapping_absence_ref(result, work["equipment_id"])
        evidence = (absence_ref,) if absence_ref is not None else ()
        return "未找到设备编号对应的合同映射。", "insufficient", evidence
    evidence: list[SqlRef] = []
    for row in rows:
        if row.get("contract_id") and str(row.get("equipment_id", "")) == work["equipment_id"]:
            evidence.extend(_mapping_row_refs(row))

    applicable: list[dict[str, Any]] = []
    excluded_candidates: list[dict[str, Any]] = []
    for row in rows:
        if not row.get("contract_id") or str(row.get("equipment_id", "")) != work["equipment_id"]:
            continue
        reasons: list[str] = []
        equipment_customer_id = row.get("equipment_customer_id")
        contract_customer_id = row.get("contract_customer_id")
        invalid_customer_values = {"", "none", "null", "unknown", "n/a", "na"}
        if (not isinstance(equipment_customer_id, str)
                or equipment_customer_id.strip().casefold() in invalid_customer_values
                or not isinstance(contract_customer_id, str)
                or contract_customer_id.strip().casefold() in invalid_customer_values
                or equipment_customer_id.strip() != contract_customer_id.strip()):
            reasons.append("设备客户与合同客户不一致或缺失")
        basis = str(row.get("mapping_basis", "")).strip()
        if mapping_basis_status(basis) != "allowed":
            reasons.append(f"映射依据 {basis or '未提供'} 未受控")
        if not is_active_contract_status(row.get("contract_status")):
            reasons.append(f"合同状态 {row.get('contract_status') or '未提供'} 非有效")
        mapping_from = _date(row.get("mapping_valid_from"))
        mapping_to = _date(row.get("mapping_valid_to"))
        contract_from = _date(row.get("contract_effective_from"))
        contract_to = _date(row.get("contract_effective_to"))
        if None in (mapping_from, mapping_to, contract_from, contract_to):
            reasons.append("映射或合同有效期字段缺失，无法核实日期覆盖")
        else:
            if not mapping_from <= work_date <= mapping_to:
                reasons.append("设备—合同映射有效期未覆盖工单日期")
            if not contract_from <= work_date <= contract_to:
                reasons.append("合同有效期未覆盖工单日期")
        if reasons:
            excluded_candidates.append({
                "contract_id": str(row["contract_id"]),
                "reasons": reasons,
            })
        else:
            applicable.append(row)
    if len(applicable) != 1:
        state: FactState = "ambiguous" if len(applicable) > 1 else "insufficient"
        msg = "存在多个有效受控合同映射，无法唯一确定。" if len(applicable) > 1 else "没有唯一满足依据、客户和双有效期校验的合同映射。"
        if len(applicable) > 1:
            return {
                "status": "ambiguous_active_contracts",
                "equipment_id": work["equipment_id"],
                "work_order_date": work["work_order_date"],
                "candidate_contracts": [
                    _mapping_fact_value(row, excluded_candidates=[])
                    for row in applicable
                ],
            }, state, tuple(evidence)
        return msg, state, evidence
    row = applicable[0]
    value = _mapping_fact_value(row, excluded_candidates=excluded_candidates)
    return value, "supported", tuple(evidence)


def _mapping_fact_value(
    row: dict[str, Any], *, excluded_candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    """Project one already validated mapping row into the internal fact value."""
    return {
        "contract_id": str(row["contract_id"]),
        "equipment_id": str(row["equipment_id"]),
        "equipment_name": str(row["equipment_name"]) if row.get("equipment_name") else None,
        "equipment_customer_id": str(row["equipment_customer_id"]),
        "contract_customer_id": str(row["contract_customer_id"]),
        "mapping_basis": str(row["mapping_basis"]),
        "mapping_valid_from": str(row["mapping_valid_from"]),
        "mapping_valid_to": str(row["mapping_valid_to"]),
        "contract_effective_from": str(row["contract_effective_from"]),
        "contract_effective_to": str(row["contract_effective_to"]),
        "contract_status": str(row["contract_status"]),
        "mapping_relation_verified": True,
        "date_interval_policy": "closed",
        "excluded_candidates": excluded_candidates,
    }


def _mapping_stop_reason(
    rows: list[dict[str, Any]], result: ToolResult, facts: Mapping[str, FactRecord]
) -> str | None:
    """Classify only a row-backed mapping failure with a protocol allowlist code."""
    work = facts.get("work_order_facts")
    if work is None or work.state != "supported":
        return None
    equipment_id = work.value.get("equipment_id")
    if not isinstance(equipment_id, str) or not equipment_id:
        return None
    if not _mapping_query_evidence(result, equipment_id):
        return None
    matched = [row for row in rows if str(row.get("equipment_id", "")) == equipment_id]
    if not matched:
        # A zero-row result is a valid stop only when the exact executed query
        # and row-count metadata also produce a traceable absence reference.
        return "mapping_unresolved" if _mapping_absence_ref(result, equipment_id) else None

    invalid_customer_values = {"", "none", "null", "unknown", "n/a", "na"}
    if any(
        not isinstance(row.get("equipment_customer_id"), str)
        or not isinstance(row.get("contract_customer_id"), str)
        or row["equipment_customer_id"].strip().casefold() in invalid_customer_values
        or row["contract_customer_id"].strip().casefold() in invalid_customer_values
        or row["equipment_customer_id"].strip() != row["contract_customer_id"].strip()
        for row in matched
    ):
        return "customer_scope_conflict"

    if any(mapping_basis_status(row.get("mapping_basis", "")) != "allowed"
           for row in matched):
        return "mapping_basis_unapproved"

    work_date = date.fromisoformat(work.value["work_order_date"])
    for row in matched:
        windows = (
            _date(row.get("mapping_valid_from")),
            _date(row.get("mapping_valid_to")),
            _date(row.get("contract_effective_from")),
            _date(row.get("contract_effective_to")),
        )
        if None in windows:
            return None
        mapping_from, mapping_to, contract_from, contract_to = windows
        if not (mapping_from <= work_date <= mapping_to
                and contract_from <= work_date <= contract_to):
            return "validity_window_not_cover"

    active = [row for row in matched if is_active_contract_status(row.get("contract_status"))]
    if len(active) > 1:
        return "ambiguous_active_contracts"
    if not active:
        return "contract_status_inactive"
    return None


def _baseline_mapping(rows, refs, _result, _context, facts):
    """匹配消融组：仅采用首条可用 JOIN 结果，不执行关系与有效期核验。"""
    work = facts["work_order_facts"].value
    if not rows:
        absence_ref = _mapping_absence_ref(_result, str(work.get("equipment_id", "")))
        evidence = (absence_ref,) if absence_ref is not None else ()
        return "未找到可用于检索合同的映射候选。", "insufficient", evidence
    row = next((item for item in rows if item.get("contract_id")), None)
    if row is None:
        return "映射候选缺少合同编号。", "insufficient", ()
    required = (
        "equipment_id", "equipment_customer_id", "contract_customer_id",
        "mapping_basis", "mapping_valid_from", "mapping_valid_to",
        "contract_effective_from", "contract_effective_to",
    )
    if any(row.get(key) is None for key in required):
        return "首条映射候选缺少展示字段。", "insufficient", ()
    # 匹配参考臂只关闭“关系核验”，不允许凭模型叙述制造来源。
    # SQL 执行层可能只返回部分参与表的行级引用（例如模型生成了带别名的
    # 联接查询），因此不能把严格事实核验所需的“三表齐全”条件误用于
    # 参考臂，否则参考臂会在本来可以回答的题上提前拒答。这里保留所有
    # 实际产生的 SQL 引用；没有任何 SQL 引用时仍然拒绝，避免伪造证据。
    evidence = [ref for ref in refs if isinstance(ref, SqlRef)]
    if not evidence:
        return "映射候选缺少 SQL 来源引用。", "insufficient", ()
    return {
        "contract_id": str(row["contract_id"]),
        "equipment_id": str(row["equipment_id"] or work["equipment_id"]),
        "equipment_name": str(row["equipment_name"]) if row.get("equipment_name") else None,
        "equipment_customer_id": str(row["equipment_customer_id"]),
        "contract_customer_id": str(row["contract_customer_id"]),
        "mapping_basis": str(row["mapping_basis"]),
        "mapping_valid_from": str(row["mapping_valid_from"]),
        "mapping_valid_to": str(row["mapping_valid_to"]),
        "contract_effective_from": str(row["contract_effective_from"]),
        "contract_effective_to": str(row["contract_effective_to"]),
    }, "supported", evidence


def _validate_contract_document(result: ToolResult, context, facts):
    mapping = facts["controlled_contract_mapping"].value
    if not isinstance(result.data, str) or not result.data.strip():
        return FactDecision("文档检索没有可用回答。", "insufficient", diagnostic_code="document_retrieval_empty")
    # 候选检索、重排结果与生成模型实际引用是不同集合。判据只从重排后的真实
    # 候选中确定性筛出“合同编号匹配且原文包含目标条款”的片段，不采信答案自述。
    refs = _contract_candidate_refs(result, mapping["contract_id"])
    if not refs:
        return FactDecision("检索结果没有该适用合同的原文引用。", "insufficient", diagnostic_code="document_reference_missing")

    snippet_text = "\n".join(ref.snippet for ref in refs)
    # 合同原文通常按设备名称/型号/站点描述适用范围，不一定写数据库主键。
    # 设备编号到合同的关系已由上游受控 JOIN 独立核验；这里要求原文再明确
    # 覆盖精确设备名称（或直接列出编号），避免把“查到了同合同文档”误当成范围证明。
    equipment_id = str(mapping["equipment_id"])
    equipment_name = str(mapping.get("equipment_name") or "").strip()
    if not _contract_scope_matches_equipment(snippet_text, equipment_id, equipment_name):
        if _contract_scope_explicitly_excludes_equipment(snippet_text, equipment_id, equipment_name):
            return FactDecision(
                "合同原文明确排除该设备，不能将其作为适用合同。", "insufficient", tuple(refs),
                diagnostic_code="document_scope_excluded",
            )
        # 受控结构化映射已经独立证明设备—合同关系时，合同正文不必再次
        # 逐字重复数据库主键或设备名称；只有文档明确排除时才阻断。手工构造
        # 的旧测试/调用未带此标记，继续保留原先的保守行为。
        if not mapping.get("mapping_relation_verified"):
            return FactDecision(
                "合同候选片段未明确覆盖该设备编号或设备名称。", "insufficient", tuple(refs),
                diagnostic_code="document_scope_unproven",
            )
    repair_clauses = [
        (match.group(1), int(match.group(2)), _line_for_match(snippet_text, match.start()))
        for match in _REPAIR_LIMIT_RE.finditer(snippet_text)
    ]
    # 合同正文也可能写成“修复完成应在 3 小时内达到 service_restored”，
    # 不能因没有 Markdown 表格行就丢弃这条原文证据。正文形式已由“修复完成/服务恢复”
    # 锚点限定，且对象范围仍由上面的精确编号/名称校验负责。
    repair_clauses.extend(
        (
            match.group("descriptor"),
            int(match.group("limit")),
            _line_for_match(snippet_text, match.start()),
        )
        for match in _REPAIR_LIMIT_PROSE_RE.finditer(snippet_text)
    )
    signed_limits = {
        int(match.group("limit"))
        for match in _SIGNED_REPAIR_LIMIT_RE.finditer(snippet_text)
    }
    unsigned_supplement_limits = {
        int(match.group("limit"))
        for match in _UNSIGNED_SUPPLEMENT_LIMIT_RE.finditer(snippet_text)
    }
    if ("服务恢复" in snippet_text or "修复完成" in snippet_text) and unsigned_supplement_limits:
        if len(signed_limits) == 1 and any(
            limit != next(iter(signed_limits)) for limit in unsigned_supplement_limits
        ):
            return FactDecision({
                "conflict_type": "unsigned_supplement_unresolved",
                "signed_limit_hours": next(iter(signed_limits)),
                "unsigned_draft_limit_hours": sorted(unsigned_supplement_limits),
                "reason": "签署正文与未签署补充稿时限冲突，档案未消解冲突。",
            }, "ambiguous", tuple(refs), diagnostic_code="document_sla_override_unresolved")
        if not signed_limits:
            return FactDecision({
                "conflict_type": "unsigned_supplement_without_signed_baseline",
                "signed_limit_hours": None,
                "unsigned_draft_limit_hours": sorted(unsigned_supplement_limits),
                "reason": "发现未签署补充稿时限，但未能核实有效签署正文阈值。",
            }, "ambiguous", tuple(refs), diagnostic_code="document_sla_override_unresolved")
    # 现阶段不尝试从重叠范围、否定条款或特别/补充约定中推导优先级。
    # 无法证明唯一阈值适用于当前设备时，宁可停止判定，不取候选中的一个数字。
    response_clauses = [
        (match.group(1), int(match.group(2)) * (60 if match.group(3) == "小时" else 1))
        for match in _FIRST_RESPONSE_LIMIT_RE.finditer(snippet_text)
    ]
    response_clauses.extend(
        ("first_response", int(match.group("limit")) * (60 if match.group("unit") == "小时" else 1))
        for match in _FIRST_RESPONSE_LIMIT_PROSE_RE.finditer(snippet_text)
    )
    response_applicable = all(
        _repair_clause_targets_equipment(descriptor, equipment_id, equipment_name,
                                         generic_terms=("first_response", "首次响应"))
        for descriptor, _ in response_clauses
    )
    response_limits = {limit for _, limit in response_clauses} if response_applicable else set()
    applicable_repair_clauses, unresolved_scope = _applicable_repair_clauses(
        repair_clauses, equipment_id, equipment_name
    )
    if unresolved_scope:
        return FactDecision(
            "修复阈值所在条款未明确关联到本设备。", "insufficient", tuple(refs),
            diagnostic_code="document_sla_scope_unproven",
        )
    limits = {limit for _, limit, _ in applicable_repair_clauses}
    if len(limits) > 1:
        return FactDecision(
            "同一合同检索到多个修复时限，特别约定适用关系未由本节点确认。",
            "ambiguous",
            tuple(refs),
            diagnostic_code="document_sla_ambiguous",
        )
    if not limits:
        if repair_clauses:
            return FactDecision(
                "检索到的修复时限均明确针对其他设备，本设备没有可用的时限条款。",
                "insufficient", tuple(refs),
                diagnostic_code="document_sla_scope_unproven",
            )
        reason = "合同原文明确未约定修复完成时限。" if _explicitly_unspecified(snippet_text) else "合同原文片段未显示修复完成时限。"
        return FactDecision(
            reason,
            "insufficient",
            tuple(refs),
            diagnostic_code="document_sla_unstated" if _explicitly_unspecified(snippet_text) else "document_sla_unparsed",
        )
    if _has_sla_override(snippet_text):
        return FactDecision(
            "合同证据含有例外或优先约定，当前核验器不能可靠确定适用时限。",
            "ambiguous",
            tuple(refs),
            diagnostic_code="document_sla_override_unresolved",
        )
    # 修复耗时采用已注册的数据口径 accepted → service_restored：事件含义来自
    # 本次运行包的数据字典，具体时间来自事件行；合同文档负责提供适用范围和 SLA 阈值，
    # 不要求业务文档复述数据库内部事件代码。
    return FactDecision({
        "limit_hours": next(iter(limits)),
        "first_response_limit_minutes": next(iter(response_limits)) if len(response_limits) == 1 else None,
        "first_response_clause_status": "supported" if len(response_limits) == 1 else "ambiguous" if len(response_limits) > 1 else "insufficient",
        "contract_id": mapping["contract_id"],
    }, "supported", tuple(refs))


def _contract_candidate_refs(result: ToolResult, contract_id: str) -> list[DocRef]:
    """两组共享同一已重排候选快照，再分别应用严格/宽松判据。"""
    rerank_steps = [step for step in result.trace if step.tool == "rerank"]
    if rerank_steps:
        # 有重排记录时，它就是本次决策候选边界；未命中必须拒判，不能用
        # 生成答案的 refs 或前一轮重排结果绕过最终候选筛选。
        refs = [ref for ref in (rerank_steps[-1].hit or [])
                if isinstance(ref, DocRef) and ref.doc_id == contract_id]
    else:
        refs = [ref for ref in result.refs if isinstance(ref, DocRef) and ref.doc_id == contract_id]
    return refs


def _contract_scope_matches_equipment(text: str, equipment_id: str, equipment_name: str) -> bool:
    """只接受适用范围句中的精确编号/名称；相关排除语句一律阻断。"""
    positive = False
    excluded = False
    exact_name_pattern = (
        re.compile(rf"^{re.escape(equipment_name)}(?=$|[\s|：:（(])")
        if equipment_name else None
    )
    exact_id_pattern = re.compile(rf"^{re.escape(equipment_id)}(?![\w-])")
    exact_id_search = re.compile(rf"(?<![\w-]){re.escape(equipment_id)}(?![\w-])")
    exact_name_search = (
        re.compile(rf"(?<![\w-]){re.escape(equipment_name)}(?![\w-])")
        if equipment_name else None
    )
    lines = text.splitlines()
    for line_index, line in enumerate(lines):
        matches = list(_SCOPE_MARKER_RE.finditer(line))
        for index, scope_match in enumerate(matches):
            # Limit evidence to this scope clause. Sentence/list separators and
            # a second scope marker close it, preventing unrelated mentions.
            clause_end = len(line)
            for separator in ("。", "；", ";", "|"):
                position = line.find(separator, scope_match.end())
                if position >= 0:
                    clause_end = min(clause_end, position)
            if index + 1 < len(matches):
                clause_end = min(clause_end, matches[index + 1].start())
            scope_clause = line[scope_match.end():clause_end]
            target_mentioned = False
            for item_index, item in enumerate(re.split(r"[，,、]", scope_clause)):
                candidate = item.strip()
                if exact_id_pattern.match(candidate) or (
                    exact_name_pattern is not None and exact_name_pattern.match(candidate)
                ):
                    target_mentioned = True
                    break
                if item_index == 0:
                    candidate = re.sub(
                        r"^(?:仅|以下)?\s*(?:设备(?:编号|名称)?)?\s*[:：]?\s*",
                        "",
                        candidate,
                    )
                if exact_id_pattern.match(candidate) or (
                    exact_name_pattern is not None and exact_name_pattern.match(candidate)
                ):
                    target_mentioned = True
                    break
            if target_mentioned:
                if _SCOPE_EXCLUSION_RE.search(line):
                    excluded = True
                else:
                    positive = True
            # 合同正文常把“适用于某类设备”和“其中包含设备编号”写成相邻
            # 两句，而不是同一张表格行。只检查紧邻的下一句，不把逗号后或
            # 更远处的其他设备描述误当成适用范围。
            if not target_mentioned:
                first_sentence_end = line.find("。", scope_match.end())
                if first_sentence_end >= 0:
                    next_sentence_end = line.find("。", first_sentence_end + 1)
                    if next_sentence_end < 0:
                        next_sentence_end = len(line)
                    window = line[first_sentence_end + 1:next_sentence_end]
                    if (exact_id_search.search(window) or
                            (exact_name_search is not None and exact_name_search.search(window))):
                        if _SCOPE_EXCLUSION_RE.search(window):
                            excluded = True
                        else:
                            positive = True
    # 有些合同把范围写在小节标题下，而不是使用“适用于”句式，例如：
    # “## 设备范围”下一行列出设备编号。只在明确的范围标题对应的小节内
    # 查找精确编号/名称，直到下一个 Markdown 小节，避免跨章节串联。
    for line_index, line in enumerate(lines):
        if not _SCOPE_HEADING_RE.match(line):
            continue
        section_lines: list[str] = []
        for following in lines[line_index + 1:]:
            if re.match(r"^\s*#{1,6}\s+", following):
                break
            section_lines.append(following)
        section = "\n".join(section_lines)
        if (exact_id_search.search(section) or
                (exact_name_search is not None and exact_name_search.search(section))):
            if _SCOPE_EXCLUSION_RE.search(section):
                excluded = True
            else:
                positive = True
    return positive and not excluded


def _contract_scope_explicitly_excludes_equipment(
    text: str, equipment_id: str, equipment_name: str
) -> bool:
    """只在排除语句同时点名当前设备时返回 True。

    文档没有重复设备编号不等于排除；只有明确写出“不适用/不包括”等否定
    范围并命中当前编号或名称，才允许阻断已核验的结构化映射。
    """
    identifiers = [value.strip() for value in (equipment_id, equipment_name) if value and value.strip()]
    if not identifiers:
        return False
    for line in text.splitlines():
        if not _SCOPE_EXCLUSION_RE.search(line):
            continue
        if any(re.search(rf"(?<![\w-]){re.escape(identifier)}(?![\w-])", line)
               for identifier in identifiers):
            return True
    return False


def _repair_clause_targets_equipment(
    descriptor: str | None, equipment_id: str, equipment_name: str,
    *, generic_terms: tuple[str, ...] = ("service_restored", "服务恢复", "修复完成"),
) -> bool:
    """Require a generic contract-level metric or an explicit target in the SLA row."""
    value = (descriptor or "").strip().strip("` ").casefold()
    if not value:
        # A one-column repair threshold is treated as the contract-level SLA;
        # its separate scope clause has already been checked above.
        return True
    compact = re.sub(r"\s+", "", value)
    normalized_id = equipment_id.casefold()
    identifiers = re.findall(
        r"(?<![A-Z0-9])[A-Z]{1,8}[-_]?[A-Z0-9]*\d[A-Z0-9_-]*(?![A-Z0-9])",
        (descriptor or "").upper(),
    )
    if any(identifier.casefold() != equipment_id.casefold() for identifier in identifiers):
        return False
    if normalized_id and re.search(
        rf"(?<![a-z0-9_-]){re.escape(normalized_id)}(?![a-z0-9_-])", value
    ):
        return True
    escaped_name = re.escape(equipment_name.strip().casefold()).replace(r"\ ", r"\s+")
    if escaped_name and re.search(
        rf"(?<![a-z0-9_-]){escaped_name}(?=$|[\s，,、|：:（(]|故障|修复|维修|报修|服务|恢复|运行|耗时)",
        value,
    ):
        return True
    global_scope_terms = ("所有适用设备", "合同范围内设备", "全部适用设备")
    if any(term in compact for term in global_scope_terms):
        return True
    if re.search(r"(?:设备|泵|阀|控制器|仪器|传感器|电机|主机|机组|系统|平台|机柜|终端|服务器)", value):
        return False
    compact_without_underscores = compact.replace("_", "")
    return any(term.replace("_", "") in compact_without_underscores for term in generic_terms)


def _line_for_match(text: str, position: int) -> str:
    """Return the source line containing a clause match for scope interpretation."""
    start = text.rfind("\n", 0, position) + 1
    end = text.find("\n", position)
    if end < 0:
        end = len(text)
    return text[start:end]


def _applicable_repair_clauses(
    clauses: list[tuple[str | None, int, str]],
    equipment_id: str,
    equipment_name: str,
) -> tuple[list[tuple[str | None, int, str]], bool]:
    """Filter repair SLAs by equipment before checking whether the value is unique.

    A clause that names the queried asset is more specific than the contract-level
    baseline and therefore supersedes that baseline for the same metric. Clauses
    naming another asset are excluded. If a clause appears equipment-scoped but
    cannot be matched reliably, fail closed instead of treating it as generic.
    """
    equipment_id_pattern = (
        re.compile(rf"(?<![\w-]){re.escape(equipment_id)}(?![\w-])", re.IGNORECASE)
        if equipment_id else None
    )
    normalized_name = equipment_name.strip().casefold()
    equipment_marker = re.compile(
        r"(?:设备|泵|阀|控制器|仪器|传感器|电机|主机|机组|系统|平台|机柜|终端|服务器|"
        r"自动扶梯|站台门|屏蔽门)"
    )
    specific: list[tuple[str | None, int, str]] = []
    generic: list[tuple[str | None, int, str]] = []
    unresolved_scope = False

    for clause in clauses:
        descriptor, _limit, source_line = clause
        line = source_line.strip()
        has_target_id = bool(equipment_id_pattern and equipment_id_pattern.search(line))
        has_target_name = bool(normalized_name and normalized_name in line.casefold())
        if has_target_id or has_target_name:
            specific.append(clause)
            continue
        if _REPAIR_EQUIPMENT_ID_RE.search(line):
            # The line is explicitly scoped, but to another equipment id.
            continue
        descriptor_text = (descriptor or "").strip().casefold()
        if equipment_marker.search(line) or equipment_marker.search(descriptor_text):
            unresolved_scope = True
            continue
        generic.append(clause)

    # A matched device tier wins over a generic baseline; unrelated device
    # clauses do not enter the uniqueness check for this equipment.
    return (specific if specific else generic), unresolved_scope


def _explicitly_unspecified(text: str) -> bool:
    """拒绝把“并非未约定”等双重否定误读为合同明确无时限。"""
    for sentence in re.split(r"[。；;\n]", text):
        if _UNSPECIFIED_RE.search(sentence) and not _NEGATED_UNSPECIFIED_RE.search(sentence):
            return True
    return False


def _has_sla_override(text: str) -> bool:
    """识别时限优先/例外条款。

    两类必须排除，否则会把「口径说明」误读成「优先约定」，导致
    一个本来能定的时限被 fail closed 成 ambiguous：

    ① **时区口径**：「以上时限以 Asia/Shanghai 时区为准」——纯粹在说时间用哪个
       时区，不改变时限数值。
    ② **度量口径**：「修复完成以服务恢复事件为准」「以设备编号为准」——在说
       用哪个事件/字段来度量，也不改变时限数值（见 `_SLA_DEFINITION_RE`）。
    """
    for match in _SLA_OVERRIDE_RE.finditer(text):
        fragment = match.group(0)
        if "Asia/Shanghai" in fragment or "UTC" in fragment:
            continue
        if _SLA_DEFINITION_RE.search(fragment):
            continue
        return True
    return False


def _baseline_contract_document(result: ToolResult, context, facts):
    """匹配消融组：从同一 RAG 检索候选取首个可解析条款，不判适用范围/冲突。"""
    mapping = facts["controlled_contract_mapping"].value
    refs = _contract_candidate_refs(result, mapping["contract_id"])
    if not refs:
        return FactDecision("检索结果没有该合同的文档引用。", "insufficient")
    text = "\n".join(ref.snippet for ref in refs)
    repair_match = _REPAIR_LIMIT_RE.search(text)
    if repair_match is None:
        return FactDecision("检索片段中未解析到修复时限。", "insufficient", tuple(refs))
    response_limit = None
    if _requests_first_response(context):
        response_match = _FIRST_RESPONSE_LIMIT_RE.search(text)
        if response_match is not None:
            # group(1) is the optional clause descriptor; the numeric value
            # is group(2), followed by the unit in group(3).  The matched
            # reference must not fail merely because a first-response clause
            # uses the descriptor form of the shared table regex.
            response_limit = int(response_match.group(2)) * (
                60 if response_match.group(3) == "小时" else 1
            )
    return FactDecision({
        "limit_hours": int(repair_match.group(2)),
        "first_response_limit_minutes": response_limit,
        "first_response_clause_status": "supported" if response_limit is not None else "insufficient",
        "contract_id": mapping["contract_id"],
    }, "supported", tuple(refs))


def _document_validation_trace(result: ToolResult, decision: FactDecision) -> ToolResult:
    value = decision.value
    trace_step = TraceStep(
        step_id=len(result.trace) + 1,
        tool="cross_source_doc_validation",
        input={"candidate_doc_ids": sorted({ref.doc_id for ref in decision.evidence if isinstance(ref, DocRef)})},
        output={
            "status": decision.state,
            "diagnostic_code": decision.diagnostic_code,
            "contract_id": value.get("contract_id") if isinstance(value, dict) else None,
            "service_restored_limit_hours": value.get("limit_hours") if isinstance(value, dict) else None,
            "first_response_limit_minutes": value.get("first_response_limit_minutes") if isinstance(value, dict) else None,
            "first_response_clause_status": value.get("first_response_clause_status") if isinstance(value, dict) else None,
            "selected_ref_count": len(decision.evidence),
            "answer_text_used_as_fact": False,
        },
        elapsed_ms=0.0,
        hit=list(decision.evidence),
    )
    refs = list(result.refs)
    known = {(ref.doc_id, ref.char_start, ref.char_end, ref.page, ref.section_path) for ref in refs if isinstance(ref, DocRef)}
    for ref in decision.evidence:
        if not isinstance(ref, DocRef):
            continue
        key = (ref.doc_id, ref.char_start, ref.char_end, ref.page, ref.section_path)
        if key not in known:
            refs.append(ref)
            known.add(key)
    return result.model_copy(update={"trace": [*result.trace, trace_step], "refs": refs})


def _validate_duration(rows, refs, _result, context, facts):
    work_order_id = _work_order_id(context)
    evidence = _table_evidence(refs, {"work_order_events"})
    if evidence is None:
        return "事件结果缺少工单事件表的可追溯来源。", "insufficient", ()
    scoped_rows = [row for row in rows if str(row.get("work_order_id", "")).upper() == work_order_id]
    if len(scoped_rows) != len(rows):
        return "事件查询混入其他工单记录，不能计算修复耗时。", "insufficient", evidence
    accepted = [_datetime(row.get("event_at")) for row in scoped_rows if row.get("event_type") == "accepted"]
    restored = [_datetime(row.get("event_at")) for row in scoped_rows if row.get("event_type") == "service_restored"]
    first_response = [_datetime(row.get("event_at")) for row in scoped_rows if row.get("event_type") == "first_response"]
    include_first_response = _requests_first_response(context)
    if len(accepted) != 1 or len(restored) != 1 or accepted[0] is None or restored[0] is None:
        return "受理或服务恢复事件缺失/重复，不能计算修复耗时。", "insufficient", evidence
    if accepted[0].tzinfo is None or restored[0].tzinfo is None:
        return "事件时间缺少时区，不能安全对齐时间口径。", "insufficient", evidence
    cited_event_types = _cited_event_types(evidence, work_order_id, {"accepted", "service_restored"})
    if cited_event_types != {"accepted", "service_restored"}:
        return "事件查询引用没有同时覆盖受理和服务恢复记录。", "insufficient", evidence
    first_response_seconds = None
    first_response_status = "insufficient"
    first_response_message = "首次响应事件缺失或重复，不能计算首次响应耗时。"
    if len(first_response) == 1 and first_response[0] is not None:
        first_response_refs = _cited_event_types(evidence, work_order_id, {"accepted", "first_response"})
        if first_response[0].tzinfo is None:
            first_response_message = "首次响应时间缺少时区，不能安全对齐时间口径。"
        elif include_first_response and first_response_refs != {"accepted", "first_response"}:
            first_response_message = "首次响应查询引用没有同时覆盖受理和首次响应事件。"
        else:
            first_response_seconds = (first_response[0] - accepted[0]).total_seconds()
            if first_response_seconds < 0:
                first_response_seconds = None
                first_response_status = "contradicted"
                first_response_message = "首次响应时间早于受理时间，事件时间倒序，停止首次响应判定。"
            else:
                first_response_status = "supported"
                first_response_message = "首次响应事件及时间顺序均可核验。"
    duration_seconds = (restored[0] - accepted[0]).total_seconds()
    if duration_seconds < 0:
        return "服务恢复时间早于受理时间，事件时间倒序，停止时限判定。", "contradicted", evidence
    return {
        "work_order_id": work_order_id,
        "accepted_at": accepted[0].isoformat(),
        "service_restored_at": restored[0].isoformat(),
        "duration_seconds": duration_seconds,
        "first_response_event_count": len(first_response),
        "first_response_at": first_response[0].isoformat() if len(first_response) == 1 and first_response[0] is not None else None,
        "first_response_duration_seconds": first_response_seconds,
        "first_response_status": first_response_status,
        "first_response_message": first_response_message,
    }, "supported", evidence


def _cited_event_types(refs: list[RefT], work_order_id: str, allowed: set[str]) -> set[str]:
    """仅认可主键同时精确包含当前工单号和完整事件类型的行级引用。"""
    found: set[str] = set()
    for ref in refs:
        if not isinstance(ref, SqlRef) or not ref.row_pk:
            continue
        primary_key: dict[str, str] = {}
        for component in ref.row_pk.split("&"):
            key, separator, value = component.partition("=")
            if separator:
                primary_key[key.strip()] = value.strip()
        if primary_key.get("work_order_id", "").upper() != work_order_id:
            continue
        event_type = primary_key.get("event_type", "")
        if event_type in allowed:
            found.add(event_type)
    return found


def _baseline_duration(rows, refs, _result, context, _facts):
    """匹配消融组：按事件类型计算时差，但不检查倒序与逐事件引用覆盖。"""
    work_order_id = _work_order_id(context)
    evidence = _table_evidence(refs, {"work_order_events"})
    if not evidence:
        return "事件结果缺少 SQL 来源引用。", "insufficient", ()
    accepted = [_datetime(row.get("event_at")) for row in rows if row.get("event_type") == "accepted"]
    restored = [_datetime(row.get("event_at")) for row in rows if row.get("event_type") == "service_restored"]
    first_response = [_datetime(row.get("event_at")) for row in rows if row.get("event_type") == "first_response"]
    if len(accepted) != 1 or len(restored) != 1 or accepted[0] is None or restored[0] is None:
        return "无法从事件结果中唯一计算修复耗时。", "insufficient", evidence
    if accepted[0].tzinfo is None or restored[0].tzinfo is None:
        return "事件时间缺少时区。", "insufficient", evidence
    first_response_seconds = None
    first_response_at = None
    if len(first_response) == 1 and first_response[0] is not None:
        first_response_at = first_response[0].isoformat()
        first_response_seconds = (first_response[0] - accepted[0]).total_seconds()
    return {
        "work_order_id": work_order_id,
        "accepted_at": accepted[0].isoformat(),
        "service_restored_at": restored[0].isoformat(),
        "duration_seconds": (restored[0] - accepted[0]).total_seconds(),
        "first_response_event_count": len(first_response),
        "first_response_at": first_response_at,
        "first_response_duration_seconds": first_response_seconds,
        "first_response_status": "supported" if first_response_seconds is not None else "insufficient",
        "first_response_message": "首次响应事件可计算。" if first_response_seconds is not None else "首次响应事件缺失或重复。",
    }, "supported", evidence


def _make_first_response_duration_handler(*, verification_enabled: bool = True):
    def decide(_context, facts):
        source = facts.get("repair_duration")
        if source is None or source.state != "supported":
            return FactTaskOutput(FactDecision("事件查询未受支持，不能派生首次响应耗时。", "insufficient"))
        value = source.value
        if value.get("first_response_status") != "supported" or value.get("first_response_duration_seconds") is None:
            state = value.get("first_response_status")
            if state not in {"insufficient", "contradicted"}:
                state = "insufficient"
            return FactTaskOutput(FactDecision(
                value.get("first_response_message") or "首次响应事件不足，不能计算首次响应耗时。",
                state,
            ))
        refs = tuple(
            ref for ref in _record_refs(source)
            if isinstance(ref, SqlRef)
            and any(token in (ref.row_pk or "") or token in ref.snippet for token in ("accepted", "first_response"))
        )
        if verification_enabled:
            covered = {
                token for ref in refs for token in ("accepted", "first_response")
                if token in (ref.row_pk or "") or token in ref.snippet
            }
            if covered != {"accepted", "first_response"}:
                return FactTaskOutput(FactDecision("首次响应耗时缺少受理或首次响应事件引用。", "insufficient", refs))
        return FactTaskOutput(FactDecision({
            "work_order_id": value["work_order_id"],
            "accepted_at": value["accepted_at"],
            "first_response_at": value["first_response_at"],
            "duration_seconds": float(value["first_response_duration_seconds"]),
        }, "supported", refs))

    return decide


def _make_first_response_verdict_handler(*, verification_enabled: bool = True):
    def decide(_context, facts):
        clause = facts.get("contract_repair_limit")
        duration = facts.get("first_response_duration")
        if clause is None or clause.state != "supported" or duration is None or duration.state != "supported":
            return FactTaskOutput(FactDecision("首次响应条款或事件耗时未受支持，无法判断。", "insufficient"))
        limit_minutes = clause.value.get("first_response_limit_minutes")
        if not isinstance(limit_minutes, int) or limit_minutes <= 0:
            clause_state = clause.value.get("first_response_clause_status")
            if clause_state == "ambiguous":
                return FactTaskOutput(FactDecision("合同候选中存在冲突的首次响应时限，无法唯一判定。", "ambiguous"))
            return FactTaskOutput(FactDecision("检索到的合同证据未能支持唯一的首次响应时限。", "insufficient"))
        duration_seconds = float(duration.value["duration_seconds"])
        limit_seconds = limit_minutes * 60
        verdict = "满足" if duration_seconds <= limit_seconds else "超时"
        unique_refs: dict[tuple[str, ...], RefT] = {}
        for record in (clause, duration):
            for ref in _record_refs(record):
                unique_refs.setdefault(evidence_key(ref), ref)
        refs = tuple(unique_refs.values())
        result_value = {"limit_minutes": limit_minutes, "duration_seconds": duration_seconds, "verdict": verdict}
        trace = TraceStep(
            step_id=1,
            tool="cross_source_alignment",
            input={"work_order_id": duration.value["work_order_id"], "metric": "first_response"},
            output={
                "status": "supported",
            "validator_mode": "strict_cross_source_verification" if verification_enabled else "relaxed_rule_ablation",
                "metric": "first_response",
                "duration_seconds": duration_seconds,
                "limit_seconds": limit_seconds,
                "verdict": verdict,
                "metric_checks": [{
                    "metric": "first_response",
                    "start_event": "accepted",
                    "end_event": "first_response",
                    "elapsed_seconds": duration_seconds,
                    "limit": limit_minutes,
                    "unit": "minutes",
                    "within_limit": duration_seconds <= limit_seconds,
                }],
            },
            elapsed_ms=0.0,
            hit=list(refs),
        )
        return FactTaskOutput(
            FactDecision(result_value, "supported", refs),
            ToolResult(data=result_value, trace=[trace], refs=list(refs)),
        )

    return decide


def _make_verdict_handler(*, verification_enabled: bool = True):
    def decide(context, facts):
        required = ("controlled_contract_mapping", "contract_repair_limit", "repair_duration")
        for task_id in required:
            record = facts.get(task_id)
            if record is None or record.state != "supported":
                return FactTaskOutput(FactDecision("必要前置事实未受支持，无法判断是否满足合同修复时限。", "insufficient"))

        mapping = facts["controlled_contract_mapping"].value
        clause = facts["contract_repair_limit"].value
        duration = facts["repair_duration"].value
        limit_seconds = float(clause["limit_hours"]) * 3600
        duration_seconds = float(duration["duration_seconds"])
        verdict = "满足" if duration_seconds <= limit_seconds else "超时"
        unique_refs: list[RefT] = []
        seen_ref_keys: set[tuple[str, ...]] = set()
        response_record = facts.get("first_response_verdict")
        evidence_tasks = list(required)
        if response_record is not None and response_record.state == "supported":
            evidence_tasks.append("first_response_verdict")
        for task_id in evidence_tasks:
            for ref in _record_refs(facts[task_id]):
                ref_key = evidence_key(ref)
                if ref_key not in seen_ref_keys:
                    unique_refs.append(ref)
                    seen_ref_keys.add(ref_key)
        refs = tuple(unique_refs)
        answer_parts = [
            f"工单 {duration['work_order_id']} 对应设备 {mapping['equipment_id']}，"
            f"适用合同 {mapping['contract_id']} 的修复时限为 {clause['limit_hours']} 小时；"
            f"受理至服务恢复耗时 {_readable_duration(duration_seconds)}，判定为{verdict}。"
        ]
        if mapping.get("date_interval_policy") == "closed":
            answer_parts.append("映射有效期与合同有效期按闭区间核验，起始日和截止日均包含。")
        excluded_candidates = mapping.get("excluded_candidates", [])
        if excluded_candidates:
            excluded_text = "；".join(
                f"{item['contract_id']}：{'、'.join(item['reasons'])}"
                for item in excluded_candidates
            )
            answer_parts.append("其他映射候选已逐项核查并排除：" + excluded_text + "。")
        work_record = facts.get("work_order_facts")
        if work_record is not None and isinstance(work_record.value, dict):
            opened_at = work_record.value.get("opened_at")
            if opened_at:
                # 与 orchestrator._fact_plan_answer 同一口径：不要把内部字段名
                # `opened_at=` 当成用户文案拼进答案。
                answer_parts.insert(0, f"工单受理时间 {_readable_time(opened_at)}；")
        if response_record is not None:
            if response_record.state == "supported":
                response_value = response_record.value
                response_minutes = float(response_value["duration_seconds"]) / 60
                answer_parts.insert(
                    0,
                    f"首次响应耗时 {response_minutes:g} 分钟，时限 {response_value['limit_minutes']} 分钟，"
                    f"判定为{response_value['verdict']}。",
                )
            elif response_record.value:
                answer_parts.insert(0, f"首次响应无法判断：{response_record.value}。")
            elif _requests_first_response(context):
                response_duration = facts.get("first_response_duration")
                response_reason = (
                    response_duration.value
                    if response_duration is not None and response_duration.value
                    else "必要事件或条款证据不足。"
                )
                answer_parts.insert(0, f"首次响应无法判断：{response_reason}。")
        elif _requests_first_response(context):
            response_duration = facts.get("first_response_duration")
            response_reason = (
                response_duration.value
                if response_duration is not None and response_duration.value
                else "必要事件或条款证据不足。"
            )
            answer_parts.insert(0, f"首次响应无法判断：{response_reason}。")
        answer = "".join(answer_parts)
        metric_checks = [{
            "metric": "service_restored",
            "start_event": "accepted",
            "end_event": "service_restored",
            "elapsed_seconds": duration_seconds,
            "limit": clause["limit_hours"],
            "unit": "hours",
            "within_limit": duration_seconds <= limit_seconds,
        }]
        if response_record is not None and response_record.state == "supported":
            response_value = response_record.value
            metric_checks.append({
                "metric": "first_response",
                "start_event": "accepted",
                "end_event": "first_response",
                "elapsed_seconds": float(response_value["duration_seconds"]),
                "limit": response_value["limit_minutes"],
                "unit": "minutes",
                "within_limit": response_value["verdict"] == "满足",
            })
        trace = TraceStep(
            step_id=1,
            tool="cross_source_alignment",
            input={"work_order_id": duration["work_order_id"], "contract_id": mapping["contract_id"]},
            output={
                "status": "supported",
                "validator_mode": "strict_cross_source_verification" if verification_enabled else "relaxed_rule_ablation",
                "mapping_basis": mapping["mapping_basis"],
                "mapping_and_contract_windows_checked_separately": verification_enabled,
                "mapping_interval_policy": mapping.get("date_interval_policy"),
                "excluded_mapping_candidates": excluded_candidates,
                "customer_scope_checked": verification_enabled,
                "event_order_checked": verification_enabled,
                "duration_seconds": duration_seconds,
                "limit_seconds": limit_seconds,
                "verdict": verdict,
                "metric_checks": metric_checks,
                "first_response": response_record.value if response_record is not None else None,
            },
            elapsed_ms=0.0,
            hit=list(refs),
        )
        result = ToolResult(data=answer, trace=[trace], refs=list(refs))
        return FactTaskOutput(FactDecision(answer, "supported", refs), result)

    return decide


def _record_refs(record: FactRecord) -> list[RefT]:
    return list(record.evidence_refs)


def _mapping_query_evidence(result: ToolResult, equipment_id: str | None = None) -> bool:
    """确认三表内连接和设备编号精确过滤。

    A filter on ``equipment.equipment_id`` is equivalent to one on
    ``contract_equipment.equipment_id`` only after the corresponding inner
    join has been proven. This accepts equivalent SQL spellings without
    accepting a name join, an outer/OR-expanded predicate, or a pre-filtered
    first-row query.
    """
    executed_sql = next(
        (
            step.output.get("sql")
            for step in reversed(result.trace)
            if isinstance(step.output.get("sql"), str)
            and "row_count" in step.output
            and not step.output.get("error")
        ),
        None,
    )
    if not executed_sql:
        return False
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(executed_sql, read="postgres")
    except Exception:
        return False

    aliases: dict[str, str] = {}
    tables: set[str] = set()
    for table in tree.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name:
            continue
        tables.add(name)
        aliases[(table.alias_or_name or name).lower()] = name
    if not {"contract_equipment", "equipment", "contracts"}.issubset(tables):
        return False

    def _column_key(column: exp.Column) -> tuple[str, str] | None:
        table = aliases.get((column.table or "").lower(), (column.table or "").lower())
        name = column.name.lower()
        return (table, name) if table and name else None

    # Only equality predicates in JOIN ... ON establish a relation used to
    # propagate an exact identifier filter. WHERE equalities do not count as
    # joins, preventing a disconnected three-table query from passing.
    join_equalities: set[frozenset[tuple[str, str]]] = set()
    for join in tree.find_all(exp.Join):
        if join.side or join.kind.upper() not in {"", "INNER"}:
            return False
        on = join.args.get("on")
        if on is None or any(isinstance(node, (exp.Or, exp.Not)) for node in on.walk()):
            return False
        for equality in on.find_all(exp.EQ):
            left, right = equality.this, equality.expression
            if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
                continue
            left_key, right_key = _column_key(left), _column_key(right)
            if left_key and right_key:
                join_equalities.add(frozenset({left_key, right_key}))

    required_joins = {
        frozenset({("contract_equipment", "equipment_id"), ("equipment", "equipment_id")}),
        frozenset({("contract_equipment", "contract_id"), ("contracts", "contract_id")}),
    }
    if not required_joins.issubset(join_equalities):
        return False
    if equipment_id is None:
        return True

    where = tree.args.get("where")
    if where is None:
        return False
    # An OR can make a matching branch non-authoritative. The verifier accepts
    # only a conjunctive exact filter.
    if any(isinstance(node, (exp.Or, exp.Not)) for node in where.walk()):
        return False
    equivalent_equipment_columns = {
        ("contract_equipment", "equipment_id"),
        ("equipment", "equipment_id"),
    }
    for equality in where.find_all(exp.EQ):
        left, right = equality.this, equality.expression
        if isinstance(left, exp.Literal) and isinstance(right, exp.Column):
            left, right = right, left
        left_key = _column_key(left) if isinstance(left, exp.Column) else None
        if (
            isinstance(left, exp.Column)
            and left_key in equivalent_equipment_columns
            and isinstance(right, exp.Literal)
            and right.is_string
            and right.this == equipment_id
        ):
            return True
    return False


def _mapping_absence_trace(result: ToolResult, decision: FactDecision) -> ToolResult:
    """Attach executed-query absence metadata or a versioned mapping-policy check."""
    transformed = result
    if decision.stop_reason_code == "mapping_basis_unapproved":
        policy = load_mapping_basis_policy()
        observed_bases: set[str] = set()
        for ref in decision.evidence:
            if not isinstance(ref, SqlRef) or ref.table.split(".")[-1] != "contract_equipment":
                continue
            match = re.search(r"依据\s+([^。;]+)", ref.snippet)
            if match:
                observed_bases.add(match.group(1).strip())
        observations = [
            {"mapping_basis": basis, "status": mapping_basis_status(basis)}
            for basis in sorted(observed_bases)
        ]
        policy_step = TraceStep(
            step_id=len(transformed.trace) + 1,
            tool="cross_source_policy_check",
            input={"policy_id": policy["policy_id"], "version": policy["version"]},
            output={
                "status": "mapping_basis_unapproved",
                "policy_status": policy.get("status"),
                "source": policy["source"],
                "allowed_bases": policy["allowed"],
                "observations": observations,
            },
            elapsed_ms=0.0,
            hit=list(decision.evidence),
        )
        transformed = transformed.model_copy(update={
            "trace": [*transformed.trace, policy_step],
        })

    if (
        decision.state != "insufficient"
        or not isinstance(decision.value, str)
        or decision.value not in {
            "未找到设备编号对应的合同映射。",
            "未找到可用于检索合同的映射候选。",
        }
        or not _mapping_query_evidence(transformed)
    ):
        return transformed
    executed = next((
        step for step in reversed(transformed.trace)
        if isinstance(step.output.get("sql"), str) and "row_count" in step.output
        and not step.output.get("error")
    ), None)
    if executed is None:
        return transformed
    exact_equipment_id = _mapping_exact_filter_value(executed.output["sql"])
    if exact_equipment_id is None:
        return transformed
    absence_ref = _mapping_absence_ref(transformed, exact_equipment_id)
    if absence_ref is None:
        return transformed
    # 绝不把答案文字自称“未找到”当作零行证据。过滤字段和值从实际
    # 执行 SQL 的 AST 提取，并与实际 row_count 一起落到 trace 元数据。
    step = TraceStep(
        step_id=len(transformed.trace) + 1,
        tool="cross_source_absence_check",
        input={"table": "contract_equipment", "filters": {"equipment_id": exact_equipment_id}},
        output={
            "table": "contract_equipment",
            "filters": {"equipment_id": exact_equipment_id},
            "row_count": executed.output["row_count"],
            "derived_from_executed_query": True,
        },
        elapsed_ms=0.0,
        hit=[absence_ref],
    )
    refs = list(transformed.refs)
    if evidence_key(absence_ref) not in {evidence_key(ref) for ref in refs}:
        refs.append(absence_ref)
    return transformed.model_copy(update={"trace": [*transformed.trace, step], "refs": refs})


def _mapping_absence_ref(result: ToolResult, equipment_id: str) -> SqlRef | None:
    """Create row-level negative evidence only from a verified zero-row SQL query."""
    if not _mapping_query_evidence(result, equipment_id):
        return None
    executed = next((
        step for step in reversed(result.trace)
        if isinstance(step.output.get("sql"), str) and "row_count" in step.output
        and not step.output.get("error")
    ), None)
    if (
        executed is None
        or type(executed.output.get("row_count")) is not int
        or executed.output.get("row_count") != 0
        or _mapping_exact_filter_value(executed.output["sql"]) != equipment_id
    ):
        return None
    return SqlRef(
        kind="sql",
        table="contract_equipment",
        column="equipment_id",
        row_pk=None,
        snippet=(
            f"按 contract_equipment.equipment_id={equipment_id} 精确查询，"
            "数据库实际返回 0 行。"
        ),
    )


def _mapping_exact_filter_value(executed_sql: str) -> str | None:
    """Extract one exact equipment id from an already validated mapping query."""
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(executed_sql, read="postgres")
        aliases = {
            (table.alias_or_name or table.name).lower(): table.name.lower()
            for table in tree.find_all(exp.Table)
        }
        where = tree.args.get("where")
        if where is None or any(isinstance(node, (exp.Or, exp.Not)) for node in where.walk()):
            return None
        values: set[str] = set()
        for equality in where.find_all(exp.EQ):
            left, right = equality.this, equality.expression
            if isinstance(left, exp.Literal) and isinstance(right, exp.Column):
                left, right = right, left
            table = aliases.get((left.table or "").lower(), (left.table or "").lower()) if isinstance(left, exp.Column) else ""
            if (
                table in {"equipment", "contract_equipment"}
                and isinstance(left, exp.Column)
                and left.name.lower() == "equipment_id"
                and isinstance(right, exp.Literal)
                and right.is_string
            ):
                values.add(right.this)
        return next(iter(values)) if len(values) == 1 else None
    except Exception:
        return None


def _mapping_row_refs(row: dict[str, Any]) -> list[SqlRef]:
    """从一行已执行且通过 JOIN AST 核对的结果派生三张源表的逐行引用。"""
    equipment_id = str(row["equipment_id"])
    contract_id = str(row["contract_id"])
    equipment_fields = {
        "equipment_id": equipment_id,
        "equipment_name": row.get("equipment_name"),
        "customer_id": row.get("equipment_customer_id"),
    }
    mapping_fields = {
        "equipment_id": equipment_id,
        "contract_id": contract_id,
        "valid_from": row.get("mapping_valid_from"),
        "valid_to": row.get("mapping_valid_to"),
        "mapping_basis": row.get("mapping_basis"),
    }
    contract_fields = {
        "contract_id": contract_id,
        "customer_id": row.get("contract_customer_id"),
        "effective_from": row.get("contract_effective_from"),
        "effective_to": row.get("contract_effective_to"),
        "status": row.get("contract_status"),
    }
    return [
        SqlRef(
            kind="sql",
            table="equipment",
            column="/".join(equipment_fields),
            row_pk=equipment_id,
            snippet=(f"设备编号 {equipment_id}，设备名称 {row.get('equipment_name', '未返回')}，"
                     f"属于客户 {row['equipment_customer_id']}；"
                     "客户范围由设备主数据记录提供。"
                     + _machine_sql_field_suffix(equipment_fields)),
        ),
        SqlRef(
            kind="sql",
            table="contract_equipment",
            column="/".join(mapping_fields),
            row_pk=contract_id + "|" + equipment_id,
            snippet=(f"受控映射：设备 {equipment_id} → 合同 {contract_id}；映射有效期 "
                     f"{row['mapping_valid_from']} 至 {row['mapping_valid_to']}；"
                     f"依据 {row['mapping_basis']}。"
                     + _machine_sql_field_suffix(mapping_fields)),
        ),
        SqlRef(
            kind="sql",
            table="contracts",
            column="/".join(contract_fields),
            row_pk=contract_id,
            snippet=(f"合同 {contract_id} 属于客户 {row['contract_customer_id']}；合同有效期 "
                     f"{row['contract_effective_from']} 至 {row['contract_effective_to']}；"
                     f"状态 {row['contract_status']}。"
                     + _machine_sql_field_suffix(contract_fields)),
        ),
    ]


def _machine_sql_field_suffix(fields: dict[str, Any]) -> str:
    """Append stable field/value labels without replacing readable citation prose."""
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


def _dsn_with_search_path(
    dsn: str, *, db_schema: str = "cross_source_demo", extra_schema: str = "public",
    read_only: bool = False, extension_schema: str | None = None,
) -> str:
    """把新连接的默认 schema 限定到隔离演示 schema，不设置进程级 PGOPTIONS。"""
    from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

    extension_schema = extension_schema or "public"
    for schema in (db_schema, extra_schema, extension_schema):
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
            raise ValueError("事实计划数据库 schema 名称只能含小写字母、数字与下划线")
    parsed = urlsplit(dsn)
    if not parsed.scheme.startswith("postgres"):
        raise ValueError("隔离事实计划仅支持 PostgreSQL 连接串")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key == "options" for key, _ in query):
        raise ValueError("演示连接串已包含 options；拒绝覆盖现有 PostgreSQL 会话选项")
    raw_query = parsed.query
    search_path = ",".join(dict.fromkeys((db_schema, extra_schema, extension_schema, "public")))
    session_options = f"-c search_path={search_path}"
    if read_only:
        session_options += " -c default_transaction_read_only=on"
    options = "options=" + quote(session_options, safe="")
    query_text = raw_query + ("&" if raw_query else "") + options
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query_text, parsed.fragment))


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


__all__ = ["build_cross_source_fact_plan_executor"]
