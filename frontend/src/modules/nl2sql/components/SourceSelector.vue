<script setup lang="ts">
/**
 * 新建查询顶部：选择数据库并做就绪检查（doc 11：不再有“开始查询”按钮，
 * 就绪通过后直接在下方输入，首次发送时创建会话）。
 * 只在还没有会话时出现；打开已有会话不会再次检查。
 */
import { computed } from 'vue'
import type { DataSourceListItem } from '../../../api/types'
import type { ReadinessData } from '../api/types'
import type { ReadinessState } from '../composables/useChat'

const props = defineProps<{
  sources: DataSourceListItem[]
  loading: boolean
  error: string
  selectedSourceId: string
  readinessState: ReadinessState
  readinessData: ReadinessData | null
  readinessError: string
  busy: boolean
  notice: string
  sourceInherited: boolean
}>()

const emit = defineEmits<{
  select: [sourceId: string]
}>()

const blockedChecks = computed(() => {
  const data = props.readinessData
  if (data === null || data.ready) return []
  return data.checks.filter((item) => item.state === 'fail')
})

const statusTone = computed(() => {
  if (props.notice || props.error || props.readinessState === 'blocked' || props.readinessState === 'error') {
    return 'fail'
  }
  if (props.readinessState === 'ready' && !props.busy) return 'ok'
  return ''
})

function onSourceLabel(item: DataSourceListItem) {
  const schemas = item.selected_schemas.join(', ')
  return `${item.display_name}（${item.db_name}${schemas ? ` · ${schemas}` : ''}）`
}
</script>

<template>
  <section class="source-bar">
    <label>
      <span>数据库</span>
      <span class="select-wrap">
        <svg class="lead" viewBox="0 0 16 16" aria-hidden="true">
          <ellipse cx="8" cy="4" rx="5" ry="2" />
          <path d="M3 4v8c0 1.1 2.2 2 5 2s5-.9 5-2V4" />
          <path d="M3 8c0 1.1 2.2 2 5 2s5-.9 5-2" />
        </svg>
        <select
          :class="{ idle: selectedSourceId === '' }"
          :value="selectedSourceId"
          :disabled="loading || busy"
          @change="emit('select', ($event.target as HTMLSelectElement).value)"
        >
          <option value="" disabled>{{ loading ? '加载中…' : '请选择数据库' }}</option>
          <option v-for="item in sources" :key="item.source_id" :value="item.source_id">
            {{ onSourceLabel(item) }}
          </option>
        </select>
        <svg class="chev" viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg>
      </span>
    </label>

    <!-- 创建提示与列表错误独立展示，互不遮挡（Kimi R1-02）：两者各自生成、各自清理 -->
    <div class="status" :class="statusTone">
      <svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6" /><path d="M8 7.2V11" /><path d="M8 5h.01" /></svg>
      <div class="status-copy">
        <p v-if="notice">{{ notice }}</p>
        <p v-if="error">{{ error }}</p>
        <p v-else-if="sources.length === 0 && !loading">暂无已启用的数据源，请先在数据源页登记并启用。</p>
        <p v-else-if="selectedSourceId === ''">新建查询前先选择数据库并完成检查，再次打开会话不会重复检查。</p>
        <p v-else-if="readinessState === 'checking'">正在检查知识就绪状态…</p>
        <p v-else-if="busy">正在创建会话…</p>
        <p v-else-if="readinessState === 'ready'">
          {{ sourceInherited ? '已沿用当前数据库，可直接输入问题。' : '数据源已就绪，请在下方输入问题。' }}
        </p>
        <p v-else-if="readinessState === 'blocked'">
          <strong>数据源暂未就绪。</strong>
          <span v-for="check in blockedChecks" :key="check.name">{{ check.message || check.error_code }}</span>
        </p>
        <p v-else-if="readinessState === 'error'">{{ readinessError }}</p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.source-bar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 20px 28px;
  background: #f8fafd;
  border-bottom: 1px solid #d9e3ef;
}

label {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-shrink: 0;
  color: #17293f;
  font-size: 14px;
  font-weight: 700;
}

.select-wrap {
  position: relative;
  width: 280px;
}

.select-wrap svg {
  position: absolute;
  top: 50%;
  width: 16px;
  height: 16px;
  transform: translateY(-50%);
  fill: none;
  stroke: #66788f;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
  pointer-events: none;
}

.select-wrap .lead {
  left: 13px;
}

.select-wrap .chev {
  right: 12px;
  width: 15px;
  height: 15px;
}

select {
  width: 100%;
  appearance: none;
  border: 1px solid #d9e3ef;
  background: #fff;
  color: #17293f;
  border-radius: 10px;
  padding: 11px 36px;
  font-size: 13px;
  font-weight: 400;
}

select.idle {
  color: #66788f;
}

.status {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: flex-start;
  gap: 7px;
  color: #66788f;
  font-size: 12px;
  line-height: 1.5;
}

.status > svg {
  width: 14px;
  height: 14px;
  margin-top: 2px;
  flex-shrink: 0;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.status-copy {
  min-width: 0;
}

.status p {
  margin: 0;
}

.status p + p {
  margin-top: 4px;
}

.status.ok {
  color: #0e9f8a;
}

.status.fail {
  color: #9a6b32;
}

.status.fail span {
  margin-left: 6px;
}
</style>
