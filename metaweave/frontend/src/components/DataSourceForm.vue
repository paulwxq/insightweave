<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { checkConnection, checkSavedConnection, createDataSource, updateDataSource } from '../api/dataSources'
import { ApiError, type FormMode } from '../api/http'
import type {
  ConnectionCheckResponse,
  ConnectionFields,
  DataSourceListItem,
  DuplicateSourceDetail,
} from '../api/types'
import SchemaSelector from './SchemaSelector.vue'

const props = withDefaults(
  defineProps<{ mode?: FormMode; sourceId?: string; initial?: DataSourceListItem | null }>(),
  { mode: 'create', sourceId: '', initial: null },
)
const emit = defineEmits<{ saved: [item: DataSourceListItem] }>()

const form = reactive({
  display_name: '',
  host: '',
  port: 5432,
  db_name: '',
  username: '',
  password: '',
  sslmode: '',
  description: '',
})
const showPassword = ref(false)
const checking = ref(false)
const saving = ref(false)
const checked = ref<ConnectionCheckResponse | null>(null)
const selectedSchemas = ref<string[]>([])
const originalSchemas = ref<string[]>([])
const savedTarget = ref<{
  host: string
  port: number
  db_name: string
  username: string
  sslmode: string | null
} | null>(null)
const checkMessage = ref('')
const saveMessage = ref('')
const duplicateDetails = ref<DuplicateSourceDetail[]>([])
let revision = 0
let hydrating = false

const portValid = computed(
  () => Number.isInteger(Number(form.port)) && Number(form.port) >= 1 && Number(form.port) <= 65535,
)
const fieldsReady = computed(
  () => Boolean(form.host.trim() && form.db_name.trim() && form.username.trim()) && portValid.value,
)
const targetUnchanged = computed(() => {
  const saved = savedTarget.value
  if (!saved) return false
  return (
    form.host === saved.host &&
    Number(form.port) === saved.port &&
    form.db_name === saved.db_name &&
    form.username === saved.username &&
    (form.sslmode || null) === saved.sslmode
  )
})
const passwordOptional = computed(() => props.mode === 'edit' && targetUnchanged.value)
const connectionReady = computed(() => fieldsReady.value && (passwordOptional.value || Boolean(form.password)))
const canSave = computed(
  () =>
    Boolean(form.display_name.trim()) &&
    Boolean(checked.value) &&
    selectedSchemas.value.length > 0 &&
    form.description.trim().length <= 1000 &&
    connectionReady.value &&
    !saving.value &&
    !checking.value,
)

function applyInitial(item: DataSourceListItem) {
  hydrating = true
  originalSchemas.value = [...item.selected_schemas]
  savedTarget.value = {
    host: item.host,
    port: item.port,
    db_name: item.db_name,
    username: item.username,
    sslmode: item.sslmode,
  }
  form.display_name = item.display_name
  form.host = item.host
  form.port = item.port
  form.db_name = item.db_name
  form.username = item.username
  form.password = ''
  form.sslmode = item.sslmode ?? ''
  form.description = item.description ?? ''
  checked.value = null
  selectedSchemas.value = []
  checkMessage.value = ''
  saveMessage.value = ''
  duplicateDetails.value = []
  hydrating = false
}

watch(
  () => props.initial,
  (item) => {
    if (item) applyInitial(item)
  },
  { immediate: true },
)

watch(
  () => [form.host, form.port, form.db_name, form.username, form.password, form.sslmode],
  () => {
    if (hydrating) return
    revision += 1
    checked.value = null
    selectedSchemas.value = []
    checkMessage.value = ''
    saveMessage.value = ''
    duplicateDetails.value = []
  },
  { flush: 'sync' },
)

watch(
  () => [form.display_name, form.description, selectedSchemas.value],
  () => {
    duplicateDetails.value = []
    saveMessage.value = ''
  },
)

function connectionFields(): ConnectionFields {
  return {
    db_type: 'postgresql',
    host: form.host,
    port: Number(form.port),
    db_name: form.db_name,
    username: form.username,
    password: form.password,
    sslmode: form.sslmode || null,
  }
}

function shownError(error: unknown, fallback: string): string {
  if (props.mode === 'edit' && error instanceof ApiError && error.code === 'connection_failed') {
    return `${error.message}若源库密码已变更，请在密码框填写新密码后再测试`
  }
  return error instanceof Error ? error.message : fallback
}

