import type { ApiErrorResponse, DuplicateSourceDetail } from './types'
import { validateApiBaseUrl } from './apiBaseUrl'

const API_BASE_URL = validateApiBaseUrl(import.meta.env.VITE_API_BASE_URL)

const ERROR_MESSAGES: Record<string, string> = {
  invalid_request: '请检查填写的信息。',
  unsupported_postgresql_version: '仅支持 PostgreSQL 15.0 及以上。',
  schema_missing: '所选 Schema 已不存在，请重新测试连接。',
  schema_no_usage: '当前账号无权访问所选 Schema。',
  schema_not_selectable: '系统或临时 Schema 不能登记。',
  connection_failed: '源库连接或查询失败，请检查连接信息。',
  platform_unavailable: '平台库暂不可用，请稍后重试。',
  duplicate_source: '已有相同连接信息的数据源，请确认是否仍要新建。',
  not_found: '数据源不存在',
  step_not_supported: '所选步骤暂不支持',
  step_not_ready: '步骤尚未采集成功',
  publish_conflict: '发布发生并发冲突，请重新发布',
  source_disabled: '该数据源已禁用，不能创建新的采集作业或画像。',
  password_required: '连接信息已修改，请填写密码后重试',
  output_delete_failed: '该数据源的输出目录删除失败，登记记录仍保留',
  source_busy: '该数据源有正在执行的采集或发布步骤，请稍后再试。',
  internal_error: '服务暂时无法处理请求。',
}

export const DUPLICATE_SOURCE_MESSAGE = {
  create: '已有相同连接信息的数据源，请确认是否仍要新建。',
  edit: '已有相同连接信息的数据源，请确认是否仍要保存。',
} as const

export type FormMode = keyof typeof DUPLICATE_SOURCE_MESSAGE

export function messageForCode(code: string, mode: FormMode = 'create'): string {
  if (code === 'duplicate_source') return DUPLICATE_SOURCE_MESSAGE[mode]
  return ERROR_MESSAGES[code] ?? '请求失败，请稍后重试。'
}

export class ApiError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    // doc 22.2 审核修订 36/49：details 兼容对象形态（含空对象）——只要后端给的是
    // 对象就原样保留，dim_value 拦截的 {reason}/{tables} 才能到组件
    public readonly details: DuplicateSourceDetail[] | Record<string, unknown> = [],
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

export async function apiRequest<T>(
  path: string,
  init: RequestInit = {},
  mode: FormMode = 'create',
  preferServerMessage = false,
): Promise<T> {
  let response: Response
  try {
    response = await fetch(new URL(path, API_BASE_URL), {
      ...init,
      headers: { 'Content-Type': 'application/json', ...init.headers },
    })
  } catch {
    throw new ApiError('network_error', '无法连接平台服务，请检查后端是否已启动。')
  }

  if (response.status === 204) {
    return undefined as T
  }

  let body: unknown
  try {
    body = await response.json()
  } catch {
    throw new ApiError('invalid_response', '平台服务返回了无法识别的响应。')
  }
  if (!response.ok) {
    const error = body as Partial<ApiErrorResponse>
    const code = typeof error.code === 'string' ? error.code : 'internal_error'
    const rawDetails = error.details
    const details: DuplicateSourceDetail[] | Record<string, unknown> = Array.isArray(rawDetails)
      ? rawDetails
      : typeof rawDetails === 'object' && rawDetails !== null
        ? (rawDetails as Record<string, unknown>)
        : []
    const serverMessage = typeof error.message === 'string' ? error.message.trim() : ''
    const message =
      preferServerMessage && serverMessage ? serverMessage : messageForCode(code, mode)
    throw new ApiError(code, message, details)
  }
  return body as T
}
