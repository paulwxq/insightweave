import { Nl2sqlApiError, Nl2sqlNotConfiguredError } from './api/client'

/** 服务端 error_code → 用户可见文案（安全化，不回显内部细节）。 */
export const NL2SQL_MESSAGES: Record<string, string> = {
  invalid_request: '请检查提交内容后重试。',
  session_not_found: '会话不存在，请刷新列表。',
  source_not_found: '登记的数据源不存在。',
  result_not_found: '结果不存在或不可访问。',
  route_not_found: '接口不存在。',
  method_not_allowed: '请求方法不支持。',
  session_busy: '本次未执行，会话正在处理另一项操作，请稍后手动重试。',
  request_id_conflict: '同会话运行标识冲突，请重试。',
  session_metadata_invalid: '会话展示信息不完整，可刷新；若无法恢复请新建会话。',
  source_inactive: '数据源已停用，请新建会话选择其他来源。',
  source_changed: '来源连接或范围已变化，请新建会话。',
  source_mismatch: '请求来源与会话绑定的来源不一致。',
  session_binding_missing: '会话状态字段缺失或矛盾，不能续聊。',
  knowledge_changed: '知识版本已变化，本轮一致性未确认，请新建会话。',
  session_invalidated: '会话已失效，请新建会话。',
  source_not_ready: '数据源暂未就绪，请检查发布状态后重试。',
  source_unsupported: '数据源类型暂不支持。',
  source_config_invalid: '数据源配置不完整，请在数据源页修正。',
  origin_not_allowed: '来源不在允许列表。',
  host_not_allowed: 'Host 不在允许列表。',
  unsupported_media_type: '写接口仅接受 JSON 请求。',
  query_capacity_exceeded: '服务繁忙，请稍后重试。',
  rate_limited: '提交过于频繁，请稍后重试。',
  lock_backend_error: '锁服务暂时不可用，请稍后重试。',
  source_registry_unavailable: '登记服务暂时不可用，请稍后重试。',
  dependency_unavailable: '依赖服务暂时不可用，请稍后重试。',
  history_unavailable: '历史读取暂时不可用，请稍后重试。',
  history_record_invalid: '历史记录损坏，无法安全读取。',
  result_integrity_error: '结果完整性校验失败。',
  result_encoding_error: '结果编码失败。',
  internal_error: '服务暂时无法处理请求。',
  network_error: '无法连接 NL2SQL 服务，请检查后端是否已启动。',
  timeout: '结果尚未确认，后端可能仍在执行；请勿重复提交，稍后在历史中查看。',
  invalid_response: 'NL2SQL 服务返回了无法识别的响应。',
  not_configured: '未配置 VITE_NL2SQL_API_BASE_URL，NL2SQL 查询不可用。',
}

export function messageForNl2sql(error: Nl2sqlApiError | Nl2sqlNotConfiguredError): string {
  const preset = NL2SQL_MESSAGES[error.code]
  if (preset) return preset
  if (error.message && error.code !== 'internal_error') return error.message
  return NL2SQL_MESSAGES.internal_error
}
