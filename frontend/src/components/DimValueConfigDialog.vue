<script setup lang="ts">
// doc 7.2 §3.1.1/§4：dim_value 草稿编辑弹窗（运行草稿 / 版本草稿两种用途）。
// 仅从 props 初始化并深拷贝为独立编辑状态；确认只回传父组件，不调用
// 配置 YAML GET/PUT。新建页传入配置接口同次返回的推荐；其他场景及人工新增
// 表的推荐经独立只读接口读取（0/1 校验口径不变）。人工可为每表
// 配置一个或多个字段（数组形态），不限制最多一个。
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'

import {
  getDimValueObjectColumns,
  getDimValueObjects,
  getDimValueRecommendations,
} from '../api/workspaces'
import type {
  DimValueColumn,
  DimValueObject,
  DimValueRecommendationItem,
  DimValueTableParam,
} from '../api/types'

interface Row {
  schema_name: string
  object_name: string
  /** 已选字段（数组形态；空数组 = 未指定） */
  embedding_col: string[]
  /** 推荐展示三态（doc 7.1 §4.4）：已加载 / 会话内新增未加载 / 画像不可用 */
  recState: 'loaded' | 'unloaded' | 'unavailable'
  recColumns: string[]
  recConfidence: number | null
}

const props = defineProps<{
  workspaceId: string
  dbName: string
  /** 当前草稿或引用版本默认配置（数组形态）；弹窗深拷贝为独立编辑状态 */
  initialTables: DimValueTableParam[]
  /** 与初始配置同次读取的推荐；缺省时沿用独立推荐接口。 */
  initialRecommendations?: DimValueRecommendationItem[]
  /** run = 本次运行草稿；version = 作业版本草稿 */
  purpose: 'run' | 'version'
}>()

const emit = defineEmits<{
  close: []
  apply: [tables: DimValueTableParam[]]
}>()

const rows = ref<Row[]>([])
const objects = ref<DimValueObject[]>([])
/** 候选表接口失败：草稿仍可查看/应用，仅禁用新增（doc 7.2 §4 局部降级） */
const objectsError = ref('')
const recError = ref('')
const selectedKey = ref('')
const columns = ref<DimValueColumn[]>([])
const columnsLoading = ref(false)
const columnsError = ref('')
const applyError = ref('')
const addOpen = ref(false)
const addSearch = ref('')
const addInput = ref<HTMLInputElement | null>(null)

const keyOf = (row: { schema_name: string; object_name: string }) =>
  `${row.schema_name}.${row.object_name}`

const selectedRow = computed(() => rows.value.find((r) => keyOf(r) === selectedKey.value) || null)

const unsetCount = computed(() => rows.value.filter((r) => !r.embedding_col.length).length)

const confirmText = computed(() =>
  props.purpose === 'run' ? '应用到本次执行' : '应用到作业配置',
)
const scopeNote = computed(() =>
  props.purpose === 'run'
    ? '仅应用到本次运行，不修改工作区配置文件'
    : '随作业保存写入新版本，不直接修改工作区文件',
)

function recText(row: Row): string {
  if (row.recState === 'unloaded') return '推荐未加载'
  if (row.recState === 'unavailable') return '推荐信息不可用'
  return row.recColumns[0] || '无推荐'
}

function recConf(row: Row): string {
  if (row.recState !== 'loaded' || !row.recColumns[0]) return ''
  return row.recConfidence === null ? '—' : row.recConfidence.toFixed(2)
}

function recTitle(row: Row): string | undefined {
  if (row.recState === 'unloaded') return '推荐未加载，应用后重新打开可查看'
  if (row.recState !== 'loaded' || !row.recColumns[0]) return undefined
  const conf = row.recConfidence === null ? '—' : row.recConfidence.toFixed(2)
  return `模型推荐：${row.recColumns[0]}（置信度 ${conf}）`
}

function recMuted(row: Row): boolean {
  return row.recState !== 'loaded' || row.recColumns.length === 0
}

/** 与推荐的差异按字段集合判断（doc 7.2 §3.1.1）：多选其他字段也算不同 */
const differsFromRec = (row: Row) => {
  if (row.recState !== 'loaded' || row.recColumns.length === 0) return false
  if (row.embedding_col.length !== row.recColumns.length) return true
  return row.recColumns.some((name) => !row.embedding_col.includes(name))
}

