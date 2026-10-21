import type { AskResponse, RefT } from '../types/contracts'

export type ChatMessage = {
  id: string
  role: 'user' | 'assistant' | 'error'
  content: string
  createdAt: string
  pending?: boolean
  failedQuestion?: string
  response?: AskResponse
}

export type Session = {
  id: string
  title: string
  updatedAt: string
  messages: ChatMessage[]
  pinned?: boolean
}

const maxMessages = 50
const fallbackMaxMessages = 20

function parse<T>(raw: string | null, fallback: T): T {
  try { return raw ? JSON.parse(raw) as T : fallback } catch { return fallback }
}

function sortSessions(sessions: Session[]) {
  return sessions.sort((left, right) => Number(Boolean(right.pinned)) - Number(Boolean(left.pinned)) || right.updatedAt.localeCompare(left.updatedAt))
}

export class LocalSessionStore {
  private scope = 'guest'
  private get sessionIdKey() { return `kb.session_id:${this.scope}` }
  private get sessionsKey() { return `kb.sessions:${this.scope}` }
  private get modelProfileKey() { return `verimind.modelProfileId:${this.scope}` }
  setScope(userId?: string) { this.scope = userId ? `user-${userId}` : 'guest' }
  getModelProfileId() { return localStorage.getItem(this.modelProfileKey) }
  setModelProfileId(id: string) { localStorage.setItem(this.modelProfileKey, id) }
  clearModelProfileId() { localStorage.removeItem(this.modelProfileKey) }
  getActiveId() { return localStorage.getItem(this.sessionIdKey) }
  setActiveId(id: string) { localStorage.setItem(this.sessionIdKey, id) }
  list() { return parse<Session[]>(localStorage.getItem(this.sessionsKey), []) }
  save(session: Session): boolean {
    const sessions = this.list().filter((item) => item.id !== session.id)
    sessions.unshift(session)
    const write = (limit: number) => {
      const compact = sortSessions(sessions).map((item) => ({
        ...item,
        messages: item.messages.slice(-limit).map((message) => {
          // Drop snapshots written by older builds; progress is only live request state.
          const stored = { ...message } as ChatMessage & { progress?: unknown }
          delete stored.progress
          return stored
        }),
      }))
      localStorage.setItem(this.sessionsKey, JSON.stringify(compact))
    }
    try {
      write(maxMessages)
      return true
    } catch {
      try {
        write(fallbackMaxMessages)
        return true
      } catch {
        return false
      }
    }
  }
  remove(id: string): boolean {
    return this.removeMany([id])
  }
  removeMany(ids: string[]): boolean {
    try {
      const removed = new Set(ids)
      const sessions = this.list().filter((item) => !removed.has(item.id))
      localStorage.setItem(this.sessionsKey, JSON.stringify(sessions))
      return true
    } catch {
      return false
    }
  }
  clear(): boolean {
    try {
      localStorage.removeItem(this.sessionsKey)
      localStorage.removeItem(this.sessionIdKey)
      localStorage.removeItem(this.modelProfileKey)
      return true
    } catch {
      return false
    }
  }
}

export const localSessions = new LocalSessionStore()

export function refTitle(ref: RefT) {
  return ref.kind === 'doc' ? (ref.doc_title || '文档证据') : `${ref.table}${ref.column ? ` · ${ref.column}` : ''}`
}
