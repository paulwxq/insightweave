<script setup lang="ts">
/**
 * 发布作业「查看参数」弹窗（doc 6.3 §4.1，设计稿 34）：按 domain /
 * dim_value / rel 三个 tab 只读展示。发布版本的参数即工作区配置文件
 * （db_domains.yaml、dim_tables.yaml、rel 产物），不随版本保存；弹窗按
 * 打开时点读取最新文件，修改须走各配置编辑弹窗。
 */
import { onMounted, ref } from 'vue'
import { ApiError } from '../api/http'
import { getDimValueConfig, getDomainConfig, getRelConfig } from '../api/workspaces'
import type {
  DimValueConfigTable,
  DomainConfigDomain,
  RelConfigRelationship,
} from '../api/types'
import { shownAtCqlThreshold } from '../utils/relDisplayFilter'

const props = defineProps<{ workspaceId: string }>()

const emit = defineEmits<{ close: [] }>()

const tabs = [
  { id: 'domain', label: 'domain（业务主题配置）' },
  { id: 'dim_value', label: 'dim_value' },
  { id: 'rel', label: 'rel' },
] as const
type TabId = (typeof tabs)[number]['id']
const active = ref<TabId>('domain')

const databaseDesc = ref('')
const maxPerTable = ref<number | null>(null)
const domains = ref<DomainConfigDomain[]>([])
const dimTables = ref<DimValueConfigTable[]>([])
const dimVersion = ref('')
const relRows = ref<RelConfigRelationship[]>([])
const relThreshold = ref<number | null>(null)
const loading = ref(true)
const error = ref('')

// rel 列表与编辑弹窗同一展示口径：外键恒展示，其余 composite_score >= 阈值
function relShown(row: RelConfigRelationship): boolean {
  const score = typeof row.composite_score === 'number' ? row.composite_score : null
  return shownAtCqlThreshold(String(row.discovery_method ?? 'manual'), score, relThreshold.value ?? 1)
}

function tableRef(value: unknown): string {
  const ref = value as { schema?: unknown; table?: unknown }
  if (ref && typeof ref === 'object' && typeof ref.table === 'string') {
    return typeof ref.schema === 'string' && ref.schema ? `${ref.schema}.${ref.table}` : ref.table
  }
  return String(value)
}

function relCols(row: RelConfigRelationship, side: 'from' | 'to'): string[] {
  const many = row[`${side}_columns`]
  if (Array.isArray(many)) return many.map(String)
  const single = row[`${side}_column`]
  return single ? [String(single)] : []
}

