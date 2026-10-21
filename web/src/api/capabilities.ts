const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export interface CrossSourceDemoReadiness {
  status: 'ready' | 'disabled' | 'unavailable'
}

/**
 * Reports whether the isolated work-order/contract demonstration can run now.
 * A ready status is based on a read-only backend probe; it is not an evaluation result.
 */
export async function getCrossSourceDemoReadiness(): Promise<CrossSourceDemoReadiness> {
  const response = await fetch(`${API_BASE}/api/capabilities/cross-source-demo`, {
    signal: AbortSignal.timeout(4000),
    cache: 'no-store',
  })
  if (!response.ok) throw new Error(`跨源演示状态读取失败（${response.status}）`)
  const result = await response.json() as CrossSourceDemoReadiness
  if (!['ready', 'disabled', 'unavailable'].includes(result.status)) {
    throw new Error('跨源演示状态响应格式无效')
  }
  return result
}
