<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import AppIcon from './AppIcon.vue'
import { useAuthStore, type AuthUser } from '../stores/auth'

const emit = defineEmits<{ close: [] }>()
const auth = useAuthStore()
const users = ref<AuthUser[]>([])
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const success = ref('')
const createDraft = reactive({ username: '', password: '', role: 'user' as 'admin' | 'user' })
const passwordDrafts = reactive<Record<string, string>>({})
const changePassword = reactive({ current: '', next: '', confirm: '' })
const passwordChanged = ref(false)
const isAdmin = computed(() => auth.user?.role === 'admin')

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { cache: 'no-store', ...init })
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: string } | null
    throw new Error(body?.detail || `请求失败（${response.status}）`)
  }
  return response.json() as Promise<T>
}

async function loadUsers() {
  if (!isAdmin.value) return
  loading.value = true
  error.value = ''
  try { users.value = (await api<{ users: AuthUser[] }>('/api/admin/users')).users }
  catch (reason) { error.value = reason instanceof Error ? reason.message : '账号列表读取失败' }
  finally { loading.value = false }
}

async function createUser() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  success.value = ''
  try {
    await api('/api/admin/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(createDraft),
    })
    success.value = `账号“${createDraft.username.trim()}”已创建。请通过安全渠道把初始密码交给使用者。`
    createDraft.username = ''
    createDraft.password = ''
    createDraft.role = 'user'
    await loadUsers()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '账号创建失败'
  } finally { busy.value = false }
}

