/**
 * 把线性的进度事件流还原成带嵌套层级的时间线。
 *
 * 为什么需要这一层：后端发的是**线性事件**，但阶段本身是**嵌套**的。
 * 一次文档问答的真实顺序是这样（实测 16 条事件）：
 *
 *     stage-3  tool_doc_search_1  started    检索并核对文档   ← 外层，先开
 *     stage-4  rag_retrieve       started    检索相关文档片段  ┐
 *     stage-4  rag_retrieve       completed  检索相关文档片段  │ 内层，
 *     stage-5  rag_rerank         started    排序并筛选证据片段 │ 在外层
 *     stage-5  rag_rerank         completed  排序并筛选证据片段 │ 还没关的
 *     stage-6  rag_generate       started    依据证据生成回答  │ 时候开合
 *     stage-6  rag_generate       completed  依据证据生成回答  ┘
 *     stage-3  tool_doc_search_1  completed  检索并核对文档   ← 外层，最后才关
 *
 * 平铺渲染这 6 行会出现两个「进行中」（`tool_doc_search_1` 与 `rag_generate`），
 * 读起来像两件事在并行赛跑 —— 实测用户第一反应就是「为什么有两个在转」。
 * 但那一刻系统没有并发，是**大步骤没做完，其中某个小步骤刚开始**。
 *
 * 还原规则来自一个不变量：后开的一定先关（栈），这是 `app/progress.py` 用
 * dict 记账所保证的。于是只用一条规则就能定出层级 ——
 * 某阶段收到终态时，栈里排在它后面的，全都是它的子阶段。
 *
 * 为什么要还原而不是让后端直接发层级：后端事件是「请求作用域的 UI 元数据」，
 * 不带契约承诺（见 `app/progress.py` 开头注释）。层级是**怎么显示**的问题，
 * 属于前端；让后端为显示需要而改事件格式，会把展示逻辑焊进接口。
 */
import type { AskProgressStage } from '../types/contracts'

/** 已经结束的状态。`started` 之外的都算：成功、被挡、跳过、失败。 */
const TERMINAL = new Set<AskProgressStage['status']>(['completed', 'blocked', 'skipped', 'failed'])

export type ProgressNode = AskProgressStage & {
  /** 嵌套深度，顶层为 0。用于缩进，不用于排序。 */
  depth: number
  /** 这个阶段已经结束（收到过终态）。未结束的就是界面上的「进行中」。 */
  settled: boolean
}

/**
 * 事件流 → 带层级的时间线，按「首次出现」顺序返回。
 *
 * 顺序不能按 id 排：id 是后端流水号，但一个阶段的 id 在它 `started` 时就定死了，
 * 而它的**结束位置**在流里可能很靠后（外层阶段就是这样）。按开始时间排才符合
 * 「用户看到事情发生的先后」。
 */
export function buildTimeline(stages: AskProgressStage[]): ProgressNode[] {
  const nodes = new Map<string, ProgressNode>()
  /** 只放还没结束的阶段，栈顶是最近开始的。 */
  const open: ProgressNode[] = []

  for (const stage of stages) {
    const existing = nodes.get(stage.id)

    if (stage.status === 'started') {
      // 同一 id 重复 started 时保留原位置，只刷新字段，不新增一行。
      const node: ProgressNode = existing
        ? { ...existing, ...stage, settled: false }
        : { ...stage, depth: open.length, settled: false }
      nodes.set(stage.id, node)
      // 重开一个 id 时重新压栈（含重复 started 的情况），否则它的结束会错认
      // 栈里别人为子级。
      if (!open.includes(node)) open.push(node)
      continue
    }

    if (!TERMINAL.has(stage.status)) continue

    // 一个阶段结束 = 栈里排在它后面的全部是它的子阶段。
    // 只按 id 定位，不用节点对象比对：重复 started 会换掉对象引用，按 id 才不会漏。
    const position = open.findIndex((item) => item.id === stage.id)
    if (position >= 0) open.splice(position)

    const node = existing ?? { ...stage, depth: 0, settled: false }
    nodes.set(stage.id, { ...node, ...stage, settled: true })
  }

  return [...nodes.values()]
}

/**
 * 时间线里还在进行的阶段数。
 *
 * 界面上「两个在转」是正常的（外层大步骤 + 内层小步骤），但**大于 1 时必须有
 * 嵌套关系可解释**。这个函数给验证脚本用：它比对着时间线的层级断言这一点。
 */
export function activeCount(timeline: ProgressNode[]): number {
  return timeline.filter((node) => !node.settled).length
}

/**
 * 各个顶层阶段的完成情况，用于「N/M 个环节」这类摘要。
 *
 * 只数 depth === 0 的：内层阶段是外层的组成部分，一起计入会让分母随检索实现
 * 变化而跳动（换一个检索策略，环节数就变了），不能作为对用户承诺的口径。
 */
export function topLevelProgress(timeline: ProgressNode[]): { done: number; total: number } {
  const tops = timeline.filter((node) => node.depth === 0)
  return { done: tops.filter((node) => node.settled).length, total: tops.length }
}
