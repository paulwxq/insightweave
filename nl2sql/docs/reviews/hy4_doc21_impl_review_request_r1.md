# doc21 实现审核请求（hy4 / r1）

> 日期：2026-11-16
> 依据：`nl2sql/docs/21_新建查询入口数据库沿用与跳过页面检查方案.md`（定稿）
> 实施范围：仅前端 Vue；顶部新建沿用数据库并跳过页面检查、左侧新建清空选择、后台检查保持现状。
> 代码根目录：`/opt/projects/insightweave`（前端在 `frontend/`，使用 `.venv` 无关，测试走 Node）
> 状态：实现与自动化验证完成，业务代码写入已暂停，等待原 Codex 审核反馈。

## 1. 范围与约束

- 按 §6、§6.1 的文件、函数及接入位置实施；未修改方案、未扩大功能。
- 未改动后台接口、数据库连接配置、DeepAgents、SQL 生成、任务账本、Agent 链路；后台 `create_web_session`／查询执行的检查逻辑保持现状（§4）。
- 未提交、未推送、未部署、未重启服务；未改动 `dist/`（已被 gitignore）。
- 工作区其他 57 项未提交修改（Python／docs／实验脚本等）全部保留、未触碰。
- `frontend/src/modules/nl2sql/views/QueryView.vue` 的工作区增量**包含两部分**：上一轮 `10.2_新建查询修订版.html` 页头修订（蓝色「新建查询」按钮移到左侧「查询类型区」、自然语言查询标签改浅灰弱化样式，上一轮已完成但未提交），以及本次 doc21 接线。审核本次增量时请注意区分；上一轮改动本身不属于 doc21。

## 2. 修改清单

| 文件 | 类型 | 内容 | 对应章节 |
| --- | --- | --- | --- |
| `frontend/src/modules/nl2sql/newQueryReset.ts` | 新增（24 行） | `registerNewQueryReset(handler)` / `notifyNewQueryReset()`：模块内单例当前回调，注销核对自身、无接收者直接结束，不排队／不重放／无 TTL | §5.3、§6、§6.1 |
| `frontend/src/modules/nl2sql/composables/useChat.ts` | 修改（+19） | 新增 `sourceInherited` ref；新增 `adoptSourceForNewQuery(sourceId)`（复用 `resetReadiness()` 作废旧检查令牌、清旧状态，再设选源 / `ready` / 沿用标记，不调用 API、不伪造 `ReadinessData`）；`resetReadiness()` 与 `checkReadiness()` 起始同步清除沿用标记；返回对象导出二者 | §5.2、§6、§6.1 |
| `frontend/src/modules/nl2sql/views/QueryView.vue` | 修改（+125/−24，含上一轮页头修订） | `newQueryTo` 在会话页且已取得 `sessionInfo.source_id` 时带 `source_id`（保留规范化 `home`）；新增 `pendingSourceInheritance`（`{sourceId, targetFullPath}` 组件内临时意图）与 `clearPendingSourceInheritance()`；新增 `createFromCurrentConversation(event)`（忙碌/修饰键守卫、保存意图、`event.preventDefault()`、一次 `realRouter.push`、失败按归属清理、成功保留意图）；顶部 RouterLink 加 `v-if="onSessionPage"` + `custom` 插槽输出真实 `href`，点击走上述函数，不再调用插槽 `navigate`，移除旧 `guardNewQuery`；`presetSource` 监听内先识别匹配意图（校验 `sourceId` 与 `route.fullPath`）→ 消费 → `adoptSourceForNewQuery()` 并 return，替代 `checkReadiness()`，`apiReady` 未就绪保留意图，失配／落到非新建页清除意图，普通分支原样保留；新增 `resetNewQueryFromSidebar()`（仅新建页、非忙碌：清意图 + `invalidatePendingCreate` + `resetCurrentSession` + `resetReadiness`）并在 setup 注册／`onUnmounted` 注销；`headerMeta` 沿用可输入时改为「已沿用当前数据库，可直接输入问题。」；`SourceSelector` 接线增加 `:source-inherited` | §5.1、§5.4、§6、§6.1 |
| `frontend/src/components/AppSidebar.vue` | 修改（+17） | 引入 `notifyNewQueryReset`；`guardNewQuery` 保留忙碌限制，普通左键／无修饰键／未被阻止且 `onNewQueryPage` 为真时先同步通知再继续一次 `navigate(event)`；不新增 `router.replace()`；修饰键等保留原链接行为、不发送通知 | §5.3、§6、§6.1 |
| `frontend/src/modules/nl2sql/components/SourceSelector.vue` | 修改（+4/−1） | `defineProps` 增加 `sourceInherited: boolean`；`ready` 主文案按沿用标记二选一（沿用：已沿用当前数据库，可直接输入问题。／普通：数据源已就绪，请在下方输入问题。），不新增第二条提示、不伪造检查结果 | §5.4、§6、§6.1 |
| `frontend/src/modules/nl2sql/composables/useChat.test.cjs` | 修改（+87） | 新增 4 例：沿用直接就绪且零页面检查／不创建会话、沿用后手动改选清除标记并对新库按原流程检查、迟到 readiness 响应不覆盖沿用状态、沿用后首次发送用沿用库创建一次新会话并在新会话提交 | §6、§6.1、§7 |
| `frontend/src/modules/nl2sql/components/SourceSelector.test.cjs` | 修改（+66/−1） | 公共挂载参数显式提供 `sourceInherited: false`；新增沿用/普通就绪主文案互斥、沿用标记清除后文案同步回退（同一实例响应式切换）两例；保留原有错误展示测试 | §5.4、§6.1 |
| `frontend/src/modules/nl2sql/newQueryNavigation.test.cjs` | 新增（764 行） | 真实 `QueryView.vue` / `AppSidebar.vue`（compiler-sfc 编译）+ 真实 `vue-router`（memory history）+ 真实 `useChat.ts` / `newQueryReset.ts` / `queryBusy.ts`，只替换 API 边界与展示型子组件；覆盖左右入口、顶部沿用、零页面检查、`apiReady` 延迟、当前会话未加载、带 `source_id` 新建页去参、重复导航清空、顶部导航失败清理、修饰键、忙碌限制、同步通知注册/注销隔离 | §6.1、§7 |

