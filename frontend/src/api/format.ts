/**
 * Web UI 时间展示函数。
 *
 * 只接受后端契约的 UTC 六位微秒、`Z` 结尾字符串，固定按北京时间
 * （Asia/Shanghai）显示：不依赖浏览器时区、不手写加 8 小时。
 * `Date` 只保留毫秒精度，因此解析用副本把小数截为三位，
 * 展示仍拼接原始小数（不依赖引擎对六位小数的扩展解析）。
 * 非法或解析失败的字符串返回 `—`，缺失值由调用方按原页面文案处理。
 *
 * - `displayDate`：全精度 `yyyy-MM-dd HH:mm:ss.SSSSSS`（默认口径）；
 * - `displayDateMs`：毫秒 `yyyy-MM-dd HH:mm:ss.SSS`（运行记录等紧凑列表）。
 */
const UTC_MICRO_RE = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.(\d{6})Z$/

export function displayDate(value: string): string {
  if (typeof value !== 'string' || !UTC_MICRO_RE.test(value)) return '—'
  const parseable = value.replace(/(\.\d{3})\d+(?=Z$)/, '$1')
  const date = new Date(parseable)
  if (Number.isNaN(date.getTime())) return '—'
  const parts = formatShanghaiParts(date)
  const part = (type: string): string =>
    parts.find((item) => item.type === type)?.value ?? ''
  const fraction = value.slice(-7, -1) // 原始六位小数（不重建）
  return `${part('year')}-${part('month')}-${part('day')} ${part('hour')}:${part('minute')}:${part('second')}.${fraction}`
}

/** 毫秒口径：与 displayDate 同一解析链路，小数只拼前三位（截断，不四舍五入）。 */
export function displayDateMs(value: string): string {
  if (typeof value !== 'string' || !UTC_MICRO_RE.test(value)) return '—'
  const parseable = value.replace(/(\.\d{3})\d+(?=Z$)/, '$1')
  const date = new Date(parseable)
  if (Number.isNaN(date.getTime())) return '—'
  const parts = formatShanghaiParts(date)
  const part = (type: string): string =>
    parts.find((item) => item.type === type)?.value ?? ''
  const fraction = value.slice(-7, -4) // 原始小数前三位（毫秒）
  return `${part('year')}-${part('month')}-${part('day')} ${part('hour')}:${part('minute')}:${part('second')}.${fraction}`
}

function formatShanghaiParts(date: Date) {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    hourCycle: 'h23',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).formatToParts(date)
}

/**
 * 宽松时间展示：先按 displayDate 的严格契约，失败时回退为通用 ISO 解析
 * （doc 6 §4.3：发布页 collected_at 为上海时区 `+08:00` 的 ISO 文本）。
 * 回退分支按北京时间展示到秒；无法解析返回 `—`。
 */
export function displayDateLoose(value: string | null | undefined): string {
  if (typeof value !== 'string' || value === '') return '—'
  const strict = displayDate(value)
  if (strict !== '—') return strict
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  const parts = formatShanghaiParts(date)
  const part = (type: string): string =>
    parts.find((item) => item.type === type)?.value ?? ''
  return `${part('year')}-${part('month')}-${part('day')} ${part('hour')}:${part('minute')}:${part('second')}`
}
