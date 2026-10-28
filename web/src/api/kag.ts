const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export type KagReadiness = {
  ready: boolean
  status: 'ready' | 'not_configured' | 'unavailable'
  reason_code: string | null
}

/** Read readiness only; the browser never receives KAG credentials or runtime paths. */
export async function getKagReadiness(): Promise<KagReadiness> {
  const response = await fetch(`${API_BASE}/api/kag/status`, {
    signal: AbortSignal.timeout(5000),
    cache: 'no-store',
  })
  if (!response.ok) throw new Error(`KAG 状态读取失败（${response.status}）`)
  return await response.json() as KagReadiness
}

export type KagGraphStats = {
  measured_at: string
  database: string
  source_edges: number
  business_edges: number
  relation_types: number
  note: string
}

export type RemoteKagStatus = {
  base: string
  app_id: number
  /** 后端当前选用的回答链路；`kag` 表示直连实验服务器上的 KAG。 */
  backend: string
  reachable: boolean
  status: 'ready' | 'unavailable'
  reason_code: string | null
  graph: KagGraphStats
}

/**
 * 实验服务器上那套 KAG 的可达性。
 *
 * 与 `getKagReadiness()` 的区别：那个读的是老项目的本地 runtime 子进程
 * （`127.0.0.1:8887`，需要本机装 Python 运行时），本项目是直接连实验服务器。
 * 两者都返回 ready/unavailable，但探测对象不同，不能互相替代。
 */
export async function getRemoteKagStatus(): Promise<RemoteKagStatus> {
  const response = await fetch(`${API_BASE}/api/kag/remote`, {
    signal: AbortSignal.timeout(6000),
    cache: 'no-store',
  })
  if (!response.ok) throw new Error(`KAG 服务状态读取失败（${response.status}）`)
  return await response.json() as RemoteKagStatus
}

export type FactTableRow = {
  fact_id: string
  label: string | null
  criterion: string
  fact: string
  value: string
  status: 'supported' | 'insufficient'
}

export type FactTableResult = {
  question: string
  rows: FactTableRow[]
  table: string
}

/** 离线重算一道题的事实表：不调模型、不消耗算力，用来单独讲解判定过程。 */
export async function getFactTable(question: string): Promise<FactTableResult> {
  const response = await fetch(`${API_BASE}/api/kag/fact-table`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
    signal: AbortSignal.timeout(15000),
  })
  if (!response.ok) throw new Error(`事实表生成失败（${response.status}）`)
  return await response.json() as FactTableResult
}

export type FrozenQuestion = { qid: string; text: string; layer?: string }

/** 冻结题库清单；事实表面板用它提供「换个问题看看」。 */
export async function getFrozenQuestions(): Promise<FrozenQuestion[]> {
  const response = await fetch(`${API_BASE}/api/kag/questions`, {
    signal: AbortSignal.timeout(8000),
    cache: 'no-store',
  })
  if (!response.ok) throw new Error(`题库读取失败（${response.status}）`)
  const data = await response.json() as { questions?: FrozenQuestion[] }
  return data.questions ?? []
}
