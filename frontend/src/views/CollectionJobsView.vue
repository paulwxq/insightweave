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

const jobCount = computed(() => payload.value?.job_count ?? 0)
const pageCount = computed(() => Math.max(1, Math.ceil(jobCount.value / PAGE_SIZE)))
const pageItems = computed(() => {
  const items = payload.value?.items ?? []
  const start = (page.value - 1) * PAGE_SIZE
  return items.slice(start, start + PAGE_SIZE)
})
const pageNumbers = computed(() =>
  Array.from({ length: pageCount.value }, (_, index) => index + 1),
)

function statusText(status: string): string {
  if (status === 'running') return '运行中'
  if (status === 'failed') return '失败'
  return '成功'
}

function leaveMissing() {
  return router.replace({ path: '/data-sources', query: { notice: 'not_found' } })
}

function goBack() {
  const previous = window.history.state?.back
  if (typeof previous === 'string' && previous.length > 0) {
    router.back()
    return
  }
  router.push('/data-sources')
}

async function load() {
  loading.value = true
  loadError.value = ''
  payload.value = null
  page.value = 1
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
        <span class="ds-kicker">COLLECTION JOBS</span>
        <h1>数据采集作业列表</h1>
        <p>查看数据源的基本信息，以及该数据源下的全部采集作业记录，按执行时间倒序排列。</p>
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
          <p>
            <span>SOURCE ID</span>
            <strong>{{ payload.source.source_id }}</strong>
          </p>
          <p>
            <span>数据源名称</span>
            <strong>{{ payload.source.display_name }}</strong>
          </p>
          <p>
            <span>HOST : PORT</span>
            <strong>{{ payload.source.host }}:{{ payload.source.port }}</strong>
          </p>
          <p>
            <span>数据库名称</span>
            <strong>{{ payload.source.db_name }}</strong>
          </p>
          <p>
            <span>数据库类型 / 版本</span>
            <strong>{{ payload.source.db_type_version }}</strong>
          </p>
          <p>
            <span>描述</span>
            <strong>{{ payload.source.description || '—' }}</strong>
          </p>
        </div>
      </section>

      <section class="ds-jobs-card">
        <div class="ds-jobs-list-head">
          <div>
            <span class="ds-kicker ds-kicker-sm">COLLECTION JOBS</span>
            <h2>数据采集作业列表</h2>
            <p>该数据源下的采集与画像作业，按执行时间倒序排列。</p>
          </div>
          <div class="ds-jobs-actions">
            <span class="ds-jobs-count">共 {{ jobCount }} 个作业</span>
            <RouterLink
              v-if="payload.source.is_active"
              class="ds-jobs-create"
              :to="`/data-sources/${sourceId}/collection-jobs/new`"
            >
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14" /><path d="M5 12h14" /></svg>
              新建采集作业
            </RouterLink>
            <span v-else class="ds-jobs-create off" aria-disabled="true">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14" /><path d="M5 12h14" /></svg>
              新建采集作业
            </span>
          </div>
        </div>

        <p v-if="!payload.source.is_active" class="ds-banner" role="alert">
          该数据源已禁用，不能创建新的采集作业或画像
        </p>

        <div class="ds-jobs-table">
          <div class="ds-jobs-row ds-jobs-head" role="row">
            <span>JOB ID</span>
            <span>执行时间 / 状态</span>
            <span>包含步骤</span>
            <span>执行者</span>
            <span></span>
          </div>
          <p v-if="pageItems.length === 0" class="ds-jobs-empty">该数据源还没有采集作业</p>
          <div v-for="job in pageItems" :key="job.job_id" class="ds-jobs-row">
            <strong class="ds-jobs-id">{{ job.job_id }}</strong>
            <span class="ds-jobs-time">
              {{ displayDate(job.executed_at) }}
              <span class="home-status" :class="job.job_status">{{ statusText(job.job_status) }}</span>
            </span>
            <span class="ds-jobs-steps">
              <span v-for="step in job.steps" :key="`${job.job_id}-${step}`">{{ step }}</span>
            </span>
            <span class="ds-jobs-user">{{ job.run_by_user_id || '' }}</span>
            <span class="ds-jobs-ops">
              <RouterLink
                v-if="payload.source.is_active && job.job_status === 'success'"
                class="ds-jobs-publish"
                :to="`/data-sources/${sourceId}/collection-jobs/${job.job_id}/publish`"
              >发布</RouterLink>
              <span
                v-else
                class="ds-jobs-publish off"
                aria-disabled="true"
                :title="job.job_status === 'success' ? undefined : '作业存在失败或运行中的步骤，不能发布'"
              >发布</span>
              <RouterLink
                v-if="payload.source.is_active"
                class="ds-jobs-rerun"
                :to="`/data-sources/${sourceId}/collection-jobs/new?job_id=${job.job_id}`"
              >
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M21 12a9 9 0 0 0-15.5-6.3L3 8" />
                  <path d="M3 3v5h5" />
                  <path d="M3 12a9 9 0 0 0 15.5 6.3L21 16" />
                  <path d="M21 21v-5h-5" />
                </svg>
                重新执行
              </RouterLink>
              <span v-else class="ds-jobs-rerun off" aria-disabled="true">
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M21 12a9 9 0 0 0-15.5-6.3L3 8" />
                  <path d="M3 3v5h5" />
                  <path d="M3 12a9 9 0 0 0 15.5 6.3L21 16" />
                  <path d="M21 21v-5h-5" />
                </svg>
                重新执行
              </span>
            </span>
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
    </template>
  </div>
</template>
