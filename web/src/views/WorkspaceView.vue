<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import ChatThread from '../components/ChatThread.vue'
import Composer from '../components/Composer.vue'
import EvidencePanel from '../components/EvidencePanel.vue'
import DatabasePanel from '../components/DatabasePanel.vue'
import SettingsPanel from '../components/SettingsPanel.vue'
import { useConversationStore } from '../stores/conversation'
import type { Session } from '../stores/persistence'
import { checkService } from '../api/service'
import { getCrossSourceDemoReadiness, type CrossSourceDemoReadiness } from '../api/capabilities'
import { getDatabaseStatus, getUnstructuredDatabaseStatus } from '../api/database'
import type { DatabaseStatus, UnstructuredDatabaseStatus } from '../api/database'
import { getKagReadiness } from '../api/kag'
import { toggleTheme, useTheme } from '../utils/theme'
import { useAuthStore } from '../stores/auth'

const { theme, followsSystem } = useTheme()

const conversation = useConversationStore()
const auth = useAuthStore()
const authActionError = ref('')
const sidebarOpen = ref(false)
const sidebarCollapsed = ref(false)
const mobileViewport = ref(false)
let mobileViewportQuery: MediaQueryList | undefined
const evidenceOpen = ref(false)
const menuSessionId = ref('')
const menuPosition = ref({ top: 0, left: 0 })
let menuCloseTimer: ReturnType<typeof setTimeout> | undefined
const batchMode = ref(false)
const selectedSessionIds = ref<string[]>([])
const guideOpen = ref(false)
/**
 * 使用指南是**顺序引导**而不是参考手册：第一次打开的用户按
 * 能答什么 → 怎么看答案 → 开始用 的顺序走完，每步只做一件事。
 * 手动从顶栏打开时默认停在第 1 步；「下一步」推进，步骤条可点回退。
 */
type GuideStep = 'what' | 'how' | 'start'
const guideStep = ref<GuideStep>('what')
const guideStepIndex = computed(() => GUIDE_STEPS.findIndex((s) => s.id === guideStep.value))
const GUIDE_STEPS: { id: GuideStep; label: string }[] = [
  { id: 'what', label: '能答什么' },
  { id: 'how', label: '怎么看答案' },
  { id: 'start', label: '开始用' },
]
/** 每步的精选示例：全部来自灵感库中实测跑通过的问句（与 questionGallery.ts 同源）。 */
const GUIDE_TRY_QUESTIONS = ['平台上一共有多少位客户？', '智能计算设备年度维保服务合同里，质保期是多久？']
const GUIDE_START_QUESTIONS = ['2023 年一整年一共开了多少张发票？', '机房日常巡检的时间安排是怎样的？', '买家一共有多少位？']
const CROSS_SOURCE_TRY_QUESTION = '工单 WO-2026-0001 是否在合同修复时限内完成？修复耗时是多少？'
const architectureOpen = ref(false)
/**
 * 架构导览的两种视图。
 *
 * `flow` 是按数据流动画的五阶段演示，`modules` 是模块目录。
 * 默认停在 flow：评委第一次打开时，先看懂「一条问题经过哪几步」比
 * 先看到八个模块名更有用。切到 modules 是「我想知道某个模块具体做什么」。
 */
const architectureView = ref<'flow' | 'modules'>('flow')
/**
 * 已展开的模块名集合。
 *
 * 用 Set 而不是单个 string：展开是「查看详情」，不是「单选」。点开第二个
 * 就把第一个收起来，会让人以为看漏了刚才那张卡。卡片各自独立展开、各自
 * 收起，代价只是弹窗变长 —— 弹窗本来就能滚动。
 */
const expandedModules = ref<Set<string>>(new Set())
/**
 * 流程图当前走到第几个阶段，以及是否处于「正在推进」状态。
 *
 * 到末尾停住而不是循环 —— 循环动画会让人一直等它「再跑一遍」，
 * 停住才表示「这就是完整路径」。切回该视图时重跑一次。
 *
 * **节拍不再由 JS 控制。** 旧版用 `setInterval` 每 620ms 把计数器 +1，
 * 五个阶段要走 3.1 秒，且每一跳都是"格子忽然变亮"的离散跳变 —— 主观上
 * 就是"一顿一顿"。现在 JS 只负责开头置位、结尾收尾（各一次），
 * 中间的逐项推进交给 CSS 的 `animation-delay`，全程约 1.1 秒且是连续过渡。
 *
 * 为什么交给 CSS 更好：animation-delay 由浏览器的动画时钟统一驱动，
 * 不会被主线程上的其他任务推迟（JS 定时器会），因此不会出现"某两格之间
 * 明显多停了一下"这种不均匀感 —— 那正是"卡顿"最常见的来源之一。
 */
const flowProgress = ref(0)
let flowTimer: ReturnType<typeof setInterval> | null = null
/**
 * 界面偏好面板。放这里而不是做成居中弹窗：设置是「停下来调一下再回去」，
 * 滑出面板不会把整屏遮住，改完直接看背景里的对话。
 */
const settingsOpen = ref(false)
const databaseOpen = ref(false)
let guideOpener: HTMLElement | null = null
let architectureOpener: HTMLElement | null = null
let settingsOpener: HTMLElement | null = null
let databaseOpener: HTMLElement | null = null
const serviceState = ref<'checking' | 'online' | 'offline'>('checking')
const databaseStatus = ref<DatabaseStatus | null>(null)
const unstructuredDatabaseStatus = ref<UnstructuredDatabaseStatus | null>(null)
const crossSourceDemoStatus = ref<CrossSourceDemoReadiness['status'] | 'checking'>('checking')
const kagReady = ref(false)
let serviceTimer: ReturnType<typeof setInterval> | undefined
const homeQuestionSections = [
  {
    id: 'data', title: '问数', icon: 'database', tone: 'blue',
    description: '从业务数据库查询数量、记录和统计结果。',
    scope: '答案可查看数据表与查询记录。',
    questions: ['平台上一共有多少位客户？', '2023 年一整年一共开了多少张发票？'],
  },
  {
    id: 'document', title: '问答', icon: 'document', tone: 'teal',
    description: '从资料中查找条款、说明和原文出处。',
    scope: '答案可点击引用核对原文。',
    questions: ['智能计算设备年度维保服务合同里，质保期是多久？', '机房日常巡检的时间安排是怎样的？'],
  },
  {
    id: 'cross-source', title: '跨源问题', icon: 'cross-source', tone: 'cross',
    description: '把业务记录与相关文档对应起来，再核对结论和依据。',
    scope: '当前专用示例范围：工单—合同；需隔离演示数据与核验服务。',
    questions: [CROSS_SOURCE_TRY_QUESTION],
  },
]
/** 输入框组件。主页示例卡要把问句塞进去，只能从外面调它的 `fill()`。 */
const composerRef = ref<{ fill: (question: string) => void; focus: () => void } | null>(null)
/**
 * FEVA 模块目录。
 *
 * 每个模块除了卡片上的摘要，还带三样展开后才显示的内容：
 * `detail`（它到底做什么）、`io`（输入什么、输出什么）、`wired`（现在是否
 * 接在默认问答链路上）。第三项是刻意保留的诚实标注 —— KAG 不属于默认检索路线，
 * 只在服务可用且用户主动选择 KAG 补充深度检索时作为候选补充。
 */
const architectureModules = [
  {
    name: 'FactFlow', icon: 'fact-flow', title: '事实流', status: '工单—合同专用流程',
    concept: 'F · 事实（Fact）',
    description: '把复合问题整理成待回答事实和依赖关系，先完成前置事实，再执行依赖任务。',
    detail: '复合问题里往往藏着先后顺序：要先知道工单对应的合同是哪一份，才谈得上查这份合同的条款。事实流把这个顺序显式写成一张依赖图，而不是让模型在一次生成里同时兼顾推理和取值。',
    io: { input: '一句复合问题 + 工单号或客户编号', output: '待回答事实清单 + 它们之间的依赖顺序' },
    wired: true,
  },
  {
    name: 'SourceBridge', icon: 'source-bridge', title: '跨源证桥', status: '工单—合同专用流程',
    concept: 'V · 核验（Verification）',
    description: '依据稳定编号、受控映射、客户范围和有效期核验不同来源中的对象关系。',
    detail: '结构化库里是工单号，文档里写的是合同名，两者不是同一个字符串。跨源证桥负责判断「这两个东西指的是不是同一个对象」，判据是稳定编号、客户范围与有效期，而不是名称相似度 —— 相似度会把同名不同客户的两个合同误判成一个。',
    io: { input: '各来源各自提到的对象（编号、名称、时间）', output: '确认为同一对象的对应关系，或明确判为不一致' },
    wired: true,
  },
  {
    name: 'MetricAlign', icon: 'metric-align', title: '口径对齐', status: '工单—合同专用流程',
    concept: 'A · 对齐（Alignment）',
    description: '核对事件类型、起止时间、时区、单位和合同条款定义，再比较事实。',
    detail: '「多少天」这句话在工单表和合同条款里可能不是同一个算法：一个按自然日算，一个按工作日算；一个含当天，一个不含。口径对齐把这些定义摆到一起核对，对不上就不给结论，而不是先算出来再说。',
    io: { input: '两侧各自的时间范围、单位与事件定义', output: '口径是否可比的判定；不可比时说明差在哪一项' },
    wired: true,
  },
  {
    name: 'EvidenceLedger', icon: 'evidence-ledger', title: '证据账本', status: '内部事实记录',
    concept: 'E · 证据（Evidence）',
    description: '记录事实的候选、支持、冲突、不足或歧义状态，并保留对应来源。',
    detail: '这是可解释性的落点：每条事实都要带上它是被什么支持的、来自哪一份文件或哪一行数据。证据不足或存在冲突时如实记录状态，而不是挑一个看起来合理的填上去。',
    io: { input: '各步骤产出的事实与它们引用的原始记录', output: '带状态标注与来源指针的事实账本' },
    wired: true,
  },
  {
    name: 'BoundedVerify', icon: 'bounded-verify', title: '有界求证', status: '边界持续复核',
    description: '限制补查与重试；依赖条件不成立时停止后续步骤。多合同晚停仍是跟踪项。',
    detail: '补查和重试都要有上限。前置条件不成立时，正确的做法是停下并说明停在哪一步，而不是继续往下编 —— 继续往下走会产出一个看不出问题的错答案，比直接报错更危险。',
    io: { input: '当前步骤的完成状态与剩余预算', output: '继续 / 停止的判定；停止时给出断点位置' },
    wired: true,
  },
  {
    name: 'QueryGuard', icon: 'query-guard', title: '查询卫士', status: '数据库问数主线',
    description: '覆盖表字段定位、关联语义检查、SQL 只读校验和数据库执行。',
    detail: '数据库问数的完整通道：先把问题里的词对应到真实的表和字段，再规划多表之间的关联路径，生成的 SQL 先过只读校验（用 sqlglot 解析语法树，拒绝写操作），通过后才交给数据库执行。',
    io: { input: '自然语言问题 + 库表结构', output: '可执行的只读 SQL + 结果集 + 引用的表字段' },
    wired: true,
  },
  {
    name: 'DocLens', icon: 'doc-lens', title: '文证透镜', status: '文档问答主线',
    description: '覆盖文档解析、章节切片、混合检索、重排及原文引用定位。',
    detail: '文档问数的完整通道。切片时保留章节层级和页码坐标，检索时向量与关键词混合召回再重排，最后把答案指回原文的确切位置 —— 这是「点引用能跳到原文那一行」的底层依据。',
    io: { input: '自然语言问题 + 文档语料', output: '答案 + 原文出处（文档、页码、章节、字符区间）' },
    wired: true,
  },
  {
    name: 'KAG增强路线', icon: 'gap2graph', title: '图谱按需增强', status: '用户可选·非默认路径',
    description: '标准检索默认走项目 RAG；用户选择 KAG 补充深度检索时，KAG 可补充候选原文。',
    detail: 'KAG 补充深度检索是可选路线：项目 RAG 与 KAG 提供候选原文，之后仍由项目统一处理答案与引用。是否可用取决于后端运行状态；未就绪时标准检索仍可使用。',
    io: { input: '用户选择 KAG 补充深度检索的问题', output: '补充候选原文，由项目统一回答和关联引用' },
    wired: false,
  },
]
/**
 * 流程图：一条问题从进来到给出答案经过的五个阶段。
 *
 * 与 `architectureModules` 是两种切分方式 —— 那边按模块（谁负责什么），
 * 这边按时间顺序（先发生什么）。`modules` 记的是每个阶段由哪些模块承担，
 * 展开时用来对应回模块目录，两处要一起改。
 */
