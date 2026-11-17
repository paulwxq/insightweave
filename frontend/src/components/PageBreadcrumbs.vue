<script setup lang="ts">
/**
 * doc 9 §6 页面路径：只接收各页算好的段落，不请求接口。
 * 最后一段是当前页（不可点，aria-current="page"），前面每段是链接；
 * 名称未到达的上级段由页面以占位文字 + to 传入，仍是可点链接（§6.1）。
 * to 支持 query 对象形式；带特殊字符的 query 值必须用对象构造，不得拼字符串。
 */
import type { RouteLocationRaw } from 'vue-router'

defineProps<{
  segments: { label: string; to?: RouteLocationRaw }[]
}>()
</script>

<template>
  <nav v-if="segments.length" class="page-breadcrumbs" aria-label="页面路径">
    <template v-for="(seg, i) in segments" :key="i">
      <span v-if="i > 0" class="crumb-sep" aria-hidden="true">/</span>
      <RouterLink
        v-if="seg.to && i < segments.length - 1"
        class="crumb-link"
        :to="seg.to"
      >{{ seg.label }}</RouterLink>
      <span
        v-else
        class="crumb-current"
        :aria-current="i === segments.length - 1 ? 'page' : undefined"
      >{{ seg.label }}</span>
    </template>
  </nav>
</template>

<style scoped>
/* §6.1：沿用现有文字色（链接 #66788F、当前段 #17293F），分隔符 "/"，11px，不另起一套导航样式 */
.page-breadcrumbs {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 11px;
  min-width: 0;
}
.crumb-link {
  color: #66788f;
}
.crumb-link:hover {
  color: #2e7cd6;
  text-decoration: underline;
}
.crumb-sep {
  color: #66788f;
}
.crumb-current {
  color: #17293f;
  overflow-wrap: anywhere;
}
</style>
