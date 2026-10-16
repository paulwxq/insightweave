/** 与 backend/jobs/collection_run.py 的偏序与阻断文案保持一致（doc 19.1、22.1、23.1）。 */

const STEP_DEPS: Record<string, readonly string[]> = {
  json: ['ddl'],
  md: ['ddl', 'json'],
  domain: ['md'],
  dim_config: ['json'],
  rel: ['json', 'domain'],
  sql: ['md', 'domain', 'rel'],
}

const BLOCK_MESSAGES: Record<string, string> = {
  'ddl→json':
    '本次请求的 DDL 未成功，JSON 未启动；如需使用已有 DDL 产物，请单独重跑 JSON',
  'json→md':
    '本次请求的 JSON 未成功，MD 未启动；如需使用已有 JSON 产物，请单独重跑 MD',
  'ddl→md':
    '本次请求的 DDL 未成功，MD 未启动；如需使用已有 JSON 产物，请单独重跑 MD',
  'md→domain':
    '本次请求的 MD 未成功，Domain 未启动；如需使用已有 MD 产物，请单独重跑 Domain',
  'json→dim_config':
    '本次请求的 JSON 未成功，Dim Config 未启动；如需使用已有 JSON 产物，请单独重跑 Dim Config',
  'json→rel':
    '本次请求的 JSON 未成功，Rel 未启动；如需使用已有 JSON 产物，请单独重跑 Rel',
  'domain→rel':
    '本次请求的 Domain 未成功，Rel 未启动；如需使用已有 Domain 产物，请单独重跑 Rel',
  'md→sql': '本次请求的 MD 未成功，SQL 未启动；请修复 MD 后重跑 SQL',
  'domain→sql': '本次请求的 Domain 未成功，SQL 未启动；请修复 Domain 后重跑 SQL',
  'rel→sql': '本次请求的 Rel 未成功，SQL 未启动；请修复 Rel 后重跑 SQL',
}

/** 同请求内拓扑排序，与 `normalize_collection_steps` 一致。 */
export function executionOrder(chosen: string[]): string[] {
  const position = new Map(chosen.map((step, index) => [step, index]))
  const pending = new Map(
    chosen.map((step) => [
      step,
      new Set(
        (STEP_DEPS[step] ?? []).filter((dep) => position.has(dep)),
      ),
    ]),
  )
  const ordered: string[] = []
  const done = new Set<string>()
  while (pending.size > 0) {
    const available = [...pending.keys()].filter((step) => {
      const deps = pending.get(step)!
      return [...deps].every((dep) => done.has(dep))
    })
    if (available.length === 0) {
      break
    }
    const next = available.reduce((best, step) =>
      (position.get(step)! < position.get(best)! ? step : best),
    )
    ordered.push(next)
    done.add(next)
    pending.delete(next)
  }
  return ordered
}

export function upstreamBlockedMessage(
  step: string,
  chosen: string[],
  failed: ReadonlySet<string>,
): string | null {
  const chosenSet = new Set(chosen)
  let pairs: Array<[string, string]>
  if (step === 'md' && !chosenSet.has('json')) {
    pairs = [['ddl', 'md']]
  } else if (step === 'md') {
    pairs = [['json', 'md']]
  } else {
    pairs = Object.entries(BLOCK_MESSAGES)
      .map(([key, message]) => {
        const [upstream, downstream] = key.split('→')
        return [upstream, downstream, message] as const
      })
      .filter(([, downstream]) => downstream === step)
      .map(([upstream, downstream]) => [upstream, downstream])
  }
  for (const [upstream] of pairs) {
    if (chosenSet.has(upstream) && failed.has(upstream)) {
      const key = `${upstream}→${step}`
      return BLOCK_MESSAGES[key] ?? null
    }
  }
  return null
}
