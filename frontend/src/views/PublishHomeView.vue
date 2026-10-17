<script setup lang="ts">
import { computed } from 'vue'
import SourceWorkBrowser from '../components/SourceWorkBrowser.vue'
import { getPublishHome } from '../api/dataSources'
import { useSourceHome, type HomeJob, type HomePayload } from '../composables/useSourceHome'

const home = useSourceHome(
  async (sourceId) => {
    const response = await getPublishHome(sourceId)
    return response as HomePayload
  },
  (item) => item.publish_id ?? item.job_id,
)

const metrics = [
  { label: '当前成功发布', hint: '该数据源当前成功的发布步骤数' },
  { label: '成功执行的发布作业', hint: '最近 30 天成功完成' },
  { label: '失败的发布作业', hint: '需要关注并重试的失败作业' },
]
const metricValues = computed(() => [
  home.snapshot.value?.counts.current ?? 0,
  home.snapshot.value?.counts.success_recent ?? 0,
  home.snapshot.value?.counts.failed ?? 0,
])
const sourceId = computed(() => home.selectedSourceId.value)
const newEnabled = computed(() => home.actionsReady.value)
const newTo = computed(() => `/profile-publishing/${sourceId.value}/new`)

function rerunEnabled(item: HomeJob) {
  return home.actionsReady.value && Boolean(item.publish_id)
}

function rerunTitle() {
  return ''
}

function rerunTo(item: HomeJob) {
  if (!item.publish_id) return ''
  return `/profile-publishing/${sourceId.value}/jobs/${item.job_id}/publish?publish_id=${item.publish_id}`
}

function itemTitle(item: HomeJob) {
  return item.publish_id ?? item.job_id
}
</script>

<template>
  <SourceWorkBrowser
    kicker="PROFILE PUBLISHING"
    title="数据画像发布"
    description="按数据源浏览发布作业，选中作业后可重新执行，也可以随时创建新的发布作业。"
    card-title="选择数据源与发布作业"
    card-kicker="SOURCE & PUBLISH"
    card-description="左侧选择数据源（source_id），右侧选择该数据源下的发布作业（job_id），选中作业后可重新执行或新建作业。"
    list-title="发布作业列表"
    :metrics="metrics"
    :metric-values="metricValues"
    new-label="新建发布作业"
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
    :item-key="(item) => item.publish_id ?? item.job_id"
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
