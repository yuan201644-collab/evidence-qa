import type { ModelProfileOption } from '../types/contracts'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export type ModelCatalog = {
  default_model_id: string
  models: ModelProfileOption[]
}

export async function getModelCatalog(signal?: AbortSignal): Promise<ModelCatalog> {
  const response = await fetch(`${API_BASE}/api/models`, {
    headers: { Accept: 'application/json' },
    cache: 'no-store',
    signal,
  })
  if (!response.ok) throw new Error(`模型配置读取失败（${response.status}）`)
  return response.json() as Promise<ModelCatalog>
}