测试宿主说明：`newQueryNavigation.test.cjs` 的自定义渲染器把 `insert(anchor)` 修正为「插入到 anchor 之前」（`splice(index, 0, child)`），并按数组逐个注册合并后的事件监听器（非 custom RouterLink 会把 attrs 的 `onClick` 与内部 `navigate` 合并）。这两处是测试宿主对真实 Vue 行为的对齐，不是生产代码改动。

## 3. 验证结果

全部在 `/opt/projects/insightweave/frontend` 执行，退出码均为 0。

### 3.1 先补测试、确认旧版本失败（§7 第 1 项）

实现前执行三份测试：`38 tests / 26 pass / 12 fail`，失败原因全部指向未实现的 doc21 行为（缺 `adoptSourceForNewQuery`、缺沿用文案、顶部入口不携带 `source_id`、重复导航不清空草稿、`apiReady` 延迟未应用沿用、导航失败后意图残留、忙碌限制、缺 `newQueryReset` 模块）。记录见 `/tmp/doc21_pre2.log`（临时文件）。

### 3.2 实现后三份目标测试

```
node --test src/modules/nl2sql/composables/useChat.test.cjs \
            src/modules/nl2sql/components/SourceSelector.test.cjs \
            src/modules/nl2sql/newQueryNavigation.test.cjs
```

`1..38 / # tests 38 / # pass 38 / # fail 0`。

### 3.3 全量前端 `.cjs` 测试（回归）

```
node --test $(find src -name "*.test.cjs" | sort | tr '\n' ' ')
```

`1..57 / # tests 57 / # pass 57 / # fail 0`（含 `QuestionInput.test.cjs`、`openQuestionsBehavior.test.cjs`、`openQuestionsContract.test.cjs`、`sessionList.test.cjs` 等既有测试，无回归）。

### 3.4 typecheck

```
npm run typecheck   # vue-tsc --noEmit
```

退出码 0，无诊断输出。

### 3.5 build

```
npm run build       # vue-tsc --noEmit && vite build
```

退出码 0；`vite v6.4.3`，`✓ 198 modules transformed`，`✓ built in 1.63s`；产物 `dist/assets/index-BmMa1xmX.js 449.29 kB`、`dist/assets/index-CqdhGeQR.css 148.09 kB`。

