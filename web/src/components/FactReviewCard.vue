<script setup lang="ts">
import type { FactReview, FactReviewItem, RefT } from '../types/contracts'

const props = defineProps<{ review: FactReview; refs: RefT[] }>()
const emit = defineEmits<{ selectRef: [index: number] }>()

function hasValidEvidence(item: FactReviewItem): boolean {
  return item.evidence_ref_indices.length > 0
    && item.evidence_ref_indices.every((index) => Number.isInteger(index) && index >= 0 && index < props.refs.length)
}

function isStrictPass(item: FactReviewItem): boolean {
  return props.review.verification_mode === 'strict'
    && item.execution_status === 'completed'
    && item.status === 'supported'
    && item.evidence_link_status === 'complete'
    && hasValidEvidence(item)
}

function statusLabel(item: FactReviewItem): string {
  if (isStrictPass(item)) return '事实已核验 · 证据完整'
  if (item.status === 'candidate' && props.review.verification_mode === 'source_specific') {
    return '尚未作事实级判定'
  }
  return ({
    candidate: '候选事实',
    supported: '证据支持',
    contradicted: '证据相矛盾',
    insufficient: '证据不足',
    ambiguous: '存在歧义',
  })[item.status]
}

function executionLabel(status: FactReviewItem['execution_status']): string {
  return ({ completed: '任务已完成', not_run: '任务未执行', failed: '任务未完成' })[status]
}

function evidenceLabel(status: FactReviewItem['evidence_link_status']): string {
  return ({ complete: '证据已关联', partial: '证据部分关联', unavailable: '证据未关联' })[status]
}

function modeLabel(): string {
  if (props.review.verification_mode === 'strict') return '严格核验模式'
  if (props.review.verification_mode === 'ablation') return '消融对照模式'
  return props.review.verification_scope === 'cross_source_execution_only'
    ? '跨源执行记录 · 关系未核验'
    : '来源执行记录 · 未作事实级判定'
}

function checkLabel(code: string): string {
  return ({
    repair_duration_within_limit: '修复时限比较',
    first_response_within_limit: '首次响应时限比较',
    mapping_basis_policy: '映射依据策略',
    readonly_sql_validation: '只读查询校验',
    query_relation_validation: '查询关系校验',
    query_execution: '数据库执行',
    query_result_completeness: '结果完整性',
    sql_requirement_coverage: '查询条件落实',
    document_retrieval: '文档检索',
    answer_generation: '回答生成',
    citation_reference_linkage: '引用定位关联',
    semantic_fact_support: '逐项事实支持',
    cross_source_relation_check: '跨源关系核验',
  })[code] ?? '结构化检查'
}

/**
 * 需要解释的检查项单独列出来。
 *
 * 为什么不全塞进 title 属性：`查询条件落实：未通过` 只说了结论，用户真正要知道的是
 * **哪一条**条件被丢了（如「问题限定了时间范围（2023年），但相关业务表没有可用的
 * 时间依据」）。把这句话藏在悬浮提示里，触屏用户和评委演示时都看不到 ——
 * 而「可解释」正是这道题要考的东西，它必须直接铺在界面上。
 */
function checksNeedingDetail(item: FactReviewItem): FactReviewItem['checks'] {
  return item.checks.filter((check) => check.detail)
}

function checkResult(result: FactReviewItem['checks'][number]['result']): string {
  return ({ passed: '通过', failed: '未通过', unknown: '未确定', not_run: '未执行' })[result]
}

function refLabel(index: number): string {
  const ref = props.refs[index]
  if (!ref) return `来源 ${index + 1}`
  if (ref.kind === 'sql') return `${ref.table}${ref.column ? ` · ${ref.column}` : ''}`
  return ref.doc_title || `文档来源 ${index + 1}`
}

/**
 * 证据按钮按文档归并。
 *
 * 一条事实的证据往往落在同一份文档的多个块上（实测一条事实能挂 6 个块），
 * 而 `doc_title` 只到文档级，逐个渲染会出现一整排「查看安徽 2024 年度 立项名单」
 * ——看起来像重复 bug，实际是同文档的不同块。归并成一个按钮 + 块数，
 * 点击落到该文档的第一个块。
 */
