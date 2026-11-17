<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import type { DeletionTargetChange } from '../api/types'

const props = defineProps<{
  change: DeletionTargetChange
  mode: 'delete' | 'clear'
  busy: boolean
}>()
defineEmits<{ cancel: []; confirm: [] }>()
const cancelButton = ref<HTMLButtonElement | null>(null)
onMounted(() => cancelButton.value?.focus())

const rows = computed(() => {
  const { previous, current } = props.change
  return [
    ['Milvus 主机', previous.milvus.host, current.milvus.host],
    ['Milvus 端口', previous.milvus.port, current.milvus.port],
    ['Milvus 数据库', previous.milvus.database, current.milvus.database],
    ['表结构集合', previous.milvus.collections[0], current.milvus.collections[0]],
    ['维度值集合', previous.milvus.collections[1], current.milvus.collections[1]],
    ['SQL 示例集合', previous.milvus.collections[2], current.milvus.collections[2]],
    ['Neo4j 地址', previous.neo4j.uri, current.neo4j.uri],
    ['Neo4j 数据库', previous.neo4j.database, current.neo4j.database],
  ]
})
</script>

<template>
  <section aria-label="存储目标变更确认">
    <p>存储配置与上次执行时不同。确认后，将在当前目标中重新清理该数据源的数据。</p>
    <div class="target-comparison">
      <table>
        <caption>原存储目标与当前存储目标，变化项已突出显示</caption>
        <thead><tr><th scope="col">配置项</th><th scope="col">原目标</th><th scope="col">当前目标</th></tr></thead>
        <tbody>
          <tr v-for="[label, previous, current] in rows" :key="label" :class="{ changed: previous !== current }">
            <th scope="row">{{ label }}</th><td>{{ previous }}</td><td>{{ current }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="target-note">如果原目标和当前目标是不同的服务，原目标中尚未清理的数据会保留。</p>
    <div class="dialog-actions">
      <button ref="cancelButton" type="button" class="button button-outline" :disabled="busy" @click="$emit('cancel')">返回</button>
      <button type="button" class="button button-primary" :disabled="busy" @click="$emit('confirm')">
        {{ busy ? '正在提交…' : mode === 'delete' ? '确认继续删除' : '确认继续清理' }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.target-comparison { overflow-x: auto; margin-bottom: 16px; }
table { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 12px; }
caption { text-align: left; color: #66788f; padding-bottom: 10px; }
th, td { padding: 10px 8px; border: 1px solid #d9e3ef; text-align: left; vertical-align: top; overflow-wrap: anywhere; }
thead { background: #f2f6fb; }
th:first-child { width: 27%; }
.changed { background: #fff4dc; }
.changed td:last-child { color: #8b4c00; font-weight: 700; }
.target-note { color: #66788f; }
</style>
