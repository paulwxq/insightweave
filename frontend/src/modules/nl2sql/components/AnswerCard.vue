<script setup lang="ts">
import type { TurnEvidenceView } from '../api/types'
import EvidenceBlock from './EvidenceBlock.vue'

defineProps<{
  sessionId: string
  answer: string
  elapsedMs: number | null
  evidence: TurnEvidenceView[]
  clarify: string[]
  unsafe: boolean
}>()
</script>

<template>
  <div class="ai-row">
    <div class="avatar" aria-hidden="true">
      <svg viewBox="0 0 16 16">
        <path
          d="M8 1.2 9 4.2 12.2 5 9 5.8 8 8.8 7 5.8 3.8 5 7 4.2 8 1.2Zm4.2.6.4 1.2 1.2.4-1.2.4-.4 1.2-.4-1.2-1.2-.4 1.2-.4.4-1.2ZM3.2 10.2l.5 1.4 1.5.5-1.5.5-.5 1.4-.5-1.4-1.4-.5 1.4-.5.5-1.4Z"
        />
      </svg>
    </div>
    <article class="card">
      <p class="summary" :class="{ caution: unsafe }">{{ answer }}</p>
      <ul v-if="clarify.length > 0" class="clarify">
        <li v-for="(item, index) in clarify" :key="index">{{ item }}</li>
      </ul>
      <EvidenceBlock
        v-for="(item, index) in evidence"
        :key="`${item.result_ref ?? 'sql'}-${index}`"
        :session-id="sessionId"
        :sql="item.sql"
        :result-ref="item.result_ref"
        :elapsed-ms="elapsedMs"
        :source-truncated="item.source_truncated"
      />
    </article>
  </div>
</template>

<style scoped>
.ai-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  width: 100%;
}

.avatar {
  width: 32px;
  height: 32px;
  border-radius: 16px;
  background: #e3eefb;
  display: grid;
  place-items: center;
  flex-shrink: 0;
}

.avatar svg {
  width: 16px;
  height: 16px;
  fill: #2e7cd6;
}

.card {
  flex: 1;
  min-width: 0;
  background: #fff;
  border: 1px solid #d9e3ef;
  border-radius: 14px;
  padding: 18px 22px;
  display: grid;
  gap: 14px;
}

.summary {
  margin: 0;
  color: #41556d;
  font-size: 13px;
  line-height: 21px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.summary.caution {
  color: #9a6b32;
}

.clarify {
  margin: 0;
  padding-left: 18px;
  color: #5c6f8a;
  font-size: 12px;
  line-height: 1.7;
}
</style>