onMounted(async () => {
  try {
    const [domain, dim, rel] = await Promise.all([
      getDomainConfig(props.workspaceId),
      getDimValueConfig(props.workspaceId),
      getRelConfig(props.workspaceId),
    ])
    databaseDesc.value = domain.database.description ?? ''
    maxPerTable.value = domain.max_domains_per_table
    domains.value = domain.domains
    dimTables.value = dim.tables
    dimVersion.value = dim.version
    relThreshold.value = rel.score_threshold
    relRows.value = rel.relationships.filter(relShown)
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : '参数读取失败。'
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="dialog-backdrop" @click.self="emit('close')">
    <div class="dialog-panel with-close rp-panel" role="dialog" aria-modal="true" aria-label="查看参数">
      <button type="button" class="dialog-close" aria-label="关闭" @click="emit('close')">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18" /><path d="m6 6 12 12" /></svg>
      </button>
      <p>查看参数</p>
      <p class="dialog-note">
        发布步骤参数为工作区配置文件（db_domains.yaml、dim_tables.yaml、rel 产物），
        不随版本保存；此处按打开时点只读展示，修改走各步骤的「编辑」弹窗。
      </p>

      <div class="rp-tabs" role="tablist">
        <button
          v-for="tab in tabs"
          :key="tab.id"
          type="button"
          role="tab"
          :aria-selected="active === tab.id"
          :class="{ on: active === tab.id }"
          @click="active = tab.id"
        >
          {{ tab.label }}
        </button>
      </div>

      <p v-if="loading" class="ds-jobs-state">正在读取参数…</p>
      <p v-else-if="error" class="status-message" role="alert">{{ error }}</p>
      <div v-else class="rp-scroll">
        <!-- domain（业务主题配置） -->
        <div v-if="active === 'domain'" class="rp-body">
          <p v-if="databaseDesc" class="rp-desc">{{ databaseDesc }}</p>
          <p class="rp-meta">每表最多归入主题数：{{ maxPerTable ?? '—' }}</p>
          <div v-for="domain in domains" :key="domain.name" class="rp-block">
            <div class="rp-block-head">
              <strong>{{ domain.name }}</strong>
              <em>{{ domain.tables.length }} 张表</em>
            </div>
            <p class="rp-desc">{{ domain.description }}</p>
            <p v-if="domain.tables.length" class="rp-tables">{{ domain.tables.join('、') }}</p>
          </div>
        </div>

        <!-- dim_value -->
        <div v-else-if="active === 'dim_value'" class="rp-body">
          <p class="rp-meta">配置版本：{{ dimVersion || '—' }} · {{ dimTables.length }} 张表</p>
          <p v-if="dimTables.length === 0" class="rp-empty">尚未配置任何表的维度值列。</p>
          <div v-for="table in dimTables" :key="`${table.schema_name}.${table.object_name}`" class="rp-line">
            <strong>{{ table.schema_name }}.{{ table.object_name }}</strong>
            <span>embedding 列：{{ table.embedding_col ?? '未设置' }}</span>
          </div>
        </div>

        <!-- rel -->
        <div v-else class="rp-body">
          <p class="rp-meta">
            展示口径与编辑弹窗一致：外键恒展示，其余 composite_score ≥
            {{ relThreshold ?? '—' }}；共 {{ relRows.length }} 条。
          </p>
          <p v-if="relRows.length === 0" class="rp-empty">没有达到展示口径的关系。</p>
          <div v-for="row in relRows" :key="String(row.relationship_id)" class="rp-line">
            <strong>{{ tableRef(row.from_table) }}（{{ relCols(row, 'from').join('、') }}）</strong>
            <span class="rp-arrow">→</span>
            <strong>{{ tableRef(row.to_table) }}（{{ relCols(row, 'to').join('、') }}）</strong>
            <span>{{ row.cardinality ?? '—' }}</span>
            <em>{{ row.discovery_method ?? 'manual' }}<template v-if="typeof row.composite_score === 'number'"> · {{ row.composite_score }}</template></em>
          </div>
        </div>
      </div>

      <div class="dialog-actions">
        <button type="button" class="button button-outline" @click="emit('close')">关闭</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.rp-panel {
  width: min(100%, 720px);
}
.rp-tabs {
  display: flex;
  gap: 6px;
  margin-bottom: 12px;
}
.rp-tabs button {
  border: 1px solid #d2e0f2;
  border-radius: 10px;
  background: #f1f7ff;
  color: #2c3e58;
  font-size: 12px;
  font-weight: 700;
  padding: 7px 12px;
}
.rp-tabs button.on {
  background: #2e7cd6;
  border-color: #2e7cd6;
  color: #fff;
}
.rp-scroll {
  max-height: 46vh;
  overflow: auto;
  border: 1px solid #e1e9f4;
  border-radius: 12px;
  padding: 12px 14px;
}
.rp-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.rp-desc {
  margin: 0;
  color: #17293f;
  font-size: 12px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.rp-meta {
  margin: 0;
  color: #66788f;
  font-size: 11px;
}
.rp-block {
  border: 1px solid #e1e9f4;
  border-radius: 10px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: #fcfdff;
}
.rp-block-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.rp-block-head strong {
  font-size: 13px;
  color: #17293f;
}
.rp-block-head em {
  font-style: normal;
  color: #2e7cd6;
  font-size: 11px;
}
.rp-tables {
  margin: 0;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 10px;
  color: #66788f;
  overflow-wrap: anywhere;
}
.rp-line {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  border-bottom: 1px solid #eef3f9;
  padding: 7px 0;
}
.rp-line:last-child {
  border-bottom: 0;
}
.rp-line strong {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
  color: #17293f;
  overflow-wrap: anywhere;
}
.rp-line span {
  color: #66788f;
  font-size: 11px;
}
.rp-line em {
  font-style: normal;
  margin-left: auto;
  color: #2e7cd6;
  font-size: 11px;
  white-space: nowrap;
}
.rp-arrow {
  color: #2e7cd6;
  font-weight: 700;
}
.rp-empty {
  margin: 0;
  color: #66788f;
  font-size: 12px;
}
</style>
