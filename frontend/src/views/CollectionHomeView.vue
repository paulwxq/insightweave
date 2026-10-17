<script setup lang="ts">
import { computed } from 'vue'
import SourceWorkBrowser from '../components/SourceWorkBrowser.vue'
import { getCollectionHome } from '../api/dataSources'
import { useSourceHome, type HomeJob, type HomePayload } from '../composables/useSourceHome'

const home = useSourceHome(
  async (sourceId) => {
    const response = await getCollectionHome(sourceId)
    return response as HomePayload
  },
  (item) => item.job_id,
)

const metrics = [
  { label: '正在执行的采集作业', hint: '正在运行的采集作业' },
  { label: '成功执行的采集作业', hint: '最近 30 天成功完成' },
  { label: '失败的采集作业', hint: '需要关注并重试的失败作业' },
]
const metricValues = computed(() => [
  home.snapshot.value?.counts.running ?? 0,
  home.snapshot.value?.counts.success_recent ?? 0,
  home.snapshot.value?.counts.failed ?? 0,
])
const sourceId = computed(() => home.selectedSourceId.value)
const newEnabled = computed(() => home.actionsReady.value)
const newTo = computed(() => `/data-collection/${sourceId.value}/new`)

function rerunEnabled(item: HomeJob) {
  return home.actionsReady.value && item.status !== 'running'
}

function rerunTitle(item: HomeJob) {
  return item.status === 'running' ? '当前步骤可能已有作业正在执行' : ''
}

function rerunTo(item: HomeJob) {
  return `/data-collection/${sourceId.value}/new?job_id=${item.job_id}`
}

function itemTitle(item: HomeJob) {
  return item.job_id
}
</script>

<template>
  <SourceWorkBrowser
    kicker="DATA COLLECTION"
    title="数据采集作业"
    description="按数据源浏览采集作业，选中作业后可重新执行，也可以随时创建新的采集作业。"
    card-title="选择数据源与采集作业"
    card-kicker="SOURCE & JOB"
    card-description="左侧选择数据源（source_id），右侧选择该数据源下的采集作业（job_id），选中作业后可重新执行或新建作业。"
    list-title="采集作业列表"
    :metrics="metrics"
    :metric-values="metricValues"
    new-label="新建采集作业"
    rerun-label="重新执行"
    :new-to="newTo"
    :new-enabled="newEnabled"
    :rerun-to="rerunTo"
    :rerun-enabled="rerunEnabled"
    :rerun-title="rerunTitle"
    :sources="home.sources.value"
    :filtered-sources="home.filteredSources.value"
    :db-type="home.dbType.value"
    :selected-source-id="home.selectedSourceId.value"
    :page-items="home.pageItems.value"
    :item-key="(item) => item.job_id"
    :item-title="itemTitle"
    :selected-key="home.selectedKey.value"
    :sort-desc="home.sortDesc.value"
    :page="home.page.value"
    :page-count="home.pageCount.value"
    :page-numbers="home.pageNumbers.value"
    :total="home.sortedItems.value.length"
    :refreshing="home.refreshing.value"
    :home-loading="home.homeLoading.value"
    :home-error="home.homeError.value"
    :has-snapshot="Boolean(home.snapshot.value)"
    :sources-error="home.sourcesError.value"
    :sources-loading="home.sourcesLoading.value"
    @update:db-type="home.dbType.value = $event"
    @choose-source="home.chooseSource"
    @select-item="home.selectItem"
    @refresh="home.refreshHome"
    @retry="home.retryHome"
    @reload-sources="home.reloadSources"
    @toggle-sort="home.toggleSort"
    @go-to-page="home.goToPage"
  />
</template>
