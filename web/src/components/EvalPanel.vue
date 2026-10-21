<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import AppIcon from './AppIcon.vue'
import { getEvidenceUrl } from '../api/evidence'
import { fetchEvalSuites, cancelEval, runEval } from '../api/eval'
import type { EvalCaseResult, EvalSuite, EvalSummary } from '../api/eval'

const emit = defineEmits<{ close: [] }>()

const suites = ref<EvalSuite[]>([])
const loading = ref(true)
const loadError = ref('')
const activeSuite = ref('')
const running = ref(false)
const runError = ref('')
const results = ref<EvalCaseResult[]>([])
const summary = ref<EvalSummary | null>(null)
const progress = ref({ index: 0, total: 0 })
const expandedId = ref('')
const consumedMs = ref(0)
/**
 * 后端眼里有没有评测在跑。
 *
 * 必须与本地 running 分开存：点「停止」后本地立刻认为结束了，但后端要跑完
 * 手上那题才真的退出。这中间若只看本地状态，按钮会显示成可点、点下去却 409
 * ——「点了停止就再也开不了评测」就是这么来的。所以按钮以**后端状态**为准，
 * 本地 running 只管"我正在接收事件"。
 */
const serverRunning = ref(false)
/** 本次运行的 run_id。停止时回传，避免误停刚好开跑的新一轮。 */
const runId = ref('')
/** 已请求停止、正在等后端收尾。用于把按钮文案从「停止」换成「正在停止…」。 */
const stopping = ref(false)
/** 这一轮是被停止的，不是跑完的 —— 界面不能把半截结果当成绩展示。 */
const cancelled = ref(false)
let controller: AbortController | null = null
let timer: ReturnType<typeof setInterval> | undefined
let poller: ReturnType<typeof setInterval> | undefined

const activeMeta = computed(() => suites.value.find((item) => item.key === activeSuite.value) ?? null)
const percent = computed(() => (progress.value.total ? Math.round((progress.value.index / progress.value.total) * 100) : 0))
const passRate = computed(() => {
  const total = results.value.length
  if (!total) return 0
  return results.value.filter((item) => item.passed).length / total
})
/** 能不能按「开始评测」。后端在跑时不能 —— 这比让用户点了才收到 409 友好。 */
const canStart = computed(() => !running.value && !serverRunning.value)

function seconds(ms: number | undefined): string {
  if (!ms) return '—'
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const body = await fetchEvalSuites()
    suites.value = body.suites
    if (!activeSuite.value && body.suites.length) activeSuite.value = body.suites[0]!.key
    // 面板打开时后端可能已经有一轮在跑（比如上次中途关掉了面板）。
    // 如实认领它，而不是假装什么都没有。
    serverRunning.value = body.running
    if (body.running && body.current) runId.value = body.current.run_id
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '读取评测套件失败'
  } finally {
    loading.value = false
  }
}

async function start() {
  if (!canStart.value || !activeSuite.value) return
  running.value = true
  stopping.value = false
  cancelled.value = false
  runError.value = ''
  results.value = []
  summary.value = null
  expandedId.value = ''
  progress.value = { index: 0, total: activeMeta.value?.case_count ?? 0 }
  consumedMs.value = 0
  controller = new AbortController()
  const startedAt = Date.now()
  timer = setInterval(() => { consumedMs.value = Date.now() - startedAt }, 200)
  try {
    await runEval(activeSuite.value, {
      onCase: (index, total, result) => {
        progress.value = { index, total }
        results.value = [...results.value, result]
      },
      onProgress: (index, total) => { progress.value = { index, total } },
      onDone: (payload) => {
        summary.value = payload
        runId.value = payload.run_id
        progress.value = { index: payload.total, total: payload.total }
      },
      // 后端确认停了。**这时才敢说"已停止"** —— 之前是点一下按钮就宣布停了，
      // 而后端还在跑，两边说法不一致。
      onCancelled: (index, total) => {
        cancelled.value = true
        progress.value = { index, total }
      },
      onError: (message) => { runError.value = message },
    }, controller.signal)
  } catch (error) {
    if (!(error instanceof DOMException && error.name === 'AbortError')) {
      runError.value = error instanceof Error ? error.message : '评测中断'
    }
  } finally {
    running.value = false
    if (timer) clearInterval(timer)
    timer = undefined
    controller = null
    void refreshRunning()
  }
}

