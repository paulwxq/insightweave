# doc 6.3 实施复审请求（R3 / 最多 4 轮）

- 日期：2026-11-13
- 实施者：本次接手会话（用户明确授权接手；历史任务文件中"必须由原 Claude/pane 实施"的执行者约束已被本次交接替代）
- 基线：`50067ce`（doc 6.2 收尾），分支 `feature/multi-source-knowledge-isolation`，**全部为未提交工作区 diff**，未 reset / 覆盖 / 清理任何既有实现或测试
- 权威方案：`docs/6.3_db_deepagent已登记任务终态与答案覆盖门控方案.md`（含 §13.7），SHA256 `c393d2ea5fe558964467e14f82fe45839d290d7cae808578b2300547ea021593`，**未修改**
- 上轮报告：[`codex_doc63_impl_review_r2.md`](codex_doc63_impl_review_r2.md)（13 项：3 P1 + 10 P2，另 M1）
- 本轮处理：逐项核实 R2-01…R2-13 与 M1，**13 项全部成立并修复**，M1 采纳；新增可执行回归 `test_doc63_r2_regressions.py`（50 例）与前端行为测试 `openQuestionsBehavior.test.cjs`（6 例）
- 轮次状态：已完成 R2/4；本次请求 **R3**，最多剩 R4。**未自行宣布 Codex 审核通过**，自查不算独立外审。

## 1. 共享根因与修复策略

R2-01/02/03 的共同根因是「缺一个按完整身份解析、递归 direct/input 边、快照权威的共享证据图核验入口」：

- 重写 `services/reuse_qualification.py`：`EvidenceGraphVerifier` 按 `(session, thread, run, evidence)` 完整身份解析节点，快照存在即权威（缺源/缺任务不回退 live），无快照才用线程账本/轮换携带；`evidence_edges()` 统一提取 `direct_reuse_from` 与 `input_result_refs` 两种边；`verify_node()` 递归核验身份、result/task 引用、重复冲突、环、跨任务消费必须是上游选定成果、同任务探索输入保留；`role="source"` 额外要求来源为最终有效 completed 版本的选定成果。
- `evidence_edges()` 同时供 `persistence/maintenance.py::_protected_closure` 按完整来源身份追踪 direct/input 闭包（协议/作用域/存在性/环），避免各处写一套不完整的血缘遍历。
- 四入口（reuse 工具、completed 登记、提交门控、Runner）全部改为调用同一模块；Runner 与提交工具显式传入可信 `ctx.session_id/thread_id/carried`，不再从不存在的 `state.session_id` 推断。

工具、内部服务、completed 登记、最终提交与 Runner 判断因此使用一致规则（回归 `test_four_entry_points_agree_on_unqualified_source` 同时验证合法来源一致接受、非法来源四入口一致拒绝）。

## 2. 逐项判断、修改位置与证据

