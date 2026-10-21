import { defineStore } from 'pinia'
import { askWithProgress } from '../api/ask'
import type { AskProgressStage, AskResponse, RefT } from '../types/contracts'
import { localSessions, type ChatMessage, type Session } from './persistence'

/**
 * 生成会话/消息 id。
 *
 * 不能用裸的 `crypto.randomUUID()`：它只在**安全上下文**里存在，而 `127.0.0.1`
 * 与 `localhost` 被浏览器算作安全，换成校园网 IP + HTTP 就不是了。那时
 * `crypto.randomUUID` 是 undefined，点提问会在建会话那一步直接抛
 * `TypeError: crypto.randomUUID is not a function`，请求根本发不出去。
 * 页面能打开只是因为渲染不依赖它，所以这个坑只在局域网演示时才会暴露。
 *
 * 降级用 getRandomValues：同样来自浏览器密码学强度随机源，不用 Date.now
 * 兜底（同毫秒创建多个会话时会撞 id）。
 */
function uuidFallback(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40 // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80 // variant 10
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}
const makeId = () => crypto.randomUUID?.() ?? uuidFallback()
const now = () => new Date().toISOString()
const DEFAULT_MODEL_PROFILE_ID = 'deepseek-v4-1-flash'
function storedModelProfileId(): string {
  try { return localSessions.getModelProfileId() ?? DEFAULT_MODEL_PROFILE_ID }
  catch { return DEFAULT_MODEL_PROFILE_ID }
}
// 控制器不进入响应式状态或浏览器存储；每个会话独立停止等待。
const activeRequests = new Map<string, AbortController>()

