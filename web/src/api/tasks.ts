const API_BASE = import.meta.env.VITE_API_BASE ?? ''

/** 一条量化证据。ratio 用于画进度条，value 是给人看的原始写法（如 "15/16"）。 */
export interface TaskMetric {
  label: string
  value: string
  ratio?: number
  help?: string
}

/** 证据出处。面板上要能点开看是哪个报告，可解释性不只是对答案而言。 */
export interface TaskEvidence {
  kind: 'report' | 'cases' | 'doc' | 'code' | 'test'
  path: string
  note?: string
}

/** 一个中级任务的当前状态。数字全部来自 infra/tasks_evidence.json，前端不做加工。 */
export interface TaskEvidenceItem {
  id: number
  title: string
  subtitle: string
  /** verified = 有报告可查；under_review = 证据不足、待补。 */
  status: 'verified' | 'under_review'
  status_label: string
  headline: string
  metrics: TaskMetric[]
  mechanism: string
  evidence: TaskEvidence[]
  known_gaps: string[]
  scope: string
}

export interface TasksEvidence {
  contract_version?: string
  generated_at?: string
  purpose?: string
  audience_note?: string
  suite_note?: string
  tasks: TaskEvidenceItem[]
  error?: string
}

export async function fetchTasksEvidence(): Promise<TasksEvidence> {
  const response = await fetch(`${API_BASE}/api/tasks/evidence`, { cache: 'no-store' })
  if (!response.ok) throw new Error(`读取中级任务证据失败：${response.status} ${response.statusText}`)
  return response.json() as Promise<TasksEvidence>
}

/** 证据类型的中文名与图标名 —— 图标名必须在 AppIcon 的 paths 里有定义。 */
export const EVIDENCE_KIND_META: Record<TaskEvidence['kind'], { label: string; icon: string }> = {
  report: { label: '运行报告', icon: 'document' },
  cases: { label: '用例文件', icon: 'database' },
  doc: { label: '设计文档', icon: 'document' },
  code: { label: '实现代码', icon: 'panel' },
  test: { label: '单元测试', icon: 'check' },
}
