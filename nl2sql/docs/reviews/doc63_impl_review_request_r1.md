# doc 6.3 实施首次审核请求（R1 / 最多 4 轮）

- 日期：2026-11-13
- 实施者：右下 Claude（term_1f3c33d8）；审核：左侧 Codex（term_8eed56cb）
- 方案：`docs/6.3_db_deepagent已登记任务终态与答案覆盖门控方案.md`
  SHA256 `c393d2ea5fe558964467e14f82fe45839d290d7cae808578b2300547ea021593`
  （全文含 §13.7；本轮未改文档）
- 代码基线：`50067ce`（未提交工作区 diff，未 commit/push）
- 变更规模：36 文件 +5035/−977（含测试）；生产 27 文件、测试 9 文件、前端 4 文件

## 变更清单（按 §10 表）

| 位置 | 内容 |
| --- | --- |
| 新 `services/task_ledger.py` | 整批候选计划、add/revise/status/void 状态机、版本先检（task_plan_conflict 含完整重放）、完整规范化操作 no-op（不递增版本）、执行 DAG（自依赖/重复/未知/环/前向引用）、终止审计引用、blocked_by 根因链核验与生成、replacement 快照、每批同 ID 单操作、成果归属校验 |
| 新 `services/task_completion.py` | 共享终态/证据/血缘/覆盖/no_data/状态判定（§7.3 顺序、§7.4 映射表）；旁证资格（规则 4）；unresolved 恰覆盖；问题合并 `merge_delivery_open_questions`（§7.2 四步算法） |
| 新 `services/result_facts.py` | `known_saved_row_count`（type is int 且 ≥0）/`is_proven_untruncated`（全组显式 False）/按身份聚合（冲突→未知），与 6.2 `_known_saved_row_counts` 复用同一实现 |
| 新 `tools/task_tools.py` | `record_sub_tasks`（单独调用检查、错误包带 current_plan_version+任务摘要、no_op 回执明确当前状态） |
| 新 `tools/_task_errors.py` | 错误事件工厂（`ERR-`+uuid4 hex；重放沿用原 ID；`failure_ref` 随错误包返回）；`source_task_ref` 仅在服务端核验目标后填入 |
| 新 `persistence/session_guard.py` | `SessionGuardLease` + PG（复用已持有锁）与 memory（进程内 guard，签发实例随凭据校验）双后端；`validate_lease` 作用域+存续校验 |
| `schemas.py` | SubTaskRecord/TaskPlanRecord/TaskRef 族/AnalysisInputRef（结构化输入血缘）/DirectReuseRef/TerminationContext/ReplacementSnapshot；ToolErrorRecord 完整身份+error_id；FindingItem finding_id+task_refs（参数层格式与提交内唯一）；DbAgentFinalAnswer.unresolved_task_refs（default_factory=list）；RunFailureRecord+task_completion_rejected；ValidationRecord/ExecutionRecord/AnalysisRecord 任务归属字段 |
| `state.py` | `db_sub_tasks`/`db_task_plans` 通道（键=规范 JSON 编码 `(run,task,rev)`；身份校验；status/void 同键更新允许——写入准入由服务层+计划单调 reducer 把关）；计划 reducer 严格 +1/幂等/旧版拒绝；`db_tool_errors` 专用 `append_dedup_tool_errors`（完整 error_ref 键、事实含 tool_call_id、单侧内部校验）；执行/分析事实字段扩展 |
| `services/sql_pipeline.py` | auth_key + purpose+sub_task_id+revision；LedgerView 扩展（current_tasks 先最大版再判 void、require_current/writable、executions/analyses_of_run、evidence_by_id、records_by_result_ref、error_by_ref 同引用重复拒绝、task_snapshot）；副作用前任务准入（`require_task_ready_for_work`：依赖全 completed）；validate/execute/validate_and_execute 任务参数透传；占位符输入血缘（唯一归属+声明依赖检查+结构化 AnalysisInputRef）；LedgerUpdate.tool_errors |
| `tools/sql_tools.py` | validate/execute 必填 sub_task 参数；任务准入与 SQL 失败统一留错误账本（failure_ref 返回；准入失败不伪造归属）；预算停止不留任务错误账本 |
| `tools/result_tools.py` | analyze_result 结构化输入 `[{result_ref, evidence_id}]`（服务端核对匹配）+目标任务必填；probe 输入拒绝、同任务当前 revision、跨任务须声明依赖且 completed、血缘递归去重+环检测；下推继承任务；ResultMeta.source_task；read_result 只读不变 |
| `tools/guard_tools.py` | reuse_evidence 双模式：T/D 裸 ID（歧义拒绝）/E/A 完整来源引用（thread+run+evidence，候选索引同键异事实 evidence_source_conflict、超目录拒绝、无任务归属来源拒绝）+目标任务必填；新 ID=sha256(run,task,rev,直接来源完整键)（多目标不冲突）；direct_reuse_from 与链首 reused_from 分存；会话守卫凭据校验（session_guard_required） |
| `tools/final_answer.py` | 共享门控接入（结构归一后一次语义判定）；拒绝不写记录、错误包带 expected_status/uncovered/pending；通过时记录 plan_version/规则版本/coverage_map |
| `middleware/final_answer_gate.py` | 提醒带待处理任务摘要（有限次数） |
| `runner.py` | `_finalize` 一次归一+一次语义判定→`eligible_submission` 贯穿评估/正文/条件 b；`should_fallback = invoke_started and (eligible is None or not answer.strip())`（§7.5 谓词）；task_completion_rejected 失败类型（结构合法被拒）与 no_final_answer（未提交/结构无效）区分；兜底正文追加确定性任务摘要；交付 open_questions=合并列表（原列表保真归档）；`_seal_run_snapshot`（写 QA/run_errors 前封存全部任务版本+原始记录+覆盖结论+interrupted_tasks）；QA/run_errors 保存 snapshot_ref；会话守卫租约注入 ctx（PG 复用已有锁/memory 进程内）；携带证据 thread_id 经 ResultMeta 补齐+交接清单暴露完整来源引用 |
| `persistence/archive.py` | RUN_SNAPSHOT_SCHEMA_VERSION=1；write/read_run_snapshot（幂等/冲突报错）；read_task_snapshot/read_evidence_snapshot（未知版本拒绝）；refs_of_record 兼容结构化 input_result_refs；list_run_snapshots/list_run_snapshot_sessions |
| `persistence/maintenance.py` | R2/R3 收口：按资源所属 session 分组进同一把会话锁；锁内重读归属/年龄；保留闭包=仍在保留期 QA/run_errors+携带引用+运行快照递归展开（分析/占位符输入）；孤儿快照按归档保留期清理；坏元数据目录不按 mtime 强删（未来 created_at 保守保留）；memory 模式跨进程清理拒绝（memory_in_process 显式同进程） |
| `services/result_store.py` | ResultMeta.source_task；delete/purge_expired 收口（须有效 lease+会话清单；无全局绕过入口） |
| `agent.py`/`tools/__init__.py` | record_sub_tasks 入 build_business_tools；STEP4 14 件/FULL 16 件（todos 17）；TOOL_VISIBLE_FIELDS 同步（含 reuse/analyze/validate/execute/submit 新参数）；include_result_tools=False 分支同样含任务工具 |
| `config/prompts.yaml` + `skills/query-planning/SKILL.md` | 任务登记工作流（先登记再查库、单独调用、版本确认、终态登记规则、findings 覆盖与 unresolved 分工、no_data 严格口径、dependency_blocked 批量登记） |
| `api/v2`（dto/webstore/runs） | WebTurn.open_questions 可选（旧缺 None/新无问题 []）；write_turn 参数+安全拒绝分支写 []；dto 来源白名单投影（内部加字段不再严格解包被拒）；public_turn 非法值按 None |
| `cli.py` | 标签改"澄清与补充问题"；交付 open_questions 即合并列表 |
| 前端 | `types.ts` WebTurn.open_questions（null/[]/list）；`useChat.snapshotTurn` 与服务端保存值一致；MessageList 历史消费保存值（null 不显示）；AnswerCard "澄清与补充问题" 标题 |

