<script setup lang="ts">
import type { DeletionRunRef } from '../api/types'
import { displayDateMs } from '../api/format'

/**
 * 删除/清理被 409（resource_has_unfinished_runs）拒绝时的阻断运行清单。
 * 作业名链接跳作业详情页并通过 run_id 定位对应运行；
 * 悬浮 title 展示完整运行 UUID。
 */
defineProps<{ runs: DeletionRunRef[] }>()

const KIND_TEXT: Record<string, string> = { collection: '采集', publish: '发布' }
const STATUS_TEXT: Record<string, string> = {
  running: '运行中',
  pending: '排队中',
  succeeded: '已成功',
  failed: '已失败',
  canceled: '已取消',
}
</script>

<template>
  <div v-if="runs.length" class="del-blocking">
    <p class="del-blocking-title">以下未结束运行将阻断本次删除：</p>
    <ul>
      <li v-for="run in runs" :key="run.run_id">
        <RouterLink
          v-if="run.workspace_id"
          class="del-blocking-job"
          :to="{ path: `/workspaces/${run.workspace_id}/${run.type}/jobs/${run.job_id}`, query: { run_id: run.run_id } }"
          :title="`运行 ${run.run_id}`"
        >
          {{ KIND_TEXT[run.type] ?? run.type }} · {{ run.job_name || run.job_id }}
        </RouterLink>
        <span v-else class="del-blocking-job" :title="`运行 ${run.run_id}`">
          {{ KIND_TEXT[run.type] ?? run.type }} · {{ run.job_name || run.job_id }}
        </span>
        <span class="del-blocking-meta">
          {{ run.job_id }} · {{ STATUS_TEXT[run.status] ?? run.status }}<template v-if="run.started_at"> · 开始于 {{ displayDateMs(run.started_at) }}</template>
        </span>
      </li>
    </ul>
    <p class="del-blocking-hint">请先等待这些运行结束；遗留状态经核对修复后，可重新检查并确认删除。</p>
  </div>
</template>
