<script setup lang="ts">
/**
 * 新建查询顶部：选择数据库并做就绪检查。
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
  creating: boolean
  notice: string
}>()

const emit = defineEmits<{
  select: [sourceId: string]
  create: []
}>()

const blockedChecks = computed(() => {
  const data = props.readinessData
  if (data === null || data.ready) return []
  return data.checks.filter((item) => item.state === 'fail')
})

const canCreate = computed(
  () => props.selectedSourceId !== '' && props.readinessState === 'ready' && !props.creating,
)

function onSourceLabel(item: DataSourceListItem) {
  const schemas = item.selected_schemas.join(', ')
  return `${item.display_name}（${item.db_name}${schemas ? ` · ${schemas}` : ''}）`
}
</script>

<template>
  <section class="source-bar">
    <label>
      <span>数据库</span>
      <select
        :value="selectedSourceId"
        :disabled="loading || creating"
        @change="emit('select', ($event.target as HTMLSelectElement).value)"
      >
        <option value="" disabled>{{ loading ? '加载中…' : '请选择数据库' }}</option>
        <option v-for="item in sources" :key="item.source_id" :value="item.source_id">
          {{ onSourceLabel(item) }}
        </option>
      </select>
    </label>

    <p v-if="error" class="status fail">{{ error }}</p>
    <p v-else-if="notice" class="status fail">{{ notice }}</p>
    <p v-else-if="sources.length === 0 && !loading" class="status">
      暂无已启用的数据源，请先在数据源页登记并启用。
    </p>
    <p v-else-if="selectedSourceId === ''" class="status">新建查询前先选择数据库并完成检查。再次打开会话不会重复检查。</p>
    <p v-else-if="readinessState === 'checking'" class="status">正在检查知识就绪状态…</p>
    <p v-else-if="readinessState === 'ready'" class="status ok">数据源已就绪，可以开始查询。</p>
    <div v-else-if="readinessState === 'blocked'" class="status fail">
      <strong>数据源暂未就绪。</strong>
      <span v-for="check in blockedChecks" :key="check.name">{{ check.message || check.error_code }}</span>
    </div>
    <p v-else-if="readinessState === 'error'" class="status fail">{{ readinessError }}</p>

    <button type="button" class="start" :disabled="!canCreate" @click="emit('create')">
      {{ creating ? '正在创建…' : '开始查询' }}
    </button>
  </section>
</template>

<style scoped>
.source-bar {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 12px 22px;
  background: #fff;
  border-bottom: 1px solid #d9e3ef;
  flex-wrap: wrap;
}

label {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #17293f;
  font-size: 12px;
  font-weight: 700;
}

select {
  min-width: 240px;
  border: 1px solid #d9e3ef;
  background: #f4f7fb;
  color: #17293f;
  border-radius: 10px;
  padding: 8px 12px;
  font-size: 13px;
  font-weight: 500;
}

.status {
  margin: 0;
  flex: 1;
  min-width: 180px;
  color: #66788f;
  font-size: 12px;
  line-height: 1.5;
}

.status.ok {
  color: #0e9f8a;
}

.status.fail {
  color: #9a6b32;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.start {
  border: 0;
  background: #2e7cd6;
  color: #fff;
  border-radius: 10px;
  padding: 8px 14px;
  font-size: 13px;
  font-weight: 700;
}

.start:disabled {
  background: #b7cbe4;
}
</style>
