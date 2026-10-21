<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import AppIcon from './AppIcon.vue'
import { getDatabaseSchema, getDatabaseStatus, getUnstructuredDatabase, getUnstructuredDatabaseStatus } from '../api/database'
import type { DatabaseSchema, DatabaseStatus, DatabaseTable, UnstructuredDatabase, UnstructuredDatabaseStatus } from '../api/database'
import { getCrossSourceDemoReadiness, type CrossSourceDemoReadiness } from '../api/capabilities'

const emit = defineEmits<{ close: [] }>()
const props = withDefaults(defineProps<{ statusOnly?: boolean }>(), { statusOnly: false })

const structured = ref<DatabaseStatus | null>(null)
const unstructured = ref<UnstructuredDatabaseStatus | null>(null)
const schema = ref<DatabaseSchema | null>(null)
const unstructuredDatabase = ref<UnstructuredDatabase | null>(null)
const crossSourceDemo = ref<CrossSourceDemoReadiness | null>(null)
const error = ref('')
const loading = ref(true)
const activeView = ref<'overview' | 'structured' | 'documents' | 'cross-source'>('overview')
const expandedTables = ref<Set<string>>(new Set())
const crossSourceTables = [
  { name: 'work_orders', description: '工单与设备、客户等基本信息' },
  { name: 'work_order_events', description: '工单受理、到场、恢复等事件时间' },
  { name: 'equipment', description: '设备编号、名称及所属范围' },
  { name: 'contracts', description: '合同编号、客户和合同状态' },
  { name: 'contract_equipment', description: '设备与合同之间的受控映射' },
]

const structuredState = computed(() => structured.value?.status ?? null)
const unstructuredState = computed(() => unstructured.value?.status ?? null)
const crossSourceStatus = computed(() => {
  if (loading.value && !crossSourceDemo.value) return 'checking'
  return crossSourceDemo.value?.status ?? 'unavailable'
})

function statusText(value: string | null): string {
  if (!value) return '状态未知'
  if (value === 'connected' || value === 'catalog_mismatch') return '已连接'
  if (value === 'memory') return '内存模式（未连接数据库）'
  if (value === 'not_used') return '未启用'
  if (value === 'not_initialized') return '未初始化'
  return '未连接'
}

function statusClass(value: string | null): string {
  if (value === 'catalog_mismatch') return 'connected'
  return value ?? 'unknown'
}

function crossSourceStatusText(): string {
  if (crossSourceStatus.value === 'ready') return '已就绪'
  if (crossSourceStatus.value === 'disabled') return '未启用'
  if (crossSourceStatus.value === 'checking') return '检查中'
  return '暂不可用'
}

function crossSourceStatusClass(): string {
  if (crossSourceStatus.value === 'ready') return 'connected'
  if (crossSourceStatus.value === 'disabled') return 'not_used'
  if (crossSourceStatus.value === 'unavailable') return 'unavailable'
  return 'unknown'
}

async function load() {
  loading.value = true
  error.value = ''
  const [structuredResult, schemaResult, unstructuredResult, unstructuredDatabaseResult, crossSourceResult] = await Promise.allSettled([
    getDatabaseStatus(),
    props.statusOnly ? Promise.resolve(null) : getDatabaseSchema(),
    getUnstructuredDatabaseStatus(),
    props.statusOnly ? Promise.resolve(null) : getUnstructuredDatabase(),
    getCrossSourceDemoReadiness(),
  ])
  if (structuredResult.status === 'fulfilled') structured.value = structuredResult.value
  else { structured.value = null; error.value = '暂时无法读取数据库连接状态。' }
  if (schemaResult.status === 'fulfilled') schema.value = schemaResult.value
  else { schema.value = null; error.value = '暂时无法读取数据库结构。' }
  if (unstructuredResult.status === 'fulfilled') unstructured.value = unstructuredResult.value
  else { unstructured.value = null; error.value = '暂时无法读取数据库连接状态。' }
  if (unstructuredDatabaseResult.status === 'fulfilled') unstructuredDatabase.value = unstructuredDatabaseResult.value
  else { unstructuredDatabase.value = null; error.value = '暂时无法读取文档索引。' }
  crossSourceDemo.value = crossSourceResult.status === 'fulfilled' ? crossSourceResult.value : null
  loading.value = false
}

