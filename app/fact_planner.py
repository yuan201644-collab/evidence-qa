"""小范围的显式事实计划样例；只用于隔离的工单—合同演示路径。"""

from __future__ import annotations

import re

from app.evidence_ledger import FactPlan, FactTask

WORK_ORDER_ID_PATTERN = r"WO-(?:\d{4}|[A-Z]{2,8})-\d{3,6}"
#: 严格校验用（配合 `fullmatch`，末尾带 `\Z`）。保留 `WORK_ORDER_ID_RE` 这个名字是因为
#: `app/cross_source_fact_plan.py` 直接 `from app.fact_planner import WORK_ORDER_ID_RE`。
WORK_ORDER_ID_RE = _WORK_ORDER_ID = re.compile(rf"{WORK_ORDER_ID_PATTERN}\Z", re.IGNORECASE)
#: 演示链路（工单—合同）用：从整句自由文本里 SEARCH 演示工单编号，保留捕获组供
#: `orchestrator.route` 取 `group(1)`。与上面的 `_WORK_ORDER_ID` 不同——后者末尾带
#: `\Z` 且配合 `fullmatch` 做严格校验，用在整句问题上永远匹配不到，故不能复用。
#:
#: 形态比只认 `WO-\d{4}-\d{4}` 的窄版更宽（另接受 `WO-ABCD-12345`、位数不同的编号），
#: 是窄版的严格超集：标准 4-4 形态两边都命中。
WORK_ORDER_ID_SEARCH_RE = re.compile(
    rf"(?<![A-Z0-9])({WORK_ORDER_ID_PATTERN})(?![A-Z0-9-])", re.IGNORECASE
)
_NEGATED_REQUEST = re.compile(
    r"(?:不需要|无需|无须|不用|不必|不要|不再|不核验|不检查|不判断|不讨论|不统计|不关注|不考虑)"
    r"\s*(?:对|核验|检查|判断|查询|计算|统计|讨论|分析|关注|提及)?\s*$"
)


def requests_first_response(question: str) -> bool:
    """只在用户明确提到首次响应时增加该指标的事实分支。"""
    return _requests_metric(question, "首次响应")


def requests_repair_completion(question: str) -> bool:
    return _requests_metric(question, "修复") or _requests_metric(question, "服务恢复")


def _requests_metric(question: str, metric: str) -> bool:
    for match in re.finditer(re.escape(metric), question):
        prefix = question[max(0, match.start() - 12):match.start()]
        suffix = question[match.end():match.end() + 12]
        if _NEGATED_REQUEST.search(prefix):
            continue
        if re.match(r"\s*(?:无需|不需要|不用|不必|不核验|不检查|不判断|不讨论|不统计|不关注|不考虑)", suffix):
            continue
        return True
    return False


def build_work_order_sla_plan(
    work_order_id: str, *, include_first_response: bool = False
) -> FactPlan:
    """为工单 SLA 问题构造依赖图；本函数不执行工具或生成结论。"""
    normalized_id = work_order_id.strip().upper()
    if not WORK_ORDER_ID_RE.fullmatch(normalized_id):
        raise ValueError("工单编号格式应为 WO-区域或年份-序号")

    plan = FactPlan()
    plan.add_task(FactTask(
        task_id="work_order_facts",
        target_fact=f"工单 {normalized_id} 的设备与必要事件",
        source_candidates=("nl2sql",),
    ))
    plan.add_task(FactTask(
        task_id="controlled_contract_mapping",
        target_fact="设备对应的唯一、受控且在工单日期有效的合同映射",
        source_candidates=("nl2sql",),
        depends_on=("work_order_facts",),
    ))
    plan.add_task(FactTask(
        task_id="contract_repair_limit",
        target_fact="适用合同文档中的修复完成时限",
        source_candidates=("doc_search",),
        # Do not retrieve or interpret an SLA until both the applicable
        # contract and the event interval being judged are supported.
        depends_on=("controlled_contract_mapping", "repair_duration"),
    ))
    plan.add_task(FactTask(
        task_id="repair_duration",
        target_fact="必要事件之间、口径一致且非负的修复耗时",
        source_candidates=("nl2sql",),
        # A duration is only relevant to this cross-source SLA path after the
        # equipment-to-contract relationship has been verified.
        depends_on=("work_order_facts", "controlled_contract_mapping"),
    ))
    if include_first_response:
        plan.add_task(FactTask(
            task_id="first_response_duration",
            target_fact="受理至首次响应的非负耗时",
            source_candidates=("nl2sql",),
            depends_on=("repair_duration",),
        ))
        plan.add_task(FactTask(
            task_id="first_response_verdict",
            target_fact="首次响应耗时是否满足合同首次响应时限",
            source_candidates=("cross_source_alignment",),
            depends_on=("contract_repair_limit", "first_response_duration"),
        ))
    plan.add_task(FactTask(
        task_id="sla_verdict",
        target_fact="判断修复指标；首次响应请求作为可独立支持或限定的并列事实",
        source_candidates=("cross_source_alignment",),
        depends_on=("contract_repair_limit", "repair_duration"),
    ))
    plan.validate()
    return plan

