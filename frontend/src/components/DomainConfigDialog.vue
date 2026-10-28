<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'

import { getDomainConfig, getDomainTables, putDomainConfig } from '../api/workspaces'
import { ApiError } from '../api/http'
import type { DomainConfigDomain, DomainConfigTableOption } from '../api/types'

const props = defineProps<{
  workspaceId: string
}>()

const emit = defineEmits<{
  close: []
}>()

const loading = ref(true)
const fatal = ref('')
const saveError = ref('')
const saving = ref(false)
const fingerprint = ref('')
const baseline = ref('')
const dbName = ref('')
const dbDescription = ref<string | null>(null)
const maxDomains = ref<number | null>(null)
const domains = ref<DomainConfigDomain[]>([])
const candidates = ref<DomainConfigTableOption[]>([])
const openPicker = ref<number | null>(null)
const pickerQuery = ref('')
const editing = ref<{ index: number; field: 'name' | 'description' } | null>(null)

const candidateById = computed(() => {
  const map = new Map<string, string>()
  for (const item of candidates.value) map.set(item.id, item.label)
  return map
})

const membershipCount = computed(() => {
  const counts = new Map<string, number>()
  for (const domain of domains.value) {
    for (const table of domain.tables) counts.set(table, (counts.get(table) ?? 0) + 1)
  }
  return counts
})

function domainSnapshot(rows: DomainConfigDomain[]) {
  return JSON.stringify(
    rows.map((item) => ({
      name: item.name.trim(),
      description: item.description.trim(),
      tables: [...item.tables],
    })),
  )
}

const dirty = computed(() => domainSnapshot(domains.value) !== baseline.value)

function tableLabel(id: string) {
  return candidateById.value.get(id) ?? id
}

function overLimit(id: string) {
  if (maxDomains.value == null) return false
  return (membershipCount.value.get(id) ?? 0) > maxDomains.value
}

function pickerOptions(index: number) {
  const selected = new Set(domains.value[index]?.tables ?? [])
  const keyword = pickerQuery.value.trim().toLowerCase()
  return candidates.value.filter((item) => {
    if (selected.has(item.id)) return false
    return !keyword || item.label.toLowerCase().includes(keyword)
  })
}

function reasonOf(error: unknown) {
  if (error instanceof ApiError) {
    const details = error.details
    const reason =
      typeof details === 'object' &&
      details !== null &&
      !Array.isArray(details) &&
      'reason' in details
        ? (details as { reason?: unknown }).reason
        : ''
    if (typeof reason === 'string' && reason) return reason
    return error.message
  }
  return '请求失败，请稍后重试。'
}

async function load() {
  loading.value = true
  fatal.value = ''
  saveError.value = ''
  try {
    const [config, tables] = await Promise.all([
      getDomainConfig(props.workspaceId),
      getDomainTables(props.workspaceId),
    ])
    fingerprint.value = config.fingerprint
    dbName.value = config.database.name
    dbDescription.value = config.database.description
    maxDomains.value = config.max_domains_per_table
    domains.value = config.domains.map((item) => ({
      name: item.name,
      description: item.description,
      tables: [...item.tables],
    }))
    baseline.value = domainSnapshot(domains.value)
    candidates.value = tables.tables
  } catch (error) {
    fatal.value = reasonOf(error)
    domains.value = []
  } finally {
    loading.value = false
  }
}

function isEditing(index: number, field: 'name' | 'description') {
  return editing.value?.index === index && editing.value.field === field
}

function startEdit(index: number, field: 'name' | 'description') {
  editing.value = { index, field }
  void nextTick(() => {
    document
      .querySelector<HTMLElement>('.domain-cfg-grid > input, .domain-cfg-grid > textarea')
      ?.focus()
  })
}

function stopEdit() {
  editing.value = null
}

function cancelEdit(event: KeyboardEvent) {
  event.stopPropagation()
  stopEdit()
}

function addDomain() {
  domains.value.push({ name: '', description: '', tables: [] })
  startEdit(domains.value.length - 1, 'name')
}

function removeDomain(index: number) {
  domains.value.splice(index, 1)
  if (openPicker.value === index) openPicker.value = null
  if (editing.value?.index === index) editing.value = null
}

function removeTable(index: number, table: string) {
  const row = domains.value[index]
  if (!row) return
  row.tables = row.tables.filter((item) => item !== table)
}

function addTable(index: number, id: string) {
  const row = domains.value[index]
  if (!row || row.tables.includes(id)) return
  row.tables.push(id)
  pickerQuery.value = ''
}

function togglePicker(index: number) {
  if (openPicker.value === index) {
    openPicker.value = null
    pickerQuery.value = ''
    return
  }
  openPicker.value = index
  pickerQuery.value = ''
}

