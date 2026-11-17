/**
 * doc 9 §7：来源（from）参数的校验、构造与各页返回目标解析。
 *
 * - from 只存在于前端路由 query，不提交给任何后端接口（§7.2）。
 * - 所有地址的拆装一律经 URL / URLSearchParams，不手拼、不手写解码（§7.2）。
 * - 返回文案规则见 §7.1；各页认可矩阵见 §7.4；创建工作区后的来源合成见 §7.6。
 */

const ORIGIN = 'http://meta.invalid'
const MAX_LENGTH = 2000
const CONTROL_CHARS = /[\u0000-\u001f\u007f]/

export type FromKind =
  | 'data-sources'
  | 'data-sources-detail'
  | 'workspaces'
  | 'collection-jobs'
  | 'publish-jobs'

export interface ResolvedFrom {
  kind: FromKind
  /** 净化后的地址：不合法的内层 from 已剥离 */
  target: string
}

/** 解析站内地址的基础形态；不满足硬性约束返回 null。 */
function parseAddress(raw: string): { pathname: string; params: URLSearchParams } | null {
  if (
    typeof raw !== 'string' ||
    raw.length === 0 ||
    raw.length > MAX_LENGTH ||
    !raw.startsWith('/') ||
    raw.startsWith('//') ||
    raw.includes('\\') ||
    CONTROL_CHARS.test(raw)
  ) {
    return null
  }
  let url: URL
  try {
    url = new URL(raw, ORIGIN)
  } catch {
    return null
  }
  if (url.origin !== ORIGIN) {
    return null
  }
  return { pathname: url.pathname, params: url.searchParams }
}

function rebuild(pathname: string, params: URLSearchParams): string {
  const query = params.toString()
  return query ? `${pathname}?${query}` : pathname
}

/** 数据源两种形式不允许内部再带 from（§7.2 校验）。 */
function sourceFormTarget(pathname: string, params: URLSearchParams): ResolvedFrom | null {
  if (params.has('from')) return null
  return { kind: pathname === '/data-sources' ? 'data-sources' : 'data-sources-detail', target: rebuild(pathname, params) }
}

/**
 * 内层 from 仅允许数据源形式（§7.2 嵌套：内层来自数据源页面，最多一层）。
 * 返回净化后的内层地址；不合法返回 null（调用方丢弃内层）。
 */
function validateInnerSource(raw: string): string | null {
  const parsed = parseAddress(raw)
  if (!parsed) return null
  if (parsed.pathname === '/data-sources') {
    return sourceFormTarget(parsed.pathname, parsed.params)?.target ?? null
  }
  if (/^\/data-sources\/[^/]+$/.test(parsed.pathname) && parsed.params.toString() === '') {
    return parsed.pathname
  }
  return null
}

/** 校验 from 值（§7.2 白名单）；不合法返回 null，由页面按无来源处理。 */
export function resolveFrom(raw: string | null | undefined): ResolvedFrom | null {
  if (!raw) return null
  const parsed = parseAddress(raw)
  if (!parsed) return null
  const { pathname, params } = parsed

  if (pathname === '/data-sources') return sourceFormTarget(pathname, params)
  if (/^\/data-sources\/[^/]+$/.test(pathname)) {
    // 数据源详情：不带 query
    if (params.toString() !== '') return null
    return { kind: 'data-sources-detail', target: pathname }
  }
  if (pathname === '/workspaces') {
    // 唯一允许嵌套的形式：内层不合法只丢内层（§7.2 嵌套）
    const inner = params.get('from')
    if (inner !== null) {
      const validInner = validateInnerSource(inner)
      if (validInner === null) params.delete('from')
      else params.set('from', validInner)
    }
    return { kind: 'workspaces', target: rebuild(pathname, params) }
  }
  if (pathname === '/collection-jobs' || pathname === '/publish-jobs') {
    // 全局作业列表不承载内层来源（§7.2 嵌套：只有工作区列表可能带 from）
    if (params.has('from')) return null
    return { kind: pathname === '/collection-jobs' ? 'collection-jobs' : 'publish-jobs', target: rebuild(pathname, params) }
  }
  return null
}

