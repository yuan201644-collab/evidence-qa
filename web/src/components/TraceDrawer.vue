<script setup lang="ts">
import { computed, ref } from 'vue'
import AppIcon from './AppIcon.vue'
import type { TraceStep } from '../types/contracts'
import { recordedStages, stepLabel } from '../utils/trace'

const props = defineProps<{ trace: TraceStep[] }>()
const open = ref(false)
const expanded = ref<number | null>(null)
const total = computed(() => props.trace.reduce((sum, step) => sum + step.elapsed_ms, 0))
const stages = computed(() => recordedStages(props.trace))
const duration = (milliseconds: number) => milliseconds < 1000 ? Math.round(milliseconds) + ' 毫秒' : (milliseconds / 1000).toFixed(2) + ' 秒'
const pretty = (value: Record<string, unknown>) => Object.keys(value).length ? JSON.stringify(value, null, 2) : '无'
</script>

<template>
  <section v-if="trace.length" class="trace" aria-label="实际处理过程">
    <button class="trace__toggle" :aria-expanded="open" @click="open = !open"><span><AppIcon name="sources" :size="13" />{{ open ? '收起处理详情' : '查看处理详情' }}<small>{{ trace.length }} 步</small></span><span class="trace__time"><AppIcon name="clock" :size="12" />工具累计 {{ duration(total) }}<AppIcon name="chevron" :size="12" :class="{ rotated: open }" /></span></button>
    <div v-if="open" class="trace__body vm-detail">
      <div class="trace-path"><span class="trace-path__label">实际处理路径</span><template v-for="(stage, index) in stages" :key="index"><AppIcon v-if="index" name="chevron" :size="9" /><span>{{ stage }}</span></template></div>
      <article v-for="(step, index) in trace" :key="index" class="trace-step">
        <button class="trace-step__head" :aria-expanded="expanded === index" @click="expanded = expanded === index ? null : index"><span class="step-no">{{ step.step_id }}</span><b>{{ stepLabel(step.tool) }}</b><span class="hit">{{ step.hit === null ? '未记录来源' : (step.hit?.length ?? 0) + ' 条来源' }}</span><time>{{ duration(step.elapsed_ms) }}</time><AppIcon name="chevron" :size="11" :class="{ rotated: expanded === index }" /></button>
        <div v-if="expanded === index" class="trace-step__detail vm-detail"><p>工具标识：<code>{{ step.tool }}</code></p><div><b>输入</b><pre>{{ pretty(step.input) }}</pre></div><div><b>输出</b><pre>{{ pretty(step.output) }}</pre></div></div>
      </article>
    </div>
  </section>
</template>

<style scoped>
.trace { margin-top: 19px; padding-top: 14px; border-top: 1px solid var(--border-subtle); }
.trace-path { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; margin-bottom: 10px; color: var(--text-muted); font-size: 12px; }
.trace-path > span:not(:first-child) { padding: 2px 5px; border-radius: 4px; color: var(--text-secondary); background: var(--bg-subtle); }
.trace-path__label { margin-right: 5px; color: var(--text-muted); }
.trace__toggle { display: flex; align-items: center; justify-content: space-between; gap: 8px; width: 100%; padding: 4px 0; border: 0; color: var(--text-secondary); text-align: left; background: transparent; font-size: 13px; }
.trace__toggle > span { display: flex; align-items: center; gap: 6px; }
.trace__toggle small { color: var(--text-muted); font-size: 12px; }
.trace__toggle .trace__time { color: var(--text-muted); font-size: 12px; }
.trace__toggle :deep(.app-icon), .trace-step__head :deep(.app-icon) { transition: transform var(--motion-fast) var(--motion-ease); }
.rotated { transform: rotate(90deg); }
.trace__body { margin-top: 12px; border: 1px solid var(--border-subtle); border-radius: 9px; overflow: hidden; }
.trace-step + .trace-step { border-top: 1px solid var(--border-subtle); }
.trace-step__head { display: flex; align-items: center; gap: 8px; width: 100%; padding: 10px 11px; border: 0; text-align: left; background: var(--bg-subtle); }
.step-no { display: grid; flex-shrink: 0; width: 20px; height: 20px; place-items: center; border: 1px solid var(--border-subtle); border-radius: 50%; color: var(--text-muted); font-size: 11px; }
.trace-step__head b { color: var(--text-secondary); font-size: 14px; font-weight: 500; }
.hit { margin-left: auto; color: var(--text-muted); font-size: 12px; }
time { color: var(--text-muted); font-size: 12px; }
.trace-step__head > svg { color: var(--text-faint); }
.trace-step__detail { border-top: 1px solid var(--border-subtle); padding: 12px; color: var(--text-secondary); background: var(--bg-surface); }
.trace-step__detail p { margin: 0 0 10px; font-size: 12px; }
.trace-step__detail > div + div { margin-top: 12px; }
.trace-step__detail b { color: var(--text-secondary); font-size: 12px; font-weight: 500; }
.trace-step__detail pre { max-height: 250px; overflow: auto; margin: 4px 0 0; padding: 9px; border-radius: 6px; background: var(--bg-app); font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 767px) { .trace__time > svg:first-child { display: none; } .hit { display: none; } time { margin-left: auto; } }
</style>
