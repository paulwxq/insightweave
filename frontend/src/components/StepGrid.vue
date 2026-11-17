<script setup lang="ts">
/**
 * 步骤勾选网格（采集/发布通用）。available 限定可勾选范围（如版本步骤
 * 子集）；不可勾选的步骤置灰展示，不隐藏。
 * 行主文字恒为步骤 ID（doc 6.3 §6.2）；采集 domain/dim 的命令行参数
 * 仅作次要旁注，不代替步骤 ID。
 */
import { computed } from 'vue'
import type { JobKind } from '../api/types'
import { stepCliHint, stepGroups } from '../utils/steps'

const props = defineProps<{
  kind: JobKind
  selected: Record<string, boolean>
  /** 可勾选的步骤白名单；缺省为该类作业全部步骤 */
  available?: string[]
  disabled?: boolean
}>()

const emit = defineEmits<{ toggle: [step: string] }>()

const groups = computed(() =>
  stepGroups(props.kind).map((group) => {
    const rows = group.steps.map((step) => {
      // inVersion：版本白名单内的步骤（计数依据）；enabled 另受只读 disabled 影响
      const inVersion = props.available ? props.available.includes(step) : true
      return {
        id: step,
        hint: stepCliHint(props.kind, step),
        enabled: !props.disabled && inVersion,
      }
    })
    // 计数按本版本可勾选项（不受只读状态影响）；受限时用 n/N 明示与分组定义的差异
    const selectableCount = group.steps.filter((step) =>
      props.available ? props.available.includes(step) : true,
    ).length
    return {
      ...group,
      rows,
      countText:
        selectableCount === group.steps.length
          ? `${group.steps.length} 项`
          : `${selectableCount}/${group.steps.length} 项`,
    }
  }),
)

function groupOn(group: { rows: { id: string; enabled: boolean }[] }) {
  const selectable = group.rows.filter((row) => row.enabled)
  return selectable.length > 0 && selectable.every((row) => props.selected[row.id])
}

function toggleGroup(group: { rows: { id: string; enabled: boolean }[] }) {
  const selectable = group.rows.filter((row) => row.enabled)
  if (selectable.length === 0) return
  const next = !groupOn(group)
  for (const row of selectable) {
    if (Boolean(props.selected[row.id]) !== next) emit('toggle', row.id)
  }
}
</script>

<template>
  <div class="ds-new-job-grid">
    <article v-for="group in groups" :key="group.id" class="ds-new-job-group">
      <header>
        <button
          type="button"
          class="nj-check lg"
          :class="{ on: groupOn(group) }"
          :disabled="disabled || group.rows.every((row) => !row.enabled)"
          :aria-pressed="groupOn(group)"
          @click="toggleGroup(group)"
        >
          <svg v-if="groupOn(group)" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6 9 17l-5-5" /></svg>
        </button>
        <strong>{{ group.title }}</strong>
        <em>{{ group.countText }}</em>
      </header>
      <ul>
        <li v-for="row in group.rows" :key="row.id" :class="{ off: !row.enabled }">
          <button
            type="button"
            class="nj-check"
            :class="{ on: Boolean(selected[row.id]) }"
            :disabled="!row.enabled"
            :aria-pressed="Boolean(selected[row.id])"
            :aria-label="row.id"
            @click="emit('toggle', row.id)"
          >
            <svg v-if="selected[row.id]" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6 9 17l-5-5" /></svg>
          </button>
          <span class="nj-step-id">{{ row.id }}</span>
          <small v-if="row.hint" class="nj-step-hint">{{ row.hint }}</small>
          <slot name="after-step" :step="row.id" />
        </li>
      </ul>
    </article>
  </div>
</template>
