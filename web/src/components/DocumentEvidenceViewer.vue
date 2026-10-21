<script setup lang="ts">
import { defineAsyncComponent, ref, watch } from 'vue'
import type { DocRef } from '../types/contracts'
import { getDocumentFileUrl, getDocumentPreviewUrl } from '../api/documents'

const PdfEvidenceViewer = defineAsyncComponent(() => import('./PdfEvidenceViewer.vue'))
const props = defineProps<{ refItem: DocRef }>()

type Preview = { kind: 'pdf'; file_name: string } | { kind: 'text'; file_name: string; content: string }
const preview = ref<Preview>()
const loading = ref(false)
const error = ref('')
let requestId = 0

watch(() => props.refItem.doc_id, async (docId) => {
  const currentRequest = ++requestId
  preview.value = undefined
  error.value = ''
  loading.value = true
  try {
    const response = await fetch(getDocumentPreviewUrl(docId), { cache: 'no-store' })
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    const result = await response.json() as Preview
    if (currentRequest === requestId) preview.value = result
  } catch (cause) {
    if (currentRequest === requestId) {
      error.value = cause instanceof Error ? cause.message : '无法读取文档原文'
    }
  } finally {
    if (currentRequest === requestId) loading.value = false
  }
}, { immediate: true })
</script>

<template>
  <PdfEvidenceViewer v-if="preview?.kind === 'pdf'" :ref-item="refItem" />
  <section v-else class="document-source">
    <header class="document-source__head">
      <div>
        <p>文档原文<span v-if="refItem.extraction_method"> · {{ refItem.extraction_method === 'plain' ? '纯文本' : '文本抽取' }}</span></p>
        <strong>{{ preview?.file_name || refItem.doc_title || refItem.doc_id }}</strong>
      </div>
      <a :href="getDocumentFileUrl(refItem.doc_id, preview?.kind === 'text' ? preview.file_name : undefined)" target="_blank" rel="noreferrer">打开原文件</a>
    </header>
    <p v-if="loading" class="document-source__state" role="status">正在读取文档原文…</p>
    <p v-else-if="error" class="document-source__state document-source__state--error" role="alert">原文读取失败：{{ error }}</p>
    <pre v-else class="document-source__text">{{ preview?.kind === 'text' ? preview.content : '' }}</pre>
  </section>
</template>

<style scoped>
.document-source { display: flex; flex: 1; min-height: 0; flex-direction: column; }
.document-source__head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 17px 18px 12px; border-bottom: 1px solid var(--border-subtle); }
.document-source__head > div { min-width: 0; }
.document-source__head p { margin: 0 0 6px; color: var(--text-muted); font-size: 12px; }
.document-source__head strong { display: block; overflow-wrap: anywhere; color: var(--text-heading); font-size: 14px; font-weight: 500; line-height: 1.7; }
.document-source__head a { flex-shrink: 0; color: var(--brand-primary); font-size: 13px; text-decoration: none; }
.document-source__head a:hover { text-decoration: underline; }
.document-source__state { padding: 20px; color: var(--text-muted); font-size: 14px; }
.document-source__state--error { color: var(--state-error); }
.document-source__text { min-height: 0; overflow: auto; margin: 0; padding: 18px; color: var(--text-secondary); background: var(--bg-surface); font: inherit; font-size: 14px; line-height: 1.9; white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
