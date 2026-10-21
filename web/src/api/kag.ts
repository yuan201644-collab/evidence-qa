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
