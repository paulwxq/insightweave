<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { getAssetProfile } from '../api/assets'
import { displayDate } from '../api/format'
import type { AssetProfileDetail, AssetProfileResponse } from '../api/types'
import { formatSize } from '../utils/format'
import PageBreadcrumbs from '../components/PageBreadcrumbs.vue'

const PAGE_SIZE = 10
const route = useRoute()
const sourceId = computed(() => String(route.params.sourceId ?? ''))
const loading = ref(true)
const error = ref('')
const payload = ref<AssetProfileResponse | null>(null)
const page = ref(1)

const profile = computed(() => payload.value?.profile ?? null)
const schemas = computed(() => profile.value?.schemas ?? [])
const pageCount = computed(() => Math.max(1, Math.ceil(schemas.value.length / PAGE_SIZE)))
const safePage = computed(() => Math.min(page.value, pageCount.value))
const visibleSchemas = computed(() => {
  const start = (safePage.value - 1) * PAGE_SIZE
  return schemas.value.slice(start, start + PAGE_SIZE)
})
const pageNumbers = computed(() => Array.from({ length: pageCount.value }, (_, index) => index + 1))

watch(
  sourceId,
  () => {
    page.value = 1
    reload()
  },
  { immediate: true },
)

function schemaCaption(names: string[]): string {
  if (names.length === 1 && names[0] === 'public') return 'public · 默认 Schema'
  const shown = names.slice(0, 3).join(' · ')
  return names.length > 3 ? `${shown} 等 ${names.length} 个` : shown
}

function tableCaption(detail: AssetProfileDetail): string {
  return detail.totals.partitioned_table_count > 0 ? '普通表 · 分区表' : '普通表 · 无分区表'
}

function versionText(detail: AssetProfileDetail): string {
  return detail.server_version ? `${detail.db_type_label} ${detail.server_version}` : detail.db_type_label
}

async function reload() {
  loading.value = true
  error.value = ''
  try {
    payload.value = await getAssetProfile(sourceId.value)
  } catch (cause) {
    payload.value = null
    error.value = cause instanceof Error ? cause.message : '数据库画像加载失败。'
  } finally {
    loading.value = false
  }
}

</script>

