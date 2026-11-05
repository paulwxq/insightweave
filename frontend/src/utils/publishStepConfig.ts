// doc 7.2 §3.1/§3.2：发布步骤 dim_value/domain 配置的形态窄化与显式初始化转换。
// 执行页与作业编辑器共用：step_config 中的版本默认配置读取（数组形态），
// 以及"从当前配置文件初始化草稿"的 GET 结果转换（字符串 → 单元素数组；
// 含逗号字符串按"未指定"转空数组并提示改用字段列表）。
import type {
  DimValueConfigTable,
  DimValueTableParam,
  DomainConfigDomain,
  DomainParam,
} from '../api/types'

export function asStringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : []
}

/** 从 step_config 读取 dim_value.tables；无配置或旧结构返回 null（区别于空清单） */
export function parseDimTables(
  stepConfig: Record<string, unknown> | null | undefined,
): DimValueTableParam[] | null {
  const sub = stepConfig?.dim_value
  if (typeof sub !== 'object' || sub === null) return null
  const tables = (sub as { tables?: unknown }).tables
  if (!Array.isArray(tables)) return null
  return tables.flatMap((row) => {
    if (typeof row !== 'object' || row === null) return []
    const item = row as Record<string, unknown>
    if (typeof item.schema_name !== 'string' || typeof item.object_name !== 'string') return []
    return [
      {
        schema_name: item.schema_name,
        object_name: item.object_name,
        embedding_col: asStringList(item.embedding_col),
      },
    ]
  })
}

/** 从 step_config 读取 domain.domains；无配置或旧结构返回 null（区别于空清单） */
export function parseDomains(
  stepConfig: Record<string, unknown> | null | undefined,
): DomainParam[] | null {
  const sub = stepConfig?.domain
  if (typeof sub !== 'object' || sub === null) return null
  const domains = (sub as { domains?: unknown }).domains
  if (!Array.isArray(domains)) return null
  return domains.flatMap((row) => {
    if (typeof row !== 'object' || row === null) return []
    const item = row as Record<string, unknown>
    if (typeof item.name !== 'string' || typeof item.description !== 'string') return []
    return [
      {
        name: item.name,
        description: item.description,
        tables: asStringList(item.tables),
      },
    ]
  })
}

/**
 * 显式初始化：配置文件 GET 的 dim 表清单 → 草稿数组形态（doc 7.2 §3.2，
 * 仅在用户点击"从当前配置初始化"时执行，不是运行时回退）。
 * 单列字符串 → 单元素数组；字段列表原样保留；null/空 → 空数组。
 * 含逗号字符串已被后端读取归一化为 null（按未指定）；对应表名由 GET 响应的
 * comma_string_tables 独立携带，调用方据此提示"请使用字段列表格式"。
 */
export function dimDraftFromConfig(tables: DimValueConfigTable[]): DimValueTableParam[] {
  return tables.map((item) => {
    let cols: string[] = []
    if (Array.isArray(item.embedding_col)) {
      cols = asStringList(item.embedding_col).filter((col) => !!col.trim())
    } else if (typeof item.embedding_col === 'string' && item.embedding_col.trim()) {
      // 单列字符串 → 单元素数组（含逗号串已被后端读取归一化为 null，不会到达这里）
      cols = [item.embedding_col.trim()]
    }
    return {
      schema_name: item.schema_name,
      object_name: item.object_name,
      embedding_col: cols,
    }
  })
}

/** 比较实际执行配置；表顺序和字段选择顺序不视为用户修改。 */
export function sameDimTables(left: DimValueTableParam[], right: DimValueTableParam[]): boolean {
  const normalize = (tables: DimValueTableParam[]) => tables.map((table) => ({
    schema_name: table.schema_name,
    object_name: table.object_name,
    embedding_col: [...new Set(table.embedding_col)].sort(),
  })).sort((a, b) => a.schema_name.localeCompare(b.schema_name) || a.object_name.localeCompare(b.object_name))
  return JSON.stringify(normalize(left)) === JSON.stringify(normalize(right))
}

/** 显式初始化：配置文件 GET 的 domain 清单 → 草稿（深拷贝，形态本就一致） */
export function domainDraftFromConfig(domains: DomainConfigDomain[]): DomainParam[] {
  return domains.map((item) => ({
    name: item.name,
    description: item.description,
    tables: [...item.tables],
  }))
}
