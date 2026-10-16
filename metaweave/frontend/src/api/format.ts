/**
 * Web UI 唯一的时间展示函数（doc 19.3 §4）。
 *
 * 只接受后端契约的 UTC 六位微秒、`Z` 结尾字符串，固定按北京时间
 * （Asia/Shanghai）显示为 `yyyy-MM-dd HH:mm:ss.SSSSSS`：不依赖浏览器时区、
 * 不手写加 8 小时。`Date` 只保留毫秒精度，因此解析用副本把小数截为三位，
 * 展示仍拼接原始六位小数（不依赖引擎对六位小数的扩展解析）。
 * 非法或解析失败的字符串返回 `—`，缺失值由调用方按原页面文案处理。
 */
const UTC_MICRO_RE = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.(\d{6})Z$/

export function displayDate(value: string): string {
  if (typeof value !== 'string' || !UTC_MICRO_RE.test(value)) return '—'
  const parseable = value.replace(/(\.\d{3})\d+(?=Z$)/, '$1')
  const date = new Date(parseable)
  if (Number.isNaN(date.getTime())) return '—'
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    hourCycle: 'h23',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).formatToParts(date)
  const part = (type: string): string =>
    parts.find((item) => item.type === type)?.value ?? ''
  const fraction = value.slice(-7, -1) // 原始六位小数（不重建）
  return `${part('year')}-${part('month')}-${part('day')} ${part('hour')}:${part('minute')}:${part('second')}.${fraction}`
}
