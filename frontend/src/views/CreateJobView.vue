<script setup lang="ts">
/**
 * 新建作业（采集/发布共用，doc 6.2 §9.1.2；doc 7.2 §3.2 改造）：只保存作业与
 * 版本 1，不执行；步骤依赖仅作交互提示，最终校验与排序由后端完成。
 *
 * 发布作业的 dim_value/domain 编辑的是首版本草稿：弹窗确认只回传清单，
 * 不写工作区配置文件；保存时完整清单随作业写入版本 1 默认配置（完整替换）。
 * 新建时自动读取 YAML 初始化草稿（非运行时回退）；勾选 dim_value/domain 时对应草稿必须完整才能保存。
 * Rel 不再提供编辑入口（doc 7.2 §7.6，保留 rel 步骤选择与执行）。
 */
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError } from '../api/http'
import type {
  DimValueTableParam,
  DomainParam,
  JobKind,
  PublishCheckResponse,
  PublishCheckStep,
} from '../api/types'
import {
  createJob,
  getPublishCheck,
} from '../api/workspaces'
import DimValueConfigDialog from '../components/DimValueConfigDialog.vue'
import DomainConfigDialog from '../components/DomainConfigDialog.vue'
import DomainDescriptionDialog from '../components/DomainDescriptionDialog.vue'
import { usePublishConfigInitialization } from '../composables/usePublishConfigInitialization'
import StepGrid from '../components/StepGrid.vue'
import { useWorkspaceContext } from '../composables/useWorkspaceContext'
import { allSteps, unchosenDeps } from '../utils/steps'

const props = defineProps<{ kind: JobKind }>()

const route = useRoute()
const router = useRouter()
const workspaceId = computed(() =>
  typeof route.params.workspaceId === 'string' ? route.params.workspaceId : '',
)
const { workspace, source, loading: ctxLoading, error: ctxError } = useWorkspaceContext(
  () => workspaceId.value,
)

const DB_TYPE_LABEL: Record<string, string> = {
  postgresql: 'PostgreSQL',
  mysql: 'MySQL',
  sqlite: 'SQLite',
  csv: 'CSV',
  excel: 'Excel',
}

const kindText = computed(() => (props.kind === 'collection' ? '采集' : '发布'))
const pageTitle = computed(() =>
  props.kind === 'collection' ? '新建数据采集作业' : '新建数据发布作业',
)

function sourceTypeText(): string {
  if (!source.value) return '—'
  const label = DB_TYPE_LABEL[source.value.db_type] ?? source.value.db_type
  return source.value.db_version ? `${label} ${source.value.db_version}` : label
}
const basePath = computed(
  () => `/workspaces/${workspaceId.value}/${props.kind === 'collection' ? 'collection' : 'publish'}`,
)

const NAME_MAX = 200
const DESC_MAX = 1000

const jobName = ref('')
const jobDesc = ref('')
const selected = ref<Record<string, boolean>>({})
const saving = ref(false)
const saveError = ref('')

const nameCount = computed(() => [...jobName.value].length)
const descCount = computed(() => [...jobDesc.value].length)

// doc 6.2 §7.2：description 是 Domain 首次生成的默认输入；未保存时不上送，
// 由后端按临时输入 → 版本默认 → 数据源描述回退。该输入只在
// db_domains.yaml 不存在时参与首次生成，不覆盖已有描述。
const savedDescription = ref<string | null>(null)
const descriptionOpen = ref(false)

// 发布：Domain/Dim 首版本草稿编辑（doc 7.2 §3.2）。草稿随作业保存写入版本 1，
// 不写工作区配置文件；null = 尚未配置，自动读取 YAML 初值，读取失败可手工配置。
const domainOpen = ref(false)
const dimOpen = ref(false)
const dimDraft = ref<DimValueTableParam[] | null>(null)
const domainDraft = ref<DomainParam[] | null>(null)

function onDimApply(tables: DimValueTableParam[]) {
  dimDraft.value = tables
  dimOpen.value = false
}

function onDomainApply(domains: DomainParam[]) {
  domainDraft.value = domains
  domainOpen.value = false
}

const { initializing, initError } = usePublishConfigInitialization(
  () => [props.kind, workspaceId.value] as const,
  dimDraft,
  domainDraft,
)