### 3.6 真实浏览器冒烟（localhost:5173，ego-browser 快照/DOM 探针）

| 场景 | 结果 |
| --- | --- |
| `/query/new?home=/query` | 顶部蓝色「新建查询」按钮不渲染（`.head-side .head-btn-primary` = 0），`headSide` 仅「自然语言查询」，说明「选择数据库并完成检查后开始提问」，输入禁用 |
| 会话页 `/query/s_8d25…?home=/query` | 顶部按钮渲染（= 1），`headSide` =「新建查询 自然语言查询」（上一轮 10.2 左侧布局在会话页保留） |
| 会话页点击顶部新建 | 链接 `/query/new?home=/query&source_id=db_src_1`；落地后 `headerMeta` 与 `SourceSelector` 主文案均为「已沿用当前数据库，可直接输入问题。」，`source-chip` = Orders订单库，输入已启用（未出现「正在检查」流程） |
| 沿用页点击左侧「新建查询」 | 地址变为 `/query/new`（不含 `source_id`/`home`），`headerMeta` 回到「选择数据库并完成检查后开始提问」，`source-chip` = 未选择数据库，输入重新禁用 |

浏览器验证使用 `dispatchEvent` 合成点击，未创建真实会话、未提交真实查询。

## 4. 未验证项 / 待审核确认

1. **真实后端首次发送**：§7 的「人工验证两个入口和一次首次发送，确认新旧会话独立」中，真实服务端创建会话 + 提交查询未执行（避免产生真实会话与 SQL 执行）。该路径由 `useChat.test.cjs` 的「沿用后首次发送」用 API 替身覆盖（创建一次、提交到新会话、零页面检查），请审核是否足够。
2. **真实后端错误路径**：沿用后数据库停用／配置变化、首次发送被拒、创建失败等未在真实后端复现；§4 要求前端正常展示错误、不自动重提，仅由既有测试与替身覆盖。
3. **10.2 原型与 doc21 的冲突（需确认）**：`10.2_新建查询修订版.html` 在「新建查询」页的左侧「查询类型区」画了蓝色「新建查询」按钮；doc21 §1／§6.1 明确要求顶部按钮**只在会话页显示**、`/query/new` 不渲染，新建页清空只能走左侧入口。本次按 doc21 加了 `v-if="onSessionPage"`，因此 `/query/new` 页头只剩「自然语言查询」标签，与 10.2 原型视觉不一致。请审核此取舍是否符合预期。
4. **§5.4 空状态文案范围（需确认）**：本次按 §6.1 清单修改了 `headerMeta`、复用 `guideStep`（ready→第 3 步）、`SourceSelector` 主文案；空状态自身的标题「选择数据库，开始智能查询」与描述「选择数据库并完成检查后，用自然语言开始提问。」**未改动**（§6.1 未列出，且普通手动检查就绪态原本也显示同样文案）。若 §5.4「不能仍要求完成检查后开始提问」被解读为也要改这两句，请指出，本次未擅自扩大。
5. **修饰键／新标签页真实浏览器行为**：仅由 `newQueryNavigation.test.cjs` 的「修饰键点击左侧入口」覆盖（合成事件），未在真实浏览器用真实按键/中键验证；顶部入口修饰键点击仅按 §5.1/§6.1 逻辑处理，未做真实新标签页验证。
6. **跨浏览器／移动端、长会话/大结果集、真实网络延迟**：未验证。
7. **未执行**：未跑 Python 侧测试（本次无 Python 改动）；未跑 `npm run build` 之外的打包产物运行验证；未做 Lint（本次为 Vue/TS，仓库未提供前端 lint 脚本）。
8. ego-browser 提示有可用更新（0.6.3.12），按仓库规范未自行升级。

## 5. 工作区状态

- 本次新增/修改共 8 个前端文件（清单见 §2），均未提交。
- `git status` 中其余 57 项未提交修改（Python 源码、docs、测试脚本、实验文件等）保持原样，未因本次工作变动。
- 未提交/推送/部署/重启；未修改方案文档 `21_…方案.md`。

请读取本报告，审核本次增量并反馈问题；业务代码写入已暂停。