const FEVA_FLOW = [
  { step: '识别事实', module: 'FactFlow', desc: '把一句话拆成待回答的事实与它们的先后依赖。' },
  { step: '核验关系', module: 'SourceBridge', desc: '确认不同来源提到的对象是不是同一个。' },
  { step: '对齐口径', module: 'MetricAlign', desc: '核对时间范围、单位与事件定义是否可比。' },
  { step: '关联证据', module: 'EvidenceLedger', desc: '为每条事实记下支持它的原始记录与来源。' },
  { step: '边界处理', module: 'BoundedVerify', desc: '条件不成立就停下并说明断点，不硬往下编。' },
]
/** 流程图里最后落到的那条实际链路（问数或问文档），动画走完后显示。 */
const FEVA_OUTLETS = [
  { name: 'QueryGuard', label: '数据库问数', icon: 'database' },
  { name: 'DocLens', label: '文档问答', icon: 'document' },
]

/**
 * 会话时间显示到分钟。
 *
 * 当天只显示「时:分」——列表里绝大多数是今天的会话，带上日期反而是噪音；
 * 跨天补上「月/日 时:分」，跨年再补年份。用 toLocaleString 逐段取，
 * 不自己拼字符串，避免时区与补零判断出错。
 */
function formatSessionTime(value: string): string {
  const at = new Date(value)
  if (Number.isNaN(at.getTime())) return ''
  const time = at.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  const now = new Date()
  const sameDay = at.getFullYear() === now.getFullYear()
    && at.getMonth() === now.getMonth()
    && at.getDate() === now.getDate()
  if (sameDay) return time
  const monthDay = at.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
  if (at.getFullYear() === now.getFullYear()) return `${monthDay} ${time}`
  return `${at.getFullYear()}/${monthDay} ${time}`
}

const hasMessages = computed(() => conversation.messages.length > 0)
const sidebarExpanded = computed(() => mobileViewport.value ? sidebarOpen.value : !sidebarCollapsed.value)
const sidebarToggleLabel = computed(() => sidebarExpanded.value ? '收起左侧会话栏' : '展开左侧会话栏')
const sidebarToggleIcon = computed(() => mobileViewport.value && !sidebarOpen.value ? 'menu' : 'sidebar-fold')
const roundCount = computed(() => conversation.messages.filter((message) => message.role === 'user').length)
const lastAnswer = computed(() => [...conversation.messages].reverse().find((message) => message.response))
const evidenceCount = computed(() => lastAnswer.value?.response?.refs.length ?? 0)
/**
 * 追问建议取「最后一条带建议的回答」。
 *
 * 为什么不是只看最后一条消息：正在等待时最后一条是 pending 的助手气泡（没有
 * response），若直接取 `.at(-1).response` 建议会在提问瞬间消失、答完又出现，
 * 视觉上闪一下。改成回溯最近一条**已经有回答**的消息，提问期间建议原地保留，
 * 用户还能看到自己是从哪条建议点过来的。
 */
const activeSuggestions = computed(() => {
  for (let i = conversation.messages.length - 1; i >= 0; i -= 1) {
    const response = conversation.messages[i]?.response
    if (response) return response.suggestions ?? []
  }
  return []
})
const serviceLabel = computed(() => ({ checking: '正在连接服务', online: '后端服务已连接', offline: '后端服务未连接' })[serviceState.value])
const structuredScopeStatus = computed(() => {
  const status = databaseStatus.value?.status
  if (status === 'connected') return '已连接'
  if (status === 'catalog_mismatch') return '目录需核对'
  if (status === 'unavailable') return '暂不可用'
  if (status === 'not_used') return '未启用'
  return '状态未知'
})
const documentScopeStatus = computed(() => {
  const status = unstructuredDatabaseStatus.value?.status
  if (status === 'connected') return '已连接'
  if (status === 'memory') return '内存索引'
  if (status === 'unavailable') return '暂不可用'
  if (status === 'not_used') return '未启用'
  return '状态未知'
})
const crossSourceDemoScopeStatus = computed(() => {
  if (crossSourceDemoStatus.value === 'ready') return '已连接'
  if (crossSourceDemoStatus.value === 'checking') return '正在检查'
  if (crossSourceDemoStatus.value === 'disabled') return '未启用'
  return '暂不可用'
})

function homeSectionAvailable(sectionId: string): boolean {
  if (sectionId === 'data') return databaseStatus.value?.status === 'connected'
  if (sectionId === 'document') {
    return ['connected', 'memory'].includes(unstructuredDatabaseStatus.value?.status ?? '')
  }
  return sectionId === 'cross-source' && crossSourceDemoStatus.value === 'ready'
}
function homeSectionStatus(sectionId: string): string {
  if (sectionId === 'data') return structuredScopeStatus.value
  if (sectionId === 'document') return documentScopeStatus.value
  if (crossSourceDemoStatus.value === 'ready') return '演示链路已就绪'
  if (crossSourceDemoStatus.value === 'checking') return '正在检查演示数据'
  return '演示数据未连接'
}
const databaseSidebarLabel = computed(() => {
  if (!databaseStatus.value) return '状态未知 · 点击查看库表'
  const labels: Record<DatabaseStatus['status'], string> = {
    connected: '已连接',
    unavailable: '连接失败',
    not_used: '真实业务库未启用',
    catalog_mismatch: '已连接',
  }
  const structuredLabel = labels[databaseStatus.value.status]
  if (!unstructuredDatabaseStatus.value) return `${structuredLabel} · 文档库状态未知`
  const unstructuredLabels: Record<UnstructuredDatabaseStatus['status'], string> = {
    connected: '文档库已连接',
    memory: '文档内存索引',
    not_used: '文档检索未启用',
    unavailable: '文档库连接失败',
  }
  return `${structuredLabel} · ${unstructuredLabels[unstructuredDatabaseStatus.value.status]}`
})
const databaseSidebarState = computed(() => {
  if (databaseStatus.value?.status === 'unavailable' || unstructuredDatabaseStatus.value?.status === 'unavailable') return 'unavailable'
  if (unstructuredDatabaseStatus.value?.status === 'memory') return 'memory'
  if (databaseStatus.value?.status === 'not_used' || unstructuredDatabaseStatus.value?.status === 'not_used') return 'not_used'
  if (databaseStatus.value?.status === 'catalog_mismatch') return 'connected'
  return databaseStatus.value?.status ?? 'unknown'
})

const menuSession = computed(() => conversation.sessions.find((session) => session.id === menuSessionId.value))

function submit(question: string) { void conversation.submit(question) }
/**
 * 点主页示例卡：只把问句填进输入框，不替用户发送。
 *
 * 冷启动时随手点一张卡，用户还没看清问句长什么样；直接发出去，答错了
 * 他都不知道该改哪一句。填进输入框让他看一眼、改一改再决定要不要问。
 * 追问建议走的是另一条路（`Composer` 的 `pick()`，点了直接发）—— 那是
 * 他看过上一轮答案之后才点的，意图已经明确，不需要再确认一次。
 */
function useExample(question: string) {
  if (conversation.isPending) return
  composerRef.value?.fill(question)
}
/** 使用指南由用户主动打开，不阻挡首次提问。 */
function showGuide() {
  guideStep.value = 'what'
  guideOpen.value = true
}
/** 引导推进一步；最后一步的「下一步」就是收尾关闭。 */
function nextGuideStep() {
  const next = GUIDE_STEPS[guideStepIndex.value + 1]
  if (next) guideStep.value = next.id
  else closeGuide()
}
/** 关闭使用指南并把焦点交还输入框。 */
function closeGuide() {
  guideOpen.value = false
  guideOpener = null
  composerRef.value?.focus()
}
/** 指南里点精选问句：填入输入框，仍由用户决定是否发送。 */
function pickGuideQuestion(question: string) {
  closeGuide()
  composerRef.value?.fill(question)
}
function syncMobileViewport() {
  mobileViewport.value = mobileViewportQuery?.matches ?? window.matchMedia('(max-width: 767px)').matches
}
function toggleSidebar() {
  if (mobileViewport.value) sidebarOpen.value = !sidebarOpen.value
  else sidebarCollapsed.value = !sidebarCollapsed.value
}
/** 系统说明是次级入口；动画只用于解释流程，不代表某次请求正在执行。 */
function showArchitecture() {
  architectureOpener = document.activeElement instanceof HTMLElement ? document.activeElement : null
  architectureView.value = 'flow'
  expandedModules.value = new Set()
  architectureOpen.value = true
  startFlow()
}

/**
 * 启动流程动画。
 *
 * 旧版在这里跑 `setInterval` 每 620ms 推一格，五个阶段 3.1 秒，
 * 并且每一格都是"忽然变亮"的离散跳变。现在只做两件事：
 *   ② 挂一个单发定时器，在动画真正走完时把状态钉在"全部点亮"。
 * 中间不再有任何 JS 参与，主线程不会被节拍打断。
 *
 * 停在末尾而不是循环；用户切到模块目录或关闭弹窗时停止计时。
 */
