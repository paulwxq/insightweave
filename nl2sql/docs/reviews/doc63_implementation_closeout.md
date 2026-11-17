# doc 6.3 实施收尾记录

- 日期：2026-11-13
- 对象：[doc 6.3 方案](../6.3_db_deepagent已登记任务终态与答案覆盖门控方案.md)（含 §13.7），批准版本 SHA256 `c393d2ea5fe558964467e14f82fe45839d290d7cae808578b2300547ea021593`，**未修改、哈希未变**
- 基线：`50067ce2bc7dbd273c42f81f31fce0eab255ebf5`，分支 `feature/multi-source-knowledge-isolation`
- 状态：Codex 独立审核 **R6 PASS**（[codex_doc63_impl_review_r6.md](codex_doc63_impl_review_r6.md)），代码审核循环闭环；之后的 M1/M2 收尾增量已获 **R7 PASS**（[codex_doc63_impl_review_r7.md](codex_doc63_impl_review_r7.md)）。用户在 Vue 前端完成三次正常真实查询，Codex 核对日志与运行记录通过；本记录随实现一并提交当前本地分支，**未推送、未部署到生产**。
- 本记录为收尾说明，不复制各轮审核报告正文；逐轮结论与证据见第 3 节对应报告。各轮报告中的“未提交”“未执行真实模型”等状态描述保留为当时事实，最新状态以本文为准。

## 1. 实现范围及与 6.1 清单的对应关系

本次只实施 [6.1 清单](../6.1_db_deepagent能力补齐问题清单与实施建议.md) 的 **02—04**，与 doc 6.3 §1 的边界一致；不扩展 SQL 修正上限（06）、表结构硬门控（07）、防重复执行（08）、自动批次调度（09）、时间表达（10）、独立 SQL 模式（11）、旧入口去留（12）或性能复用（13）。

| 6.1 条目 | 工作项 | 本次状态 |
| --- | --- | --- |
| 02 | 已登记任务的终态与答案覆盖门控 | **代码已实现、离线审核通过**（本轮核心） |
| 03 | 任务范围内的 SQL 授权与证据归属 | **代码已实现、离线审核通过**（02 的任务身份配套） |
| 04 | 任务修订、依赖失效与历史沿用规则 | **代码已实现、离线审核通过**（02/03 的版本与血缘配套） |
| 01、05 | 最终答案兜底、主模型单层重试 | 由 doc 6.2 实现，本次保留不重写（见 [doc62_implementation_closeout.md](doc62_implementation_closeout.md)） |

「代码已实现、离线审核通过」= 离线单测 / 装配 / 真实 Runner 控制流 + Codex R1–R6 代码审核闭环。其后三次 Vue 正常真实查询也已通过（§5.1），**不等于真实 PG 清理并发、完整模型题集、仓库外消费者或全面浏览器验收通过**（见第 6 节）。

## 2. 关键代码、持久化与对外契约变化

### 2.1 任务账本、计划版本与专用 reducer

- `state.py`：新增 `db_sub_tasks`（键按 `(run_id, sub_task_id, revision)` 规范编码，`task_ledger_key` / `parse_task_ledger_key`）；专用 reducer `merge_task_records`（不可变事实字段一致性强校验、同 ID 异事实拒绝）与 `task_record_update_allowed` / `is_legal_void_transition`（允许 completed/failed/needs_clarification 合法转 voided，禁止非法覆盖）。
- `services/task_ledger.py`：`TaskLedger.apply()` 以 `expected_plan_version` 版本先检做整批幂等；候选计划深拷贝不原地改写输入；`add/status/revise/void` 操作（`replacement` 是 `void` 的可选替代目标参数）、固定直接依赖、`blocked_by` 整批复核、终止依据（`termination_context`）服务端生成；决策时事实规则 `derive_decision_root` / `terminal_facts` 与复核端共享。
- `tools/task_tools.py`：`make_record_sub_tasks_tool` / `record_sub_tasks`，单条 AIMessage 串行约束；`agent.py` 的 `_TASK_TOOLS` / 装配把登记工具与 `submit_final_answer` 纳入工具集。

### 2.2 提交门控与 Runner 交付资格（共享规则）

