/**
 * NL2SQL 模块内展示格式化。
 *
 * 后端时间为带时区 ISO 8601（+08:00 偏移），与 MetaWeave 的 UTC 微秒格式
 * 不同；不共用 displayDate（其正则只接受 `…Z` 六位微秒）。按北京时间展示。
 */
export function formatNl2sqlTime(value: string | null | undefined, withSeconds = false): string {
  if (typeof value !== 'string' || value === '') return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    hourCycle: 'h23',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    ...(withSeconds ? { second: '2-digit' as const } : {}),
  }).formatToParts(date)
  const part = (type: string): string =>
    parts.find((item) => item.type === type)?.value ?? ''
  const clock = `${part('hour')}:${part('minute')}${withSeconds ? `:${part('second')}` : ''}`
  return `${part('year')}-${part('month')}-${part('day')} ${clock}`
}

/** 问题长度按 Unicode 码点计（doc 2 §4.2：1–1000）。 */
export function codepointLength(value: string): number {
  return Array.from(value).length
}

interface ShanghaiClock {
  key: string
  hour: string
  minute: string
  month: string
  day: string
}

function shanghaiClock(date: Date): ShanghaiClock | null {
  if (Number.isNaN(date.getTime())) return null
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    hourCycle: 'h23',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).formatToParts(date)
  const part = (type: string): string => parts.find((item) => item.type === type)?.value ?? ''
  const year = part('year')
  const month = part('month')
  const day = part('day')
  if (!year || !month || !day) return null
  return { key: `${year}-${month}-${day}`, hour: part('hour'), minute: part('minute'), month, day }
}

function dayKeyOffset(key: string, days: number): string {
  const [year, month, day] = key.split('-').map((item) => Number(item))
  const date = new Date(Date.UTC(year, month - 1, day))
  date.setUTCDate(date.getUTCDate() + days)
  const y = date.getUTCFullYear()
  const m = String(date.getUTCMonth() + 1).padStart(2, '0')
  const d = String(date.getUTCDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}

/** 会话标题搜索上限，与后端 q 的码点上限一致。 */
export const SESSION_KEYWORD_MAX = 100

/** 问题长度上限（doc 2 §4.2：1–1000 码点），输入组件与发送入口共用。 */
export const QUESTION_MAX = 1000

/** 整理后的搜索词；超限返回提示，不截短。空字符串表示不搜索。 */
export function sessionKeyword(raw: string): { keyword: string; error: string } {
  const keyword = raw.trim()
  if (codepointLength(keyword) > SESSION_KEYWORD_MAX) {
    return { keyword: '', error: '关键字最多 100 个字符' }
  }
  return { keyword, error: '' }
}

/** 消息区日期分隔：今天 14:20 / 昨天 18:07 / 10月16日 09:41。 */
export function formatTurnDivider(value: string | null | undefined, now = new Date()): string {
  if (typeof value !== 'string' || value === '') return '今天'
  const date = new Date(value)
  const clock = shanghaiClock(date)
  const today = shanghaiClock(now)
  if (!clock || !today) return '今天'
  const hm = `${clock.hour}:${clock.minute}`
  if (clock.key === today.key) return `今天 ${hm}`
  if (clock.key === dayKeyOffset(today.key, -1)) return `昨天 ${hm}`
  return `${Number(clock.month)}月${Number(clock.day)}日 ${hm}`
}

/** 头部「更新于 14:32」。 */
export function formatClock(value: string | null | undefined): string {
  if (typeof value !== 'string' || value === '') return ''
  const clock = shanghaiClock(new Date(value))
  if (!clock) return ''
  return `${clock.hour}:${clock.minute}`
}

/** 结果卡「耗时 0.38 秒」。 */
export function formatElapsed(elapsedMs: number | null | undefined): string {
  if (typeof elapsedMs !== 'number' || !Number.isFinite(elapsedMs) || elapsedMs < 0) return ''
  const seconds = elapsedMs / 1000
  const text = seconds < 10 ? seconds.toFixed(2) : seconds.toFixed(1)
  return `耗时 ${text} 秒`
}