function startFlow() {
  stopFlow()
  flowProgress.value = 0
  flowTimer = setInterval(() => {
    if (flowProgress.value >= FEVA_FLOW.length) stopFlow()
    else flowProgress.value += 1
  }, 620)
}
function stopFlow() {
  if (flowTimer !== null) clearInterval(flowTimer as ReturnType<typeof setInterval>)
  flowTimer = null
}
/** 切视图时：进流程图重跑动画，进目录则停表并保留已展开项。 */
function switchArchitectureView(view: 'flow' | 'modules') {
  architectureView.value = view
  if (view === 'flow') startFlow()
  else stopFlow()
}
/** 点模块卡片：再点一次收起；同时只展开一个。 */
function toggleModule(name: string) {
  // 必须换一个新 Set 再赋值。Vue 3 的 ref 对 Set 的变更不是深度监听：
  // 直接 expandedModules.value.add/delete 改了内容也不会触发重新渲染。
  const next = new Set(expandedModules.value)
  if (next.has(name)) next.delete(name)
  else next.add(name)
  expandedModules.value = next
}
function showSettings() {
  settingsOpener = document.activeElement instanceof HTMLElement ? document.activeElement : null
  settingsOpen.value = true
}
function selectRef(messageId: string, index: number) { conversation.selectRef(messageId, index); evidenceOpen.value = true }
function newSession() { conversation.newSession(); sidebarOpen.value = false; evidenceOpen.value = false }
function toggleEvidence() {
  if (!conversation.activeRef && lastAnswer.value?.response?.refs.length) conversation.selectRef(lastAnswer.value.id, 0)
  evidenceOpen.value = !evidenceOpen.value
}
function switchSession(id: string) {
  if (batchMode.value) return toggleSelected(id)
  conversation.switchSession(id)
  sidebarOpen.value = false
  evidenceOpen.value = false
}
function toggleMenu(event: MouseEvent, id: string) {
  cancelMenuClose()
  if (menuSessionId.value === id) {
    menuSessionId.value = ''
    return
  }
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  menuPosition.value = { top: rect.bottom + 3, left: rect.right - 142 }
  menuSessionId.value = id
}
function cancelMenuClose() {
  if (menuCloseTimer) clearTimeout(menuCloseTimer)
  menuCloseTimer = undefined
}
function scheduleMenuClose() {
  cancelMenuClose()
  menuCloseTimer = setTimeout(() => { menuSessionId.value = '' }, 120)
}
function renameSession(session: Session) {
  menuSessionId.value = ''
  const title = window.prompt('重命名会话', session.title)
  if (title !== null) conversation.renameSession(session.id, title)
}
function togglePin(session: Session) {
  conversation.toggleSessionPin(session.id)
  menuSessionId.value = ''
}
function toggleSelected(id: string) {
  selectedSessionIds.value = selectedSessionIds.value.includes(id)
    ? selectedSessionIds.value.filter((item) => item !== id)
    : [...selectedSessionIds.value, id]
}
function enterBatchMode() {
  batchMode.value = true
  selectedSessionIds.value = []
  menuSessionId.value = ''
}
function exitBatchMode() {
  batchMode.value = false
  selectedSessionIds.value = []
}
function deleteSessions(ids: string[]) {
  if (!ids.length) return
  if (window.confirm(`删除选中的 ${ids.length} 个本地会话？此操作无法恢复。`)) {
    conversation.deleteSessions(ids)
    exitBatchMode()
  }
}
async function refreshService() { serviceState.value = await checkService() ? 'online' : 'offline' }
async function logout() {
  authActionError.value = ''
  try {
    await auth.logout()
  } catch (error) {
    authActionError.value = error instanceof Error ? error.message : '退出登录失败，请重试。'
  }
}
async function refreshCrossSourceDemoStatus() {
  try {
    const readiness = await getCrossSourceDemoReadiness()
    crossSourceDemoStatus.value = readiness.status
  } catch {
    crossSourceDemoStatus.value = 'unavailable'
  }
}
async function refreshKagStatus() {
  try {
    kagReady.value = (await getKagReadiness()).ready
  } catch {
    kagReady.value = false
  }
  if (!kagReady.value && conversation.retrievalMode === 'deep') conversation.setRetrievalMode('standard')
}
async function refreshDatabaseStatus() {
  const [structured, unstructured] = await Promise.allSettled([
    getDatabaseStatus(),
    getUnstructuredDatabaseStatus(),
  ])
  databaseStatus.value = structured.status === 'fulfilled' ? structured.value : null
  unstructuredDatabaseStatus.value = unstructured.status === 'fulfilled' ? unstructured.value : null
}
function showDatabase() {
  databaseOpener = document.activeElement instanceof HTMLElement ? document.activeElement : null
  // 数据库面板是独立的全屏浮层。先收起可能残留的其他浮层，避免它们
  // 在过渡期间盖住数据库面板，尤其是首次使用指南或移动端侧栏遮罩。
  guideOpen.value = false
  architectureOpen.value = false
  settingsOpen.value = false
  sidebarOpen.value = false
  databaseOpen.value = true
}
function restoreDatabaseFocus() {
  if (!databaseOpener?.isConnected) return
  const rect = databaseOpener.getBoundingClientRect()
  const visibleInViewport = rect.width > 0 && rect.height > 0 && rect.right > 0 && rect.bottom > 0 && rect.left < window.innerWidth && rect.top < window.innerHeight
  if (visibleInViewport) databaseOpener.focus()
  else document.querySelector<HTMLElement>('.sidebar-toggle')?.focus()
}
function onEscape(event: KeyboardEvent) {
  if (event.key === 'Tab' && guideOpen.value) {
    const buttons = Array.from(document.querySelectorAll<HTMLElement>('.guide button'))
    const first = buttons[0]
    const last = buttons.at(-1)
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
  }
  if (event.key === 'Tab' && architectureOpen.value) {
    const buttons = Array.from(document.querySelectorAll<HTMLElement>('.architecture-dialog button'))
    const first = buttons[0]
    const last = buttons.at(-1)
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
  }
  if (event.key === 'Tab' && settingsOpen.value) {
    const buttons = Array.from(document.querySelectorAll<HTMLElement>('.settings button, .settings input'))
    const first = buttons[0]
    const last = buttons.at(-1)
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
  }
  if (event.key === 'Tab' && databaseOpen.value) {
    const controls = Array.from(document.querySelectorAll<HTMLElement>('.database-dialog button:not(:disabled), .database-dialog input:not(:disabled)'))
    const first = controls[0]
    const last = controls.at(-1)
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
  }
  if (event.key !== 'Escape') return
  guideOpen.value = false
  architectureOpen.value = false
  settingsOpen.value = false
  databaseOpen.value = false
  sidebarOpen.value = false
  evidenceOpen.value = false
  menuSessionId.value = ''
}
watch(() => conversation.activeSessionId, () => { evidenceOpen.value = false })
watch(guideOpen, async (open) => {
  if (open) {
    guideOpener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    await nextTick()
    document.querySelector<HTMLButtonElement>('.guide button')?.focus()
  } else {
    if (guideOpener) {
      // closeGuide 会把焦点交给输入框并顺手清掉 opener（它想接管焦点）；
      // 这里只在 opener 还活着时才恢复 —— 比如按 Esc 关闭的路径。
      guideOpener.focus()
    }
  }
})
watch(architectureOpen, async (open) => {
  if (open) {
    await nextTick()
    document.querySelector<HTMLButtonElement>('.architecture-dialog button')?.focus()
  } else {
    // 关掉弹窗必须停掉流程定时器，否则它在后台继续推进，
    // 下次打开时会在动画进行到一半的基础上叠加。
    stopFlow()
    architectureOpener?.focus()
  }
})
watch(settingsOpen, async (open) => {
  if (open) await nextTick()
  else settingsOpener?.focus()
})
watch(databaseOpen, async (open) => {
  if (open) {
    await nextTick()
    document.querySelector<HTMLButtonElement>('.database-dialog .close-button')?.focus()
  } else restoreDatabaseFocus()
})
onMounted(() => {
  mobileViewportQuery = window.matchMedia('(max-width: 767px)')
  syncMobileViewport()
  mobileViewportQuery.addEventListener('change', syncMobileViewport)
  void refreshService()
  void refreshDatabaseStatus()
  void refreshCrossSourceDemoStatus()
  void refreshKagStatus()
  serviceTimer = setInterval(() => { void refreshService(); void refreshDatabaseStatus(); void refreshKagStatus() }, 30000)
  window.addEventListener('keydown', onEscape)
})
onUnmounted(() => {
  mobileViewportQuery?.removeEventListener('change', syncMobileViewport)
  cancelMenuClose()
  if (serviceTimer) clearInterval(serviceTimer)
  stopFlow()
  window.removeEventListener('keydown', onEscape)
})
</script>