function tableKey(table: DatabaseTable): string {
  return `${table.schema}.${table.name}`
}

function toggleTable(table: DatabaseTable) {
  const key = tableKey(table)
  const next = new Set(expandedTables.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expandedTables.value = next
}

function statusCount(value: number | null | undefined, suffix: string): string {
  return value === null || value === undefined ? `—${suffix}` : `${value}${suffix}`
}

onMounted(() => { void load() })
</script>

<template>
  <div class="database-backdrop" @click.self="emit('close')">
    <section class="database-dialog" role="dialog" aria-modal="true" aria-labelledby="database-title" @keydown.esc.prevent.stop="emit('close')">
      <header class="database-head">
        <span class="database-head__mark"><img src="/brand/icons/database-catalog.svg" alt="" aria-hidden="true" /></span>
        <div class="database-head__title">
          <h2 id="database-title">{{ statusOnly ? '数据库连接状态' : '数据库概览' }}</h2>
          <p>{{ statusOnly ? '三类来源当前是否可用' : '业务数据库、文档资料与跨源演示库' }}</p>
        </div>
        <div class="database-head__actions">
          <button class="database-retry vm-press" type="button" :disabled="loading" @click="load"><AppIcon name="retry" :size="14" />重新检查</button>
          <button class="icon-button close-button vm-press" type="button" aria-label="关闭数据库面板" @click="emit('close')"><AppIcon name="close" :size="18" /></button>
        </div>
      </header>

      <div class="database-body" :aria-busy="loading">
        <p v-if="error" class="database-error" role="alert">{{ error }}</p>
        <div class="database-status-grid">
          <section class="database-status-card" aria-label="结构化业务库连接状态" role="status">
            <span class="database-status-card__label"><AppIcon name="database" :size="16" />结构化业务库</span>
            <strong v-if="!statusOnly">{{ structured?.label || 'Chinook 业务库' }}</strong>
            <span class="database-status-card__result"><i class="database-status-dot" :class="statusClass(structuredState)" aria-hidden="true" />{{ statusText(structuredState) }}</span>
            <small v-if="!statusOnly">{{ statusCount(schema?.table_count ?? structured?.table_count, ' 张表') }} · {{ structured?.dialect?.toUpperCase() || '—' }}</small>
          </section>
          <section class="database-status-card" aria-label="文档向量库连接状态" role="status">
            <span class="database-status-card__label"><AppIcon name="document" :size="16" />文档向量库</span>
            <strong v-if="!statusOnly">{{ unstructuredDatabase?.label || '文档向量库' }}</strong>
            <span class="database-status-card__result"><i class="database-status-dot" :class="statusClass(unstructuredState)" aria-hidden="true" />{{ statusText(unstructuredState) }}</span>
            <small v-if="!statusOnly">{{ statusCount(unstructuredDatabase?.corpus_document_count, ' 份文档') }} · {{ statusCount(unstructuredDatabase?.chunk_count, ' 个切片') }}</small>
          </section>
          <section class="database-status-card" aria-label="跨源演示库连接状态" role="status">
            <span class="database-status-card__label database-status-card__label--cross"><AppIcon name="cross-source" :size="16" />跨源演示库</span>
            <strong v-if="!statusOnly">cross_source_demo</strong>
            <span class="database-status-card__result"><i class="database-status-dot" :class="crossSourceStatusClass()" aria-hidden="true" />{{ crossSourceStatusText() }}</span>
            <small v-if="!statusOnly">PostgreSQL · 隔离的合成数据</small>
          </section>
        </div>

        <nav v-if="!statusOnly" class="database-tabs" role="tablist" aria-label="数据库详情分类">
          <button type="button" role="tab" :aria-selected="activeView === 'overview'" :class="{ active: activeView === 'overview' }" @click="activeView = 'overview'">概览</button>
          <button type="button" role="tab" :aria-selected="activeView === 'structured'" :class="{ active: activeView === 'structured' }" @click="activeView = 'structured'">业务表 <small>{{ schema?.table_count ?? '—' }}</small></button>
          <button type="button" role="tab" :aria-selected="activeView === 'documents'" :class="{ active: activeView === 'documents' }" @click="activeView = 'documents'">文档索引 <small>{{ unstructuredDatabase?.corpus_document_count ?? '—' }}</small></button>
          <button type="button" role="tab" :aria-selected="activeView === 'cross-source'" :class="{ active: activeView === 'cross-source' }" @click="activeView = 'cross-source'">跨源演示库 <small>5 类表</small></button>
        </nav>

        <div v-if="statusOnly" class="database-status-only" role="status">仅展示数据库连接状态</div>
        <div v-else-if="activeView === 'overview'" class="database-overview" role="tabpanel">
          <div class="database-overview__intro"><div><b>数据源内容</b><span>只读目录，不展示业务行数据或连接密钥。</span></div><button type="button" class="database-link" @click="activeView = 'structured'">查看业务表 <AppIcon name="arrow" :size="13" /></button></div>
          <div class="database-overview__metrics">
            <div><span>业务表</span><b>{{ schema?.table_count ?? '—' }}</b></div>
            <div><span>字段</span><b>{{ schema?.tables.reduce((total, table) => total + table.column_count, 0) ?? '—' }}</b></div>
            <div><span>文档</span><b>{{ unstructuredDatabase?.corpus_document_count ?? '—' }}</b></div>
            <div><span>向量切片</span><b>{{ unstructuredDatabase?.chunk_count ?? '—' }}</b></div>
            <div><span>跨源关联表</span><b>5</b></div>
          </div>
          <div class="database-overview__split">
            <section><h3><AppIcon name="database" :size="14" />业务库表</h3><ul><li v-for="table in (schema?.tables || []).slice(0, 5)" :key="tableKey(table)"><span>{{ table.schema }}.{{ table.name }}</span><small>{{ table.column_count }} 字段</small></li></ul><button type="button" class="database-link" @click="activeView = 'structured'">查看全部 {{ schema?.table_count ?? 0 }} 张表 <AppIcon name="arrow" :size="13" /></button></section>
            <section><h3><AppIcon name="document" :size="14" />文档索引</h3><ul><li v-for="document in (unstructuredDatabase?.documents || []).slice(0, 5)" :key="document.doc_id"><span>{{ document.title }}</span><small>{{ document.extension.toUpperCase() }}</small></li></ul><button type="button" class="database-link" @click="activeView = 'documents'">查看全部 {{ unstructuredDatabase?.corpus_document_count ?? 0 }} 份文档 <AppIcon name="arrow" :size="13" /></button></section>
            <section><h3><AppIcon name="cross-source" :size="14" />跨源演示库</h3><ul><li><span>cross_source_demo.work_orders</span><small>工单</small></li><li><span>cross_source_demo.contracts</span><small>合同</small></li><li><span>cross_source_demo.work_order_events</span><small>事件</small></li></ul><button type="button" class="database-link" @click="activeView = 'cross-source'">查看 5 类关联数据表 <AppIcon name="arrow" :size="13" /></button></section>
          </div>
        </div>

        <div v-else-if="activeView === 'structured'" class="database-detail-list" role="tabpanel">
          <div v-if="!schema?.tables.length && !loading" class="database-empty">暂无可展示的业务表结构。</div>
          <article v-for="table in schema?.tables || []" :key="tableKey(table)" class="database-table">
            <button type="button" class="database-table__toggle" :aria-expanded="expandedTables.has(tableKey(table))" @click="toggleTable(table)"><span><b>{{ table.schema }}.{{ table.name }}</b><small>{{ table.description || '暂无表说明' }}</small></span><em>{{ table.column_count }} 字段</em><AppIcon name="chevron" :size="14" /></button>
            <div v-if="expandedTables.has(tableKey(table))" class="database-table__columns"><div class="database-column database-column--head"><span>字段</span><span>类型</span><span>约束</span><span>说明</span></div><div v-for="column in table.columns" :key="column.name" class="database-column"><span><b>{{ column.name }}</b><small v-if="column.foreign_key_to">→ {{ column.foreign_key_to }}</small></span><span>{{ column.type || '—' }}</span><span>{{ column.is_primary_key ? '主键' : column.nullable ? '可空' : '必填' }}</span><span>{{ column.description || '—' }}</span></div></div>
          </article>
        </div>

        <div v-else-if="activeView === 'documents'" class="database-detail-list" role="tabpanel">
          <div class="database-doc-summary"><b>{{ unstructuredDatabase?.label || '文档向量库' }}</b><span>{{ statusText(unstructuredState) }} · {{ unstructuredDatabase?.backend || '—' }}</span><small v-if="unstructuredDatabase?.warning">{{ unstructuredDatabase.warning }}</small></div>
          <div v-if="!unstructuredDatabase?.documents.length && !loading" class="database-empty">暂无可展示的文档索引。</div>
          <article v-for="document in unstructuredDatabase?.documents || []" :key="document.doc_id" class="database-document"><span class="database-document__badge">{{ document.extension.toUpperCase() }}</span><div><b>{{ document.title }}</b><small>{{ document.doc_id }}</small></div><em>{{ document.indexed_chunk_count ?? '—' }} 切片</em></article>
        </div>
        <div v-else class="database-detail-list" role="tabpanel">
          <div class="database-doc-summary"><b>跨源演示库 · cross_source_demo</b><span>{{ crossSourceStatusText() }} · PostgreSQL</span><small>独立合成数据，只服务工单与合同的跨源演示；合同正文由文档资料提供。不会与 Chinook 或 AdventureWorks 合并。</small></div>
          <article v-for="table in crossSourceTables" :key="table.name" class="database-document">
            <span class="database-document__badge">SQL</span><div><b>cross_source_demo.{{ table.name }}</b><small>{{ table.description }}</small></div>
          </article>
          <p class="database-footnote">该演示库只读。系统会将结构化记录与合同原文对应核对；当前状态仅表示演示服务可用，不等于所有跨源场景均已验证。</p>
        </div>
        <p class="database-footnote">{{ statusOnly ? '点击“重新检查”可刷新连接状态。' : '点击“重新检查”可刷新连接状态、表结构与文档索引。' }}</p>
      </div>
    </section>
  </div>
</template>

<style scoped>
.database-backdrop { position: fixed; inset: 0; z-index: 230; display: grid; place-items: center; padding: 22px; background: var(--scrim); backdrop-filter: blur(3px); }
.database-dialog { display: flex; flex-direction: column; width: min(820px, 100%); max-height: min(780px, 90dvh); overflow: hidden; border: 1px solid var(--border-subtle); border-radius: 18px; background: var(--bg-elevated); box-shadow: var(--shadow-guide); }
.database-head { display: flex; align-items: center; gap: 12px; padding: 19px 21px 16px; border-bottom: 1px solid var(--border-subtle); }
.database-head__mark { display: grid; width: 43px; height: 43px; flex: 0 0 43px; place-items: center; border: 1px solid var(--border-brand); border-radius: 12px; background: var(--brand-soft); }
.database-head__mark img { width: 32px; height: 32px; }
.database-head__title { flex: 1; min-width: 0; }
.database-head h2 { margin: 0; color: var(--text-heading); font-size: 19px; }
.database-head p { margin: 3px 0 0; color: var(--text-secondary); font-size: 13px; }
.database-head__actions { display: flex; align-items: center; gap: 8px; }
.database-retry { display: inline-flex; align-items: center; gap: 6px; padding: 7px 10px; border: 1px solid var(--border-subtle); border-radius: 8px; color: var(--text-secondary); background: var(--bg-surface); font-size: 13px; }
.database-retry:hover:not(:disabled) { border-color: var(--border-brand); color: var(--brand-primary); }
.database-retry:disabled { opacity: .6; cursor: wait; }
.database-body { display: flex; flex: 1; min-height: 0; flex-direction: column; padding: 18px 21px 17px; }
.database-error { margin: 0 0 10px; color: var(--state-error); font-size: 12px; }
.database-status-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 11px; }
.database-status-card { display: grid; gap: 7px; min-width: 0; padding: 14px 15px; border: 1px solid var(--border-subtle); border-radius: 12px; background: var(--bg-surface); }
.database-status-card__label { display: flex; align-items: center; gap: 8px; color: var(--text-secondary); font-size: 14px; }
.database-status-card__label svg { color: var(--brand-primary); }
.database-status-card__label--cross svg { color: var(--accent-teal); }
.database-status-card strong { overflow: hidden; color: var(--text-primary); font-size: 14px; text-overflow: ellipsis; white-space: nowrap; }
.database-status-card__result { display: flex; align-items: center; gap: 8px; color: var(--text-primary); font-size: 14px; font-weight: 600; }
.database-status-card > small { color: var(--text-muted); font-size: 11px; }
.database-status-only { flex: 1; min-height: 60px; display: grid; place-items: center; color: var(--text-muted); font-size: 12px; }
.database-status-dot { width: 9px; height: 9px; flex: 0 0 9px; border-radius: 50%; background: var(--state-neutral); }
.database-status-dot.connected { background: var(--accent-teal); box-shadow: 0 0 0 3px var(--accent-teal-soft); }
.database-status-dot.memory { background: var(--brand-primary); box-shadow: 0 0 0 3px var(--brand-soft); }
.database-status-dot.not_used { background: var(--state-warning); }
.database-status-dot.unavailable, .database-status-dot.not_initialized { background: var(--state-error); }
.database-tabs { display: flex; gap: 5px; margin-top: 16px; padding-bottom: 9px; border-bottom: 1px solid var(--border-subtle); }
.database-tabs button { display: inline-flex; align-items: center; gap: 5px; padding: 7px 11px; border: 1px solid transparent; border-radius: 8px; color: var(--text-secondary); background: transparent; font-size: 13px; font-weight: 600; }
.database-tabs button:hover { color: var(--brand-primary); background: var(--brand-soft); }
.database-tabs button.active { border-color: var(--border-brand); color: var(--brand-primary); background: var(--brand-soft); }
.database-tabs button small { color: var(--text-muted); font-size: 11px; font-weight: 600; }
.database-tabs button.active small { color: var(--brand-primary); }
.database-overview, .database-detail-list { min-height: 0; overflow: auto; padding-top: 14px; }
.database-overview__intro { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.database-overview__intro div { display: grid; gap: 3px; }
.database-overview__intro b { color: var(--text-primary); font-size: 14px; }
.database-overview__intro span { color: var(--text-muted); font-size: 12px; }
.database-link { display: inline-flex; align-items: center; gap: 4px; padding: 0; border: 0; color: var(--brand-primary); background: transparent; font-size: 12px; white-space: nowrap; }
.database-link:hover { color: var(--brand-deep); text-decoration: underline; }
.database-overview__metrics { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; margin-top: 12px; }
.database-overview__metrics div { display: grid; gap: 3px; padding: 10px 11px; border: 1px solid var(--border-subtle); border-radius: 9px; background: var(--bg-subtle); }
.database-overview__metrics span { color: var(--text-muted); font-size: 10px; }
.database-overview__metrics b { color: var(--text-heading); font-size: 18px; font-variant-numeric: tabular-nums; }
.database-overview__split { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 11px; margin-top: 12px; }
.database-overview__split section { min-width: 0; padding: 13px; border: 1px solid var(--border-subtle); border-radius: 11px; background: var(--bg-surface); }
.database-overview__split h3 { display: flex; align-items: center; gap: 6px; margin: 0 0 9px; color: var(--text-primary); font-size: 13px; }
.database-overview__split h3 svg { color: var(--brand-primary); }
.database-overview__split ul { display: grid; gap: 5px; margin: 0 0 9px; padding: 0; list-style: none; }
.database-overview__split li { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-width: 0; color: var(--text-secondary); font-size: 12px; }
.database-overview__split li span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.database-overview__split li small { flex: none; color: var(--text-muted); font-size: 11px; }
.database-table { margin-bottom: 7px; border: 1px solid var(--border-subtle); border-radius: 10px; background: var(--bg-surface); }
.database-table__toggle { display: flex; align-items: center; gap: 10px; width: 100%; padding: 11px 12px; border: 0; color: var(--text-primary); text-align: left; background: transparent; }
.database-table__toggle:hover { background: var(--bg-hover); }
.database-table__toggle > span { display: grid; flex: 1; min-width: 0; gap: 3px; }
.database-table__toggle b { overflow: hidden; color: var(--text-primary); font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.database-table__toggle small { overflow: hidden; color: var(--text-muted); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.database-table__toggle em { flex: none; color: var(--text-muted); font-size: 11px; font-style: normal; }
.database-table__toggle svg { flex: none; color: var(--text-muted); transition: transform var(--motion-fast) var(--motion-ease); }
.database-table__toggle[aria-expanded="true"] svg { transform: rotate(90deg); color: var(--brand-primary); }
.database-table__columns { overflow-x: auto; border-top: 1px solid var(--border-subtle); }
.database-column { display: grid; grid-template-columns: minmax(100px, 1fr) minmax(90px, .8fr) 70px minmax(150px, 1.7fr); gap: 9px; padding: 8px 12px; border-bottom: 1px solid var(--border-subtle); color: var(--text-secondary); font-size: 12px; }
.database-column:last-child { border-bottom: 0; }
.database-column--head { color: var(--text-muted); background: var(--bg-subtle); font-size: 11px; }
.database-column > span { min-width: 0; overflow-wrap: anywhere; }
.database-column b { color: var(--text-primary); font-weight: 600; }
.database-column small { display: block; margin-top: 2px; color: var(--brand-primary); font-size: 10px; }
.database-doc-summary { display: flex; flex-wrap: wrap; align-items: baseline; gap: 7px; margin-bottom: 9px; }
.database-doc-summary b { color: var(--text-primary); font-size: 14px; }
.database-doc-summary span { color: var(--text-muted); font-size: 12px; }
.database-doc-summary small { flex-basis: 100%; color: var(--state-warning); font-size: 12px; }
.database-document { display: flex; align-items: center; gap: 9px; min-width: 0; padding: 9px 10px; border: 1px solid var(--border-subtle); border-radius: 9px; background: var(--bg-surface); }
.database-document + .database-document { margin-top: 6px; }
.database-document__badge { display: grid; width: 34px; height: 26px; flex: 0 0 34px; place-items: center; border-radius: 6px; color: var(--brand-primary); background: var(--brand-soft); font-size: 10px; font-weight: 700; }
.database-document > div { display: grid; flex: 1; min-width: 0; gap: 2px; }
.database-document b, .database-document small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.database-document b { color: var(--text-primary); font-size: 12px; }
.database-document small { color: var(--text-muted); font-size: 11px; }
.database-document em { flex: none; color: var(--text-muted); font-size: 11px; font-style: normal; }
.database-empty { padding: 25px 10px; color: var(--text-muted); text-align: center; font-size: 13px; }
.database-footnote { margin: 14px 1px 0; color: var(--text-muted); font-size: 12px; }
@media (max-width: 560px) {
  .database-backdrop { padding: 12px; }
  .database-dialog { max-height: 92dvh; }
  .database-head { gap: 9px; padding: 15px 13px; }
  .database-head__mark { width: 37px; height: 37px; flex-basis: 37px; }
  .database-head__mark img { width: 28px; height: 28px; }
  .database-head h2 { font-size: 17px; }
  .database-head p { font-size: 11px; }
  .database-head__actions { gap: 5px; }
  .database-retry { gap: 4px; padding: 7px; font-size: 0; }
  .database-body { padding: 15px 13px; }
  .database-status-grid { grid-template-columns: 1fr; }
  .database-status-card { padding: 12px; }
  .database-tabs { margin-top: 12px; }
  .database-tabs button { flex: 1; justify-content: center; padding: 7px 5px; font-size: 12px; }
  .database-overview__metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .database-overview__split { grid-template-columns: 1fr; }
  .database-column { grid-template-columns: minmax(100px, 1fr) 80px 58px 150px; }
}
</style>
