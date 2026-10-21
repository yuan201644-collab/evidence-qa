import type { AskProgressStage, AskRequest, AskResponse, ResultSetPage } from '../types/contracts'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''
const ASK_TIMEOUT_MS = 180_000

function responseError(status: number): string {
  if (status === 429) return '服务当前较忙，请稍后重新尝试。'
  if (status === 502 || status === 503 || status === 504) return '问答服务暂时无法响应，请稍后重试。'
  if (status >= 500) return '本次回答未能完成。请重试；若持续失败，请检查后端与模型服务。'
  if (status === 401) return '登录状态已失效，请刷新页面后重新登录。'
  if (status === 403) return '当前账号无权执行此操作。'
  if (status === 422 || status === 400) return '问题未能提交，请检查输入后重试。'
  return `请求未完成（${status}），请重试。`
}

function isSuccessfulTerminalStep(step: AskResponse['trace'][number]): boolean {
  const output = step.output ?? {}
  if (typeof output.error === 'string' && output.error.trim()) return false
  if (typeof output.row_count === 'number') return true
  if (step.tool === 'hybrid_retrieve' && typeof output.num_hits === 'number') return true
  if (step.tool === 'llm_generate' && typeof output.answer_len === 'number') return true
  return false
}

function finalSuccessfulTools(response: AskResponse): Set<string> {
  const successful = new Set<string>()
  const review = response.fact_review
  if (!review) return successful
  if (review.verification_mode === 'source_specific') {
    for (const item of review.items) {
      const check = (code: string) => item.checks.find((entry) => entry.code === code)?.result
      if ((item.fact_id === 'structured_query_target' || item.fact_id === 'structured_source_fact')
        && check('query_execution') === 'passed') successful.add('nl2sql')
      if ((item.fact_id === 'document_answer_target' || item.fact_id === 'document_source_fact')
        && check('answer_generation') === 'passed') successful.add('doc_search')
    }
  } else if (review.coverage === 'cross_source_work_order_sla'
    && review.items.every((item) => item.execution_status !== 'failed')) {
    successful.add('cross_source_demo')
  }
  return successful
}

function hasProjectionTerminalFailure(response: AskResponse): boolean {
  const review = response.fact_review
  if (!review || response.need_clarify) return false
  if (review.verification_mode === 'source_specific') {
    for (const item of review.items) {
      if (item.fact_id === 'cross_source_relation') continue
      if (item.execution_status === 'failed' || item.execution_status === 'not_run') return true
      if (['structured_query_failed', 'query_execution_missing', 'document_generation_missing'].includes(item.reason_code ?? '')) return true
      const failedExecution = item.checks.some((check) =>
        (check.code === 'query_execution' && check.result === 'failed')
        || (check.code === 'readonly_sql_validation' && check.result === 'failed'),
      )
      const missingGeneration = item.checks.some((check) =>
        check.code === 'answer_generation' && check.result === 'not_run',
      )
      if (failedExecution || missingGeneration) return true
    }
    return false
  }
  return review.items.some((item) => item.execution_status === 'failed')
}

function hasUnresolvedToolFailure(response: AskResponse): boolean {
  if (response.need_clarify || hasProjectionTerminalFailure(response)) return !response.need_clarify
  const trace = response.trace ?? []
  const successfulTools = finalSuccessfulTools(response)
  return trace.some((step, index) => {
    const error = step.output?.error
    if (typeof error !== 'string' || !error.trim() || successfulTools.has(step.tool)) return false
    const isFailedExpansion = step.tool === 'nl2sql'
      && (step.output?.purpose === 'bounded_result_materialization'
        || step.input?.purpose === 'bounded_result_materialization')
    const hasSuccessfulCappedQueryBeforeIt = trace.slice(0, index).some((previous) =>
      previous.tool === 'nl2sql'
      && typeof previous.output?.row_count === 'number'
      && previous.output?.error == null
      && previous.output?.possible_truncation === true,
    )
    if (isFailedExpansion && hasSuccessfulCappedQueryBeforeIt) return false
    return !trace.slice(index + 1).some((later) => later.tool === step.tool && isSuccessfulTerminalStep(later))
  })
}

/**
 * 这轮失败是否已被核验卡**具体解释**过。
 *
 * 背景：S2 起，`2023年有多少客户？` 这类问题会同时给出两条结论 ——
 * 「数据库执行：失败」与「查询条件落实：失败 · 客户表没有可用的时间依据」。
 * 后者才是用户真正需要知道的事（条件为什么落不了地）。
 *
 * 而 `hasProjectionTerminalFailure` 见到 `query_execution: failed` 就整轮
 * 判定失败，前端于是抛「本次请求未完成」并**丢弃整张核验卡** —— 用户只看到
 * 一句笼统报错，看不到具体原因。这不是安全保护，是把已经算出来的解释扔掉。
 *
 * 判据刻意收得很紧：只有当卡片里确实存在一条**说明性**的失败结论
 * （带 detail 的 `sql_requirement_coverage`）时才让卡片显示出来；
 * 单纯"执行失败但没说为什么"仍然按原逻辑报错。
 */
function isFailureExplainedByReview(response: AskResponse): boolean {
  const review = response.fact_review
  if (!review) return false
  return review.items.some((item) =>
    item.checks.some((check) =>
      check.code === 'sql_requirement_coverage'
      && (check.result === 'failed' || check.result === 'unknown')
      && typeof check.detail === 'string'
      && check.detail.trim().length > 0,
    ),
  )
}

