import { createRouter, createWebHistory, type RouteLocationGeneric } from 'vue-router'
import HomeView from '../views/HomeView.vue'
import DataAssetsView from '../views/DataAssetsView.vue'
import DataSourcesView from '../views/DataSourcesView.vue'
import CreateDataSourceView from '../views/CreateDataSourceView.vue'
import DataSourceDetailView from '../views/DataSourceDetailView.vue'
import CollectionJobsView from '../views/CollectionJobsView.vue'
import CreateCollectionJobView from '../views/CreateCollectionJobView.vue'
import PublishView from '../views/PublishView.vue'
import PublishJobsView from '../views/PublishJobsView.vue'
import SelectPublishJobView from '../views/SelectPublishJobView.vue'
import DatabaseProfileView from '../views/DatabaseProfileView.vue'
import ObjectListView from '../views/ObjectListView.vue'
import ObjectDetailView from '../views/ObjectDetailView.vue'
import CollectionHomeView from '../views/CollectionHomeView.vue'
import PublishHomeView from '../views/PublishHomeView.vue'

function keepQuery(path: string) {
  return (to: RouteLocationGeneric) => ({ path, query: to.query, hash: to.hash })
}

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
    { path: '/data-collection', name: 'collection-home', component: CollectionHomeView },
    {
      path: '/data-collection/:sourceId/new',
      name: 'collection-home-new',
      component: CreateCollectionJobView,
    },
    { path: '/profile-publishing', name: 'publish-home', component: PublishHomeView },
    {
      path: '/profile-publishing/:sourceId/new',
      name: 'publish-home-new',
      component: SelectPublishJobView,
    },
    {
      path: '/profile-publishing/:sourceId/jobs/:jobId/publish',
      name: 'publish-home-run',
      component: PublishView,
    },
    { path: '/data-sources', name: 'data-sources', component: DataSourcesView },
    { path: '/data-sources/new', name: 'create-data-source', component: CreateDataSourceView },
    { path: '/data-sources/:sourceId/edit', name: 'edit-data-source', component: CreateDataSourceView },
    {
      path: '/data-sources/:sourceId/collection-jobs/new',
      name: 'create-collection-job',
      component: CreateCollectionJobView,
    },
    {
      path: '/data-sources/:sourceId/collection-jobs/:jobId/publish',
      name: 'publish-job',
      component: PublishView,
    },
    {
      path: '/data-sources/:sourceId/collection-jobs',
      name: 'collection-jobs',
      component: CollectionJobsView,
    },
    {
      path: '/data-sources/:sourceId/publish-jobs/new',
      name: 'new-publish',
      component: SelectPublishJobView,
    },
    {
      path: '/data-sources/:sourceId/publish-jobs',
      name: 'publish-jobs',
      component: PublishJobsView,
    },
    { path: '/data-sources/:sourceId', name: 'data-source-detail', component: DataSourceDetailView },
    { path: '/data-assets/new', redirect: keepQuery('/data-sources/new') },
    {
      path: '/data-assets/:sourceId/edit',
      redirect: (to) => keepQuery(`/data-sources/${to.params.sourceId}/edit`)(to),
    },
    {
      path: '/data-assets/:sourceId',
      redirect: (to) => keepQuery(`/data-sources/${to.params.sourceId}`)(to),
    },
  ],
})
