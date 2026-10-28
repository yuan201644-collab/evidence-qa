<script setup lang="ts">
import { nextTick, onMounted, reactive, ref } from 'vue'
import AppIcon from './AppIcon.vue'
import { useConversationStore } from '../stores/conversation'
import { followsSystem, toggleTheme, useSystemTheme, useTheme } from '../utils/theme'
import { getEditableModelProfiles, saveEditableModelProfile } from '../api/model-config'
import type { EditableModelProfile, ModelProfileDraft } from '../api/model-config'
import { useAuthStore } from '../stores/auth'

const emit = defineEmits<{ close: [] }>()
/**
 * 后端直连 KAG 时为真。
 *
 * 直连链路的模型、温度、检索路数全部由 KAG 容器里的配置决定，应用侧改了不生效。
 * 这一整块「API 与模型配置」（含密钥输入框）在这种模式下必须隐藏：留着它，
 * 演示时点开设置会看到一排 API 密钥输入框和一个改了没反应的模型 ID，
 * 既误导又不体面。配置本身仍然存在于后端，只是不在这条链路上生效。
 */
const props = withDefaults(defineProps<{ directKag?: boolean }>(), { directKag: false })

const { theme } = useTheme()
const conversation = useConversationStore()
const auth = useAuthStore()
const modelProfiles = ref<EditableModelProfile[]>([])
const modelDrafts = reactive<Record<string, ModelProfileDraft>>({})
const modelLoadError = ref('')
const modelLoading = ref(true)
const modelSaving = ref('')
const modelStatuses = reactive<Record<string, string>>({})

const panel = ref<HTMLElement | null>(null)
const clearConfirmation = ref<HTMLElement | null>(null)
const clearConfirmOpen = ref(false)
const clearSuccess = ref(false)
const clearError = ref('')

/**
 * 面板用滑出式而不是居中弹窗：设置是「停下来调一下再回去」，居中弹窗会把
 * 整屏遮住，用户改完一个开关要多点一次关闭。
 *
 * 下面两件事是滑出面板必须自己做的：
 * 1. 打开后把焦点移进面板，否则键盘用户按 Tab 还在背后的按钮上走。
 * 2. 关闭时由父组件把焦点还给顶栏的设置按钮。
 */
onMounted(() => {
  panel.value?.querySelector<HTMLButtonElement>('.settings__tab')?.focus()
  if (auth.isAdmin) void loadModelProfiles()
})

async function loadModelProfiles() {
  modelLoadError.value = ''
  modelLoading.value = true
  try {
    modelProfiles.value = await getEditableModelProfiles()
    for (const profile of modelProfiles.value) {
      modelDrafts[profile.id] = { model: profile.model, base_url: profile.base_url, api_key: '' }
    }
  } catch (error) {
    modelLoadError.value = error instanceof Error ? error.message : '模型配置读取失败'
  } finally {
    modelLoading.value = false
  }
}

async function saveModelProfile(profile: EditableModelProfile) {
  const draft = modelDrafts[profile.id]
  if (!draft || modelSaving.value) return
  modelSaving.value = profile.id
  modelStatuses[profile.id] = ''
  modelLoadError.value = ''
  try {
    const saved = await saveEditableModelProfile(profile.id, { ...draft })
    const index = modelProfiles.value.findIndex((item) => item.id === profile.id)
    if (index >= 0) modelProfiles.value.splice(index, 1, saved)
    draft.api_key = ''
    modelStatuses[profile.id] = saved.configured
      ? '配置已保存，可在顶栏模型菜单中选择。'
      : '地址与模型已保存；填写 API 密钥后才能使用。'
    window.dispatchEvent(new Event('model-config-updated'))
  } catch (error) {
    modelStatuses[profile.id] = error instanceof Error ? error.message : '配置保存失败'
  } finally {
    modelSaving.value = ''
  }
}

async function requestClearConfirmation() {
  clearError.value = ''
  clearSuccess.value = false
  clearConfirmOpen.value = true
  await nextTick()
  clearConfirmation.value?.scrollIntoView({ block: 'nearest', behavior: 'auto' })
  clearConfirmation.value?.querySelector<HTMLButtonElement>('.settings-confirm__cancel')?.focus()
}

