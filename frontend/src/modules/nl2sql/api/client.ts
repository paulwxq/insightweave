/**
 * NL2SQL Web API 客户端（doc 2 §6.2/§7.3）。
 *
 * - origin 来自 VITE_NL2SQL_API_BASE_URL，**首次调用时惰性校验**：缺失或
 *   非法不回退 MetaWeave 地址，抛 Nl2sqlNotConfiguredError 由页面提示。
 * - 处理顺序：HTTP 状态 → JSON/结构校验 → 外层业务 code → 内层查询状态。
 *   网络/超时/非 JSON 分别给出可识别的本地 code，不统称“后端未启动”。
 * - query 超时 480 秒（doc 2 §7.3：300 秒协作预算 + 最后一次模型调用 +
 *   收尾余量）；超时提示“结果尚未确认”，由调用方禁止自动重发。
 */

import { validateApiBaseUrl } from '../../../api/apiBaseUrl'
import type {
  Envelope,
  ErrorData,
  PageData,
  ReadinessData,
  ResultPageData,
  SessionInfo,
  WebQueryResult,
  WebTurn,
} from './types'

export const QUERY_TIMEOUT_MS = 480_000
const DEFAULT_TIMEOUT_MS = 30_000

/** 前端本地错误 code（与服务端 error_code 闭集区分）。 */
export type LocalErrorCode = 'not_configured' | 'network_error' | 'timeout' | 'invalid_response'

export class Nl2sqlNotConfiguredError extends Error {
  readonly code: LocalErrorCode = 'not_configured'

  constructor(message: string) {
    super(message)
    this.name = 'Nl2sqlNotConfiguredError'
  }
}

export class Nl2sqlApiError extends Error {
  constructor(
    /** 服务端 error_code 或本地 code（not_configured/network_error/…）。 */
    public readonly code: string,
    message: string,
    public readonly httpStatus: number | null = null,
    public readonly data: ErrorData | null = null,
    /** 429/409 附带的 Retry-After 秒数。 */
    public readonly retryAfterSeconds: number | null = null,
  ) {
    super(message)
    this.name = 'Nl2sqlApiError'
  }
}

let cachedBaseUrl: string | null = null

/** 惰性解析并校验 NL2SQL origin（doc 2 §6.2：禁止回退 MetaWeave 地址）。 */
export function nl2sqlBaseUrl(): string {
  if (cachedBaseUrl !== null) return cachedBaseUrl
  const raw = import.meta.env.VITE_NL2SQL_API_BASE_URL
  if (!raw) {
    throw new Nl2sqlNotConfiguredError(
      '缺少 VITE_NL2SQL_API_BASE_URL，请配置浏览器可访问的 NL2SQL 后端地址。',
    )
  }
  cachedBaseUrl = validateApiBaseUrl(raw)
  return cachedBaseUrl
}

/** 测试/配置变更后清缓存。 */
export function resetNl2sqlBaseUrlCache(): void {
  cachedBaseUrl = null
}

function parseRetryAfter(response: Response): number | null {
  const raw = response.headers.get('Retry-After')
  if (raw === null) return null
  const seconds = Number.parseInt(raw, 10)
  return Number.isFinite(seconds) && seconds > 0 ? seconds : null
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  let base: string
  try {
    base = nl2sqlBaseUrl()
  } catch (error) {
    if (error instanceof Nl2sqlNotConfiguredError) throw error
    throw error
  }

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  let response: Response
  try {
    response = await fetch(new URL(path, base), {
      ...init,
      headers: { 'Content-Type': 'application/json', ...init.headers },
      signal: controller.signal,
    })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new Nl2sqlApiError('timeout', '请求超时，结果尚未确认，后端可能仍在执行。')
    }
    throw new Nl2sqlApiError('network_error', '无法连接 NL2SQL 服务，请检查后端是否已启动。')
  } finally {
    clearTimeout(timer)
  }

  let body: Envelope<T> | null = null
  try {
    body = (await response.json()) as Envelope<T>
  } catch {
    throw new Nl2sqlApiError(
      'invalid_response',
      'NL2SQL 服务返回了无法识别的响应。',
      response.status,
    )
  }
  if (typeof body?.code !== 'number' || typeof body?.data === 'undefined') {
    throw new Nl2sqlApiError('invalid_response', 'NL2SQL 服务响应结构不完整。', response.status)
  }
  if (body.code !== 200) {
    const data = body.data as unknown as ErrorData
    const code = typeof data?.error_code === 'string' ? data.error_code : 'internal_error'
    const message = typeof body.message === 'string' && body.message ? body.message : code
    throw new Nl2sqlApiError(
      code,
      message,
      response.status,
      data ?? null,
      parseRetryAfter(response),
    )
  }
  return body.data
}

// ---------------------------------------------------------------- 端点

export function getReadiness(sourceId: string): Promise<ReadinessData> {
  return request<ReadinessData>(`/api/v2/sources/${encodeURIComponent(sourceId)}/readiness`)
}

export function createSession(sourceId: string): Promise<SessionInfo> {
  return request<SessionInfo>('/api/v2/sessions', {
    method: 'POST',
    body: JSON.stringify({ source_id: sourceId }),
  })
}

export interface SessionListParams {
  sourceId?: string
  limit?: number
  offset?: number
}

export function listSessions(params: SessionListParams = {}): Promise<PageData<SessionInfo>> {
  const query = new URLSearchParams()
  if (params.sourceId) query.set('source_id', params.sourceId)
  if (params.limit !== undefined) query.set('limit', String(params.limit))
  if (params.offset !== undefined) query.set('offset', String(params.offset))
  const suffix = query.size > 0 ? `?${query.toString()}` : ''
  return request<PageData<SessionInfo>>(`/api/v2/sessions${suffix}`)
}

export function getSession(sessionId: string): Promise<SessionInfo> {
  return request<SessionInfo>(`/api/v2/sessions/${encodeURIComponent(sessionId)}`)
}

export function listTurns(
  sessionId: string,
  limit = 50,
  offset = 0,
): Promise<PageData<WebTurn>> {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  return request<PageData<WebTurn>>(
    `/api/v2/sessions/${encodeURIComponent(sessionId)}/turns?${query.toString()}`,
  )
}

/** 同步问答（D4）：480 秒超时（doc 2 §7.3）。 */
export function submitQuery(
  sessionId: string,
  question: string,
  sourceId: string,
): Promise<WebQueryResult> {
  return request<WebQueryResult>(
    `/api/v2/sessions/${encodeURIComponent(sessionId)}/query`,
    {
      method: 'POST',
      body: JSON.stringify({ question, source_id: sourceId }),
    },
    QUERY_TIMEOUT_MS,
  )
}

export function getResult(
  sessionId: string,
  resultRef: string,
  offset = 0,
  limit = 100,
): Promise<ResultPageData> {
  const query = new URLSearchParams({ offset: String(offset), limit: String(limit) })
  return request<ResultPageData>(
    `/api/v2/sessions/${encodeURIComponent(sessionId)}/results/${encodeURIComponent(resultRef)}?${query.toString()}`,
  )
}
