<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'
import ModelSelector from './ModelSelector.vue'
import QuestionGallery from './QuestionGallery.vue'
import type { Suggestion } from '../types/contracts'

const props = withDefaults(
  defineProps<{
    disabled: boolean
    welcome?: boolean
    suggestions?: Suggestion[]
    modelProfileId?: string
    retrievalMode?: 'standard' | 'deep'
    kagReady?: boolean
  }>(),
  { welcome: false, suggestions: () => [], modelProfileId: '', retrievalMode: 'standard', kagReady: false },
)
const emit = defineEmits<{
  submit: [question: string]
  stop: []
  'update:modelProfileId': [value: string]
  'update:retrievalMode': [value: 'standard' | 'deep']
}>()
const value = ref('')
const textarea = ref<HTMLTextAreaElement | null>(null)
const retrievalModeSelect = ref<HTMLSelectElement | null>(null)
const deepConfirmDialog = ref<HTMLElement | null>(null)
const deepConfirmCancel = ref<HTMLButtonElement | null>(null)
const deepConfirmOpen = ref(false)
const expandedSuggestionReasonIndex = ref<number | null>(null)
/**
 * 把问句放进输入框，但**不发送**。
 *
 * 用于主页的示例卡片：点一张卡只是「我想问这个」，发不发由用户看完再说 ——
 * 直接替他发出去，他连问句长什么样都没看清，答错了也不知道该怪谁。
 * 与 `pick()`（追问建议）的区别就在这里：建议是用户已经看过上下文才点的，
 * 示例是冷启动时随手点的。
 */
function fill(question: string) {
  value.value = question
  focus()
}
/** 把光标挪进输入框并放到末尾，用户接着改就行。 */
function focus() {
  const el = textarea.value
  if (!el) return
  el.focus()
  // 光标落到末尾而不是全选：全选会让下一次输入直接覆盖整个问句，
  // 而多数人是想在此基础上补一句。
  el.setSelectionRange(el.value.length, el.value.length)
}
defineExpose({ fill, focus })
function submit() {
  if (props.disabled || !value.value.trim()) return
  emit('submit', value.value.trim())
  value.value = ''
}
function keydown(event: KeyboardEvent) {
  if (event.isComposing || event.keyCode === 229) return
  if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); submit() }
}
/** 点建议 = 直接提交。建议本身已经是完整问句，不需要再让用户编辑。 */
function pick(question: string) {
  if (props.disabled) return
  // 清空输入框：用户点了建议，说明他放弃了自己正在打的那半句。
  value.value = ''
  expandedSuggestionReasonIndex.value = null
  emit('submit', question)
}
function toggleSuggestionReason(index: number) {
  expandedSuggestionReasonIndex.value = expandedSuggestionReasonIndex.value === index ? null : index
}
function updateRetrievalMode(event: Event) {
  const target = event.target
  if (!(target instanceof HTMLSelectElement)) return
  if (target.value === 'deep') {
    // Keep the controlled select on its current value until the user accepts the Beta warning.
    target.value = props.retrievalMode
    deepConfirmOpen.value = true
    return
  }
  emit('update:retrievalMode', 'standard')
}
function closeDeepConfirm() {
  deepConfirmOpen.value = false
}
function confirmDeepRetrieval() {
  emit('update:retrievalMode', 'deep')
  deepConfirmOpen.value = false
}
function onDeepConfirmKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    closeDeepConfirm()
    return
  }
  if (event.key !== 'Tab') return
  const buttons = deepConfirmDialog.value?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')
  if (!buttons?.length) return
  const first = buttons[0]
  const last = buttons[buttons.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}
watch(deepConfirmOpen, async (open) => {
  await nextTick()
  if (open) deepConfirmCancel.value?.focus()
  else retrievalModeSelect.value?.focus()
})
</script>

