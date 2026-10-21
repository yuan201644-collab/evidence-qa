<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import AppIcon from './AppIcon.vue'
import { getModelCatalog } from '../api/models'
import { localSessions } from '../stores/persistence'
import type { ModelProfileOption } from '../types/contracts'

const props = withDefaults(defineProps<{ modelProfileId: string; disabled?: boolean }>(), { disabled: false })
const emit = defineEmits<{ 'update:modelProfileId': [value: string] }>()

const profiles = ref<ModelProfileOption[]>([])
const isOpen = ref(false)
const isLoading = ref(true)
const loadFailed = ref(false)
const root = ref<HTMLElement | null>(null)
const selected = computed(() => profiles.value.find((item) => item.id === props.modelProfileId))

function selectProfile(profile: ModelProfileOption) {
  if (!profile.configured || props.disabled) return
  emit('update:modelProfileId', profile.id)
  try { localSessions.setModelProfileId(profile.id) } catch { /* Storage can be unavailable in private browsing. */ }
  isOpen.value = false
}

function onPointerDown(event: PointerEvent) {
  if (root.value && event.target instanceof Node && !root.value.contains(event.target)) isOpen.value = false
}

function onKeyDown(event: KeyboardEvent) {
  if (event.key === 'Escape') isOpen.value = false
}

async function loadProfiles() {
  isLoading.value = true
  loadFailed.value = false
  try {
    const catalog = await getModelCatalog()
    profiles.value = catalog.models
    // 与 store 共用按账号分作用域的键；此前这里另写一个无作用域的
    // verimind.modelProfileId，既读不回来（hydrate 读的是分作用域键），
    // 也不受「清空本地设置」清除，会把上一位用户的模型选择留在这台机器上。
    const stored = (() => { try { return localSessions.getModelProfileId() } catch { return null } })()
    const preferred = profiles.value.find((item) => item.id === (stored || props.modelProfileId) && item.configured)
    const backendDefault = profiles.value.find((item) => item.id === catalog.default_model_id && item.configured)
    const firstReady = profiles.value.find((item) => item.configured)
    const initial = preferred ?? backendDefault ?? firstReady
    if (initial && initial.id !== props.modelProfileId) selectProfile(initial)
  } catch {
    loadFailed.value = true
  } finally {
    isLoading.value = false
  }
}

function onModelConfigUpdated() {
  void loadProfiles()
}

onMounted(() => {
  document.addEventListener('pointerdown', onPointerDown)
  document.addEventListener('keydown', onKeyDown)
  window.addEventListener('model-config-updated', onModelConfigUpdated)
  void loadProfiles()
})

onUnmounted(() => {
  document.removeEventListener('pointerdown', onPointerDown)
  document.removeEventListener('keydown', onKeyDown)
  window.removeEventListener('model-config-updated', onModelConfigUpdated)
})
</script>

<template>
  <div ref="root" class="model-picker">
    <button
      class="model-picker__trigger"
      type="button"
      :disabled="disabled || isLoading || loadFailed"
      :aria-expanded="isOpen"
      aria-haspopup="menu"
      aria-label="选择问答模型"
      :title="loadFailed ? '模型配置不可用或尚未配置可用模型' : `当前模型：${selected?.label ?? '读取中'}`"
      @click="isOpen = !isOpen"
    >
      <AppIcon name="spark" :size="15" />
      <span class="model-picker__label">{{ isLoading ? '读取模型' : selected?.label ?? '模型未就绪' }}</span>
      <AppIcon class="model-picker__chevron" name="chevron" :size="12" />
    </button>
    <Transition name="model-menu">
      <div v-if="isOpen" class="model-picker__menu" role="menu" aria-label="选择模型">
        <p class="model-picker__eyebrow">本次问答使用</p>
        <button
          v-for="profile in profiles"
          :key="profile.id"
          class="model-picker__option"
          :class="{ 'is-selected': profile.id === modelProfileId, 'is-disabled': !profile.configured }"
          type="button"
          role="menuitemradio"
          :aria-checked="profile.id === modelProfileId"
          :disabled="!profile.configured || disabled"
          @click="selectProfile(profile)"
        >
          <span class="model-picker__option-mark"><AppIcon v-if="profile.id === modelProfileId" name="check" :size="13" /></span>
          <span class="model-picker__option-copy"><b>{{ profile.label }}</b><small>{{ profile.provider }} · {{ profile.configured ? profile.model : '需在后端配置凭据' }}</small></span>
          <span v-if="!profile.configured" class="model-picker__badge">未配置</span>
        </button>
        <p class="model-picker__foot">密钥保存在服务端，不会发送到浏览器。</p>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.model-picker { position: relative; flex-shrink: 0; }