async function runCheck() {
  if (!fieldsReady.value || checking.value || saving.value) return
  if (!passwordOptional.value && !form.password) {
    checkMessage.value = '连接信息已修改，请填写密码后重试'
    return
  }
  const requestRevision = revision
  checking.value = true
  checkMessage.value = ''
  try {
    const result =
      props.mode === 'edit' && passwordOptional.value && !form.password
        ? await checkSavedConnection(props.sourceId)
        : await checkConnection(connectionFields())
    if (requestRevision !== revision) return
    checked.value = result
    selectedSchemas.value =
      props.mode === 'edit'
        ? originalSchemas.value.filter((name) => result.schemas.includes(name))
        : []
    checkMessage.value = result.schemas.length
      ? `连接成功 · PostgreSQL ${result.server_version} · 发现 ${result.schemas.length} 个 Schema`
      : `连接成功 · PostgreSQL ${result.server_version} · 没有可选的业务 Schema`
  } catch (error) {
    if (requestRevision !== revision) return
    checked.value = null
    selectedSchemas.value = []
    checkMessage.value = shownError(error, '连接检查失败。')
  } finally {
    checking.value = false
  }
}

async function save(confirmDuplicate = false) {
  if (!canSave.value) return
  saving.value = true
  saveMessage.value = ''
  duplicateDetails.value = []
  try {
    const shared = {
      ...connectionFields(),
      display_name: form.display_name,
      selected_schemas: selectedSchemas.value,
      description: form.description.trim() || null,
      confirm_duplicate: confirmDuplicate,
    }
    const item =
      props.mode === 'edit'
        ? await updateDataSource(props.sourceId, {
            db_type: 'postgresql',
            host: shared.host,
            port: shared.port,
            db_name: shared.db_name,
            username: shared.username,
            ...(form.password ? { password: form.password } : {}),
            sslmode: shared.sslmode,
            display_name: shared.display_name,
            selected_schemas: shared.selected_schemas,
            description: shared.description,
            confirm_duplicate: confirmDuplicate,
          })
        : await createDataSource(shared)
    emit('saved', item)
  } catch (error) {
    if (error instanceof ApiError && error.code === 'duplicate_source') {
      // duplicate_source 的 details 恒为数组形态（doc 22.2 放宽了 ApiError.details 类型）
      duplicateDetails.value = Array.isArray(error.details) ? error.details : []
    }
    saveMessage.value = shownError(error, props.mode === 'edit' ? '保存失败。' : '创建失败。')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <form class="source-form" :class="{ 'edit-source-form': mode === 'edit' }" @submit.prevent="save(false)">
    <section v-if="mode === 'edit'" class="ds-panel ds-panel-row">
      <div class="ds-panel-head">
        <span class="ds-step">1</span>
        <div>
          <h2>数据库类型</h2>
          <p>登记后不可修改</p>
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
    <section v-else class="form-card">
      <div class="section-heading"><span class="step-badge">1</span><div><h2>选择数据库类型</h2><p>本阶段支持 PostgreSQL，更多类型后续开放</p></div></div>
      <div class="type-grid">
        <div class="type-tile chosen"><span class="type-icon">▤</span><strong>PostgreSQL</strong><small>关系型数据库</small><span class="type-check">✓</span></div>
        <div v-for="name in ['MySQL', 'SQLite', 'SQL Server', 'Oracle']" :key="name" class="type-tile disabled" aria-disabled="true"><span class="type-icon">▤</span><strong>{{ name }}</strong><small>待开放</small></div>
      </div>
    </section>

    <section class="form-card">
      <div class="section-heading"><span class="step-badge">2</span><div><h2>填写连接信息</h2><p>{{ mode === 'edit' ? '带 * 为必填项，密码仅加密存储' : '带 * 为必填项，密码仅用于连接验证与加密登记' }}</p></div></div>
      <fieldset class="field-grid" :disabled="saving">
        <label class="field full"><span>数据源显示名称 *</span><input v-model="form.display_name" required placeholder="例如：订单业务库" autocomplete="off" /></label>
        <label class="field"><span>Host *</span><input v-model="form.host" required placeholder="数据库主机名或 IP" autocomplete="off" /></label>
        <label class="field"><span>Port *</span><input v-model.number="form.port" type="number" min="1" max="65535" required /></label>
        <label class="field"><span>Database *</span><input v-model="form.db_name" required placeholder="数据库名称" autocomplete="off" /></label>
        <label class="field"><span>User *</span><input v-model="form.username" required placeholder="数据库用户名" autocomplete="off" /></label>
        <label class="field"><span>Password *</span><span class="password-wrap"><input v-model="form.password" :type="showPassword ? 'text' : 'password'" :required="mode === 'create'" autocomplete="new-password" :placeholder="mode === 'edit' ? '不修改请留空' : '数据库密码'" /><button type="button" class="password-toggle" :aria-label="showPassword ? '隐藏密码' : '显示密码'" @click="showPassword = !showPassword"><svg v-if="mode === 'edit'" viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z" /><circle cx="12" cy="12" r="3" /></svg><template v-else>{{ showPassword ? '隐藏' : '显示' }}</template></button></span></label>
        <label class="field"><span>SSL Mode</span><select v-model="form.sslmode"><option value="">未指定</option><option v-for="mode in ['disable', 'allow', 'prefer', 'require', 'verify-ca', 'verify-full']" :key="mode" :value="mode">{{ mode }}</option></select></label>
      </fieldset>
      <div class="connection-actions"><button type="button" class="button button-primary connect-button" :disabled="!fieldsReady || checking || saving" @click="runCheck"><svg v-if="mode === 'edit'" viewBox="0 0 24 24" aria-hidden="true"><path d="m13 2-2 2.5h3L12 7" /><path d="M10 14v-3" /><path d="M14 14v-3" /><path d="M11 19c-1.7 0-3-1.3-3-3v-3h8v3c0 1.7-1.3 3-3 3Z" /><path d="M12 22v-3" /></svg>{{ checking ? '连接中…' : (mode === 'edit' ? '连接' : '⌁  测试连接') }}</button><span v-if="checkMessage" class="status-message" :class="{ success: checked }" role="status">{{ checkMessage }}</span></div>
    </section>

    <section class="form-card">
      <div class="section-heading"><span class="step-badge">3</span><div><h2>{{ mode === 'edit' ? '勾选需要注册的 Schema' : '勾选需要登记的 Schema' }}</h2><p>{{ mode === 'edit' ? '连接成功后自动获取，可多选' : '测试连接成功后自动获取，可多选' }}</p></div></div>
      <div v-if="!checked" class="schema-await">请先填写连接信息并测试连接。</div>
      <template v-else><p class="field-caption">Schema * <span v-if="selectedSchemas.length && mode !== 'edit'">· 已选 {{ selectedSchemas.length }} 项</span></p><SchemaSelector v-model="selectedSchemas" :schemas="checked.schemas" :disabled="saving" :show-chips="mode === 'edit'" /></template>
    </section>

    <section class="form-card">
      <div class="section-heading"><span class="step-badge">4</span><div><h2>{{ mode === 'edit' ? '数据源说明(可选)' : '数据源说明（可选）' }}</h2><p>帮助团队成员理解这个数据源的内容与用途</p></div></div>
      <label class="field"><span class="sr-only">数据源说明</span><textarea v-model="form.description" maxlength="1000" rows="4" :disabled="saving" placeholder="例如：订单、支付与评价业务数据…" /></label>
      <small class="character-count">{{ form.description.trim().length }} / 1000</small>
    </section>

    <div v-if="saveMessage" class="form-feedback" role="alert">
      <p>{{ saveMessage }}</p>
      <ul v-if="duplicateDetails.length"><li v-for="item in duplicateDetails" :key="item.source_id">{{ item.display_name }} · {{ item.source_id }}</li></ul>
      <button v-if="duplicateDetails.length" type="button" class="button button-outline" :disabled="saving" @click="save(true)">{{ mode === 'edit' ? '确认仍保存到当前数据源' : '确认仍创建独立数据源' }}</button>
    </div>
    <div class="form-actions"><RouterLink to="/data-sources" class="button button-outline">取消</RouterLink><button type="submit" class="button button-primary" :disabled="!canSave"><svg v-if="mode === 'edit' && !saving" viewBox="0 0 24 24" aria-hidden="true"><ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M3 5v14a9 3 0 0 0 18 0V5" /><path d="M3 12a9 3 0 0 0 18 0" /></svg>{{ saving ? (mode === 'edit' ? '保存中…' : '创建中…') : (mode === 'edit' ? '保存修改' : '▤  创建数据源') }}</button></div>
  </form>
</template>