| 项 | 判断 | 修改位置（路径相对 `src/modules/db_deepagent/`） | 回归测试（`test_doc63_r2_regressions.py` 等） | 实际结果 |
| --- | --- | --- | --- | --- |
| **R2-01 P1** 合法沿用把目标记录当来源；提交装配取 `state.session_id` | 成立，已修 | `services/reuse_qualification.py:420` `verify_reuse_source` 按 direct 引用解析真正的来源原记录（`visible_record` 仅做身份交叉核对）；`tools/final_answer.py:48,183` 与 `runner.py:869` 显式传可信 `ctx.session_id/thread_id`；`services/task_ledger.py:940` 不再把本轮新记录当来源 | `TestR201FullPath::test_legal_reuse_completed_submit_runner_same_thread`、`test_legal_reuse_cross_thread_snapshot`、`test_source_revised_reuse_rejected`；`test_four_entry_points_agree_on_unqualified_source` | 完整路径 `reuse→completed→submit→Runner` 全部成功；`snap.reuse_failures == {}`；同线程跨 run、跨线程快照、多跳来源均成功；来源 revise 后沿用被拒。`gate probe`: `legal_reuse_snapshot failures={}`、`legal_reuse_complete accepted=true`（修复前分别为 `来源记录与引用的 run 不符` 与 `stale_task_inputs`） |
| **R2-02 P1** 完整证据图与全部选定成果未核验 | 成立，已修 | `services/reuse_qualification.py:53,110,178,273,449`；`services/task_completion.py:540` 起「全部选定成果逐项核验」；`tools/result_tools.py:230` 跨任务输入必须上游选定成果；`persistence/archive.py:259` reader 增 thread 核对；`persistence/maintenance.py:496` 闭包完整 direct/input 遍历 | `TestR202EvidenceGraph` 全部 9 例（未选成果消费、派生绕过、快照缺输入、ghost 成果、wrong thread、四入口一致、多跳快照、删 checkpoint 快照、同任务分析/已选上游成果合法路径） | 5 个反例逐项拒绝；`gate probe`: `extra_primary_ghost accepted=false`、`wrong_thread_snapshot_readable` 改为抛 `ValueError`；合法同任务分析、已选上游成果、多跳沿用与删 checkpoint 后完整快照仍成功 |
| **R2-03 P1** 清理失败后仍继续删除 | 成立，已修 | `persistence/maintenance.py:496` `_protected_closure` 校验协议/作用域/保护根存在性/全部 direct/input 缺源/重复/环并返回 `problems`；`persistence/maintenance.py:679` `_cleanup_session_resources` 合并 `closure_problems + graph_problems`，任一问题即 `report.skipped` 追加"跳过该会话全部…删除"并 `return` | `TestR203CleanupControlFlow` 6 例（读取失败、坏协议、缺源、成环、缺根、正常会话仍可清理） | 真实清理控制流下 `report.result_dirs == []` 且 `skipped` 含失败原因；正常会话仍产生到期删除候选。`lifecycle probe` 的旧 `assert report.result_dirs`（编码修复前行为）因此失败，属探针自身断言过期 |
| **R2-04 P2** reducer 禁止终态合法 void | 成立，已修 | `state.py:171,188,204` 新增共享 `is_legal_void_transition`（要求 `void_operation` + 非空白 reason，且不可变事实由 reducer 先比对）；`task_record_update_allowed` 单独判定 void；`services/task_ledger.py:1100` `_apply_void` 复用同一规则 | `TestR204ReducerVoid` 4 例（3 个终态→voided 经真实 TaskLedger 输出 + 真实 `merge_task_records`；completed→pending 与同 revision 改 content 仍拒绝）；`test_ledger_reducers.py` 全通过 | 3 个终态→voided 全部 `accepted=true`；非法覆盖仍 `LedgerConsistencyError` |
| **R2-05 P2** 历史 blocked/replacement 按当前候选重验、输入被原地改写 | 成立，已修 | `services/task_ledger.py:470` 候选改 `copy.deepcopy`；`_validate_candidate` 第 3/4 步只处理 `modified_ids` 中的**新决策**，历史决策不重验、不重生成；`services/task_completion.py:432` 对历史终止记录按固定版本复核 | `TestR205HistoricalDecisions` 3 例（void 后 revise 目标、blocked 后 revise 上游、旧快照前后深比较不变） | 两处 revise 均成功；`gate probe`: `replacement_revised / blocked_upstream_revised accepted=true`，`old_replacement_snapshot_mutated before == after` |
| **R2-06 P2** 新终止快照混用 revision、早于原因派生 | 成立，已修 | `services/task_ledger.py:309` `_dependency_state_snapshot` 改按固定 `(run,id,revision)` 版本索引取事实；`_validate_candidate:1232` 建 `version_index`，第 3 步先派生全部服务端原因、第 5 步再封存依赖快照，按 `sorted(modified_ids)` 保证批次顺序无关 | `TestR206Snapshots` 3 例（旧版依赖终止、三跳同批正反序） | `gate probe`: `old_dep_new_status` 快照 `A@1 completed`（修复前 `A@1 pending`）；`root_snapshots` T3 保存 T2 的最终服务端原因 `依赖 T1@1 阻塞（根因：ROOT）`（修复前 `PLACEHOLDER`） |
| **R2-07 P2** 交付正文只显示任务目标 | 成立，已修 | `services/task_completion.py:64,587,815` `CompletionDecision.finding_summaries` + `build_delivery_answer_section` 逐项展示合法 finding 文本及对应分项；`runner.py:735` 调用点不变 | `TestR207Delivery` 2 例（模型 answer 无结论仍展示；失败/澄清说明继续存在） | `gate probe`: `delivery_finding_missing` section 含 `销售额为123万元` |
| **R2-08 P2** 无提交时 expected_status 被当已核验 | 成立，已修 | `services/task_completion.py:540` 任务级成果核验移到 `normalized_submission is None` 之前，独立于提交；`CompletionDecision.verified_completed_refs`；`runner.py:918` `_fallback_task_section` 改用逐项核验结论 | `TestR208Fallback` 4 例（无提交/预算停止/模型异常 × 缺证据；合法核验可显示已完成） | `gate probe`: `missing_submission_ghost_completion expected_status=null, errors=[task_evidence_mismatch], section=已登记完成（未核验）` |
| **R2-09 P2** 本地分析工具/内部服务无 guard | 成立，已修 | `services/result_analysis.py:350` `analyze()` 入口 `require_session_lease`；`tools/result_tools.py:412` 工具路径复用；`services/sql_pipeline.py:192` `getattr` 兼容只读探针 | `TestR209Guard` 3 例（ToolNode 无凭据拒绝且不写 A；直调内部服务拒绝；有效凭据成功） | 3 例通过；`test_result_analysis.py` 的 `CTX` 补真实进程内 guard 凭据（独立会话，不与 `s-test` 冲突） |
| **R2-10 P2** 再次轮换拼错直接来源 thread | 成立，已修 | `schemas.py:186,215` `ExecutionRecord/AnalysisRecord` 增 `session_id/thread_id`；`services/sql_pipeline.py:1022`、`tools/result_tools.py:501`、`tools/guard_tools.py:864` 记录保存自身完整 scope；`runner.py:1593` `_origin_of_record` 优先用记录自身身份，不从 ResultMeta 补造 | `TestR210Carry::test_two_rotations_carry_direct_source_identity` | 真实 `ResultMeta`（文件生成于 `t-probe`）下，新记录携带 origin 为 `thread=t-middle, run=run-mid, evidence=新ID`，清单一致 |
| **R2-11 P2** 核验目标后的沿用失败未绑定任务 | 成立，已修 | `tools/guard_tools.py:631` `_ea_error` 默认 `owned=True`（闭包定义在 `require_task_ready_for_work` 通过之后）；`evidence_source_conflict` 也经同一错误包；未知目标/准入失败仍用普通 `error_envelope` 不伪造归属 | `TestR211FailureBinding` 2 例（failure_ref 原样登记 `failed/tool_failed` 成功；未知目标不伪造归属） | 数据源不符场景 `failure_ref.source_task_ref` 为字典，`record_sub_tasks` 登记成功 |
| **R2-12 P2** interrupted_tasks 参数形状错误 | 成立，已修 | `runner.py:1305` `_seal_run_snapshot` 传本 run 过滤后的**任务 mapping**（非 `{"db_sub_tasks": ...}` 包装） | `TestR212Interrupted` 4 例（pending、in_progress、无中止项、含其他 run 同名任务） | pending/in_progress 正确进入 `interrupted_tasks`；其他 run 任务被忽略 |
| **R2-13 P2** 门控接受缺失阻塞依据的 dependency_blocked | 成立，已修 | `services/task_completion.py:375` 起：必填 `blocked_by`/`termination_context`（含 `blocked_by_root`、`dependency_snapshots`）、每个 blocked_by 的固定历史版本存在性、`_historical_blocked_root` 按 `all_task_records` 独立推导并 `_root_matches` 逐项比对（伪造根因拒绝）；不要求上游仍是当前失败状态 | `TestR213BlockedAudit` 5 例（依据缺失、context 缺失、旧版本不存在、伪造根因、历史依据合法但上游 revise 仍接受） | `gate probe`: `missing_blocked_audit accepted=false`；合法历史依据路径 `accepted=true` |
| **M1** 前端测试只是源码正则 | 采纳，已补可执行行为测试 | `frontend/.../composables/useChat.ts:54` 把 `snapshotTurn` 提为模块级纯导出（不改算法）；新增 `frontend/.../openQuestionsBehavior.test.cjs` | `openQuestionsBehavior.test.cjs` 6 例 | 真实 `useChat.ts` + `compiler-sfc` 编译的真实 `MessageList.vue` + Vue `createRenderer` 挂载；验证三态、快照/历史一致、completed 非空即展示、不安全终态写 `[]` 且原问题不泄漏。源码正则测试保留作辅助，报告明确区分"源码契约检查"与"行为验收" |

