/**
 * 运行终止（doc 11 §4.4/§4.7）纯规则：终止按钮三态门控与弹窗计划变更展示。
 * 只从已加载的运行详情推导，不发起请求；实际裁决由后端在锁内作出。
 */
import type { RunDetail, RunStepItem } from '../api/types'

/** §4.7：注册残留/执行中场景的逃生口提示，与后端 409 文案一致 */
export const TERMINATE_ACTIVE_HINT =
  '该运行正在本进程执行中，不能终止；若确认执行已中止，请重启后端后重试。'

export interface TerminateButtonState {
  visible: boolean
  disabled: boolean
  hint: string
}

/** §4.4.1/§4.7：pending/running 可见；executor_active=true 禁用；缺失不门控 */
export function terminateButtonState(run: RunDetail | null): TerminateButtonState {
  const visible =
    run !== null && (run.status === 'pending' || run.status === 'running')
  const disabled = visible && run?.executor_active === true
  return {
    visible,
    disabled,
    hint: disabled ? TERMINATE_ACTIVE_HINT : '',
  }
}

/** §4.4.2 计划变更规则：running→失败、pending→被阻断、其余保持不变 */
export function plannedStepTransition(status: RunStepItem['status']): string {
  if (status === 'running') return '失败'
  if (status === 'pending') return '被阻断'
  return '保持不变'
}

/** 汇总裁决（§1.3）：全部成功→success；有成功非全部→partial_success；无成功→failed */
export function plannedFinalStatus(
  steps: RunStepItem[],
): 'success' | 'partial_success' | 'failed' | null {
  if (steps.length === 0) return null
  const resolved = steps.map((step) =>
    step.status === 'running'
      ? ('failed' as const)
      : step.status === 'pending'
        ? ('blocked' as const)
        : step.status,
  )
  if (resolved.every((status) => status === 'success')) return 'success'
  if (resolved.some((status) => status === 'success')) return 'partial_success'
  return 'failed'
}

/** 原因非空（去空白）且勾选承诺后方可提交（§4.4.2） */
export function canSubmitTerminate(reason: string, confirmed: boolean): boolean {
  return reason.trim() !== '' && confirmed
}
