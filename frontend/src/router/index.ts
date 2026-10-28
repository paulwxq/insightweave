import { createRouter, createWebHistory } from 'vue-router'
import HomeView from '../views/HomeView.vue'
import DataAssetsView from '../views/DataAssetsView.vue'
import DataSourcesView from '../views/DataSourcesView.vue'
import CreateDataSourceView from '../views/CreateDataSourceView.vue'
import DataSourceDetailView from '../views/DataSourceDetailView.vue'
import DatabaseProfileView from '../views/DatabaseProfileView.vue'
import ObjectListView from '../views/ObjectListView.vue'
import ObjectDetailView from '../views/ObjectDetailView.vue'
import WorkspacesView from '../views/WorkspacesView.vue'
import CreateWorkspaceView from '../views/CreateWorkspaceView.vue'
import WorkspaceHomeView from '../views/WorkspaceHomeView.vue'
import WorkspaceJobListView from '../views/WorkspaceJobListView.vue'
import GlobalJobListView from '../views/GlobalJobListView.vue'
import CreateJobView from '../views/CreateJobView.vue'
import JobDetailView from '../views/JobDetailView.vue'
import RunExecuteView from '../views/RunExecuteView.vue'
import NotFoundView from '../views/NotFoundView.vue'
import { nl2sqlRoutes } from '../modules/nl2sql/routes'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'home', component: HomeView },
    { path: '/data-assets', name: 'data-assets', component: DataAssetsView },
    {
      path: '/data-assets/:sourceId/profile',
      name: 'database-profile',
      component: DatabaseProfileView,
    },
    {
      path: '/data-assets/:sourceId/objects',
      name: 'source-objects',
      component: ObjectListView,
    },
    {
      path: '/data-assets/:sourceId/objects/detail',
      name: 'source-object-detail',
      component: ObjectDetailView,
    },
    { path: '/data-sources', name: 'data-sources', component: DataSourcesView },
    { path: '/data-sources/new', name: 'create-data-source', component: CreateDataSourceView },
    { path: '/data-sources/:sourceId/edit', name: 'edit-data-source', component: CreateDataSourceView },
    // 数据源详情（doc 6.3 §4：目标详情路由为 /data-sources/:sourceId）
    { path: '/data-sources/:sourceId', name: 'source-detail', component: DataSourceDetailView },
    // 旧源内工作区路由重定向到跨源列表的源过滤视图（doc 6.3 §4.6）
    {
      path: '/data-sources/:sourceId/workspaces',
      redirect: (to) => `/workspaces?source_id=${to.params.sourceId}`,
    },
    // 工作区管理：跨源列表 + 新建（doc 6.3 §4 编号 21/22）
    { path: '/workspaces', name: 'workspaces', component: WorkspacesView },
    { path: '/workspaces/new', name: 'workspace-new', component: CreateWorkspaceView },
    // 全局作业列表（设计稿 35/37：不带工作区上下文，源/工作区下拉筛选）
    {
      path: '/collection-jobs',
      name: 'global-collection-jobs',
      component: GlobalJobListView,
      props: { kind: 'collection' },
    },
    {
      path: '/publish-jobs',
      name: 'global-publish-jobs',
      component: GlobalJobListView,
      props: { kind: 'publish' },
    },
    // 工作区（doc 6 四层模型：工作区 → 作业 → 版本 → 运行）
    { path: '/workspaces/:workspaceId', name: 'workspace-home', component: WorkspaceHomeView },
    {
      path: '/workspaces/:workspaceId/collection',
      name: 'workspace-collection',
      component: WorkspaceJobListView,
      props: { kind: 'collection' },
    },
    {
      path: '/workspaces/:workspaceId/collection/new',
      name: 'workspace-collection-new',
      component: CreateJobView,
      props: { kind: 'collection' },
    },
    {
      path: '/workspaces/:workspaceId/collection/jobs/:jobId',
      name: 'collection-job-detail',
      component: JobDetailView,
      props: { kind: 'collection' },
    },
    // 执行采集作业（doc 6.3 §4 编号 29：query 携带 revision_no 与 mode）
    {
      path: '/workspaces/:workspaceId/collection/jobs/:jobId/run',
      name: 'collection-job-run',
      component: RunExecuteView,
      props: { kind: 'collection' },
    },
    {
      path: '/workspaces/:workspaceId/publish',
      name: 'workspace-publish',
      component: WorkspaceJobListView,
      props: { kind: 'publish' },
    },
    {
      path: '/workspaces/:workspaceId/publish/new',
      name: 'workspace-publish-new',
      component: CreateJobView,
      props: { kind: 'publish' },
    },
    {
      path: '/workspaces/:workspaceId/publish/jobs/:jobId',
      name: 'publish-job-detail',
      component: JobDetailView,
      props: { kind: 'publish' },
    },
    // 执行发布作业（doc 6.3 §4 编号 31）
    {
      path: '/workspaces/:workspaceId/publish/jobs/:jobId/run',
      name: 'publish-job-run',
      component: RunExecuteView,
      props: { kind: 'publish' },
    },
    ...nl2sqlRoutes,
    // 未知路由兜底（doc 6.3 §8.1 全局错误页）
    { path: '/:pathMatch(.*)*', name: 'not-found', component: NotFoundView },
  ],
})