## 方案对应表（契约十条）

1. 私有通道/reducer/完整身份/计划版本/执行依赖 vs 终止审计分离/revise/void 历史：`state.py`+`task_ledger.py`（§4/§5）
2. 版本先检幂等（旧版本完整重放冲突、同 content 异 dependencies 冲突、合法 no-op 不递增）：`task_ledger.py` + `test_task_ledger.py::TestVersioningAndIdempotency`
3. 授权/执行/分析/沿用任务归属、仅 pending/in_progress 可写、probe 独立：`sql_pipeline.require_task_ready_for_work` + 各工具 + `test_result_tools.py::TestAnalyzeWriteAdmission` + `test_reuse_ea.py`
4. E/A 完整来源引用/可见目录/direct_reuse_from/运行快照/保留闭包/T-D 独立：`guard_tools.py`+`archive.py`+`maintenance.py`
5. failure_refs/服务端 error_id/专用 reducer/同引用异事实拒绝：`_task_errors.py`+`state.append_dedup_tool_errors`
6. 终态/finding_id/task_refs/覆盖/unresolved/严格 no_data：`task_completion.py`+`final_answer.py`
7. 共用规则一次归一一次判定贯穿状态/正文/条件 b；非空坏提交系统兜底、原始保真：`runner._finalize/_evaluate` + `test_runner_persistence.py::TestTaskGateFallbackIntegration`
8. 确定性问题合并/WebTurn/WebStore/finalize hook/前端/CLI 一致、None 与 [] 区分：`task_completion.merge_delivery_open_questions`+api/v2+前端
9. §6.5 会话 guard 持锁覆盖 P3；R2/R3 受控删除、锁内重读闭包、未知归属跳过；memory 边界：`session_guard.py`+`runner`+`maintenance`
10. 工具装配/可见参数/提示词/预算真实图：`agent.py`+prompts/skill+`test_model_retry.py::TestRetryNoSqlReexecution`（五步最短脚本 6 尝试/5 步骤）

