const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export type DatabaseStatusKind = 'connected' | 'unavailable' | 'not_used' | 'catalog_mismatch'

export interface DatabaseStatus {
  status: DatabaseStatusKind
  database: string | null
  catalog_database: string | null
  label: string
  dialect: string
  table_count: number
  checked_at: string
}

export interface DatabaseColumn {
  name: string
  type: string
  nullable: boolean
  is_primary_key: boolean
  description: string
  foreign_key_to: string | null
}

export interface DatabaseTable {
  schema: string
  name: string
  description: string
  column_count: number
  columns: DatabaseColumn[]
}

export interface DatabaseSchema {
  database: string | null
  label: string
  dialect: string
  contract_version: string
  table_count: number
  tables: DatabaseTable[]
}

export type UnstructuredStatusKind = 'connected' | 'not_initialized' | 'memory' | 'not_used' | 'unavailable'

export interface UnstructuredDocument {
  doc_id: string
  title: string
  extension: string
  indexed_chunk_count: number | null
}

export interface UnstructuredDatabase {
  status: UnstructuredStatusKind
  backend: 'pgvector' | 'memory' | 'mock'
  label: string
  database: string | null
  schema: string | null
  corpus_document_count: number
  indexed_document_count: number | null
  chunk_count: number | null
  vector_extension_ready: boolean | null
  warning: string | null
  checked_at: string
  tables: DatabaseTable[]
  documents: UnstructuredDocument[]
}

export interface UnstructuredDatabaseStatus {
  status: 'connected' | 'unavailable' | 'memory' | 'not_used'
  backend: 'pgvector' | 'memory' | 'mock'
  label: string
  checked_at: string
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    signal: AbortSignal.timeout(5000),
    cache: 'no-store',
  })
  if (!response.ok) throw new Error(`数据库信息读取失败（${response.status}）`)
  return await response.json() as T
}

export function getDatabaseStatus(): Promise<DatabaseStatus> {
  return get<DatabaseStatus>('/api/database/status')
}

export function getDatabaseSchema(): Promise<DatabaseSchema> {
  return get<DatabaseSchema>('/api/database/schema')
}

export function getUnstructuredDatabase(): Promise<UnstructuredDatabase> {
  return get<UnstructuredDatabase>('/api/database/unstructured')
}

export function getUnstructuredDatabaseStatus(): Promise<UnstructuredDatabaseStatus> {
  return get<UnstructuredDatabaseStatus>('/api/database/unstructured/status')
}