const canApply = computed(() => unsetCount.value === 0)

const searchHits = computed(() => {
  if (!addOpen.value) return []
  const keyword = addSearch.value.trim().toLowerCase()
  const inList = new Set(rows.value.map(keyOf))
  return objects.value
    .filter((obj) => !inList.has(keyOf(obj)))
    .filter((obj) => !keyword || keyOf(obj).toLowerCase().includes(keyword))
    .slice(0, 8)
})

const pendingAddCount = computed(() =>
  addOpen.value && addSearch.value.trim() ? searchHits.value.length : 0,
)

function objectType(row: Row) {
  return objects.value.find((o) => keyOf(o) === keyOf(row))?.object_type || 'table'
}

function typeLabel(objectTypeName: string) {
  if (objectTypeName === 'materialized_view') return '物化视图'
  if (objectTypeName === 'view') return '视图'
  return '表'
}

function typeKind(objectTypeName: string) {
  if (objectTypeName === 'materialized_view') return 'mv'
  if (objectTypeName === 'view') return 'view'
  return 'table'
}

/** 优先使用配置响应中的推荐；只补读响应之外的人工新增表，不覆盖已带入的推荐。 */
async function loadRecommendations() {
  recError.value = ''
  if (!rows.value.length) return
  const byKey = new Map((props.initialRecommendations ?? []).map((t) => [keyOf(t), t]))
  function applyRecommendations() {
    for (const row of rows.value) {
      if (row.recState === 'unloaded') continue // 会话内新增未应用的行
      const rec = byKey.get(keyOf(row))
      if (!rec || !rec.recommendation_available) {
        row.recState = 'unavailable'
        row.recColumns = []
        row.recConfidence = null
      } else {
        row.recState = 'loaded'
        row.recColumns = [...rec.dimension_value_columns]
        row.recConfidence = rec.dimension_value_confidence
      }
    }
  }
  // 已带入的推荐立即展示，不等待人工新增表的补读请求。
  if (props.initialRecommendations !== undefined) applyRecommendations()
  if (props.initialRecommendations === undefined || rows.value.some((row) => !byKey.has(keyOf(row)))) {
    try {
      const resp = await getDimValueRecommendations(props.workspaceId)
      for (const rec of resp.tables) {
        if (!byKey.has(keyOf(rec))) byKey.set(keyOf(rec), rec)
      }
    } catch {
      recError.value = '部分推荐信息读取失败，仅影响推荐列展示'
    }
  }
  applyRecommendations()
}

async function loadColumns(row: { schema_name: string; object_name: string }) {
  columnsLoading.value = true
  columnsError.value = ''
  columns.value = []
  try {
    const resp = await getDimValueObjectColumns(
      props.workspaceId,
      row.schema_name,
      row.object_name,
    )
    columns.value = resp.columns
  } catch {
    columnsError.value = '该对象在产物中不存在'
  } finally {
    columnsLoading.value = false
  }
}

function selectRow(row: Row) {
  const same = selectedKey.value === keyOf(row)
  selectedKey.value = keyOf(row)
  addOpen.value = false
  if (!same) void loadColumns(row)
}

/** 多选切换：再次点击已选字段即移除（doc 7.2 §6.1，不以单选覆盖其他字段） */
function toggleColumn(row: Row, columnName: string) {
  const index = row.embedding_col.indexOf(columnName)
  if (index >= 0) row.embedding_col.splice(index, 1)
  else row.embedding_col.push(columnName)
}

async function toggleAdd() {
  addOpen.value = !addOpen.value
  addSearch.value = ''
  if (!addOpen.value) return
  await nextTick()
  addInput.value?.focus()
}

function pickSearchHit(obj: DimValueObject) {
  addObject(obj)
}

function removeRow(row: Row) {
  rows.value = rows.value.filter((r) => r !== row)
  if (selectedKey.value === keyOf(row)) {
    selectedKey.value = rows.value.length ? keyOf(rows.value[0]) : ''
    if (selectedRow.value) void loadColumns(selectedRow.value)
    else columns.value = []
  }
}

function addObject(obj: DimValueObject) {
  const row: Row = {
    schema_name: obj.schema_name,
    object_name: obj.object_name,
    embedding_col: [],
    recState: 'unloaded',
    recColumns: [],
    recConfidence: null,
  }
  rows.value.push(row)
  addOpen.value = false
  addSearch.value = ''
  selectRow(row)
}

