/**
 * 运行步骤 manifest 的展示解析（doc 6.1 §11、doc 6.3 §5.3）。
 *
 * - input_manifest：`{files: [{path, collection_run_id|null}]}`
 * - output_manifest（采集）：`{files: [path]}`
 * - target_manifest（发布）：`{targets: [{type, ...目标键}]}`
 * manifest 为 SQL NULL 时不写顶层 null；展示层统一按"暂无记录"处理。
 * 目标出现在清单中不代表发布成功，成功与否以步骤状态为准。
 */
import type { ManifestFileEntry, ManifestTargetEntry } from '../api/types'

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

/** 读取的文件清单；结构缺失或空数组均返回 []（页面显示"暂无记录"） */
export function manifestFiles(manifest: Record<string, unknown> | null | undefined): ManifestFileEntry[] {
  const record = asRecord(manifest)
  const files = record?.files
  if (!Array.isArray(files)) return []
  const rows: ManifestFileEntry[] = []
  for (const entry of files) {
    const item = asRecord(entry)
    if (item === null || typeof item.path !== 'string' || item.path === '') continue
    rows.push({
      path: item.path,
      collection_run_id:
        typeof item.collection_run_id === 'string' ? item.collection_run_id : null,
    })
  }
  return rows
}

/** 生成的文件清单（采集步骤 output_manifest）；仅含成功生成的文件 */
export function manifestOutputFiles(
  manifest: Record<string, unknown> | null | undefined,
): string[] {
  const record = asRecord(manifest)
  const files = record?.files
  if (!Array.isArray(files)) return []
  return files.filter((path): path is string => typeof path === 'string' && path !== '')
}

const TARGET_TYPE_LABELS: Record<string, string> = {
  postgresql: 'PostgreSQL 表',
  milvus: 'Milvus 集合',
  neo4j: 'Neo4j 数据库',
}

/** 处理的下游目标清单（发布步骤 target_manifest） */
export function manifestTargets(
  manifest: Record<string, unknown> | null | undefined,
): ManifestTargetEntry[] {
  const record = asRecord(manifest)
  const targets = record?.targets
  if (!Array.isArray(targets)) return []
  const rows: ManifestTargetEntry[] = []
  for (const entry of targets) {
    const item = asRecord(entry)
    if (item === null || typeof item.type !== 'string') continue
    if (!(item.type in TARGET_TYPE_LABELS)) continue
    rows.push(item as unknown as ManifestTargetEntry)
  }
  return rows
}

export function targetTypeLabel(type: string): string {
  return TARGET_TYPE_LABELS[type] ?? type
}

/** 下游目标的可读标识：PostgreSQL 用全限定表名，Milvus 用集合名，Neo4j 用库名 */
export function targetIdentity(target: ManifestTargetEntry): string {
  if (target.type === 'postgresql') {
    return [target.database, target.schema, target.table].filter(Boolean).join('.')
  }
  if (target.type === 'milvus') return target.collection ?? ''
  if (target.type === 'neo4j') return target.database ?? ''
  return ''
}

/**
 * 步骤耗时（started_at → finished_at）。二者均存在且可解析时按
 * `1m 03s` / `12.4s` 展示；否则返回空串由页面显示 `—`。
 */
export function stepDuration(
  startedAt: string | null | undefined,
  finishedAt: string | null | undefined,
): string {
  if (!startedAt || !finishedAt) return ''
  const start = new Date(startedAt).getTime()
  const end = new Date(finishedAt).getTime()
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return ''
  const totalSeconds = (end - start) / 1000
  if (totalSeconds < 60) return `${totalSeconds.toFixed(1)}s`
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = Math.round(totalSeconds % 60)
  return `${minutes}m ${String(seconds).padStart(2, '0')}s`
}
