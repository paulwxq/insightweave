<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getDataSource, setDataSourceActivation } from '../api/dataSources'
import { displayDate } from '../api/format'
import { ApiError } from '../api/http'
import type { DataSourceListItem } from '../api/types'
import DeactivateSourceDialog from '../components/DeactivateSourceDialog.vue'
import DeleteSourceDialog from '../components/DeleteSourceDialog.vue'
import DeleteResourceDialog from '../components/DeleteResourceDialog.vue'
import PageBreadcrumbs from '../components/PageBreadcrumbs.vue'
import type { DeletionTaskSummary } from '../api/types'
import { resolveBack, withFrom } from '../navigation/from'

const route = useRoute()
const router = useRouter()

/** doc 9 §7.4：数据源详情不认来源，返回默认上一级（数据源列表） */
const backSpec = computed(() => resolveBack('source-detail', null, {}))
const sourceId = computed(() =>
  typeof route.params.sourceId === 'string' ? route.params.sourceId : '',
)
const source = ref<DataSourceListItem | null>(null)
const loading = ref(true)
const loadError = ref('')
const confirmOpen = ref(false)
const clearOpen = ref(false)
// doc 8.1 §7.0：从源响应的 deletion_task 找回未解决任务（刷新后同样适用）
const resumeTask = ref<DeletionTaskSummary | null>(null)
const activationOpen = ref(false)
const activationBusy = ref(false)
const activationError = ref('')

function leaveMissing() {
  return router.replace({ path: '/data-sources', query: { notice: 'not_found' } })
}

async function load() {
  loading.value = true
  loadError.value = ''
  source.value = null
  confirmOpen.value = false
  activationOpen.value = false
  activationBusy.value = false
  activationError.value = ''
  try {
    source.value = await getDataSource(sourceId.value)
  } catch (error) {
    if (
      error instanceof ApiError &&
      (error.code === 'not_found' || error.code === 'invalid_request')
    ) {
      await leaveMissing()
      return
    }
    loadError.value = error instanceof Error ? error.message : '数据源读取失败。'
  } finally {
    loading.value = false
  }
}

function openDelete() {
  resumeTask.value = null
  confirmOpen.value = true
}

function openClear() {
  resumeTask.value = null
  clearOpen.value = true
}

function openTaskProgress(task: DeletionTaskSummary) {
  if (task.action === 'delete_workspace' && task.workspace_id) {
    resumeTask.value = task
    return
  }
  resumeTask.value = task
  if (task.action === 'clear_source_knowledge') {
    clearOpen.value = true
  } else {
    confirmOpen.value = true
  }
}

/** 弹窗自身负责提交与任务轮询；完成（删除/清理成功）后刷新或离开。 */
async function onDeleteDone(action: string) {
  confirmOpen.value = false
  clearOpen.value = false
  // doc 8.1 §7.0：工作区任务弹窗由 resumeTask 驱动，成功后一并关闭
  resumeTask.value = null
  if (action === 'delete') {
    await router.push('/data-sources')
  } else {
    await load()
  }
}

function openActivation() {
  if (!source.value || activationBusy.value) return
  if (source.value.is_active) {
    activationBusy.value = false
    activationError.value = ''
    activationOpen.value = true
    return
  }
  void applyActivation(true)
}

async function applyActivation(isActive: boolean) {
  const current = source.value
  if (!current || activationBusy.value) return
  activationBusy.value = true
  activationError.value = ''
  try {
    source.value = await setDataSourceActivation(current.source_id, isActive)
    activationOpen.value = false
  } catch (error) {
    if (error instanceof ApiError && error.code === 'not_found') {
      activationOpen.value = false
      await leaveMissing()
      return
    }
    activationError.value = error instanceof Error ? error.message : '切换启用状态失败。'
  } finally {
    activationBusy.value = false
  }
}

watch(sourceId, () => void load(), { immediate: true })
</script>

