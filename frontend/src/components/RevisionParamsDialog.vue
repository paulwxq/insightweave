<script setup lang="ts">
/**
 * 发布作业「查看参数」弹窗（doc 6.3 §4.1，设计稿 34；doc 7.2 §6/§7 改造）：
 * 按 domain / dim_value / rel 三个 tab 只读展示。
 *
 * domain / dim_value 的正式默认参数自 doc 7.2 起随版本保存在
 * revision.step_config（完整清单，不再等于工作区配置文件），此处按传入
 * 版本原样展示；旧版本缺少必需配置时明确提示（断代，不回退展示 YAML）。
 * rel 本期无版本参数（doc 7.2 §7.6），仍按打开时点只读展示工作区 rel 产物。
 */
import { onMounted, ref } from 'vue'
import { ApiError } from '../api/http'
import { getRelConfig } from '../api/workspaces'
import type {
  DimValueTableParam,
  DomainParam,
  RelConfigRelationship,
  RevisionItem,
} from '../api/types'
import { shownAtCqlThreshold } from '../utils/relDisplayFilter'
import { parseDimTables, parseDomains } from '../utils/publishStepConfig'

const props = defineProps<{
  workspaceId: string
  /** 被查看的版本（dim/domain 默认参数来自其 step_config） */
  revision: RevisionItem
}>()

const emit = defineEmits<{ close: [] }>()

const tabs = [
  { id: 'domain', label: 'domain（业务主题配置）' },
  { id: 'dim_value', label: 'dim_value' },
  { id: 'rel', label: 'rel' },
] as const
type TabId = (typeof tabs)[number]['id']
const active = ref<TabId>('domain')

/** null = 该版本未保存对应配置（未勾选该步骤或旧版本缺必需配置） */
const domains = ref<DomainParam[] | null>(null)
const dimTables = ref<DimValueTableParam[] | null>(null)
const relRows = ref<RelConfigRelationship[]>([])
const relThreshold = ref<number | null>(null)
const relLoading = ref(true)
const relError = ref('')

function stepSelected(stepName: string): boolean {
  return props.revision.steps.includes(stepName)
}

/** 断代提示（doc 7.2 §6/§7.3）：勾选步骤缺必需配置时不回退 YAML */
function missingText(stepName: string): string {
  if (!stepSelected(stepName)) return `该版本未勾选 ${stepName} 步骤。`
  return `该版本未保存 ${stepName} 默认配置（旧版本格式）；执行该步骤将在创建运行前被拒绝，请编辑作业创建新版本。`
}

// rel 列表与原编辑弹窗同一展示口径：外键恒展示，其余 composite_score >= 阈值
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
  // 版本参数从 revision.step_config 解析（随版本固化，不读工作区文件）
  domains.value = parseDomains(props.revision.step_config)
  dimTables.value = parseDimTables(props.revision.step_config)
  // rel 无版本参数（doc 7.2 §7.6）：只读展示当前工作区 rel 产物
  try {
    const rel = await getRelConfig(props.workspaceId)
    relThreshold.value = rel.score_threshold
    relRows.value = rel.relationships.filter(relShown)
  } catch (cause) {
    relError.value = cause instanceof ApiError ? cause.message : 'rel 产物读取失败。'
  } finally {
    relLoading.value = false
  }
})
</script>

<template>
  <div class="dialog-backdrop" @click.self="emit('close')">
    <div class="dialog-panel with-close rp-panel" role="dialog" aria-modal="true" aria-label="查看参数">
      <button type="button" class="dialog-close" aria-label="关闭" @click="emit('close')">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18" /><path d="m6 6 12 12" /></svg>
      </button>
      <p>查看参数 · 版本 {{ revision.revision_no }}</p>
      <p class="dialog-note">
        domain / dim_value 默认参数随版本保存（doc 7.2），此处展示该版本固化的完整清单；
        rel 无版本参数，按打开时点只读展示工作区 rel 产物。
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

      <div class="rp-scroll">
        <!-- domain（业务主题配置）：版本 step_config.domain.domains -->
        <div v-if="active === 'domain'" class="rp-body">
          <template v-if="domains !== null">
            <p class="rp-meta">{{ domains.length }} 个业务域</p>
            <p v-if="domains.length === 0" class="rp-empty">该版本的 domain 配置为空清单。</p>
            <div v-for="domain in domains" :key="domain.name" class="rp-block">
              <div class="rp-block-head">
                <strong>{{ domain.name }}</strong>
                <em>{{ domain.tables.length }} 张表</em>
              </div>
              <p class="rp-desc">{{ domain.description }}</p>
              <p v-if="domain.tables.length" class="rp-tables">{{ domain.tables.join('、') }}</p>
            </div>
          </template>
          <p v-else class="rp-empty">{{ missingText('domain') }}</p>
        </div>

        <!-- dim_value：版本 step_config.dim_value.tables（字段为数组） -->
        <div v-else-if="active === 'dim_value'" class="rp-body">
          <template v-if="dimTables !== null">
            <p class="rp-meta">{{ dimTables.length }} 张表</p>
            <p v-if="dimTables.length === 0" class="rp-empty">该版本的 dim_value 配置为空清单。</p>
            <div v-for="table in dimTables" :key="`${table.schema_name}.${table.object_name}`" class="rp-line">
              <strong>{{ table.schema_name }}.{{ table.object_name }}</strong>
              <span>
                值向量化字段：{{ table.embedding_col.length ? table.embedding_col.join('、') : '未设置' }}
              </span>
            </div>
          </template>
          <p v-else class="rp-empty">{{ missingText('dim_value') }}</p>
        </div>

        <!-- rel：无版本参数，读当前工作区产物 -->
        <div v-else class="rp-body">
          <p v-if="relLoading" class="ds-jobs-state">正在读取参数…</p>
          <p v-else-if="relError" class="status-message" role="alert">{{ relError }}</p>
          <template v-else>
            <p class="rp-meta">
              rel 参数为工作区产物（不随版本保存）：外键恒展示，其余 composite_score ≥
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
          </template>
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