// 发布校验（设计稿 32/33，与详情页/执行页同一 publish-check 接口）：发布步骤
// 须通过校验才可勾选、编辑与保存；结果反映点击时刻的产物状态，保存前会复查。
const check = ref<PublishCheckResponse | null>(null)
const checking = ref(false)
const checkError = ref('')

const checkRan = computed(() => check.value !== null)
const checkByStep = computed(() => {
  const map = new Map<string, PublishCheckStep>()
  for (const step of check.value?.steps ?? []) map.set(step.step_name, step)
  return map
})
/** 发布页可勾选白名单：校验通过的步骤；未校验时为空（全部置灰） */
const passedSteps = computed(() =>
  (check.value?.steps ?? []).filter((step) => step.ready).map((step) => step.step_name),
)
const failedCount = computed(
  () => (check.value?.steps ?? []).filter((step) => !step.ready).length,
)

function stepCheck(step: string): PublishCheckStep | undefined {
  return checkByStep.value.get(step)
}

async function runPublishCheck(): Promise<boolean> {
  if (!workspaceId.value) return false
  checking.value = true
  checkError.value = ''
  try {
    check.value = await getPublishCheck(workspaceId.value)
    return true
  } catch (cause) {
    checkError.value = cause instanceof ApiError ? cause.message : '发布校验失败。'
    return false
  } finally {
    checking.value = false
  }
}

const chosenSteps = computed(() =>
  Object.keys(selected.value).filter((step) => selected.value[step]),
)

const depHints = computed(() =>
  props.kind === 'collection' ? unchosenDeps(chosenSteps.value) : [],
)

/** 版本保存完整性（doc 7.2 §5.1）：勾选 dim_value/domain 必须携带完整清单 */
const dimIncomplete = computed(
  () =>
    props.kind === 'publish' &&
    chosenSteps.value.includes('dim_value') &&
    (!dimDraft.value ||
      dimDraft.value.length === 0 ||
      dimDraft.value.some((table) => table.embedding_col.length === 0)),
)
const domainIncomplete = computed(
  () =>
    props.kind === 'publish' &&
    chosenSteps.value.includes('domain') &&
    (!domainDraft.value || domainDraft.value.length === 0),
)

const canSave = computed(
  () =>
    !ctxLoading.value &&
    !ctxError.value &&
    !saving.value &&
    !initializing.value &&
    jobName.value.trim().length > 0 &&
    nameCount.value <= NAME_MAX &&
    descCount.value <= DESC_MAX &&
    chosenSteps.value.length > 0 &&
    // 发布：勾选步骤必须全部通过校验（未校验时 chosenSteps 恒为空，保存置灰）
    (props.kind !== 'publish' ||
      (chosenSteps.value.every((step) => stepCheck(step)?.ready === true) &&
        !dimIncomplete.value &&
        !domainIncomplete.value)),
)

function toggleStep(step: string) {
  if (props.kind === 'publish' && stepCheck(step)?.ready !== true) return
  selected.value = { ...selected.value, [step]: !selected.value[step] }
}

// 发布页全选仅勾选校验通过的步骤（未通过项不可选）
function selectAll() {
  const steps = props.kind === 'publish' ? passedSteps.value : allSteps(props.kind)
  selected.value = Object.fromEntries(steps.map((step) => [step, true]))
}

function onDescriptionSaved(text: string) {
  savedDescription.value = text
  descriptionOpen.value = false
}