function clearAllLocalData() {
  clearError.value = ''
  if (!conversation.clearAllLocalData()) {
    clearError.value = '清理失败，请检查浏览器本地存储权限后重试。'
    return
  }
  useSystemTheme()
  clearConfirmOpen.value = false
  clearSuccess.value = true
}
</script>

<template>
  <div class="settings-backdrop" @click.self="emit('close')">
    <aside ref="panel" class="settings" role="dialog" aria-modal="true" aria-labelledby="settings-title">
      <header class="settings__head">
        <div class="settings__title">
          <span class="settings__mark"><AppIcon name="gear" :size="20" /></span>
          <div>
            <h2 id="settings-title">设置</h2>
            <p>{{ props.directKag ? '偏好保存在浏览器；模型与检索由 KAG 侧决定' : '偏好保存在浏览器，模型配置保存在后端' }}</p>
          </div>
        </div>
        <button class="icon-button close-button vm-press" aria-label="关闭设置" @click="emit('close')"><AppIcon name="close" :size="18" /></button>
      </header>

      <div class="settings__body">
        <section class="settings-group" aria-labelledby="settings-appearance">
          <h3 id="settings-appearance">外观</h3>
          <div class="settings-row">
            <div>
              <b>颜色主题</b>
              <small>{{ followsSystem ? '当前跟随系统设置' : '已手动指定，不随系统变化' }}</small>
            </div>
            <button class="settings__action" @click="toggleTheme()">
              <AppIcon :name="theme === 'dark' ? 'sun' : 'moon'" :size="15" />
              {{ theme === 'dark' ? '切换到亮色' : '切换到暗色' }}
            </button>
          </div>
        </section>

        <section v-if="!props.directKag && auth.isAdmin" class="settings-group" aria-labelledby="settings-model-config">
          <h3 id="settings-model-config">API 与模型配置</h3>
          <p class="settings-group__hint">配置已接入的 DeepSeek 与 GLM 服务。仅管理员可编辑；保存时密钥通过当前 HTTPS 页面提交给后端并写入项目的 .env 文件。接口不会返回密钥，浏览器也不会持久化密钥。密钥输入留空会保留现有值，保存后立即生效；本次问答使用的模型仍可在顶栏切换。</p>
          <p v-if="modelLoadError" class="settings-model__error" role="alert">{{ modelLoadError }}</p>
          <p v-if="modelLoading" class="settings-model__loading" role="status">正在读取模型配置…</p>
          <div v-else class="settings-model-list">
            <article v-for="profile in modelProfiles" :key="profile.id" class="settings-model" :data-profile-id="profile.id">
              <header class="settings-model__head">
                <div><b>{{ profile.label }}</b><small>{{ profile.provider }} · {{ profile.has_api_key ? '已有 API 密钥' : '尚未配置 API 密钥' }}</small></div>
                <span class="settings-model__state" :class="{ 'is-ready': profile.configured }">{{ profile.configured ? '可用' : '待配置' }}</span>
              </header>
              <label class="settings-field"><span>API 地址</span><input v-model="modelDrafts[profile.id].base_url" type="url" autocomplete="url" placeholder="https://api.example.com/v1" /></label>
              <label class="settings-field"><span>模型 ID</span><input v-model="modelDrafts[profile.id].model" type="text" autocomplete="off" placeholder="例如 deepseek-v4.1-flash" /></label>
              <label class="settings-field"><span>API 密钥</span><input v-model="modelDrafts[profile.id].api_key" type="password" autocomplete="new-password" :placeholder="profile.has_api_key ? '留空以保留现有密钥' : '粘贴服务商提供的 API 密钥'" /></label>
              <div class="settings-model__footer">
                <p v-if="modelStatuses[profile.id]" role="status">{{ modelStatuses[profile.id] }}</p>
                <button class="settings__action" :disabled="modelLoading || Boolean(modelSaving)" @click="saveModelProfile(profile)">{{ modelSaving === profile.id ? '正在保存…' : '保存配置' }}</button>
              </div>
            </article>
          </div>
        </section>
        <section v-else-if="!props.directKag" class="settings-group" aria-label="模型配置权限">
          <h3>模型服务</h3>
          <p class="settings-group__hint">模型服务地址与密钥由管理员统一维护。你可以在提问框中选择当前已启用的模型。</p>
        </section>
        <section v-else class="settings-group" aria-label="模型与检索来源">
          <h3>模型与检索</h3>
          <p class="settings-group__hint">本页的问答由 KAG 服务端完成：用哪个模型、怎么检索、召回多少块，都由 KAG 侧的配置决定，应用界面不提供这些选项。应用侧能调的是界面外观与本地会话。</p>
        </section>

        <section class="settings-group" aria-labelledby="settings-storage">
          <h3 id="settings-storage">数据与隐私</h3>
          <p class="settings-group__hint">
            {{ props.directKag ? '会话保存在当前浏览器，并按登录账号隔离。清空本机数据只清理当前账号的会话与界面偏好，不影响 KAG 侧任何配置。' : '会话与模型选择保存在当前浏览器，并按登录账号隔离；API 地址、模型 ID 和密钥保存在后端项目的 .env 文件。清空本机数据只清理当前账号的会话和模型选择，不会删除后端模型配置。' }}
          </p>
          <button class="settings__action settings__action--danger" @click="requestClearConfirmation">清空对话和本地设置</button>
          <section v-if="clearConfirmOpen" ref="clearConfirmation" class="settings-confirm" role="alertdialog" aria-labelledby="settings-clear-title" aria-describedby="settings-clear-description">
            <h4 id="settings-clear-title">确认清空本机数据？</h4>
            <p id="settings-clear-description">将删除当前账号在此浏览器保存的会话和问答记录，并恢复模型选择与主题的默认值。其他账号的会话及后端 API 地址、模型 ID 和密钥会保留。此操作无法撤销。</p>
            <p v-if="clearError" class="settings-confirm__error" role="alert">{{ clearError }}</p>
            <div class="settings-confirm__actions">
              <button class="settings__action settings__action--ghost settings-confirm__cancel" @click="clearConfirmOpen = false">取消</button>
              <button class="settings__action settings__action--danger" @click="clearAllLocalData">确认清空</button>
            </div>
          </section>
          <section v-if="clearSuccess" class="settings-confirm settings-confirm--success" role="status" aria-live="polite">
            <h4>已清空对话和本地设置</h4>
            <p>当前只保留一个空白会话；模型选择与主题已恢复默认。后端模型配置仍保留。</p>
            <button class="settings__action settings__action--success" @click="emit('close')">完成并关闭</button>
          </section>
        </section>
      </div>
    </aside>
  </div>