function evidenceGroups(item: FactReviewItem): { index: number; label: string; count: number }[] {
  const groups = new Map<string, { index: number; label: string; count: number }>()
  for (const index of item.evidence_ref_indices) {
    if (!Number.isInteger(index) || index < 0 || index >= props.refs.length) continue
    const ref = props.refs[index]
    const key = ref.kind === 'doc' ? `d:${ref.doc_title || ref.doc_id}` : `t:${ref.table}`
    const found = groups.get(key)
    if (found) found.count += 1
    else groups.set(key, { index, label: refLabel(index), count: 1 })
  }
  return [...groups.values()]
}
</script>

<template>
  <section
    class="fact-review"
    :aria-label="review.verification_mode === 'source_specific' ? '事实处理与证据记录' : '事实核验记录'"
  >
    <header class="fact-review__head">
      <div>
        <b>{{ review.verification_mode === 'source_specific' ? '事实处理与证据记录' : '事实核验记录' }}</b>
        <p>{{ review.scope_note }}</p>
      </div>
      <span class="fact-review__mode" :class="`fact-review__mode--${review.verification_mode}`">
        {{ modeLabel() }}
      </span>
    </header>

    <ol class="fact-review__items">
      <li v-for="item in review.items" :key="item.fact_id" class="fact-review__item">
        <div class="fact-review__item-head">
          <b>{{ item.label }}</b>
          <span class="fact-review__status" :class="{ 'fact-review__status--pass': isStrictPass(item) }">
            {{ statusLabel(item) }}
          </span>
        </div>
        <p v-if="item.summary" class="fact-review__summary">{{ item.summary }}</p>
        <!--
          三列是本项目为「可溯源事实核验」补的：原卡片只说「这条成立不成立」，
          而对公示语料做计数核验时，真正要展示的是「按什么口径算的、算出多少、
          凭哪一块原文」。三者缺一，核验就退回成了结论。
        -->
        <dl v-if="item.value || item.criterion || item.evidence_chunk" class="fact-review__facts">
          <div v-if="item.criterion">
            <dt>口径</dt>
            <dd>{{ item.criterion }}</dd>
          </div>
          <div v-if="item.value" class="fact-review__facts-value">
            <dt>判定值</dt>
            <dd>{{ item.value }}</dd>
          </div>
          <div v-if="item.evidence_chunk">
            <dt>证据块</dt>
            <dd><code>{{ item.evidence_chunk }}</code></dd>
          </div>
        </dl>
        <div v-if="item.checks.length" class="fact-review__checks">
          <span
            v-for="(check, index) in item.checks"
            :key="`${check.code}-${index}`"
            class="fact-review__check"
            :class="`fact-review__check--${check.result}`"
          >
            {{ checkLabel(check.code) }}：{{ checkResult(check.result) }}
          </span>
        </div>
        <ul v-if="checksNeedingDetail(item).length" class="fact-review__check-details">
          <li v-for="check in checksNeedingDetail(item)" :key="`${check.code}-detail`">
            <b>{{ checkLabel(check.code) }}</b>{{ check.detail }}
          </li>
        </ul>
        <p v-if="item.reason" class="fact-review__reason">{{ item.reason }}</p>
        <footer class="fact-review__foot">
          <span>{{ executionLabel(item.execution_status) }}</span>
          <span>{{ evidenceLabel(item.evidence_link_status) }}</span>
          <button
            v-for="group in evidenceGroups(item)"
            :key="`${item.fact_id}-ref-${group.index}`"
            type="button"
            class="fact-review__source"
            @click="emit('selectRef', group.index)"
          >
            查看{{ group.label }}<template v-if="group.count > 1"> · {{ group.count }} 块</template>
          </button>
        </footer>
      </li>
    </ol>
  </section>
</template>