/** 新建来源：以来源页 fullPath 为基础，仅剔除 notice，其余保留（§7.2/§7.3）。 */
export function buildFrom(fullPath: string): string {
  const idx = fullPath.indexOf('?')
  if (idx === -1) return fullPath
  const params = new URLSearchParams(fullPath.slice(idx + 1))
  // 没有 notice 时原样使用 fullPath（§7.2），避免重建引入额外编码
  if (!params.has('notice')) return fullPath
  params.delete('notice')
  return rebuild(fullPath.slice(0, idx), params)
}

export type PageKind =
  | 'workspaces-list'
  | 'workspace-new'
  | 'workspace-home'
  | 'collection-job-detail'
  | 'collection-job-new'
  | 'publish-job-detail'
  | 'publish-job-new'
  | 'run-execute'
  | 'database-profile'
  | 'object-list'
  | 'object-detail'
  | 'source-detail'
  | 'source-new'
  | 'source-edit'

export interface BackSpec {
  /** null 表示该页无返回按钮（§7.4 落地页） */
  target: string | null
  label: string
  /** 目标是否取自 from（§7.3-3 的“与返回同一页”判断依据） */
  fromTarget: boolean
}

/**
 * 返回按钮文案统一为「返回」（用户 2026-11-02 裁定，替代原 §7.1 的去向文案表）；
 * 去向由目标地址本身表达，不写在按钮上。
 */
const BACK_LABEL = '返回'

const ACCEPTS: Partial<Record<PageKind, FromKind[]>> = {
  'workspaces-list': ['data-sources', 'data-sources-detail'],
  'workspace-new': ['workspaces', 'data-sources', 'data-sources-detail'],
  'workspace-home': ['workspaces'],
  'collection-job-detail': ['collection-jobs'],
  'collection-job-new': ['collection-jobs'],
  'publish-job-detail': ['publish-jobs'],
  'publish-job-new': ['publish-jobs'],
}

export interface BackOptions {
  workspaceId?: string
  sourceId?: string
  jobId?: string
  jobKind?: 'collection' | 'publish'
  /** 新建工作区：表单当前选择的数据源（无有效来源时按它筛选返回列表） */
  selectedSourceId?: string
  /** 对象详情返回对象列表时保留 schema_name */
  schemaName?: string
}

/** 按页面认可矩阵求返回目标与文案（§7.4/§7.1）。 */
export function resolveBack(kind: PageKind, rawFrom: string | null | undefined, opts: BackOptions): BackSpec {
  const resolved = resolveFrom(rawFrom)
  const accepted = ACCEPTS[kind]
  if (resolved && accepted?.includes(resolved.kind)) {
    return { target: resolved.target, label: BACK_LABEL, fromTarget: true }
  }
  const label = BACK_LABEL
  switch (kind) {
    case 'workspaces-list':
      return { target: null, label: '', fromTarget: false }
    case 'workspace-new': {
      const params = new URLSearchParams()
      if (opts.selectedSourceId) params.set('source_id', opts.selectedSourceId)
      return { target: rebuild('/workspaces', params), label, fromTarget: false }
    }
    case 'workspace-home':
      return { target: '/workspaces', label, fromTarget: false }
    case 'collection-job-detail':
    case 'collection-job-new':
      return { target: `/workspaces/${opts.workspaceId}/collection`, label, fromTarget: false }
    case 'publish-job-detail':
    case 'publish-job-new':
      return { target: `/workspaces/${opts.workspaceId}/publish`, label, fromTarget: false }
    case 'run-execute': {
      const side = opts.jobKind === 'publish' ? 'publish' : 'collection'
      return { target: `/workspaces/${opts.workspaceId}/${side}/jobs/${opts.jobId}`, label, fromTarget: false }
    }
    case 'database-profile':
      return { target: '/data-assets', label, fromTarget: false }
    case 'object-list':
      return { target: `/data-assets/${opts.sourceId}/profile`, label, fromTarget: false }
    case 'object-detail': {
      const params = new URLSearchParams()
      if (opts.schemaName) params.set('schema_name', opts.schemaName)
      return { target: rebuild(`/data-assets/${opts.sourceId}/objects`, params), label, fromTarget: false }
    }
    case 'source-detail':
    case 'source-new':
      return { target: '/data-sources', label, fromTarget: false }
    case 'source-edit':
      return { target: `/data-sources/${opts.sourceId}`, label, fromTarget: false }
  }
}

