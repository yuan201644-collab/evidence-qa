const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export type EditableModelProfile = {
  id: string
  label: string
  provider: string
  model: string
  base_url: string
  configured: boolean
  has_api_key: boolean
}

export type ModelProfileDraft = {
  model: string
  base_url: string
  api_key: string
}

async function responseError(response: Response): Promise<Error> {
  try {
    const body = await response.json() as { detail?: string }
    return new Error(body.detail || `模型配置请求失败（${response.status}）`)
  } catch {
    return new Error(`模型配置请求失败（${response.status}）`)
  }
}

export async function getEditableModelProfiles(signal?: AbortSignal): Promise<EditableModelProfile[]> {
  const response = await fetch(`${API_BASE}/api/model-config`, {
    headers: { Accept: 'application/json' },
    cache: 'no-store',
    signal,
  })
  if (!response.ok) throw await responseError(response)
  const body = await response.json() as { profiles: EditableModelProfile[] }
  return body.profiles
}

export async function saveEditableModelProfile(
  profileId: string,
  config: ModelProfileDraft,
): Promise<EditableModelProfile> {
  const response = await fetch(`${API_BASE}/api/model-config/${encodeURIComponent(profileId)}`, {
    method: 'PUT',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  })
  if (!response.ok) throw await responseError(response)
  const body = await response.json() as { profile: EditableModelProfile }
  return body.profile
}