## 验证结果

```bash
# 后端（测试进程内隔离 SOCKS 代理环境变量）
.venv/bin/python -m pytest src/tests/db_deepagent/unit \
  src/tests/unit/services/test_llm_factory.py src/tests/unit/api/v2 -q
# → 1212 passed, 4 skipped（含新 38+39+36+17 例）

# 前端
node_modules/.bin/vue-tsc --noEmit   # → 0 错误
node --test src/modules/nl2sql/composables/useChat.test.cjs \
  src/modules/nl2sql/components/*.test.cjs src/modules/nl2sql/utils/*.test.ts \
  src/modules/nl2sql/sqlHighlight.test.ts src/modules/nl2sql/queryHomeLink.test.ts \
  src/modules/nl2sql/pageGuard.test.ts   # → 41 pass

# 格式/lint（新文件 0 问题；改过的生产文件 black 通过；
# ruff 存量债未动——基线 179 项，未清理与本任务无关文件）
```

## 未验证边界（如实）

1. **真实 PG advisory-lock 跨进程互斥未实测**：无隔离测试库；本实现未引入新锁机制（PG 侧仅把 Runner 已持有的 SessionAdvisoryLock 包装为 lease），并发证据来自 memory guard 的线程屏障测试与既有 PG 锁测试。`maintenance` 的 PG result_snapshots 会话过滤经 FakeConn 验证，未连真 PG。
2. **真实模型/数据库题集未执行**（不发起付费调用）：§11 末两行（真实题集预算余量、漏拆意图评估）未验证；五步脚本计数由离线 ScriptedChatModel 全链路覆盖。
3. **旧 checkpoint/旧归档过渡**：未上线直接切换前提，无迁移；旧无任务账本的提交按 §8.2 语义拒绝（有对应回归）。
4. **仓库外归档消费者**：v2 QA 新增 snapshot_ref 字段的外部读取方未核查（沿用 6.2 边界）。
5. WebTurn.open_questions 前端仅有类型+存量测试；未新增前端专项快照用例。

## 需重点复核位置

- `services/task_ledger.py` `_apply_status`（转移合法性顺序、幂等比较口径、blocked_by 守卫——曾因重构吞守卫，已修复并有回归）
- `services/task_completion.py` `evaluate_submission`（§7.3 步骤顺序与 §7.4 映射的一致性、旁证资格规则 4、expected_status 计算）
- `runner.py` `_finalize/_evaluate`（优先级：持久化→runtime→预算→task_completion_rejected→no_final_answer；should_fallback 谓词；快照封存时机）
- `maintenance.py` `_cleanup_session_resources/_protected_closure`（闭包不动点、孤儿判定、坏元数据不删）
- `guard_tools.py` reuse E/A 模式（候选索引、冲突/歧义判定、ID 生成）
- `state.py` reducer 语义取舍：任务记录同键更新允许右值覆盖（写入准入在服务层+计划单调 reducer）——请确认该分层符合 §5.3 第 4 点意图

## git diff 基线

- HEAD=`50067ce`，全部改动未提交（`git diff HEAD` 可审）；
  工作区另含已批准的 doc 6.3 文档修订与任务/协调文档（不在本次代码审核范围）。