### 关于 /tmp 三份离线探针（如实披露）

- `doc63_gate_r2_probe.py` 可完整重放，16 个 case 全部显示修复后行为（见上表引用）。
- `doc63_r2_lineage_probe.py` 首个 case `VALID_REUSE_REJECTED` 现输出 `completed: ok`、`batch_failures_with_real_session: {}`、`submit_snapshot_failures: {}`（R2-01 反例转为正例）；随后在第 69 行中断，原因是探针自身假设"completed 登记会失败"——修复后该登记成功，任务进入 completed，后续再对同一 completed 任务执行 `execute_sql` 被 `require_writable_task` 正确拒绝，探针取 `q['data']['evidence_id']` 抛 `KeyError`。**这是探针前提过期，不是实现错误**；未修改 /tmp 探针，改以 `test_doc63_r2_regressions.py` 中显式构造的非法祖先来源用例验证输入血缘传播。
- `doc63_r2_lifecycle_probe.py` 在第 67 行 `assert report.result_dirs` 中断，因为 R2-03 修复后该会话零实删候选；其 `wrong_thread_snapshot_readable` 分支也会因 reader 增 thread 核对而改为抛 `ValueError`。两者都是旧断言编码了修复前行为。
- 上述事实不以探针退出码或断言成立与否宣称实现正确；正式回归以仓库内 pytest / node:test 用例为准。

