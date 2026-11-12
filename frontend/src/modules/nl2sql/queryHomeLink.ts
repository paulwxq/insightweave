/**
 * 会话页「返回」使用的问数首页地址。
 * 只保留首页自己的筛选，不使用其他模块的 from。
 */
import { codepointLength, SESSION_KEYWORD_MAX } from './format.ts'

const SOURCE_ID = /^db_src_[1-9][0-9]*$/

export function queryHomeTarget(query: {
  source_id?: unknown
  q?: unknown
  page?: unknown
}): string {
  const params = new URLSearchParams()
  const sourceId = text(query.source_id)
  const keyword = text(query.q).trim()
  const page = text(query.page)
  if (SOURCE_ID.test(sourceId) && sourceId.length <= 64) params.set('source_id', sourceId)
  if (keyword !== '' && codepointLength(keyword) <= SESSION_KEYWORD_MAX) params.set('q', keyword)
  if (/^[1-9]\d*$/.test(page)) {
    const number = Number(page)
    if (number > 1 && number <= 10000) params.set('page', String(number))
  }
  const suffix = params.toString()
  return suffix === '' ? '/query' : `/query?${suffix}`
}

/** 缺少或不合法时回到不带筛选的首页。 */
export function sessionBackTarget(raw: unknown): string {
  if (typeof raw !== 'string' || !raw.startsWith('/query') || raw.startsWith('//')) return '/query'
  let url: URL
  try {
    url = new URL(raw, 'http://localhost')
  } catch {
    return '/query'
  }
  if (url.pathname !== '/query' || url.username !== '' || url.password !== '') return '/query'
  return queryHomeTarget({
    source_id: url.searchParams.get('source_id') ?? '',
    q: url.searchParams.get('q') ?? '',
    page: url.searchParams.get('page') ?? '',
  })
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : ''
}
