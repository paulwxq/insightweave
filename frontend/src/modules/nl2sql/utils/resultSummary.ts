import type { ResultPageData } from '../api/types'

type SummaryPage = Pick<
  ResultPageData,
  'data_from_snapshot' | 'saved_row_count' | 'snapshot_rows_available'
>

export interface ResultSummary {
  /** 页面“共 N 条”的口径：文件分支为原保存行数，快照分支为快照可用行数。 */
  totalCount: number
  /** 快照分支的额外提示；文件分支为空。 */
  snapshotNotice: string
}

/** 根据结果来源生成展示计数与提示（doc 3.1 §5）。 */
export function summarizeResultPage(page: SummaryPage): ResultSummary {
  if (page.data_from_snapshot === true) {
    const snapshotRows = page.snapshot_rows_available ?? 0
    return {
      totalCount: snapshotRows,
      snapshotNotice: `完整结果文件不可用，当前展示保留快照（快照 ${snapshotRows} 行 / 原结果已保存 ${page.saved_row_count} 行）。导出仅包含快照行。`,
    }
  }
  return {
    totalCount: page.saved_row_count,
    snapshotNotice: '',
  }
}
