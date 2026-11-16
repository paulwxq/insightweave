# doc21 实现审核 R2（Codex）

> 日期：2026-11-16。
> 范围：对 Hy4 R2 的 R1-01 修正进行增量复审，结合 R1 已核验的功能结论。
> 交付记录：`hy4_doc21_impl_review_request_r2.md`。
> 结论：**PASS，R1-01 已关闭，未发现新的待修问题；本次代码审核结束。**

## 1. R1-01 处理结果

`frontend/src/modules/nl2sql/views/QueryView.vue` 的 `.new-guide` 已使用既有 `chat.sourceInherited.value` 切换标题、描述及第 2 步文案：

- 沿用场景显示“开始智能查询”“已沿用当前数据库，可直接输入问题。”及“沿用数据库”。
- 普通场景保留原标题、描述与“自动完成检查”。
- 未新增状态、组件、检查或后台分支，属于要求范围内的局部补齐。

`frontend/src/modules/nl2sql/newQueryNavigation.test.cjs` 的断言已定位 `.new-guide` 子树，不再只依赖页头文字；同时正向验证沿用文案存在、旧提示不存在，并增加普通新建场景的原文案正向断言。目标文案改为动态插值后，可被当前测试宿主观察。

## 2. 独立验证

在 `frontend` 执行：

```bash
node --test src/modules/nl2sql/composables/useChat.test.cjs src/modules/nl2sql/components/SourceSelector.test.cjs src/modules/nl2sql/newQueryNavigation.test.cjs
npm run typecheck
```

结果：**39 passed / 0 failed**；类型检查退出码 0。仓库根目录 `git diff --check -- frontend` 退出码 0。

doc21 SHA256 仍为 `b55e1f941f449d189a6cfb2b0adbad94db64e46ab64600274ca25cfd30bcc44b`，定稿方案未变。本轮仅复核增量，不重复已通过的完整首次发送探针，也未独立重复 Hy4 报告中的变异测试。

## 3. 审核边界与收尾

结合 R1，本次顶部沿用、零页面 readiness、左侧无参数新建及同路由清空、手动换库检查、首次发送到新会话的前端实现审核通过。

真实后端首次发送、真实故障及修饰键的新标签页行为等，仍保留为真实环境验收边界；本轮未运行真实查询、未调用付费模型，也未重新执行浏览器验收或 build。不把离线代码审核通过表述为已部署或真实环境全部验收。

无需继续修改业务代码。保留当前未提交工作区，后续由用户安排实际交互验收和提交。

本轮仅新增此审核报告，未修改业务代码、测试或定稿方案，未提交、推送、部署或重启服务。
