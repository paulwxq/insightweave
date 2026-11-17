<script setup lang="ts">
/**
 * 新建工作区（doc 6.3 §4 编号 22）：选择数据源 + 名称/说明，
 * 创建成功后进入工作区详情。?source_id= 预选所属数据源。
 * 长度约束与后端一致（backend/workspaces/service.py）：名称 ≤200、说明 ≤1000。
 */
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { listDataSources } from '../api/dataSources'
import { ApiError } from '../api/http'
import type { DataSourceListItem } from '../api/types'
import { createSourceWorkspace } from '../api/workspaces'
import EmptyState from '../components/EmptyState.vue'
import PageBreadcrumbs from '../components/PageBreadcrumbs.vue'
import {
  resolveBack,
  synthesizeWorkspaceCreatedFrom,
  withFrom,
  workspacesCrumbTo,
} from '../navigation/from'

const NAME_MAX = 200
const DESC_MAX = 1000

const route = useRoute()
const router = useRouter()

const sources = ref<DataSourceListItem[]>([])
const loading = ref(true)
const loadError = ref('')

const initialSourceId = computed(() => {
  const value = route.query.source_id
  return typeof value === 'string' && value !== '' ? value : ''
})

const sourceId = ref('')
const name = ref('')
const desc = ref('')
const saving = ref(false)
const saveError = ref('')
const pickerOpen = ref(false)
const pickerQuery = ref('')
const pickerInput = ref<HTMLInputElement | null>(null)

const selectedSource = computed(
  () => sources.value.find((item) => item.source_id === sourceId.value) ?? null,
)
const filteredSources = computed(() => {
  const keyword = pickerQuery.value.trim().toLowerCase()
  if (keyword === '') return sources.value
  return sources.value.filter((item) =>
    [item.display_name, item.source_id, item.db_name, item.host, item.username]
      .filter((field): field is string => Boolean(field))
      .some((field) => field.toLowerCase().includes(keyword)),
  )
})

function sourceMeta(item: DataSourceListItem): string {
  const version = item.db_version ? ` ${item.db_version}` : ''
  const endpoint = item.host ? `${item.host}:${item.port}` : item.db_name
  const user = item.username ? ` · 用户 ${item.username}` : ''
  return `${item.db_type}${version} · ${endpoint}${user}`
}

function closePicker() {
  pickerOpen.value = false
}

async function togglePicker() {
  if (saving.value) return
  if (pickerOpen.value) {
    closePicker()
    return
  }
  pickerQuery.value = ''
  pickerOpen.value = true
  await nextTick()
  pickerInput.value?.focus()
}

function onPickerFocusOut(event: FocusEvent) {
  const next = event.relatedTarget
  if (next instanceof Node && (event.currentTarget as HTMLElement).contains(next)) return
  closePicker()
}

function pickSource(item: DataSourceListItem) {
  sourceId.value = item.source_id
  pickerQuery.value = ''
  closePicker()
}

const nameCount = computed(() => [...name.value].length)
const descCount = computed(() => [...desc.value].length)

const canSave = computed(
  () =>
    !loading.value &&
    !saving.value &&
    sourceId.value !== '' &&
    name.value.trim().length > 0 &&
    nameCount.value <= NAME_MAX &&
    descCount.value <= DESC_MAX,
)

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const response = await listDataSources()
    sources.value = response.items.filter((item) => item.is_active)
    if (
      initialSourceId.value &&
      sources.value.some((item) => item.source_id === initialSourceId.value)
    ) {
      sourceId.value = initialSourceId.value
    }
  } catch (cause) {
    loadError.value = cause instanceof Error ? cause.message : '数据源列表加载失败。'
  } finally {
    loading.value = false
  }
}

function validate(): string {
  if (sourceId.value === '') return '请选择数据源'
  const trimmed = name.value.trim()
  if (!trimmed) return '请填写工作区名称'
  if (nameCount.value > NAME_MAX) return `工作区名称不能超过 ${NAME_MAX} 个字符`
  if (descCount.value > DESC_MAX) return `工作区说明不能超过 ${DESC_MAX} 个字符`
  return ''
}