/**
 * 停止评测。三件事：
 *   1. 告诉后端停 —— 否则线程会把整个套件跑完，而它占着槽位让下次开不了；
 *   2. **不立刻 abort 本机的 SSE** —— 一 abort，后端随后发来的 `cancelled`
 *      帧就永远收不到，界面只能靠猜自己是不是真被停了。让流走到终点。
 *   3. 轮询后端状态，它真停了才把开始按钮放行。
 *
 * 顺序上必须"先请求后端、再等流自然收尾"：反过来的话本机 abort 会让请求也
 * 一起被取消，后端根本收不到停止信号 —— 这正是本次要修的 bug。
 */
async function stop() {
  if (stopping.value) return
  stopping.value = true
  runError.value = ''
  try {
    await cancelEval(runId.value || undefined)
  } catch (error) {
    runError.value = error instanceof Error ? error.message : '停止评测失败'
    // 请求都没发出去，就不必再等流自然结束了。
    controller?.abort()
  }
  if (timer) clearInterval(timer)
  timer = undefined
  // 不在这里把 running 置 false：等后端确认（或轮询到它停了）再改。
  // 立刻置 false 会让按钮显示成可点，点下去却是 409 —— 就是原来的毛病。
  void pollUntilStopped()
}

/** 轮询后端直到它真的停下，再把本地状态放行。 */
async function pollUntilStopped() {
  if (poller) clearInterval(poller)
  poller = setInterval(() => { void refreshRunning() }, 1500)
}

async function refreshRunning() {
  try {
    const body = await fetchEvalSuites()
    serverRunning.value = body.running
    if (body.running && body.current) runId.value = body.current.run_id
    if (!body.running) {
      if (poller) clearInterval(poller)
      poller = undefined
      stopping.value = false
      // 不在这里把 running 置 false：流还在收尾，`runEval` 的 finally 会设。
      // 抢着设会让「已跑 N/M」的计数与还在到达的 case 帧错位。
      if (cancelled.value) {
        runError.value = `已停止。已跑完 ${results.value.length} 题，结果不全，不给出汇总成绩。`
      }
    }
  } catch {
    // 轮询失败不改状态：下一轮会再试。断网时错误由 runEval 那边报。
  }
}

function toggle(id: string) { expandedId.value = expandedId.value === id ? '' : id }

/** 与后端 `app/eval_api.py` 的 `PREVIEW_ROWS` 保持一致 —— 只影响提示文案。 */
const EVAL_PREVIEW_ROWS = 5

/** 引用判定依据的三种来源。后端给的是英文键，界面说人话。 */
const CITATION_BASIS_LABELS: Record<string, string> = {
  retrieved: '指向了本轮真实召回的片段',
  known: '指向了知识库中存在的文档',
  none: '没有可核对的引用',
}

function citationBasisLabel(basis: string): string {
  return CITATION_BASIS_LABELS[basis] ?? basis
}

void load()
onUnmounted(() => {
  // 关面板不停后端：用户可能只是想看看别的页面。但**必须停止轮询**，
  // 否则面板销毁后定时器还在打接口。
  controller?.abort()
  if (timer) clearInterval(timer)
  if (poller) clearInterval(poller)
})
</script>

