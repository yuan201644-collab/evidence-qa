<script setup lang="ts">
import { computed, ref } from 'vue'
import AppIcon from './AppIcon.vue'
import { EVIDENCE_KIND_META, fetchTasksEvidence } from '../api/tasks'
import { getEvidenceUrl } from '../api/evidence'
import type { TaskEvidenceItem, TasksEvidence } from '../api/tasks'

const emit = defineEmits<{ close: [] }>()

const data = ref<TasksEvidence | null>(null)
const loading = ref(true)
const loadError = ref('')
const activeId = ref<number | null>(null)
const expanded = ref<number | null>(null)

/**
 * 面板的核心主张：中级任务不是「我们声称做了」，而是「有报告可查」。
 * 所以每个任务都必须能顺着 evidence 找到文件，known_gaps 也必须露出来 ——
 * 只报喜的举证在答辩现场一问就穿。
 */
const tasks = computed<TaskEvidenceItem[]>(() => data.value?.tasks ?? [])
const verified = computed(() => tasks.value.filter((item) => item.status === 'verified').length)
const active = computed(() => tasks.value.find((item) => item.id === activeId.value) ?? tasks.value[0] ?? null)

/** 评分表口径：初赛每攻克 1 个基础分 5 分、上限 15；决赛 5 个及以上 15 分。 */
const scoringNote = computed(() => {
  if (!verified.value) return ''
  return `按评分表口径：初赛每攻克 1 个中级任务得 5 分（上限 15 分）；决赛攻克 5 个及以上得 15 分。当前可举证 ${verified.value} 个。`
})

function percent(ratio: number | undefined): string {
  if (typeof ratio !== 'number') return '0%'
  return `${Math.round(Math.max(0, Math.min(1, ratio)) * 100)}%`
}

function toggle(id: number) {
  expanded.value = expanded.value === id ? null : id
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const body = await fetchTasksEvidence()
    if (body.error) throw new Error(body.error)
    data.value = body
    activeId.value = body.tasks[0]?.id ?? null
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '读取中级任务证据失败'
  } finally {
    loading.value = false
  }
}

void load()
</script>

<template>
  <div class="tasks-backdrop" @click.self="emit('close')">
    <section class="tasks" role="dialog" aria-modal="true" aria-labelledby="tasks-title">
      <header class="tasks__head">
        <div class="tasks__title">
          <span class="tasks__mark"><AppIcon name="layers" :size="20" /></span>
          <div>
            <h2 id="tasks-title">中级任务能力</h2>
            <p>每个任务对应哪份报告、数字是多少、哪些还没覆盖</p>
          </div>
        </div>
        <button class="icon-button close-button vm-press" aria-label="关闭中级任务能力" @click="emit('close')"><AppIcon name="close" :size="18" /></button>
      </header>

      <div v-if="loading" class="tasks__state" role="status">正在读取任务证据…</div>
      <div v-else-if="loadError" class="tasks__state tasks__state--error" role="alert">
        {{ loadError }}<button class="tasks__retry" @click="load">重试</button>
      </div>

      <template v-else>
        <div class="tasks__lead">
          <div class="tasks__score">
            <b>{{ verified }}</b>
            <span>个任务有报告可查</span>
          </div>
          <p v-if="scoringNote" class="tasks__scoring">{{ scoringNote }}</p>
        </div>

        <div class="tasks__body">
          <!-- 左：任务列表，一眼看到全部状态 -->
          <nav class="tasks__list" aria-label="中级任务列表">
            <button
              v-for="task in tasks"
              :key="task.id"
              class="tasks-tab"
              :class="{ active: task.id === active?.id }"
              :aria-current="task.id === active?.id"
              @click="activeId = task.id"
            >
              <span class="tasks-tab__no">任务 {{ task.id }}</span>
              <span class="tasks-tab__name">{{ task.title }}</span>
              <span class="tasks-tab__head">{{ task.headline }}</span>
            </button>
          </nav>

          <!-- 右：选中任务的详情。机制与限制都要给，不能只给数字 -->
          <article v-if="active" class="tasks-detail">
            <header class="tasks-detail__head">
              <div>
                <h3>{{ active.title }}</h3>
                <p>{{ active.subtitle }}</p>
              </div>
              <span class="tasks-detail__status" :class="active.status">
                <AppIcon :name="active.status === 'verified' ? 'check' : 'clock'" :size="13" />{{ active.status_label }}
              </span>
            </header>

            <div class="tasks-metrics">
              <div v-for="metric in active.metrics" :key="metric.label" class="tasks-metric">
                <div class="tasks-metric__top">
                  <span class="tasks-metric__label">{{ metric.label }}</span>
                  <b>{{ metric.value }}</b>
                </div>
                <div v-if="typeof metric.ratio === 'number'" class="tasks-metric__bar">
                  <span :style="{ width: percent(metric.ratio) }" :class="{ full: metric.ratio >= 1 }" />
                </div>
                <p v-if="metric.help" class="tasks-metric__help">{{ metric.help }}</p>
              </div>
            </div>

            <section class="tasks-block">
              <h4>怎么做到的</h4>
              <p>{{ active.mechanism }}</p>
            </section>

            <section class="tasks-block">
              <h4>证据出处</h4>
              <ul class="tasks-evidence">
                <li v-for="item in active.evidence" :key="item.path">
                  <span class="tasks-evidence__kind">
                    <AppIcon :name="EVIDENCE_KIND_META[item.kind].icon" :size="12" />{{ EVIDENCE_KIND_META[item.kind].label }}
                  </span>
                  <a class="tasks-evidence__path" :href="getEvidenceUrl(item.path)" target="_blank" rel="noreferrer">{{ item.path }}</a>
                  <small v-if="item.note">{{ item.note }}</small>
                </li>
              </ul>
            </section>

            <section v-if="active.known_gaps.length" class="tasks-block tasks-block--gap">
              <h4><AppIcon name="warning" :size="13" />还没覆盖的</h4>
              <ul>
                <li v-for="gap in active.known_gaps" :key="gap">{{ gap }}</li>
              </ul>
            </section>

            <p class="tasks-detail__scope">{{ active.scope }}</p>
          </article>
        </div>

        <!-- 底部说明：把「这些数字的边界」讲清楚，避免被当成比赛总分 -->
        <p v-if="data?.suite_note" class="tasks__note">{{ data.suite_note }}</p>

        <details class="tasks__gaps">
          <summary>为什么有些任务还不能写成「已攻克」？</summary>
          <p v-if="data?.audience_note">{{ data.audience_note }}</p>
          <p>本面板只登记有运行报告可查的结论。查不到的写进每个任务的「还没覆盖的」，不写成已完成。</p>
        </details>
      </template>
    </section>
  </div>
