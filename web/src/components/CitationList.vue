<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'
import type { RefT } from '../types/contracts'
import { refTitle } from '../stores/persistence'

const props = defineProps<{ refs: RefT[]; activeIndex: number }>()
const emit = defineEmits<{ select: [index: number] }>()
const expanded = ref(false)
const visibleRefs = computed(() => props.refs.slice(0, expanded.value ? props.refs.length : 4))
const methodLabels = { ocr: '扫描识别', text_layer: '文本原文', plain: '文本片段' } as const
watch(() => props.activeIndex, (index) => { if (index >= 4) expanded.value = true })
</script>

<template>
  <section v-if="refs.length" class="citations" aria-label="回答来源">
    <header><span><AppIcon name="link" :size="13" />回答依据 <small>{{ refs.length }}</small></span><span class="citation-tip">点击查看对应原文或数据记录</span></header>
    <div class="citation-grid">
      <button v-for="(ref, index) in visibleRefs" :key="ref.kind + '-' + index" class="citation vm-press vm-interact" :class="{ active: index === activeIndex }" :title="ref.snippet" @click="emit('select', index)">
        <span class="citation__number">{{ index + 1 }}</span><div class="citation__body"><b>{{ refTitle(ref) }}</b><span class="citation__meta"><template v-if="ref.kind === 'doc'"><AppIcon name="document" :size="11" />{{ ref.page ? '第 ' + ref.page + ' 页' : '文档片段' }}<span v-if="ref.section_path"> · {{ ref.section_path }}</span><span v-else-if="ref.extraction_method"> · {{ methodLabels[ref.extraction_method] }}</span></template><template v-else><AppIcon name="database" :size="11" />数据记录<span v-if="ref.row_pk"> · {{ ref.row_pk }}</span></template></span><p>{{ ref.snippet }}</p></div><AppIcon class="citation__arrow" name="chevron" :size="12" />
      </button>
    </div>
    <button v-if="refs.length > 4" class="citation-more" @click="expanded = !expanded">{{ expanded ? '收起来源' : '展开其余 ' + (refs.length - 4) + ' 条来源' }}</button>
  </section>
</template>

<style scoped>
.citations { margin-top: 22px; }
.citations header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 9px; color: var(--text-muted); font-size: 13px; }
.citations header > span:first-child { display: flex; align-items: center; gap: 5px; }
.citations header small { padding: 0 5px; border-radius: 4px; background: var(--bg-subtle); font-size: 12px; }
.citation-tip { color: var(--text-muted); font-size: 12px; }
.citation-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.citation-grid:has(.citation:only-child) { grid-template-columns: 1fr; }
.citation { display: flex; align-items: flex-start; gap: 8px; width: 100%; min-width: 0; padding: 11px; border: 1px solid var(--border-subtle); border-radius: 9px; color: var(--text-secondary); text-align: left; background: var(--bg-subtle); transition: background .15s, border-color .15s; }
.citation:hover, .citation.active { border-color: var(--border-brand); background: var(--brand-soft); }
.citation__number { display: grid; width: 18px; height: 18px; flex-shrink: 0; place-items: center; margin-top: 1px; border-radius: 4px; color: var(--brand-primary); background: var(--brand-soft-strong); font-size: 12px; font-weight: 600; }
.citation__body { min-width: 0; flex: 1; }
.citation__body b { display: block; overflow: hidden; color: var(--text-secondary); font-size: 14px; font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.citation__meta { display: flex; align-items: center; overflow: hidden; gap: 3px; margin-top: 4px; color: var(--text-muted); font-size: 12px; white-space: nowrap; text-overflow: ellipsis; }
.citation__meta > span { overflow: hidden; text-overflow: ellipsis; }
.citation__body p { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; margin: 6px 0 0; color: var(--text-muted); font-size: 13px; line-height: 1.7; }
.citation__arrow { margin-top: 4px; color: var(--text-faint); transition: transform var(--motion-fast) var(--motion-ease); }
.citation:hover .citation__arrow, .citation:focus-visible .citation__arrow { transform: translateX(2px); }
.citation-more { margin-top: 9px; padding: 0; border: 0; color: var(--brand-primary); background: transparent; font-size: 13px; }
@media (max-width: 900px) { .citation-grid { grid-template-columns: 1fr; } }
</style>