/** 给链接目标附加 from（经 query 对象写入，整段只编码一次）。 */
export function withFrom(target: string, from: string | null | undefined): string {
  if (!from) return target
  const idx = target.indexOf('?')
  const pathname = idx === -1 ? target : target.slice(0, idx)
  const params = new URLSearchParams(idx === -1 ? '' : target.slice(idx + 1))
  params.set('from', from)
  return rebuild(pathname, params)
}

/**
 * §7.3-2 工作区内部下钻的原样传递：有效值以净化后的形式传给下一页
 * （不合法的内层 from 已按 §7.2 剥离），无效值不传。
 */
export function passThroughFrom(raw: string | null | undefined): string | null {
  const resolved = resolveFrom(raw)
  return resolved ? resolved.target : null
}

/** §7.3-3 层级跳转（区内切换、更早的路径段）：仅保留工作区列表来源，其余丢弃。 */
export function hierarchyFrom(raw: string | null | undefined): string | null {
  const resolved = resolveFrom(raw)
  return resolved?.kind === 'workspaces' ? resolved.target : null
}

/**
 * §7.3-3-2 路径段来源：段目标与「返回」实际目标同页（按路径比较，不比 query）
 * 时原样带上净化来源；否则按层级跳转只留工作区来源。
 */
export function crumbFrom(
  backTarget: string | null,
  segmentPath: string,
  rawFrom: string | null | undefined,
): string | null {
  const backPath = backTarget ? backTarget.split('?')[0] : null
  return backPath === segmentPath ? passThroughFrom(rawFrom) : hierarchyFrom(rawFrom)
}

/** §6.4「工作区管理」段：from 指向工作区列表时直接采用该地址（含筛选），否则 /workspaces。 */
export function workspacesCrumbTo(rawFrom: string | null | undefined): string {
  const resolved = resolveFrom(rawFrom)
  return resolved?.kind === 'workspaces' ? resolved.target : '/workspaces'
}

/** 在目标自身 query 之外追加一次性提示（§7.6），不覆盖目标筛选。 */
export function appendNotice(target: string, notice: string): string {
  const idx = target.indexOf('?')
  const pathname = idx === -1 ? target : target.slice(0, idx)
  const params = new URLSearchParams(idx === -1 ? '' : target.slice(idx + 1))
  params.set('notice', notice)
  return rebuild(pathname, params)
}

/** 创建工作区成功后工作区详情的来源合成（§7.6 四种情况）。 */
export function synthesizeWorkspaceCreatedFrom(
  rawFrom: string | null | undefined,
  newSourceId: string,
): string | null {
  const resolved = resolveFrom(rawFrom)
  if (!resolved) return null
  if (resolved.kind === 'workspaces') return resolved.target
  if (resolved.kind === 'data-sources' || resolved.kind === 'data-sources-detail') {
    if (resolved.kind === 'data-sources-detail') {
      const detailId = resolved.target.split('/')[2]
      if (detailId !== newSourceId) {
        // 情况 3：用户改选了别的数据源，不保留嵌套来源
        const params = new URLSearchParams()
        params.set('source_id', newSourceId)
        return rebuild('/workspaces', params)
      }
    }
    // 情况 2：嵌套保留原来源
    const params = new URLSearchParams()
    params.set('source_id', newSourceId)
    params.set('from', resolved.target)
    return rebuild('/workspaces', params)
  }
  return null
}
