<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { downloadResultSetCsv, getResultSetPage } from '../api/ask'
import type { ResultSetInfo, ResultSetPage } from '../types/contracts'

const props = defineProps<{ resultSet: ResultSetInfo; sessionId: string }>()
const page = ref<ResultSetPage | null>(null)
const loading = ref(false)
const error = ref('')
const exportError = ref('')
const exporting = ref(false)
const pageNumber = ref(0)
const pageCount = computed(() => Math.max(1, Math.ceil(props.resultSet.row_count / props.resultSet.page_size)))

async function exportCsv() {
  exporting.value = true
  exportError.value = ''
  try {
    const blob = await downloadResultSetCsv(props.resultSet.id, props.sessionId)
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = 'query-result.csv'
    document.body.append(anchor)
    anchor.click()
    anchor.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (cause) {
    exportError.value = cause instanceof Error ? cause.message : '下载 CSV 失败。'
  } finally {
    exporting.value = false
  }
}

async function load(offset: number) {
  loading.value = true
  error.value = ''
  try {
    page.value = await getResultSetPage(props.resultSet.id, props.sessionId, offset, props.resultSet.page_size)
    pageNumber.value = Math.floor(page.value.offset / page.value.page_size)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '读取结果失败。'
    page.value = null
  } finally {
    loading.value = false
  }
}

function showCell(value: unknown) {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

watch(() => [props.resultSet.id, props.sessionId], () => { pageNumber.value = 0; void load(0) }, { immediate: true })
</script>

<template>
  <section class="result-set" aria-label="查询结果表格">
    <header class="result-head">
      <div>
        <strong>{{ resultSet.complete ? `完整结果：${resultSet.row_count} 行` : `部分结果：已取得 ${resultSet.row_count} 行` }}</strong>
        <small>结果将在 {{ new Date(resultSet.expires_at).toLocaleTimeString() }} 过期</small>
      </div>
      <button v-if="resultSet.complete" class="export" :disabled="exporting" @click="exportCsv">{{ exporting ? '正在导出…' : '下载 CSV' }}</button>
      <span v-else class="partial">结果不完整，不能导出为完整清单</span>
    </header>

    <p v-if="loading" class="state">正在读取结果…</p>
    <p v-else-if="error" class="state error">{{ error }}</p>
    <p v-if="exportError" class="state error">{{ exportError }}</p>
    <div v-else-if="page" class="table-wrap">
      <table>
        <thead><tr><th v-for="column in page.columns" :key="column">{{ column }}</th></tr></thead>
        <tbody>
          <tr v-for="(row, rowIndex) in page.rows" :key="`${page.offset}-${rowIndex}`">
            <td v-for="column in page.columns" :key="column">{{ showCell(row[column]) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <footer v-if="page && page.row_count > page.page_size" class="pager">
      <button :disabled="loading || pageNumber === 0" @click="load(Math.max(0, (pageNumber - 1) * page.page_size))">上一页</button>
      <span>第 {{ pageNumber + 1 }} / {{ pageCount }} 页</span>
      <button :disabled="loading || page.next_offset === null" @click="load(page.next_offset ?? 0)">下一页</button>
    </footer>
  </section>
</template>

<style scoped>
.result-set { margin-top: 12px; border: 1px solid var(--border-subtle); border-radius: 10px; overflow: hidden; background: var(--bg-surface); }
.result-head, .pager { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 12px; background: var(--bg-subtle); }
.result-head div { display: grid; gap: 3px; }.result-head small, .partial, .state { color: var(--text-secondary); font-size: 14px; }
.export { border: 0; padding: 0; color: var(--brand-primary); font: inherit; font-weight: 700; text-decoration: none; white-space: nowrap; background: transparent; cursor: pointer; }.export:hover { text-decoration: underline; }.export:disabled { opacity: .55; cursor: default; }
.table-wrap { max-height: 360px; overflow: auto; } table { width: 100%; border-collapse: collapse; font-size: 14px; white-space: nowrap; }
th, td { border-bottom: 1px solid var(--border-subtle); padding: 7px 10px; text-align: left; } th { position: sticky; top: 0; background: var(--bg-surface); }
.pager button { border: 1px solid var(--border-subtle); border-radius: 6px; padding: 5px 9px; background: var(--bg-surface); cursor: pointer; }.pager button:disabled { opacity: .45; cursor: default; }
.state { padding: 0 12px; }.error { color: var(--state-error); }
</style>
