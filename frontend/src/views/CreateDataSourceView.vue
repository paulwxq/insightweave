<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getDataSource } from '../api/dataSources'
import { ApiError } from '../api/http'
import type { DataSourceListItem } from '../api/types'
import DataSourceForm from '../components/DataSourceForm.vue'
import PageBreadcrumbs from '../components/PageBreadcrumbs.vue'

const route = useRoute()
const router = useRouter()
const sourceId = computed(() =>
  typeof route.params.sourceId === 'string' ? route.params.sourceId : '',
)
const editing = computed(() => sourceId.value !== '')
const initial = ref<DataSourceListItem | null>(null)
const loading = ref(false)
const loadError = ref('')

onMounted(async () => {
  if (!editing.value) return
  loading.value = true
  try {
    initial.value = await getDataSource(sourceId.value)
  } catch (error) {
    if (
      error instanceof ApiError &&
      (error.code === 'not_found' || error.code === 'invalid_request')
    ) {
      await router.replace({ path: '/data-sources', query: { notice: 'not_found' } })
      return
    }
    loadError.value = error instanceof Error ? error.message : '数据源读取失败。'
  } finally {
    loading.value = false
  }
})

function onSaved() {
  router.push('/data-sources')
}
</script>

<template>
  <div :class="editing ? 'ds-detail' : 'create-page'">
    <PageBreadcrumbs
      :segments="editing
        ? [
            { label: '数据源管理', to: '/data-sources' },
            { label: initial?.display_name ?? '数据源', to: `/data-sources/${sourceId}` },
            { label: '修改' },
          ]
        : [
            { label: '数据源管理', to: '/data-sources' },
            { label: '新建数据源连接' },
          ]"
    />
    <header v-if="editing" class="ds-edit-header cds-header-row">
      <div>
        <span class="ds-kicker ds-kicker-sm">DATA SOURCE</span>
        <h1>修改数据源连接</h1>
        <p>数据库类型登记后不可修改；更新连接信息、Schema 与说明后保存。</p>
      </div>
      <RouterLink :to="`/data-sources/${sourceId}`" class="ds-jobs-back">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></svg>
        返回
      </RouterLink>
    </header>
    <header v-else class="page-header stacked cds-header-row">
      <div>
        <span class="eyebrow">DATA SOURCE</span>
        <h1>创建数据源连接</h1>
        <p>填写连接信息，测试成功后勾选 Schema，即可登记为可用数据源。</p>
      </div>
      <RouterLink to="/data-sources" class="ds-jobs-back">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></svg>
        返回
      </RouterLink>
    </header>
    <div v-if="loading" class="state-block" role="status">正在读取数据源…</div>
    <div v-else-if="loadError" class="state-block error-state" role="alert">{{ loadError }}</div>
    <DataSourceForm
      v-else-if="!editing || initial"
      :mode="editing ? 'edit' : 'create'"
      :source-id="sourceId"
      :initial="initial"
      :cancel-to="editing ? `/data-sources/${sourceId}` : '/data-sources'"
      @saved="onSaved"
    />
  </div>
</template>

<style scoped>
/* doc 9 验收统一：返回按钮并入页头右侧，与标题块顶对齐（覆盖两种页头的纵向布局） */
.page-header.cds-header-row,
.ds-edit-header.cds-header-row {
  display: flex;
  flex-direction: row;
  justify-content: space-between;
  align-items: flex-start;
  gap: 22px;
}
</style>
