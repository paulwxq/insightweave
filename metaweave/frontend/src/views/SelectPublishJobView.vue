<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { listCollectionJobs } from '../api/dataSources'
import { displayDate } from '../api/format'
import { ApiError } from '../api/http'
import type { CollectionJobListResponse } from '../api/types'

const PAGE_SIZE = 10

const route = useRoute()
const router = useRouter()
const sourceId = computed(() =>
  typeof route.params.sourceId === 'string' ? route.params.sourceId : '',
)
const payload = ref<CollectionJobListResponse | null>(null)
const loading = ref(true)
const loadError = ref('')
const page = ref(1)
const selectedJobId = ref('')
const sortDesc = ref(true)

// doc 20.1 §11.2：只有汇总状态为 success 的作业可选；failed 与 running 不进入候选列表
const publishableItems = computed(() =>
  (payload.value?.items ?? []).filter((item) => item.job_status === 'success'),
)
const jobCount = computed(() => publishableItems.value.length)
const pageCount = computed(() => Math.max(1, Math.ceil(jobCount.value / PAGE_SIZE)))
const sortedItems = computed(() => {
  const items = [...publishableItems.value]
  items.sort((left, right) => {
    const delta = left.executed_at.localeCompare(right.executed_at)
    return sortDesc.value ? -delta : delta
  })
  return items
})
const pageItems = computed(() => {
  const start = (page.value - 1) * PAGE_SIZE
  return sortedItems.value.slice(start, start + PAGE_SIZE)
})
const pageNumbers = computed(() =>
  Array.from({ length: pageCount.value }, (_, index) => index + 1),
)
const canContinue = computed(
  () => Boolean(payload.value?.source.is_active && selectedJobId.value),
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
  router.push(`/data-sources/${sourceId.value}/publish-jobs`)
}

function toggleExecutedAtSort() {
  sortDesc.value = !sortDesc.value
  page.value = 1
}

function choose(jobId: string) {
  if (!payload.value?.source.is_active) return
  selectedJobId.value = jobId
}

async function load() {
  loading.value = true
  loadError.value = ''
  payload.value = null
  page.value = 1
  selectedJobId.value = ''
  sortDesc.value = true
  try {
    payload.value = await listCollectionJobs(sourceId.value)
  } catch (error) {
    if (
      error instanceof ApiError &&
      (error.code === 'not_found' || error.code === 'invalid_request')
    ) {
      await leaveMissing()
      return
    }
    loadError.value = error instanceof ApiError ? error.message : '采集作业列表加载失败'
  } finally {
    loading.value = false
  }
}

watch(sourceId, load, { immediate: true })
watch(pageCount, (count) => {
  if (page.value > count) page.value = count
})
</script>

