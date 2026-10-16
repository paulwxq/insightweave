<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { checkPublication, createPublication, getDataSource, getPublishDefinition } from '../api/dataSources'
import { displayDate } from '../api/format'
import { ApiError } from '../api/http'
import type {
  DataSourceListItem,
  PublicationStepResult,
  PublishCheckStep,
} from '../api/types'
import DimValueConfigDialog from '../components/DimValueConfigDialog.vue'
import DomainConfigDialog from '../components/DomainConfigDialog.vue'
import RelEditDialog from '../components/RelEditDialog.vue'

interface DimValueRow {
  schema_name: string
  object_name: string
  embedding_col: string | null
}

interface StaticStep {
  id: string
  label: string
}

interface StepGroup {
  id: string
  title: string
  steps: StaticStep[]
}

// 已接入发布的步骤（doc 16、19.2、20、21.2、22.2、25.2）；其余步骤保留展示
// doc 23.2 §3.2：rel 追加在末尾——发布请求的步骤顺序即该常量的过滤顺序，
// rel 依赖 json/domain 产物须排两者之后（页面展示顺序由 STATIC_GROUPS 决定）
// doc 25.2 §10：sql 追加在末尾（发布请求顺序不敏感，展示位置由 STATIC_GROUPS 决定）
const SELECTABLE_STEPS = [
  'db',
  'ddl',
  'json',
  'domain',
  'table_schema',
  'dim_value',
  'rel',
  'sql',
] as const

const STATIC_GROUPS: StepGroup[] = [
  {
    id: 'a',
    title: 'a.) 对于数据库整体',
    steps: [{ id: 'db', label: 'db' }],
  },
  {
    id: 'b',
    title: 'b.) 对表进行抽样画像',
    steps: [
      { id: 'ddl', label: 'ddl' },
      { id: 'json', label: 'json' },
      { id: 'domain', label: 'domain' },
    ],
  },
  {
    id: 'c',
    title: 'c.) 分析表之间的关联关系',
    // doc 23.2 §3.2：c 组原位保留（标题不变），删 cql 行、放 rel 行
    steps: [{ id: 'rel', label: 'rel' }],
  },
  {
    id: 'd',
    title: 'd.) 生成 NL2SQL RAG metadata',
    steps: [
      { id: 'sql', label: 'sql' },
      { id: 'table_schema', label: 'table_schema' },
      { id: 'dim_value', label: 'dim_value' },
    ],
  },
]

const route = useRoute()
const router = useRouter()
const sourceId = computed(() =>
  typeof route.params.sourceId === 'string' ? route.params.sourceId : '',
)
const jobId = computed(() => (typeof route.params.jobId === 'string' ? route.params.jobId : ''))
const replayPublishId = computed(() =>
  typeof route.query.publish_id === 'string' ? route.query.publish_id : '',
)
const source = ref<DataSourceListItem | null>(null)
const checkSteps = ref<Record<string, PublishCheckStep>>({})
const loading = ref(true)
const loadError = ref('')
const selectedSteps = ref<string[]>([])
const publishing = ref(false)
const publishId = ref('')
const runError = ref('')
const runResults = ref<PublicationStepResult[]>([])

// dim_value 配置编辑与拦截弹窗（doc 22.2 §8.2）
const dimConfigOpen = ref(false)
const domainConfigOpen = ref(false)
const relEditOpen = ref(false)
const dimSavedTables = ref<DimValueRow[] | null>(null)
const blockCode = ref('')
const blockMessage = ref('')
const blockReason = ref('')
const blockTables = ref<string[]>([])

function openDimConfig() {
  dimSavedTables.value = null
  dimConfigOpen.value = true
}

function onDimConfigSaved(tables: DimValueRow[]) {
  dimConfigOpen.value = false
  dimSavedTables.value = tables
}

function onRelConfigSaved() {
  relEditOpen.value = false
}

function closeBlock() {
  blockCode.value = ''
}

function goEditFromBlock() {
  blockCode.value = ''
  openDimConfig()
}