</template>

<style scoped>
.settings-backdrop { position: fixed; inset: 0; z-index: 220; display: flex; justify-content: flex-end; background: var(--scrim); backdrop-filter: blur(3px); }
.settings { display: flex; flex-direction: column; width: min(420px, 100%); height: 100%; overflow: hidden; border-left: 1px solid var(--border-subtle); background: var(--bg-elevated); box-shadow: var(--shadow-panel); }
.settings__head { display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; gap: 12px; padding: 22px 22px 18px; border-bottom: 1px solid var(--border-subtle); }
.settings__title { display: flex; align-items: center; gap: 12px; min-width: 0; }
.settings__mark { display: grid; width: 42px; height: 42px; flex-shrink: 0; place-items: center; border-radius: 12px; color: var(--brand-primary); background: var(--brand-soft); }
.settings__title h2 { margin: 0; color: var(--text-heading); font-size: 20px; }
.settings__title p { margin: 3px 0 0; color: var(--text-muted); font-size: 13px; }
.settings__body { flex: 1; min-height: 0; overflow-y: auto; padding: 20px 22px 26px; }
.settings-group + .settings-group { margin-top: 24px; padding-top: 20px; border-top: 1px solid var(--border-subtle); }
.settings-group h3 { margin: 0 0 7px; color: var(--text-heading); font-size: 15px; }
.settings-group__hint { margin: 0 0 12px; color: var(--text-secondary); font-size: 13px; line-height: 1.85; }