<template>
  <div class="eval-backdrop" @click.self="emit('close')">
    <section class="eval" role="dialog" aria-modal="true" aria-labelledby="eval-title">
      <header class="eval__head">
        <div class="eval__title">
          <span class="eval__mark"><AppIcon name="chart" :size="20" /></span>
          <div>
            <h2 id="eval-title">系统评测</h2>
            <p>在真实链路上跑评测集，逐题给出判分与理由</p>
          </div>
        </div>
        <button class="icon-button close-button vm-press" aria-label="关闭系统评测" @click="emit('close')"><AppIcon name="close" :size="18" /></button>
      </header>

      <div v-if="loading" class="eval__state" role="status">正在读取评测套件…</div>
      <div v-else-if="loadError" class="eval__state eval__state--error" role="alert">{{ loadError }}<button class="eval__retry" @click="load">重试</button></div>

      <template v-else>
        <div class="eval__suites" role="radiogroup" aria-label="评测套件">
          <button
            v-for="suite in suites"
            :key="suite.key"
            class="eval-suite"
            :class="{ active: suite.key === activeSuite }"
            role="radio"
            :aria-checked="suite.key === activeSuite"
            :disabled="running || serverRunning"
            @click="activeSuite = suite.key"
          >
            <b>{{ suite.title }}</b>
            <small>{{ suite.case_count }} 题 · {{ suite.judge === 'sql' ? '结果集比对' : '要点与引用' }}</small>
          </button>
        </div>

        <p v-if="activeMeta" class="eval__scoring"><AppIcon name="link" :size="13" />对应评分表：{{ activeMeta.scoring_item }}<a class="eval__file" :href="getEvidenceUrl(activeMeta.case_file)" target="_blank" rel="noreferrer">打开题目文件（隐藏参考答案）</a></p>

        <div class="eval__actions">
          <button class="eval__run" :disabled="!canStart || !activeSuite" @click="start">
            <AppIcon :name="canStart ? 'arrow' : 'clock'" :size="16" />{{ serverRunning ? (stopping ? '正在停止…' : '评测进行中') : (results.length ? '重新运行' : '▶ 开始评测') }}
          </button>
          <button v-if="running || serverRunning" class="eval__stop" :disabled="stopping" @click="stop"><AppIcon name="stop" :size="14" />{{ stopping ? '正在停止…' : '停止' }}</button>
          <span v-if="running || consumedMs" class="eval__live" role="status">
            <i />已跑 {{ progress.index }}/{{ progress.total }} 题 · 用时 {{ seconds(consumedMs) }}
          </span>
        </div>

        <div v-if="progress.total" class="eval__bar" :class="{ done: !!summary }"><span :style="{ width: `${percent}%` }" /></div>
        <p v-if="runError" class="eval__error" :class="{ 'eval__error--info': cancelled }" role="alert">{{ runError }}</p>

        <!-- 汇总：先把「够不够档」摆在最上面，这是评委真正要看的一句话 -->
        <div v-if="summary" class="eval-summary">
          <div class="eval-summary__score">
            <b>{{ summary.passed }}/{{ summary.total }}</b>
            <span>{{ (summary.accuracy * 100).toFixed(1) }}%</span>
            <small>{{ summary.suite_title }} · 用时 {{ seconds(summary.total_elapsed_ms) }}</small>
          </div>
          <div class="eval-bands" aria-label="评分表目标档位">
            <span v-for="band in summary.bands" :key="band.label" :class="{ reached: band.reached }">
              {{ band.reached ? '✓' : '·' }} {{ band.label }}
            </span>
          </div>
          <div class="eval-facts">
            <span v-for="(value, key) in summary.by_difficulty" :key="key">{{ key }} {{ value.passed }}/{{ value.total }}</span>
            <span>单题中位 {{ seconds(summary.latency.p50_ms) }}</span>
            <span>最慢 {{ seconds(summary.latency.max_ms) }}</span>
          </div>
          <p class="eval-summary__scope">{{ summary.scope }}</p>
        </div>

        <!-- 逐题明细：默认收起，展开才看到答案与判分理由，避免演示时信息过载 -->
        <ol v-if="results.length" class="eval-cases">
          <li v-for="item in results" :key="item.id" class="eval-case" :class="item.passed ? 'pass' : 'fail'">
            <button class="eval-case__row" :aria-expanded="expandedId === item.id" @click="toggle(item.id)">
              <span class="eval-case__badge">{{ item.passed ? '✓' : '✕' }}</span>
              <span class="eval-case__id">{{ item.id }}</span>
              <span class="eval-case__q">{{ item.question }}</span>
              <span class="eval-case__ms">{{ seconds(item.elapsed_ms) }}</span>
            </button>
            <div v-if="expandedId === item.id" class="eval-case__detail vm-detail">
              <p class="eval-case__reason"><b>判分</b>{{ item.reason }}</p>
              <p v-if="item.first_diff" class="eval-case__diff"><b>首处差异</b>{{ item.first_diff }}</p>
              <p v-if="item.loose" class="eval-case__note">列序不同但结果一致，按「宽松通过」计入，报告中会单独计数。</p>

              <div v-if="item.sub" class="eval-case__sub">
                <span v-if="item.sub.points_total">要点 {{ item.sub.points_hit }}/{{ item.sub.points_total }}</span>
                <span>引用 {{ item.sub.citation_ok ? '合格' : '不合格' }}<template v-if="item.sub.citation_basis">（{{ citationBasisLabel(item.sub.citation_basis) }}）</template></span>
              </div>

              <!-- 判分要点：既显示该答到的全部要点，也标出哪几条没答到 -->
              <div v-if="item.answer_points?.length" class="eval-case__points">
                <b>标准要点</b>
                <ul>
                  <li v-for="point in item.answer_points" :key="point" :class="{ missed: item.sub?.missed_points?.includes(point) }">
                    {{ point }}<em v-if="item.sub?.missed_points?.includes(point)">未答到</em>
                  </li>
                </ul>
              </div>
              <p v-if="item.forbidden_entities?.length" class="eval-case__forbidden"><b>禁答</b>{{ item.forbidden_entities.join('、') }}<span v-if="item.sub?.hallucinated_entities?.length" class="hit">（出现了：{{ item.sub.hallucinated_entities.join('、') }}）</span></p>
              <div v-if="item.gold_refs?.length" class="eval-case__refs">
                <b>期望来源</b>
                <ul>
                  <li v-for="ref in item.gold_refs" :key="ref.doc + ref.anchor">
                    {{ ref.doc }}<template v-if="ref.anchor"> · {{ ref.anchor }}</template>
                  </li>
                </ul>
              </div>

              <!-- SQL 侧双栏对照：左标准、右实际。列序与值都能对得上 -->
              <div v-if="item.gold_sql || item.sql" class="eval-compare">
                <div class="eval-compare__col">
                  <h4>标准答案</h4>
                  <pre v-if="item.gold_sql" class="eval-case__sql">{{ item.gold_sql }}</pre>
                  <p v-else class="eval-compare__empty">该题未登记参考 SQL。</p>
                  <p class="eval-compare__meta">
                    结果 {{ item.gold_rows_count ?? 0 }} 行
                    <span v-if="item.gold_error" class="bad">执行出错</span>
                  </p>
                  <table v-if="item.gold_preview?.length" class="eval-rows">
                    <tbody>
                      <tr v-for="(row, index) in item.gold_preview" :key="index">
                        <td v-for="(cell, column) in row" :key="column">{{ cell }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <div class="eval-compare__col">
                  <h4>系统实际</h4>
                  <pre v-if="item.sql" class="eval-case__sql">{{ item.sql }}</pre>
                  <p v-else class="eval-compare__empty">系统未产出 SQL。</p>
                  <p class="eval-compare__meta">
                    结果 {{ item.pred_rows_count ?? 0 }} 行
                    <span v-if="item.pred_error" class="bad">执行出错</span>
                  </p>
                  <table v-if="item.pred_preview?.length" class="eval-rows">
                    <tbody>
                      <tr v-for="(row, index) in item.pred_preview" :key="index">
                        <td v-for="(cell, column) in row" :key="column">{{ cell }}</td>
                      </tr>
                    </tbody>
                  </table>
                  <p v-else-if="!item.pred_error" class="eval-compare__empty">无数据行。</p>
                </div>
              </div>

              <!-- RAG 侧：没有 SQL 可对照，只显示系统答案 -->
              <p v-if="!item.gold_sql && item.answer" class="eval-case__answer">{{ item.answer }}</p>
              <p class="eval-compare__hint">预览只显示前 {{ EVAL_PREVIEW_ROWS }} 行；判分用的是完整结果集，不是这份预览。</p>
              <p v-if="item.tools?.length" class="eval-case__tools">工具链：{{ item.tools.join(' › ') }}</p>
            </div>
          </li>
        </ol>

        <p v-if="!results.length && !running" class="eval__hint">选一个套件开始。每道题都会走真实链路，单题可能数秒到十几秒。</p>
      </template>
    </section>
  </div>
</template>

<style scoped>
.eval-backdrop { position: fixed; inset: 0; z-index: 210; display: grid; place-items: center; padding: 22px; background: var(--scrim); backdrop-filter: blur(3px); }
.eval { display: flex; flex-direction: column; width: min(860px, 100%); max-height: 90dvh; overflow: hidden; border-radius: 20px; background: var(--bg-elevated); box-shadow: var(--shadow-guide); }
.eval__head { display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; gap: 12px; padding: 22px 26px 18px; border-bottom: 1px solid var(--border-subtle); }
.eval__title { display: flex; align-items: center; gap: 12px; min-width: 0; }
.eval__mark { display: grid; width: 42px; height: 42px; flex-shrink: 0; place-items: center; border-radius: 12px; color: var(--brand-primary); background: var(--brand-soft); }
.eval__title h2 { margin: 0; color: var(--text-heading); font-size: 20px; }
.eval__title p { margin: 3px 0 0; color: var(--text-muted); font-size: 13px; }
.eval__state { padding: 40px 26px; color: var(--text-secondary); font-size: 15px; }
.eval__state--error { color: var(--state-error); }
.eval__retry { margin-left: 10px; padding: 4px 9px; border: 1px solid var(--border-subtle); border-radius: 6px; background: var(--bg-surface); color: var(--text-primary); }
.eval__suites { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 9px; padding: 18px 26px 0; }
.eval-suite { display: grid; gap: 3px; padding: 11px 13px; border: 1px solid var(--border-subtle); border-radius: 11px; color: var(--text-secondary); text-align: left; background: var(--bg-surface); transition: border-color .15s, background .15s; }
.eval-suite:hover:not(:disabled) { border-color: var(--border-brand); }
.eval-suite.active { border-color: var(--border-brand); background: var(--brand-soft); }
.eval-suite:disabled { opacity: .6; }
.eval-suite b { color: var(--text-primary); font-size: 15px; }
.eval-suite small { color: var(--text-muted); font-size: 12px; }
.eval__scoring { display: flex; align-items: center; gap: 6px; margin: 11px 26px 0; color: var(--brand-primary); font-size: 13px; }
.eval__file { margin-left: auto; color: var(--text-faint); font-size: 12px; text-decoration: underline dotted; text-underline-offset: 3px; }
.eval__file:hover { color: var(--brand-primary); text-decoration-style: solid; }
.eval__actions { display: flex; align-items: center; gap: 10px; padding: 14px 26px 0; }
.eval__run { display: flex; align-items: center; gap: 8px; padding: 9px 16px; border: 0; border-radius: 9px; color: var(--text-on-brand); background: var(--brand-primary); font-size: 15px; font-weight: 600; }
.eval__run:disabled { opacity: .65; }
.eval__stop { display: flex; align-items: center; gap: 6px; padding: 8px 13px; border: 1px solid var(--border-subtle); border-radius: 8px; color: var(--text-secondary); background: var(--bg-surface); font-size: 14px; }
.eval__live { display: flex; align-items: center; gap: 7px; margin-left: auto; color: var(--text-secondary); font-size: 13px; font-variant-numeric: tabular-nums; }
.eval__live i { width: 6px; height: 6px; border-radius: 50%; background: var(--accent-teal); animation: eval-blink 1.3s infinite; }
.eval__bar { height: 4px; margin: 13px 26px 0; overflow: hidden; border-radius: 3px; background: var(--bg-subtle); }
.eval__bar span { display: block; height: 100%; background: var(--brand-primary); transition: width .3s; }
.eval__bar.done span { background: var(--accent-teal); }
.eval__error { margin: 11px 26px 0; color: var(--state-error); font-size: 14px; }
/* 「已停止」不是错误，是用户主动做的选择。用错误红会显得像出了故障。 */
.eval__error--info { color: var(--text-secondary); }
.eval-summary { margin: 17px 26px 0; padding: 16px 18px; border: 1px solid var(--border-subtle); border-radius: 13px; background: var(--bg-subtle); }
.eval-summary__score { display: flex; align-items: baseline; gap: 11px; }
.eval-summary__score b { color: var(--text-heading); font-size: 27px; font-variant-numeric: tabular-nums; }
.eval-summary__score span { color: var(--brand-primary); font-size: 19px; font-weight: 600; }
.eval-summary__score small { margin-left: auto; color: var(--text-muted); font-size: 12px; }
.eval-bands { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 11px; }
.eval-bands span { padding: 3px 9px; border: 1px solid var(--border-subtle); border-radius: 20px; color: var(--text-faint); background: var(--bg-surface); font-size: 13px; }
.eval-bands span.reached { border-color: var(--accent-teal); color: var(--accent-teal); background: var(--accent-teal-soft); }
.eval-facts { display: flex; flex-wrap: wrap; gap: 13px; margin-top: 10px; color: var(--text-secondary); font-size: 13px; }
.eval-summary__scope { margin: 10px 0 0; color: var(--text-muted); font-size: 12px; }
.eval-cases { min-height: 0; margin: 15px 26px 0; padding: 0 0 4px; overflow-y: auto; list-style: none; display: grid; gap: 5px; }
.eval-case { border: 1px solid var(--border-subtle); border-radius: 10px; background: var(--bg-surface); }
.eval-case__row { display: flex; align-items: center; gap: 10px; width: 100%; padding: 9px 12px; border: 0; color: var(--text-secondary); text-align: left; background: transparent; font-size: 14px; }
.eval-case__badge { display: grid; width: 19px; height: 19px; flex-shrink: 0; place-items: center; border-radius: 50%; font-size: 13px; }
.eval-case.pass .eval-case__badge { color: var(--text-on-brand); background: var(--accent-teal); }
.eval-case.fail .eval-case__badge { color: var(--text-on-brand); background: var(--state-error); }
.eval-case__id { flex-shrink: 0; color: var(--text-faint); font-size: 12px; }
.eval-case__q { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.eval-case__ms { margin-left: auto; flex-shrink: 0; color: var(--text-muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.eval-case__detail { padding: 4px 13px 13px 42px; }
.eval-case__reason { display: flex; gap: 7px; margin: 0; color: var(--text-secondary); font-size: 13px; }
.eval-case__reason b { flex-shrink: 0; color: var(--text-muted); font-weight: 500; }
.eval-case__note { margin: 6px 0 0; color: var(--state-warning); font-size: 12px; }
.eval-case__sub { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 7px; color: var(--text-muted); font-size: 12px; }
.eval-case__sql { margin: 9px 0 0; padding: 9px 11px; overflow-x: auto; border-radius: 8px; color: var(--code-text); background: var(--code-bg); font-size: 13px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.eval-case__answer { margin: 9px 0 0; padding-left: 10px; border-left: 2px solid var(--border-subtle); color: var(--text-secondary); font-size: 13px; line-height: 1.7; }
.eval-case__tools { margin: 8px 0 0; color: var(--text-muted); font-size: 12px; }

/* 首处差异：判 fail 时最该被看到的一行，用警示色单独抬出来 */
.eval-case__diff { display: flex; gap: 7px; margin: 6px 0 0; padding: 7px 9px; border-radius: 7px; color: var(--state-warning); background: var(--state-warning-soft); font-size: 13px; }
.eval-case__diff b { flex-shrink: 0; font-weight: 600; }

/* 标准要点 / 期望来源：都是「该做到什么」的清单，共用一套列表样式 */
.eval-case__points, .eval-case__refs { margin-top: 8px; color: var(--text-secondary); font-size: 13px; }
.eval-case__points b, .eval-case__refs b { display: block; margin-bottom: 4px; color: var(--text-muted); font-size: 12px; font-weight: 500; }
.eval-case__points ul, .eval-case__refs ul { display: grid; gap: 3px; margin: 0; padding: 0; list-style: none; }
.eval-case__points li, .eval-case__refs li { display: flex; align-items: baseline; gap: 7px; line-height: 1.6; }
.eval-case__points li::before, .eval-case__refs li::before { content: '·'; flex-shrink: 0; color: var(--text-faint); }
.eval-case__points li.missed { color: var(--state-warning); }
.eval-case__points li em { flex-shrink: 0; padding: 1px 6px; border-radius: 5px; background: var(--state-warning-soft); font-size: 11px; font-style: normal; }
.eval-case__forbidden { margin: 8px 0 0; color: var(--text-secondary); font-size: 13px; }
.eval-case__forbidden b { margin-right: 7px; color: var(--text-muted); font-size: 12px; font-weight: 500; }
.eval-case__forbidden .hit { color: var(--state-error); }

/* 标准 vs 实际：两栏对照。窄屏叠成一栏，否则两张窄表会各自横向滚动 */
.eval-compare { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 10px; }
.eval-compare__col { min-width: 0; }
.eval-compare__col h4 { margin: 0; color: var(--text-muted); font-size: 12px; font-weight: 500; }
.eval-compare__empty { margin: 9px 0 0; color: var(--text-faint); font-size: 12px; }
.eval-compare__meta { display: flex; gap: 7px; margin: 6px 0 0; color: var(--text-muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.eval-compare__meta .bad { color: var(--state-error); }
.eval-compare__hint { grid-column: 1 / -1; margin: 0; color: var(--text-faint); font-size: 12px; }
.eval-rows { width: 100%; margin-top: 7px; border: 1px solid var(--border-subtle); border-radius: 7px; border-collapse: separate; border-spacing: 0; overflow: hidden; font-size: 12px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.eval-rows td { padding: 5px 7px; border-bottom: 1px solid var(--border-subtle); color: var(--text-secondary); word-break: break-all; }
.eval-rows tr:last-child td { border-bottom: 0; }
.eval__hint { margin: 16px 26px 26px; color: var(--text-muted); font-size: 14px; }
@keyframes eval-blink { 50% { opacity: .35; } }
@media (max-width: 767px) {
  .eval-backdrop { padding: 0; }
  .eval { width: 100%; max-height: 100dvh; height: 100dvh; border-radius: 0; }
  .eval__head, .eval__suites, .eval__actions, .eval-summary, .eval-cases, .eval__hint, .eval__scoring, .eval__error, .eval__bar { padding-left: 0; padding-right: 0; }
  .eval__head { padding: 16px 18px 14px; }
  .eval__suites { grid-template-columns: 1fr; margin: 0 18px; padding: 16px 0 0; }
  .eval__scoring, .eval__actions, .eval-summary, .eval-cases, .eval__hint, .eval__error, .eval__bar { margin-left: 18px; margin-right: 18px; }
  .eval__actions { flex-wrap: wrap; padding-top: 12px; }
  .eval__live { width: 100%; margin-left: 0; }
  .eval-case__detail { padding-left: 13px; }
  /* 两栏对照在窄屏会各剩不到 160px，两张表都要横向拖。叠起来读更顺。 */
  .eval-compare { grid-template-columns: 1fr; }
}
@media (prefers-reduced-motion: reduce) {
  .eval__live i { animation: none; }
}
</style>
