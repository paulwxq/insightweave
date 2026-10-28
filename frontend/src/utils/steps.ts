/**
 * 采集/发布步骤元数据与展示助手（doc 6.2 §4.3、§8.1）。
 *
 * 前端的排序与依赖仅作交互提示；最终白名单、子集校验、依赖排序、
 * 发布固定顺序与发布准入全部由后端执行（doc 6.2 §9.2）。
 */

import type { JobKind, RunStatus, RunStepStatus } from '../api/types'

export const COLLECTION_STEPS = ['db', 'ddl', 'json', 'md', 'domain', 'dim', 'rel', 'sql'] as const

export const PUBLISH_STEPS = [
  'db',
  'ddl',
  'json',
  'domain',
  'table_schema',
  'dim_value',
  'rel',
  'sql',
] as const

/** 采集步骤之间的依赖边（doc 6 §3）；db 无依赖 */
const COLLECTION_STEP_DEPS: Record<string, readonly string[]> = {
  json: ['ddl'],
  md: ['ddl', 'json'],
  domain: ['md'],
  dim: ['json'],
  rel: ['json', 'domain'],
  sql: ['md', 'domain', 'rel'],
}

/** 发布步骤依赖的采集产物目录（doc 6.2 §8.1，与 backend precheck.STEP_INPUT_DIRS 一致） */
export const PUBLISH_STEP_INPUT_DIRS: Record<string, readonly string[]> = {
  db: ['db'],
  ddl: ['ddl'],
  json: ['json'],
  domain: ['domain'],
  table_schema: ['json', 'md'],
  dim_value: ['dim'],
  rel: ['json', 'domain', 'rel'],
  sql: ['sql'],
}

export interface StepGroupDef {
  id: string
  title: string
  steps: string[]
}

const COLLECTION_GROUPS: StepGroupDef[] = [
  { id: 'a', title: 'a.) 对于数据库整体', steps: ['db'] },
  { id: 'b', title: 'b.) 对表进行抽样画像', steps: ['ddl', 'json', 'md', 'domain', 'dim'] },
  { id: 'c', title: 'c.) 分析表之间的关联关系', steps: ['rel'] },
  { id: 'd', title: 'd.) 生成 NL2SQL RAG metadata', steps: ['sql'] },
]

const PUBLISH_GROUPS: StepGroupDef[] = [
  { id: 'a', title: 'a.) 对于数据库整体', steps: ['db'] },
  { id: 'b', title: 'b.) 对表进行抽样画像', steps: ['ddl', 'json', 'domain'] },
  { id: 'c', title: 'c.) 分析表之间的关联关系', steps: ['rel'] },
  {
    id: 'd',
    title: 'd.) 生成 NL2SQL RAG metadata',
    steps: ['sql', 'table_schema', 'dim_value'],
  },
]

export function stepGroups(kind: JobKind): StepGroupDef[] {
  return kind === 'collection' ? COLLECTION_GROUPS : PUBLISH_GROUPS
}

/** 该类作业的全部步骤（固定顺序） */
export function allSteps(kind: JobKind): string[] {
  return kind === 'collection' ? [...COLLECTION_STEPS] : [...PUBLISH_STEPS]
}

/**
 * 采集步骤的命令行参数旁注（doc 6.3 §6.2）：主标识恒为步骤 ID，
 * domain/dim 可附加 CLI 提示作次要说明；其余步骤与全部发布步骤无旁注。
 * 提交后端的步骤名始终是步骤 ID，不用这些字符串。
 */
const COLLECTION_CLI_HINTS: Record<string, string> = {
  domain: '--generate-domains --description "…"',
  dim: '--generate-dim-config',
}

export function stepCliHint(kind: JobKind, step: string): string {
  if (kind === 'collection') return COLLECTION_CLI_HINTS[step] ?? ''
  return ''
}

/** 同请求内拓扑排序（与后端 normalize_collection_steps 一致，仅用于页面提示） */
export function executionOrder(chosen: string[]): string[] {
  const position = new Map(chosen.map((step, index) => [step, index]))
  const pending = new Map(
    chosen.map((step) => [
      step,
      new Set((COLLECTION_STEP_DEPS[step] ?? []).filter((dep) => position.has(dep))),
    ]),
  )
  const ordered: string[] = []
  const done = new Set<string>()
  while (pending.size > 0) {
    const available = [...pending.keys()].filter((step) => {
      const deps = pending.get(step)!
      return [...deps].every((dep) => done.has(dep))
    })
    if (available.length === 0) break
    const next = available.reduce((best, step) =>
      position.get(step)! < position.get(best)! ? step : best,
    )
    ordered.push(next)
    done.add(next)
    pending.delete(next)
  }
  return ordered
}

/**
 * 交互提示：勾选步骤的依赖未同选时，说明这些依赖将读取工作区现有产物
 * （doc 6 §3：只排序选中步骤，不自动补选上游）。
 */
export function unchosenDeps(chosen: readonly string[]): Array<{ step: string; deps: string[] }> {
  const chosenSet = new Set(chosen)
  const rows: Array<{ step: string; deps: string[] }> = []
  for (const step of chosen) {
    const deps = (COLLECTION_STEP_DEPS[step] ?? []).filter((dep) => !chosenSet.has(dep))
    if (deps.length > 0) rows.push({ step, deps })
  }
  return rows
}

// ---- 运行状态文案 ----

/** 触发方式文案：scheduled 本期仅预留（doc 6.2 §6.2），全部为 manual */
export function triggerTypeText(triggerType: string): string {
  switch (triggerType) {
    case 'manual':
      return '手动'
    case 'scheduled':
      return '调度'
    default:
      return triggerType
  }
}

export function runStatusText(status: RunStatus | string): string {
  switch (status) {
    case 'pending':
      return '等待执行'
    case 'running':
      return '运行中'
    case 'success':
      return '成功'
    case 'partial_success':
      return '部分成功'
    case 'failed':
      return '失败'
    default:
      return status
  }
}

export function stepStatusText(status: RunStepStatus | string): string {
  switch (status) {
    case 'pending':
      return '等待'
    case 'running':
      return '执行中'
    case 'success':
      return '成功'
    case 'failed':
      return '失败'
    case 'blocked':
      return '被阻断'
    default:
      return status
  }
}

export function isTerminalRunStatus(status: RunStatus | string): boolean {
  return status === 'success' || status === 'partial_success' || status === 'failed'
}
