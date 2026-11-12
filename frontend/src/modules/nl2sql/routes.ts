/**
 * NL2SQL 查询模块路由（doc 10）。
 *
 * /query：会话首页。/query/new：未绑定的新建页，可按地址预选数据源。
 * /query/:sessionId：恢复既有会话，由后端解析来源绑定，不信任 URL 中的 source_id。
 */
import type { RouteRecordRaw } from 'vue-router'
import QueryHomeView from './views/QueryHomeView.vue'
import QueryView from './views/QueryView.vue'

export const nl2sqlRoutes: RouteRecordRaw[] = [
  { path: '/query', name: 'nl2sql-home', component: QueryHomeView },
  { path: '/query/new', name: 'nl2sql-query', component: QueryView },
  { path: '/query/:sessionId', name: 'nl2sql-query-session', component: QueryView },
]
