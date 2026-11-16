/**
 * 左侧「新建查询」入口的同步重置通知（doc 21 §5.3）。
 *
 * 只保存当前已挂载 QueryView 的回调：注册替换当前引用，注销时核对是否仍是
 * 自己的注册，通知时同步调用当前回调；没有回调直接结束。不保存待消费信号、
 * 不排队、不重放，因此无需 TTL 或「下一 tick 过期」机制。
 */

type ResetHandler = () => void

let currentHandler: ResetHandler | null = null

/** 注册当前接收者的重置回调，返回只注销自己注册的注销函数。 */
export function registerNewQueryReset(handler: ResetHandler): () => void {
  currentHandler = handler
  return () => {
    if (currentHandler === handler) currentHandler = null
  }
}

/** 同步通知当前接收者；没有接收者时直接结束。 */
export function notifyNewQueryReset(): void {
  currentHandler?.()
}