async function save() {
  if (saving.value || !canSave.value) return
  saveError.value = validate()
  if (saveError.value) return
  saving.value = true
  try {
    const workspace = await createSourceWorkspace(sourceId.value, {
      workspace_name: name.value.trim(),
      workspace_desc: desc.value.trim() || null,
    })
    // 创建成功后进入工作区详情（doc 6.3 §4 编号 22）；
    // doc 9 §7.6：工作区详情的来源按进入新建页时的 from 四种情况合成
    await router.push(
      withFrom(
        `/workspaces/${workspace.workspace_id}`,
        synthesizeWorkspaceCreatedFrom(queryFrom(), sourceId.value),
      ),
    )
  } catch (cause) {
    saveError.value = cause instanceof ApiError ? cause.message : '工作区创建失败。'
  } finally {
    saving.value = false
  }
}

// ---- doc 9 §7.4：返回与来源 ----

function queryFrom(): string | null {
  const raw = route.query.from
  return typeof raw === 'string' ? raw : null
}

/** 认 /workspaces…、数据源来源；默认按表单当前选择筛选列表 */
const backSpec = computed(() =>
  resolveBack('workspace-new', queryFrom(), { selectedSourceId: sourceId.value }),
)

function cancel() {
  if (backSpec.value.target) void router.push(backSpec.value.target)
}

onMounted(load)
</script>

<template>
  <div class="cw-page">
    <PageBreadcrumbs
      :segments="[
        { label: '工作区管理', to: workspacesCrumbTo(queryFrom()) },
        { label: '新建工作区' },
      ]"
    />
    <header class="ds-jobs-header">
      <div>
        <span class="ds-kicker">NEW WORKSPACE</span>
        <h1>新建工作区</h1>
        <p>
          工作区归属一个数据源并隔离一份文件产物；同源可有多个工作区，
          工作区内创建采集与发布作业。
        </p>
      </div>
      <div class="cw-actions">
        <button type="button" class="ds-jobs-back" @click="cancel">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></svg>
          {{ backSpec.label }}
        </button>
      </div>
    </header>

    <p v-if="loading" class="ds-jobs-state">正在读取数据源列表…</p>
    <div v-else-if="loadError" class="state-block error-state" role="alert">
      <p>{{ loadError }}</p>
      <button type="button" class="button button-outline" @click="load">重试</button>
    </div>
    <EmptyState
      v-else-if="sources.length === 0"
      title="还没有可用的数据源"
      description="工作区必须归属一个启用的数据源；请先登记数据源。"
    >
      <template #action>
        <RouterLink to="/data-sources/new" class="button button-primary">新建数据源连接</RouterLink>
      </template>
    </EmptyState>

    <section v-else class="ds-jobs-card">
      <div class="ds-jobs-list-head">
        <div>
          <span class="ds-kicker ds-kicker-sm">WORKSPACE INFO</span>
          <h2>基本信息</h2>
        </div>
      </div>
      <form class="cw-form" @submit.prevent="save">
        <div class="field cw-picker" @focusout="onPickerFocusOut">
          <span>选择数据源 <em class="cw-required">*</em></span>
          <div class="cw-picker-control">
            <button
              type="button"
              class="cw-picker-trigger"
              :aria-expanded="pickerOpen"
              aria-haspopup="listbox"
              :disabled="saving"
              @click="togglePicker"
            >
              <span v-if="selectedSource" class="cw-picker-value">
                <strong>{{ selectedSource.display_name }}</strong>
                <small>{{ selectedSource.source_id }}</small>
              </span>
              <span v-else class="cw-picker-placeholder">搜索数据源名称或 ID…</span>
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
            </button>
            <div v-if="pickerOpen" class="cw-picker-panel" role="listbox">
              <label class="cw-picker-search">
                <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
                <input
                  ref="pickerInput"
                  v-model="pickerQuery"
                  type="search"
                  placeholder="搜索数据源名称或 ID…"
                  aria-label="搜索数据源"
                  @keydown.esc.prevent="closePicker"
                />
              </label>
              <p v-if="filteredSources.length === 0" class="cw-picker-empty">没有匹配的数据源</p>
              <ul v-else>
                <li v-for="item in filteredSources" :key="item.source_id">
                  <button
                    type="button"
                    role="option"
                    :aria-selected="item.source_id === sourceId"
                    :class="{ active: item.source_id === sourceId }"
                    @mousedown.prevent="pickSource(item)"
                  >
                    <span>
                      <strong>{{ item.display_name }}</strong>
                      <small>{{ item.source_id }}</small>
                      <em>{{ sourceMeta(item) }}</em>
                    </span>
                    <svg v-if="item.source_id === sourceId" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6 9 17l-5-5" /></svg>
                  </button>
                </li>
              </ul>
            </div>
          </div>
        </div>
        <label class="field">
          <span>工作区名称 <em class="cw-required">*</em></span>
          <input
            v-model="name"
            type="text"
            placeholder="例如：生产采集"
            :disabled="saving"
          />
          <span class="character-count">{{ nameCount }}/{{ NAME_MAX }}</span>
        </label>
        <label class="field">
          <span>工作区说明（可选）</span>
          <textarea
            v-model="desc"
            rows="4"
            placeholder="用途说明"
            :disabled="saving"
          ></textarea>
          <span class="character-count">{{ descCount }}/{{ DESC_MAX }}</span>
        </label>
        <p v-if="saveError" class="status-message" role="alert">{{ saveError }}</p>
        <div class="dialog-actions">
          <button type="button" class="button button-outline" :disabled="saving" @click="cancel">
            取消
          </button>
          <button type="submit" class="button button-primary" :disabled="!canSave || saving">
            {{ saving ? '创建中' : '创建工作区' }}
          </button>
        </div>
      </form>
      <p class="cw-note">
        创建工作区会在服务端建立对应的产物目录；工作区不可改绑数据源。
      </p>
    </section>
  </div>
