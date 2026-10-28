<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import ChatThread from '../components/ChatThread.vue'
import Composer from '../components/Composer.vue'
import EvidencePanel from '../components/EvidencePanel.vue'
import DatabasePanel from '../components/DatabasePanel.vue'
import FactTablePanel from '../components/FactTablePanel.vue'
import SettingsPanel from '../components/SettingsPanel.vue'
import { useConversationStore } from '../stores/conversation'
import type { Session } from '../stores/persistence'
import { checkService } from '../api/service'
import { getCrossSourceDemoReadiness, type CrossSourceDemoReadiness } from '../api/capabilities'
import { getDatabaseStatus, getUnstructuredDatabaseStatus } from '../api/database'
import type { DatabaseStatus, UnstructuredDatabaseStatus } from '../api/database'
import { getFactTable, getFrozenQuestions, getRemoteKagStatus } from '../api/kag'
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
/**
 * 每步的精选示例。
 *
 * 两条约束与 `questionGallery.ts` 一致：① 取自冻结题库（`qbank_v4.json`）原句，
 * 一字不改——页面上的句子必须真的跑通过；② 句子自足，不带「上述名单」「该办法」
 * 这类依赖上一题的指代，否则用户单独贴进来时模型不知道指哪份文件。
 */
const GUIDE_TRY_QUESTIONS = [
  '2016 年度山东省高校科研发展计划自筹经费项目名单中，青岛黄海学院共有几项？其中应用类、基础类、研究报告类各多少项？',
  '2024 年度名单中出现了 2023 年度名单没有的项目类别，是哪几类？各有多少项？',
]
const GUIDE_START_QUESTIONS = [
  '2024 年度名单的项目总数比 2023 年度增加了多少项？',
  '2017 年度山东省高校科研计划立项名单中，被标注为「自筹项目」的课题，其经费按山东省高校科学技术计划项目管理办法应由谁负责筹集？',
  '2023、2024、2025 三个年度的安徽高校科研项目名单中，「重大」类项目分别有多少项？',
]
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
/** 事实核验面板：逐题看「拆成什么事实、判出什么值、落在哪个块」。 */
const factTableOpen = ref(false)
let guideOpener: HTMLElement | null = null
let architectureOpener: HTMLElement | null = null
let settingsOpener: HTMLElement | null = null
let databaseOpener: HTMLElement | null = null
const serviceState = ref<'checking' | 'online' | 'offline'>('checking')
const databaseStatus = ref<DatabaseStatus | null>(null)
const unstructuredDatabaseStatus = ref<UnstructuredDatabaseStatus | null>(null)
const crossSourceDemoStatus = ref<CrossSourceDemoReadiness['status'] | 'checking'>('checking')
const kagReady = ref(false)
/** 后端当前走的哪条链路（`kag` = 直连实验服务器）。界面据此隐藏失效控件。 */
const askBackend = ref('orchestrator')
let serviceTimer: ReturnType<typeof setInterval> | undefined
/**
 * 首页的示例分类。
 *
 * 只保留语料真正支持的两类：一类走事实表（某校某年某类有几项），
 * 一类走条款检索（名单与管理办法对照）。老项目原有的「问数」「跨源」两块
 * 依赖业务数据库与工单—合同语料，本项目没有这两份数据，留着就是死入口，
 * 演示时反而暴露「这是一套搬过来的壳」。
 */