<template>
  <div class="ds-detail">
    <div v-if="loading" class="state-block" role="status">正在读取数据源…</div>
    <div v-else-if="loadError" class="state-block error-state" role="alert">
      <p>{{ loadError }}</p>
      <button type="button" class="button button-outline" @click="load">重试</button>
      <RouterLink to="/data-sources" class="ds-jobs-back">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></svg>
          返回
        </RouterLink>
    </div>
    <template v-else-if="source">
      <PageBreadcrumbs
        :segments="[
          { label: '数据源管理', to: '/data-sources' },
          { label: source.display_name },
        ]"
      />
      <header class="ds-detail-header">
        <div class="ds-title">
          <span class="ds-kicker">DATA SOURCE</span>
          <h1>{{ source.display_name }}</h1>
          <p class="ds-source-id">Source ID: {{ source.source_id }}</p>
          <p class="ds-lead">已登记的连接信息，只可查看；采集与发布作业在工作区内管理。</p>
        </div>
        <div class="ds-detail-actions">
          <RouterLink class="ds-jobs-back" :to="backSpec.target!">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></svg>
            {{ backSpec.label }}
          </RouterLink>
          <button
            type="button"
            class="ds-enable"
            :disabled="activationBusy"
            :aria-pressed="source.is_active"
            :title="source.is_active ? '点击禁用该数据源' : '当前已禁用，点击启用'"
            @click="openActivation"
          >
            启用
            <span class="ds-switch" :class="{ off: !source.is_active }" aria-hidden="true"><span></span></span>
          </button>
          <p v-if="activationError && !activationOpen" class="status-message" role="alert">{{ activationError }}</p>
          <button
            v-if="source.deletion_task"
            type="button"
            class="ds-danger"
            @click="openTaskProgress(source.deletion_task)"
          >
            {{ source.deletion_task.status === "failed" ? "处理失败任务" : "查看删除/清理进度" }}
          </button>
          <button type="button" class="ds-danger" @click="openClear">清理已发布数据</button>
          <button type="button" class="ds-danger" @click="openDelete">删除</button>
          <RouterLink class="ds-edit" :to="`/data-sources/${source.source_id}/edit`">修改</RouterLink>
        </div>
      </header>

      <section class="ds-panel ds-panel-row">
        <div class="ds-panel-head">
          <span class="ds-step">1</span>
          <div>
            <h2>数据库类型</h2>
            <p>数据库类型不可修改</p>
          </div>
        </div>
        <span class="ds-type-badge">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <ellipse cx="12" cy="5" rx="9" ry="3" />
            <path d="M3 5v14a9 3 0 0 0 18 0V5" />
            <path d="M3 12a9 3 0 0 0 18 0" />
          </svg>
          PostgreSQL
        </span>
      </section>

      <section class="ds-panel">
        <div class="ds-panel-head">
          <span class="ds-step">2</span>
          <div>
            <h2>连接信息</h2>
            <p>以下为已保存的连接目标</p>
          </div>
        </div>
        <div class="ds-fields">
          <p><span>显示名称</span><strong>{{ source.display_name }}</strong></p>
          <p class="ds-mono"><span>主机</span><strong>{{ source.host }}</strong></p>
          <p class="ds-mono"><span>端口</span><strong>{{ source.port }}</strong></p>
          <p class="ds-mono"><span>数据库名</span><strong>{{ source.db_name }}</strong></p>
          <p class="ds-mono"><span>用户名</span><strong>{{ source.username }}</strong></p>
          <p class="ds-mono"><span>SSL 模式</span><strong>{{ source.sslmode || '未指定' }}</strong></p>
          <p class="ds-mono"><span>数据库版本</span><strong>{{ source.db_version || '版本未知' }}</strong></p>
          <p class="ds-mono"><span>登记时间</span><strong>{{ displayDate(source.created_at) }}</strong></p>
        </div>
      </section>

      <section class="ds-panel">
        <div class="ds-panel-head">
          <span class="ds-step">3</span>
          <div>
            <h2>已选 Schema</h2>
            <p>登记时保存的 Schema，打开本页不会重新连接</p>
          </div>
        </div>
        <div class="ds-schemas">
          <span v-for="schema in source.selected_schemas" :key="schema">{{ schema }}</span>
        </div>
      </section>

      <section class="ds-panel">
        <div class="ds-panel-head">
          <span class="ds-step">4</span>
          <div>
            <h2>说明</h2>
            <p>登记时填写的说明</p>
          </div>
        </div>
        <p class="ds-note">{{ source.description || '未填写' }}</p>
      </section>

      <section class="ds-panel">
        <div class="ds-panel-head">
          <span class="ds-step">5</span>
          <div>
            <h2>工作区</h2>
            <p>工作区隔离文件产物；同一数据源可有多个工作区，采集与发布作业都保存在工作区内</p>
          </div>
        </div>
        <div class="ws-entry-actions">
          <RouterLink class="ws-entry-link" :to="withFrom(`/workspaces?source_id=${source.source_id}`, `/data-sources/${source.source_id}`)">
            <strong>查看该数据源的工作区</strong>
            <small>进入工作区管理列表（按本数据源过滤）</small>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14" /><path d="m12 5 7 7-7 7" /></svg>
          </RouterLink>
          <RouterLink class="ws-entry-link" :to="withFrom(`/workspaces/new?source_id=${source.source_id}`, `/data-sources/${source.source_id}`)">
            <strong>新建工作区</strong>
            <small>为本数据源创建新的工作区</small>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14" /><path d="M5 12h14" /></svg>
          </RouterLink>
        </div>
      </section>

      <div class="ds-job-links">
        <RouterLink :to="`/data-assets/${source.source_id}/objects`">JSON 对象画像 <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14" /><path d="m12 5 7 7-7 7" /></svg></RouterLink>
      </div>
    </template>
    <DeactivateSourceDialog
      v-if="activationOpen && source"
      :display-name="source.display_name"
      :busy="activationBusy"
      :error="activationError"
      @cancel="activationOpen = false"
      @confirm="applyActivation(false)"
    />
    <DeleteSourceDialog
      v-if="confirmOpen && source"
      :source-id="source.source_id"
      :display-name="source.display_name"
      :resume-task-id="resumeTask?.action === 'delete_source' ? resumeTask.task_id : undefined"
      @cancel="confirmOpen = false; resumeTask = null"
      @done="onDeleteDone"
    />
    <DeleteSourceDialog
      v-if="clearOpen && source"
      :source-id="source.source_id"
      :display-name="source.display_name"
      mode="clear"
      :resume-task-id="resumeTask?.action === 'clear_source_knowledge' ? resumeTask.task_id : undefined"
      @cancel="clearOpen = false; resumeTask = null"
      @done="onDeleteDone"
    />
    <DeleteResourceDialog
      v-if="resumeTask?.action === 'delete_workspace' && resumeTask.workspace_id"
      kind="workspace"
      :target-id="resumeTask.workspace_id"
      :display-name="resumeTask.workspace_id"
      :resume-task-id="resumeTask.task_id"
      @cancel="resumeTask = null"
      @done="onDeleteDone('delete_workspace')"
    />
  </div>
</template>

<style scoped>
.ws-entry-actions {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
}
.ws-entry-link {
  display: flex;
  flex-direction: column;
  gap: 4px;
  border: 1px solid #e1e9f4;
  border-radius: 12px;
  background: #fcfdff;
  padding: 14px 18px;
  min-width: 240px;
  color: inherit;
}
.ws-entry-link:hover {
  border-color: #2e7cd6;
}
.ws-entry-link strong {
  color: #17293f;
  font-size: 14px;
}
.ws-entry-link small {
  color: #66788f;
  font-size: 11px;
}
.ws-entry-link svg {
  width: 14px;
  height: 14px;
  margin-top: 6px;
  align-self: flex-end;
  fill: none;
  stroke: #2e7cd6;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
@media (max-width: 800px) {
  .ws-entry-actions {
    flex-direction: column;
  }
}
</style>