.settings-options { display: grid; gap: 8px; }
.settings-option { display: flex; align-items: flex-start; gap: 10px; padding: 12px 13px; border: 1px solid var(--border-subtle); border-radius: 12px; background: var(--bg-surface); cursor: pointer; transition: border-color .15s, background .15s; }
.settings-option:hover { border-color: var(--border-brand); }
.settings-option.active { border-color: var(--border-brand); background: var(--brand-soft); }
.settings-option input { width: 15px; height: 15px; flex-shrink: 0; margin: 2px 0 0; accent-color: var(--brand-primary); }
.settings-option__body { display: grid; gap: 3px; min-width: 0; }
.settings-option__body b { color: var(--text-primary); font-size: 14.5px; }
.settings-option__body small { color: var(--text-secondary); font-size: 12.5px; line-height: 1.7; }

.settings-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 13px; border: 1px solid var(--border-subtle); border-radius: 12px; background: var(--bg-surface); }
.settings-row b { display: block; color: var(--text-primary); font-size: 14.5px; }
.settings-row small { color: var(--text-muted); font-size: 12.5px; }
.settings-model-list { display: grid; gap: 10px; }
.settings-model { display: grid; gap: 10px; padding: 13px; border: 1px solid var(--border-subtle); border-radius: 12px; background: var(--bg-surface); }
.settings-model__head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.settings-model__head > div { display: grid; gap: 3px; min-width: 0; }
.settings-model__head b { color: var(--text-primary); font-size: 14px; }
.settings-model__head small { color: var(--text-secondary); font-size: 12px; }
.settings-model__state { flex: none; padding: 3px 7px; border: 1px solid var(--border-subtle); border-radius: 999px; color: var(--text-secondary); background: var(--bg-subtle); font-size: 11px; }
.settings-model__state.is-ready { border-color: var(--state-success-border); color: var(--state-success); background: var(--state-success-soft); }
.settings-field { display: grid; gap: 5px; color: var(--text-secondary); font-size: 12.5px; }
.settings-field input { width: 100%; min-width: 0; padding: 8px 9px; border: 1px solid var(--border-subtle); border-radius: 8px; outline: 0; color: var(--text-primary); background: var(--bg-elevated); font: inherit; font-size: 13px; }
.settings-field input:focus { border-color: var(--border-focus); box-shadow: 0 0 0 2px var(--brand-soft); }
.settings-field input::placeholder { color: var(--text-muted); }
.settings-model__footer { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-height: 32px; }
.settings-model__footer p, .settings-model__loading { margin: 0; color: var(--text-secondary); font-size: 12px; line-height: 1.6; }
.settings-model__footer p { flex: 1; }
.settings-model__error { margin: 0 0 10px; color: var(--state-error); font-size: 12.5px; line-height: 1.6; }
.settings__action:disabled { opacity: .55; cursor: wait; }
.settings__action { display: inline-flex; align-items: center; gap: 6px; flex-shrink: 0; padding: 7px 11px; border: 1px solid var(--border-brand); border-radius: 9px; color: var(--brand-primary); background: var(--brand-soft); font-size: 13px; }
.settings__action:hover { background: var(--brand-soft-strong); }
.settings__action--ghost { margin-top: 4px; border-color: var(--border-subtle); color: var(--text-secondary); background: var(--bg-surface); }
.settings__action--ghost:hover { border-color: var(--border-strong); background: var(--bg-subtle); }
.settings__action--danger { margin-top: 9px; border-color: var(--state-error-border); color: var(--state-error); background: var(--state-error-soft); }
.settings__action--danger:hover { border-color: var(--state-error); }
.settings-confirm { margin-top: 12px; padding: 13px; border: 1px solid var(--state-error-border); border-radius: 11px; background: var(--state-error-soft); }
.settings-confirm h4 { margin: 0 0 6px; color: var(--state-error); font-size: 14px; }
.settings-confirm p { margin: 0; color: var(--text-secondary); font-size: 12.5px; line-height: 1.7; }
.settings-confirm__error { margin-top: 8px !important; color: var(--state-error) !important; }
.settings-confirm__actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 11px; }
.settings-confirm__actions .settings__action { margin-top: 0; }
.settings-confirm--success { border-color: var(--state-success-border); background: var(--state-success-soft); }
.settings-confirm--success h4 { color: var(--state-success); }
.settings__action--success { margin-top: 10px; border-color: var(--state-success-border); color: var(--state-success); background: var(--bg-surface); }

@media (max-width: 767px) {
  .settings { width: 100%; }
  .settings__head { padding: 18px 18px 15px; }
  .settings__body { padding: 17px 18px 22px; }
}
</style>