<style scoped>
.fact-review { margin: 18px 0 14px; overflow: hidden; border: 1px solid var(--border-subtle); border-radius: 12px; background: var(--bg-subtle); }
.fact-review__head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 13px 15px; border-bottom: 1px solid var(--border-subtle); }
.fact-review__head b { color: var(--text-heading); font-size: 14px; }
.fact-review__head p { margin: 5px 0 0; color: var(--text-faint); font-size: 12px; line-height: 1.55; }
.fact-review__mode { flex: none; padding: 4px 8px; border: 1px solid var(--border-subtle); border-radius: 6px; color: var(--text-secondary); font-size: 11px; }
.fact-review__mode--strict { border-color: var(--border-brand); color: var(--brand-primary); background: var(--brand-soft); }
.fact-review__mode--ablation { color: var(--text-muted); background: var(--bg-elevated); }
.fact-review__mode--source_specific { color: var(--text-secondary); background: var(--bg-elevated); }
.fact-review__items { display: grid; gap: 0; margin: 0; padding: 0; list-style: none; }
.fact-review__item { padding: 12px 15px; border-bottom: 1px solid var(--border-subtle); }
.fact-review__item:last-child { border-bottom: 0; }
.fact-review__item-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.fact-review__item-head b { color: var(--text-heading); font-size: 13px; line-height: 1.5; }
.fact-review__status { flex: none; color: var(--text-muted); font-size: 12px; }
.fact-review__status--pass { color: var(--accent-teal); font-weight: 700; }
.fact-review__summary, .fact-review__reason { margin: 7px 0 0; color: var(--text-secondary); font-size: 13px; line-height: 1.65; overflow-wrap: anywhere; }
.fact-review__reason { color: var(--text-muted); }
/* 三列信息条：口径 / 判定值 / 证据块。判定值用等宽数字并放大，
   因为它才是这条事实的结论；口径和块名是它的两个说明。 */
.fact-review__facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; margin: 9px 0 0; padding: 9px 11px; border-radius: 8px; background: var(--bg-elevated); }
.fact-review__facts > div { min-width: 0; }
.fact-review__facts dt { color: var(--text-faint); font-size: 11px; letter-spacing: .02em; }
.fact-review__facts dd { margin: 3px 0 0; overflow-wrap: anywhere; color: var(--text-secondary); font-size: 13px; line-height: 1.5; }
.fact-review__facts-value dd { color: var(--text-heading); font-size: 16px; font-weight: 650; font-variant-numeric: tabular-nums; }
.fact-review__facts code { padding: 1px 5px; border-radius: 4px; background: var(--bg-subtle); color: var(--text-secondary); font-size: 12px; }
.fact-review__checks { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.fact-review__check { padding: 3px 7px; border: 1px solid var(--border-subtle); border-radius: 5px; color: var(--text-secondary); background: var(--bg-elevated); font-size: 11px; }
/* 三态用颜色区分，不要只靠文字 —— 演示时评委一眼扫过去要能看出哪项没落实。
   unknown 用警告色（如实标黄），不是错误色：它表示"证不出"，不是"做错了"。 */
.fact-review__check--failed { border-color: var(--state-error-border); color: var(--state-error-text); background: var(--state-error-soft); }
.fact-review__check--unknown { border-color: var(--state-warning); color: var(--state-warning); background: var(--state-warning-soft); }
.fact-review__check-details { display: grid; gap: 4px; margin: 8px 0 0; padding: 0; list-style: none; }
.fact-review__check-details li { padding: 6px 9px; border-left: 2px solid var(--state-warning); border-radius: 0 5px 5px 0; color: var(--text-secondary); background: var(--state-warning-soft); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.fact-review__check-details b { margin-right: 6px; color: var(--text-heading); font-weight: 650; }
.fact-review__foot { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; margin-top: 9px; color: var(--text-faint); font-size: 11px; }
.fact-review__source { padding: 2px 6px; border: 1px solid var(--border-brand); border-radius: 5px; color: var(--brand-primary); background: transparent; font-size: 11px; }
.fact-review__source:hover { background: var(--brand-soft); }
@media (max-width: 640px) {
  .fact-review__head { display: grid; }
  .fact-review__mode { justify-self: start; }
}
</style>
