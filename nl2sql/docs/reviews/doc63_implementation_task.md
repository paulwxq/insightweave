# doc 6.3 实施与 Codex 审核任务

> 最新结果：R6 主体代码审核 PASS；其后 M1/M2 收尾增量已获 R7 PASS，见 `codex_doc63_impl_review_r7.md`。当前没有待修改项，本文其余委派流程作为历史记录保留；未经用户后续安排不提交、部署或操作真实资源。

> 后续授权：用户已完成三次 Vue 正常真实查询并由 Codex 核对日志，随后明确要求 Codex 总结并提交。本次本地提交已获授权，早期“未提交/不得提交”的约束仅描述先前阶段；没有推送、生产部署或真实资源清理授权，真实验证范围见 `doc63_implementation_closeout.md`。

用户已批准 doc 6.3，要求右下角现有 Claude Code 开发代码与测试，完成后通知左侧 Codex 审核。Claude 应核实 Codex 反馈，存在的问题修复、不成立的反馈给出代码/测试依据，再通知 Codex 复审。

> 最新交接与推进授权（2026-11-13，优先于本文历史执行者/轮次说明）：用户已授权 CodeBuddy / hy4 接手，完成通知由用户人工传递，不要求使用原 Claude pane 或 Orca。用户在 R4 复审时进一步明确：不要因第四轮或 token 投入保守放弃；已成功的改动保留，有明确可行修法就继续推进。当前已完成 R6，报告见 `codex_doc63_impl_review_r6.md`，结论 PASS，R5-01 已验证修复，代码审核循环闭环。此前“R4 后自动停止、不进入 R5”的推进限制按该最新意见更新；仍保留真实累计轮次、不清零。本文其余范围、保护工作区、隔离测试与未经授权不提交/部署等约束继续有效；真实外部集成边界详见 R6 报告。

## 实施基线与工作范围

- 工作目录 `/opt/projects/insightweave`，模块目录 `nl2sql`，当前分支 `feature/multi-source-knowledge-isolation`。
- 代码基线 `50067ce`，6.2 已实施并收尾；开始任务时业务代码无未提交变更，只有已批准的 6.3 文档修订。
- 权威方案 `nl2sql/docs/6.3_db_deepagent已登记任务终态与答案覆盖门控方案.md`，委派时 SHA256 为 `c393d2ea5fe558964467e14f82fe45839d290d7cae808578b2300547ea021593`。必须阅读全文，包含 §13.7；不能仅依据上次会话里较早的 doc 6.3 记忆实施。
- 实施 §10 的完整闭环及 §11 的测试矩阵，包括前后端和清理互斥的必要配套；本任务不是继续审核文档，也不是只建任务表。
- 保留 6.2 的有效性归一、兜底、归档 v2、单层重试、预算计数、异常与持久化状态优先级。
- 遵守项目 AGENTS.md/CLAUDE.md 和实际适用的技能。使用 `nl2sql/.venv/bin/python`。SDK 以本地安装的版本匹配源码为准。
- 保护当前已批准文档和其他已有修改；不得 reset、清理历史数据、执行实际资源清理、提交/推送/合并代码、部署或重启用户服务。测试隔离在临时目录、内存及专用测试数据中，不操作真实业务数据。方案若必须改变，先通过本 Dispatch 向 Codex 说明证据和建议。

## 必须核对的契约

1. 业务任务/计划私有通道、专用 reducer、完整版本身份、批量候选计划及执行 DAG 与终止审计依据分离；revision/void/replacement 的历史决策语义。
2. 版本先检的完整操作幂等：旧 expected_plan_version 的完整重放仍冲突；同 content、不同 dependencies 冲突；合法 no-op 不递增计划版本。
3. SQL 授权、执行、分析、占位符和 E/A 沿用的任务归属；只有 pending/in_progress 可写，probe 不伪造任务。
4. E/A 完整来源引用、可见来源目录、直接沿用血缘、完整运行来源快照及保留闭包；T/D 独立模式及歧义拒绝。
5. failure_refs、服务端 error_id、错误完整身份和专用去重 reducer；同引用异事实拒绝，读取绕过 reducer 的重复也拒绝；工具失败、主动放弃、依赖阻塞不混淆。
6. 任务终态、finding_id/task_refs、成功覆盖、unresolved 恰覆盖和状态严格匹配；可靠行数及全组显式 False 的截断聚合，不从默认投影证明 no_data。
7. 工具与 Runner 共用规则；一次结构归一与语义判定得到 eligible_submission，贯穿状态/正文/归档条件 b；非空坏提交必须系统兜底，原始提交保真。
8. 确定性分项正文、权威/补充问题合并顺序；WebTurn/WebStore/finalize hook、前端快照/历史和 CLI 一致，旧缺失 None 与新 [] 区分，安全投影不泄漏问题。
9. §6.5 运行/清理共用会话 guard，持锁覆盖来源检查到 P3；R2/R3 及 delete/purge 的受控删除，锁内重读闭包、未知归属/后端故障跳过；PG 与 memory 边界。
10. 工具装配、模型可见参数、提示词/Skill、错误映射、预算与真实图测试完整同步，不能只在测试手动构造 LedgerView 绕开实际装配。

