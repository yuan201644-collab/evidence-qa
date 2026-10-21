<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import AnswerBubble from './AnswerBubble.vue'
import type { ChatMessage } from '../stores/persistence'
import type { AskProgressStage } from '../types/contracts'

const props = defineProps<{ messages: ChatMessage[]; activeMessageId: string; activeRefIndex: number; sessionId: string; progressByMessage: Record<string, AskProgressStage[]> }>()
const emit = defineEmits<{ selectRef: [messageId: string, index: number]; retry: [question: string] }>()
const element = ref<HTMLElement>()
async function scrollToLatest(behavior: ScrollBehavior) {
  await nextTick()
  const target = element.value
  if (!target) return
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  target.scrollTo({ top: target.scrollHeight, behavior: reduceMotion ? 'auto' : behavior })
}
watch(
  () => {
    const latest = props.messages.at(-1)
    const latestProgressCount = latest ? props.progressByMessage[latest.id]?.length ?? 0 : 0
    return [props.sessionId, props.messages.length, latest?.pending, latestProgressCount] as const
  },
  ([sessionId], previous) => {
    // 切换历史会话或首次挂载时直接落到底部，避免从顶部播放一段滚动动画。
    // 同一会话中新消息到达时仍保留平滑跟随。
    const behavior: ScrollBehavior = !previous || previous[0] !== sessionId ? 'auto' : 'smooth'
    /*
      等待结束（pending: true -> false）这一帧先不滚。
      那一刻答案块刚从"时间线"换成"答案 + 一行摘要"，DOM 高度还在变；
      平滑滚动和这次塌缩撞在同一帧，看起来就是内容一边往上抽一边被拉到底。
      让出两帧、等布局稳定后再滚，滚动目标才是最终高度，视觉上只剩一次平顺跟随。
      提问时（pending 变 true）不动：那时高度还在增长，逐帧跟着滚是用户预期的。
    */
    const settled = previous?.[2] === true && props.messages.at(-1)?.pending === false
    if (settled) {
      void nextTick().then(() => requestAnimationFrame(() => requestAnimationFrame(() => { void scrollToLatest(behavior) })))
      return
    }
    void scrollToLatest(behavior)
  },
  { immediate: true, flush: 'post' },
)
</script>

<template>
  <main ref="element" class="thread" aria-label="问答记录">
    <div class="thread__inner">
      <AnswerBubble v-for="message in messages" :key="message.id" :message="message" :progress="progressByMessage[message.id] ?? []" :is-active="message.id === activeMessageId" :active-ref-index="message.id === activeMessageId ? activeRefIndex : -1" :session-id="sessionId" @select-ref="emit('selectRef', message.id, $event)" @retry="emit('retry', $event)" />
    </div>
  </main>
</template>

<style scoped>
.thread { flex: 1; min-height: 0; overflow-y: auto; padding: 15px clamp(20px, 3vw, 42px) 22px; }
.thread__inner { width: 100%; max-width: 930px; margin: 0 auto; }
@media (max-width: 767px) { .thread { padding: 4px 17px 12px; } }
</style>