- `services/task_completion.py`：`CompletionSnapshot` / `CompletionDecision`、`evaluate_submission()`；任务终态、finding 合法覆盖、未选成果消费、派生结果绕过、缺源与快照身份核验、确定性分项正文与权威/补充问题合并。`validate_blocked_decision()` 逐项核验全部 `blocked_by`，首项根因摘要 `blocked_by_root` 仅绑定首项。快照装配函数 `build_completion_snapshot()` 位于 `tools/final_answer.py`，供提交工具与 Runner 共用。
- `tools/final_answer.py`：提交工具与 `build_completion_snapshot()` 复用同一门控；`runner.py` 的 `_finalize()` / `_task_completion_decision()` / `_fallback_task_section()` 与提交工具用同一 `eligible_submission` 判定，贯穿状态、交付正文与归档条件 b。保留 6.2 的归一提交、原始/交付归档分离、系统兜底、单层重试与预算/异常优先级。
- `errors.py`：`persistence_error_types()` / `is_persistence_error()` 下沉到零项目内依赖的叶子模块，供 `runner._finalize` 受控 I/O 异常边界（persistence 最高优先级 / runtime 收敛 / `SourceUnqualified` 受控业务拒绝）与 `final_answer` 共用。

### 2.3 SQL 授权、分析、沿用的任务身份与血缘

- `services/reuse_qualification.py`：统一证据图核验入口 `EvidenceGraphVerifier`（完整身份解析、快照权威、递归 direct/input 边、缺源/冲突/环、按 `(完整身份, 角色)` 实例级缓存、逐边 expected 约束）、`evidence_edges()`、`verify_input_edges()` / `verify_consumed_edges()` / `unqualified_reuse_evidence()`、`SourceUnqualified`、`TASK_LEDGER_PROTOCOL_VERSION`。
- `services/sql_pipeline.py`：`resolve_placeholder_lineage()` 在 EXPLAIN/授权/执行前共享核验完整祖先、`require_session_lease()`；`tools/sql_tools.py` / `tools/result_tools.py` / `tools/guard_tools.py` / `services/result_analysis.py` 统一以 `make_ledger_view` 装配并校验有效运行 guard、任务归属、沿用注册与失败引用归属；删除裸 `evidence_id` 的另套递归。
- `schemas.py`：`ExecutionRecord` / `AnalysisRecord` 携带 `session_id` / `thread_id` 等完整来源身份；`TaskFailureRef` / `TerminationContext` / `DependencyStateSnapshot` / `blocked_by` 等终止审计字段。

### 2.4 运行来源快照、保留根、清理互斥与依赖删除

- `persistence/archive.py`：`RUN_SNAPSHOT_SCHEMA_VERSION`、`write/read/list/delete_run_snapshot`、`refs_of_record()`；读路径严格校验完整作用域身份。归档 v2（`ARCHIVE_SCHEMA_VERSION=2`）沿用 6.2 契约。
- `persistence/maintenance.py`：`_retained_snapshot_roots()`（仍在保留期 QA/run_errors/携带引用为根）、`_protected_closure()`（图合法性检查与保护标记分离、递归保留闭包、`edge_out` 暴露快照级来源图）、`_plan_orphan_snapshot_deletions()`（TTL 删除集、递归延迟、全部到期一起回收）、`_order_orphan_deletions()`（消费者先于来源的稳定拓扑排序，R6/M2 排序失败抛 `OrphanDeletionOrderError`）、`_cleanup_session_resources()`（会话锁内重读、排序/删除失败保守停止、dry-run 不实删）。`persistence/session_guard.py` 的进程内 guard 与 PG advisory lock 提供运行/清理互斥。

### 2.5 对外契约（API / 历史展示）

- 返回协议 `DbAgentResult` 保留 6.2 状态七值、`archived`、`errors`；归档 `delivery_answer` / `answer_source` / `read_delivery_answer()` v2 兼容不变。
- 前端 `frontend/src/modules/nl2sql/`：`api/types.ts` 的 `WebTurn.open_questions`（doc 6.3 §8.3 合并问题列表，旧记录 `None` 与新 `[]` 区分）、`useChat.ts` 的 `snapshotTurn()` 安全投影（不安全终态不带回 SQL/结果引用与问题）、WebStore 保存的同一合并列表一致；前端行为测试 `openQuestionsBehavior.test.cjs`。