<template>
  <div class="ds-jobs">
    <header class="ds-jobs-header">
      <div>
        <span class="ds-kicker">NEW PUBLISH</span>
        <h1>数据画像发布</h1>
        <p>查看数据源基本信息，选择一个已完成的采集作业（仅支持单选），点击下方按钮发布数据画像。</p>
      </div>
      <button type="button" class="ds-jobs-back" @click="goBack">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></svg>
        返回
      </button>
    </header>

    <p v-if="loading" class="ds-jobs-state">正在读取采集作业…</p>
    <p v-else-if="loadError" class="ds-jobs-state error">{{ loadError }}</p>

    <template v-else-if="payload">
      <section class="ds-jobs-card">
        <div>
          <span class="ds-kicker ds-kicker-sm">DATA SOURCE INFO</span>
          <h2>数据源基本信息</h2>
        </div>
        <div class="ds-jobs-facts">
          <p><span>SOURCE ID</span><strong>{{ payload.source.source_id }}</strong></p>
          <p><span>数据源名称</span><strong>{{ payload.source.display_name }}</strong></p>
          <p><span>HOST : PORT</span><strong>{{ payload.source.host }}:{{ payload.source.port }}</strong></p>
          <p><span>数据库名称</span><strong>{{ payload.source.db_name }}</strong></p>
          <p><span>数据库类型 / 版本</span><strong>{{ payload.source.db_type_version }}</strong></p>
          <p><span>描述</span><strong>{{ payload.source.description || '—' }}</strong></p>
        </div>
      </section>

      <section class="ds-jobs-card">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">COLLECTION JOBS</span>
            <h2>选择数据采集作业</h2>
            <p>从该数据源已全部成功的采集作业中单选一个，按执行时间{{ sortDesc ? '倒序' : '正序' }}排列；存在失败或运行中步骤的作业不在此列。</p>
          </div>
          <div class="ds-jobs-actions">
            <button
              type="button"
              class="asset-sort"
              :aria-label="sortDesc ? '执行时间倒序，点击改为正序' : '执行时间正序，点击改为倒序'"
              @click="toggleExecutedAtSort"
            >
              执行时间
              <svg viewBox="0 0 24 24" aria-hidden="true" :class="{ asc: !sortDesc }">
                <path d="M12 5v14" /><path d="m19 12-7 7-7-7" />
              </svg>
            </button>
            <span class="ds-jobs-count">共 {{ jobCount }} 个作业</span>
          </div>
        </div>

        <p v-if="!payload.source.is_active" class="ds-banner" role="alert">
          该数据源已禁用，不能发布画像
        </p>

        <div class="ds-jobs-table">
          <div class="ds-jobs-row ds-jobs-head" role="row">
            <span>JOB ID</span>
            <span>执行时间</span>
            <span>包含步骤</span>
            <span>执行者</span>
            <span></span>
          </div>
          <p v-if="pageItems.length === 0" class="ds-jobs-empty">该数据源还没有可发布的采集作业</p>
          <div v-for="job in pageItems" :key="job.job_id" class="ds-jobs-row">
            <span class="ds-publish-pick">
              <button
                type="button"
                class="nj-check"
                :class="{ on: selectedJobId === job.job_id }"
                :disabled="!payload.source.is_active"
                :aria-pressed="selectedJobId === job.job_id"
                :aria-label="`选择 ${job.job_id}`"
                @click="choose(job.job_id)"
              >
                <svg v-if="selectedJobId === job.job_id" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6 9 17l-5-5" /></svg>
              </button>
              <strong class="ds-jobs-id">{{ job.job_id }}</strong>
            </span>
            <span class="ds-jobs-time">{{ displayDate(job.executed_at) }}</span>
            <span class="ds-jobs-steps">
              <span v-for="step in job.steps" :key="`${job.job_id}-${step}`">{{ step }}</span>
            </span>
            <span class="ds-jobs-user">{{ job.run_by_user_id || '' }}</span>
            <span></span>
          </div>
        </div>

        <div class="ds-jobs-pager">
          <span>共 {{ jobCount }} 条记录 · 第 {{ page }} / {{ pageCount }} 页</span>
          <div>
            <button type="button" :disabled="page <= 1" aria-label="上一页" @click="page -= 1">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6" /></svg>
            </button>
            <button
              v-for="number in pageNumbers"
              :key="number"
              type="button"
              :class="{ current: number === page }"
              @click="page = number"
            >
              {{ number }}
            </button>
            <button type="button" :disabled="page >= pageCount" aria-label="下一页" @click="page += 1">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6" /></svg>
            </button>
          </div>
        </div>
      </section>

      <div class="ds-new-job-next">
        <RouterLink
          v-if="canContinue"
          :to="
            route.path.startsWith('/profile-publishing')
              ? `/profile-publishing/${sourceId}/jobs/${selectedJobId}/publish`
              : `/data-sources/${sourceId}/collection-jobs/${selectedJobId}/publish`
          "
        >
          下一步：发布数据画像
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14" /><path d="m12 5 7 7-7 7" /></svg>
        </RouterLink>
        <button v-else type="button" disabled>
          下一步：发布数据画像
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14" /><path d="m12 5 7 7-7 7" /></svg>
        </button>
      </div>
    </template>
  </div>
</template>
