<script setup lang="ts">
import { computed } from 'vue'
import type { Bbox } from '../types/contracts'
import { pdfRectToCanvas, type PdfViewport } from '../utils/coords'

const props = defineProps<{ bboxes: Bbox[]; viewport: PdfViewport; pageView: number[] }>()
const rects = computed(() => props.bboxes.map((bbox) => pdfRectToCanvas(props.viewport, props.pageView, bbox)))
</script>

<template>
  <div class="highlight-layer" aria-hidden="true">
    <i v-for="(rect, index) in rects" :key="index" :style="{ left: `${rect.left}px`, top: `${rect.top}px`, width: `${rect.width}px`, height: `${rect.height}px` }" />
  </div>
</template>

<style scoped>
.highlight-layer { position: absolute; inset: 0; pointer-events: none; }.highlight-layer i { position: absolute; border: 1px solid var(--evidence-amber); border-radius: 2px; background: var(--evidence-amber-soft); animation: attention .55s ease-out 1; } @keyframes attention { from { opacity: 0; } to { opacity: 1; } }
</style>