## 验证与报告

- 可按 §10 分步开发，但全部完成并执行适当验证后再提交首次审核；过程中通过 Dispatch 报告实质阻塞，不因规模大自行裁剪范围。
- 运行相关 db_deepagent 单元/装配/真实图测试、llm_factory 和 API v2 回归；前端改动执行对应测试及类型/构建检查。格式、lint 与必要的类型检查覆盖修改范围，存量问题须区分。
- 并发测试按 §11 使用实际同步边界；PG 锁测试需要独立连接，不能把两个互不相关的 mock 当作跨进程互斥证据。没有隔离测试数据库时明确报告验证缺口，由 Codex 判断下一步，不能称真实 PG 验证通过。
- 不必为完成本轮擅自发起付费真实模型调用或访问业务数据库；如题集环境/凭据/测试依赖缺失，报告准确边界。
- 首次完成写 `nl2sql/docs/reviews/doc63_impl_review_request_r1.md`，含变更清单、方案对应表、精确测试命令/结果、未验证边界、git diff 基线、文档哈希和需重点复核位置。
- 本次正式编排返回 agent_unconfigured，无法识别原有自定义 Claude pane。协调者已按恢复指南确认 Run 中没有 Task/Dispatch、目标仍空闲；根据 AGENTS.md 降级到 Orca Terminal，保留原 pane/会话/模型，不创建替代 Agent。本任务没有有效 Dispatch，不得伪造 worker_done。
- 完成或遇到实质阻塞时，先写报告并在当前终端输出明确标记，再显式给协调者发状态消息。首次完成命令如下（需先按技能解析 CLI，本会话已确定使用 orca-ide）：

```bash
orca-ide orchestration send --to run:run_ed5e28e26c11 --run run_ed5e28e26c11 --from term_b59a7d6d-45b0-4710-9083-5074bd6aa37d --type status --subject DOC63_IMPL_R3_READY --body 'R2反馈已逐项处理，请 Codex 读取 nl2sql/docs/reviews/doc63_impl_review_request_r3.md 复审；我已暂停业务写入等待反馈。' --json
```

- 上述命令已在 R2 后更新轮次；目前已完成 R2/4，下一次为 R3。句柄为先前 runtime 重启后重新核实的同一 pane；之后按实际轮次和有效句柄更新，不重复发送旧轮通知。此 status 是显式完成通知，不是自动 Worker 回报，也不代表审核通过。协调者会主动读取终端输出、报告、diff 和必要验证证据核实。若状态消息失败，将错误及回执写入报告；可通过 Orca Terminal 向左侧 Codex 当前句柄 `term_03e470db-d4fa-43f4-bd98-1af31ba39fdf` 发送同一完成标记及报告路径，避免重复提交整个开发任务。

## 审核循环：最多 4 轮

- 首次代码审核计 R1；后续复审核计 R2、R3、R4，开发阶段内部自测不占审核轮次。
- Codex 将报告写为 `docs/reviews/codex_doc63_impl_review_rN.md`；本次降级期间通过原 Orca Terminal 发送反馈，不与有效 Dispatch 双重控制。
- Claude 逐项核实反馈：接受者修复并加适当回归验证，不接受者写出具体理由及证据；完成后写下一轮 request，说明逐项处理和验证结果，再以上述 status 方式回报（更新轮次标记与报告路径）。
- Codex 审核期间 Claude 暂停业务代码写入，保持审核对象稳定；Codex 只审，不与 Claude 同时改业务代码。
- R4 若仍有未解决的正确性问题、契约缺口或实质性分歧，双方停止自动修改/第五轮审核，向用户报告待裁定项。不得静默清零轮次或另起循环。
- Codex 明确 PASS 才算代码审核闭环；首次实现完成、测试通过或 worker_done 均不等于已经通过 Codex 审核。
