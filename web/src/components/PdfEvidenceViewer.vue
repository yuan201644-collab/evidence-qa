<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import type { PDFDocumentLoadingTask, RenderTask } from 'pdfjs-dist'
import type { DocRef } from '../types/contracts'
import { getDocumentFileUrl } from '../api/documents'
import { getCachedPage, getDocument, pdfLoadOptions, setCachedPage, type CachedPage } from '../utils/pdf'
import PdfHighlightLayer from './PdfHighlightLayer.vue'

const props = defineProps<{ refItem: DocRef }>()
const canvas = ref<HTMLCanvasElement>()
const scrollElement = ref<HTMLElement>()
const availableWidth = ref(0)
const fitMode = ref(true)
const zoom = ref(1)
const pageNumber = ref(props.refItem.page || 1)
const totalPages = ref<number>()
const rendered = ref<CachedPage>()
const error = ref('')
const loading = ref(false)
const documents = new Map<string, PDFDocumentLoadingTask>()
const maxCachedDocuments = 3
let sequence = 0
let activeRenderTask: RenderTask | undefined
let resizeObserver: ResizeObserver | undefined
const extractionMethodLabel = computed(() => ({
  ocr: '扫描件 · OCR',
  text_layer: '文本层',
  plain: '纯文本',
} as const)[props.refItem.extraction_method ?? 'text_layer'])

const hasBboxes = computed(() => props.refItem.bboxes.length > 0)
const cacheKey = computed(() => `${props.refItem.doc_id}:${pageNumber.value}:${fitMode.value ? 'fit-' + availableWidth.value : zoom.value}`)

watch(scrollElement, (element) => {
  resizeObserver?.disconnect()
  if (!element) return
  availableWidth.value = element.clientWidth
  resizeObserver = new ResizeObserver(() => { availableWidth.value = element.clientWidth })
  resizeObserver.observe(element)
})

function setZoom(value: number) { fitMode.value = false; zoom.value = value }

function paint(item: CachedPage) {
  const target = canvas.value
  if (!target) return
  target.width = item.canvas.width
  target.height = item.canvas.height
  target.style.width = `${item.viewport.width}px`
  target.style.height = `${item.viewport.height}px`
  target.getContext('2d')?.drawImage(item.canvas, 0, 0)
}

// `rendered` 变化会让 v-if 创建新的 canvas。除 renderPage 中的 nextTick 外，
// 再由画布挂载事件补画一次，覆盖引用快速切换和热更新时的 DOM 时序。
watch([canvas, rendered], ([target, item]) => {
  if (!target || !item) return
  paint(item)
})

function getDocumentTask(docId: string) {
  let task = documents.get(docId)
  if (task) {
    documents.delete(docId)
    documents.set(docId, task)
    return task
  }
  task = getDocument({ url: getDocumentFileUrl(docId), ...pdfLoadOptions })
  documents.set(docId, task)
  if (documents.size > maxCachedDocuments) {
    const oldest = documents.entries().next().value as [string, PDFDocumentLoadingTask] | undefined
    if (oldest) {
      documents.delete(oldest[0])
      void oldest[1].destroy().catch(() => undefined)
    }
  }
  return task
}

async function renderPage() {
  const current = ++sequence
  activeRenderTask?.cancel()
  activeRenderTask = undefined
  error.value = ''
  loading.value = true
  const cached = getCachedPage(cacheKey.value)
  if (cached) {
    totalPages.value = cached.pageCount
    rendered.value = cached
    await nextTick()
    if (current !== sequence) return
    paint(cached)
    loading.value = false
    return
  }
  let renderTask: RenderTask | undefined
  try {
    const loadingTask = getDocumentTask(props.refItem.doc_id)
    const document = await loadingTask.promise
    if (current !== sequence) return
    totalPages.value = document.numPages
    const page = await document.getPage(Math.max(1, Math.min(pageNumber.value, document.numPages)))
    if (current !== sequence) return
    const baseWidth = page.getViewport({ scale: 1 }).width
    const scale = fitMode.value && availableWidth.value > 28 ? (availableWidth.value - 28) / baseWidth : zoom.value
    const viewport = page.getViewport({ scale })
    const offscreen = window.document.createElement('canvas')
    offscreen.width = Math.ceil(viewport.width)
    offscreen.height = Math.ceil(viewport.height)
    renderTask = page.render({ canvas: offscreen, viewport })
    activeRenderTask = renderTask
    await renderTask.promise
    const item: CachedPage = { canvas: offscreen, viewport, pageView: [...page.view], pageCount: document.numPages }
    setCachedPage(cacheKey.value, item)
    if (current !== sequence) return
    rendered.value = item
    await nextTick()
    if (current !== sequence) return
    paint(item)
  } catch (cause) {
    if (current === sequence) error.value = cause instanceof Error ? cause.message : 'PDF 加载失败。'
  } finally {
    if (renderTask && activeRenderTask === renderTask) activeRenderTask = undefined
    if (current === sequence) loading.value = false
  }
}

