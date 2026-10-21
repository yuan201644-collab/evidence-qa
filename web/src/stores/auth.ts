import { defineStore } from 'pinia'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export type AuthUser = {
  id: string
  username: string
  role: 'admin' | 'user'
  active: boolean
  created_at: string
}

async function responseError(response: Response): Promise<string> {
  const body = await response.json().catch(() => null) as { detail?: string } | null
  if (response.status === 429) return body?.detail || '登录尝试过多，请稍后再试。'
  if (response.status === 401) return body?.detail || '用户名或密码不正确。'
  return body?.detail || `请求未完成（${response.status}）。`
}

export const useAuthStore = defineStore('auth', {
  state: () => ({ user: null as AuthUser | null, loading: false }),
  getters: {
    isAdmin: (state) => state.user?.role === 'admin',
  },
  actions: {
    async initialize() {
      this.loading = true
      try {
        const response = await fetch(`${API_BASE}/api/auth/me`, { cache: 'no-store' })
        if (response.ok) {
          const body = await response.json() as { user: AuthUser }
          this.user = body.user
        } else {
          this.user = null
        }
      } catch {
        this.user = null
      } finally {
        this.loading = false
      }
    },
    async login(username: string, password: string) {
      const response = await fetch(`${API_BASE}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })
      if (!response.ok) throw new Error(await responseError(response))
      const body = await response.json() as { user: AuthUser }
      this.user = body.user
    },
    async logout() {
      const response = await fetch(`${API_BASE}/api/auth/logout`, { method: 'POST' })
      if (!response.ok) throw new Error(await responseError(response))
      this.user = null
    },
    async changePassword(current_password: string, new_password: string) {
      const response = await fetch(`${API_BASE}/api/auth/password`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ current_password, new_password }),
      })
      if (!response.ok) throw new Error(await responseError(response))
    },
  },
})
