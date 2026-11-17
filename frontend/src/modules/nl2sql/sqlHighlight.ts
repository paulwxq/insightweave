/** SQL 展开区的轻量着色。不使用 v-html，按记号拆开后由组件上色。 */

export type SqlTokenKind = 'keyword' | 'as' | 'string' | 'number' | 'text'

export interface SqlToken {
  text: string
  kind: SqlTokenKind
}

const KEYWORDS = new Set([
  'SELECT',
  'FROM',
  'WHERE',
  'GROUP',
  'BY',
  'HAVING',
  'ORDER',
  'LIMIT',
  'OFFSET',
  'JOIN',
  'INNER',
  'LEFT',
  'RIGHT',
  'FULL',
  'OUTER',
  'CROSS',
  'ON',
  'AND',
  'OR',
  'NOT',
  'IN',
  'IS',
  'NULL',
  'LIKE',
  'BETWEEN',
  'DISTINCT',
  'UNION',
  'ALL',
  'CASE',
  'WHEN',
  'THEN',
  'ELSE',
  'END',
  'ASC',
  'DESC',
  'INTERVAL',
  'CURRENT_DATE',
  'CURRENT_TIMESTAMP',
  'COUNT',
  'SUM',
  'AVG',
  'MIN',
  'MAX',
  'CAST',
  'WITH',
])

const TOKEN =
  /('(?:''|[^'])*'|"(?:[^"]|"")*"|--[^\n]*|\/\*[\s\S]*?\*\/|\b\d+(?:\.\d+)?\b|[A-Za-z_][A-Za-z0-9_]*|\s+|.)/g

export function highlightSql(sql: string): SqlToken[] {
  const tokens: SqlToken[] = []
  for (const match of sql.matchAll(TOKEN)) {
    const text = match[0]
    if (!text) continue
    if (text.startsWith("'") || text.startsWith('"') || text.startsWith('--') || text.startsWith('/*')) {
      tokens.push({ text, kind: 'string' })
      continue
    }
    if (/^\d/.test(text)) {
      tokens.push({ text, kind: 'number' })
      continue
    }
    if (/^[A-Za-z_]/.test(text)) {
      const upper = text.toUpperCase()
      if (upper === 'AS') tokens.push({ text, kind: 'as' })
      else if (KEYWORDS.has(upper)) tokens.push({ text, kind: 'keyword' })
      else tokens.push({ text, kind: 'text' })
      continue
    }
    tokens.push({ text, kind: 'text' })
  }
  return tokens
}