</template>

<style scoped>
.tasks-backdrop { position: fixed; inset: 0; z-index: 210; display: grid; place-items: center; padding: 22px; background: var(--scrim); backdrop-filter: blur(3px); }
.tasks { display: flex; flex-direction: column; width: min(980px, 100%); max-height: 90dvh; overflow: hidden; border-radius: 20px; background: var(--bg-elevated); box-shadow: var(--shadow-guide); }
.tasks__head { display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; gap: 12px; padding: 22px 26px 18px; border-bottom: 1px solid var(--border-subtle); }
.tasks__title { display: flex; align-items: center; gap: 12px; min-width: 0; }
.tasks__mark { display: grid; width: 42px; height: 42px; flex-shrink: 0; place-items: center; border-radius: 12px; color: var(--brand-primary); background: var(--brand-soft); }
.tasks__title h2 { margin: 0; color: var(--text-heading); font-size: 20px; }
.tasks__title p { margin: 3px 0 0; color: var(--text-muted); font-size: 13px; }
.tasks__state { padding: 40px 26px; color: var(--text-secondary); font-size: 15px; }
.tasks__state--error { color: var(--state-error); }
.tasks__retry { margin-left: 10px; padding: 4px 9px; border: 1px solid var(--border-subtle); border-radius: 6px; background: var(--bg-surface); color: var(--text-primary); }

.tasks__lead { display: flex; align-items: baseline; flex-wrap: wrap; gap: 9px 14px; padding: 16px 26px 0; }
.tasks__score { display: flex; align-items: baseline; gap: 7px; }
.tasks__score b { color: var(--accent-teal); font-size: 27px; font-variant-numeric: tabular-nums; }
.tasks__score span { color: var(--text-secondary); font-size: 14px; }
.tasks__scoring { margin: 0; color: var(--text-muted); font-size: 13px; }

.tasks__body { display: grid; grid-template-columns: 232px minmax(0, 1fr); gap: 16px; min-height: 0; padding: 16px 26px 0; }
.tasks__list { display: flex; flex-direction: column; gap: 7px; align-content: start; overflow-y: auto; }
.tasks-tab { display: grid; gap: 3px; padding: 10px 12px; border: 1px solid var(--border-subtle); border-radius: 11px; color: var(--text-secondary); text-align: left; background: var(--bg-surface); transition: border-color .15s, background .15s; }
.tasks-tab:hover { border-color: var(--border-brand); }
.tasks-tab.active { border-color: var(--border-brand); background: var(--brand-soft); }
.tasks-tab__no { color: var(--text-faint); font-size: 12px; letter-spacing: .04em; }
.tasks-tab__name { color: var(--text-primary); font-size: 14.5px; font-weight: 600; }
.tasks-tab__head { color: var(--accent-teal); font-size: 12.5px; font-variant-numeric: tabular-nums; }