<template>
  <div class="workspace" :class="{ 'sidebar-open': sidebarOpen, 'sidebar-collapsed': sidebarCollapsed }">
    <aside id="session-sidebar" class="sidebar" :aria-hidden="mobileViewport ? !sidebarOpen : sidebarCollapsed" :inert="mobileViewport ? !sidebarOpen : sidebarCollapsed">
      <div class="brand"><span class="brand__mark"><img src="/brand/verimind-mark.svg?v=20261012" alt="" aria-hidden="true" /></span><div><b>知证<em>VeriMind</em></b><small>可解释精准问数问答智能体</small></div></div>
      <button class="new-session vm-press vm-interact" aria-label="新建问答" @click="newSession"><AppIcon class="vm-icon-shift" name="plus" :size="18" /><span>新建问答</span></button>
      <div class="workspace-label"><AppIcon name="chat" :size="18" /><span>问答工作台</span><span class="workspace-label__dot" /></div>
      <div class="sidebar-heading"><span>最近会话</span><button v-if="!batchMode" class="text-button" @click="enterBatchMode">管理</button></div>
      <nav aria-label="会话历史">
        <div v-for="session in conversation.sessions" :key="session.id" class="session-item">
          <button v-if="batchMode" class="session-check" :class="{ selected: selectedSessionIds.includes(session.id) }" :aria-label="`选择会话：${session.title}`" @click.stop="toggleSelected(session.id)">✓</button>
          <button class="session-select" :class="{ active: session.id === conversation.activeSessionId, selected: selectedSessionIds.includes(session.id) }" @click="switchSession(session.id)">
            <span class="session-title"><AppIcon :name="session.pinned ? 'pin' : 'chat'" :size="15" /><b>{{ session.title }}</b></span><small>{{ formatSessionTime(session.updatedAt) }}</small>
          </button>
          <button v-if="!batchMode" class="session-more" :class="{ open: menuSessionId === session.id }" :aria-label="`会话操作：${session.title}`" title="会话操作" @mouseenter="cancelMenuClose" @mouseleave="scheduleMenuClose" @click.stop="toggleMenu($event, session.id)"><AppIcon name="more" :size="17" /></button>
        </div>
      </nav>
      <Teleport to="body">
        <div v-if="menuSession" class="session-menu session-menu--floating vm-detail" :style="{ top: `${menuPosition.top}px`, left: `${menuPosition.left}px` }" @mouseenter="cancelMenuClose" @mouseleave="scheduleMenuClose">
          <button @click="renameSession(menuSession)"><AppIcon name="edit" :size="14" />重命名</button>
          <button @click="togglePin(menuSession)"><AppIcon name="pin" :size="14" />{{ menuSession.pinned ? '取消置顶' : '置顶' }}</button>
          <button @click="enterBatchMode"><AppIcon name="list-check" :size="14" />多选</button>
          <button class="danger" @click="deleteSessions([menuSession.id])"><AppIcon name="trash" :size="14" />删除</button>
        </div>
      </Teleport>
      <div v-if="batchMode" class="batch-actions"><span>已选 {{ selectedSessionIds.length }} 项</span><button :disabled="!selectedSessionIds.length" @click="deleteSessions(selectedSessionIds)">删除</button><button @click="exitBatchMode">完成</button></div>
      <div class="sidebar-foot">
        <div class="sidebar-status" aria-label="运行状态">
          <div class="sidebar-user">
            <span class="sidebar-user__identity"><b>{{ auth.user?.username }}</b><small>{{ auth.user?.role === 'admin' ? '管理员' : '已登录' }}</small></span>
            <button type="button" class="sidebar-user__logout" @click="logout">退出</button>
          </div>
          <p v-if="authActionError" class="sidebar-auth-error" role="alert">{{ authActionError }}</p>
          <button class="service-state" :class="serviceState" title="点击重新检查后端连通性" @click="refreshService"><i aria-hidden="true" /><span>{{ serviceLabel }}</span><AppIcon name="retry" :size="13" /></button>
          <button class="database-entry vm-press" type="button" aria-label="打开数据库连接状态" :title="`数据库连接：${databaseSidebarLabel}`" @click.stop.prevent="showDatabase"><span class="database-entry__icon"><img src="/brand/icons/database-catalog.svg" alt="" aria-hidden="true" /></span><span class="database-entry__copy"><b>数据库连接</b><small>{{ databaseSidebarLabel }}</small></span><i class="database-entry__dot" :class="databaseSidebarState" aria-hidden="true" /></button>
          <p class="local-note"><AppIcon name="shield" :size="13" /><span>会话仅保存在此浏览器</span></p>
        </div>
      </div>
    </aside>

    <section class="conversation" :class="{ 'conversation--empty': !hasMessages }">
      <header class="topbar">
        <button class="icon-button sidebar-toggle vm-press" :aria-label="sidebarToggleLabel" :title="sidebarToggleLabel" aria-controls="session-sidebar" :aria-expanded="sidebarExpanded" @click="toggleSidebar"><AppIcon class="sidebar-toggle__icon" :class="{ 'sidebar-toggle__icon--collapsed': !sidebarExpanded }" :name="sidebarToggleIcon" /></button>
        <div class="breadcrumb"><span>工作台</span><AppIcon name="chevron" :size="12" /><b>智能问答</b></div>
        <div v-if="hasMessages" class="session-context">
          <h1 class="session-context__title" :title="conversation.activeSession?.title || '新建问答'">{{ conversation.activeSession?.title || '新建问答' }}</h1>
        </div>
        <div class="topbar__actions"><button class="architecture-button" aria-label="查看系统说明" title="系统说明：了解事实核验架构与当前能力范围" @click="showArchitecture()"><img src="/brand/icons/feva-architecture.svg" alt="" /><span>系统说明</span></button><button class="theme-button" :aria-label="theme === 'dark' ? '切换到亮色模式' : '切换到暗色模式'" :title="(theme === 'dark' ? '切换到亮色模式' : '切换到暗色模式') + (followsSystem ? '（当前跟随系统）' : '')" @click="toggleTheme()"><AppIcon :name="theme === 'dark' ? 'sun' : 'moon'" :size="17" /></button><button class="guide-button" aria-label="使用指南" title="使用指南" @click="showGuide()"><AppIcon name="help" :size="16" /><span>使用指南</span></button><button class="settings-button" aria-label="设置" title="设置：处理过程展示方式与界面外观" @click="showSettings"><AppIcon name="gear" :size="16" /><span>设置</span></button><button v-if="hasMessages" class="evidence-button" :class="{ active: evidenceOpen }" :aria-expanded="evidenceOpen" @click="toggleEvidence"><AppIcon name="panel" :size="17" /><span>来源证据</span><small v-if="evidenceCount">{{ evidenceCount }}</small></button></div>
      </header>
      <div class="conversation__body" :class="{ 'conversation__body--evidence-open': evidenceOpen }">
        <div class="conversation__content">
      <div v-if="conversation.persistFailed" class="storage-warning" role="status">浏览器存储空间不足，会话可能无法保存。</div>
      <ChatThread v-if="hasMessages" :messages="conversation.messages" :progress-by-message="conversation.progressByMessage" :active-message-id="conversation.activeMessageId" :active-ref-index="conversation.activeRefIndex" :session-id="conversation.activeSessionId" @select-ref="selectRef" @retry="conversation.retry($event)" />
      <div v-else class="welcome-scroll">
        <div class="welcome">
          <section class="welcome__intro vm-reveal" aria-labelledby="welcome-title">
            <div class="welcome__eyebrow"><span />知证 VeriMind <b>· 问答工作台</b></div>
            <h1 id="welcome-title">先查清事实，<em>再给可核对的答案。</em></h1>
            <p class="welcome__description">系统先判断问题需要确认什么，再按需查询业务数据或查找文档；遇到跨源问题，还会核对对象关系和适用条件。</p>
          </section>
          <section class="examples-section vm-reveal" aria-labelledby="examples-title">
            <div class="examples-heading"><div><span>按问题类型浏览</span><h2 id="examples-title">你可以这样提问</h2></div><p>点击示例只填入输入框，不会自动发送</p></div>
            <div class="example-row" aria-label="问数、问答与跨源问题示例">
              <article
                v-for="section in homeQuestionSections"
                :key="section.id"
                class="example-card vm-interact"
                :class="[section.tone, { 'example-card--featured': section.id === 'cross-source' }]"
                :aria-labelledby="`home-path-${section.id}`"
              >
                <div class="example-card__head">
                  <span class="example-card__icon vm-icon-shift"><AppIcon :name="section.icon" :size="20" /></span>
                  <div class="example-card__title"><h3 :id="`home-path-${section.id}`">{{ section.title }}</h3><small>{{ section.description }}</small></div>
                  <button
                    v-if="section.id === 'cross-source'"
                    type="button"
                    class="example-card__status example-card__status--retry"
                    :class="{ 'is-ready': homeSectionAvailable(section.id), 'is-waiting': crossSourceDemoStatus === 'checking' }"
                    :disabled="crossSourceDemoStatus === 'checking'"
                    aria-label="重新检查跨源演示数据"
                    title="重新检查独立跨源演示链路"
                    @click="refreshCrossSourceDemoStatus"
                  >{{ homeSectionStatus(section.id) }}<AppIcon name="retry" :size="11" /></button>
                  <span v-else class="example-card__status" :class="{ 'is-ready': homeSectionAvailable(section.id) }">{{ homeSectionStatus(section.id) }}</span>
                </div>
                <div class="example-card__questions">
                  <button
                    v-for="question in section.questions"
                    :key="question"
                    type="button"
                    class="example-card__question vm-press"
                    :disabled="conversation.isPending || !homeSectionAvailable(section.id)"
                    :title="homeSectionAvailable(section.id) ? '填入输入框，可修改后再发送' : homeSectionStatus(section.id)"
                    @click="useExample(question)"
                  >
                    <span>{{ question }}</span>
                    <AppIcon name="arrow" :size="15" />
                  </button>
                </div>
                <p class="example-card__scope">{{ section.scope }}</p>
                <span v-if="section.id === 'cross-source'" class="example-card__feature-tag"><AppIcon name="check" :size="12" />本项目重点能力</span>
              </article>
            </div>
          </section>
          <section class="source-scope" aria-labelledby="source-scope-title">
            <div class="source-scope__heading"><div><span>本机已知来源</span><h2 id="source-scope-title">当前资料范围</h2></div><button type="button" @click="showDatabase">查看来源目录<AppIcon name="arrow" :size="13" /></button></div>
            <div class="source-scope__items">
              <button class="source-scope__item" type="button" @click="showDatabase"><span class="source-scope__icon"><AppIcon name="database" :size="16" /></span><span><b>{{ databaseStatus?.label || '业务数据' }}</b><small>结构化数据 · {{ structuredScopeStatus }}</small></span><AppIcon class="source-scope__open" name="arrow" :size="13" /></button>
              <button class="source-scope__item" type="button" @click="showDatabase"><span class="source-scope__icon source-scope__icon--doc"><AppIcon name="document" :size="16" /></span><span><b>{{ unstructuredDatabaseStatus?.label || '文档资料' }}</b><small>文档检索 · {{ documentScopeStatus }}</small></span><AppIcon class="source-scope__open" name="arrow" :size="13" /></button>
              <button class="source-scope__item" type="button" @click="showDatabase"><span class="source-scope__icon source-scope__icon--cross"><AppIcon name="cross-source" :size="16" /></span><span><b>跨源演示库</b><small>独立 PostgreSQL · {{ crossSourceDemoScopeStatus }}</small></span><AppIcon class="source-scope__open" name="arrow" :size="13" /></button>
            </div>
          </section>
          <section class="welcome-flow" aria-labelledby="welcome-flow-title">
            <div class="welcome-flow__heading"><span>回答怎样形成</span><h2 id="welcome-flow-title">从问题，到可核对的答案</h2></div>
            <ol><li><b>识别</b><span>问题要确认哪些事实</span></li><li><b>查找</b><span>按需要查数据或文档</span></li><li><b>核对</b><span>复合问题检查关系与口径</span></li><li><b>回答</b><span>展示结论和本次来源</span></li></ol>
            <p>每轮实际执行的查询、检索与处理记录，以回答中显示的来源和处理详情为准。</p>
          </section>
        </div>
      </div>
      <Composer v-if="hasMessages" ref="composerRef" :disabled="conversation.isPending" :suggestions="activeSuggestions" :model-profile-id="conversation.selectedModelProfileId" :retrieval-mode="conversation.retrievalMode" :kag-ready="kagReady" @update:model-profile-id="conversation.setModelProfile($event)" @update:retrieval-mode="conversation.setRetrievalMode($event)" @submit="submit" @stop="conversation.stopWaiting()" />
      <Composer v-else ref="composerRef" class="welcome-composer" :disabled="conversation.isPending" :welcome="true" :model-profile-id="conversation.selectedModelProfileId" :retrieval-mode="conversation.retrievalMode" :kag-ready="kagReady" @update:model-profile-id="conversation.setModelProfile($event)" @update:retrieval-mode="conversation.setRetrievalMode($event)" @submit="submit" @stop="conversation.stopWaiting()" />
        </div>
        <section v-if="evidenceOpen" class="evidence vm-panel" aria-label="来源证据面板"><header class="evidence__head"><div><AppIcon name="link" :size="17" /><b>来源证据</b><span>核对原始出处</span></div><button class="icon-button close-button vm-press" aria-label="关闭证据面板" @click="evidenceOpen = false"><AppIcon name="close" :size="18" /></button></header><EvidencePanel :ref-item="conversation.activeRef" :sql="conversation.activeResponse?.sql ?? null" :data-source="conversation.activeResponse?.data_source ?? null" /></section>
        <button v-if="sidebarOpen || evidenceOpen" class="scrim" :class="{ 'scrim--sidebar': sidebarOpen }" aria-label="关闭浮层" @click="sidebarOpen = false; evidenceOpen = false" />
      </div>
    </section>
    <Teleport to="body">
      <Transition name="vm-dialog"><SettingsPanel v-if="settingsOpen" @close="settingsOpen = false" /></Transition>
      <Transition name="vm-dialog"><DatabasePanel v-if="databaseOpen" @close="databaseOpen = false" /></Transition>
    </Teleport>
    <Teleport to="body">
      <Transition name="vm-dialog"><div v-if="architectureOpen" class="architecture-backdrop" :inert="!architectureOpen" @click.self="architectureOpen = false"><section class="architecture-dialog" role="dialog" aria-modal="true" aria-labelledby="architecture-title"><header><div><span class="architecture-dialog__eyebrow">VERIMIND · SYSTEM MAP</span><h2 id="architecture-title">FEVA 事实—证据核验架构</h2></div><button class="icon-button close-button vm-press" aria-label="关闭架构导览" @click="architectureOpen = false"><AppIcon name="close" /></button></header><p class="architecture-dialog__intro">系统先判断问题要查数据库还是文档；需要跨源核对时，再拆分事实、核验对象与口径，并把结论关联到证据。单源问数或问答仍走各自路径；当前专用跨源流程用于工单—合同场景。</p><div class="architecture-tabs" role="tablist" aria-label="架构导览视图"><button role="tab" :aria-selected="architectureView === 'flow'" :class="{ active: architectureView === 'flow' }" @click="switchArchitectureView('flow')"><AppIcon name="arrow" :size="13" />流程演示</button><button role="tab" :aria-selected="architectureView === 'modules'" :class="{ active: architectureView === 'modules' }" @click="switchArchitectureView('modules')"><AppIcon name="sources" :size="13" />模块目录</button></div>
        <div v-if="architectureView === 'flow'" class="architecture-flowview" role="tabpanel">
          <ol class="feva-flow"><li v-for="(stage, index) in FEVA_FLOW" :key="stage.step" class="feva-flow__item" :class="{ 'is-reached': flowProgress > index, 'is-active': flowProgress === index + 1 }" :style="{ '--motion-order': index, '--stage-step': index }"><span class="feva-flow__index">{{ String(index + 1).padStart(2, '0') }}</span><div class="feva-flow__body"><b>{{ stage.step }}</b><small>{{ stage.desc }}</small><em>由 {{ stage.module }} 承担</em></div><span class="feva-flow__pulse" aria-hidden="true" /></li></ol>
          <div class="feva-outlets" :class="{ 'is-visible': flowProgress >= FEVA_FLOW.length }"><span class="feva-outlets__label">随后进入</span><span v-for="outlet in FEVA_OUTLETS" :key="outlet.name" class="feva-outlets__card"><AppIcon :name="outlet.icon" :size="14" /><b>{{ outlet.name }}</b><small>{{ outlet.label }}</small></span><span class="feva-outlets__card research"><AppIcon name="link" :size="14" /><b>KAG增强路线</b><small>用户手选·非默认</small></span></div>
          <p class="feva-flow__note">这条路径对工单—合同这类跨源复合问题成立。只有一句单源问题时，系统会直接进入下面的问数或问答链路，不经过前五个阶段。</p>
        </div>
        <div v-else class="architecture-module-grid" role="tabpanel"><article v-for="(module, index) in architectureModules" :key="module.name" class="architecture-module vm-reveal" :style="{ '--motion-order': Math.min(index, 4) }" :class="{ 'architecture-module--research': !module.wired, 'is-expanded': expandedModules.has(module.name) }"><button class="architecture-module__toggle vm-press" :aria-expanded="expandedModules.has(module.name)" @click="toggleModule(module.name)"><div class="architecture-module__head"><span class="architecture-module__icon" aria-hidden="true"><img :src="`/brand/icons/${module.icon}.svg`" alt="" /></span><div class="architecture-module__labels"><b>{{ module.name }}</b><span>{{ module.name === 'KAG增强路线' ? (kagReady ? '用户可选·已就绪' : '用户可选·服务未就绪') : module.status }}</span></div></div><h3>{{ module.title }}</h3><p>{{ module.description }}</p><span class="architecture-module__more">{{ expandedModules.has(module.name) ? '收起' : '展开详细' }}<AppIcon name="chevron" :size="12" /></span></button><div v-if="expandedModules.has(module.name)" class="architecture-module__detail vm-detail"><p>{{ module.detail }}</p><dl><div><dt>输入</dt><dd>{{ module.io.input }}</dd></div><div><dt>输出</dt><dd>{{ module.io.output }}</dd></div></dl><p class="architecture-module__wired" :class="{ 'is-off': !module.wired && !(module.name === 'KAG增强路线' && kagReady) }"><AppIcon :name="module.wired || (module.name === 'KAG增强路线' && kagReady) ? 'check' : 'warning'" :size="12" />{{ module.name === 'KAG增强路线' ? (kagReady ? '仅在用户选择 KAG 补充深度检索时启用；标准路径仍走 RAG' : 'KAG 服务未就绪；标准检索保持可用') : module.wired ? '已接入问答路径' : '当前未接入问答路径' }}</p></div></article></div>
        <p class="architecture-dialog__note">这些名称用于帮助理解项目中的实际模块和研究方向，不代表独立算法或额外验收结论。</p></section></div></Transition>
    </Teleport>
    <Teleport to="body">
      <Transition name="vm-dialog"><div v-if="guideOpen" class="guide-backdrop" :inert="!guideOpen" @click.self="closeGuide"><section class="guide" role="dialog" aria-modal="true" aria-labelledby="guide-title"><header><span class="guide__icon"><AppIcon name="help" :size="24" /></span><button class="icon-button close-button vm-press" aria-label="关闭使用指南" @click="closeGuide"><AppIcon name="close" /></button></header><p class="guide__eyebrow">第 {{ guideStepIndex + 1 }} 步 · 共 {{ GUIDE_STEPS.length }} 步</p><h2 id="guide-title">{{ GUIDE_STEPS[guideStepIndex]?.label }}</h2>
        <nav class="guide__dots" aria-label="引导进度">
          <button v-for="(step, index) in GUIDE_STEPS" :key="step.id" type="button" role="tab" :aria-selected="guideStep === step.id" :class="{ active: guideStep === step.id, done: index < guideStepIndex }" :disabled="index > guideStepIndex" :aria-label="`第 ${index + 1} 步：${step.label}`" @click="guideStep = step.id" />
        </nav>
        <div class="guide__panes" :class="`guide__panes--${guideStep}`">
        <div v-if="guideStep === 'what'" class="guide__pane" role="tabpanel">
          <p class="guide__lead">系统支持三类问题，并按问题选择对应的数据与文档路径：</p>
          <ul class="guide__kinds">
            <li><b>问数</b><span>用自然语言查询业务数据；答案可查看本次执行记录与数据来源。</span></li>
            <li><b>问答</b><span>从资料中查找条款与说明；引用可打开对应原文位置。</span></li>
            <li><b>跨源问题</b><span>先核对不同来源中的对象关系与适用条件，再组织结论和证据。</span></li>
          </ul>
          <p class="guide__note">事实卡展示本轮实际记录的事实状态、检查与证据关联，不表示答案中的每句话都已独立核验。超出数据与资料范围的问题会说明依据不足或请求补充条件。</p>
          <p class="guide__try">试一试：</p>
          <ul class="guide__examples">
            <li v-for="question in GUIDE_TRY_QUESTIONS" :key="question"><button type="button" @click="pickGuideQuestion(question)">{{ question }}<AppIcon name="arrow" :size="13" /></button></li>
            <li v-if="crossSourceDemoStatus === 'ready'"><button type="button" @click="pickGuideQuestion(CROSS_SOURCE_TRY_QUESTION)">{{ CROSS_SOURCE_TRY_QUESTION }}<AppIcon name="arrow" :size="13" /></button></li>
          </ul>
        </div>
        <div v-else-if="guideStep === 'how'" class="guide__pane" role="tabpanel">
          <ol class="guide__list">
            <li><b>问得具体：给出口径，答案才可核对</b><span>提供文档名、对象、指标与时间范围；问得太模糊系统会反问而不是硬猜，直接在输入框补充即可。</span></li>
            <li><b>查看事实卡与引用：核对本轮记录及其范围</b><span>事实卡说明哪些处理已执行、哪些检查有结果，以及证据是否关联；点击引用可打开文档原文或数据来源。它不替代逐句事实审查。</span></li>
            <li><b>展开处理详情，在同一会话里继续追问</b><span>「查看处理详情」展示实际工具调用链，每步输入输出与耗时都在。追问时代词、省略主语都能接上；答案下方的「可以接着问」点了直接发送。</span></li>
          </ol>
          <p class="guide__note">检索方式：标准检索默认使用项目 RAG。KAG 补充深度检索（RAG + KAG）是 Beta 测试路径，稳定性与效率尚未充分验证；仅在 KAG 服务就绪时可选，选择前会再次提示。KAG 只补充候选原文，不替代项目的答案与引用流程。</p>
        </div>
        <div v-else class="guide__pane" role="tabpanel">
          <p class="guide__lead">挑一句试试，或直接开始提问。完整的能力清单在输入框左侧的四角星里。</p>
          <ul class="guide__examples">
            <li v-for="question in GUIDE_START_QUESTIONS" :key="question"><button type="button" @click="pickGuideQuestion(question)">{{ question }}<AppIcon name="arrow" :size="13" /></button></li>
          </ul>
          <p class="guide__note">服务与数据状态看左下角；回答过程中输入框会变成停止按钮，随时可中止等待。会话保存在当前浏览器。</p>
        </div>
        </div>
        <div class="guide__footer"><span class="guide__skip-text">可随时从顶部“使用指南”再次打开</span><button class="guide__confirm vm-press" @click="nextGuideStep">{{ guideStepIndex === GUIDE_STEPS.length - 1 ? '开始提问' : '下一步' }}<AppIcon name="arrow" :size="17" /></button></div></section></div></Transition>
    </Teleport>
  </div>