<template>
  <div class="composer-wrap" :class="{ 'composer-wrap--welcome': welcome }">
    <!-- 追问建议：只在有建议且不在等待时出现 —— 等待时点不动，徒增困惑 -->
    <div v-if="suggestions.length && !disabled" class="suggestions vm-detail">
      <span class="suggestions__label"><AppIcon name="spark" :size="12" />可以接着问</span>
      <ul class="suggestions__list">
        <li v-for="(item, index) in suggestions" :key="index" class="suggestion-item">
          <button
            type="button"
            class="suggestion-chip vm-press"
            :title="item.reason"
            :aria-label="`追问建议：${item.question}${item.reason ? '（依据：' + item.reason + '）' : ''}`"
            @click="pick(item.question)"
          >
            <span class="suggestion-chip__text">{{ item.question }}</span>
            <span v-if="item.reason" class="suggestion-chip__reason">{{ item.reason }}</span>
          </button>
          <button
            v-if="item.reason"
            type="button"
            class="suggestion-reason-toggle vm-press"
            :aria-label="`${expandedSuggestionReasonIndex === index ? '收起' : '查看'}建议依据：${item.reason}`"
            :aria-expanded="expandedSuggestionReasonIndex === index"
            :aria-controls="`suggestion-reason-panel-${index}`"
            @click="toggleSuggestionReason(index)"
          >
            <AppIcon name="help" :size="14" />
          </button>
        </li>
      </ul>
      <div
        v-if="expandedSuggestionReasonIndex !== null && suggestions[expandedSuggestionReasonIndex]?.reason"
        :id="`suggestion-reason-panel-${expandedSuggestionReasonIndex}`"
        class="suggestions__reason-panel"
        role="status"
      >
        <b>建议依据</b>
        <span>{{ suggestions[expandedSuggestionReasonIndex].reason }}</span>
      </div>
    </div>
    <form class="composer" @submit.prevent="submit">
      <div class="composer__input"><QuestionGallery v-if="!disabled" @pick="fill" /><span v-else class="composer__spark"><AppIcon name="spark" :size="19" /></span><textarea ref="textarea" v-model="value" :disabled="disabled" rows="2" aria-label="输入问题" :placeholder="welcome ? '有什么问题？试着问一问数据或文档…' : '继续提问，或补充需要的信息…'" @keydown="keydown" /></div>
      <div class="composer__bottom">
        <div class="composer__aside">
          <!-- 模型选择放在这里而不是顶栏：它是「这一问用哪个模型」的设置，
               和输入是一体的；放顶栏会让人以为它管整个工作台。 -->
          <div class="composer__mode-group" role="group" aria-label="本轮问答设置">
            <ModelSelector
              :model-profile-id="modelProfileId"
              :disabled="disabled"
              @update:model-profile-id="emit('update:modelProfileId', $event)"
            />
            <label class="retrieval-mode" :class="{ 'retrieval-mode--deep': retrievalMode === 'deep' }">
              <AppIcon class="retrieval-mode__icon" name="document" :size="14" />
              <select ref="retrievalModeSelect" :value="retrievalMode" :disabled="disabled" aria-label="文档检索模式" @change="updateRetrievalMode">
              <option value="standard">标准检索</option>
                <option value="deep" :disabled="!kagReady">KAG 补充深度检索</option>
              </select>
              <AppIcon class="retrieval-mode__chevron" name="chevron" :size="12" />
            </label>
          </div>
          <span class="composer__hint">{{ disabled ? '正在等待服务返回，可停止等待' : 'Enter 发送 · Shift + Enter 换行' }}</span>
        </div>
        <button v-if="disabled" type="button" class="stop-button vm-press" @click="emit('stop')"><AppIcon name="stop" :size="13" />停止等待</button>
        <button v-else type="submit" class="vm-press vm-interact" :disabled="!value.trim()" aria-label="发送问题"><span>发送</span><AppIcon class="vm-icon-shift" name="arrow" :size="17" /></button>
      </div>
    </form>
    <p v-if="!kagReady" class="retrieval-mode-note">KAG 补充深度检索尚未就绪，当前可使用标准检索。</p>
    <p v-else-if="retrievalMode === 'deep'" class="retrieval-mode-note">KAG 补充深度检索会增加等待时间；仅对文档检索生效。</p>
    <p class="composer-note"><AppIcon name="link" :size="12" />回答请结合来源证据核对；信息不足时可继续补充条件。</p>
    <details class="composer-note-mobile">
      <summary aria-label="查看回答提示"><AppIcon name="link" :size="13" /><span>提示</span></summary>
      <p>回答请结合来源证据核对；信息不足时可继续补充条件。</p>
    </details>
    <Teleport to="body">
      <Transition name="vm-dialog">
        <div v-if="deepConfirmOpen" class="retrieval-beta-backdrop" @click.self="closeDeepConfirm">
          <section
            ref="deepConfirmDialog"
            class="retrieval-beta"
            role="dialog"
            aria-modal="true"
            aria-labelledby="retrieval-beta-title"
            tabindex="-1"
            @keydown="onDeepConfirmKeydown"
          >
            <header class="retrieval-beta__head">
              <span class="retrieval-beta__icon"><AppIcon name="warning" :size="18" /></span>
              <span class="retrieval-beta__badge">BETA 测试路径</span>
              <button type="button" class="icon-button close-button vm-press" aria-label="关闭 Beta 提示" @click="closeDeepConfirm"><AppIcon name="close" :size="16" /></button>
            </header>
            <h2 id="retrieval-beta-title">启用 KAG 补充深度检索？</h2>
            <p>此路径会在标准 RAG 检索基础上请求 KAG 补充候选原文，之后仍由本系统生成答案和引用。</p>
            <div class="retrieval-beta__notice"><b>稳定性与效率尚未充分验证</b><span>检索可能更慢、召回不完整或发生失败；这是 Beta 测试路径，不是默认问答方式。</span></div>
            <footer class="retrieval-beta__actions">
              <button ref="deepConfirmCancel" type="button" class="retrieval-beta__cancel" @click="closeDeepConfirm">继续标准检索</button>
              <button type="button" class="retrieval-beta__confirm" @click="confirmDeepRetrieval">确认使用 KAG 补充检索</button>
            </footer>
          </section>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<style scoped>
