<script setup lang="ts">
import PlaceholderPanel from '../components/PlaceholderPanel.vue'
</script>

<template>
  <div class="home-page">
    <section class="hero-card">
      <div class="hero-copy">
        <span class="eyebrow">METAWEAVE · DATA ANALYTICS PLATFORM</span>
        <h1><span>MetaWeave</span><br />数据分析系统</h1>
        <p class="hero-lead">让数据，成为可发现、可理解的资产。</p>
        <p class="hero-subline">从登记 PostgreSQL 数据源开始，逐步构建数据资产目录。</p>
        <RouterLink to="/data-assets" class="button button-primary hero-button">进入数据资产 <span>→</span></RouterLink>
        <div class="hero-points"><span>● 本地优先</span><span>● 数据源登记</span><span>● 工作区隔离产物</span></div>
      </div>
      <div class="hero-illustration" aria-hidden="true">
        <div class="illustration-card"><span class="illustration-icon">▤</span><div><small>DATA SOURCE</small><strong>PostgreSQL</strong><span>连接 · 验证 · 登记</span></div></div>
        <div class="illustration-line"></div>
        <div class="illustration-card muted"><span class="illustration-icon">◇</span><div><small>DATA CATALOG</small><strong>资产目录</strong><span>采集与发布待接入</span></div></div>
      </div>
    </section>
    <div class="feature-grid">
      <div class="feature-card"><span class="feature-icon">▤</span><h3>数据源连接</h3><p>登记并验证 PostgreSQL 数据源。</p></div>
      <div class="feature-card"><span class="feature-icon">▦</span><h3>工作区采集</h3><p>在工作区内保存采集作业，按版本执行生成画像产物。</p></div>
      <div class="feature-card"><span class="feature-icon">◇</span><h3>画像发布</h3><p>发布作业把工作区产物加载到平台库、向量库与图数据库。</p></div>
    </div>
    <PlaceholderPanel title="分析项目尚未接入" description="本阶段先完成数据源登记闭环，不展示原型里的示例项目或分析结果。" />
  </div>
</template>