## 3. R1—R6 审核过程索引（R6 PASS）

| 轮次 | 实施请求 | 审核报告（结论） | 要点 |
| --- | --- | --- | --- |
| R1 | [doc63_impl_review_request_r1.md](doc63_impl_review_request_r1.md) | [codex_doc63_impl_review_r1.md](codex_doc63_impl_review_r1.md)（REQUEST_CHANGES，17 项） | 首轮实现全面核验 |
| R2 | [doc63_impl_review_request_r2.md](doc63_impl_review_request_r2.md) | [codex_doc63_impl_review_r2.md](codex_doc63_impl_review_r2.md)（REQUEST_CHANGES，13 项） | 统一证据图核验、reducer/历史语义、Runner 收尾、前端行为测试 |
| R3 | [doc63_impl_review_request_r3.md](doc63_impl_review_request_r3.md) | [codex_doc63_impl_review_r3.md](codex_doc63_impl_review_r3.md)（REQUEST_CHANGES，5 组） | consumer scope 血缘、占位符副作用前置、畸形边严格解析、阻塞决策时事实、保护根集合 |
| R4 | [doc63_impl_review_request_r4.md](doc63_impl_review_request_r4.md) | [codex_doc63_impl_review_r4.md](codex_doc63_impl_review_r4.md)（REQUEST_CHANGES，3 项 + M1） | Runner I/O 异常边界、多个独立阻塞根、跨周期清理缺源、verifier 缓存 |
| R5 | [doc63_impl_review_request_r5.md](doc63_impl_review_request_r5.md) | [codex_doc63_impl_review_r5.md](codex_doc63_impl_review_r5.md)（REQUEST_CHANGES，1 项 P2） | 孤儿快照删除部分失败仍删来源 |
| R6 | [doc63_impl_review_request_r6.md](doc63_impl_review_request_r6.md) | [codex_doc63_impl_review_r6.md](codex_doc63_impl_review_r6.md)（**PASS**） | R5-01 关闭（消费者先于来源 + 第一次失败/保留即停止）；无新增必修问题；**代码审核闭环** |

累计轮次真实保留、不清零；R6 PASS 后用户要求补 M1/M2 两项非阻塞建议，本收尾处理见第 4 节，对应 [R7 请求](doc63_impl_review_request_r7.md) 与 [R7 PASS 报告](codex_doc63_impl_review_r7.md)。R7 为收尾增量复审，不改写 R6 已通过事实。

## 4. 本次 M1 / M2 处理情况（R6 PASS 后增量）

- **M1（收尾文档同步）**：6.1 的 02—04 由「当前缺口／配套改造」改标「代码已实现、离线审核通过」并附本记录与 R6 报告链接，保留原问题/设计背景、不动 06—13；新增本收尾记录；[v4 详细设计](../v4/5_db_deepagent详细设计.md) 增加顶部状态注记、关键章节交叉引用与新 §22 综合同步。明确「代码已实现」与「真实环境已验收」的区别。
- **M2（排序失败明确拒绝）**：`_order_orphan_deletions()` 无法覆盖整个 `deletion_set` 时抛局部专用异常 `OrphanDeletionOrderError`，不再返回按名称拼接、含未满足依赖节点的部分顺序；`_cleanup_session_resources()` 窄捕获该异常，记录清晰原因并跳过本会话本轮全部孤儿运行快照实删（不使用部分排序结果、不穿透维护流程、不撤销此前其它阶段已完成操作、不新增事务或通用图框架）。补 `test_doc63_r7_regressions.py`（6 例），保留既有 R5-01 正式回归。

关于 6.3 批准稿的历史措辞：doc 6.3 §1、§2 中「当前代码没有业务任务账本」「新账本和门控仍未实施」「当前链路…」等，以及 §2.3 的「当前行为」，描述的都是 **`50067ce` 分析基线**（方案编写时的历史状态），不是 R6 之后工作区的实现状态。为保持批准版本固定哈希，本轮**不改写批准稿**；该差异在 [6.1 清单](../6.1_db_deepagent能力补齐问题清单与实施建议.md)、[v4 详细设计 §22](../v4/5_db_deepagent详细设计.md) 与本记录中统一说明。