async function save() {
  if (!canSave.value) return
  saving.value = true
  saveError.value = ''
  try {
    if (props.kind === 'publish') {
      // 保存前复查：勾选后的产物/配置状态可能已变化；只拦所选步骤，
      // 未勾选的失败步骤不阻塞保存（doc 6.2 §8.2.2 同源检查）
      const ok = await runPublishCheck()
      if (!ok) {
        saveError.value = checkError.value || '发布校验失败，无法保存。'
        return
      }
      const failing = chosenSteps.value.filter((step) => !stepCheck(step)?.ready)
      if (failing.length > 0) {
        const detail = failing
          .map((step) => `${step}（${stepCheck(step)?.reason ?? '原因未知'}）`)
          .join('；')
        saveError.value = `所选步骤发布校验未通过：${detail}。请重新校验并调整勾选。`
        return
      }
    }
    const steps = chosenSteps.value
    const stepConfig: Record<string, Record<string, unknown>> = {}
    if (props.kind === 'collection') {
      if (steps.includes('domain') && savedDescription.value !== null) {
        stepConfig.domain = { description: savedDescription.value }
      }
    } else {
      // 完整清单随保存写入版本 1（完整替换语义；不做增量合并）
      if (steps.includes('dim_value') && dimDraft.value) {
        stepConfig.dim_value = { tables: dimDraft.value }
      }
      if (steps.includes('domain') && domainDraft.value) {
        stepConfig.domain = { domains: domainDraft.value }
      }
    }
    const job = await createJob(props.kind, workspaceId.value, {
      job_name: jobName.value.trim(),
      job_desc: jobDesc.value.trim() || null,
      config: {
        steps,
        step_config: Object.keys(stepConfig).length > 0 ? stepConfig : null,
      },
    })
    await router.push(`${basePath.value}/jobs/${job.job_id}`)
  } catch (cause) {
    saveError.value = cause instanceof ApiError ? cause.message : `${kindText.value}作业保存失败。`
  } finally {
    saving.value = false
  }
}

watch(
  () => props.kind,
  () => {
    selected.value = {}
    savedDescription.value = null
    dimDraft.value = null
    domainDraft.value = null
    initError.value = ''
    check.value = null
    checkError.value = ''
  },
)

function goBack() {
  const previous = window.history.state?.back
  if (typeof previous === 'string' && previous.length > 0) {
    router.back()
    return
  }
  router.push(basePath.value)
}
</script>