.composer-wrap { flex-shrink: 0; padding: 15px clamp(20px, 3vw, 42px) max(16px, env(safe-area-inset-bottom)); }
.composer-wrap--welcome { padding-top: 8px; }
/* 建议区与输入框共用同一条 850px 中轴，视觉上属于同一块 */
.suggestions { width: 100%; max-width: 930px; margin: 0 auto 10px; }
.suggestions__label { display: flex; align-items: center; gap: 5px; margin-bottom: 7px; color: var(--text-faint); font-size: 12px; }
.suggestions__list { display: flex; flex-wrap: wrap; gap: 8px; margin: 0; padding: 0; list-style: none; }
.suggestion-item { display: flex; min-width: 0; }
.suggestion-chip { display: flex; flex-direction: column; gap: 2px; max-width: 100%; padding: 7px 12px; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); color: var(--text-secondary); background: var(--bg-surface); text-align: left; transition: border-color .15s, color .15s, background .15s; }
.suggestion-chip:hover { border-color: var(--border-brand); color: var(--brand-primary); background: var(--brand-soft); }
.suggestion-chip__text { font-size: 14px; line-height: 1.5; }
/* 「依据」这一行是可解释性的一部分：让用户点之前就知道建议从哪来 */
.suggestion-chip__reason { color: var(--text-faint); font-size: 12px; line-height: 1.4; }
.suggestion-chip:hover .suggestion-chip__reason { color: var(--brand-primary); }
.suggestion-reason-toggle { display: none; }
.suggestions__reason-panel { display: none; }
.composer { width: 100%; max-width: 930px; margin: 0 auto; padding: 17px 20px 12px; border: 1px solid var(--border-strong); border-radius: 17px; background: var(--bg-surface); box-shadow: 0 8px 26px rgba(35, 59, 102, .06); transition: border-color .15s, box-shadow .15s; }
.composer:focus-within { border-color: var(--border-focus); box-shadow: var(--shadow-focus-ring), var(--shadow-card); }
.composer__input { display: flex; align-items: flex-start; gap: 12px; }
.composer__spark { color: var(--text-faint); padding-top: 3px; }
textarea { flex: 1; min-width: 0; min-height: 54px; max-height: 160px; resize: vertical; padding: 0; border: 0; outline: none; color: var(--text-primary); background: transparent; font-size: 16px; line-height: 1.9; }
textarea::placeholder { color: var(--text-faint); }
.composer__bottom { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 10px; }
/* 左侧一组：模型选择 + 输入提示。两者都是「提问前的设定」，放一起。 */
.composer__aside { display: flex; align-items: center; gap: 10px; min-width: 0; }
.composer__mode-group { display: flex; align-items: center; gap: 6px; min-width: 0; }
.composer__hint { color: var(--text-faint); font-size: 11px; white-space: nowrap; }
.retrieval-mode { position: relative; display: inline-flex; flex: 0 1 auto; align-items: center; min-width: 0; min-height: 30px; border: 1px solid var(--border-brand); border-radius: 9px 6px 9px 6px; color: var(--brand-primary); background: var(--brand-soft); transition: border-color var(--motion-fast) var(--motion-ease), background-color var(--motion-fast) var(--motion-ease); }
.retrieval-mode:hover { border-color: var(--brand-primary); background: var(--brand-soft-strong); }
.retrieval-mode--deep { border-color: var(--state-warning-border); color: var(--state-warning); background: var(--state-warning-soft); }
.retrieval-mode--deep:hover { border-color: var(--state-warning); background: var(--state-warning-soft); }
.retrieval-mode__icon { flex: 0 0 auto; margin-left: 9px; pointer-events: none; }
.retrieval-mode select { width: 158px; max-width: 174px; min-width: 0; min-height: 28px; padding: 5px 23px 5px 7px; appearance: none; border: 0; outline: 0; color: inherit; background: transparent; font-size: 11.5px; font-weight: 600; cursor: pointer; }
.retrieval-mode select:disabled { cursor: not-allowed; opacity: .62; }
.retrieval-mode:focus-within { border-color: var(--border-focus); outline: 2px solid var(--brand-soft); outline-offset: 1px; }
.retrieval-mode__chevron { position: absolute; right: 7px; transform: rotate(90deg); pointer-events: none; }
.retrieval-beta-backdrop { position: fixed; inset: 0; z-index: 230; display: grid; place-items: center; padding: 20px; background: var(--scrim); backdrop-filter: blur(3px); }
.retrieval-beta { width: min(440px, 100%); padding: 22px; border: 1px solid var(--border-subtle); border-radius: 15px 10px 15px 10px; color: var(--text-primary); background: var(--bg-elevated); box-shadow: var(--shadow-guide); }
.retrieval-beta__head { display: flex; align-items: center; gap: 9px; }
.retrieval-beta__icon { display: grid; width: 32px; height: 32px; place-items: center; border-radius: 8px 5px 8px 5px; color: var(--state-warning); background: var(--state-warning-soft); }
.retrieval-beta__badge { padding: 4px 8px; border: 1px solid var(--state-warning-border); border-radius: 999px; color: var(--state-warning); font-size: 10px; font-weight: 700; letter-spacing: .04em; }
.retrieval-beta__head .close-button { margin-left: auto; }
.retrieval-beta h2 { margin: 17px 0 8px; color: var(--text-heading); font-size: 18px; }
.retrieval-beta > p { margin: 0; color: var(--text-secondary); font-size: 13px; line-height: 1.7; }
.retrieval-beta__notice { display: grid; gap: 4px; margin-top: 14px; padding: 11px 13px; border-left: 3px solid var(--state-warning); border-radius: 3px 8px 8px 3px; color: var(--text-secondary); background: var(--state-warning-soft); font-size: 12px; line-height: 1.6; }
.retrieval-beta__notice b { color: var(--state-warning); font-size: 12px; }
.retrieval-beta__actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px; }
.retrieval-beta__actions button { min-height: 36px; padding: 8px 12px; border-radius: 8px 5px 8px 5px; font-size: 12px; font-weight: 600; }
.retrieval-beta__cancel { border: 1px solid var(--border-strong); color: var(--text-secondary); background: var(--bg-surface); }
.retrieval-beta__cancel:hover { border-color: var(--border-brand); color: var(--brand-primary); background: var(--brand-soft); }
.retrieval-beta__confirm { border: 1px solid var(--brand-primary); color: var(--text-on-brand); background: var(--brand-primary); }
.retrieval-beta__confirm:hover { background: var(--brand-deep); }
.retrieval-mode-note { width: 100%; max-width: 930px; margin: 6px auto 0; color: var(--text-faint); font-size: 10px; }
/* 输入框里的模型选择器：比顶栏那版更收敛 —— 它不该跟「发送」抢注意力。
   尺寸用 :deep() 穿透打进子组件，因为 ModelSelector 的样式是 scoped 的。 */
