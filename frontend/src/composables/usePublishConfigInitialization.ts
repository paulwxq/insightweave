import { ref, watch, type Ref } from 'vue'
import { getDimValueConfig, getDomainConfig } from '../api/workspaces'
import type { DimValueTableParam, DomainParam } from '../api/types'
import { dimDraftFromConfig, domainDraftFromConfig } from '../utils/publishStepConfig'

/** 自动加载版本草稿的初值；已有版本参数、用户应用的草稿均不覆盖。 */
export function usePublishConfigInitialization(
  context: () => readonly unknown[],
  dimDraft: Ref<DimValueTableParam[] | null>,
  domainDraft: Ref<DomainParam[] | null>,
) {
  const initializing = ref(false)
  const initError = ref('')

  watch(context, async ([kind, workspaceId], _previous, onCleanup) => {
    let active = true
    onCleanup(() => { active = false })
    initializing.value = false
    initError.value = ''
    if (kind !== 'publish' || typeof workspaceId !== 'string' || !workspaceId) return

    initializing.value = true
    const errors: string[] = []
    await Promise.all([
      dimDraft.value === null ? getDimValueConfig(workspaceId).then((config) => {
        if (!active || dimDraft.value !== null) return
        dimDraft.value = dimDraftFromConfig(config.tables)
        if (config.comma_string_tables.length) {
          errors.push(`以下表的字段配置包含逗号串，已按未指定处理，请编辑并使用字段列表：${config.comma_string_tables.join('、')}。`)
        }
      }).catch(() => {
        errors.push('维度值配置自动读取失败，可点击编辑手工配置。')
      }) : Promise.resolve(),
      domainDraft.value === null ? getDomainConfig(workspaceId).then((config) => {
        if (active && domainDraft.value === null) {
          domainDraft.value = domainDraftFromConfig(config.domains)
        }
      }).catch(() => {
        errors.push('业务域配置自动读取失败，可点击编辑手工配置。')
      }) : Promise.resolve(),
    ])
    if (!active) return
    initError.value = errors.join(' ')
    initializing.value = false
  }, { immediate: true, flush: 'post' })

  return { initializing, initError }
}