async function updateUser(user: AuthUser, patch: Partial<Pick<AuthUser, 'active' | 'role'>>) {
  error.value = ''
  success.value = ''
  try {
    const result = await api<{ user: AuthUser }>(`/api/admin/users/${encodeURIComponent(user.id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ active: patch.active ?? user.active, role: patch.role ?? user.role }),
    })
    const index = users.value.findIndex((item) => item.id === user.id)
    if (index >= 0) users.value.splice(index, 1, result.user)
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '账号更新失败'
    await loadUsers()
  }
}

function changeRole(user: AuthUser, event: Event) {
  const role = (event.target as HTMLSelectElement).value
  if (role === 'admin' || role === 'user') void updateUser(user, { role })
}

function changeActive(user: AuthUser, event: Event) {
  void updateUser(user, { active: (event.target as HTMLInputElement).checked })
}

async function resetPassword(user: AuthUser) {
  const password = passwordDrafts[user.id] || ''
  error.value = ''
  success.value = ''
  try {
    await api(`/api/admin/users/${encodeURIComponent(user.id)}/password`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    })
    passwordDrafts[user.id] = ''
    success.value = `“${user.username}”的密码已重置，原有登录会话已失效。`
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '密码重置失败'
  }
}

async function submitPasswordChange() {
  error.value = ''
  success.value = ''
  passwordChanged.value = false
  if (changePassword.next !== changePassword.confirm) {
    error.value = '两次输入的新密码不一致。'
    return
  }
  try {
    await auth.changePassword(changePassword.current, changePassword.next)
    changePassword.current = ''
    changePassword.next = ''
    changePassword.confirm = ''
    passwordChanged.value = true
    success.value = '密码已修改，当前登录保持有效，其他会话已退出。'
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '密码修改失败'
  }
}

async function logout() {
  error.value = ''
  try { await auth.logout() }
  catch (reason) { error.value = reason instanceof Error ? reason.message : '退出失败，请重试。' }
}

onMounted(() => { void loadUsers() })
</script>

<template>
  <div class="account-backdrop" @click.self="emit('close')">
    <aside class="account-panel" role="dialog" aria-modal="true" aria-labelledby="account-title">
      <header class="account-head">
        <span class="account-mark"><AppIcon name="shield" :size="20" /></span>
        <div><h2 id="account-title">账号与权限</h2><p>{{ auth.user?.username }} · {{ isAdmin ? '管理员' : '普通用户' }}</p></div>
        <button class="icon-button close-button" aria-label="关闭账号面板" @click="emit('close')"><AppIcon name="close" :size="18" /></button>
      </header>
      <div class="account-body">
        <p v-if="error" class="account-message account-message--error" role="alert">{{ error }}</p>
        <p v-if="success" class="account-message account-message--success" role="status">{{ success }}</p>

        <section class="account-section">
          <h3>修改我的密码</h3>
          <p class="account-hint">新密码至少 12 位。修改后其他已登录设备会退出。</p>
          <form class="account-form" @submit.prevent="submitPasswordChange">
            <label><span>当前密码</span><input v-model="changePassword.current" type="password" autocomplete="current-password" required /></label>
            <label><span>新密码</span><input v-model="changePassword.next" type="password" autocomplete="new-password" minlength="12" required /></label>
            <label><span>再次输入新密码</span><input v-model="changePassword.confirm" type="password" autocomplete="new-password" minlength="12" required /></label>
            <button class="account-action" type="submit" :disabled="!changePassword.current || changePassword.next.length < 12">保存新密码</button>
          </form>
        </section>

        <section v-if="isAdmin" class="account-section">
          <div class="account-section__head"><h3>账号管理</h3><button class="account-refresh" :disabled="loading" @click="loadUsers"><AppIcon name="retry" :size="14" />刷新</button></div>
          <p class="account-hint">关闭注册。管理员创建的账号可登录问答；仅管理员能访问评测、项目证据及模型密钥配置。</p>
          <form class="account-create" @submit.prevent="createUser">
            <label><span>用户名</span><input v-model.trim="createDraft.username" minlength="3" maxlength="48" autocomplete="off" required placeholder="3–48 个字符" /></label>
            <label><span>初始密码</span><input v-model="createDraft.password" minlength="12" maxlength="256" autocomplete="new-password" required placeholder="至少 12 位" /></label>
            <label><span>角色</span><select v-model="createDraft.role"><option value="user">普通用户</option><option value="admin">管理员</option></select></label>
            <button class="account-action" type="submit" :disabled="busy || createDraft.username.length < 3 || createDraft.password.length < 12">{{ busy ? '正在创建…' : '创建账号' }}</button>
          </form>

          <div class="account-users" :aria-busy="loading">
            <p v-if="loading" class="account-hint">正在读取账号…</p>
            <article v-for="user in users" :key="user.id" class="account-user">
              <div class="account-user__name"><b>{{ user.username }}</b><small>{{ user.id === auth.user?.id ? '当前账号' : user.created_at.slice(0, 10) }}</small></div>
              <select :value="user.role" :disabled="user.id === auth.user?.id" :aria-label="`${user.username} 的角色`" @change="changeRole(user, $event)"><option value="user">普通用户</option><option value="admin">管理员</option></select>
              <label class="account-active"><input type="checkbox" :checked="user.active" :disabled="user.id === auth.user?.id" @change="changeActive(user, $event)" /><span>{{ user.active ? '启用' : '停用' }}</span></label>
              <div class="account-reset"><input v-model="passwordDrafts[user.id]" type="password" autocomplete="new-password" minlength="12" maxlength="256" :aria-label="`为 ${user.username} 设置新密码`" placeholder="重置密码（至少 12 位）" /><button :disabled="(passwordDrafts[user.id] || '').length < 12" @click="resetPassword(user)">重置</button></div>
            </article>
            <p v-if="!loading && !users.length" class="account-hint">尚无账号。</p>
          </div>
        </section>

        <section class="account-section account-section--logout">
          <p>退出后，本账号的会话记录仍保留在此浏览器；其他账号使用独立的本地记录空间。</p>
          <button class="account-logout" @click="logout"><AppIcon name="close" :size="15" />退出登录</button>
        </section>
      </div>
    </aside>
  </div>
</template>

<style scoped>
.account-backdrop { position: fixed; inset: 0; z-index: 240; display: flex; justify-content: flex-end; background: var(--scrim); backdrop-filter: blur(3px); }
.account-panel { display: flex; flex-direction: column; width: min(520px, 100%); height: 100%; overflow: hidden; border-left: 1px solid var(--border-subtle); background: var(--bg-elevated); box-shadow: var(--shadow-panel); }
.account-head { display: flex; flex: none; align-items: center; gap: 12px; padding: 20px 22px 16px; border-bottom: 1px solid var(--border-subtle); }
.account-mark { display: grid; width: 42px; height: 42px; flex: none; place-items: center; border-radius: 12px; color: var(--brand-primary); background: var(--brand-soft); }
.account-head > div { flex: 1; min-width: 0; }.account-head h2 { margin: 0; color: var(--text-heading); font-size: 19px; }.account-head p { margin: 3px 0 0; color: var(--text-secondary); font-size: 13px; }
.account-body { flex: 1; overflow-y: auto; padding: 18px 22px 26px; }.account-section + .account-section { margin-top: 23px; padding-top: 20px; border-top: 1px solid var(--border-subtle); }
.account-section h3 { margin: 0; color: var(--text-heading); font-size: 15px; }.account-hint { margin: 7px 0 12px; color: var(--text-secondary); font-size: 12.5px; line-height: 1.7; }
.account-message { margin: 0 0 13px; padding: 10px 12px; border: 1px solid var(--border-subtle); border-radius: 9px; font-size: 13px; line-height: 1.6; }.account-message--error { border-color: var(--state-error-border); color: var(--state-error); background: var(--state-error-soft); }.account-message--success { border-color: var(--state-success-border); color: var(--state-success); background: var(--state-success-soft); }
.account-form, .account-create { display: grid; gap: 10px; }.account-form label, .account-create label { display: grid; gap: 5px; color: var(--text-secondary); font-size: 12px; }.account-form input, .account-create input, .account-create select, .account-user select, .account-reset input { min-width: 0; padding: 8px 9px; border: 1px solid var(--border-subtle); border-radius: 8px; outline: 0; color: var(--text-primary); background: var(--bg-surface); font: inherit; font-size: 13px; }.account-form input:focus, .account-create input:focus, .account-create select:focus, .account-reset input:focus { border-color: var(--border-focus); box-shadow: 0 0 0 2px var(--brand-soft); }
.account-action, .account-logout { display: inline-flex; align-items: center; justify-content: center; gap: 6px; width: fit-content; padding: 8px 12px; border: 1px solid var(--border-brand); border-radius: 8px; color: var(--brand-primary); background: var(--brand-soft); font: inherit; font-size: 13px; font-weight: 650; cursor: pointer; }.account-action:disabled { opacity: .5; cursor: not-allowed; }.account-action:hover:not(:disabled) { background: var(--brand-soft-strong); }
.account-section__head { display: flex; align-items: center; justify-content: space-between; }.account-refresh { display: inline-flex; align-items: center; gap: 5px; padding: 5px 8px; border: 1px solid var(--border-subtle); border-radius: 7px; color: var(--text-secondary); background: var(--bg-surface); font-size: 12px; }.account-refresh:disabled { opacity: .5; }
.account-create { grid-template-columns: 1fr 1fr; gap: 9px; padding: 12px; border: 1px solid var(--border-subtle); border-radius: 11px; background: var(--bg-surface); }.account-create button { align-self: end; min-height: 34px; }.account-create label:last-of-type { grid-column: 1 / -1; }
.account-users { display: grid; gap: 8px; margin-top: 13px; }.account-user { display: grid; grid-template-columns: minmax(85px, 1fr) auto auto; gap: 8px; align-items: center; padding: 10px; border: 1px solid var(--border-subtle); border-radius: 10px; background: var(--bg-surface); }.account-user__name { display: grid; min-width: 0; gap: 3px; }.account-user__name b { overflow: hidden; color: var(--text-primary); font-size: 13px; text-overflow: ellipsis; }.account-user__name small { color: var(--text-muted); font-size: 11px; }.account-user select { max-width: 120px; padding: 6px 7px; font-size: 11px; }.account-user select:disabled, .account-active input:disabled { opacity: .6; }.account-active { display: inline-flex; align-items: center; gap: 4px; color: var(--text-secondary); font-size: 11px; }.account-active input { accent-color: var(--brand-primary); }.account-reset { display: flex; grid-column: 1 / -1; gap: 6px; }.account-reset input { flex: 1; }.account-reset button { padding: 6px 9px; border: 1px solid var(--border-brand); border-radius: 7px; color: var(--brand-primary); background: var(--brand-soft); font-size: 11px; }.account-reset button:disabled { opacity: .45; cursor: not-allowed; }
.account-section--logout { display: flex; align-items: center; justify-content: space-between; gap: 12px; }.account-section--logout p { margin: 0; color: var(--text-secondary); font-size: 12px; line-height: 1.6; }.account-logout { flex: none; border-color: var(--state-error-border); color: var(--state-error); background: var(--state-error-soft); }
@media (max-width: 520px) { .account-head { padding: 17px; }.account-body { padding: 16px 17px 22px; }.account-create { grid-template-columns: 1fr; }.account-create label:last-of-type { grid-column: auto; }.account-section--logout { align-items: flex-start; flex-direction: column; } }
</style>