</template>

<style scoped>
.cw-page {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.cw-page .ds-jobs-header {
  align-items: flex-start;
}
.cw-actions {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}
.cw-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.cw-required {
  color: #dc4a4a;
  font-style: normal;
}
.cw-note {
  margin: 0;
  color: #66788f;
  font-size: 12px;
}
.cw-form .field > span:first-child {
  font-size: 12px;
}
.cw-form input,
.cw-form textarea {
  font-size: 12px;
  padding: 7px 12px;
}
.cw-form .character-count {
  font-size: 11px;
}
.cw-form .dialog-actions .button {
  min-height: 32px;
  padding: 0 14px;
  font-size: 12px;
}
.cw-picker {
  position: relative;
}
.cw-picker-control {
  position: relative;
}
.cw-picker-trigger {
  width: 100%;
  min-height: 34px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  border: 1px solid #d2e0f2;
  border-radius: 10px;
  background: #f1f7ff;
  padding: 6px 12px;
  color: #20364f;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}
.cw-picker-trigger:disabled {
  opacity: 0.6;
}
.cw-picker-value {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}
.cw-picker-value strong {
  font-size: 12px;
  font-weight: 700;
}
.cw-picker-value small,
.cw-picker-panel small {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
  color: #2e7cd6;
  font-weight: 500;
}
.cw-picker-placeholder {
  color: #8a99ac;
}
.cw-picker-trigger > svg,
.cw-picker-search svg,
.cw-picker-panel li button > svg {
  width: 14px;
  height: 14px;
  flex: 0 0 14px;
  fill: none;
  stroke: #66788f;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.cw-picker-panel {
  position: absolute;
  z-index: 20;
  top: calc(100% + 6px);
  left: 0;
  right: 0;
  background: #fff;
  border: 1px solid #d9e3ef;
  border-radius: 12px;
  box-shadow: 0 8px 24px rgba(23, 41, 63, 0.1);
  padding: 8px;
}
.cw-picker-search {
  display: flex;
  align-items: center;
  gap: 6px;
  border: 1px solid #d9e3ef;
  border-radius: 8px;
  padding: 6px 8px;
  color: #66788f;
}
.cw-picker-search input {
  width: 100%;
  min-width: 0;
  border: 0;
  padding: 0;
  outline: none;
  background: transparent;
  font-size: 12px;
  color: #17293f;
}
.cw-picker-panel ul {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  max-height: 240px;
  overflow: auto;
}
.cw-picker-panel li button {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  text-align: left;
  border: 1px solid transparent;
  background: transparent;
  border-radius: 8px;
  padding: 8px 10px;
  cursor: pointer;
}
.cw-picker-panel li button span {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px 8px;
  min-width: 0;
}
.cw-picker-panel li button strong {
  font-size: 12px;
  color: #17293f;
}
.cw-picker-panel li button em {
  flex-basis: 100%;
  font-style: normal;
  font-size: 11px;
  color: #66788f;
}
.cw-picker-panel li button.active,
.cw-picker-panel li button:hover {
  background: #e3eefb;
  border-color: #2e7cd6;
}
.cw-picker-empty {
  margin: 8px 0 0;
  padding: 8px 10px;
  color: #66788f;
  font-size: 12px;
}
</style>
