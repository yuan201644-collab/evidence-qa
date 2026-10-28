<script setup lang="ts">
import { ref } from 'vue'
import { useAuthStore } from '../stores/auth'

const emit = defineEmits<{ success: [] }>()
const auth = useAuthStore()
const username = ref('')
const password = ref('')
const visible = ref(false)
const submitting = ref(false)
const error = ref('')

async function submit() {
  if (submitting.value) return
  submitting.value = true
  error.value = ''
  try {
    await auth.login(username.value.trim(), password.value)
    password.value = ''
    emit('success')
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '登录失败，请稍后再试。'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <main class="auth-page">
    <section class="auth-card" aria-labelledby="auth-title">
      <img class="auth-card__mark" src="/brand/evidence-qa-mark.svg?v=20261028" alt="循证问答" />
      <p class="auth-card__eyebrow">SECURE WORKSPACE</p>
      <h1 id="auth-title">登录循证问答</h1>
      <p class="auth-card__subtitle">使用管理员分配的账号进入问答工作台。</p>
      <form class="auth-form" @submit.prevent="submit">
        <label class="auth-field">
          <span>用户名</span>
          <input v-model="username" name="username" autocomplete="username" required minlength="3" maxlength="48" autofocus />
        </label>
        <label class="auth-field">
          <span>密码</span>
          <div class="auth-password">
            <input v-model="password" name="password" :type="visible ? 'text' : 'password'" autocomplete="current-password" required maxlength="256" />
            <button type="button" :aria-label="visible ? '隐藏密码' : '显示密码'" @click="visible = !visible">{{ visible ? '隐藏' : '显示' }}</button>
          </div>
        </label>
        <p v-if="error" class="auth-error" role="alert">{{ error }}</p>
        <button class="auth-submit" type="submit" :disabled="submitting || !username || !password">
          {{ submitting ? '正在验证…' : '安全登录' }}
        </button>
      </form>
      <p class="auth-card__foot">账号由系统管理员创建 · 登录会话有效期 12 小时</p>
    </section>
  </main>
</template>

<style scoped>
.auth-page { display: grid; min-height: 100dvh; place-items: center; padding: 24px; background: radial-gradient(ellipse at 50% 0%, var(--brand-soft), transparent 45%), var(--bg-app); }
.auth-card { width: min(420px, 100%); padding: 38px; border: 1px solid var(--border-subtle); border-radius: 22px; background: var(--bg-elevated); box-shadow: var(--shadow-panel); }
.auth-card__mark { display: block; width: 54px; height: 54px; margin: 0 auto 22px; }
.auth-card__eyebrow { margin: 0 0 8px; color: var(--brand-primary); font-size: 11px; font-weight: 750; letter-spacing: .18em; text-align: center; }
.auth-card h1 { margin: 0; color: var(--text-heading); font-size: 26px; text-align: center; }
.auth-card__subtitle { margin: 9px 0 27px; color: var(--text-secondary); font-size: 14px; text-align: center; }
.auth-form { display: grid; gap: 17px; }
.auth-field { display: grid; gap: 7px; color: var(--text-secondary); font-size: 13px; font-weight: 650; }
.auth-field input { width: 100%; min-width: 0; padding: 12px 13px; border: 1px solid var(--border-subtle); border-radius: 10px; outline: 0; color: var(--text-primary); background: var(--bg-surface); font: inherit; font-size: 15px; }
.auth-field input:focus { border-color: var(--border-focus); box-shadow: 0 0 0 3px var(--brand-soft); }
.auth-password { display: flex; align-items: center; gap: 5px; padding-right: 9px; border: 1px solid var(--border-subtle); border-radius: 10px; background: var(--bg-surface); }
.auth-password:focus-within { border-color: var(--border-focus); box-shadow: 0 0 0 3px var(--brand-soft); }
.auth-password input { border: 0; box-shadow: none !important; background: transparent; }
.auth-password button { flex: none; padding: 5px; color: var(--text-muted); background: transparent; font: inherit; font-size: 12px; }
.auth-error { margin: -3px 0 0; color: var(--state-error); font-size: 13px; }
.auth-submit { min-height: 44px; border: 0; border-radius: 10px; color: #fff; background: var(--brand-primary); font: inherit; font-size: 15px; font-weight: 700; cursor: pointer; transition: transform .16s ease, filter .16s ease; }
.auth-submit:hover:not(:disabled) { filter: brightness(1.08); transform: translateY(-1px); }
.auth-submit:disabled { opacity: .55; cursor: wait; }
.auth-card__foot { margin: 22px 0 0; color: var(--text-muted); font-size: 12px; text-align: center; }
@media (max-width: 520px) { .auth-card { padding: 30px 24px; border-radius: 18px; } }
@media (prefers-reduced-motion: reduce) { .auth-submit { transition: none; } }
</style>