function apply() {
  applyError.value = ''
  if (unsetCount.value > 0) {
    applyError.value = `无法应用：${unsetCount.value} 张表未指定 embedding_col`
    return
  }
  // 只回传执行配置字段，推荐信息不进入（doc 7.1 §4.4 / doc 7.2 §3.1）
  emit(
    'apply',
    rows.value.map((r) => ({
      schema_name: r.schema_name,
      object_name: r.object_name,
      embedding_col: [...r.embedding_col],
    })),
  )
}

function close() {
  emit('close')
}

function onKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape') return
  if (addOpen.value) {
    addOpen.value = false
    addSearch.value = ''
    return
  }
  close()
}

onMounted(() => {
  // props 深拷贝为独立编辑状态（§4 第 1 项）
  rows.value = props.initialTables.map((t) => ({
    schema_name: t.schema_name,
    object_name: t.object_name,
    embedding_col: [...t.embedding_col],
    recState: 'loaded',
    recColumns: [],
    recConfidence: null,
  }))
  selectedKey.value = rows.value.length ? keyOf(rows.value[0]) : ''
  if (selectedRow.value) void loadColumns(selectedRow.value)
  // 候选表与推荐异步加载，失败只局部降级，不阻断草稿展示
  getDimValueObjects(props.workspaceId)
    .then((resp) => {
      objects.value = resp.objects
    })
    .catch(() => {
      objects.value = []
      objectsError.value = '候选表列表读取失败，暂不能新增表；已有草稿仍可编辑和应用'
    })
  void loadRecommendations()
  window.addEventListener('keydown', onKeydown)
})
onUnmounted(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <div class="dialog-backdrop dimcfg-backdrop" @click.self="close">
    <div class="dialog-panel dimcfg" role="dialog" aria-modal="true" aria-labelledby="dimcfg-title">
      <header class="dimcfg-head">
        <div>
          <p class="dimcfg-kicker">DIM VALUE EMBEDDING CONFIG</p>
          <div class="dimcfg-title-row">
            <h2 id="dimcfg-title">维表值向量化配置</h2>
            <span class="dimcfg-file">
              {{ purpose === 'run' ? '本次执行草稿' : '版本配置草稿' }}
            </span>
          </div>
          <p class="dimcfg-lead">
            为每张表 / 视图 / 物化视图选择一个或多个 embedding_col（值向量化字段）；
            {{ scopeNote }}。
          </p>
        </div>
        <button type="button" class="dimcfg-x" aria-label="关闭" @click="close">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg>
        </button>
      </header>

      <div class="dimcfg-body">
        <section class="dimcfg-side">
          <div class="dimcfg-side-head">
            <strong>表列表</strong>
            <span class="dimcfg-count">
              {{ rows.length }} 张表
              <template v-if="unsetCount"> · {{ unsetCount }} 张未指定</template>
              <template v-else-if="rows.length"> · 全部已指定</template>
              <template v-if="pendingAddCount"> · 新增 {{ pendingAddCount }} 张待选择</template>
            </span>
          </div>
          <div class="dimcfg-card">
            <div class="dimcfg-scroll">
              <div class="dimcfg-grid dimcfg-grid-head">
                <span>表 / 视图 / 物化视图</span>
                <span>值向量化字段名</span>
                <span>模型推荐</span>
                <span>操作</span>
              </div>
              <div
                v-for="row in rows"
                :key="keyOf(row)"
                class="dimcfg-grid dimcfg-item"
                :class="{ active: keyOf(row) === selectedKey }"
                @click="selectRow(row)"
              >
                <div class="dimcfg-object">
                  <strong :title="keyOf(row)">{{ keyOf(row) }}</strong>
                  <em class="dimcfg-type-tag" :class="typeKind(objectType(row))">
                    {{ typeLabel(objectType(row)) }}
                  </em>
                </div>
                <span class="dimcfg-col" :class="{ unset: !row.embedding_col.length }">
                  {{ row.embedding_col.length ? row.embedding_col.join('、') : '未指定' }}
                </span>
                <span
                  class="dimcfg-rec"
                  :class="{ muted: recMuted(row) }"
                  :title="recTitle(row)"
                >
                  <span class="dimcfg-rec-text">{{ recText(row) }}</span>
                  <span v-if="recConf(row)" class="dimcfg-rec-conf">{{ recConf(row) }}</span>
                  <em v-if="differsFromRec(row)" class="dimcfg-diff">与推荐不同</em>
                </span>
                <div class="dimcfg-ops">
                  <button type="button" class="dimcfg-delete" :aria-label="`删除 ${keyOf(row)}`" @click.stop="removeRow(row)">
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                      <path d="M4 7h16" />
                      <path d="M9 7V5h6v2" />
                      <path d="M7 7l1 13h8l1-13" />
                    </svg>
                    删除
                  </button>
                </div>
              </div>
              <p v-if="!rows.length" class="dimcfg-empty">尚未添加任何表</p>
            </div>
            <div v-if="addOpen" class="dimcfg-finder">
              <label class="dimcfg-finder-input">
                <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6" /><path d="m20 20-3.5-3.5" /></svg>
                <input
                  ref="addInput"
                  v-model="addSearch"
                  type="search"
                  placeholder="输入关键字搜索 schema.table，支持表 / 视图 / 物化视图"
                  aria-label="搜索要新增的表"
                />
                <button type="button" aria-label="关闭搜索" @click="toggleAdd">×</button>
              </label>
              <ul v-if="searchHits.length" class="dimcfg-menu">
                <li v-for="obj in searchHits" :key="keyOf(obj)">
                  <button type="button" @click="pickSearchHit(obj)">
                    <span>{{ keyOf(obj) }}</span>
                    <em class="dimcfg-type-tag" :class="typeKind(obj.object_type)">
                      {{ typeLabel(obj.object_type) }}
                    </em>
                  </button>
                </li>
              </ul>
              <p v-else class="dimcfg-finder-empty">没有匹配的对象</p>
            </div>
          </div>
          <button
            type="button"
            class="dimcfg-add"
            :class="{ on: addOpen }"
            :disabled="!!objectsError"
            @click="toggleAdd"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
            新增表
          </button>
          <p v-if="objectsError" class="dimcfg-hint alert" role="alert">
            {{ objectsError }}
          </p>
          <p v-else-if="recError" class="dimcfg-hint alert" role="alert">
            {{ recError }}
          </p>
        </section>

        <section class="dimcfg-structure">
          <template v-if="selectedRow">
            <div class="dimcfg-side-head">
              <strong>表结构</strong>
              <span class="dimcfg-object-tag">{{ keyOf(selectedRow) }}</span>
              <span v-if="!columnsLoading && !columnsError" class="dimcfg-field-count">
                {{ columns.length }} 个字段
              </span>
            </div>
            <p v-if="columnsLoading" class="dimcfg-status">读取表结构…</p>
            <p v-else-if="columnsError" class="dimcfg-status alert">{{ columnsError }}</p>
            <div v-else class="dimcfg-card dimcfg-card-fill">
              <div class="dimcfg-scroll">
                <div class="dimcfg-cols dimcfg-cols-head">
                  <span>column_name</span>
                  <span>column_type</span>
                  <span>字段注释</span>
                  <span>样例数据</span>
                  <span>操作</span>
                </div>
                <div v-for="col in columns" :key="col.name" class="dimcfg-cols dimcfg-col-row">
                  <strong>{{ col.name }}</strong>
                  <span class="dimcfg-type">{{ col.data_type }}</span>
                  <span class="dimcfg-comment">{{ col.comment || '—' }}</span>
                  <span class="dimcfg-sample">{{ col.sample_value || '—' }}</span>
                  <button
                    type="button"
                    class="dimcfg-plus"
                    :class="{ on: selectedRow.embedding_col.includes(col.name) }"
                    @click="selectedRow && toggleColumn(selectedRow, col.name)"
                  >
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                      <path v-if="selectedRow.embedding_col.includes(col.name)" d="M5 12.5 9.5 17 19 7" />
                      <path v-else d="M12 5v14M5 12h14" />
                    </svg>
                    {{ selectedRow.embedding_col.includes(col.name) ? '已选择' : '添加' }}
                  </button>
                </div>
                <p v-if="!columns.length" class="dimcfg-empty">该对象没有字段</p>
              </div>
            </div>
          </template>
          <template v-else>
            <div class="dimcfg-side-head">
              <strong>表结构</strong>
              <span class="dimcfg-field-count">未选择</span>
            </div>
            <div class="dimcfg-card dimcfg-placeholder">
              <span class="dimcfg-placeholder-mark" aria-hidden="true">
                <svg viewBox="0 0 24 24"><rect x="4" y="5" width="16" height="14" rx="2" /><path d="M4 10h16M9 5v14" /></svg>
              </span>
              <p>选择或新增表后，此处展示该表的字段列表</p>
              <small>新增的表需要先指定值向量化字段（embedding_col）后才能应用；其模型推荐在应用后重新打开时展示</small>
            </div>
          </template>
        </section>
      </div>

      <footer class="dimcfg-foot">
        <span class="dimcfg-path">databases › {{ dbName || '…' }} › tables</span>
        <span v-if="applyError" class="dimcfg-save-error" role="alert">{{ applyError }}</span>
        <span v-else class="dimcfg-save-note">{{ scopeNote }}</span>
        <button type="button" class="dimcfg-cancel" @click="close">取消</button>
        <button type="button" class="dimcfg-save" :disabled="!canApply" @click="apply">
          <svg v-if="canApply" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5 9.5 17 19 7" /></svg>
          {{ confirmText }}
        </button>
      </footer>

    </div>
  </div>
</template>

<style scoped>
.dimcfg-backdrop { grid-template-columns: minmax(0, 1fr); }
.dimcfg {
  width: min(1480px, calc(100vw - 48px));
  max-width: 100%;
  height: min(800px, calc(100vh - 48px));
  max-height: calc(100vh - 48px);
  padding: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-radius: 16px;
  border: 1px solid #d9e3ef;
  box-shadow: 0 16px 48px #17293f40;
  font-family: "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
  color: #17293f;
}
.dimcfg-head {
  display: flex;
  align-items: flex-start;
  gap: 16px;
  padding: 20px 28px;
  border-bottom: 1px solid #d9e3ef;
  flex-shrink: 0;
}
.dimcfg-head > div { min-width: 0; flex: 1; }
.dimcfg p { margin: 0; }
.dimcfg-kicker {
  margin: 0;
  color: #2e7cd6;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 2.5px;
}
.dimcfg-title-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 5px;
}
.dimcfg-title-row h2 {
  margin: 0;
  font-size: 20px;
  font-weight: 700;
  letter-spacing: 0;
}
.dimcfg-file {
  background: #eef3f9;
  color: #2e7cd6;
  border-radius: 7px;
  padding: 3px 8px;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 10px;
  font-weight: 700;
}
.dimcfg-lead {
  margin: 5px 0 0;
  color: #66788f;
  font-size: 12px;
  line-height: 1.5;
}
.dimcfg-x {
  width: 30px;
  height: 30px;
  border: 1px solid #d9e3ef;
  border-radius: 9px;
  background: #fff;
  color: #66788f;
  display: grid;
  place-items: center;
  flex-shrink: 0;
  padding: 0;
}
.dimcfg-x svg,
.dimcfg-delete svg,
.dimcfg-pick svg,
.dimcfg-add svg,
.dimcfg-hint svg,
.dimcfg-warn svg,
.dimcfg-plus svg,
.dimcfg-finder svg,
.dimcfg-menu svg,
.dimcfg-placeholder-mark svg,
.dimcfg-save svg {
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.dimcfg-x svg { width: 14px; height: 14px; }
.dimcfg-x:hover { background: #eef3f9; }
.dimcfg-body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(0, 3fr);
}
.dimcfg-side,
.dimcfg-structure {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 18px 24px;
}
.dimcfg-side { border-right: 1px solid #d9e3ef; padding-inline: 16px; }
.dimcfg-side-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.dimcfg-side-head strong { font-size: 13px; }
.dimcfg-count,
.dimcfg-object-tag {
  background: #e3eefb;
  color: #2e7cd6;
  border-radius: 8px;
  font-weight: 700;
}
.dimcfg-count { padding: 2px 8px; font-size: 10px; }
.dimcfg-object-tag {
  padding: 3px 10px;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
}
.dimcfg-field-count {
  margin-left: auto;
  color: #66788f;
  font-size: 10px;
}
.dimcfg-card {
  border: 1px solid #d9e3ef;
  border-radius: 12px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  min-height: 0;
  background: #fff;
}
.dimcfg-card:has(.dimcfg-finder) { overflow: visible; }
.dimcfg-card-fill { flex: 1; }
.dimcfg-scroll { overflow: auto; min-height: 0; flex: 1; }
.dimcfg-grid,
.dimcfg-cols {
  display: grid;
  align-items: center;
  padding: 8px 12px;
}
.dimcfg-grid {
  grid-template-columns: minmax(160px, 2.2fr) minmax(80px, 1fr) minmax(80px, 1fr) 60px;
  min-width: 404px;
  gap: 0;
}
.dimcfg-cols {
  gap: 0;
  grid-template-columns: repeat(4, minmax(0, 1fr)) 76px;
}
.dimcfg-grid-head,
.dimcfg-cols-head {
  position: sticky;
  top: 0;
  z-index: 1;
  background: #eef3f9;
  color: #66788f;
  font-size: 10px;
  font-weight: 700;
  flex-shrink: 0;
}
.dimcfg-grid.dimcfg-grid-head > span:nth-child(4) { text-align: right; }
.dimcfg-cols-head > span:nth-child(5) { text-align: right; }
.dimcfg-item {
  border-top: 1px solid #d9e3ef;
  cursor: pointer;
  min-height: 50px;
}
.dimcfg-item.active { background: #e3eefb; }
.dimcfg-item.warn { background: #fdeeee; }
.dimcfg-item.warn.active { background: #fde4e4; }
.dimcfg-warn {
  display: flex;
  gap: 8px;
  margin: 0;
  padding: 8px 10px;
  border-radius: 10px;
  background: #fdeeee;
  color: #9a3d3d;
  font-size: 11px;
  line-height: 1.5;
}
.dimcfg-warn svg { width: 14px; height: 14px; margin-top: 2px; color: #c25454; flex-shrink: 0; }
.dimcfg-warn button {
  border: 0;
  background: transparent;
  color: #2e7cd6;
  font: inherit;
  font-weight: 700;
  padding: 0 2px;
  text-decoration: underline;
}
.dimcfg-object {
  min-width: 0;
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.dimcfg-object strong {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dimcfg-type-tag {
  display: inline-flex;
  align-items: center;
  align-self: flex-start;
  font-style: normal;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.2px;
  line-height: 1.25;
  padding: 2px 6px;
  border-radius: 6px;
  border: 1px solid transparent;
  white-space: nowrap;
}
.dimcfg-type-tag.table {
  color: #0e9f8a;
  background: #ddf0eb;
  border-color: #b8e5d9;
}
.dimcfg-type-tag.view {
  color: #2e7cd6;
  background: #e8f2fc;
  border-color: #c5daf2;
}
.dimcfg-type-tag.mv {
  color: #7a6fd0;
  background: #ede9f8;
  border-color: #d4cce8;
}
.dimcfg-col {
  min-width: 0;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
  font-weight: 700;
  color: #2e7cd6;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dimcfg-col.unset {
  font-family: "Noto Sans SC", "PingFang SC", sans-serif;
  font-size: 10px;
  color: #c25454;
}
.dimcfg-rec {
  min-width: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  justify-items: start;
  gap: 3px;
  color: #0e9f8a;
}
.dimcfg-rec-text {
  min-width: 0;
  width: 100%;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 10px;
  font-weight: 700;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dimcfg-rec-conf {
  flex-shrink: 0;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 10px;
  font-weight: 700;
  white-space: nowrap;
}
.dimcfg-rec.muted .dimcfg-rec-text {
  font-family: "Noto Sans SC", "PingFang SC", sans-serif;
  font-weight: 600;
  color: #8a99ac;
}
.dimcfg-diff {
  flex-shrink: 0;
  font-style: normal;
  font-size: 9px;
  font-weight: 700;
  padding: 1px 6px;
  border-radius: 6px;
  background: #fdf3e3;
  border: 1px solid #f0d9b5;
  color: #a06a1a;
  white-space: nowrap;
}
.dimcfg-ops { display: flex; justify-content: flex-end; gap: 6px; }
.dimcfg-delete,
.dimcfg-pick {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: 1px solid #d9e3ef;
  border-radius: 8px;
  background: #fff;
  padding: 4px 9px;
  font-size: 10px;
  font-weight: 700;
  line-height: 1.2;
  white-space: nowrap;
}
.dimcfg-delete { color: #c25454; }
.dimcfg-pick { color: #2e7cd6; }
.dimcfg-delete svg,
.dimcfg-pick svg { width: 10px; height: 10px; }
.dimcfg-delete:hover { background: #fdf4f4; }
.dimcfg-pick:hover { background: #eef5ff; }
.dimcfg-add {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  width: 100%;
  border: 1px solid #2e7cd6;
  border-radius: 12px;
  background: #fff;
  color: #2e7cd6;
  padding: 9px 0;
  font-size: 11px;
  font-weight: 700;
  flex-shrink: 0;
}
.dimcfg-add svg { width: 12px; height: 12px; }
.dimcfg-add:hover { background: #eef5ff; }
.dimcfg-add:disabled {
  background: #f2f6fa;
  border-color: #d9e3ef;
  color: #8a99ac;
  cursor: not-allowed;
}
.dimcfg-hint {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin: 0;
  color: #66788f;
  font-size: 11px;
  line-height: 1.45;
}
.dimcfg-hint svg {
  width: 12px;
  height: 12px;
  margin-top: 2px;
  color: #2e7cd6;
  flex-shrink: 0;
}
.dimcfg-hint.alert { color: #c25454; }
.dimcfg-hint.alert svg { color: #c25454; }
.dimcfg-plus {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  border: 1px solid #c5d7ee;
  border-radius: 8px;
  background: #fff;
  color: #2e7cd6;
  padding: 3px 8px;
  font-size: 10px;
  font-weight: 700;
  white-space: nowrap;
}
.dimcfg-plus svg { width: 10px; height: 10px; }
.dimcfg-plus.on { background: #2e7cd6; border-color: #2e7cd6; color: #fff; }
.dimcfg-plus:hover { background: #eef5ff; }
.dimcfg-plus.on:hover { background: #2468c0; }
.dimcfg-finder { position: relative; border-top: 1px solid #d9e3ef; background: #f7fafd; padding: 8px; }
.dimcfg-finder-input {
  display: flex;
  align-items: center;
  gap: 6px;
  border: 1px solid #2e7cd6;
  border-radius: 10px;
  background: #fff;
  padding: 0 8px;
}
.dimcfg-finder-input svg { width: 14px; height: 14px; color: #66788f; flex-shrink: 0; }
.dimcfg-finder-input input {
  flex: 1;
  min-width: 0;
  border: 0;
  background: transparent;
  padding: 8px 0;
  font: inherit;
  font-size: 12px;
  outline: none;
}
.dimcfg-finder-input button {
  border: 0;
  background: transparent;
  color: #66788f;
  font-size: 16px;
  line-height: 1;
  padding: 0 2px;
}
.dimcfg-menu {
  position: absolute;
  z-index: 3;
  left: 8px;
  right: 8px;
  top: calc(100% + 4px);
  margin: 0;
  padding: 4px;
  list-style: none;
  background: #fff;
  border: 1px solid #d9e3ef;
  border-radius: 10px;
  box-shadow: 0 10px 24px #17293f1f;
  max-height: 220px;
  overflow: auto;
}
.dimcfg-menu button {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  border: 0;
  background: transparent;
  border-radius: 8px;
  padding: 7px 8px;
  text-align: left;
}
.dimcfg-menu button:hover { background: #eef5ff; }
.dimcfg-menu span {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
  font-weight: 700;
}
.dimcfg-menu .dimcfg-type-tag { flex-shrink: 0; }
.dimcfg-menu svg { width: 12px; height: 12px; color: #2e7cd6; }
.dimcfg-finder-empty { margin: 6px 4px 2px; color: #66788f; font-size: 11px; }
.dimcfg-add.on { background: #eef5ff; }
.dimcfg-col-row {
  align-items: start;
  border-top: 1px solid #d9e3ef;
  min-height: 36px;
  padding-top: 10px;
  padding-bottom: 10px;
}
.dimcfg-col-row .dimcfg-plus { margin-top: 1px; }
.dimcfg-col-row strong {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
}
.dimcfg-type {
  min-width: 0;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 10px;
  color: #66788f;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.dimcfg-comment {
  min-width: 0;
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dimcfg-sample {
  min-width: 0;
  font-size: 10px;
  line-height: 1.45;
  color: #66788f;
  white-space: pre-wrap;
  word-break: break-word;
}
.dimcfg-col-row .dimcfg-plus { justify-self: end; }
.dimcfg-radio {
  width: 16px;
  height: 16px;
  border: 1px solid #d9e3ef;
  border-radius: 8px;
  background: #fff;
  padding: 0;
  display: grid;
  place-items: center;
}
.dimcfg-radio.on {
  background: #2e7cd6;
  border-color: #2e7cd6;
}
.dimcfg-radio i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #fff;
}
.dimcfg-radio:focus-visible,
.dimcfg-pick:focus-visible,
.dimcfg-delete:focus-visible,
.dimcfg-add:focus-visible,
.dimcfg-save:focus-visible,
.dimcfg-cancel:focus-visible,
.dimcfg-x:focus-visible {
  outline: 2px solid #2e7cd6;
  outline-offset: 2px;
}
.dimcfg-placeholder {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  color: #66788f;
  padding: 24px;
}
.dimcfg-placeholder-mark {
  width: 42px;
  height: 42px;
  border-radius: 50%;
  background: #eef3f9;
  color: #8aa0b8;
  display: grid;
  place-items: center;
  margin-bottom: 10px;
}
.dimcfg-placeholder-mark svg { width: 18px; height: 18px; }
.dimcfg-placeholder p { margin: 0 0 6px; font-size: 13px; font-weight: 700; color: #17293f; }
.dimcfg-placeholder small { font-size: 11px; }
.dimcfg-empty { margin: 0; padding: 16px; text-align: center; color: #66788f; font-size: 12px; }
.dimcfg-status { margin: 18px 28px; color: #66788f; font-size: 13px; }
.dimcfg-status.alert { color: #c25454; }
.dimcfg-foot {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 24px;
  border-top: 1px solid #d9e3ef;
  flex-shrink: 0;
}
.dimcfg-path {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 10px;
  color: #66788f;
}
.dimcfg-save-note { margin-left: auto; color: #66788f; font-size: 10px; }
.dimcfg-save-error { margin-left: auto; color: #c25454; font-size: 11px; }
.dimcfg-cancel,
.dimcfg-save {
  border-radius: 10px;
  padding: 9px 20px;
  font-size: 12px;
  font-weight: 700;
}
.dimcfg-cancel {
  border: 1px solid #d9e3ef;
  background: #fff;
  color: #66788f;
}
.dimcfg-save {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 0;
  background: #2e7cd6;
  color: #fff;
}
.dimcfg-save svg { width: 12px; height: 12px; }
.dimcfg-save:disabled { background: #d9e3ef; color: #8a99ac; }
.dimcfg-sub-backdrop {
  position: absolute;
  inset: 0;
  background: rgba(23, 41, 63, 0.35);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 2;
}
.dimcfg { position: relative; }
.dimcfg-sub-panel {
  width: min(520px, calc(100% - 48px));
  max-height: calc(100% - 48px);
  overflow: auto;
  background: #fff;
  border: 1px solid #d9e3ef;
  border-radius: 14px;
  padding: 18px 20px;
}
.dimcfg-sub-title { margin: 0 0 12px; font-size: 15px; font-weight: 700; }
.dimcfg-search { display: block; margin-bottom: 10px; }
.dimcfg-search span { display: block; margin-bottom: 6px; font-size: 12px; font-weight: 700; }
.dimcfg-search input {
  width: 100%;
  border: 1px solid #d9e3ef;
  border-radius: 10px;
  background: #f7fafd;
  padding: 8px 10px;
  font: inherit;
  font-size: 13px;
}
.dimcfg-object-list { max-height: 280px; overflow: auto; }
.dimcfg-object-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 2px;
  border-bottom: 1px solid #eef3f9;
  font-size: 13px;
}
.dimcfg-object-row span:first-child {
  flex: 1;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 12px;
  font-weight: 700;
}
.dimcfg-object-row em { font-style: normal; font-size: 11px; font-weight: 700; color: #3b82c4; }
.dimcfg-object-row em.view { color: #0e9f8a; }
.dimcfg-object-row em.mv { color: #7a6fd0; }
.dimcfg-object-row.muted { color: #9aa5b1; }
.dimcfg-added { color: #66788f; font-size: 11px; font-weight: 700; }
.dimcfg-sub-actions { display: flex; justify-content: flex-end; margin-top: 12px; }
@media (max-width: 1040px) {
  .dimcfg-backdrop { padding: 12px; }
  .dimcfg-body { grid-template-columns: 1fr; overflow: auto; }
  .dimcfg-side { border-right: 0; border-bottom: 1px solid #d9e3ef; }
  .dimcfg { height: calc(100vh - 24px); width: calc(100vw - 24px); }
}
</style>
