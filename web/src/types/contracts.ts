/**
 * Mirror of app/contracts.py. Keep this file aligned with the backend models.
 *
 * DocRef bboxes are PyMuPDF text-layer rectangles in points: their origin is
 * the top-left of the page, y grows downward, and [x0, y0, x1, y1] means
 * top-left to bottom-right. They are geometric evidence; char offsets are not.
 */

export type Bbox = [number, number, number, number]

/** app/contracts.py: SqlRef */
export type SqlRef = {
  kind: 'sql'
  snippet: string
  table: string
  column: string | null
  row_pk: string | null
  /** Query-result evidence identity; this is not a database row primary key. */
  result_id?: string | null
}

/** app/contracts.py: DocRef */
export type DocRef = {
  kind: 'doc'
  snippet: string
  doc_id: string
  doc_title: string | null
  page: number | null
  section_path: string | null
  char_start: number | null
  char_end: number | null
  bboxes: Bbox[]
  page_width: number | null
  page_height: number | null
  extraction_method?: 'ocr' | 'text_layer' | 'plain' | null
}

/** app/contracts.py: RefT */
export type RefT = SqlRef | DocRef

/** app/contracts.py: TraceStep. hit is the actual list of source refs, not a boolean. */
export type TraceStep = {
  step_id: number
  tool: string
  input: Record<string, unknown>
  output: Record<string, unknown>
  elapsed_ms: number
  hit: RefT[] | null
}

/** app/contracts.py: AskRequest */
export type AskRequest = {
  question: string
  session_id: string
  retrieval_mode?: 'standard' | 'deep'
}

/** Safe public model metadata; API credentials are never part of this type. */
export type ModelProfileOption = {
  id: string
  label: string
  provider: string
  model: string
  configured: boolean
  selected: boolean
}

/** app/contracts.py: ResultSetInfo */
export type ResultSetInfo = {
  id: string
  row_count: number
  complete: boolean
  page_size: number
  expires_at: string
}

/** app/contracts.py: ResultSetPage */
export type ResultSetPage = {
  id: string
  columns: string[]
  rows: Record<string, unknown>[]
  offset: number
  page_size: number
  row_count: number
  complete: boolean
  next_offset: number | null
  expires_at: string
}

/** app/contracts.py: Suggestion —— 一条可点的追问建议 */
export type Suggestion = {
  question: string
  reason: string
}

/**
 * app/contracts.py: DataSource —— 本轮答案实际读的是哪一份数据。
 *
 * 两种来源都要标：database = 哪个库（问数链路），corpus = 哪一批文档（问答链路）。
 * 刻意不含主机/账号/连接串 —— 那是运维信息，不该出现在面向业务用户的界面上。
 * database/dialect 在 corpus 来源下为 null，反之 document_count/formats 在
 * database 来源下为空 —— 所以 kind 判在前，不要先读字段再判空。
 */
export type DataSource = {
  kind: 'database' | 'corpus'
  database?: string | null
  dialect?: string | null
  label: string
  document_count?: number | null
  formats?: string[]
}

/**
 * app/contracts.py: ContextContinuation —— 本轮是否接续了上一轮问句。
 *
 * 只在这个字段非 null 时表示「这一轮是接着上一轮的」。
 * `previous_question` 是**累积文本**（可能已含更早轮次的补充），
 * 所以显示时不能当成"上一句的原文"理解 —— 它是到上一轮为止的完整上下文。
 */
export type ContextContinuation = {
  previous_question: string
  round_index: number
}

export type FactReviewCheck = {
  code: string
  result: 'passed' | 'failed' | 'unknown' | 'not_run'
  /** 可读的结论说明；仅在该结论需要解释时提供（如哪条查询条件没落实）。 */
  detail?: string | null
}

export type FactReviewItem = {
  fact_id: string
  label: string
  summary?: string | null
  status: 'candidate' | 'supported' | 'contradicted' | 'insufficient' | 'ambiguous'
  execution_status: 'completed' | 'not_run' | 'failed'
  evidence_link_status: 'complete' | 'partial' | 'unavailable'
  checks: FactReviewCheck[]
  evidence_ref_indices: number[]
  reason_code?: string | null
  reason?: string | null
}

export type FactReview = {
  schema_version: '2'
  coverage: 'structured_query' | 'document_qa' | 'cross_source_work_order_sla' | 'cross_source_unplanned'
  verification_mode: 'strict' | 'ablation' | 'source_specific'
  verification_scope: 'query_execution_and_evidence' | 'document_retrieval_and_citation' | 'fact_dependencies_and_business_checks' | 'cross_source_execution_only'
  scope_note: string
  items: FactReviewItem[]
}

/** app/contracts.py: AskResponse */
export type AskResponse = {
  answer: string
  trace: TraceStep[]
  refs: RefT[]
  sql: string | null
  need_clarify: boolean
  result_set?: ResultSetInfo | null
  suggestions?: Suggestion[]
  data_source?: DataSource | null
  context_continuation?: ContextContinuation | null
  fact_review?: FactReview | null
}

/** Interactive-only event; deliberately separate from the persisted answer contract. */
export type AskProgressStage = {
  id: string
  stage_id: string
  label: string
  status: 'started' | 'completed' | 'blocked' | 'skipped' | 'failed'
  elapsed_ms?: number
}
