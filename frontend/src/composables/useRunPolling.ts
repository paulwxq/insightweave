/**
 * 运行详情轮询（doc 6.2 §6.3）：一次点击 = 一个运行，前端轮询详情，
 * 3s 间隔，终态停止；离开页面停止轮询但运行继续（不取消、不重提交）。
 */
import { onUnmounted, ref } from 'vue'
import { getRunDetail } from '../api/workspaces'
import type { JobKind, RunDetail } from '../api/types'
import { isTerminalRunStatus } from '../utils/steps'

const POLL_INTERVAL_MS = 3000

export function useRunPolling(kind: JobKind) {
  const runId = ref('')
  const detail = ref<RunDetail | null>(null)
  const loading = ref(false)
  const error = ref('')
  let timer: ReturnType<typeof setTimeout> | null = null
  let requestSeq = 0

  function stopTimer() {
    if (timer !== null) {
      clearTimeout(timer)
      timer = null
    }
  }

  async function pollOnce() {
    const token = ++requestSeq
    try {
      const row = await getRunDetail(kind, runId.value)
      if (token !== requestSeq || !runId.value) return
      detail.value = row
      error.value = ''
      if (!isTerminalRunStatus(row.status)) {
        timer = setTimeout(() => void pollOnce(), POLL_INTERVAL_MS)
      }
    } catch (cause) {
      if (token !== requestSeq || !runId.value) return
      error.value = cause instanceof Error ? cause.message : '运行详情查询失败'
      stopTimer()
    }
  }

  function open(id: string) {
    stopTimer()
    requestSeq++
    runId.value = id
    detail.value = null
    error.value = ''
    if (!id) return
    loading.value = true
    void pollOnce().finally(() => {
      loading.value = false
    })
  }

  function retry() {
    if (!runId.value || timer !== null) return
    void pollOnce()
  }

  /** 立即刷新一次（doc 11 §4.4.2：终止成功后不等下一轮 3 秒轮询）。 */
  function refresh() {
    if (!runId.value) return
    stopTimer()
    void pollOnce()
  }

  function close() {
    stopTimer()
    requestSeq++
    runId.value = ''
    detail.value = null
    error.value = ''
    loading.value = false
  }

  onUnmounted(stopTimer)

  return { runId, detail, loading, error, open, retry, refresh, close }
}