watch(() => props.refItem, (item, previous) => {
  const targetPage = item.page || 1
  const documentChanged = item.doc_id !== previous?.doc_id
  if (documentChanged) totalPages.value = undefined
  if (documentChanged || pageNumber.value !== targetPage) rendered.value = undefined
  pageNumber.value = targetPage
}, { deep: true })
watch([() => props.refItem.doc_id, pageNumber, zoom, fitMode, availableWidth], renderPage, { immediate: true })
onUnmounted(() => {
  sequence += 1
  resizeObserver?.disconnect()
  activeRenderTask?.cancel()
  activeRenderTask = undefined
  documents.forEach((task) => { void task.destroy().catch(() => undefined) })
})
</script>

<template>
  <section class="viewer">
    <header>
      <div><p>文档证据<span v-if="refItem.extraction_method"> · {{ extractionMethodLabel }}</span></p><strong>{{ refItem.doc_title || '文档原文' }}</strong></div>
      <div class="zoom" aria-label="PDF 缩放">
        <button :class="{ selected: fitMode }" @click="fitMode = true">适宽</button>
        <button v-for="value in [1, 1.5]" :key="value" :class="{ selected: !fitMode && zoom === value }" @click="setZoom(value)">{{ value }}×</button>
      </div>
    </header>
    <div class="page-controls">
      <button :disabled="pageNumber <= 1" aria-label="上一页文档" @click="pageNumber--">‹</button>
      <span>第 {{ pageNumber }} 页<span v-if="totalPages"> / {{ totalPages }}</span></span>
      <button :disabled="totalPages === undefined || pageNumber >= totalPages" aria-label="下一页文档" @click="pageNumber++">›</button>
      <button v-if="pageNumber !== refItem.page" class="return-to-ref" @click="pageNumber = refItem.page || 1">返回引用页</button>
    </div>
    <p v-if="!hasBboxes" class="notice">此证据没有可用的文字层坐标；已定位到页面，但不会绘制可能误导的高亮框。</p>
    <p v-if="loading" class="notice">正在渲染当前页…</p>
    <div v-if="error" class="error"><b>PDF 无法加载</b><span>{{ error }}</span><a :href="getDocumentFileUrl(refItem.doc_id)" target="_blank" rel="noreferrer">尝试直接打开文件</a></div>
    <div v-else ref="scrollElement" class="pdf-scroll">
      <div v-if="rendered" class="pdf-page" :style="{ width: `${rendered.viewport.width}px`, height: `${rendered.viewport.height}px` }">
        <canvas ref="canvas" />
        <PdfHighlightLayer v-if="hasBboxes && pageNumber === refItem.page" :bboxes="refItem.bboxes" :viewport="rendered.viewport" :page-view="rendered.pageView" />
      </div>
    </div>
  </section>
</template>

<style scoped>
.viewer { display: flex; flex: 1; min-height: 0; flex-direction: column; }
header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 17px 18px 12px; }
header > div:first-child { min-width: 0; flex: 1; }
header p { margin: 0 0 6px; color: var(--text-muted); font-size: 12px; }
header strong { display: block; color: var(--text-heading); font-size: 14px; font-weight: 500; line-height: 1.7; overflow-wrap: anywhere; }
.zoom { display: flex; flex-shrink: 0; align-items: flex-start; gap: 3px; }
.zoom button, .page-controls button { border: 1px solid var(--border-subtle); border-radius: 5px; color: var(--text-secondary); background: var(--bg-surface); }
.zoom button { padding: 3px 6px; font-size: 12px; }
.zoom .selected { border-color: var(--border-brand); color: var(--brand-primary); background: var(--brand-soft); }
.page-controls { display: flex; align-items: center; justify-content: center; gap: 10px; padding: 0 0 12px; color: var(--text-muted); font-size: 12px; }
.page-controls button { width: 24px; height: 24px; font-size: 20px; line-height: 12px; }
.page-controls .return-to-ref { width: auto; padding: 2px 6px; color: var(--brand-primary); font-size: 12px; }
.notice { margin: 0 16px 10px; border-radius: 6px; padding: 7px 9px; color: var(--state-warning); background: var(--state-warning-soft); font-size: 12px; }
/* 纸张外的舞台跟随主题；纸张本体（.pdf-page）恒白——见 tokens.css 文件头说明。
   渲染出来的 PDF 内容是白底黑字，若把纸张也变暗，白纸黑字会像浮在黑框里。 */
.pdf-scroll { flex: 1; min-height: 0; overflow: auto; padding: 14px; background: var(--bg-paper-stage); }
.pdf-page { position: relative; margin: 0 auto; box-shadow: var(--shadow-paper); background: var(--surface-paper); }
.pdf-page canvas { position: absolute; inset: 0; display: block; }
.error { display: grid; gap: 6px; margin: 14px; border: 1px solid var(--state-error-border); border-radius: var(--radius-md); padding: 12px; color: var(--state-error); background: var(--state-error-soft); font-size: 14px; }
.error span { overflow-wrap: anywhere; }
.error a { color: var(--brand-primary); }
</style>