## 3. 用户指定的完整路径验证

| 要求 | 用例 | 实际 |
| --- | --- | --- |
| `reuse → completed → submit → Runner` 成功正例 | `TestR201FullPath::test_legal_reuse_completed_submit_runner_same_thread`（真实 ToolNode 累积 Command 更新，不手工拼最终状态） | 通过：reuse ok、completed ok、submit 写入 `db_final_answers`、`evaluate_submission` 与 `Runner._task_completion_decision` 均 `accepted=true`；另覆盖同线程跨 run、跨线程快照、来源 revise 拒绝 |
| `TaskLedger 输出 → 实际 reducer 应用` | `TestR204ReducerVoid`（3 终态→voided）；`test_four_entry_points_agree_on_unqualified_source`（合法 completed 登记经 `merge_task_records`）；`TestR205` / `TestR206` 全部经 `_merge` 真实 reducer | 通过 |
| 实际 cleanup 控制流 | `TestR203CleanupControlFlow`（直接调 `_cleanup_session_resources`，真实 `FakeStore/InMemoryStore`、临时结果目录、dry-run 不删真实资源） | 通过：失败场景零实删候选、正常会话仍可清理 |

## 4. 精确验证命令与结果

后端（在 `nl2sql` 下，进程内隔离代理变量）：

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  -u http_proxy -u https_proxy \
  .venv/bin/python -m pytest src/tests/db_deepagent/unit \
  src/tests/unit/services/test_llm_factory.py src/tests/unit/api/v2 \
  -q --disable-warnings
# 1299 passed, 4 skipped in ~13s
```

R2 专项：

```bash
.venv/bin/python -m pytest \
  src/tests/db_deepagent/unit/test_doc63_r2_regressions.py -q --disable-warnings
# 50 passed
```

格式 / lint（仅本轮改动文件；存量债未动）：

```bash
.venv/bin/black --check <18 个本轮改动 .py>          # 18 files would be left unchanged
.venv/bin/ruff check services/reuse_qualification.py \
  src/tests/db_deepagent/unit/test_doc63_r2_regressions.py   # All checks passed!
