/**
 * NL2SQL 查询模块路由（doc 2 §6.1）。
 *
 * /query：选择数据源并新建对话；/query/:sessionId：恢复既有会话，
 * 由后端解析来源绑定（不信任 URL query 中的 source_id 覆盖绑定）。
 */
import type { RouteRecordRaw } from 'vue-router'
import QueryView from './views/QueryView.vue'

export const nl2sqlRoutes: RouteRecordRaw[] = [
  { path: '/query', name: 'nl2sql-query', component: QueryView },
  { path: '/query/:sessionId', name: 'nl2sql-query-session', component: QueryView },
]
