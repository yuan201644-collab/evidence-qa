<script setup lang="ts">
import AppIcon from './AppIcon.vue'
import type { DataSource, SqlRef } from '../types/contracts'
defineProps<{ refItem: SqlRef; sql: string | null; dataSource?: DataSource | null }>()
</script>

<template>
  <section class="sql-card">
    <span class="source-type"><AppIcon name="database" :size="15" />数据库证据</span>
    <!-- 数据源标识：告诉用户这条 SQL 连的是哪个库。没有 data_source 时整行不渲染 ——
         纯文档问答没有"哪个库"可言，挂一个库名标签反而是错的。 -->
    <div v-if="dataSource" class="data-source" :title="`库名 ${dataSource.database} · 方言 ${dataSource.dialect}`">
      <span class="data-source__label">数据来源</span>
      <span class="data-source__name">{{ dataSource.label }}</span>
    </div>
    <h3>{{ refItem.table }}</h3>
    <dl><div v-if="refItem.column"><dt>字段</dt><dd>{{ refItem.column }}</dd></div><div v-if="refItem.row_pk"><dt>行主键</dt><dd>{{ refItem.row_pk }}</dd></div></dl>
    <div class="section-label">返回的来源记录</div><blockquote>{{ refItem.snippet }}</blockquote>
    <template v-if="sql"><div class="section-label sql-label">查询语句 <small>供核对本次查询</small></div><pre><code>{{ sql }}</code></pre></template>
  </section>
</template>

<style scoped>
.sql-card { min-height: 0; overflow: auto; padding: 25px; }
.source-type { display: flex; align-items: center; gap: 7px; color: var(--brand-primary); font-size: 13px; }
.data-source { display: flex; flex-wrap: wrap; align-items: baseline; gap: 7px; margin-top: 11px; padding: 8px 11px; border: 1px solid var(--border-subtle); border-radius: 9px; background: var(--bg-subtle); }
.data-source__label { flex-shrink: 0; color: var(--text-muted); font-size: 12px; }
.data-source__name { color: var(--text-secondary); font-size: 13px; font-weight: 500; overflow-wrap: anywhere; }
h3 { margin: 13px 0 20px; color: var(--text-heading); font-size: 21px; overflow-wrap: anywhere; }
dl { display: grid; gap: 10px; margin: 0 0 25px; padding-bottom: 21px; border-bottom: 1px solid var(--border-subtle); }
dl > div { display: flex; gap: 16px; font-size: 13px; }
dt { flex-shrink: 0; width: 40px; color: var(--text-muted); }
dd { margin: 0; color: var(--text-secondary); overflow-wrap: anywhere; }
.section-label { color: var(--text-muted); font-size: 12px; margin-bottom: 9px; }
.section-label small { margin-left: 8px; font-size: 11px; color: var(--text-faint); }
blockquote { margin: 0; padding: 16px; border: 1px solid var(--border-subtle); border-radius: 9px; color: var(--text-secondary); background: var(--bg-subtle); font-size: 14px; line-height: 1.9; white-space: pre-wrap; overflow-wrap: anywhere; }
.sql-label { margin-top: 27px; }
pre { overflow: auto; margin: 0; padding: 18px; border-radius: 10px; color: var(--code-text); background: var(--code-bg); font-size: 14px; line-height: 1.85; white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
