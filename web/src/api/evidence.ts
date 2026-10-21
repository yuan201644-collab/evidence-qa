const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export function getEvidenceUrl(path: string): string {
  const query = new URLSearchParams({ path })
  return `${API_BASE}/api/evidence/file?${query.toString()}`
}