<template>
  <div class="ds-page profile-page">
    <PageBreadcrumbs
      :segments="[
        { label: '数据资产', to: '/data-assets' },
        { label: payload?.source.display_name ?? '数据源' },
      ]"
    />
    <header class="ds-header">
      <div class="ds-title">
        <span class="ds-kicker">DATABASE PROFILE</span>
        <h1>数据库画像</h1>
        <p>元数据治理 · 数据库整体画像。数据来源：metaweave 采集作业（schema_version 1）。</p>
      </div>
      <div class="detail-actions">
        <RouterLink to="/data-assets" class="ds-jobs-back">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></svg>
          返回
        </RouterLink>
      </div>
    </header>

    <div v-if="loading" class="state-block">正在读取数据库画像…</div>
    <div v-else-if="error" class="state-block error-state">
      <p>{{ error }}</p>
      <button type="button" class="button button-outline" @click="reload">重试</button>
    </div>
    <div v-else-if="payload?.state === 'not_published'" class="state-block empty-state">
      <h3>尚未发布数据库画像</h3>
      <p>请先在工作区内完成采集并发布数据库画像（db 步骤）。</p>
    </div>
    <template v-else-if="payload?.state === 'ready' && profile">
      <section class="profile-facts" :key="payload.publish?.publish_run_id">
        <div class="profile-fact"><small>数据源名称</small><strong>{{ payload.source.display_name }}</strong></div>
        <div class="profile-fact"><small>数据库名称</small><strong>{{ profile.db_name }}</strong></div>
        <div class="profile-fact"><small>连接信息</small><strong>{{ profile.host }}:{{ profile.port }}</strong></div>
        <div class="profile-fact"><small>连接用户名</small><strong>{{ payload.source.username.trim() || '—' }}</strong></div>
        <div class="profile-fact"><small>数据库类型 / 版本</small><strong>{{ versionText(profile) }}</strong></div>
        <div class="profile-fact">
          <small>安装平台</small>
          <strong :title="profile.server_version_full || undefined">{{ profile.os_platform || '未知' }}</strong>
        </div>
        <div class="profile-fact"><small>记录行数</small><strong>{{ profile.record_count ?? '暂未提供' }}</strong></div>
        <div class="profile-fact"><small>最近采集时间</small><strong>{{ displayDate(profile.collected_at) }}</strong></div>
      </section>

      <section class="ds-metrics profile-metrics">
        <article class="ds-metric">
          <div class="ds-metric-main">
            <span class="ds-metric-icon ds-metric-icon-blue"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16" /><path d="M4 12h16" /><path d="M4 17h10" /></svg></span>
            <div><small>Schema</small><strong>{{ profile.selected_schemas.length }}</strong></div>
          </div>
          <p>{{ schemaCaption(profile.selected_schemas) }}</p>
        </article>
        <article class="ds-metric">
          <div class="ds-metric-main">
            <span class="ds-metric-icon ds-metric-icon-blue"><svg viewBox="0 0 24 24" aria-hidden="true"><rect width="18" height="14" x="3" y="5" rx="2" /><path d="M3 10h18" /></svg></span>
            <div><small>数据表</small><strong>{{ profile.totals.ordinary_table_count + profile.totals.partitioned_table_count }}</strong></div>
          </div>
          <p>{{ tableCaption(profile) }}</p>
        </article>
        <article class="ds-metric">
          <div class="ds-metric-main">
            <span class="ds-metric-icon ds-metric-icon-green"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v18" /><path d="m8 7 4-4 4 4" /><path d="m8 17 4 4 4-4" /></svg></span>
            <div><small>物化视图</small><strong>{{ profile.totals.materialized_view_count }}</strong></div>
          </div>
          <p>Materialized View（MV）</p>
        </article>
        <article class="ds-metric">
          <div class="ds-metric-main">
            <span class="ds-metric-icon ds-metric-icon-slate"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z" /><circle cx="12" cy="12" r="3" /></svg></span>
            <div><small>普通视图</small><strong>{{ profile.totals.view_count }}</strong></div>
          </div>
          <p>View · 非物化</p>
        </article>
        <article class="ds-metric">
          <div class="ds-metric-main">
            <span class="ds-metric-icon ds-metric-icon-slate"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m22 12-4-8H6L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2Z" /><path d="M6 12h.01" /><path d="M10 12h.01" /></svg></span>
            <div><small>数据库大小</small><strong>{{ formatSize(profile.size_bytes) }}</strong></div>
          </div>
          <p>{{ profile.size_bytes.toLocaleString('en-US') }} 字节 · 整库</p>
        </article>
      </section>

      <section class="catalog-card profile-detail">
        <div class="catalog-heading">
          <div>
            <div class="eyebrow">SCHEMA DETAIL</div>
            <h2>Schema 结构明细</h2>
            <p>按 Schema 统计的对象数量分布，含总计行。</p>
          </div>
          <span class="pill">{{ profile.selected_schemas.length }} 个 Schema</span>
        </div>
        <div class="profile-schema" role="table">
          <div class="profile-schema-row profile-schema-head" role="row">
            <span>SCHEMA</span>
            <span>数据表</span>
            <span>分区表</span>
            <span>分区子表</span>
            <span>视图</span>
            <span>物化视图</span>
            <span>对象合计</span>
          </div>
          <div v-for="schema in visibleSchemas" :key="schema.name" class="profile-schema-row" role="row">
            <RouterLink
              class="profile-schema-link"
              :to="{ name: 'source-objects', params: { sourceId }, query: { schema_name: schema.name } }"
            >
              {{ schema.name }}
            </RouterLink>
            <span>{{ schema.ordinary_table_count }}</span>
            <span>{{ schema.partitioned_table_count }}</span>
            <span>{{ schema.partition_child_count }}</span>
            <span>{{ schema.view_count }}</span>
            <span>{{ schema.materialized_view_count }}</span>
            <span>{{ schema.object_count }}</span>
          </div>
          <div class="profile-schema-row" role="row">
            <strong>合计（全部 Schema）</strong>
            <span>{{ profile.totals.ordinary_table_count }}</span>
            <span>{{ profile.totals.partitioned_table_count }}</span>
            <span>{{ profile.totals.partition_child_count }}</span>
            <span>{{ profile.totals.view_count }}</span>
            <span>{{ profile.totals.materialized_view_count }}</span>
            <span>{{ profile.totals.object_count }}</span>
          </div>
        </div>
        <div v-if="schemas.length > PAGE_SIZE" class="ds-jobs-pager">
          <span>共 {{ schemas.length }} 条记录 · 第 {{ safePage }} / {{ pageCount }} 页</span>
          <div>
            <button type="button" :disabled="safePage <= 1" aria-label="上一页" @click="page = safePage - 1">上一页</button>
            <button
              v-for="number in pageNumbers"
              :key="number"
              type="button"
              :class="{ current: number === safePage }"
              @click="page = number"
            >
              {{ number }}
            </button>
            <button type="button" :disabled="safePage >= pageCount" aria-label="下一页" @click="page = safePage + 1">下一页</button>
          </div>
        </div>
      </section>

      <div class="ds-job-links profile-next">
        <RouterLink :to="`/data-assets/${sourceId}/objects`">
          查看当前库表的画像
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14" /><path d="m12 5 7 7-7 7" /></svg>
        </RouterLink>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* doc 9 验收统一：返回按钮顶对齐标题首行（ds-header 全局为 flex-end 且与数据源列表共用，故本页局部覆盖） */
.ds-header {
  align-items: flex-start;
}
</style>