<template>
  <div class="ds-new-job">
    <header class="ds-new-job-header">
      <div>
        <span class="ds-kicker">{{ kind === 'collection' ? 'NEW COLLECTION JOB' : 'NEW PUBLISH JOB' }}</span>
        <h1>{{ pageTitle }}</h1>
        <p v-if="workspace && source">
          工作区已选定：{{ workspace.workspace_name }} · 数据源：{{ source.display_name }}。
          勾选需要的{{ kindText }}步骤后保存，保存不执行任何步骤。
        </p>
        <p v-else>勾选需要的{{ kindText }}步骤后保存，保存不执行任何步骤。</p>
      </div>
      <div class="ds-new-job-actions">
        <button type="button" class="ds-new-job-back" @click="goBack">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></svg>
          返回
        </button>
      </div>
    </header>

    <p v-if="ctxLoading" class="ds-jobs-state">正在读取工作区…</p>
    <p v-else-if="ctxError" class="ds-jobs-state error">{{ ctxError }}</p>

    <template v-else-if="workspace && source">
      <section class="ds-jobs-card">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">WORKSPACE INFO</span>
            <h2>工作区基本信息</h2>
          </div>
        </div>
        <div class="ds-jobs-facts cj-source-facts">
          <p><span>工作区名称</span><strong class="cj-plain">{{ workspace.workspace_name }}</strong></p>
          <p><span>数据源名称</span><strong class="cj-plain">{{ source.display_name }}</strong></p>
          <p><span>数据库名称</span><strong>{{ source.db_name }}</strong></p>
          <p><span>HOST : PORT</span><strong>{{ source.host }}:{{ source.port }}</strong></p>
          <p><span>数据库类型 / 版本</span><strong class="cj-plain">{{ sourceTypeText() }}</strong></p>
        </div>
      </section>

      <section class="ds-jobs-card">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">JOB INFO</span>
            <h2>作业信息</h2>
          </div>
          <span class="cj-required-note">* 为必填项</span>
        </div>
        <div class="cj-form">
          <label class="field">
            <span>作业名称 <em class="cj-required">*</em></span>
            <input v-model="jobName" type="text" placeholder="例如：订单全量采集" :disabled="saving" />
            <span class="character-count" :class="{ over: nameCount > NAME_MAX }">{{ nameCount }}/{{ NAME_MAX }}</span>
          </label>
          <label class="field">
            <span class="cj-label-row">
              作业用途说明
              <em class="character-count" :class="{ over: descCount > DESC_MAX }">{{ descCount }}/{{ DESC_MAX }}</em>
            </span>
            <textarea v-model="jobDesc" rows="3" placeholder="例如：订单与订单明细表的结构与字典采集" :disabled="saving"></textarea>
          </label>
        </div>
        <p v-if="saveError" class="status-message" role="alert">{{ saveError }}</p>
      </section>

      <section class="ds-jobs-card ds-new-job-steps">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">{{ kind === 'collection' ? 'COLLECTION STEPS' : 'PUBLISH STEPS' }}</span>
            <h2>步骤选择</h2>
            <p v-if="kind === 'collection'">采集、抽样画像、关联分析与 RAG 元数据生成。</p>
            <p v-else>只有采集成功且产物就绪的步骤可以发布；点击「发布校验」后，通过的步骤才可勾选、编辑与保存。</p>
          </div>
          <div class="ds-new-job-step-actions">
            <span
              v-if="kind === 'publish' && checkRan"
              class="cj-check-summary"
              :class="failedCount > 0 ? 'bad' : 'ok'"
            >
              {{ failedCount > 0 ? `${failedCount} 项未通过` : `${passedSteps.length} 项通过` }}
            </span>
            <button
              v-if="kind === 'publish'"
              type="button"
              :disabled="checking || ctxLoading"
              @click="runPublishCheck"
            >
              {{ checking ? '正在校验' : '发布校验' }}
            </button>
            <button type="button" @click="selectAll">全选</button>
            <button type="button" @click="selected = {}">清空</button>
          </div>
        </div>

        <p v-if="kind === 'publish' && checkError" class="status-message" role="alert">{{ checkError }}</p>

        <p
          v-for="hint in depHints"
          :key="hint.step"
          class="ds-new-job-hint"
          role="status"
        >
          {{ hint.step }} 未同选依赖步骤（{{ hint.deps.join('、') }}）：
          执行时将读取本工作区现有产物，不会自动补跑上游。
        </p>

        <StepGrid
          :kind="kind"
          :selected="selected"
          :available="kind === 'publish' ? passedSteps : undefined"
          :disabled="saving || checking || initializing"
          @toggle="toggleStep"
        >
          <template #after-step="{ step }">
            <button
              v-if="kind === 'collection' && step === 'domain'"
              type="button"
              class="nj-step-edit"
              :disabled="saving"
              @click="descriptionOpen = true"
            >
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" /></svg>
              编辑
            </button>
            <template v-if="kind === 'publish'">
              <span
                v-if="checkRan && stepCheck(step)?.ready"
                class="cj-check-mark ok"
                title="发布校验通过"
              >
                <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="m8.4 12.2 2.4 2.4 4.8-5.2" /></svg>
              </span>
              <span
                v-else-if="checkRan"
                class="cj-check-mark bad"
                :title="`发布校验未通过：${stepCheck(step)?.reason ?? '原因未知'}`"
              >
                <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="M12 7.6v5.2" /><path d="M12 16.4v.2" /></svg>
              </span>
              <em
                v-if="checkRan && !stepCheck(step)?.ready"
                class="cj-step-reason"
                :title="stepCheck(step)?.reason ?? '原因未知'"
              >
                {{ stepCheck(step)?.reason ?? '原因未知' }}
              </em>
              <!-- 首版本草稿编辑（doc 7.2 §3.2）：只改将保存进版本 1 的清单，
                   不写工作区配置文件；rel 无编辑入口 -->
              <template v-if="step === 'domain'">
                <em v-if="domainDraft !== null" class="cj-draft-mark">
                  已配置 {{ domainDraft.length }} 个域
                </em>
                <button
                  type="button"
                  class="nj-step-edit"
                  :disabled="saving || checking || initializing"
                  @click="domainOpen = true"
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" /></svg>
                  编辑
                </button>
              </template>
              <template v-if="step === 'dim_value'">
                <em v-if="dimDraft !== null" class="cj-draft-mark">
                  已配置 {{ dimDraft.length }} 张表
                </em>
                <button
                  type="button"
                  class="nj-step-edit"
                  :disabled="saving || checking || initializing"
                  @click="dimOpen = true"
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" /></svg>
                  编辑
                </button>
              </template>
            </template>
          </template>
        </StepGrid>

        <p v-if="initError" class="status-message" role="alert">{{ initError }}</p>
        <p v-if="dimIncomplete" class="status-message" role="alert">
          dim_value 需要完整的版本配置：请为至少一张表指定值向量化字段（自动读取配置后编辑），
          或取消勾选 dim_value。
        </p>
        <p v-if="domainIncomplete" class="status-message" role="alert">
          domain 需要完整的版本配置：请配置至少一个业务域（自动读取配置后编辑），
          或取消勾选 domain。
        </p>
      </section>

      <div class="cj-footer">
        <button type="button" class="button button-outline" :disabled="saving" @click="goBack">取消</button>
        <button type="button" class="button button-primary" :disabled="!canSave" @click="save">
          {{ saving ? '保存中' : '保存作业' }}
        </button>
      </div>
    </template>

    <DomainDescriptionDialog
      v-if="descriptionOpen"
      :initial-text="savedDescription ?? source?.description ?? ''"
      @save="onDescriptionSaved"
      @cancel="descriptionOpen = false"
    />

    <!-- 首版本草稿编辑（doc 7.2 §3.2：确认只回传清单，随保存写入版本 1；
         rel 无编辑入口） -->
    <DomainConfigDialog
      v-if="domainOpen"
      :workspace-id="workspaceId"
      :db-name="source?.db_name || ''"
      :initial-domains="domainDraft ?? []"
      purpose="version"
      @close="domainOpen = false"
      @apply="onDomainApply"
    />
    <DimValueConfigDialog
      v-if="dimOpen"
      :workspace-id="workspaceId"
      :db-name="source?.db_name || ''"
      :initial-tables="dimDraft ?? []"
      purpose="version"
      @close="dimOpen = false"
      @apply="onDimApply"
    />
  </div>
