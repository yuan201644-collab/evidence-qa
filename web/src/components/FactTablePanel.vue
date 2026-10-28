<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import AppIcon from './AppIcon.vue'
import { getFactTable, getFrozenQuestions, getRemoteKagStatus } from '../api/kag'
import type { FactTableRow, FrozenQuestion, KagGraphStats } from '../api/kag'

const emit = defineEmits<{ close: [] }>()

const questions = ref<FrozenQuestion[]>([])
const graph = ref<KagGraphStats | null>(null)
const activeQid = ref('')
const rows = ref<FactTableRow[]>([])
const tableText = ref('')
const loading = ref(false)
const error = ref('')

/**
 * 按题层分组。
 *
 * M = 计数/集合类，这套方法的主场；B = 条款对照；D = 字段语义。
 * 分组只是为了让人知道「哪些题本来就该出表、哪些本来就不该」——
 * 这比把 31 道平铺成列表更诚实：不是所有题都能拆成事实。
 */
const LAYER_LABEL: Record<string, string> = { M: '计数与集合', B: '条款对照', D: '字段语义' }
const grouped = computed(() => {
  const out: Record<string, FrozenQuestion[]> = {}
  for (const q of questions.value) {
    const key = q.layer || '其他'
    ;(out[key] ??= []).push(q)
  }
  return out
})
const activeQuestion = computed(
  () => questions.value.find((q) => q.qid === activeQid.value)?.text ?? '',
)
/**
 * 题干是否依赖上一题的上下文。
 *
 * 冻结题库是成套出的：`该名单中，中国海洋大学有几项` 前一句才刚说过是哪份名单。
 * 在这面板里它们是**独立点开**的，没有上文，所以定位不到文档、不出表。
 * 不标出来，会让人以为「这套方法连这题都算不出来」；标出来才是真实情形——
 * 缺的是线索，不是能力。这类题在完整对话里正常。
 */
const NEEDS_CONTEXT_RE = /^(上述|两份名单|三份名单)|该名单|该办法/
function needsContext(text: string): boolean {
  return NEEDS_CONTEXT_RE.test(text || '')
}
const contextCount = computed(() => questions.value.filter((q) => needsContext(q.text)).length)

async function loadQuestion(text: string, qid: string) {
  activeQid.value = qid
  loading.value = true
  error.value = ''
  try {
    const result = await getFactTable(text)
    rows.value = result.rows
    tableText.value = result.table
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '事实表生成失败'
    rows.value = []
    tableText.value = ''
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  try {
    const [list, remote] = await Promise.all([getFrozenQuestions(), getRemoteKagStatus()])
    questions.value = list
    graph.value = remote.graph
    // 默认选第一道 M 层题：这是这套方法的主场，也最容易看懂。
    const first = list.find((q) => q.layer === 'M') ?? list[0]
    if (first) await loadQuestion(first.text, first.qid)
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : '题库读取失败'
  }
})
</script>

