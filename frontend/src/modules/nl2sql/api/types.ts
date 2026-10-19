/**
 * NL2SQL Web API（/api/v2）DTO（doc 2 §4.2/§4.5）。
 *
 * 与后端 src/api/v2/dto.py 显式字段一一对应；统一封装
 * {code, message, data, trace_id} 的 data 部分即各业务类型。
 * 不用任意 dict 静默增减字段；新增值须与后端闭集同步。
 */

/** 统一响应封装；code 为 HTTP 状态数字值（doc 2 §4.2）。 */
export interface Envelope<T> {
  code: number
  message: string
  data: T
  trace_id: string
}

/** 非 200 标准 data：至少 {error_code, request_id, run_id}。 */
export interface ErrorData {
  error_code: string
  request_id: string | null
  run_id: string | null
  [key: string]: unknown
}

export type SessionState = 'active' | 'invalidated'
export type InvalidReason =
  | 'knowledge_changed'
  | 'dependency_unavailable'
  | 'source_changed'
  | 'session_binding_missing'

/** 会话信息（doc 2 §4.2）；state/continuable 由服务端投影。 */
export interface SessionInfo {
  session_id: string
  source_id: string
  title: string
  created_at: string
  last_active_at: string
  turn_count: number
  state: SessionState
  continuable: boolean
  invalid_reason: InvalidReason | null
  invalidated_at: string | null
}

/** 历史轮次回放用的证据投影：只含画表格和展开 SQL 所需字段。 */
export interface TurnEvidenceView {
  sql: string | null
  result_ref: string | null
  row_count: number
  source_truncated: boolean
}

/**
 * 历史轮次。旧记录没有展示投影时 evidence 为空、elapsed_ms 为空，只展示问答。
 * 新建查询写入的轮次带回 SQL 与 result_ref，再次打开会话可回放表格。
 */
export interface WebTurn {
  question: string
  answer: string
  saved_at?: string | null
  elapsed_ms?: number | null
  evidence?: TurnEvidenceView[]
}

/** 会话/轮次分页 data：{items, limit, offset, has_more, next_offset}。 */
export interface PageData<T> {
  items: T[]
  limit: number
  offset: number
  has_more: boolean
  next_offset: number | null
}

// --------------------------------------------------------------- 查询响应

export interface WebFinding {
  text: string
  evidence_ids: string[]
}

export interface WebEvidenceOrigin {
  evidence_id: string
  thread_id: string
  request_id: string
  run_id: string
  datasource_id: string
  user_text: string | null
}

export interface WebEvidenceItem {
  evidence_id: string
  kind: string
  tool_call_id: string
  source: string
  sql: string | null
  sql_template: string | null
  result_ref: string | null
  input_result_refs: string[]
  operation: Record<string, unknown> | null
  row_count: number
  source_truncated: boolean
  warnings: string[]
  reused_from: WebEvidenceOrigin | null
  result_generated_at: string
}

export interface WebRunFailure {
  phase: string
  kind: string
  message: string
  primary: boolean
}

export interface WebStats {
  elapsed_ms: number
  model_calls: number
  summary_model_calls: number
  total_model_calls: number
  tool_calls: number
  input_tokens: number | null
  output_tokens: number | null
  token_usage_complete: boolean
}

/** 当前轮查询响应（doc 2 §4.2 字段表，均必返）。 */
export interface WebQueryResult {
  session_id: string
  thread_id: string | null
  request_id: string
  run_id: string
  status: string
  answer: string
  findings: WebFinding[]
  evidence: WebEvidenceItem[]
  metric_definitions: string[]
  limitations: string[]
  open_questions: string[]
  errors: WebRunFailure[]
  warnings: string[]
  archived: boolean
  stats: WebStats
  source_id: string
  history_saved: boolean | null
  error_code: string | null
}

// --------------------------------------------------------------- 结果读取

/** v2 单元格信封（doc 2 §4.5）：{kind, value}，value 类型由 kind 决定。 */
export interface CellV2 {
  kind: string
  value: unknown
}

export interface ResultColumn {
  name: string
  pg_type: string
}

/** binary 单元格的 value 形态：{length, content_saved:false}。 */
export interface BinaryCellValue {
  length: number
  content_saved: boolean
}

export interface ResultPageData {
  columns: ResultColumn[]
  rows: CellV2[][]
  offset: number
  limit: number
  next_offset: number | null
  has_more: boolean
  saved_row_count: number
  source_truncated: boolean
}

// --------------------------------------------------------------- 就绪检查

export interface ReadinessCheckItem {
  name: string
  required: boolean
  state: 'pass' | 'warn' | 'fail'
  error_code: string | null
  message: string
  counts: Record<string, number> | null
}

export interface ReadinessData {
  source_id: string
  compatible: boolean | null
  ready: boolean
  checked_at: string
  checks: ReadinessCheckItem[]
}
