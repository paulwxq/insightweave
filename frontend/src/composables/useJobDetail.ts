/**
 * 作业详情共享逻辑（采集/发布）：作业信息、版本列表与选中版本、运行历史分页。
 * 作业/版本不存在时跳回指定回退列表。
 * 运行历史页码由视图从地址栏 query 提供（doc 6.3 §4.1：分页状态写入
 * query，可刷新恢复）；视图改页时写 query，本组合式函数只读页码并加载。
 */
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ApiError } from '../api/http'
import type { JobItem, JobKind, RevisionItem, RunListResponse } from '../api/types'
import { getJob, listJobRuns, listRevisions } from '../api/workspaces'

const RUNS_PAGE_SIZE = 10

/**
 * @param notFoundTarget 作业/版本不存在时的完整回退地址（含自身 query 与
 *   notice，doc 9 §7.6：用作业详情页的返回目标，从全局列表进来则回全局列表）；
 *   缺省回数据源列表并附提示。
 * @param runsPageRef 运行历史页码（1 起）；缺省为第 1 页。
 * @param workspaceIdRef 路由上的工作区 ID；提供时校验作业归属（§7.6：
 *   作业不属于该工作区同样走"不存在"回退），工作区参数变化时重新加载校验。
 */
export function useJobDetail(
  kind: JobKind,
  jobIdRef: () => string,
  notFoundTarget?: () => string,
  runsPageRef?: () => number,
  workspaceIdRef?: () => string,
) {
  const router = useRouter()
  const job = ref<JobItem | null>(null)
  const revisions = ref<RevisionItem[]>([])
  // 页面当前引用展示的版本；发起运行必须携带该版本号（doc 6.2 §6.2）
  const selectedRevisionNo = ref<number | null>(null)
  const loading = ref(true)
  const error = ref('')

  const runs = ref<RunListResponse | null>(null)
  const runsLoading = ref(false)
  const runsError = ref('')
  const runsPage = computed(() => Math.max(1, runsPageRef ? runsPageRef() : 1))
  const runsOffset = computed(() => (runsPage.value - 1) * RUNS_PAGE_SIZE)

  const selectedRevision = computed(
    () =>
      revisions.value.find((row) => row.revision_no === selectedRevisionNo.value) ??
      job.value?.revision ??
      null,
  )
  const latestRevisionNo = computed(() => job.value?.latest_revision_no ?? null)

  async function loadJob() {
    const jobId = jobIdRef()
    if (!jobId) return
    loading.value = true
    error.value = ''
    job.value = null
    revisions.value = []
    selectedRevisionNo.value = null
    try {
      const [jobRow, revisionRows] = await Promise.all([
        getJob(kind, jobId),
        listRevisions(kind, jobId),
      ])
      // §7.6：作业不属于路由工作区时按"不存在"回退，与执行页同规则
      const expectedWorkspace = workspaceIdRef ? workspaceIdRef() : ''
      if (expectedWorkspace && jobRow.workspace_id !== expectedWorkspace) {
        await router.replace(
          notFoundTarget ? notFoundTarget() : '/data-sources?notice=not_found',
        )
        return
      }
      job.value = jobRow
      revisions.value = revisionRows.items
      selectedRevisionNo.value = jobRow.latest_revision_no
    } catch (cause) {
      if (
        cause instanceof ApiError &&
        (cause.code === 'not_found' || cause.code === 'invalid_request')
      ) {
        await router.replace(
          notFoundTarget ? notFoundTarget() : '/data-sources?notice=not_found',
        )
        return
      }
      error.value = cause instanceof Error ? cause.message : '作业信息加载失败。'
    } finally {
      loading.value = false
    }
  }

  async function loadRuns() {
    const jobId = jobIdRef()
    if (!jobId) return
    runsLoading.value = true
    runsError.value = ''
    try {
      runs.value = await listJobRuns(kind, jobId, {
        limit: RUNS_PAGE_SIZE,
        offset: runsOffset.value,
      })
    } catch (cause) {
      runsError.value = cause instanceof ApiError ? cause.message : '运行历史加载失败。'
    } finally {
      runsLoading.value = false
    }
  }

  function selectRevision(revisionNo: number) {
    selectedRevisionNo.value = revisionNo
  }

  watch(
    () => [jobIdRef(), workspaceIdRef ? workspaceIdRef() : ''],
    () => {
      void loadJob()
      void loadRuns()
    },
    { immediate: true },
  )
  watch(runsPage, () => void loadRuns())

  return {
    job,
    revisions,
    selectedRevisionNo,
    selectedRevision,
    latestRevisionNo,
    loading,
    error,
    runs,
    runsLoading,
    runsError,
    runsPage,
    runsOffset,
    runsPageSize: RUNS_PAGE_SIZE,
    loadJob,
    loadRuns,
    selectRevision,
  }
}