.composer__mode-group :deep(.model-picker__trigger) { min-height: 30px; max-width: 190px; padding: 5px 9px; gap: 6px; border-radius: 9px 6px 9px 6px; font-size: 11.5px; }
.composer__mode-group :deep(.model-picker__menu) { top: auto; bottom: calc(100% + 8px); left: 0; right: auto; }
button { display: flex; gap: 10px; align-items: center; padding: 9px 15px; border: 0; border-radius: 9px; color: var(--text-on-brand); background: var(--brand-primary); font-size: 13px; font-weight: 600; }
button:hover { background: var(--brand-deep); }
button:disabled { opacity: 1; color: var(--text-faint); background: var(--bg-subtle); }
button.stop-button { color: var(--text-secondary); background: var(--bg-subtle); }
button.stop-button:hover { background: var(--bg-hover); }
.composer-note { display: flex; align-items: center; justify-content: center; gap: 5px; margin: 9px 0 0; color: var(--text-secondary); font-size: 12px; }
.composer-note-mobile { display: none; }
@media (max-width: 767px) {
  .composer-wrap, .composer-wrap--welcome { padding: 8px 14px max(8px, env(safe-area-inset-bottom)); }
  .composer { padding: 11px 12px 8px; border-radius: 13px; }
  textarea { font-size: 15px; min-height: 42px; }
  .composer__hint { display: none; }
  .composer__bottom { margin-top: 7px; }
  .composer__aside { gap: 6px; }
  .composer__aside { gap: 6px; }
  .composer__mode-group { gap: 5px; }
  .composer__mode-group :deep(.model-picker__trigger) { max-width: 132px; min-height: 28px; padding: 4px 7px; gap: 4px; font-size: 11px; }
  .retrieval-mode { min-height: 28px; }
  .retrieval-mode__icon { width: 12px; height: 12px; margin-left: 6px; }
  .retrieval-mode select { width: 142px; max-width: 154px; min-height: 26px; padding: 4px 18px 4px 5px; font-size: 10px; }
  .retrieval-mode__chevron { right: 5px; width: 10px; height: 10px; }
  .retrieval-beta { padding: 18px; }
  .retrieval-beta__actions { flex-direction: column-reverse; }
  .retrieval-beta__actions button { width: 100%; justify-content: center; }
  .retrieval-mode-note { padding: 0 4px; font-size: 9px; }

  .suggestions { display: grid; grid-template-columns: max-content minmax(0, 1fr); align-items: center; column-gap: 8px; margin-bottom: 7px; }
  .suggestions__label { margin: 0; white-space: nowrap; font-size: 11px; }
  .suggestions__list { min-width: 0; flex-wrap: nowrap; gap: 5px; overflow-x: auto; overscroll-behavior-x: contain; scrollbar-width: none; }
  .suggestions__list::-webkit-scrollbar { display: none; }
  .suggestion-item { flex: 0 0 min(72vw, 300px); max-width: 100%; align-items: center; gap: 4px; }
  .suggestion-chip { display: block; flex: 1; min-width: 0; overflow: hidden; padding: 7px 9px; border-radius: 9px; }
  .suggestion-chip__text { display: block; overflow: hidden; color: inherit; font-size: 13px; line-height: 1.35; text-overflow: ellipsis; white-space: nowrap; }
  .suggestion-chip__reason { display: none; }
  .suggestion-reason-toggle { display: grid; width: 28px; height: 30px; flex: 0 0 28px; place-items: center; padding: 0; border: 1px solid var(--border-subtle); border-radius: 8px; color: var(--text-faint); background: var(--bg-surface); }
  .suggestion-reason-toggle[aria-expanded="true"] { color: var(--brand-primary); border-color: var(--border-brand); background: var(--brand-soft); }
  .suggestions__reason-panel { display: flex; grid-column: 1 / -1; gap: 6px; margin-top: 6px; padding: 8px 10px; border: 1px solid var(--border-subtle); border-radius: 9px; color: var(--text-secondary); background: var(--bg-subtle); font-size: 12px; line-height: 1.45; }
  .suggestions__reason-panel b { flex: 0 0 auto; color: var(--text-primary); }

  .composer-note { display: none; }
  .composer-note-mobile { position: relative; display: block; width: max-content; margin: 5px auto 0; color: var(--text-faint); }
  .composer-note-mobile summary { display: flex; align-items: center; gap: 4px; padding: 2px 7px; border-radius: 999px; color: inherit; font-size: 11px; list-style: none; cursor: pointer; }
  .composer-note-mobile summary::-webkit-details-marker { display: none; }
  .composer-note-mobile summary:hover, .composer-note-mobile[open] summary { color: var(--brand-primary); background: var(--brand-soft); }
  .composer-note-mobile > p { position: absolute; z-index: 5; right: 50%; bottom: calc(100% + 7px); width: min(88vw, 340px); margin: 0; padding: 9px 11px; transform: translateX(50%); border: 1px solid var(--border-subtle); border-radius: 10px; color: var(--text-secondary); background: var(--bg-surface); box-shadow: var(--shadow-card); font-size: 12px; line-height: 1.5; text-align: center; }
}
</style>
