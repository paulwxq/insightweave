<script setup lang="ts">
const props = defineProps<{
  schemas: string[]
  modelValue: string[]
  disabled?: boolean
  showChips?: boolean
}>()

const emit = defineEmits<{ 'update:modelValue': [value: string[]] }>()

function toggle(name: string) {
  if (props.disabled) return
  const selected = new Set(props.modelValue)
  if (selected.has(name)) selected.delete(name)
  else selected.add(name)
  emit('update:modelValue', props.schemas.filter((item) => selected.has(item)))
}
</script>

<template>
  <div class="schema-selector">
    <div v-if="showChips && schemas.length" class="schema-chip-bar">
      <span v-for="name in modelValue" :key="name" class="schema-picked">
        {{ name }}
        <button type="button" :disabled="disabled" :aria-label="`移除 ${name}`" @click="toggle(name)">×</button>
      </span>
    </div>
    <div v-if="!schemas.length" class="schema-empty">没有可选择的业务 Schema，请检查源库账号权限。</div>
    <div v-else class="schema-list">
      <label v-for="name in schemas" :key="name" class="schema-row" :class="{ selected: modelValue.includes(name) }">
        <input
          type="checkbox"
          :checked="modelValue.includes(name)"
          :disabled="disabled"
          @change="toggle(name)"
        />
        <span class="schema-name">{{ name }}</span>
        <span v-if="modelValue.includes(name)" class="schema-selected-label">已选</span>
      </label>
    </div>
  </div>
</template>