## 5. 已验证结果

- 后端全量（R7 增量后）：`db_deepagent/unit + llm_factory + API v2` **1375 passed / 4 skipped**。R6 Codex 独立基线为 1369 passed / 4 skipped（含 R5-01 的 8 例）；本轮新增 M2 回归 6 例。
- 维护相关专项（R2/R3/R4/R5/R7 维护回归 + `test_maintenance_cleanup.py`）：**153 passed**。
- 既有完整路径：合法 `query→A→completed→reuse A→completed→继续分析→submit→真实 Runner.run/交付/QA 归档` 经 Codex R4/R6 独立验证成功；`TaskLedger→真实 reducer`、实际清理控制流、真实 Runner I/O 故障边界等均有正式回归。
- 提交收尾时独立复跑前端 **57 passed / 0 failed**，`vue-tsc --noEmit` 退出 0。此前 R7 未重跑前端，提交前的这次为新增验证。
- 提交前检查发现 8 个测试文件需要 Black 格式化，已仅调整格式，并逐文件比较 AST，确认语法树不变；未改业务代码或断言逻辑。最终 `black --check` 覆盖全部 53 个本次 Python 文件；新增测试文件 `ruff` 检查及 `git diff --check` 通过。存量 lint 债单独保留，不表述为全量 Ruff 告警清零。
- 此外对本次新增的 15 个 Python 文件执行完整 Ruff 检查并清零：整理导入、UTC 写法和未使用局部变量，将任务账本一处遮蔽 `dataclasses.field` 的循环变量改名为 `field_name`，将两处 schema 测试的广义 `Exception` 断言收紧为 `ValidationError`。没有新增业务机制；整理后重新执行后端全量回归。此处是提交前的小范围质量整理，区别于 R7 审核时只检查单个新增回归文件的结果。
- 批准方案 SHA256 不变；本次提交的父提交为 `50067ce`，没有重置或丢弃已有工作区内容。日志、运行结果、敏感配置及 `/tmp` 探针不纳入提交。

上述后端全量由 Codex 在 R7 独立复跑：**1375 passed / 4 skipped，13.38 秒**；实际排序器异常边界及既有 R6 独立探针均通过。复核时校正了 `replacement` 参数归属、`build_completion_snapshot()` 模块位置及 6.2/6.3 基线文案，未改业务代码，详见 R7 报告。

### 5.1 Vue 正常真实查询验收（2026-11-13）

用户在本地 Vue 前端提交查询，Codex 只读取现有日志、审计及结果文件，没有额外调用模型或查询业务数据库。三轮属于同一会话 `s_4f00a8482b33409184893606cf7df9fc`，使用真实主模型与业务数据：

| 场景 | request_id | 结果及覆盖 | 业务 SQL 执行次数 | 耗时 / 模型调用 |
| --- | --- | --- | --- | --- |
| 简单查询 | `req_3be0745d728e4cbe9fe05fd3a1e33d36` | 商品05，19 条订单；T1@1 对应一条 finding | 1 | 24.312 秒 / 9 |
| 双任务查询 | `req_ed550dcddee74b8da304b705d4349ebd` | 商品05、19 条；订单总数 500 条；T1@1/T2@1 分别有证据和 finding | 2 | 19.150 秒 / 7 |
| 同会话跨轮沿用 | `req_f52c32d85c034fd2ba89153b1e6bffd3` | 沿用上一轮商品05、19 条的结果，保留来源身份和原生成时间 | 0 | 16.013 秒 / 4 |

三轮运行记录均为 `status=completed`、`archived=true`，`errors`、`failures` 为空。审计中的全部模型尝试成功，无异常重试。沿用轮实际工具序列为 `record_sub_tasks → reuse_evidence → record_sub_tasks → submit_final_answer`，没有再次调用 `validate_sql` / `execute_sql`；本轮 `result_ref` 与上一轮商品查询相同，`reused_from` 指向其来源证据 `E-0047f369`，原生成时间为 `2026-11-13T17:40:35+08:00`。这不表示该轮没有建立业务库连接或没有平台库元数据读取。