</template>

<style scoped>
.ds-new-job .ds-new-job-header {
  align-items: flex-end;
}
.cj-source-facts {
  grid-template-columns: repeat(5, minmax(0, 1fr));
}
.cj-plain {
  font-family: "Noto Sans SC", system-ui, sans-serif;
}
.cj-required-note {
  color: #66788f;
  font-size: 11px;
}
.cj-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
  width: 100%;
}
.cj-form .field {
  margin: 0;
}
.cj-form .field > span:first-child,
.cj-label-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 12px;
  color: #17293f;
  font-weight: 700;
  font-size: 12px;
  margin-bottom: 6px;
}
.cj-required {
  color: #dc4a4a;
  font-style: normal;
}
.cj-form input,
.cj-form textarea {
  width: 100%;
  border: 1px solid #d9e3ef;
  border-radius: 10px;
  background: #eef3f9;
  color: #20364f;
  font-size: 12px;
  line-height: 1.4;
  padding: 6px 10px;
  outline: none;
  resize: vertical;
}
.cj-form input {
  height: 32px;
}
.cj-form textarea {
  min-height: 72px;
}
.cj-form .character-count {
  font-size: 11px;
}
.cj-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}
.cj-footer .button {
  min-height: 36px;
  font-size: 13px;
}
.cj-form input:focus,
.cj-form textarea:focus {
  border-color: #3a83dc;
  box-shadow: 0 0 0 3px #dbeeff;
}
.character-count.over {
  color: #dc4a4a;
}
/* 发布校验（设计稿 32/33）：结果摘要 + 步骤行状态标记与截断原因 */
.cj-check-summary {
  display: inline-flex;
  align-items: center;
  padding: 7px 2px 7px 0;
  font-size: 11px;
  font-weight: 700;
  white-space: nowrap;
}
.cj-check-summary.ok {
  color: #16a34a;
}
.cj-check-summary.bad {
  color: #dc2626;
}
.cj-check-mark {
  display: inline-flex;
  flex-shrink: 0;
}
.cj-check-mark svg {
  width: 13px;
  height: 13px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.cj-check-mark.ok {
  color: #16a34a;
}
.cj-check-mark.bad {
  color: #dc2626;
}
.cj-step-reason {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-style: normal;
  color: #dc4a4a;
  font-size: 11px;
}
/* 版本草稿状态与自动初始化（doc 7.2 §3.2） */
.cj-draft-mark {
  font-style: normal;
  font-size: 9px;
  font-weight: 400;
  padding: 1px 6px;
  border-radius: 6px;
  background: #e3eefb;
  border: 1px solid #c5d7ee;
  color: #2e7cd6;
  white-space: nowrap;
}

@media (max-width: 1100px) {
  .cj-source-facts {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