<template>
  <div class="ft-backdrop" @click.self="emit('close')">
    <section
      class="ft-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="ft-title"
      @keydown.esc.prevent.stop="emit('close')"
    >
      <header class="ft-head">
        <span class="ft-head__mark"><img src="/brand/icons/evidence-ledger.svg" alt="" aria-hidden="true" /></span>
        <div class="ft-head__title">
          <h2 id="ft-title">事实核验面板</h2>
          <p>逐题看「拆成什么事实、判出什么值、落在哪个原文块」——离线重算，不调用模型</p>
        </div>
        <button class="icon-button vm-press" type="button" aria-label="关闭事实核验面板" @click="emit('close')">
          <AppIcon name="close" :size="18" />
        </button>
      </header>

      <div class="ft-body">
        <aside class="ft-list" aria-label="题库">
          <p class="ft-list__hint">冻结题库 {{ questions.length }} 道<template v-if="contextCount"> · 其中 {{ contextCount }} 道标了「需上文」</template></p>
          <div v-for="(items, layer) in grouped" :key="layer" class="ft-list__group">
            <p class="ft-list__label">{{ LAYER_LABEL[layer] || layer }}<small>{{ items.length }}</small></p>
            <button
              v-for="q in items"
              :key="q.qid"
              type="button"
              class="ft-list__item vm-interact"
              :class="{ 'is-active': q.qid === activeQid }"
              :title="q.text"
              @click="loadQuestion(q.text, q.qid)"
            >
              <b>{{ q.qid }}</b>
              <i v-if="needsContext(q.text)" class="ft-list__ctx" title="题干依赖上一题上下文，单独重算时缺少文档线索">需上文</i>
              <span>{{ q.text }}</span>
            </button>
          </div>
        </aside>

        <main class="ft-main" :aria-busy="loading">
          <p class="ft-question">{{ activeQuestion || '选择左侧任一题' }}</p>
          <p v-if="error" class="ft-error" role="alert">{{ error }}</p>

          <div v-if="loading" class="ft-placeholder">正在重算事实表…</div>

          <template v-else-if="rows.length">
            <table class="ft-table">
              <thead>
                <tr><th>#</th><th>口径</th><th>事实</th><th>判定值</th><th>状态</th></tr>
              </thead>
              <tbody>
                <tr v-for="row in rows" :key="row.fact_id">
                  <td class="ft-table__id">{{ row.fact_id }}</td>
                  <td class="ft-table__criterion">{{ row.criterion || '—' }}</td>
                  <td>{{ row.fact }}</td>
                  <td class="ft-table__value">{{ row.value }}</td>
                  <td>
                    <span class="ft-tag" :class="row.status === 'supported' ? 'ft-tag--ok' : 'ft-tag--warn'">
                      {{ row.status === 'supported' ? '已判定' : '无据' }}
                    </span>
                  </td>
                </tr>
              </tbody>
            </table>
            <p class="ft-note">
              这一页只做「拆」与「判」，证据列留空——<b>没有检索就不该声称有证据</b>。
              证据块要等真正提一次问，由那次召回的块填上。
            </p>
          </template>

          <div v-else class="ft-placeholder">
            <template v-if="needsContext(activeQuestion)">这道题的题干依赖上一题的上下文（「上述」「该名单」这类指代），在本面板里被单独点开，缺少属于哪份文档的线索，因此不定位、不出表。放到完整对话里接着上一题问，它才会生成事实表。</template>
            <template v-else>这道题拆不成「某文档 · 某单位 · 某类别」形式的子问（例如条款题：条款正文不在结构化记录里），因此不出表，会落回检索链路作答。</template>
          </div>

          <dl v-if="graph" class="ft-graph">
            <div>
              <dt>证据边</dt>
              <dd>{{ graph.source_edges.toLocaleString() }}<small>条 source 边</small></dd>
            </div>
            <div>
              <dt>业务边</dt>
              <dd>{{ graph.business_edges.toLocaleString() }}<small>条</small></dd>
            </div>
            <div>
              <dt>关系类型</dt>
              <dd>{{ graph.relation_types }}<small>种</small></dd>
            </div>
            <p>{{ graph.note }}（{{ graph.database }}，实测于 {{ graph.measured_at }}）</p>
          </dl>
        </main>
      </div>
    </section>
  </div>
</template>