function assertNoToolFailure(response: AskResponse): AskResponse {
  if (hasUnresolvedToolFailure(response) && !isFailureExplainedByReview(response)) {
    throw new Error('本次查询未能完成，系统没有将工具错误当作答案。请稍后重试；若持续失败，请检查服务状态。')
  }
  return response
}

export async function ask(request: AskRequest, signal?: AbortSignal, modelProfileId?: string): Promise<AskResponse> {
  const controller = new AbortController()
  const abort = () => controller.abort(signal?.reason)
  signal?.addEventListener('abort', abort, { once: true })
  if (signal?.aborted) abort()
  const timeout = window.setTimeout(() => controller.abort(new DOMException('请求超时', 'TimeoutError')), ASK_TIMEOUT_MS)
  try {
    const response = await fetch(`${API_BASE}/api/ask`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-AskData-Fact-Review': '2',
        ...(modelProfileId ? { 'X-Model-Profile': modelProfileId } : {}),
      },
      body: JSON.stringify(request),
      signal: controller.signal,
    })
    if (!response.ok) throw new Error(responseError(response.status))
    return assertNoToolFailure(await response.json() as AskResponse)
  } catch (error) {
    if (controller.signal.aborted) {
      const reason: unknown = controller.signal.reason
      if (reason instanceof DOMException && reason.name === 'TimeoutError') {
        throw new Error('等待回答已超过 3 分钟，请稍后重试或简化问题。')
      }
      throw new Error('已停止等待本次回答。')
    }
    if (error instanceof TypeError) throw new Error('无法连接问答服务，请检查网络或后端是否启动。')
    throw error
  } finally {
    window.clearTimeout(timeout)
    signal?.removeEventListener('abort', abort)
  }
}

/** Consume real execution-stage events and the final response from /api/ask/stream. */
export async function askWithProgress(
  request: AskRequest,
  onProgress: (stage: AskProgressStage) => void,
  signal?: AbortSignal,
  modelProfileId?: string,
): Promise<AskResponse> {
  const controller = new AbortController()
  const abort = () => controller.abort(signal?.reason)
  signal?.addEventListener('abort', abort, { once: true })
  if (signal?.aborted) abort()
  const timeout = window.setTimeout(() => controller.abort(new DOMException('请求超时', 'TimeoutError')), ASK_TIMEOUT_MS)
  try {
    const response = await fetch(`${API_BASE}/api/ask/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        'X-AskData-Fact-Review': '2',
        ...(modelProfileId ? { 'X-Model-Profile': modelProfileId } : {}),
      },
      body: JSON.stringify(request),
      signal: controller.signal,
    })
    // Older already-running backends may not have reloaded the streaming route.
    // Fall back to the stable endpoint without changing the AskResponse shape.
    if (response.status === 404 || response.status === 405) {
      return await ask(request, controller.signal, modelProfileId)
    }
    if (!response.ok) throw new Error(responseError(response.status))
    if (!response.body) throw new Error('问答服务未返回处理进度。')

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let result: AskResponse | undefined
    let streamError: string | undefined
    const processFrame = (frame: string) => {
      const data = frame.split(/\r?\n/).filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trim()).join('\n')
      if (!data) return
      let event: { type?: string; id?: string; stage_id?: string; label?: string; status?: AskProgressStage['status']; elapsed_ms?: number; response?: AskResponse; message?: string }
      try { event = JSON.parse(data) as typeof event } catch { return }
      if (event.type === 'stage' && event.id && event.stage_id && event.label && event.status) {
        onProgress({ id: event.id, stage_id: event.stage_id, label: event.label, status: event.status, elapsed_ms: event.elapsed_ms })
      } else if (event.type === 'complete' && event.response) {
        result = event.response
      } else if (event.type === 'error') {
        streamError = event.message || '服务处理未完成，请重试。'
      }
    }

    try {
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const frames = buffer.split(/\r?\n\r?\n/)
        buffer = frames.pop() ?? ''
        for (const frame of frames) processFrame(frame)
      }
      buffer += decoder.decode()
      if (buffer.trim()) processFrame(buffer)
    } finally {
      reader.releaseLock()
    }
    if (streamError) throw new Error(streamError)
    if (!result) throw new Error('问答进度流已结束，但未收到最终回答。')
    return assertNoToolFailure(result)
  } catch (error) {
    if (controller.signal.aborted) {
      const reason: unknown = controller.signal.reason
      if (reason instanceof DOMException && reason.name === 'TimeoutError') {
        throw new Error('等待回答已超过 3 分钟，请稍后重试或简化问题。')
      }
      throw new Error('已停止等待本次回答。')
    }
    if (error instanceof TypeError) throw new Error('无法连接问答服务，请检查网络或后端是否启动。')
    throw error
  } finally {
    window.clearTimeout(timeout)
    signal?.removeEventListener('abort', abort)
  }
}

export async function getResultSetPage(
  id: string,
  sessionId: string,
  offset: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<ResultSetPage> {
  const query = new URLSearchParams({ offset: String(offset), page_size: String(pageSize) })
  const response = await fetch(`${API_BASE}/api/results/${encodeURIComponent(id)}?${query}`, {
    headers: { 'X-Session-ID': sessionId },
    signal,
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: string } | null
    throw new Error(body?.detail || `读取结果失败：${response.status} ${response.statusText}`)
  }
  return response.json() as Promise<ResultSetPage>
}

export async function downloadResultSetCsv(id: string, sessionId: string): Promise<Blob> {
  const response = await fetch(`${API_BASE}/api/results/${encodeURIComponent(id)}/export.csv`, {
    headers: { 'X-Session-ID': sessionId },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: string } | null
    throw new Error(body?.detail || `导出失败：${response.status} ${response.statusText}`)
  }
  return response.blob()
}
