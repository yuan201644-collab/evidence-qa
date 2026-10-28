import type { TraceStep } from '../types/contracts'

const labels: Record<string, string> = {
  normalizer: '要素识别与改写', semantic_clarifier: '问题口径检查',
  schema_linker: '表字段定位', join_planner: '关联路径规划',
  sql_semantic_validator: '关联与粒度检查', nl2sql: '数据库查询',
  doc_search: '文档检索', hybrid_retrieve: '混合检索', rerank: '证据排序',
  cross_rerank: '跨文档证据排序', parse_provenance: '文档来源定位',
  llm_generate: '组织回答', graph_reason: '关系检索', decompose: '问题分解',
  sufficiency_check: '证据充分性检查', context_merge: '汇集上下文',
  kag_supplement: 'KAG 补充检索', joint_rerank: '联合证据排序',
  // 直连 KAG 链路的三段 + 本项目插进去的两段。不加这几条，
  // 处理详情里会显示成两个「工具调用」——最难看的兜底文案。
  kag_plan: 'KAG 解析与规划', kag_retrieve: 'KAG 三路检索',
  fact_table: '生成待确认事实表', fact_evidence: '为事实挂原文块',
  cross_source_demo: '跨源问答', cross_source_sql: '查询对象与事件',
  cross_source_doc_retrieval: '检索适用条款', cross_source_alignment: '跨源口径核对',
  contract_bind: '合同关联', contract_item_route: '合同问题识别',
  contract_chain_incomplete: '合同证据缺口检查', provenance_complete: '补全来源记录',
  entity_bind: '对象关联', relation_bind: '关系关联', clarification_needed: '请求补充信息',
  policy_relation_route: '制度关系识别', service_relation_route: '服务关系识别',
  equipment_relation_route: '设备关系识别', plan_fallback: '调整检索计划',
}

export function stepLabel(tool: string): string { return labels[tool] ?? '工具调用' }

/** 路径概览来自已返回的调用记录，不在等待期间推测执行阶段。 */
export function recordedStages(trace: TraceStep[]): string[] {
  const stages = trace.map((step) => {
    if (/normaliz|clarif|decompose|planner|route/.test(step.tool)) return '问题处理'
    if (/nl2sql|schema_link|sql/.test(step.tool)) return '数据查询'
    if (/retrieve|retrieval|rerank|doc_search|parse_provenance/.test(step.tool)) return '文档检索'
    if (/align|bind|sufficien|graph|semantic_validator/.test(step.tool)) return '关系与口径检查'
    if (/fact_/.test(step.tool)) return '事实核验'
    if (/generate|merge|provenance_complete/.test(step.tool)) return '组织回答'
    return '工具执行'
  })
  return stages.filter((stage, index) => index === 0 || stage !== stages[index - 1])
}