<style scoped>
.ft-backdrop { position: fixed; inset: 0; z-index: 230; display: grid; place-items: center; padding: 22px; background: var(--scrim); backdrop-filter: blur(3px); }
.ft-dialog { display: flex; width: min(1080px, 100%); max-height: min(86vh, 780px); flex-direction: column; overflow: hidden; border: 1px solid var(--border-subtle); border-radius: 14px; background: var(--bg-elevated); box-shadow: var(--shadow-dialog, 0 24px 60px rgba(16, 24, 40, .24)); }
.ft-head { display: flex; align-items: flex-start; gap: 12px; padding: 16px 18px; border-bottom: 1px solid var(--border-subtle); }
.ft-head__mark { display: grid; width: 34px; height: 34px; flex: none; place-items: center; border-radius: 9px; background: var(--brand-soft); }
.ft-head__mark img { width: 20px; height: 20px; }
.ft-head__title { flex: 1; min-width: 0; }
.ft-head__title h2 { margin: 0; color: var(--text-heading); font-size: 16px; }
.ft-head__title p { margin: 4px 0 0; color: var(--text-faint); font-size: 12px; line-height: 1.55; }
.ft-body { display: grid; min-height: 0; flex: 1; grid-template-columns: 268px minmax(0, 1fr); }
.ft-list { overflow-y: auto; padding: 12px; border-right: 1px solid var(--border-subtle); background: var(--bg-subtle); }
.ft-list__hint { margin: 0 0 10px; color: var(--text-faint); font-size: 12px; }
.ft-list__group { margin-bottom: 12px; }
.ft-list__label { display: flex; align-items: center; gap: 6px; margin: 0 0 6px; color: var(--text-muted); font-size: 12px; }
.ft-list__label small { padding: 0 5px; border-radius: 4px; background: var(--bg-elevated); font-size: 11px; }
.ft-list__item { display: flex; width: 100%; flex-wrap: wrap; gap: 4px 7px; margin-bottom: 4px; padding: 7px 8px; border: 1px solid transparent; border-radius: 7px; color: var(--text-secondary); text-align: left; background: transparent; font-size: 12px; line-height: 1.45; }
.ft-list__item:hover { background: var(--bg-elevated); }
.ft-list__item.is-active { border-color: var(--border-brand); background: var(--brand-soft); color: var(--text-heading); }
.ft-list__item b { flex: none; color: var(--brand-primary); font-variant-numeric: tabular-nums; }
.ft-list__item span { display: -webkit-box; flex: 1 1 100%; order: 3; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; }
.ft-list__ctx { flex: none; order: 2; padding: 1px 5px; border-radius: 4px; color: var(--state-warning); background: var(--state-warning-soft); font-size: 10px; font-style: normal; white-space: nowrap; }
.ft-main { overflow-y: auto; padding: 18px; }
.ft-question { margin: 0 0 12px; color: var(--text-heading); font-size: 14px; line-height: 1.6; }
.ft-error { margin: 0 0 12px; padding: 8px 10px; border-radius: 7px; color: var(--state-error-text); background: var(--state-error-soft); font-size: 12px; }
.ft-placeholder { padding: 26px 14px; border: 1px dashed var(--border-subtle); border-radius: 10px; color: var(--text-muted); text-align: center; font-size: 13px; line-height: 1.7; }
.ft-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.ft-table th { padding: 7px 9px; border-bottom: 1px solid var(--border-subtle); color: var(--text-faint); text-align: left; font-weight: 500; font-size: 12px; }
.ft-table td { padding: 9px; border-bottom: 1px solid var(--border-subtle); color: var(--text-secondary); vertical-align: top; line-height: 1.55; }
.ft-table__id { color: var(--text-faint); font-variant-numeric: tabular-nums; }
.ft-table__criterion { color: var(--text-muted); font-size: 12px; }
.ft-table__value { color: var(--text-heading); font-size: 15px; font-weight: 650; white-space: nowrap; font-variant-numeric: tabular-nums; }
.ft-tag { padding: 2px 7px; border-radius: 5px; font-size: 11px; white-space: nowrap; }
.ft-tag--ok { color: var(--accent-teal); background: var(--brand-soft); }
.ft-tag--warn { color: var(--state-warning); background: var(--state-warning-soft); }
.ft-note { margin: 12px 0 0; padding: 9px 11px; border-left: 2px solid var(--border-brand); border-radius: 0 6px 6px 0; color: var(--text-muted); background: var(--bg-subtle); font-size: 12px; line-height: 1.65; }
.ft-graph { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; margin: 18px 0 0; padding: 12px; border: 1px solid var(--border-subtle); border-radius: 10px; background: var(--bg-subtle); }
.ft-graph > div { min-width: 0; }
.ft-graph dt { color: var(--text-faint); font-size: 11px; }
.ft-graph dd { margin: 3px 0 0; color: var(--text-heading); font-size: 18px; font-weight: 650; font-variant-numeric: tabular-nums; }
.ft-graph dd small { margin-left: 3px; color: var(--text-faint); font-size: 11px; font-weight: 400; }
.ft-graph p { grid-column: 1 / -1; margin: 6px 0 0; color: var(--text-faint); font-size: 11px; line-height: 1.6; }
@media (max-width: 820px) {
  .ft-backdrop { padding: 10px; }
  .ft-body { grid-template-columns: 1fr; }
  .ft-list { max-height: 168px; border-right: 0; border-bottom: 1px solid var(--border-subtle); }
}
</style>
