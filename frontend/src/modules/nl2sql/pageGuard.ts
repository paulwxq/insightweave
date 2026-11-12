/**
 * 查询页实例与创建请求的归属。
 *
 * 实例序号只增不减。卸载只把“当前实例”清掉，不把计数器退回到还会再发出来的值，
 * 因此离开后再进入，旧回调的序号不会和新页面相同。
 * 创建请求使用另一条序号。同一新建页更换预选时作废进行中的创建，
 * 不依赖页面有没有卸载。
 */
export function createPageGuard() {
  let nextInstance = 0
  let activeInstance = 0
  let createGeneration = 0

  function openInstance() {
    const instance = ++nextInstance
    activeInstance = instance
    return {
      stillHere(): boolean {
        return activeInstance === instance
      },
      release() {
        if (activeInstance === instance) activeInstance = 0
      },
    }
  }

  function createStamp(): number {
    return createGeneration
  }

  function invalidateCreate() {
    createGeneration += 1
  }

  return { openInstance, createStamp, invalidateCreate }
}

export const nl2sqlPageGuard = createPageGuard()
