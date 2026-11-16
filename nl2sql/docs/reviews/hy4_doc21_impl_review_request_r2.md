# doc21 实现审核请求 r2（hy4 / R1-01 局部补齐）

> 日期：2026-11-16
> 依据：`nl2sql/docs/reviews/codex_doc21_impl_review_r1.md`（PASS_WITH_NITS，1 项 P3）、Orca 收件 `msg_0832ac4034fa`、doc21 定稿 §5.4。
> 范围：仅处理 R1-01 文案遗漏，不重写已通过逻辑、不扩展需求。
> 状态：局部修正与相关验证完成，业务代码写入已暂停，等待原 Codex 增量复审。

## 1. 判断：R1-01 成立

读取 `codex_doc21_impl_review_r1.md` 与本人 Orca 收件（`orca-ide orchestration check --json` 取得 `msg_0832ac4034fa`，来自 `term_03e470db-d4fa-43f4-bd98-1af31ba39fdf`），二者一致。

核对 `frontend/src/modules/nl2sql/views/QueryView.vue` 修正前（行 417/418/422）：

- `<h2>选择数据库，开始智能查询</h2>`
- `<p>选择数据库并完成检查后，用自然语言开始提问。</p>`
- 第 2 步 `<i>2</i>自动完成检查`

顶部沿用成功后，`currentSessionId` 已清空、尚未发首问，`showNewGuide` 仍为真，故该空状态仍会显示；页头 `headerMeta`、`SourceSelector`、第 3 步高亮虽已一致，空状态自身仍要求「选择数据库」「完成检查」，与 doc21 §5.4 及「沿用即可输入」不符。确认成立，按 R1 的最小建议局部补齐。

同时核实 R1 §3 的测试宿主提醒：`newQueryNavigation.test.cjs` 的 `insertStaticContent()` 是 `[null, null]` 空替身，完全静态提升的 `<h2>`/`<p>` 文本不会被插入 DOM，因此「断言整页包含沿用说明」或「静态文本不存在」都可能产生假阴性/假阳性。本次把目标文案改为插值（动态文本，不走静态提升），并加 `.new-guide` 区域级断言与可观察性守卫。

## 2. 修改位置

本轮仅改 2 个文件（其余 doc21 文件未动）。

### `frontend/src/modules/nl2sql/views/QueryView.vue`（空状态 `.new-guide`，约行 417–425）

复用既有 `chat.sourceInherited.value`，用插值局部切换三处文案；普通流程保留原文案原意，未新增状态、组件、配置或检查：

| 位置 | 沿用（`sourceInherited === true`） | 普通 |
| --- | --- | --- |
| 标题 `<h2>` | 开始智能查询 | 选择数据库，开始智能查询 |
| 描述 `<p>` | 已沿用当前数据库，可直接输入问题。 | 选择数据库并完成检查后，用自然语言开始提问。 |
| 第 2 步 `<span>` | 沿用数据库 | 自动完成检查 |

第 1 步「选择数据库」、第 3 步「输入自然语言问题」、空状态图标、`guideStep` 高亮逻辑均未改。改为插值后这三条目标文案成为动态文本，测试宿主可实际观察。

### `frontend/src/modules/nl2sql/newQueryNavigation.test.cjs`

- 新增 `guideText(h)`：只在渲染树中定位 `.new-guide` 元素并取该子树文本，不使用整页文本（页头本来就含沿用说明）。
- 新增 `assertGuideObservable(text, label)`：断言该区域含固定可见的「选择数据库」（第 1 步）与「输入自然语言问题」（第 3 步），先证明宿主确实能观察引导区域，再做包含/不包含断言。
- 在既有「顶部沿用」用例追加 `.new-guide` 区域断言：标题「开始智能查询」存在、「选择数据库，开始智能查询」不存在；沿用描述存在、「选择数据库并完成检查后」不存在；「沿用数据库」存在、「自动完成检查」不存在。原整页断言保留但改述为「页头说明」，不再充当引导区域证据。
- 新增「普通新建页空状态保留原提示，仅沿用场景局部切换文案」用例：正向断言原标题、原描述、原第 2 步均在 `.new-guide` 内（同时证明该区域标题/描述文本可被宿主观察），并断言不出现沿用文案。

未改 `useChat.ts`、`newQueryReset.ts`、`AppSidebar.vue`、`SourceSelector.vue`、`useChat.test.cjs`、`SourceSelector.test.cjs`；未改定稿文档 `21_…方案.md` 与 R1 报告。

## 3. 测试结果

在 `/opt/projects/insightweave/frontend` 执行：

```
node --test src/modules/nl2sql/composables/useChat.test.cjs \
            src/modules/nl2sql/components/SourceSelector.test.cjs \
            src/modules/nl2sql/newQueryNavigation.test.cjs
# 1..39 / # tests 39 / # pass 39 / # fail 0 / 退出码 0

npm run typecheck        # vue-tsc --noEmit，退出码 0，无诊断
```

r1 为 38 例，本轮新增 1 例共 39 例，全部通过。

**变异验证（断言有效性）**：临时把 `QueryView.vue` 三处插值还原为修正前的静态文案，用 `--test-name-pattern="R1-01|顶部沿用：会话页显示"` 运行，得到 `2 tests / 0 pass / 2 fail`，失败原因分别为「沿用标题不再要求选择数据库」与「普通新建页保留原标题」，即新断言确实能捕获该遗漏；随后由 shell trap 自动还原文件，并复跑确认 `39/39` 通过、typecheck 退出码 0。变异只在临时内存/备份文件中进行，未留在工作区。

按「仅按改动需要验证」执行：本轮只改模板文案与对应组件测试，故只跑相关测试与 typecheck；未重跑全量 57 例 `.cjs`、未重跑 build、未做浏览器冒烟、未调用真实业务库/后端/付费模型（r1 已验证且本轮未触碰相关逻辑）。

## 4. 约束与工作区

- 保留所有其他未提交修改：前端其余 6 个 doc21 文件、以及 59 项非前端未提交修改（Python 源码、docs、测试脚本、R1 报告等）均未触碰；上一轮 10.2 页头修订仍在。
- 未修改定稿方案、未新增功能、未重写已通过的导航/通知/composable 逻辑、未提交/推送/部署/重启、未调用真实业务库或付费模型。
- 未留下临时文件（`.nav_spike.cjs` / `.nav_debug.cjs` 已于上轮清理；本轮备份在 `/tmp`）。
- 未自行宣布独立审核通过；本次仅回报增量，等待原 Codex 复审。

## 5. 待确认（沿用 r1，不在本轮范围）

真实后端首次发送、真实后端错误路径、修饰键/新标签页的真实浏览器行为、跨浏览器与移动端，仍未做人工/真实环境验证；doc21 §1/§6.1 与 10.2 原型关于顶部按钮在新页是否显示的取舍已由 R1 §2 认可，无需再改。