</template>

<style scoped>
.workspace { --sidebar-width: 252px; display: grid; grid-template-columns: var(--sidebar-width) minmax(0, 1fr); height: 100vh; height: 100dvh; overflow: hidden; background: var(--bg-app); transition: grid-template-columns var(--motion-base) var(--motion-ease); }
.sidebar { position: relative; display: flex; flex-direction: column; min-width: 0; min-height: 0; padding: 25px 17px 18px; overflow: hidden; border-right: 1px solid var(--border-subtle); background: var(--bg-surface); transition: opacity var(--motion-fast) var(--motion-exit-ease), transform var(--motion-base) var(--motion-ease), padding var(--motion-base) var(--motion-ease), border-color var(--motion-fast); }
@media (min-width: 768px) {
  .workspace.sidebar-collapsed { --sidebar-width: 0px; }
  .workspace.sidebar-collapsed .sidebar { padding-right: 0; padding-left: 0; transform: translateX(-12px); border-right-color: transparent; opacity: 0; pointer-events: none; }
}
.sidebar::after { position: absolute; top: 18px; right: 0; bottom: 18px; width: 1px; background: linear-gradient(transparent, var(--border-subtle) 12%, var(--border-subtle) 88%, transparent); content: ''; }
.brand { display: flex; align-items: center; gap: 11px; margin: 0 9px 27px; }
.brand__mark { position: relative; display: block; width: 41px; height: 41px; flex: 0 0 41px; overflow: hidden; border-radius: 10px; box-shadow: var(--shadow-brand); }
.brand__mark img { display: block; width: 100%; height: 100%; }
.brand__mark::after { position: absolute; inset: -10%; pointer-events: none; clip-path: polygon(25% 0, 55% 0, 80% 100%, 50% 100%); background: rgba(255,255,255,.22); transform: translateX(-180%); content: ''; }
@media (hover: hover) { .brand:hover .brand__mark::after { animation: vm-blade var(--motion-sweep) var(--motion-ease) both; } }
.brand b { display: block; color: var(--text-heading); font-size: 25px; letter-spacing: .1em; line-height: 1.2; }
.brand b em { margin-left: 7px; color: var(--text-muted); font-size: 14px; font-style: normal; font-weight: 500; letter-spacing: .04em; }
.brand small { display: block; margin-top: 5px; color: var(--text-muted); font-size: 13px; letter-spacing: .02em; white-space: nowrap; }
.new-session { display: flex; align-items: center; gap: 9px; height: 46px; padding: 0 14px; border: 1px solid var(--border-brand); border-radius: 11px; color: var(--brand-primary); background: var(--brand-soft); font-weight: 650; text-align: left; transition: background .15s, border-color .15s, transform .15s; }
.new-session:hover { transform: translateY(-1px); border-color: var(--brand-primary-weak); background: var(--brand-soft-strong); }
.workspace-label { display: flex; align-items: center; gap: 10px; margin-top: 17px; padding: 13px 12px; border: 1px solid var(--border-subtle); border-radius: 10px; color: var(--text-secondary); background: var(--bg-subtle); font-size: 14px; font-weight: 600; }
.workspace-label__dot { width: 5px; height: 5px; margin-left: auto; border-radius: 50%; background: var(--brand-primary); }
.sidebar-heading { display: flex; align-items: center; justify-content: space-between; padding: 22px 10px 11px; color: var(--text-muted); font-size: 12px; letter-spacing: .06em; }
.text-button { padding: 0; border: 0; color: var(--text-muted); background: transparent; font-size: 13px; }
.text-button:hover { color: var(--brand-primary); }
nav { min-height: 0; overflow-y: auto; display: grid; gap: 5px; }
.session-item { display: flex; align-items: center; min-width: 0; border: 1px solid transparent; border-radius: 10px; transition: background .15s, border-color .15s; }
.session-item:has(.active) { background: var(--brand-soft); }
.session-item:has(.active) { border-color: var(--border-brand); }
.session-select { flex: 1; display: grid; gap: 4px; min-width: 0; padding: 10px 11px; border: 0; border-radius: 9px; color: var(--text-secondary); text-align: left; background: transparent; }
.session-select:hover { background: var(--bg-hover); }
.session-select.active, .session-select.selected { color: var(--brand-primary); background: var(--brand-soft); }
.session-title { display: flex; align-items: center; gap: 8px; min-width: 0; }
.session-title b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; font-weight: 500; }
.session-select small { margin-left: 23px; color: var(--text-muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.session-more, .session-check { display: grid; flex-shrink: 0; width: 27px; height: 30px; place-items: center; border: 0; border-radius: 7px; color: var(--text-muted); background: transparent; }
.session-more { margin-right: 4px; }
.session-more:hover, .session-more.open { color: var(--brand-primary); background: var(--bg-subtle); }
.session-check { color: transparent; font-size: 14px; }
.session-check.selected { color: var(--text-on-brand); background: var(--brand-primary); }
.session-menu { display: grid; width: 142px; padding: 5px; border: 1px solid var(--border-subtle); border-radius: 11px; box-shadow: var(--shadow-menu); background: var(--bg-elevated); }
.session-menu--floating { position: fixed; z-index: 100; }
.session-menu button { display: flex; align-items: center; gap: 8px; width: 100%; padding: 8px; border: 0; border-radius: 6px; color: var(--text-secondary); text-align: left; background: transparent; font-size: 14px; }
.session-menu button:hover { background: var(--bg-subtle); color: var(--brand-primary); }
.session-menu .danger { color: var(--state-error); }
.batch-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 10px 5px; font-size: 13px; }
.batch-actions span { margin-right: auto; color: var(--text-secondary); }
.batch-actions button { padding: 4px 7px; border: 1px solid var(--border-subtle); border-radius: 5px; background: var(--bg-surface); }
.sidebar-foot { margin-top: auto; padding-top: 22px; }
.sidebar-status { padding: 5px 8px 0; border: 1px solid var(--border-subtle); border-radius: 11px; background: var(--bg-subtle); }
.sidebar-user { display:flex; align-items:center; gap:10px; min-height:38px; padding:3px 5px 7px; border-bottom:1px solid var(--border-subtle); }
.sidebar-user__identity { display:grid; gap:2px; min-width:0; }
.sidebar-user__identity b { overflow:hidden; color:var(--text-primary); font-size:12px; font-weight:650; text-overflow:ellipsis; white-space:nowrap; }
.sidebar-user__identity small { color:var(--text-muted); font-size:10px; }
.sidebar-user__logout { margin-left:auto; padding:5px 8px; border:1px solid var(--border-subtle); border-radius:7px; color:var(--text-secondary); background:var(--bg-surface); font-size:11px; }
.sidebar-user__logout:hover { border-color:var(--border-brand); color:var(--brand-primary); background:var(--brand-soft); }
.sidebar-auth-error { margin:5px; color:var(--state-error); font-size:11px; }
.service-state { display: flex; align-items: center; gap: 8px; width: 100%; min-height: 32px; padding: 4px 5px; border: 0; border-radius: 7px; color: var(--text-secondary); text-align: left; background: transparent; font-size: 12px; transition: color var(--motion-fast) var(--motion-ease), background-color var(--motion-fast) var(--motion-ease); }
.service-state:hover { color: var(--brand-primary); background: var(--brand-soft); }
.service-state:focus-visible { outline: 2px solid var(--brand-primary); outline-offset: 1px; }
.service-state > svg { margin-left: auto; color: var(--text-muted); transition: color var(--motion-fast) var(--motion-ease); }
.service-state:hover > svg { color: var(--brand-primary); }
.service-state i, .working-state i { width: 7px; height: 7px; flex: 0 0 7px; border-radius: 50%; background: var(--state-neutral); }
.service-state.online i { background: var(--accent-teal); }
.service-state.offline i { background: var(--state-error); }
.database-entry { display: flex; align-items: center; gap: 9px; width: 100%; min-height: 49px; margin: 5px 0 7px; padding: 6px 8px; border: 1px solid var(--border-subtle); border-radius: 9px; color: var(--text-secondary); text-align: left; background: var(--bg-surface); transition: border-color var(--motion-fast) var(--motion-ease), background-color var(--motion-fast) var(--motion-ease), color var(--motion-fast) var(--motion-ease), transform var(--motion-fast) var(--motion-ease); }
.database-entry:hover { border-color: var(--border-brand); color: var(--brand-primary); background: var(--brand-soft); }
.database-entry:focus-visible { outline: 2px solid var(--brand-primary); outline-offset: 2px; }
.database-entry__icon { display: grid; width: 34px; height: 34px; flex: 0 0 34px; place-items: center; border-radius: 8px; background: var(--brand-soft); }
.database-entry__icon img { display: block; width: 28px; height: 28px; }
.database-entry__copy { display: grid; gap: 2px; min-width: 0; }
.database-entry__copy b { color: var(--text-primary); font-size: 13px; font-weight: 600; }
.database-entry__copy small { overflow: hidden; color: var(--text-secondary); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.database-entry__dot { width: 7px; height: 7px; flex: 0 0 7px; margin-left: auto; border-radius: 50%; background: var(--state-neutral); }
.database-entry__dot.connected { background: var(--accent-teal); }
.database-entry__dot.memory { background: var(--brand-primary); }
.database-entry__dot.not_used { background: var(--state-warning); }
.database-entry__dot.unavailable, .database-entry__dot.catalog_mismatch { background: var(--state-error); }
/* 用 --text-secondary 而非 --text-muted：这句是「会话仅保存在此浏览器」，
   属安全/信任类提示，用户需要真读进去。--text-muted 在这块底色上只有 3.44，
   低于 WCAG AA 的 4.5；调深 --text-muted 又会和 --text-secondary 撞色，
   所以问题不在 token 值，在这句不该用装饰性小字的颜色。 */
.local-note { display: flex; align-items: center; gap: 7px; min-height: 30px; margin: 0; padding: 6px 5px 7px; border-top: 1px solid var(--border-subtle); color: var(--text-secondary); font-size: 12px; }
.conversation { position: relative; display: flex; flex-direction: column; min-width: 0; min-height: 0; background: var(--bg-app); }
.topbar { position: relative; z-index: 20; display: flex; align-items: center; justify-content: flex-start; flex-shrink: 0; min-width: 0; min-height: 70px; padding: 0 clamp(20px, 3vw, 42px); border-bottom: 1px solid var(--border-subtle); background: var(--bg-glass); backdrop-filter: blur(8px); }
.conversation__body { position: relative; display: grid; flex: 1; min-width: 0; min-height: 0; overflow: hidden; grid-template-columns: minmax(0, 1fr); grid-template-rows: minmax(0, 1fr); }
.conversation__body--evidence-open { grid-template-columns: minmax(0, 1fr) clamp(390px, 36vw, 570px); }
.conversation__content { display: flex; flex-direction: column; min-width: 0; min-height: 0; overflow: hidden; background: var(--bg-app); }
.breadcrumb { display: flex; flex: 0 0 auto; align-items: center; gap: 10px; color: var(--text-muted); font-size: 14px; }
.breadcrumb b { color: var(--text-secondary); font-weight: 500; }
.session-context { display: flex; flex: 1 1 0; align-items: center; gap: 9px; min-width: 0; margin-left: 14px; padding-left: 14px; border-left: 1px solid var(--border-subtle); }
.session-context__title { flex: 1 1 auto; min-width: 0; overflow: hidden; margin: 0; color: var(--text-heading); font-size: 14px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
.topbar__actions { display: flex; flex: 0 0 auto; align-items: center; gap: 8px; margin-left: auto; }
.architecture-button { display: flex; align-items: center; gap: 7px; padding: 8px 11px; border: 1px solid var(--border-brand); border-radius: 9px; color: var(--brand-primary); background: var(--brand-soft); font-size: 13px; font-weight: 650; }
.architecture-button img { display: block; width: 18px; height: 18px; }
button.architecture-button:hover { background: var(--brand-soft-strong); }
.theme-button { display: grid; width: 34px; height: 34px; place-items: center; padding: 0; border: 1px solid var(--border-subtle); border-radius: 9px; color: var(--text-secondary); background: var(--bg-surface); transition: color .15s, background .15s, border-color .15s; }
.theme-button:hover { color: var(--brand-primary); border-color: var(--border-brand); background: var(--brand-soft); }
.guide-button, .evidence-button, .settings-button { display: flex; align-items: center; gap: 7px; padding: 8px 10px; border: 0; border-radius: 9px; color: var(--text-secondary); background: transparent; font-size: 12px; }
.guide-button:hover, .evidence-button:hover, .settings-button:hover { background: var(--bg-subtle); }
.settings-button:hover { color: var(--brand-primary); }
.evidence-button { border: 1px solid var(--border-subtle); background: var(--bg-surface); }
.evidence-button.active { color: var(--brand-primary); background: var(--brand-soft); border-color: var(--border-brand); }
.evidence-button small { display: grid; min-width: 18px; height: 18px; padding: 0 4px; place-items: center; border-radius: 5px; color: var(--brand-primary); background: var(--brand-soft); font-size: 11px; }
.storage-warning { padding: 8px 20px; color: var(--state-warning); background: var(--state-warning-soft); font-size: 13px; }
.welcome-scroll { display: flex; flex: 1; min-height: 0; overflow-y: auto; padding: 24px clamp(20px, 4vw, 54px) 18px; }
.welcome { width: 100%; max-width: 940px; margin: 0 auto; padding: 10px 0 26px; }
.welcome__intro { min-width: 0; padding: 0; }
.welcome__eyebrow { display: flex; align-items: center; gap: 9px; margin-bottom: 12px; color: var(--text-secondary); font-size: 12px; letter-spacing: .035em; }
.welcome__eyebrow > span { width: 7px; height: 7px; border-radius: 2px; background: var(--accent-teal); box-shadow: 0 0 0 4px var(--accent-teal-soft); }
.welcome__eyebrow b { color: var(--text-muted); font-weight: 500; }
.welcome h1 { margin: 0; color: var(--text-heading); font-size: clamp(30px, calc(2.1vw + 16px), 42px); font-weight: 680; line-height: 1.25; letter-spacing: -.035em; }
.welcome h1 em { color: var(--brand-primary); font-style: normal; }
.welcome__description { max-width: 740px; margin: 11px 0 0; color: var(--text-secondary); font-size: 14px; line-height: 1.8; }
.welcome-composer { position: relative; z-index: 2; border-top: 1px solid var(--border-subtle); background: var(--bg-app); }
.examples-section { margin-top: 18px; }
.examples-heading { display: flex; align-items: end; justify-content: space-between; gap: 12px; margin-bottom: 13px; }
.examples-heading > div > span { display: block; margin-bottom: 3px; color: var(--brand-primary); font-size: 10px; font-weight: 700; letter-spacing: .08em; }
.examples-heading h2 { margin: 0; color: var(--text-heading); font-size: 17px; font-weight: 650; }
.examples-heading p { margin: 0 2px 2px; color: var(--text-muted); font-size: 11px; }
.example-row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 11px; }
.example-card { position: relative; display: flex; flex-direction: column; gap: 11px; min-width: 0; padding: 13px 14px 12px; overflow: hidden; border: 1px solid var(--border-subtle); border-radius: 13px; color: var(--text-primary); background: var(--bg-surface); box-shadow: var(--shadow-card); }
.example-card__head { display: flex; align-items: center; gap: 9px; min-width: 0; }
.example-card__icon { display: grid; width: 33px; height: 33px; flex: 0 0 33px; place-items: center; border: 1px solid var(--border-subtle); border-radius: 9px; color: var(--accent-teal); background: var(--accent-teal-soft); }
.example-card.blue .example-card__icon { color: var(--brand-primary); background: var(--brand-soft); }
.example-card.cross .example-card__icon { color: var(--brand-primary); border-color: color-mix(in srgb, var(--brand-primary) 24%, var(--border-subtle)); background: color-mix(in srgb, var(--brand-primary) 9%, var(--bg-surface)); }
.example-card__title { min-width: 0; flex: 1; }
.example-card__title h3 { margin: 0; color: var(--text-heading); font-size: 14px; font-weight: 650; }
.example-card__title small { display: block; margin-top: 3px; color: var(--text-muted); font-size: 11px; line-height: 1.45; }
.example-card__status { flex: 0 0 auto; padding: 4px 7px; border: 1px solid var(--border-subtle); border-radius: 999px; color: var(--text-muted); background: var(--bg-subtle); font-size: 10px; line-height: 1.2; }
.example-card__status.is-ready { color: var(--state-success); border-color: color-mix(in srgb, var(--state-success) 25%, var(--border-subtle)); background: var(--state-success-soft); }
.example-card__status.is-waiting { color: var(--brand-primary); }
.example-card__status--retry { display: inline-flex; align-items: center; gap: 4px; cursor: pointer; transition: border-color var(--motion-fast), background-color var(--motion-fast), color var(--motion-fast); }
.example-card__status--retry:hover:not(:disabled), .example-card__status--retry:focus-visible:not(:disabled) { border-color: var(--border-brand); color: var(--brand-primary); outline: none; }
.example-card__status--retry:disabled { cursor: progress; }
.example-card__questions { display: grid; gap: 6px; }
.example-card__question { display: flex; align-items: center; justify-content: space-between; gap: 9px; width: 100%; min-width: 0; padding: 8px 9px 8px 10px; border: 1px solid var(--border-subtle); border-radius: 9px; color: var(--text-secondary); text-align: left; background: var(--bg-subtle); font-size: 12px; line-height: 1.45; cursor: pointer; transition: border-color var(--motion-fast), color var(--motion-fast), background-color var(--motion-fast); }
.example-card__question span { min-width: 0; }
.example-card__question svg { flex: 0 0 auto; color: var(--text-faint); }
.example-card__question:hover:not(:disabled), .example-card__question:focus-visible:not(:disabled) { border-color: var(--border-brand); color: var(--brand-primary); background: var(--brand-soft); outline: none; }
.example-card__question:disabled { color: var(--text-muted); background: var(--bg-subtle); cursor: not-allowed; opacity: .72; }
.example-card__scope { margin: auto 0 0; color: var(--text-faint); font-size: 10.5px; line-height: 1.45; }
.example-card--featured { grid-column: 1 / -1; display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr); align-items: center; gap: 9px 20px; padding: 15px 17px; border-color: color-mix(in srgb, var(--brand-primary) 32%, var(--border-subtle)); background: linear-gradient(105deg, color-mix(in srgb, var(--brand-primary) 7%, var(--bg-surface)), var(--bg-surface) 68%); }
.example-card--featured::before { position: absolute; inset: 0 auto 0 0; width: 3px; background: var(--brand-primary); content: ''; }
.example-card--featured .example-card__head { grid-column: 1; }
.example-card--featured .example-card__questions { grid-column: 2; grid-row: 1 / span 2; }
.example-card--featured .example-card__question { min-height: 52px; border-color: color-mix(in srgb, var(--brand-primary) 23%, var(--border-subtle)); background: color-mix(in srgb, var(--brand-primary) 5%, var(--bg-surface)); font-weight: 550; }
.example-card--featured .example-card__scope { grid-column: 1; margin-left: 42px; }
.example-card__feature-tag { position: static; grid-column: 1; justify-self: start; display: inline-flex; align-items: center; gap: 4px; margin-left: 42px; color: var(--brand-primary); font-size: 10px; font-weight: 650; }
.example-card__feature-tag svg { color: var(--accent-teal); }
.source-scope, .welcome-flow { margin-top: 19px; padding-top: 16px; border-top: 1px solid var(--border-subtle); }
.source-scope__heading, .welcome-flow__heading { display: flex; align-items: end; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.source-scope__heading > div > span, .welcome-flow__heading > span { display: block; margin-bottom: 3px; color: var(--brand-primary); font-size: 10px; font-weight: 700; letter-spacing: .08em; }
.source-scope__heading h2, .welcome-flow__heading h2 { margin: 0; color: var(--text-heading); font-size: 15px; font-weight: 650; }
.source-scope__heading button { display: inline-flex; align-items: center; gap: 5px; padding: 4px 0; border: 0; color: var(--brand-primary); background: transparent; font-size: 11px; }
.source-scope__items { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 9px; }
.source-scope__item { display: flex; align-items: center; gap: 9px; min-width: 0; padding: 9px 11px; border: 1px solid var(--border-subtle); border-radius: 10px; color: var(--text-primary); text-align: left; background: var(--bg-surface); transition: border-color var(--motion-fast), background-color var(--motion-fast); }
.source-scope__item:hover, .source-scope__item:focus-visible { border-color: var(--border-brand); background: var(--brand-soft); }
.source-scope__icon { display: grid; width: 30px; height: 30px; flex: 0 0 30px; place-items: center; border-radius: 8px; color: var(--brand-primary); background: var(--brand-soft); }
.source-scope__icon--doc { color: var(--accent-teal); background: var(--accent-teal-soft); }
.source-scope__icon--cross { color: var(--brand-primary); background: var(--brand-soft); }
.source-scope__item > span:nth-child(2) { display: grid; gap: 2px; min-width: 0; }
.source-scope__item b { overflow: hidden; color: var(--text-heading); font-size: 11.5px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.source-scope__item small { color: var(--text-muted); font-size: 10.5px; }
.source-scope__open { margin-left: auto; color: var(--text-faint); }
.welcome-flow ol { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0; margin: 0; padding: 0; list-style: none; }
.welcome-flow li { position: relative; display: grid; gap: 4px; min-width: 0; padding: 0 10px 0 13px; border-left: 2px solid var(--border-brand); }
.welcome-flow li:first-child { padding-left: 0; border-left: 0; }
.welcome-flow li b { color: var(--text-heading); font-size: 12px; }
.welcome-flow li span { color: var(--text-muted); font-size: 10.5px; line-height: 1.55; }
.welcome-flow > p { margin: 9px 0 0; color: var(--text-faint); font-size: 10.5px; line-height: 1.5; }
.evidence { display: flex; flex-direction: column; min-width: 0; min-height: 0; border-left: 1px solid var(--border-subtle); background: var(--bg-surface); }
.evidence__head { display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; gap: 8px; height: 68px; padding: 0 18px; border-bottom: 1px solid var(--border-subtle); }
.evidence__head > div { display: flex; align-items: center; gap: 8px; color: var(--text-secondary); font-size: 14px; }
.evidence__head span { margin-left: 4px; color: var(--text-muted); font-size: 12px; }
.icon-button { display: grid; width: 30px; height: 30px; flex-shrink: 0; place-items: center; padding: 0; border: 0; border-radius: 7px; color: var(--text-secondary); background: transparent; }
.icon-button:hover { background: var(--bg-subtle); }
.sidebar-toggle { display: grid; margin-left: -18px; margin-right: 10px; }
.sidebar-toggle__icon { transition: transform var(--motion-fast) var(--motion-ease); }
.sidebar-toggle__icon--collapsed { transform: rotateY(180deg); }
.scrim { display: none; }
.guide-backdrop { position: fixed; inset: 0; z-index: 200; display: grid; place-items: center; padding: 22px; background: var(--scrim); backdrop-filter: blur(3px); }
.guide { width: min(560px, 100%); max-height: 90dvh; overflow: auto; padding: 30px; border-radius: 20px; background: var(--bg-elevated); box-shadow: var(--shadow-guide); }
.guide header { display: flex; align-items: center; justify-content: space-between; }
.guide__icon { display: grid; width: 46px; height: 46px; place-items: center; color: var(--brand-primary); border-radius: 13px; background: var(--brand-soft); }
.guide h2 { margin: 20px 0 10px; font-size: 24px; color: var(--text-heading); }
.guide p { color: var(--text-secondary); font-size: 15px; }
/* 顺序引导：步骤文字 + 圆点进度条，三步定高一致，切换时淡入 + 轻微上移 */
.guide__eyebrow { margin: 18px 0 0; color: var(--brand-primary); font-size: 12px; font-weight: 700; letter-spacing: .1em; }
.guide h2 { margin: 6px 0 0; font-size: 24px; color: var(--text-heading); }
.guide__dots { display: flex; gap: 8px; margin: 13px 0 4px; }
.guide__dots button { width: 9px; height: 9px; padding: 0; border: 0; border-radius: 50%; background: var(--border-strong); cursor: pointer; transition: background var(--motion-fast) var(--motion-ease), transform var(--motion-fast) var(--motion-ease); }
.guide__dots button.active { background: var(--brand-primary); transform: scale(1.25); }
.guide__dots button.done { background: var(--brand-primary-weak); }
.guide__dots button:disabled { cursor: default; }
/* 页签容器定高（取最高一页），三个页签大小一致；窄屏内容流式变高时回退 auto */
.guide__panes { position: relative; min-height: 430px; }
.guide__pane { min-height: 0; animation: guide-pane-in var(--motion-slow) var(--motion-ease); }
.guide__pane > * { animation: none; }
@keyframes guide-pane-in {
  from { opacity: 0; transform: translateY(7px); }
  to { opacity: 1; transform: translateY(0); }
}
@media (prefers-reduced-motion: reduce) { .guide__pane { animation: none; } }
.guide__lead { margin: 14px 0 12px; font-size: 14.5px; }
.guide__kinds { display: grid; gap: 10px; margin: 0; padding: 0; list-style: none; }
.guide__kinds li { padding: 11px 13px; border: 1px solid var(--border-subtle); border-radius: 11px; background: var(--bg-surface); }
.guide__kinds b { display: block; color: var(--brand-primary); font-size: 14.5px; }
.guide__kinds span { display: block; margin-top: 3px; color: var(--text-secondary); font-size: 13.5px; line-height: 1.6; }
.guide__try { margin: 14px 0 8px; color: var(--text-muted); font-size: 13px; font-weight: 600; }
.guide__examples { display: flex; flex-direction: column; gap: 7px; margin: 0 0 4px; padding: 0; list-style: none; }
.guide__examples button { display: flex; align-items: center; justify-content: space-between; gap: 10px; width: 100%; padding: 9px 13px; border: 1px solid var(--border-subtle); border-radius: 9px; color: var(--text-heading); background: var(--bg-surface); font-size: 14px; text-align: left; cursor: pointer; transition: border-color var(--motion-fast) var(--motion-ease), background var(--motion-fast) var(--motion-ease); }
.guide__examples button:hover { border-color: var(--border-brand); background: var(--brand-soft); color: var(--brand-primary); }
.guide ol { display: grid; gap: 18px; margin: 25px 0; padding-left: 22px; color: var(--brand-primary); }
.guide li { padding-left: 5px; font-size: 15px; }
.guide li b { display: block; color: var(--text-primary); margin-bottom: 3px; }
.guide li span { color: var(--text-secondary); font-size: 14px; }
.guide .guide__note { padding-top: 16px; border-top: 1px solid var(--border-subtle); color: var(--text-muted); font-size: 13px; }
.guide__confirm { display: flex; align-items: center; justify-content: center; gap: 12px; flex: 1; margin-top: 20px; padding: 11px; border: 0; border-radius: 9px; color: var(--text-on-brand); background: var(--brand-primary); }
/* 底部固定操作区：指南可随时关闭，也可按步骤阅读。 */
.guide__footer { display: flex; align-items: center; gap: 14px; }
.guide__skip-text { margin-top: 20px; color: var(--text-faint); font-size: 12.5px; }
.architecture-backdrop { position: fixed; inset: 0; z-index: 210; display: grid; place-items: center; padding: 22px; background: var(--scrim); backdrop-filter: blur(3px); }
.architecture-dialog { width: min(850px, 100%); max-height: 90dvh; overflow: auto; padding: 28px; border: 1px solid var(--border-subtle); border-radius: 18px; background: var(--bg-elevated); box-shadow: var(--shadow-guide); }
.architecture-dialog > header { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; }
.architecture-dialog__eyebrow { color: var(--brand-primary); font-size: 11px; font-weight: 700; letter-spacing: .13em; }
.architecture-dialog h2 { margin: 8px 0 0; color: var(--text-heading); font-size: 22px; }
.architecture-dialog__intro { max-width: 760px; margin: 13px 0 19px; color: var(--text-secondary); font-size: 14px; line-height: 1.8; }
.architecture-tabs { display: flex; gap: 6px; margin: 0 0 16px; padding: 4px; border: 1px solid var(--border-subtle); border-radius: 10px; background: var(--bg-subtle); }
.architecture-tabs button { display: inline-flex; align-items: center; gap: 6px; padding: 7px 14px; border: 0; border-radius: 7px; color: var(--text-secondary); background: transparent; font-size: 13px; font-weight: 600; transition: color var(--motion-fast), background-color var(--motion-fast); }
.architecture-tabs button:hover { color: var(--brand-primary); }
.architecture-tabs button.active { color: var(--brand-primary); background: var(--bg-surface); box-shadow: var(--shadow-card); }

/* ---- 流程演示 ---- */
.architecture-flowview { display: grid; gap: 0; }
.feva-flow { display: grid; gap: 0; margin: 0; padding: 0; list-style: none; }
.feva-flow__item { position: relative; display: flex; align-items: flex-start; gap: 12px; padding: 9px 12px 9px 0; opacity: .42; transition: opacity var(--motion-base) var(--motion-ease), transform var(--motion-base) var(--motion-ease); }
/*
 */
.feva-flow__item.is-reached { opacity: 1; }
.feva-flow__item.is-active { transform: translateX(3px); }
.feva-flow__item::before { position: absolute; top: 34px; bottom: -2px; left: 15px; width: 1px; background: var(--border-subtle); content: ''; }
.feva-flow__item:last-child::before { display: none; }
.feva-flow__item.is-reached::before { background: var(--border-brand); }
.feva-flow__index { position: relative; z-index: 1; display: grid; width: 31px; height: 31px; flex: 0 0 31px; place-items: center; border: 1px solid var(--border-subtle); border-radius: 9px; color: var(--text-muted); background: var(--bg-surface); font-size: 11px; font-weight: 700; font-variant-numeric: tabular-nums; transition: color var(--motion-base), border-color var(--motion-base), background-color var(--motion-base); }
.feva-flow__item.is-reached .feva-flow__index { border-color: var(--border-brand); color: var(--brand-primary); background: var(--brand-soft); }
.feva-flow__body { display: grid; gap: 3px; min-width: 0; padding-top: 2px; }
.feva-flow__body b { color: var(--text-heading); font-size: 15px; }
.feva-flow__body small { color: var(--text-secondary); font-size: 13px; line-height: 1.65; }
.feva-flow__body em { color: var(--text-muted); font-size: 11.5px; font-style: normal; }
.feva-flow__pulse { display: none; }
.feva-flow__item.is-active .feva-flow__pulse { display: block; width: 7px; height: 7px; flex: 0 0 7px; margin: 12px 0 0 auto; border-radius: 50%; background: var(--accent-teal); animation: feva-pulse 1.05s var(--motion-ease) infinite; }
@keyframes feva-pulse { 0%, 100% { opacity: .35; transform: scale(.72); } 50% { opacity: 1; transform: scale(1); } }

.feva-outlets { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; margin: 4px 0 0; padding: 13px 0 0; border-top: 1px dashed var(--border-subtle); opacity: 0; transition: opacity var(--motion-enter) var(--motion-ease); }
.feva-outlets.is-visible { opacity: 1; }
.feva-outlets__label { color: var(--text-muted); font-size: 12px; }
.feva-outlets__card { display: inline-flex; align-items: baseline; gap: 6px; padding: 7px 11px; border: 1px solid var(--border-brand); border-radius: 8px; color: var(--brand-primary); background: var(--brand-soft); font-size: 12px; }
.feva-outlets__card b { font-size: 13px; }
.feva-outlets__card small { color: var(--text-secondary); font-size: 11.5px; }
.feva-outlets__card.research { border-style: dashed; border-color: var(--border-subtle); color: var(--text-muted); background: var(--bg-subtle); }
.feva-outlets__card.research b { color: var(--text-secondary); }

.feva-flow__note { margin: 14px 0 0; color: var(--text-muted); font-size: 11.5px; line-height: 1.75; }

/* ---- 模块目录 ---- */
.architecture-module-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.architecture-module { padding: 13px 14px; border: 1px solid var(--border-subtle); border-radius: 10px; background: var(--bg-surface); transition: border-color var(--motion-fast); }
.architecture-module.is-expanded { border-color: var(--border-brand); }
.architecture-module__toggle { display: block; width: 100%; padding: 0; border: 0; background: transparent; text-align: left; }
.architecture-module__head { display: flex; align-items: center; gap: 10px; min-height: 36px; }
.architecture-module__icon { display: grid; width: 40px; height: 40px; flex: 0 0 40px; place-items: center; border: 1px solid var(--border-subtle); border-radius: 12px; background: var(--brand-soft); box-shadow: inset 0 1px 0 var(--bg-surface); }
.architecture-module__icon img { display: block; width: 29px; height: 29px; }
.architecture-module__labels { display: grid; gap: 3px; min-width: 0; }
.architecture-module__labels b { color: var(--brand-primary); font-size: 13px; }
.architecture-module__labels span { color: var(--text-muted); font-size: 11px; }
.architecture-module h3 { margin: 8px 0 4px; color: var(--text-heading); font-size: 14px; }
.architecture-module p { margin: 0; color: var(--text-secondary); font-size: 12px; line-height: 1.7; }
.architecture-module--research { border-style: dashed; background: var(--bg-subtle); }
.architecture-module--research .architecture-module__icon { border-color: var(--research-icon-border); background: var(--research-icon-bg); }
.architecture-module--research .architecture-module__labels b { color: var(--text-secondary); }
.architecture-module__more { display: inline-flex; align-items: center; gap: 4px; margin-top: 9px; color: var(--brand-primary); font-size: 12px; }
.architecture-module__toggle:hover .architecture-module__more { text-decoration: underline; }
.architecture-module__detail { margin-top: 11px; padding-top: 11px; border-top: 1px solid var(--border-subtle); }
.architecture-module__concept { display: inline-flex; margin-bottom: 8px; padding: 4px 8px; border: 1px solid var(--border-brand); border-radius: 7px; color: var(--brand-primary); background: var(--brand-soft); font-size: 11.5px; font-weight: 650; }
.architecture-module__detail > p { margin: 0; color: var(--text-secondary); font-size: 12.5px; line-height: 1.8; }
.architecture-module__detail dl { display: grid; gap: 6px; margin: 11px 0 0; }
.architecture-module__detail dl > div { display: grid; grid-template-columns: 38px minmax(0, 1fr); gap: 8px; align-items: baseline; }
.architecture-module__detail dt { color: var(--text-muted); font-size: 11.5px; }
.architecture-module__detail dd { margin: 0; color: var(--text-secondary); font-size: 12px; line-height: 1.65; }
.architecture-module__wired { display: flex; align-items: center; gap: 5px; margin: 11px 0 0; color: var(--accent-teal); font-size: 11.5px; }
.architecture-module__wired.is-off { color: var(--state-warning); }
.architecture-dialog__note { margin: 15px 0 0; color: var(--text-muted); font-size: 12px; line-height: 1.7; }
@media (max-width: 1279px) {
  .workspace { --sidebar-width: 218px; }
  .topbar { padding-right: 16px; padding-left: 16px; }
  .conversation__body--evidence-open { grid-template-columns: minmax(0, 1fr); }
  .evidence { position: absolute; top: 0; right: 0; bottom: 0; z-index: 40; width: min(530px, 65vw); box-shadow: var(--shadow-panel); }
  .scrim { position: absolute; inset: 0; z-index: 30; display: block; border: 0; background: var(--scrim); }
  .session-context { margin-left: 10px; padding-left: 10px; }
  .session-context { gap: 7px; }
  .topbar__actions { gap: 6px; }
  .architecture-button, .guide-button, .settings-button, .evidence-button { padding: 6px; }
  .architecture-button span, .guide-button span, .settings-button span, .evidence-button span { display: none; }
  .architecture-button { gap: 0; }
}
@media (max-width: 980px) and (min-width: 768px) {
  .source-scope__items { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .breadcrumb { gap: 5px; }
  .breadcrumb > span, .breadcrumb > svg { display: none; }
  .session-context { margin-left: 8px; padding-left: 8px; }
  .topbar { padding-right: 12px; padding-left: 12px; }
  .welcome { max-width: 760px; }
  .welcome-scroll { padding-top: 24px; }
}
@media (max-width: 767px) {
  .workspace { display: flex; }
  .conversation { width: 100%; }
  .conversation__body { flex: 1; width: 100%; overflow: visible; }
  .sidebar { position: fixed; inset: 0 auto 0 0; z-index: 60; width: 260px; transform: translateX(-100%); transition: transform var(--motion-base) var(--motion-ease); box-shadow: var(--shadow-drawer); }
  .sidebar-open .sidebar { transform: translateX(0); }
  .sidebar-open .topbar { z-index: auto; backdrop-filter: none; }
  .sidebar-open .sidebar-toggle { position: fixed; top: 30px; left: 230px; z-index: 80; margin: 0; }
  .brand { gap: 8px; margin-left: 3px; margin-right: 0; }
  .sidebar-toggle { margin-left: 0; margin-right: 10px; }
  .topbar { height: auto; min-height: 88px; flex-wrap: wrap; align-content: center; row-gap: 5px; padding: 8px 16px; }
  .breadcrumb { gap: 5px; font-size: 13px; }
  .breadcrumb > span, .breadcrumb > svg { display: none; }
  .session-context { flex: 1 1 0; gap: 0; margin-left: 7px; padding-left: 8px; }
  .session-context__title { font-size: 12px; }
  .topbar__actions { flex: 0 0 100%; justify-content: flex-end; gap: 6px; margin-left: 0; }
  .architecture-button { padding: 6px; }
  .architecture-button span { display: none; }
  .guide-button { padding: 6px; }
  .guide-button span { display: none; }
  .settings-button { padding: 6px; }
  .settings-button span { display: none; }
  .evidence-button { font-size: 12px; }
  .welcome-scroll { padding: 24px 22px 10px; }
  .welcome { padding: 10px 0; }
  .welcome__intro { padding: 0; }
  .welcome__eyebrow { font-size: 11px; margin-bottom: 10px; }
  .welcome h1 { font-size: clamp(29px, 8vw, 36px); }
  .welcome__description { margin: 10px 0 0; font-size: 13px; }
  .source-scope__items { grid-template-columns: 1fr; }
  .welcome-flow ol { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 11px 8px; }
  .welcome-flow li, .welcome-flow li:first-child { min-height: 42px; padding: 2px 5px 2px 9px; border-left: 2px solid var(--border-brand); }
  .welcome-flow li span { font-size: 10px; }
  .examples-section { margin-top: 19px; }
  .examples-heading { align-items: end; margin-bottom: 10px; }
  .examples-heading h2 { font-size: 16px; }
  .examples-heading p { font-size: 11px; }
  .architecture-module-grid { grid-template-columns: 1fr; }
  .architecture-tabs { margin-bottom: 13px; }
  .architecture-tabs button { flex: 1; justify-content: center; padding: 7px 8px; font-size: 12px; }
  .feva-flow__item { gap: 10px; }
  .feva-flow__body b { font-size: 14px; }
  .feva-flow__body small { font-size: 12px; }
  .feva-outlets { align-items: stretch; }
  .feva-outlets__label { width: 100%; }
  .feva-outlets__card { flex: 1 1 auto; justify-content: center; }
  .architecture-dialog { padding: 20px; }
  /* 窄屏字母表改成「字母 + 词」一行、说明占下一行：三列并排会把说明挤成窄条。 */
  .feva-brief__block { padding: 13px 14px; }
  .feva-brief__letters li { grid-template-columns: 19px 1fr; row-gap: 4px; }
  .feva-brief__note { grid-column: 2; }
  .example-row { grid-template-columns: 1fr; gap: 9px; }
  .example-card { gap: 9px; padding: 12px; }
  .example-card--featured { grid-column: auto; grid-template-columns: minmax(0, 1fr); gap: 9px; padding: 13px 12px 12px 14px; }
  .example-card--featured .example-card__head, .example-card--featured .example-card__questions, .example-card--featured .example-card__scope { grid-column: 1; grid-row: auto; }
  .example-card--featured .example-card__scope { margin-left: 42px; padding-right: 2px; }
  .example-card__feature-tag { justify-self: start; margin-left: 42px; font-size: 9px; }
  .example-card__question { padding: 8px; font-size: 11.5px; }
  .example-card__status { max-width: 105px; white-space: nowrap; }
  .examples-heading p { max-width: 118px; text-align: right; }
  .conversation__head { padding: 17px 20px 12px; gap: 10px; }
  .conversation__head h1 { font-size: 16px; }
  .working-state { font-size: 12px; }
  .evidence { position: fixed; inset: 8dvh 0 0; width: 100%; border-radius: 16px 16px 0 0; }
  .scrim { position: fixed; inset: 60px 0 0; }
  .scrim--sidebar { inset: 0; }
  .scrim--sidebar { z-index: 50; }
  .guide { padding: 23px; }
}
</style>
