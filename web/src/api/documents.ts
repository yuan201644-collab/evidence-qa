// 开发服务器的 public/documents 目录默认不携带语料。默认经由后端读取
// data/corpus/raw；只有显式设为 false 时才使用静态文件副本。
const USE_REMOTE_FILES = import.meta.env.VITE_REMOTE_DOCUMENTS !== 'false'
const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export function getDocumentFileUrl(docId: string, fileName?: string): string {
  const safeId = encodeURIComponent(docId)
  const extension = fileName?.slice(fileName.lastIndexOf('.')).toLowerCase()
  if (!USE_REMOTE_FILES && (!extension || extension === '.pdf')) {
    return `${import.meta.env.BASE_URL}documents/${safeId}.pdf`
  }
  return `${API_BASE}/api/documents/${safeId}/file`
}

export function getDocumentPreviewUrl(docId: string): string {
  return `${API_BASE}/api/documents/${encodeURIComponent(docId)}/preview`
}
