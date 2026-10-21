const API_BASE = import.meta.env.VITE_API_BASE ?? ''

/** 面板上的一个评测套件，对应 eval/cases 下的一份用例文件。 */
export interface EvalSuite {
  key: string
  title: string
  /** 这套用例对应评分表哪一项 —— 直接印在面板上，省得评委去对照文档。 */
  scoring_item: string
  case_file: string
  case_count: number
  judge: 'sql' | 'rag'
}

/** 标准答案里的一条期望引用。判「引用不合格」时靠它核对期望是哪份文档的哪一节。 */
export interface EvalGoldRef {
  doc: string
  anchor: string
}

/** 单题结果。判分理由、子条件、以及**标准答案**都要留着：
 *  只显示「对/错」的话，评委没法自己对一眼。 */
export interface EvalCaseResult {
  id: string
  question: string
  difficulty?: string | null
  elapsed_ms?: number
  elapsed_s?: number
  passed: boolean
  reason?: string
  answer?: string
  tools?: string[]
  sql?: string
  loose?: boolean
  /** 参考 SQL。判分不靠它 —— 判分比的是结果集，它只用来给人看。 */
  gold_sql?: string
  gold_rows_count?: number
  pred_rows_count?: number
  /** 结果集预览（最多前后各 5 行），单元格已转成短文本。 */
  gold_preview?: string[][]
  pred_preview?: string[][]
  /** 首处差异的人话描述，例如「第 1 行第 1 列：标准 = 59，实际 = 58」。 */
  first_diff?: string
  gold_error?: string
  pred_error?: string
  sub?: {
    points_hit?: number
    points_total?: number
    missed_points?: string[]
    hallucinated_entities?: string[]
    citation_ok?: boolean
    citation_basis?: string
  }
  /** RAG 类：本题全部判分要点（含已命中的），不只是缺的那几条。 */
  answer_points?: string[]
  forbidden_entities?: string[]
  gold_refs?: EvalGoldRef[]
}

export interface EvalSummary {
  run_id: string
  suite: string
  suite_title: string
  scoring_item: string
  total: number
  passed: number
  accuracy: number
  bands: { threshold: number; label: string; reached: boolean }[]
  by_difficulty: Record<string, { total: number; passed: number; accuracy: number }>
  latency: { p50_ms: number; max_ms: number }
  total_elapsed_ms: number
  generated_at: string
  scope: string
}

export interface EvalStreamHandlers {
  onCase?: (index: number, total: number, result: EvalCaseResult) => void
  onProgress?: (index: number, total: number) => void
  onDone?: (summary: EvalSummary) => void
  onError?: (message: string) => void
  /** 后端确认了这一轮被停止。与"本机停止接收"不同，这是真的停了。 */
  onCancelled?: (index: number, total: number) => void
}

/** 后端眼里当前有没有评测在跑 —— 界面据此判断按钮该不该可点。 */
export interface EvalSuitesPayload {
  suites: EvalSuite[]
  running: boolean
  current?: { run_id: string; suite: string; total: number; done: number } | null
}

export async function fetchEvalSuites(): Promise<EvalSuitesPayload> {
  const response = await fetch(`${API_BASE}/api/eval/suites`, { cache: 'no-store' })
  if (!response.ok) throw new Error(`读取评测套件失败：${response.status} ${response.statusText}`)
  return response.json() as Promise<EvalSuitesPayload>
}

/**
 * 请后端停下当前这一轮。**不等待它真的停** —— 线程要跑完手上那题才退出。
 *
 * 这是修「点了停止就再也开不了评测」的关键一步：之前点停止只 abort 了本机
 * 的 fetch，后端一无所知，线程继续跑完整个套件；而 `start()` 见到还在跑就
 * 一律 409。停止必须是双向的：本机不再接收 + 后端真的停下。
 *
 * 传 runId 是为了防止误停 —— 请求到达时上一轮可能刚好结束、下一轮已开始。
 */
export async function cancelEval(runId?: string): Promise<{ requested: boolean; running: boolean }> {
  const query = runId ? `?run_id=${encodeURIComponent(runId)}` : ''
  const response = await fetch(`${API_BASE}/api/eval/cancel${query}`, { method: 'POST' })
  if (!response.ok) throw new Error(`停止评测失败：${response.status} ${response.statusText}`)
  return response.json() as Promise<{ requested: boolean; running: boolean }>
}

/**
 * 开一次评测并消费 SSE 进度流。
 *
 * 手写解析器而不是用 EventSource：EventSource 只能发 GET 且无法带请求体 ——
 * `/api/eval/run` 用 query 传套件，本来 GET 也行，但这里刻意与 `/api/ask`
 * 保持同一套「按 \n\n 切帧、逐行剥 data: 前缀」的解析方式，
 * 前端只需维护一种流解析逻辑。
 */
export async function runEval(
  suite: string,
  handlers: EvalStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response
  try {
    response = await fetch(`${API_BASE}/api/eval/run?suite=${encodeURIComponent(suite)}`, {
      method: 'POST',
      signal,
    })
  } catch {
    throw new Error('无法连接评测服务，请确认后端已启动。')
  }
  if (!response.ok) {
    // 409 是「已有评测在跑」，body 里的 detail 是给人看的中文说明。
    const body = await response.json().catch(() => null) as { detail?: string } | null
    throw new Error(body?.detail || `评测未能开始：${response.status} ${response.statusText}`)
  }
  if (!response.body) throw new Error('评测服务未返回进度流。')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      // 帧之间以空行分隔；最后一段可能不完整，留在 buffer 里等下一块。
      const frames = buffer.split('\n\n')
      buffer = frames.pop() ?? ''
      for (const frame of frames) {
        const line = frame.split('\n').find((item) => item.startsWith('data:'))
        if (!line) continue
        let payload: { type: string; index?: number; total?: number; data?: unknown }
        try {
          payload = JSON.parse(line.slice(5).trim())
        } catch {
          continue
        }
        if (payload.type === 'case' && payload.data) {
          handlers.onCase?.(payload.index ?? 0, payload.total ?? 0, payload.data as EvalCaseResult)
        } else if (payload.type === 'progress') {
          handlers.onProgress?.(payload.index ?? 0, payload.total ?? 0)
        } else if (payload.type === 'done') {
          handlers.onDone?.(payload.data as EvalSummary)
        } else if (payload.type === 'cancelled') {
          handlers.onCancelled?.(payload.index ?? 0, payload.total ?? 0)
        } else if (payload.type === 'error') {
          handlers.onError?.(String(payload.data ?? '评测中断'))
        }
      }
    }
  } finally {
    reader.releaseLock()
  }
}
