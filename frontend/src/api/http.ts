import type { ApiErrorResponse } from './types'
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
  not_found: '对象不存在',
  step_not_supported: '所选步骤暂不支持',
  step_not_ready: '步骤尚未采集成功',
  step_config_invalid: '步骤或参数不合法，请检查步骤与参数选择。',
  publish_precheck_failed: '发布预检未通过，本次请求未创建发布运行。',
  // doc 10.2 §5.1–§5.3：发布并行准入与双向保留 409（正常时服务端文案优先）
  source_publish_active: '该数据源已有进行中的发布运行，请等待完成或收尾后重试。',
  workspace_publish_active: '该工作区存在进行中的发布，请完成或收尾后重试。',
  workspace_collection_active: '该工作区存在进行中的采集，请完成或收尾后重试。',
  publish_capacity_exhausted: '发布容量已满，请稍后重试。',
  workspace_name_exists: '同一数据源下已存在同名工作区。',
  workspace_directory_failed: '工作区目录创建失败。',
  job_name_exists: '同一工作区内已存在同名作业。',
  source_has_workspaces: '该数据源已关联工作区，不能删除；请先人工处理工作区相关记录。',
  source_disabled: '该数据源已禁用。',
  rel_regenerated: 'Rel 产物已重新生成，请重新加载。',
  rel_write_failed: 'Rel 配置保存失败。',
  rel_config_output_missing: 'Rel 产物不存在；请先在本工作区运行 rel 采集步骤。',
  rel_config_invalid: 'Rel 关系配置不合法，请检查后重试。',
  domain_config_output_missing: 'Domain 配置产物不存在；请先在本工作区运行 domain 采集步骤。',
  domain_config_invalid: 'Domain 配置内容不合法。',
  domain_config_regenerated: 'Domain 配置已变更，请重新加载后重试。',
  domain_config_backup_failed: '备份失败，本次保存未写入任何内容，请重试。',
  domain_config_write_failed: '配置写回失败，已完成备份供人工核对。',
  dim_config_output_missing: 'Dim 配置产物不存在；请先在本工作区运行 dim 采集步骤。',
  dim_config_regenerated: 'Dim 配置已变更，请重新加载后重试。',
  dim_value_config_invalid: 'dim_tables.yaml 配置无效；请重跑 dim 步骤重新生成后再编辑。',
  dim_value_column_missing: '所选字段不存在于该对象的产物中。',
  publish_conflict: '发布发生并发冲突，请重新发布',
  password_required: '连接信息已修改，请填写密码后重试',
  output_delete_failed: '该数据源的输出目录删除失败，登记记录仍保留',
  source_busy: '该数据源有正在执行的采集或发布步骤，请稍后再试。',
  deletion_targets_changed: '存储目标已变化，请核对并确认是否继续。',
  resource_has_unfinished_runs: '存在未结束运行，请查看运行清单；状态更新后可重新确认。',
  run_resolve_active: '该运行正在本进程执行中，不能终止；若确认执行已中止，请重启后端后重试。',
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
    // details 兼容数组与对象形态（发布预检逐步骤原因、dim_value 拦截等），
    // 具体结构由各调用方按错误码自行收窄
    public readonly details: unknown = [],
    /** HTTP 状态码；网络失败等无响应场景为 0 */
    public readonly status = 0,
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
    const serverMessage = typeof error.message === 'string' ? error.message.trim() : ''
    const message =
      preferServerMessage && serverMessage ? serverMessage : messageForCode(code, mode)
    throw new ApiError(code, message, error.details, response.status)
  }
  return body as T
}