function validateLocal(): string {
  const names = new Set<string>()
  for (const domain of domains.value) {
    const name = domain.name.trim()
    const description = domain.description.trim()
    if (!name || !description) return 'domain 名称和描述都不能为空。'
    if (names.has(name)) return `domain 名称重复：${name}`
    names.add(name)
    if (new Set(domain.tables).size !== domain.tables.length) {
      return `${name} 内有重复的表。`
    }
  }
  return ''
}

async function save() {
  if (!dirty.value || saving.value) return
  saveError.value = ''
  const local = validateLocal()
  if (local) {
    saveError.value = local
    return
  }
  const noTables = domains.value.every((domain) => domain.tables.length === 0)
  if (domains.value.length === 0) {
    const ok = window.confirm(
      '保存后的文件没有任何表归属，发布 domain 时会失败。之后重跑或发布 rel 会因 domain 列表为空失败。',
    )
    if (!ok) return
  } else if (noTables) {
    const ok = window.confirm('保存后的文件没有任何表归属，发布 domain 时会失败。')
    if (!ok) return
  }
  saving.value = true
  try {
    await putDomainConfig(props.workspaceId, {
      fingerprint: fingerprint.value,
      database: { name: dbName.value, description: dbDescription.value },
      max_domains_per_table: maxDomains.value,
      domains: domains.value.map((domain) => ({
        name: domain.name,
        description: domain.description,
        tables: [...domain.tables],
      })),
    })
    emit('close')
  } catch (error) {
    if (error instanceof ApiError && error.code === 'domain_config_regenerated') {
      fatal.value = reasonOf(error)
      domains.value = []
      return
    }
    saveError.value = reasonOf(error)
  } finally {
    saving.value = false
  }
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && !saving.value) emit('close')
}