cd /opt/projects/insightweave && git diff --check        # clean
```

前端（在 `frontend` 下）：

```bash
node_modules/.bin/vue-tsc --noEmit                      # 0 errors
node --test $(find src/modules/nl2sql -name '*.test.cjs' -o -name '*.test.ts' | grep nl2sql | sort)
# 57 pass / 0 fail（含新增 openQuestionsBehavior 6 例）
node --test src/modules/nl2sql/openQuestionsBehavior.test.cjs   # 6 pass / 0 fail
```

离线探针（仅作交叉核对，不代替上面断言）：`PYTHONPATH=. .venv/bin/python /tmp/doc63_gate_r2_probe.py` 完整可跑；另两份因旧断言过期中断，详见 §2 末。

## 5. 未验证边界（与 R2 报告一致，如实保留）

1. **真实 PG 跨进程互斥/真实 PG DELETE 未连库执行**：清理控制流验证使用 `FakeStore`/`InMemoryStore`、临时结果与 dry-run；未验证真实 PG 上的跨连接锁与 DELETE。
2. **真实模型题集与预算余量未执行**：未发起付费模型调用；未在默认预算下跑简单题、登记修正、状态重提与紧预算中止的真实模型题集。
3. **仓库外归档消费者未核查**：未检查外部读取 QA/run_errors/run_snapshots 的消费者兼容性。
4. **浏览器人工验收未执行**：M1 使用真实 SFC 编译 + Vue 渲染器挂载，不等同浏览器人工验收。
5. **未提交、未推送、未合并、未部署、未重启服务、未清理真实历史/结果**；未调用 Orca（用户明确说明由人工传递完成通知）。
6. 源码正则测试 `openQuestionsContract.test.cjs` 保留为辅助，不作为行为验收结论；`ruff` 在部分存量文件仍有既有告警（如 `UP035`/`SIM102`/`F402` 等），本轮新增/重写文件 0 告警，存量债未改动。

## 6. git 基线 / 方案哈希 / 重点复审位置

- HEAD `50067ce2bc7dbd273c42f81f31fce0eab255ebf5`，分支 `feature/multi-source-knowledge-isolation`；59+ 个未提交变更全部保留（含上一实现者的 R1/R2 实现与测试）；本轮未提交任何内容。
- 方案 SHA256 `c393d2ea5fe558964467e14f82fe45839d290d7cae808578b2300547ea021593`（未变化）。
- 重点复审位置：
  - `services/reuse_qualification.py`（全文重写：快照权威降级次序、四入口共享、`evidence_edges`、完整身份解析与环检测）
  - `services/task_completion.py`（任务级成果核验与提交解耦、`finding_summaries`、`verified_completed_refs`、R2-13 历史根因复核）
  - `services/task_ledger.py`（`_validate_candidate` 新/旧决策分离、`version_index` 固定 revision、深拷贝候选、void 共享规则）
  - `state.py`（`is_legal_void_transition` / `task_record_update_allowed`）
  - `persistence/maintenance.py`（`_protected_closure` 完整血缘与 problems、`_cleanup_session_resources` 失败保守停止）
  - `persistence/archive.py:259`（reader thread 核对）
  - `runner.py`（`_origin_of_record`、`_seal_run_snapshot`、`_fallback_task_section`、显式 ctx 身份传递）
  - `tools/final_answer.py`、`tools/guard_tools.py`、`tools/result_tools.py`、`services/result_analysis.py`
  - `src/tests/db_deepagent/unit/test_doc63_r2_regressions.py`（50 例验收清单）
  - `frontend/src/modules/nl2sql/openQuestionsBehavior.test.cjs`（M1 行为验收）

## 7. 轮次与状态

本次为 **R3 请求**（已完成 R2/4，最多剩 R4；更换工具不重置轮次）。**现已暂停业务写入**，等待独立审核者 R3 复审；自查结果不算独立外审，不自行宣布 Codex 审核通过。未使用 Orca 发送状态消息，由用户人工传递完成通知。R4 后若仍有未解决问题，停止自动循环交由用户裁定。
