<script setup lang="ts">
import { computed, defineAsyncComponent } from 'vue'
import AppIcon from './AppIcon.vue'
import type { DataSource, RefT } from '../types/contracts'
import SqlEvidencePanel from './SqlEvidencePanel.vue'

const DocumentEvidenceViewer = defineAsyncComponent(() => import('./DocumentEvidenceViewer.vue'))
const props = defineProps<{ refItem?: RefT; sql: string | null; dataSource?: DataSource | null }>()
const doc = computed(() => props.refItem?.kind === 'doc' ? props.refItem : undefined)
const sqlRef = computed(() => props.refItem?.kind === 'sql' ? props.refItem : undefined)
</script>

<template>
  <aside class="evidence-panel">
    <div v-if="!refItem" class="evidence-empty"><span><AppIcon name="link" :size="27" /></span><h2>把答案与出处放在一起</h2><p>点击答案中的引用编号或事实核验卡里的证据块，<br />在这里核对模型当时读到的原文。</p></div>
    <template v-else-if="doc && doc.page"><details class="quoted-snippet"><summary>引用片段 <small>{{ doc.section_path || (doc.extraction_method === 'plain' ? '文档片段' : '第 ' + doc.page + ' 页') }}</small></summary><blockquote>{{ doc.snippet }}</blockquote></details><DocumentEvidenceViewer :key="doc.doc_id" :ref-item="doc" /></template>
    <section v-else-if="doc" class="doc-fallback"><span class="source-type"><AppIcon name="document" :size="15" />原文块证据</span><h3>{{ doc.doc_title || doc.doc_id }}</h3><p class="source-id">{{ doc.doc_id }}<span v-if="doc.section_path"> · {{ doc.section_path }}</span></p><div class="fragment-label">原文引用片段</div><blockquote>{{ doc.snippet }}</blockquote><p class="fragment-note">这是模型生成答案时实际读到的原文块（按相关性排序），不是事后检索的近似结果。</p></section>
    <SqlEvidencePanel v-else-if="sqlRef" :ref-item="sqlRef" :sql="sql" :data-source="dataSource" />
  </aside>
</template>

<style scoped>
.evidence-panel { display: flex; flex: 1; min-height: 0; flex-direction: column; background: var(--bg-surface); }
.evidence-empty { margin: auto; padding: 35px 25px; text-align: center; }
.evidence-empty > span { display: grid; width: 62px; height: 62px; place-items: center; margin: 0 auto 20px; border: 1px solid var(--border-subtle); border-radius: 18px; color: var(--brand-primary-weak); background: var(--bg-subtle); }
.evidence-empty h2 { color: var(--text-secondary); font-size: 17px; font-weight: 500; }
.evidence-empty p { color: var(--text-muted); font-size: 14px; line-height: 1.9; }
.quoted-snippet { flex-shrink: 0; max-height: 180px; overflow: auto; padding: 11px 18px; border-bottom: 1px solid var(--border-subtle); background: var(--bg-subtle); }
.quoted-snippet summary { color: var(--text-secondary); cursor: pointer; font-size: 13px; }
.quoted-snippet small { margin-left: 8px; color: var(--text-muted); font-size: 12px; }
.quoted-snippet blockquote { margin: 10px 0 0; padding-left: 10px; border-left: 2px solid var(--border-brand); color: var(--text-secondary); font-size: 14px; line-height: 1.85; white-space: pre-wrap; }
.doc-fallback { min-height: 0; overflow: auto; padding: 25px; }
.source-type { display: flex; align-items: center; gap: 7px; color: var(--brand-primary); font-size: 13px; }
.doc-fallback h3 { margin: 12px 0 7px; color: var(--text-heading); font-size: 20px; line-height: 1.6; }
.source-id { margin: 0 0 26px; color: var(--text-muted); font-size: 13px; overflow-wrap: anywhere; }
.fragment-label { margin-bottom: 9px; color: var(--text-muted); font-size: 12px; }
.doc-fallback blockquote { margin: 0; padding: 15px; border: 1px solid var(--state-warning-border); border-radius: 8px; color: var(--state-warning); background: var(--state-warning-soft); font-size: 15px; line-height: 1.95; white-space: pre-wrap; overflow-wrap: anywhere; }
.fragment-note { margin-top: 15px; color: var(--text-faint); font-size: 12px; }
</style>