export const useConversationStore = defineStore('conversation', {
  state: () => ({
    sessions: [] as Session[],
    activeSessionId: '',
    activeRefIndex: 0,
    activeMessageId: '',
    persistFailed: false,
    selectedModelProfileId: storedModelProfileId(),
    retrievalMode: 'standard' as 'standard' | 'deep',
    // 请求进度是临时界面状态，不进入 ChatMessage，也不会写入本地会话。
    progressByMessage: {} as Record<string, AskProgressStage[]>,
  }),
  getters: {
    activeSession(state) { return state.sessions.find((session) => session.id === state.activeSessionId) },
    messages(): ChatMessage[] { return this.activeSession?.messages ?? [] },
    activeResponse(): AskResponse | undefined {
      const message = this.messages.find((item) => item.id === this.activeMessageId)
      return message?.response
    },
    activeRefs(): RefT[] { return this.activeResponse?.refs ?? [] },
    activeRef(): RefT | undefined { return this.activeRefs[this.activeRefIndex] },
    // 一次提交总会在末尾追加唯一的 pending 消息。较早的 pending 只能是
    // 热更新或旧版本地缓存留下的孤儿消息，不能永久锁住新的提问。
    isPending(): boolean { return this.messages.at(-1)?.pending === true },
  },
  actions: {
    setModelProfile(id: string) {
      this.selectedModelProfileId = id
      try { localSessions.setModelProfileId(id) } catch { /* Keep in-memory selection. */ }
    },
    setRetrievalMode(mode: 'standard' | 'deep') {
      this.retrievalMode = mode
    },
    hydrate() {
      this.selectedModelProfileId = storedModelProfileId()
      this.sessions = localSessions.list()
      for (const session of this.sessions) {
        for (const message of session.messages) {
          if (!message.pending) continue
          if (message.response) {
            message.pending = false
          } else {
            message.role = 'error'
            message.content = '上次请求未完成，请重试。'
            message.failedQuestion = session.messages
              .slice(0, session.messages.indexOf(message))
              .reverse()
              .find((item) => item.role === 'user')?.content
            message.pending = false
          }
        }
      }
      this.activeSessionId = localSessions.getActiveId() || this.sessions[0]?.id || ''
      if (!this.activeSessionId) this.newSession()
      else this.persist(this.activeSession)
    },
    persist(target?: Session) {
      const targetSession = target ?? this.activeSession
      if (targetSession) {
        this.persistFailed = !localSessions.save(targetSession)
        this.sessions.sort((left, right) => Number(Boolean(right.pinned)) - Number(Boolean(left.pinned)) || right.updatedAt.localeCompare(left.updatedAt))
      }
      try {
        localSessions.setActiveId(this.activeSessionId)
      } catch {
        this.persistFailed = true
      }
    },
    newSession() {
      const id = makeId()
      const session: Session = { id, title: '新建问答', updatedAt: now(), messages: [] }
      this.sessions.unshift(session)
      this.activeSessionId = id
      this.activeMessageId = ''
      this.activeRefIndex = 0
      this.persist()
    },
    switchSession(id: string) {
      this.activeSessionId = id
      this.activeMessageId = ''
      this.activeRefIndex = 0
      this.persist()
    },
    deleteSession(id: string) {
      this.deleteSessions([id])
    },
    deleteSessions(ids: string[]) {
      const removed = new Set(ids)
      if (!this.sessions.some((session) => removed.has(session.id))) return
      if (!localSessions.removeMany(ids)) {
        this.persistFailed = true
        return
      }
      const deletingActive = removed.has(this.activeSessionId)
      this.sessions = this.sessions.filter((session) => !removed.has(session.id))
      if (!deletingActive) return
      this.activeSessionId = this.sessions[0]?.id ?? ''
      this.activeMessageId = ''
      this.activeRefIndex = 0
      if (!this.activeSessionId) {
        this.newSession()
        return
      }
      this.persist()
    },
    clearAllLocalData(): boolean {
      for (const controller of activeRequests.values()) controller.abort()
      activeRequests.clear()
      if (!localSessions.clear()) {
        this.persistFailed = true
        return false
      }
      this.sessions = []
      this.activeSessionId = ''
      this.activeMessageId = ''
      this.activeRefIndex = 0
      this.progressByMessage = {}
      this.persistFailed = false
      this.selectedModelProfileId = DEFAULT_MODEL_PROFILE_ID
      this.newSession()
      return true
    },
    discardInMemory() {
      for (const controller of activeRequests.values()) controller.abort()
      activeRequests.clear()
      this.sessions = []
      this.activeSessionId = ''
      this.activeMessageId = ''
      this.activeRefIndex = 0
      this.progressByMessage = {}
      this.persistFailed = false
      this.selectedModelProfileId = DEFAULT_MODEL_PROFILE_ID
    },
    renameSession(id: string, title: string) {
      const session = this.sessions.find((item) => item.id === id)
      const cleaned = title.trim()
      if (!session || !cleaned) return
      session.title = cleaned.slice(0, 48)
      session.updatedAt = now()
      this.persist(session)
    },
    toggleSessionPin(id: string) {
      const session = this.sessions.find((item) => item.id === id)
      if (!session) return
      session.pinned = !session.pinned
      this.persist(session)
    },
    selectRef(messageId: string, index: number) {
      this.activeMessageId = messageId
      this.activeRefIndex = index
    },
    async submit(question: string) {
      const cleaned = question.trim()
      const session = this.activeSession
      if (session) {
        for (const message of session.messages.slice(0, -1)) {
          if (!message.pending) continue
          message.role = 'error'
          message.content = '上次请求未完成，请重试。'
          message.failedQuestion = session.messages
            .slice(0, session.messages.indexOf(message))
            .reverse()
            .find((item) => item.role === 'user')?.content
          message.pending = false
        }
      }
      if (!cleaned || !session || this.isPending) return
      const user: ChatMessage = { id: makeId(), role: 'user', content: cleaned, createdAt: now() }
      const pendingMessage: ChatMessage = { id: makeId(), role: 'assistant', content: '', createdAt: now(), pending: true }
      session.messages.push(user, pendingMessage)
      // 必须更新存入会话后的响应式对象；修改原始对象不会通知 isPending，
      // 会出现答案已显示而输入区仍被锁定的状态。
      const pending = session.messages[session.messages.length - 1]!
      this.progressByMessage[pending.id] = []
      session.title = cleaned.slice(0, 24)
      session.updatedAt = now()
      this.persist(session)
      const controller = new AbortController()
      activeRequests.set(session.id, controller)
      try {
        const response = await askWithProgress(
          {
            question: cleaned,
            session_id: session.id,
            ...(this.retrievalMode === 'deep' ? { retrieval_mode: 'deep' as const } : {}),
          },
          (stage) => {
            const stages = this.progressByMessage[pending.id] ?? []
            const existing = stages.findIndex((item) => item.id === stage.id)
            if (existing >= 0) stages.splice(existing, 1, stage)
            else stages.push(stage)
            this.progressByMessage[pending.id] = [...stages]
          },
          controller.signal,
          this.selectedModelProfileId,
        )
        Object.assign(pending, { content: response.answer, response, pending: false })
        if (this.activeSessionId === session.id) {
          this.activeMessageId = pending.id
          this.activeRefIndex = 0
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : '网络异常，请稍后重试。'
        Object.assign(pending, { role: 'error', content: message, pending: false, failedQuestion: cleaned })
      } finally {
        activeRequests.delete(session.id)
        // 成功时**保留** progressByMessage：答案气泡要把它收成一行摘要并支持点开回看
        // （「已完成处理过程 · N 个环节 · 用时 Xs」）。以前这里无条件 delete，
        // 结果过程区块刚建好就没了数据，只能整块消失 —— 这正是「突然消失」的来源。
        // 失败时删掉：error 气泡不展示处理过程，留着只会越积越多。
        if (pending.role === 'error') delete this.progressByMessage[pending.id]
        if (!this.sessions.includes(session)) return
        session.updatedAt = now()
        this.persist(session)
      }
    },
    retry(question: string) { return this.submit(question) },
    stopWaiting() { activeRequests.get(this.activeSessionId)?.abort() },
  },
})