const homeQuestionSections = [
  {
    id: 'count', title: '名单统计', icon: 'fact-flow', tone: 'blue',
    description: '问某校、某年、某类有几项，逐条判定并指回原文块。',
    scope: '答案附逐行事实表：口径、判定值、证据块。',
    questions: [
      // 第一条必须是「表覆盖题面每一问 + 五轮跑批全对」的题。
      // 原先放的是 M-17（中国石油大学（华东）几项 + 哪些类别），实测出问题：
      // 表只出「全类别合计 10 项」一行，覆盖不了「哪些类别」，界面单发时
      // 模型回原文数错、答成 1 项。判分器查数值所以算它对，人一眼看出错。
      '2016 年度山东省高校科研发展计划自筹经费项目名单中，青岛黄海学院共有几项？其中应用类、基础类、研究报告类各多少项？',
      '2024 年度名单的项目总数比 2023 年度增加了多少项？',
      '2024 年度名单中出现了 2023 年度名单没有的项目类别，是哪几类？各有多少项？',
    ],
  },
  {
    id: 'clause', title: '条款查找', icon: 'doc-lens', tone: 'teal',
    description: '在立项名单与管理办法之间对照条款，答出原文依据。',
    scope: '答案可点击引用核对原文块。',
    questions: [
      '2017 年度山东省高校科研计划立项名单中，被标注为「自筹项目」的课题，其经费按山东省高校科学技术计划项目管理办法应由谁负责筹集？',
      '山东省的高校科研计划管理办法与安徽省 2025 年度高校科研项目立项通知，对「科研人员自筹经费」的态度分别是什么？',
    ],
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
/**
 * 「系统说明」弹窗的模块目录。
 *
 * 这套目录回答一个问题：**KAG 在这条链路里到底做了什么、我们又在它上面加了什么。**
 * 两者必须分开写，否则评委会以为事实层是 KAG 自带的能力。
 * 前一项是上游 KAG 的职责，后四项是本项目 `contrib/kag/feva_override/` 补的。
 */
const architectureModules = [
  {
    name: 'KAG增强路线', icon: 'gap2graph', title: 'KAG 规划与检索', status: '上游能力 · 每个请求必经',
    concept: '上游能力（OpenSPG/KAG 0.8.0 · Apache-2.0）',
    description: '把问题解析成逻辑形式，规划检索步骤，再走图、文本、向量三路召回原文块。',
    detail: '每个问题先由 KAG 的规划器（feva_domain_lf_static_planning_dag）解析成逻辑形式链，再按计划调检索器，返回带块正文的原文块。注意：KAG 规划的是「查什么」，不是「哪些事要确认」——后者由下面的事实层另拆一层。这是本项目对上游能力边界的实测结论，也是全部改造的出发点。',
    io: { input: '一句自然语言问题', output: '检索计划 + 召回块（块名、正文、相关性分）' },
    wired: true,
  },
  {
    name: 'FactPlan', icon: 'fact-flow', title: '拆事实', status: '本项目补充',
    concept: '拆 · 一个子问一行',
    description: '把问题拆成一条条待确认的事实：某文档、某单位、某类别、几项。',
    detail: '复用语料解析器，不额外调模型——调模型会让这条线不可复算。粒度是「一个子问一行」而不是「一个文档一行」：题目问「应用／基础／研究报告各多少」，表里就必须各占一行，否则模型只能回原文自己数，一数就错。多文档题也因此被结构性拆开，模型无法只答一半。',
    io: { input: '自然语言问题 + 1031 条结构化记录', output: '待确认事实清单（每行一个子问）' },
    wired: true,
  },
  {
    name: 'FactJudge', icon: 'metric-align', title: '判真假', status: '本项目补充',
    concept: '判 · 集合运算',
    description: '对每条事实做集合运算判定，成不成立当场给结果，不经过模型。',
    detail: '判定不调用大模型：对结构化记录做计数、求交、求差、跨年比对。好处是可复算、可审计——同一条事实判定多少次都是同一个数；坏处是它只覆盖能拆成计数的那类子问，拆不动的题会老实落回检索。跨年差集（今年比去年多了哪几类）与差量（多了多少项）是这套判定支持的两类专门子问。',
    io: { input: '待确认事实 + 结构化记录', output: '每行的判定状态与判定值' },
    wired: true,
  },
  {
    name: 'FactEvidence', icon: 'evidence-ledger', title: '挂证据', status: '本项目补充',
    concept: '证 · 实体指回原文块',
    description: '为每条事实取出它落在哪个原文块，块名与正文一并带出。',
    detail: '这是「可溯源」的落点：图里 (项目)-[:source]->(块) 这条边把实体指回原文块，实测 34,525 条；配合 (项目)-[:承担单位]->(单位) 就能按单位和类别锁定块。容器内走图库；应用侧没有图库连接，改用本次请求实际召回的块——两者都指向「模型当时看到了什么」，比事后回查更贴近事实。取不到时如实标「证据未关联」，不填占位串。',
    io: { input: '判定结果 + 召回块', output: '每条事实的证据块名与可跳转的引用' },
    wired: true,
  },
  {
    name: 'FactAnswer', icon: 'doc-lens', title: '拼答案', status: '本项目补充',
    concept: '用 · 逐行对号',
    description: '把事实表交给模型逐行作答，答案里每个数都要能对到某一行。',
    detail: '事实表以合成条目的形式插在引用的最前面，并明确要求「表中没有的数就是语料里没有」。这一步解决的是老问题：表给了数、模型却仍回原文自己数。同时把提示词的措辞与实际注入的条目对齐——曾经出现过提示词让模型去找一个从未注入的条目，等于教它编。',
    io: { input: '事实表 + 召回块 + 用户问题', output: '答案 + 逐行事实表 + 引用块' },
    wired: true,
  },
  {
    name: 'BoundedVerify', icon: 'bounded-verify', title: '边界与如实', status: '边界持续复核',
    description: '拆不动的题老实落回检索；取不到证据就标未关联，不编。',
    detail: '这套方法有明确射程：31 道冻结题里 19 道能拆成事实、12 道拆不动（缺主语指代或本身不是计数题）。拆不动的题落回原链路，不硬凑一行假表。同理，证据取不到时标「证据未关联」而不是填一个看起来合理的块名——一条声称有出处却指错的结论，比明说「没查到」危害大得多。',
    io: { input: '当前题的拆解结果与证据取到情况', output: '可拆 / 不可拆的判定；不可拆时落到检索' },
    wired: true,
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
  { step: '规划检索', module: 'KAG增强路线', desc: 'KAG 把问题解析成逻辑形式，规划要查什么。' },
  { step: '拆事实', module: 'FactPlan', desc: '把一句话拆成一条条待确认的事实。' },
  { step: '判真假', module: 'FactJudge', desc: '用集合运算逐条判定，不经过模型。' },
  { step: '挂证据', module: 'FactEvidence', desc: '为每条事实取出它落在哪个原文块。' },
  { step: '拼答案', module: 'FactAnswer', desc: '逐行对号作答，表里没有的数就是没有。' },
]
/** 流程图里最后落到的那条实际链路，动画走完后显示。 */
const FEVA_OUTLETS = [
  { name: 'FactAnswer', label: '可溯源事实核验', icon: 'document' },
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
/**
 * 入口卡是否可用。
 *
 * 只看一个条件：实验服务器上的 KAG 通不通。两类示例走的都是同一条链路
 * （KAG 规划 → 检索 → 事实表 / 答案 → 引用块），所以不必按块分别判。
 * 老项目那套「数据库连没连、跨源演示数据准没准备好」在这里不适用。
 */
function homeSectionAvailable(_sectionId: string): boolean {
  return kagReady.value
}
function homeSectionStatus(_sectionId: string): string {
  return kagReady.value ? '链路已就绪' : 'KAG 服务未连接'
}
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
/**
 * 探测实验服务器上的 KAG 是否可达。
 *
 * 用 `/api/kag/remote` 而不是老项目的 `/api/kag/status`：后者探测的是
 * 本机 KAG runtime 子进程（`D:\kag\python310`），本项目根本没装那个运行时，
 * 拿它判断会永远显示「未就绪」，而真实链路其实是通的。
 */
async function refreshKagStatus() {
  try {
    const remote = await getRemoteKagStatus()
    kagReady.value = remote.reachable
    askBackend.value = remote.backend
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
  factTableOpen.value = false
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
      <div class="brand"><span class="brand__mark"><img src="/brand/evidence-qa-mark.svg?v=20261028" alt="" aria-hidden="true" /></span><div><b>循证问答<em>EvidenceQA</em></b><small>可溯源事实核验</small></div></div>
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
          <button class="database-entry vm-press" type="button" aria-label="打开事实核验面板" title="逐题查看判定过程与原文块，不调用模型" @click.stop.prevent="factTableOpen = true"><span class="database-entry__icon"><img src="/brand/icons/evidence-ledger.svg" alt="" aria-hidden="true" /></span><span class="database-entry__copy"><b>事实核验</b><small>{{ kagReady ? '逐题查看判定与证据' : 'KAG 服务未连接' }}</small></span><i class="database-entry__dot" :class="kagReady ? 'connected' : 'unavailable'" aria-hidden="true" /></button>
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
            <div class="welcome__eyebrow"><span />循证问答 EvidenceQA <b>· 基于 OpenSPG/KAG</b></div>
            <h1 id="welcome-title">先查清事实，<em>再给可核对的答案。</em></h1>
            <p class="welcome__description">科研管理岗每年要对着几十页立项、结题名单答一句「某校某年某类有几项」，靠人工翻页既慢又容易错。系统先把问题拆成一条条待确认的事实，逐条判定，再让每个结论指回它的原文块；拆不动的题如实落回检索，不硬凑。</p>
          </section>
          <section class="examples-section vm-reveal" aria-labelledby="examples-title">
            <div class="examples-heading"><div><span>按问题类型浏览</span><h2 id="examples-title">你可以这样提问</h2></div><p>点击示例只填入输入框，不会自动发送</p></div>
            <div class="example-row" aria-label="名单统计与条款查找示例">
              <article
                v-for="section in homeQuestionSections"
                :key="section.id"
                class="example-card vm-interact"
                :class="section.tone"
                :aria-labelledby="`home-path-${section.id}`"
              >
                <div class="example-card__head">
                  <span class="example-card__icon vm-icon-shift"><AppIcon :name="section.icon" :size="20" /></span>
                  <div class="example-card__title"><h3 :id="`home-path-${section.id}`">{{ section.title }}</h3><small>{{ section.description }}</small></div>
                  <span class="example-card__status" :class="{ 'is-ready': homeSectionAvailable(section.id) }">{{ homeSectionStatus(section.id) }}</span>
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
              </article>
            </div>
          </section>
          <section class="source-scope" aria-labelledby="source-scope-title">
            <div class="source-scope__heading"><div><span>这条链路用到的资源</span><h2 id="source-scope-title">当前资料与图规模</h2></div><button type="button" @click="factTableOpen = true">打开事实核验面板<AppIcon name="arrow" :size="13" /></button></div>
            <div class="source-scope__items">
              <button class="source-scope__item" type="button" @click="factTableOpen = true"><span class="source-scope__icon"><AppIcon name="gap2graph" :size="16" /></span><span><b>KAG 规划与检索</b><small>{{ kagReady ? '实验服务器已连接 · 图/文本/向量三路' : '服务未连接' }}</small></span><AppIcon class="source-scope__open" name="arrow" :size="13" /></button>
              <button class="source-scope__item" type="button" @click="factTableOpen = true"><span class="source-scope__icon source-scope__icon--doc"><AppIcon name="document" :size="16" /></span><span><b>公示语料</b><small>14 份 · 227 块 · 1031 条结构化记录</small></span><AppIcon class="source-scope__open" name="arrow" :size="13" /></button>
              <button class="source-scope__item" type="button" @click="factTableOpen = true"><span class="source-scope__icon source-scope__icon--cross"><AppIcon name="evidence-ledger" :size="16" /></span><span><b>证据图</b><small>34,525 条 source 边 · 326 种关系</small></span><AppIcon class="source-scope__open" name="arrow" :size="13" /></button>
            </div>
          </section>
          <section class="welcome-flow" aria-labelledby="welcome-flow-title">
            <div class="welcome-flow__heading"><span>回答怎样形成</span><h2 id="welcome-flow-title">从问题，到可核对的答案</h2></div>
            <ol><li><b>规划</b><span>KAG 把问题解析成检索计划</span></li><li><b>拆事实</b><span>拆成一条条待确认的事实</span></li><li><b>判真假</b><span>集合运算逐条判定，不经模型</span></li><li><b>挂证据</b><span>每条事实指回它的原文块</span></li></ol>
            <p>答案里的每个数字都必须能对到事实表的某一行；表里没有的数，就是语料里没有。拆不动的题老实落回检索，不硬凑。</p>
          </section>
        </div>
      </div>
      <Composer v-if="hasMessages" ref="composerRef" :disabled="conversation.isPending" :suggestions="activeSuggestions" :model-profile-id="conversation.selectedModelProfileId" :retrieval-mode="conversation.retrievalMode" :kag-ready="kagReady" :direct-kag="askBackend === 'kag'" @update:model-profile-id="conversation.setModelProfile($event)" @update:retrieval-mode="conversation.setRetrievalMode($event)" @submit="submit" @stop="conversation.stopWaiting()" />
      <Composer v-else ref="composerRef" class="welcome-composer" :disabled="conversation.isPending" :welcome="true" :model-profile-id="conversation.selectedModelProfileId" :retrieval-mode="conversation.retrievalMode" :kag-ready="kagReady" :direct-kag="askBackend === 'kag'" @update:model-profile-id="conversation.setModelProfile($event)" @update:retrieval-mode="conversation.setRetrievalMode($event)" @submit="submit" @stop="conversation.stopWaiting()" />
        </div>
        <section v-if="evidenceOpen" class="evidence vm-panel" aria-label="来源证据面板"><header class="evidence__head"><div><AppIcon name="link" :size="17" /><b>来源证据</b><span>核对原始出处</span></div><button class="icon-button close-button vm-press" aria-label="关闭证据面板" @click="evidenceOpen = false"><AppIcon name="close" :size="18" /></button></header><EvidencePanel :ref-item="conversation.activeRef" :sql="conversation.activeResponse?.sql ?? null" :data-source="conversation.activeResponse?.data_source ?? null" /></section>
        <button v-if="sidebarOpen || evidenceOpen" class="scrim" :class="{ 'scrim--sidebar': sidebarOpen }" aria-label="关闭浮层" @click="sidebarOpen = false; evidenceOpen = false" />
      </div>
    </section>
    <Teleport to="body">
      <Transition name="vm-dialog"><SettingsPanel v-if="settingsOpen" :direct-kag="askBackend === 'kag'" @close="settingsOpen = false" /></Transition>
      <Transition name="vm-dialog"><DatabasePanel v-if="databaseOpen" @close="databaseOpen = false" /></Transition>
      <Transition name="vm-dialog"><FactTablePanel v-if="factTableOpen" @close="factTableOpen = false" /></Transition>
    </Teleport>
    <Teleport to="body">
      <Transition name="vm-dialog"><div v-if="architectureOpen" class="architecture-backdrop" :inert="!architectureOpen" @click.self="architectureOpen = false"><section class="architecture-dialog" role="dialog" aria-modal="true" aria-labelledby="architecture-title"><header><div><span class="architecture-dialog__eyebrow">EVIDENCE QA · SYSTEM MAP</span><h2 id="architecture-title">FEVA 事实—证据核验架构</h2></div><button class="icon-button close-button vm-press" aria-label="关闭架构导览" @click="architectureOpen = false"><AppIcon name="close" /></button></header><p class="architecture-dialog__intro">每个问题都完整走一遍上游 KAG：解析逻辑形式 → 规划检索 → 图/文本/向量三路召回 → 生成答案。本项目的改动是在生成之前插一层<b>事实表</b>——把问题拆成一条条待确认的事实，用结构化记录逐条判定，再走图上「实体指回原文块」的边为每条事实挂证据。上面是链路，下面是分工。</p><div class="architecture-tabs" role="tablist" aria-label="架构导览视图"><button role="tab" :aria-selected="architectureView === 'flow'" :class="{ active: architectureView === 'flow' }" @click="switchArchitectureView('flow')"><AppIcon name="arrow" :size="13" />流程演示</button><button role="tab" :aria-selected="architectureView === 'modules'" :class="{ active: architectureView === 'modules' }" @click="switchArchitectureView('modules')"><AppIcon name="sources" :size="13" />模块目录</button></div>
        <div v-if="architectureView === 'flow'" class="architecture-flowview" role="tabpanel">
          <ol class="feva-flow"><li v-for="(stage, index) in FEVA_FLOW" :key="stage.step" class="feva-flow__item" :class="{ 'is-reached': flowProgress > index, 'is-active': flowProgress === index + 1 }" :style="{ '--motion-order': index, '--stage-step': index }"><span class="feva-flow__index">{{ String(index + 1).padStart(2, '0') }}</span><div class="feva-flow__body"><b>{{ stage.step }}</b><small>{{ stage.desc }}</small><em>由 {{ stage.module }} 承担</em></div><span class="feva-flow__pulse" aria-hidden="true" /></li></ol>
          <div class="feva-outlets" :class="{ 'is-visible': flowProgress >= FEVA_FLOW.length }"><span class="feva-outlets__label">随后进入</span><span v-for="outlet in FEVA_OUTLETS" :key="outlet.name" class="feva-outlets__card"><AppIcon :name="outlet.icon" :size="14" /><b>{{ outlet.name }}</b><small>{{ outlet.label }}</small></span></div>
          <p class="feva-flow__note">五个阶段对「能拆成事实」的题成立；条款题与依赖上文的指代题拆不动，会如实跳过中间三步、落回检索作答——这一步的诚实比多答一题重要。</p>
        </div>
        <div v-else class="architecture-module-grid" role="tabpanel"><article v-for="(module, index) in architectureModules" :key="module.name" class="architecture-module vm-reveal" :style="{ '--motion-order': Math.min(index, 4) }" :class="{ 'architecture-module--research': !module.wired, 'is-expanded': expandedModules.has(module.name) }"><button class="architecture-module__toggle vm-press" :aria-expanded="expandedModules.has(module.name)" @click="toggleModule(module.name)"><div class="architecture-module__head"><span class="architecture-module__icon" aria-hidden="true"><img :src="`/brand/icons/${module.icon}.svg`" alt="" /></span><div class="architecture-module__labels"><b>{{ module.name }}</b><span>{{ module.name === 'KAG增强路线' ? (kagReady ? '上游能力 · 已连接' : '上游能力 · 服务未连接') : module.status }}</span></div></div><h3>{{ module.title }}</h3><p>{{ module.description }}</p><span class="architecture-module__more">{{ expandedModules.has(module.name) ? '收起' : '展开详细' }}<AppIcon name="chevron" :size="12" /></span></button><div v-if="expandedModules.has(module.name)" class="architecture-module__detail vm-detail"><p>{{ module.detail }}</p><dl><div><dt>输入</dt><dd>{{ module.io.input }}</dd></div><div><dt>输出</dt><dd>{{ module.io.output }}</dd></div></dl><p class="architecture-module__wired" :class="{ 'is-off': !module.wired && !(module.name === 'KAG增强路线' && kagReady) }"><AppIcon :name="module.wired || (module.name === 'KAG增强路线' && kagReady) ? 'check' : 'warning'" :size="12" />{{ module.name === 'KAG增强路线' ? (kagReady ? '每个请求必经：规划 → 检索 → 生成' : '服务未连接，问答不可用') : module.wired ? '已接入问答路径' : '当前未接入问答路径' }}</p></div></article></div>
        <aside class="architecture-dialog__upstream" aria-label="上游与本项目自研的边界">
          <b>上游与本项目自研的边界</b>
          <dl>
            <div><dt>上游</dt><dd>OpenSPG / KAG <code>0.8.0</code>（Apache-2.0，基线 <code>OpenSPG-KAG-fdab15b</code>）。规划、检索、生成三件事全部由它完成。</dd></div>
            <div><dt>本项目新增</dt><dd><code>contrib/kag/</code>：事实判定扩展层、抽取器开关、结构化聚合检索路、编号检索补丁。全部以「新增模块 + 类方法补丁 + 标记文件开关」实现，不删改上游逻辑，可单独回滚。</dd></div>
            <div><dt>上游贡献</dt><dd><a href="https://github.com/OpenSPG/KAG/issues/758" target="_blank" rel="noopener noreferrer">OpenSPG/KAG #758</a>：<code>SchemaFreeExtractor</code> 尾逗号把三元组包成一元组、语义边被静默丢弃。修复后语义边占比 0.57% → 33.41%，事实表的证据边正是靠它才建得出来。</dd></div>
          </dl>
        </aside>
        <p class="architecture-dialog__note">这些名称用于帮助理解项目中的实际模块和研究方向，不代表独立算法或额外验收结论。</p></section></div></Transition>
    </Teleport>
    <Teleport to="body">
      <Transition name="vm-dialog"><div v-if="guideOpen" class="guide-backdrop" :inert="!guideOpen" @click.self="closeGuide"><section class="guide" role="dialog" aria-modal="true" aria-labelledby="guide-title"><header><span class="guide__icon"><AppIcon name="help" :size="24" /></span><button class="icon-button close-button vm-press" aria-label="关闭使用指南" @click="closeGuide"><AppIcon name="close" /></button></header><p class="guide__eyebrow">第 {{ guideStepIndex + 1 }} 步 · 共 {{ GUIDE_STEPS.length }} 步</p><h2 id="guide-title">{{ GUIDE_STEPS[guideStepIndex]?.label }}</h2>
        <nav class="guide__dots" aria-label="引导进度">
          <button v-for="(step, index) in GUIDE_STEPS" :key="step.id" type="button" role="tab" :aria-selected="guideStep === step.id" :class="{ active: guideStep === step.id, done: index < guideStepIndex }" :disabled="index > guideStepIndex" :aria-label="`第 ${index + 1} 步：${step.label}`" @click="guideStep = step.id" />
        </nav>
        <div class="guide__panes" :class="`guide__panes--${guideStep}`">
        <div v-if="guideStep === 'what'" class="guide__pane" role="tabpanel">
          <p class="guide__lead">语料是 14 份公开的高校科研项目公示文件（立项名单、结题验收结果、管理办法与通知），系统只做两件事：<b>名单里的数</b>和<b>办法里的条款</b>。</p>
          <ul class="guide__kinds">
            <li><b>名单统计</b><span>问某校、某年、某类有几项。答案附逐行事实表：口径、判定值、证据块。</span></li>
            <li><b>跨年对比</b><span>问「比去年多了几项」「多了哪几类」。这是集合运算，不是再数一遍。</span></li>
            <li><b>条款查找</b><span>在名单与管理办法之间对照，答出原文依据。条款不出事实表——条款正文不在结构化记录里。</span></li>
          </ul>
          <p class="guide__note">能拆成「某文档 · 某单位 · 某类别」的题会生成事实表；拆不动的题（条款题、依赖上文的指代题）如实落回检索作答。<b>表里没有的数，就是语料里没有</b>——不会补一个看起来合理的。</p>
          <p class="guide__note">语料全部来自公开的省级教育主管部门公示文件；问题里出现的单位是文件中的项目承担单位，与参赛团队无关。</p>
          <p class="guide__try">试一试：</p>
          <ul class="guide__examples">
            <li v-for="question in GUIDE_TRY_QUESTIONS" :key="question"><button type="button" @click="pickGuideQuestion(question)">{{ question }}<AppIcon name="arrow" :size="13" /></button></li>
          </ul>
        </div>
        <div v-else-if="guideStep === 'how'" class="guide__pane" role="tabpanel">
          <ol class="guide__list">
            <li><b>先看事实表的三列，再看答案</b><span>每行是「口径 → 判定值 → 证据块」。判定由 1031 条结构化记录做集合运算得出，不经过模型——同一个问题问几次都是同一个数，可复算、可审计。</span></li>
            <li><b>点证据块，核对模型当时读到的原文</b><span>证据取自图里「实体指回原文块」的边（实测 34,525 条）。点开看到的是那一段原文，不是一句转述。</span></li>
            <li><b>侧栏「事实核验」：不提问也能看判定过程</b><span>31 道冻结题任选，离线重算，不调用模型、不占额度、秒出。演示时若某题在等，用它先把机制讲清楚。</span></li>
          </ol>
          <p class="guide__note">检索与生成由 KAG（OpenSPG/KAG 0.8.0，Apache-2.0）完成，事实层是本项目在它之上补的一层。单题通常 1–3 分钟，重的题会到 5 分钟——等待时输入框会变成停止按钮，可随时中止。</p>
        </div>
        <div v-else class="guide__pane" role="tabpanel">
          <p class="guide__lead">挑一句试试，或直接提问。完整的可问清单在输入框左侧的四角星里，按能力分了组。</p>
          <ul class="guide__examples">
            <li v-for="question in GUIDE_START_QUESTIONS" :key="question"><button type="button" @click="pickGuideQuestion(question)">{{ question }}<AppIcon name="arrow" :size="13" /></button></li>
          </ul>
          <p class="guide__note">同一题重跑应当给出同一个结论——如果两次不一样，说明那次靠的是检索原文而不是事实表，可以在答案里的处理详情看到差别。会话只保存在这个浏览器；左下角是服务与图状态。</p>
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
/* 两张示例卡等宽等高。
   原来第一张带 `--featured`（跨整行、左右分栏），第二张半宽，看上去就是
   「一大一小」；老项目那套是给三入口用的（一个重点 + 两个普通），现在只剩
   两个入口，主次之分反而别扭——两条路径对评委价值相同。 */
.example-card.is-checked { border-color: color-mix(in srgb, var(--brand-primary) 30%, var(--border-subtle)); }
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
/* 上游 / 自研边界：评分表「AI 与开源融合」看的是「谁做了什么、什么许可、边界在哪」，
   这一段要能在演示时直接念出来。放 dl 而不是句子里，是因为评委常要抄这三行。 */
.architecture-dialog__upstream { margin: 18px 0 0; padding: 13px 15px; border: 1px solid var(--border-subtle); border-radius: 10px; background: var(--bg-subtle); }
.architecture-dialog__upstream > b { display: block; margin-bottom: 9px; color: var(--text-heading); font-size: 13px; }
.architecture-dialog__upstream dl { display: grid; gap: 8px; margin: 0; }
.architecture-dialog__upstream dl > div { display: grid; grid-template-columns: 72px minmax(0, 1fr); gap: 10px; }
.architecture-dialog__upstream dt { color: var(--text-faint); font-size: 12px; }
.architecture-dialog__upstream dd { margin: 0; color: var(--text-secondary); font-size: 12.5px; line-height: 1.7; }
.architecture-dialog__upstream code { padding: 1px 4px; border-radius: 4px; color: var(--brand-primary); background: var(--brand-soft); font-size: 11.5px; }
.architecture-dialog__upstream a { color: var(--brand-primary); text-decoration: underline; text-underline-offset: 2px; }
@media (max-width: 640px) { .architecture-dialog__upstream dl > div { grid-template-columns: 1fr; gap: 2px; } }
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
