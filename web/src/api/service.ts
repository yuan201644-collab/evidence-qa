const API_BASE = import.meta.env.VITE_API_BASE ?? ''

/** 仅检查后端连通性，不代表数据库或模型服务已通过检查。 */
export async function checkService(): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/health`, { signal: AbortSignal.timeout(5000), cache: 'no-store' })
    if (!response.ok) return false
    const body = await response.json() as { status?: string }
    return body.status === 'ok'
  } catch {
    return false
  }
}