本地原始证据位置（不纳入 Git）：`var/db_deepagent/sessions/<session_id>/runs/<request_id>.json`、该会话 `audit.jsonl`、`logs/nl2sql.log` 及 `var/db_deepagent/results/<result_ref>/meta.json` / `rows.jsonl`。用户截图与运行记录中的三个数值相符；未额外对业务库进行独立重算。

成本基线：三轮累计输入 Token 分别为 114121 / 112728 / 73380，其中缓存命中分别为 96896 / 107904 / 69632；输出分别为 1164 / 1105 / 1066。不把累计输入当作单次上下文长度，也不把缓存输入等同未缓存计价。正文结论与确定性任务摘要重复属于后续展示优化观察，不阻塞本次收尾。

### 5.2 本地提交前最终检查

- 小范围格式/lint 整理后，后端全量 **1375 passed / 4 skipped，12.55 秒**；前端本次收尾 **57 passed / 0 failed**，`vue-tsc --noEmit` 退出 0。
- 全部 **53 个本次 Python 文件 Black 检查通过**；全部 **15 个新增 Python 文件 Ruff 检查通过**。没有声称旧文件的全部 Ruff 告警已清零。
- `git diff --check`、本地文档链接检查和批准方案 SHA256 核对；按明确路径暂存本次代码/测试/文档，不使用全仓库盲目暂存。排除 `.env`、密钥文件、日志、运行数据及临时探针。

后端复跑命令（`nl2sql` 目录）：

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  -u http_proxy -u https_proxy \
  .venv/bin/python -m pytest src/tests/db_deepagent/unit \
  src/tests/unit/services/test_llm_factory.py src/tests/unit/api/v2 \
  -q --disable-warnings
```

前端命令（`frontend` 目录）：

```bash
node_modules/.bin/vue-tsc --noEmit
node --test $(rg --files src/modules/nl2sql -g '*.test.cjs' -g '*.test.ts' | sort)
```

## 6. 未验证边界（如实保留，不等同实现缺陷）

1. **真实 PG 跨进程锁 / DELETE**：提交后的补充隔离验收已在用户现有 PostgreSQL 18.1 的专用测试库完成，**7 项通过**，覆盖双向进程锁竞争、引用保护/过期回收、dry-run、真实触发器删除异常后的恢复及缺源停止，见 [真实 PG 验收记录](doc63_postgres_isolation_validation.md)。未运行实际 Runner/model、checkpoint 线程删除、网络断连或强杀进程等完整场景；不将上述通过范围泛化为全部 PG 故障已验收。
2. **完整真实模型题集与预算余量**：已通过 §5.1 的三次正常真实模型查询，但未跑完整固定题集、真实模型状态重提、紧预算中止或真实模型故障；R4-01 等异常注入为 Store GET 故障，非真实模型耗尽。
3. **仓库外归档消费者**：未核查外部读取 QA/run_errors/run_snapshots/WebStore 的兼容性。
4. **全面浏览器人工验收**：用户通过 Vue 手工完成上述三次正常查询并提供结果截图；尚未验证刷新后历史回读、浏览器错误/安全拒绝等完整流程。单测、截图及正常路径不能代替全面验收。
5. **上线状态**：用户已在本地启动后端用于真实查询验证；R6/R7 PASS 与本次本地提交均**不表示已部署生产或全部外部集成验收完成**。

## 7. 当前状态

- 用户在 R7 PASS 与三次真实查询核对后，已授权 Codex 整理并提交到当前本地分支；本记录随同提交保存。**未推送、未合并、未部署生产**；Codex 未重启服务、未清理真实资源、未额外调用模型或业务库，未使用 Orca 或旧终端/Dispatch。
- doc 6.3 批准稿及其 SHA256 保持不变；本次 M1/M2 为 R6 PASS 后的收尾与局部防御性增量，不重新开发 6.3、不重写账本/来源核验/清理体系、不扩大需求。
- 本增量已由 Codex 完成 **R7 PASS** 独立复审，M1/M2 关闭，没有待交回实现者的修改项。外部验证边界保持，不把审核通过表述为已上线。协调记录见 [doc63_implementation_coordination.md](doc63_implementation_coordination.md)。