const dbTypeVersion = computed(() => {
  if (!source.value) return ''
  return source.value.db_version
    ? `${source.value.db_type} ${source.value.db_version}`
    : source.value.db_type
})

function isSelectable(stepId: string): boolean {
  return (SELECTABLE_STEPS as readonly string[]).includes(stepId)
}

function isPublishable(stepId: string): boolean {
  return checkSteps.value[stepId]?.publishable === true
}

function applyCheck(check: { steps: PublishCheckStep[] }) {
  checkSteps.value = Object.fromEntries(
    check.steps.map((step) => [step.step_name, step]),
  )
  selectedSteps.value = selectedSteps.value.filter(isPublishable)
}

const anyPublishable = computed(() => SELECTABLE_STEPS.some(isPublishable))
const successCount = computed(
  () => runResults.value.filter((row) => row.status === 'success').length,
)
const failedCount = computed(
  () => runResults.value.filter((row) => row.status === 'failed').length,
)
const canPublish = computed(
  () =>
    !loading.value &&
    !loadError.value &&
    !publishing.value &&
    anyPublishable.value &&
    selectedSteps.value.length > 0,
)

function leaveMissing() {
  return router.replace({ path: '/data-sources', query: { notice: 'not_found' } })
}

function goBack() {
  const previous = window.history.state?.back
  if (typeof previous === 'string' && previous.length > 0) {
    router.back()
    return
  }
  router.push(`/data-sources/${sourceId.value}/collection-jobs`)
}

function selectAll() {
  selectedSteps.value = SELECTABLE_STEPS.filter(isPublishable)
}

function toggleStep(stepId: string) {
  if (!isPublishable(stepId)) return
  selectedSteps.value = selectedSteps.value.includes(stepId)
    ? selectedSteps.value.filter((id) => id !== stepId)
    : [...selectedSteps.value, stepId]
}

async function load() {
  loading.value = true
  loadError.value = ''
  source.value = null
  checkSteps.value = {}
  selectedSteps.value = []
  publishing.value = false
  publishId.value = ''
  runError.value = ''
  runResults.value = []
  try {
    const [sourceRow, check] = await Promise.all([
      getDataSource(sourceId.value),
      checkPublication(sourceId.value, jobId.value),
    ])
    source.value = sourceRow
    applyCheck(check)
    if (replayPublishId.value) {
      const definition = await getPublishDefinition(sourceId.value, replayPublishId.value)
      if (definition.job_id !== jobId.value) {
        loadError.value = '该发布不属于此采集作业'
        return
      }
      selectedSteps.value = SELECTABLE_STEPS.filter(
        (id) => isPublishable(id) && definition.steps.includes(id),
      )
    }
  } catch (error) {
    if (
      error instanceof ApiError &&
      (error.code === 'not_found' || error.code === 'invalid_request')
    ) {
      await leaveMissing()
      return
    }
    loadError.value = error instanceof ApiError ? error.message : '发布预检失败'
  } finally {
    loading.value = false
  }
}

async function publish() {
  if (!canPublish.value) return
  publishing.value = true
  runError.value = ''
  publishId.value = ''
  runResults.value = []
  const chosen = [...selectedSteps.value]
  try {
    const fresh = await checkPublication(sourceId.value, jobId.value)
    applyCheck(fresh)
    // 点击时的选择里有步骤变为不可发布：不发布缩减后的集合，
    // 提示用户重新确认，避免实际发布范围与刚选的不同。
    const stale = chosen.filter((id) => !isPublishable(id))
    if (stale.length > 0) {
      const reason = stale
        .map((id) => checkSteps.value[id]?.reason || '不可发布')
        .join('；')
      runError.value = `选中步骤已不可发布（${reason}），请重新确认后再发布`
      return
    }
    const steps = SELECTABLE_STEPS.filter((id) => chosen.includes(id))
    const response = await createPublication(sourceId.value, jobId.value, [...steps])
    publishId.value = response.publish_id
    runResults.value = response.results
  } catch (error) {
    if (
      error instanceof ApiError &&
      ['dim_value_config_invalid', 'dim_value_empty_tables', 'dim_value_not_configured'].includes(
        error.code,
      )
    ) {
      // 三态拦截弹窗（doc 22.2 §8.2）：按钮按 code 分流
      blockCode.value = error.code
      blockMessage.value = error.message
      const details = error.details
      blockReason.value =
        typeof details === 'object' && !Array.isArray(details) && details && 'reason' in details
          ? String((details as Record<string, unknown>).reason)
          : ''
      blockTables.value =
        typeof details === 'object' && !Array.isArray(details) && details && 'tables' in details
          ? ((details as Record<string, unknown>).tables as string[])
          : []
      return
    }
    runError.value = error instanceof ApiError ? error.message : '画像发布失败'
  } finally {
    publishing.value = false
  }
}