onMounted(() => {
  void load()
  window.addEventListener('keydown', onKeydown)
})
onUnmounted(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <div class="dialog-backdrop" @click.self="saving ? undefined : emit('close')">
    <div class="dialog-panel domain-cfg" role="dialog" aria-modal="true" aria-labelledby="domain-cfg-title">
      <header class="domain-cfg-head">
        <div>
          <p class="domain-cfg-kicker">DOMAIN CONFIG</p>
          <h2 id="domain-cfg-title">业务主题配置</h2>
          <span class="domain-cfg-file">db_domains.yaml</span>
        </div>
        <button type="button" class="domain-cfg-x" aria-label="关闭" :disabled="saving" @click="emit('close')">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg>
        </button>
      </header>

      <p v-if="loading" class="domain-cfg-status">正在读取配置…</p>
      <p v-else-if="fatal" class="domain-cfg-status alert" role="alert">{{ fatal }}</p>

      <template v-else>
        <div class="domain-cfg-body">
        <section class="domain-cfg-db">
          <h3>{{ dbName }}</h3>
          <p>{{ dbDescription || '采集时未写入库描述' }}</p>
        </section>
        <p class="domain-cfg-hint">
          一张表可以属于多个 domain。<template v-if="maxDomains != null">采集时建议每表不超过 {{ maxDomains }} 个主题，保存不强制。</template>
        </p>

        <div class="domain-cfg-card">
          <div class="domain-cfg-scroll">
            <div class="domain-cfg-grid head">
              <span>domain 名称</span>
              <span>domain 描述</span>
              <span>归属的表</span>
              <span>操作</span>
            </div>
            <div v-for="(domain, index) in domains" :key="index" class="domain-cfg-grid">
              <input
                v-if="isEditing(index, 'name')"
                v-model="domain.name"
                type="text"
                aria-label="domain 名称"
                @blur="stopEdit"
                @keydown.enter.prevent="stopEdit"
                @keydown.esc="cancelEdit"
              />
              <p
                v-else
                class="domain-cfg-text"
                :class="{ empty: !domain.name.trim() }"
                @dblclick="startEdit(index, 'name')"
              >{{ domain.name.trim() || '双击填写名称' }}</p>
              <textarea
                v-if="isEditing(index, 'description')"
                v-model="domain.description"
                rows="3"
                aria-label="domain 描述"
                @blur="stopEdit"
                @keydown.esc="cancelEdit"
              ></textarea>
              <p
                v-else
                class="domain-cfg-text desc"
                :class="{ empty: !domain.description.trim() }"
                @dblclick="startEdit(index, 'description')"
              >{{ domain.description.trim() || '双击填写描述' }}</p>
              <div class="domain-cfg-tables">
                <span v-for="table in domain.tables" :key="table" class="domain-cfg-chip" :class="{ warn: overLimit(table) }">
                  {{ tableLabel(table) }}
                  <button type="button" :aria-label="`移除 ${tableLabel(table)}`" @click="removeTable(index, table)">×</button>
                  <small v-if="overLimit(table)">超过建议主题数</small>
                </span>
                <div class="domain-cfg-picker">
                  <button type="button" class="domain-cfg-plus" @click="togglePicker(index)">
                    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
                    添加表
                  </button>
                  <div v-if="openPicker === index" class="domain-cfg-menu">
                    <input v-model="pickerQuery" type="search" placeholder="搜索 schema.表名" aria-label="搜索表" />
                    <ul>
                      <li v-for="option in pickerOptions(index)" :key="option.id">
                        <button type="button" @click="addTable(index, option.id)">{{ option.label }}</button>
                      </li>
                      <li v-if="pickerOptions(index).length === 0" class="empty">没有可添加的表</li>
                    </ul>
                  </div>
                </div>
              </div>
              <button type="button" class="domain-cfg-delete" @click="removeDomain(index)">
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M4 7h16" />
                  <path d="M9 7V5h6v2" />
                  <path d="M7 7l1 13h8l1-13" />
                </svg>
                删除
              </button>
            </div>
          </div>
        </div>

        <button type="button" class="domain-cfg-add" @click="addDomain">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
          新增 domain
        </button>
        </div>
        <footer class="domain-cfg-foot">
          <span v-if="saveError" class="domain-cfg-save-error" role="alert">{{ saveError }}</span>
          <span v-else class="domain-cfg-save-note">{{ dirty ? '保存后写回 db_domains.yaml' : '没有修改' }}</span>
          <button type="button" class="domain-cfg-cancel" :disabled="saving" @click="emit('close')">取消</button>
          <button type="button" class="domain-cfg-save" :disabled="saving || !dirty" @click="save">{{ saving ? '保存中' : '保存' }}</button>
        </footer>
      </template>
    </div>
  </div>
</template>

<style scoped>
.domain-cfg {
  width: min(1160px, calc(100vw - 48px));
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
  font-size: 12px;
}
.domain-cfg-head {
  display: flex;
  align-items: flex-start;
  gap: 16px;
  padding: 20px 28px;
  border-bottom: 1px solid #d9e3ef;
  flex-shrink: 0;
}
.domain-cfg-head > div { min-width: 0; flex: 1; }
.domain-cfg-kicker {
  margin: 0;
  color: #2e7cd6;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 2.5px;
}
.domain-cfg h2 {
  margin: 5px 0 0;
  font-size: 20px;
  font-weight: 700;
}
.domain-cfg-file {
  display: inline-block;
  margin: 8px 0 0;
  background: #eef3f9;
  color: #2e7cd6;
  border-radius: 7px;
  padding: 3px 8px;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 10px;
  font-weight: 700;
}
.domain-cfg-x {
  width: 30px;
  height: 30px;
  border: 1px solid #d9e3ef;
  border-radius: 9px;
  background: #fff;
  color: #66788f;
  display: grid;
  place-items: center;
  padding: 0;
  flex-shrink: 0;
}
.domain-cfg-x svg,
.domain-cfg-delete svg,
.domain-cfg-plus svg,
.domain-cfg-add svg {
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.domain-cfg-x svg { width: 14px; height: 14px; }
.domain-cfg-x:hover { background: #eef3f9; }
.domain-cfg-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 16px 24px 12px;
  gap: 10px;
}
.domain-cfg-db,
.domain-cfg-hint,
.domain-cfg-card,
.domain-cfg-add {
  margin: 0;
}
.domain-cfg-db {
  padding: 10px 12px;
  background: #f7fafd;
  border: 1px solid #d9e3ef;
  border-radius: 12px;
}
.domain-cfg-db h3 {
  margin: 0;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 12px;
}
.domain-cfg-db p,
.domain-cfg-hint {
  margin: 0;
  color: #66788f;
  font-size: 11px;
  line-height: 1.5;
}
.domain-cfg-db p { margin-top: 4px; }
.domain-cfg-card {
  border: 1px solid #d9e3ef;
  border-radius: 12px;
  overflow: visible;
  min-height: 0;
  flex: 1;
  display: flex;
  flex-direction: column;
  background: #fff;
}
.domain-cfg-scroll { overflow: auto; min-height: 0; flex: 1; border-radius: 12px; }
.domain-cfg-grid {
  display: grid;
  grid-template-columns: 152px minmax(240px, 400px) minmax(200px, 260px) 72px;
  column-gap: 12px;
  align-items: start;
  padding: 8px 12px;
}
.domain-cfg-grid.head {
  position: sticky;
  top: 0;
  z-index: 2;
  align-items: center;
  background: #eef3f9;
  color: #66788f;
  font-size: 10px;
  font-weight: 700;
}
.domain-cfg-grid.head > span:last-child { text-align: right; }
.domain-cfg-grid:not(.head) { border-top: 1px solid #d9e3ef; }
.domain-cfg-text {
  margin: 0;
  min-height: 28px;
  padding: 5px 2px;
  font-size: 12px;
  line-height: 1.45;
  cursor: text;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.domain-cfg-text.desc {
  white-space: pre-wrap;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
}
.domain-cfg-text.empty { color: #8a99ac; }
.domain-cfg-text:hover { background: #f7fafd; border-radius: 8px; }
.domain-cfg-grid input,
.domain-cfg-grid textarea {
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
  border: 1px solid #d9e3ef;
  border-radius: 8px;
  background: #fff;
  color: #17293f;
  font: inherit;
  font-size: 12px;
  line-height: 1.4;
  padding: 5px 8px;
  outline: none;
}
.domain-cfg-grid input:focus,
.domain-cfg-grid textarea:focus,
.domain-cfg-menu input:focus {
  border-color: #2e7cd6;
  box-shadow: 0 0 0 3px #dbe9fa;
}
.domain-cfg-grid textarea { resize: vertical; min-height: 44px; }
.domain-cfg-tables {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.domain-cfg-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: #eef3f9;
  border-radius: 8px;
  padding: 2px 6px;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
  font-weight: 700;
  color: #2e7cd6;
}
.domain-cfg-chip.warn { background: #fff4e5; color: #9a6b12; }
.domain-cfg-chip small { font-family: "Noto Sans SC", sans-serif; font-size: 9px; font-weight: 700; }
.domain-cfg-chip button {
  border: 0;
  background: transparent;
  color: #66788f;
  padding: 0 2px;
  font-size: 12px;
  line-height: 1;
}
.domain-cfg-delete,
.domain-cfg-plus {
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
.domain-cfg-delete { color: #c25454; justify-self: end; }
.domain-cfg-plus { color: #2e7cd6; border-color: #c5d7ee; }
.domain-cfg-delete svg,
.domain-cfg-plus svg,
.domain-cfg-add svg { width: 10px; height: 10px; }
.domain-cfg-delete:hover { background: #fdf4f4; }
.domain-cfg-plus:hover { background: #eef5ff; }
.domain-cfg-picker { position: relative; }
.domain-cfg-menu {
  position: absolute;
  z-index: 3;
  top: 28px;
  left: 0;
  width: 220px;
  background: #fff;
  border: 1px solid #d9e3ef;
  border-radius: 10px;
  padding: 8px;
  box-shadow: 0 8px 24px rgba(22, 38, 61, 0.12);
}
.domain-cfg-menu input {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid #2e7cd6;
  border-radius: 8px;
  padding: 6px 8px;
  font: inherit;
  font-size: 12px;
}
.domain-cfg-menu ul {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
  max-height: 160px;
  overflow: auto;
}
.domain-cfg-menu li button {
  width: 100%;
  text-align: left;
  border: 0;
  background: transparent;
  padding: 5px 4px;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
  color: #17293f;
}
.domain-cfg-menu li button:hover { background: #eef5ff; }
.domain-cfg-menu .empty { color: #8a99ac; font-size: 11px; padding: 6px 4px; }
.domain-cfg-add {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  border: 1px solid #2e7cd6;
  border-radius: 12px;
  background: #fff;
  color: #2e7cd6;
  padding: 9px 0;
  font-size: 11px;
  font-weight: 700;
  flex-shrink: 0;
}
.domain-cfg-add svg { width: 12px; height: 12px; }
.domain-cfg-add:hover { background: #eef5ff; }
.domain-cfg-foot {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 24px;
  border-top: 1px solid #d9e3ef;
  flex-shrink: 0;
}
.domain-cfg-save-note { margin-left: auto; color: #66788f; font-size: 10px; }
.domain-cfg-save-error { margin-left: auto; color: #c25454; font-size: 11px; }
.domain-cfg-cancel,
.domain-cfg-save {
  border-radius: 10px;
  padding: 9px 20px;
  font-size: 12px;
  font-weight: 700;
}
.domain-cfg-cancel {
  border: 1px solid #d9e3ef;
  background: #fff;
  color: #66788f;
}
.domain-cfg-save {
  border: 1px solid #2e7cd6;
  background: #2e7cd6;
  color: #fff;
}
.domain-cfg-save:disabled,
.domain-cfg-cancel:disabled { opacity: 0.6; }
.domain-cfg-status { margin: 18px 28px; color: #66788f; font-size: 13px; }
.domain-cfg-status.alert { color: #c25454; }
</style>
