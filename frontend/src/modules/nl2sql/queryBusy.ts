import type { InjectionKey, Ref } from 'vue'

/** 查询页与侧栏共享的执行中标记。只表达入口是否禁用。 */
export const queryBusyKey: InjectionKey<Ref<boolean>> = Symbol('nl2sql-query-busy')

let owner = 0

export function bindQueryBusy(flag: Ref<boolean>): { token: number; release: () => void } {
  const token = ++owner
  return {
    token,
    release() {
      if (owner === token) flag.value = false
    },
  }
}

export function ownsQueryBusy(token: number): boolean {
  return owner === token
}