watch([sourceId, jobId, replayPublishId], load, { immediate: true })
</script>

<template>
  <div class="ds-new-job">
    <header class="ds-new-job-header">
      <div>
        <span class="ds-kicker">PUBLISH PROFILE</span>
        <h1>数据画像发布</h1>
        <p>选择已采集成功的步骤。db、ddl、json、domain 发布到平台库，rel 发布到 Neo4j（生成 CQL 并加载），table_schema、dim_value、sql 发布到 Milvus。</p>
      </div>
      <div class="ds-new-job-actions">
        <button type="button" class="ds-new-job-back" @click="goBack">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></svg>
          返回
        </button>
        <button type="button" class="ds-new-job-run" :disabled="!canPublish" @click="publish">
          {{ publishing ? '发布中' : '开始发布' }}
        </button>
      </div>
    </header>

    <p v-if="loading" class="ds-jobs-state">正在读取发布预检…</p>
    <p v-else-if="loadError" class="ds-jobs-state error">
      {{ loadError }}
      <button type="button" class="ds-new-job-edit" @click="load">重试</button>
    </p>

    <template v-else-if="source">
      <section class="ds-jobs-card">
        <div class="ds-jobs-facts">
          <p><span>SOURCE ID</span><strong>{{ source.source_id }}</strong></p>
          <p><span>数据源名称</span><strong>{{ source.display_name }}</strong></p>
          <p><span>HOST : PORT</span><strong>{{ source.host }}:{{ source.port }}</strong></p>
          <p><span>数据库名称</span><strong>{{ source.db_name }}</strong></p>
          <p><span>数据库类型 / 版本</span><strong>{{ dbTypeVersion }}</strong></p>
          <p><span>JOB ID</span><strong>{{ jobId }}</strong></p>
        </div>
      </section>

      <section class="ds-jobs-card ds-new-job-steps">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">PUBLISH STEPS</span>
            <h2>发布步骤</h2>
            <p>只有采集成功且产物就绪的步骤可以发布。</p>
          </div>
          <div class="ds-new-job-step-actions">
            <button type="button" :disabled="publishing" @click="selectAll">全选</button>
            <button type="button" :disabled="publishing" @click="selectedSteps = []">清空</button>
          </div>
        </div>
        <div class="ds-new-job-grid">
          <article v-for="group in STATIC_GROUPS" :key="group.id" class="ds-new-job-group">
            <header><strong>{{ group.title }}</strong></header>
            <ul>
              <li
                v-for="step in group.steps"
                :key="step.id"
                :class="{ off: !isSelectable(step.id) || !isPublishable(step.id) }"
              >
                <button
                  v-if="isSelectable(step.id)"
                  type="button"
                  class="nj-check"
                  :class="{ on: selectedSteps.includes(step.id) }"
                  :disabled="!isPublishable(step.id) || publishing"
                  :aria-pressed="selectedSteps.includes(step.id)"
                  @click="toggleStep(step.id)"
                >
                  <svg v-if="selectedSteps.includes(step.id)" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6 9 17l-5-5" /></svg>
                </button>
                <button v-else type="button" class="nj-check" disabled aria-pressed="false"></button>
                <span>{{ step.label }}</span>
                <em v-if="isSelectable(step.id) && checkSteps[step.id]?.reason" class="ds-publish-reason">{{ checkSteps[step.id].reason }}</em>
                <em v-else-if="!isSelectable(step.id)" class="ds-publish-reason">暂不支持发布</em>
                <button
                  v-if="step.id === 'domain' && isSelectable(step.id)"
                  type="button"
                  class="nj-step-edit"
                  :disabled="!isPublishable(step.id) || publishing"
                  @click="domainConfigOpen = true"
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M12 20h9" />
                    <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" />
                  </svg>
                  编辑
                </button>
                <button
                  v-if="step.id === 'dim_value' && isSelectable(step.id)"
                  type="button"
                  class="nj-step-edit"
                  :disabled="!isPublishable(step.id) || publishing"
                  @click="openDimConfig"
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M12 20h9" />
                    <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" />
                  </svg>
                  编辑
                </button>
                <button
                  v-if="step.id === 'rel' && isSelectable(step.id)"
                  type="button"
                  class="nj-step-edit"
                  :disabled="!isPublishable(step.id) || publishing"
                  @click="relEditOpen = true"
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M12 20h9" />
                    <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" />
                  </svg>
                  编辑
                </button>
              </li>
            </ul>
          </article>
        </div>
      </section>

      <section class="ds-jobs-card ds-new-job-steps">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">PUBLISH RESULT</span>
            <h2>发布结果</h2>
            <p>发布 ID {{ publishId || '发布时生成' }}</p>
          </div>
          <span
            v-if="runResults.length"
            class="ds-new-job-result-pill"
            :class="failedCount ? 'bad' : 'ok'"
          >
            {{ successCount }} 成功 · {{ failedCount }} 失败
          </span>
        </div>
        <p v-if="publishing" class="ds-new-job-empty">发布中</p>
        <p v-else-if="runError" class="ds-jobs-state error">{{ runError }}</p>
        <ul v-else-if="runResults.length" class="ds-new-job-group result-list">
          <li v-for="row in runResults" :key="row.step" :class="row.status">
            <span>{{ row.step }}</span>
            <em>{{ row.status === 'success' ? '发布成功' : '发布失败' }}</em>
            <time>{{ displayDate(row.finished_at) }}</time>
            <small>{{ row.message }}</small>
          </li>
        </ul>
        <p v-else class="ds-new-job-empty">尚未发布</p>
      </section>
    </template>

    <DomainConfigDialog
      v-if="domainConfigOpen"
      :source-id="sourceId"
      :job-id="jobId"
      @close="domainConfigOpen = false"
    />

    <!-- dim_value 配置编辑弹窗（原型 2/3/4）-->
    <DimValueConfigDialog
      v-if="dimConfigOpen"
      :source-id="sourceId"
      :job-id="jobId"
      :db-name="source?.db_name || ''"
      @close="dimConfigOpen = false"
      @saved="onDimConfigSaved"
    />

    <!-- rel 关系编辑弹窗（原型 23.rel-1~5，doc 23.2 §3.3）-->
    <RelEditDialog
      v-if="relEditOpen"
      :source-id="sourceId"
      :job-id="jobId"
      @close="relEditOpen = false"
      @saved="onRelConfigSaved"
    />

    <!-- 保存成功确认（原型 5）。yaml 预览就是「查看文件」，不再另开一个没有落点的按钮。-->
    <div v-if="dimSavedTables" class="dialog-backdrop" @click.self="dimSavedTables = null">
      <div class="dim-saved" role="dialog" aria-modal="true" aria-labelledby="dim-saved-title">
        <span class="dim-saved-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M5 12.5 9.5 17 19 7" /></svg>
        </span>
        <h2 id="dim-saved-title">已保存至 dim_tables.yaml</h2>
        <p v-if="dimSavedTables.length">
          {{ dimSavedTables.length }} 张表的 embedding_col（值向量化字段）已全部写回，发布 dim_value 数据时将使用最新配置。
        </p>
        <p v-else>配置已保存，共 0 张维表。发布 dim_value 前需添加表并指定值向量化字段。</p>
        <div class="dim-saved-yaml">
          <div>databases:</div>
          <div class="lv1">{{ source?.db_name || 'database' }}:</div>
          <div class="lv2">tables:<template v-if="dimSavedTables.length === 0"> {}</template></div>
          <template v-for="row in dimSavedTables" :key="row.schema_name + '.' + row.object_name">
            <div class="lv3">{{ row.schema_name }}.{{ row.object_name }}:</div>
            <div class="lv4">embedding_col: <span>{{ row.embedding_col }}</span></div>
          </template>
        </div>
        <button type="button" @click="dimSavedTables = null">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5 9.5 17 19 7" /></svg>
          完成
        </button>
      </div>
    </div>

    <!-- 发布拦截弹窗（原型 5 的错误变体；按钮按 code 分流）-->
    <div v-if="blockCode" class="dialog-backdrop" @click.self="closeBlock">
      <div class="dialog-panel with-close" role="alertdialog" aria-modal="true">
        <button type="button" class="dialog-close" aria-label="关闭" @click="closeBlock">✕</button>
        <p class="dim-block-title">✕ 无法发布 dim_value</p>
        <p>{{ blockMessage }}</p>
        <p v-if="blockReason" class="dim-block-reason">原因：{{ blockReason }}</p>
        <ul v-if="blockTables.length" class="dim-ok-list">
          <li v-for="name in blockTables" :key="name"><span>{{ name }}</span></li>
        </ul>
        <p v-if="blockCode === 'dim_value_config_invalid'" class="dim-block-hint">
          配置文件已损坏，请重跑该作业的 dim_config 步骤重新生成后再编辑配置。
        </p>
        <div class="dialog-actions">
          <button type="button" class="button" @click="closeBlock">关闭</button>
          <button
            v-if="blockCode !== 'dim_value_config_invalid'"
            type="button"
            class="button button-primary"
            @click="goEditFromBlock"
          >
            去编辑配置
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dim-saved {
  width: min(460px, calc(100vw - 48px));
  background: #fff;
  border: 1px solid #d9e3ef;
  border-radius: 16px;
  box-shadow: 0 16px 48px #17293f33;
  padding: 28px 24px 22px;
  text-align: center;
}
.dim-saved-mark {
  width: 48px;
  height: 48px;
  margin: 0 auto 14px;
  border-radius: 50%;
  background: #e7f6ef;
  color: #1aae78;
  display: grid;
  place-items: center;
}
.dim-saved-mark svg,
.dim-saved button svg {
  fill: none;
  stroke: currentColor;
  stroke-width: 2.4;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.dim-saved-mark svg { width: 22px; height: 22px; }
.dim-saved h2 { margin: 0 0 8px; font-size: 18px; font-weight: 700; }
.dim-saved p {
  margin: 0 auto 16px;
  max-width: 36em;
  color: #66788f;
  font-size: 13px;
  line-height: 1.6;
}
.dim-saved-yaml {
  margin: 0 0 18px;
  padding: 14px 16px;
  text-align: left;
  background: #eef3f9;
  border-radius: 12px;
  color: #17293f;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
  line-height: 1.6;
  max-height: 240px;
  overflow: auto;
}
.dim-saved-yaml .lv1 { padding-left: 16px; }
.dim-saved-yaml .lv2 { padding-left: 32px; }
.dim-saved-yaml .lv3 { padding-left: 48px; }
.dim-saved-yaml .lv4 { padding-left: 64px; }
.dim-saved-yaml span { color: #2e7cd6; font-weight: 700; }
.dim-saved button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 0;
  border-radius: 10px;
  background: #2e7cd6;
  color: #fff;
  padding: 9px 18px;
  font-size: 13px;
  font-weight: 700;
}
.dim-saved button svg { width: 14px; height: 14px; }
</style>