.tasks-detail { min-height: 0; overflow-y: auto; padding-bottom: 4px; }
.tasks-detail__head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.tasks-detail__head h3 { margin: 0; color: var(--text-heading); font-size: 17px; }
.tasks-detail__head p { margin: 3px 0 0; color: var(--text-muted); font-size: 13px; }
.tasks-detail__status { display: inline-flex; align-items: center; gap: 5px; flex-shrink: 0; padding: 3px 9px; border-radius: 20px; font-size: 12.5px; }
.tasks-detail__status.verified { color: var(--accent-teal); background: var(--accent-teal-soft); }
.tasks-detail__status.under_review { color: var(--state-warning); background: var(--state-warning-soft); }

.tasks-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(168px, 1fr)); gap: 9px; margin-top: 13px; }
.tasks-metric { padding: 10px 12px; border: 1px solid var(--border-subtle); border-radius: 11px; background: var(--bg-subtle); }
.tasks-metric__top { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.tasks-metric__label { color: var(--text-secondary); font-size: 13px; }
.tasks-metric__top b { color: var(--text-heading); font-size: 17px; font-variant-numeric: tabular-nums; }
.tasks-metric__bar { height: 4px; margin-top: 8px; overflow: hidden; border-radius: 3px; background: var(--bg-surface); }
.tasks-metric__bar span { display: block; height: 100%; background: var(--brand-primary); }
.tasks-metric__bar span.full { background: var(--accent-teal); }
.tasks-metric__help { margin: 7px 0 0; color: var(--text-muted); font-size: 12px; line-height: 1.6; }

.tasks-block { margin-top: 16px; }
.tasks-block h4 { display: flex; align-items: center; gap: 6px; margin: 0 0 7px; color: var(--text-heading); font-size: 14px; }
.tasks-block p { margin: 0; color: var(--text-secondary); font-size: 13.5px; line-height: 1.85; }
.tasks-block--gap h4 { color: var(--state-warning); }
.tasks-block--gap ul { margin: 0; padding-left: 17px; color: var(--text-secondary); font-size: 13.5px; line-height: 1.9; }

.tasks-evidence { margin: 0; padding: 0; display: grid; gap: 6px; list-style: none; }
.tasks-evidence li { display: flex; align-items: baseline; flex-wrap: wrap; gap: 5px 9px; padding: 7px 10px; border: 1px solid var(--border-subtle); border-radius: 9px; background: var(--bg-surface); }
.tasks-evidence__kind { display: inline-flex; align-items: center; gap: 4px; flex-shrink: 0; color: var(--brand-primary); font-size: 12px; }
.tasks-evidence__path { color: var(--text-secondary); font-size: 12.5px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; text-decoration: underline dotted; text-underline-offset: 3px; }
.tasks-evidence__path:hover { color: var(--brand-primary); text-decoration-style: solid; }
.tasks-evidence small { width: 100%; color: var(--text-muted); font-size: 12px; }

.tasks-detail__scope { margin: 14px 0 0; padding-top: 11px; border-top: 1px solid var(--border-subtle); color: var(--text-faint); font-size: 12px; }

.tasks__note { flex-shrink: 0; margin: 14px 26px 0; color: var(--text-muted); font-size: 12.5px; line-height: 1.7; }
.tasks__gaps { flex-shrink: 0; margin: 11px 26px 20px; color: var(--text-secondary); font-size: 13px; }
.tasks__gaps summary { color: var(--text-muted); font-size: 13px; cursor: pointer; }
.tasks__gaps p { margin: 7px 0 0; color: var(--text-muted); font-size: 12.5px; line-height: 1.75; }

@media (max-width: 860px) {
  .tasks__body { grid-template-columns: 1fr; }
  .tasks__list { flex-direction: row; flex-wrap: wrap; overflow: visible; }
  .tasks-tab { flex: 1 1 150px; }
}
@media (max-width: 767px) {
  .tasks-backdrop { padding: 0; }
  .tasks { width: 100%; height: 100dvh; max-height: 100dvh; border-radius: 0; }
  .tasks__head { padding: 16px 18px 14px; }
  .tasks__lead, .tasks__body { padding-left: 18px; padding-right: 18px; }
  .tasks__note, .tasks__gaps { margin-left: 18px; margin-right: 18px; }
  .tasks-detail { overflow: visible; }
  .tasks { overflow-y: auto; }
}
</style>
