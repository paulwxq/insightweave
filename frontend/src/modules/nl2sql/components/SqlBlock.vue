<script setup lang="ts">
import { computed } from 'vue'
import { highlightSql } from '../sqlHighlight'

const props = defineProps<{ sql: string }>()
const tokens = computed(() => highlightSql(props.sql))
</script>

<template>
  <pre class="sql-block"><code><span
      v-for="(token, index) in tokens"
      :key="index"
      :class="`sql-${token.kind}`"
    >{{ token.text }}</span></code></pre>
</template>

<style scoped>
.sql-block {
  margin: 0;
  background: #14263c;
  border-radius: 10px;
  padding: 13px 16px;
  overflow: auto;
  max-height: 360px;
}

.sql-block code {
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 12px;
  line-height: 19px;
  white-space: pre-wrap;
  word-break: break-word;
}

.sql-keyword {
  color: #7ab8f5;
}

.sql-as {
  color: #5b7391;
}

.sql-string,
.sql-number {
  color: #e8b563;
}

.sql-text {
  color: #d6e4f2;
}
</style>