.model-picker__trigger { display: flex; align-items: center; gap: 8px; max-width: 220px; min-height: 36px; padding: 7px 10px; border: 1px solid var(--border-brand); border-radius: 9px 6px 9px 6px; color: var(--brand-primary); background: var(--brand-soft); font-size: 13px; font-weight: 650; transition: border-color var(--motion-fast) var(--motion-ease), background-color var(--motion-fast) var(--motion-ease), transform var(--motion-fast) var(--motion-ease); }
.model-picker__trigger:hover:not(:disabled) { border-color: var(--brand-primary); background: var(--brand-soft-strong); transform: translateY(-1px); }
.model-picker__trigger:focus-visible, .model-picker__option:focus-visible { outline: 2px solid var(--brand-primary); outline-offset: 2px; }
.model-picker__trigger:disabled { cursor: not-allowed; opacity: .6; }
.model-picker__label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.model-picker__chevron { transform: rotate(90deg); }
.model-picker__menu { position: absolute; z-index: 40; top: calc(100% + 8px); right: 0; width: min(290px, calc(100vw - 32px)); padding: 8px; border: 1px solid var(--border-subtle); border-radius: 12px 8px 12px 8px; background: var(--bg-surface); box-shadow: 0 16px 40px rgb(20 35 64 / 16%); }
.model-picker__eyebrow { margin: 3px 8px 7px; color: var(--text-faint); font-size: 11px; letter-spacing: .08em; }
.model-picker__option { display: flex; align-items: center; gap: 9px; width: 100%; padding: 9px 8px; border: 1px solid transparent; border-radius: 8px 5px 8px 5px; color: var(--text-primary); text-align: left; background: transparent; transition: border-color var(--motion-fast) var(--motion-ease), background-color var(--motion-fast) var(--motion-ease); }
.model-picker__option:hover:not(:disabled), .model-picker__option.is-selected { border-color: var(--border-brand); background: var(--brand-soft); }
.model-picker__option:disabled { cursor: not-allowed; opacity: .55; }
.model-picker__option-mark { display: grid; width: 18px; height: 18px; flex: 0 0 18px; place-items: center; color: var(--accent-teal); }
.model-picker__option-copy { display: grid; gap: 3px; min-width: 0; }
.model-picker__option-copy b { overflow: hidden; font-size: 13px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
.model-picker__option-copy small { color: var(--text-muted); font-size: 11px; }
.model-picker__badge { margin-left: auto; color: var(--text-faint); font-size: 11px; white-space: nowrap; }
.model-picker__foot { margin: 7px 8px 3px; color: var(--text-faint); font-size: 11px; line-height: 1.5; }
.model-menu-enter-active { transition: opacity var(--motion-base) var(--motion-ease), transform var(--motion-base) var(--motion-ease); }
.model-menu-leave-active { transition: opacity var(--motion-fast) var(--motion-exit-ease), transform var(--motion-fast) var(--motion-exit-ease); pointer-events: none; }
.model-menu-enter-from, .model-menu-leave-to { opacity: 0; transform: translateY(-4px) scale(.985); }
@media (prefers-reduced-motion: reduce) { .model-picker__trigger, .model-menu-enter-active, .model-menu-leave-active { transition: none; } .model-picker__trigger:hover:not(:disabled) { transform: none; } }
@media (max-width: 767px) { .model-picker__trigger { min-height: 33px; max-width: 152px; padding: 6px 8px; gap: 6px; font-size: 12px; } .model-picker__menu { right: -4px; } }
</style>
