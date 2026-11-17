# doc 6.3 实施复审请求（R4 / 最后一轮）

- 日期：2026-11-13
- 实施者：接手会话（用户明确授权；历史执行者约束已由交接替代）
- 基线：`50067ce`，分支 `feature/multi-source-knowledge-isolation`，全部为**未提交工作区 diff**；未 reset / 覆盖 / 清理任何既有实现或测试
- 权威方案：`docs/6.3_db_deepagent已登记任务终态与答案覆盖门控方案.md`（含 §13.7），SHA256 `c393d2ea5fe558964467e14f82fe45839d290d7cae808578b2300547ea021593`，**未修改**
- 上轮报告：[`codex_doc63_impl_review_r3.md`](codex_doc63_impl_review_r3.md)（5 组：1 P1、4 P2，另非阻塞 M1）
- 本轮处理：**R3-01…R3-05 逐项核实，5 组全部成立并修复**；M1 已补实际异常注入路径。新增回归 `test_doc63_r3_regressions.py`（36 例）。
- 轮次状态：已完成 R3/4；本次请求 **R4/4**。**未自行宣布 Codex 审核通过**，自查不算独立外审。R4 后若仍有问题，停止自动循环交由用户裁定，不进入 R5。

## 1. 共享根因与修复策略

R3-01/02/03 的共同根因是「沿用记录的继承输入属于**原证据 consumer scope**，但图核验按本轮目标重新解释；且解析函数对损坏/旧协议血缘静默降级为零条边」。R3-04 是「生成端与复核端没有共用决策时事实规则」；R3-05 是「清理把图合法性检查与保护标记混在一起，全图扫描自发创造保留根」。

- `services/reuse_qualification.py` 重写共享核心：
  - `evidence_edges()` 严格解析 `direct_reuse_from` / `input_result_refs` 容器与每条边的必需引用（R3-03）；缺省/空列表合法，存在却畸形抛 `SourceUnqualified`。
  - `EvidenceGraphVerifier.verify_input_edges()` 为单一共享消费资格规则（跨任务必须是上游选定成果、同任务探索保留、递归完整祖先）。
  - `verify_node()` 对带 `direct_reuse_from` 的沿用记录：目标身份（本轮 task/run）独立核验；继承输入先与权威来源逐项比对，再在原来源 consumer scope 中递归，不删血缘、不把历史 run 加入本轮 DAG（R3-01）。
  - `verify_consumed_edges()` 供 SQL 占位符与 analyze_result 两个工具侧入口复用，收敛此前按裸 evidence_id 的另一套递归。
- `sql_pipeline.py`：`LedgerView` 增只读 `state` / `archive` / `carried`（`make_ledger_view()` 由工具装配）；`resolve_placeholder_lineage()` 在 EXPLAIN/授权/实际执行之前调用共享消费资格规则（R3-02）。
- `tools/result_tools.py`：分析入口删除 `_validate_input_lineage()` 裸 ID 递归，改用 `verify_consumed_edges()`（R3-01）。
- `services/task_completion.py` 新增共享决策时事实规则 `terminal_facts` / `derive_decision_root` / `root_matches` / `validate_blocked_decision`；`services/task_ledger.py` 生成端复用 `derive_decision_root`，`_apply_void` 封存 `pre_void_terminal`（R3-04）。
- `persistence/maintenance.py::_protected_closure` 分离「阶段 1 图合法性检查（只读，不写 protected）」与「阶段 2 保护标记（只从最终 QA/run_errors/携带根扩展）」（R3-05）；畸形血缘解析失败记录 problem 并保守停止。

工具、内部服务、completed 登记、最终提交、Runner 与清理复用同一解析/核验规则，不再各自实现一套遍历。

## 2. 逐项判断、修改位置、成功/失败用例与实际结果

| 项 | 判断 | 修改位置（相对 `src/modules/db_deepagent/`） | 失败/反例用例 | 合法成功用例 | 实际结果 |
| --- | --- | --- | --- | --- | --- |
| **R3-01 P1** 合法分析成果沿用误拒 | 成立，已修 | `services/reuse_qualification.py:272` `verify_input_edges`、`:318` `verify_node` direct 分支、`:502` `verify_consumed_edges`；`tools/result_tools.py:277` 起删除裸 ID 递归；`services/sql_pipeline.py:282` `LedgerView.state/archive/carried`、`make_ledger_view()`；`tools/sql_tools.py`、`tools/guard_tools.py` 装配注入 | `test_forged_inherited_inputs_rejected`（伪造继承输入）、`test_source_invalidated_after_reuse_rejected`（来源后续 revise）、`test_missing_inherited_input_source_in_snapshot_rejected`（快照缺输入 E，live 有副本仍拒绝） | `test_query_analysis_reuse_completed_submit_runner_full_path`、`test_a_reuse_real_runner_finalize_delivery_archive`（真实 Runner 收尾/交付/归档）、`test_continue_consuming_reused_a_via_downstream_task`、`test_cross_thread_a_reuse_via_full_snapshot`、`test_multi_hop_a_reuse_via_snapshots`、`test_input_bearing_sql_result_reuse_succeeds`（带 inputs 的 SQL E 沿用） | `query→A→completed→reuse A→completed→submit→Runner` 全部成功；继续消费沿用 A（下游 T2）成功；同线程跨 run / 跨线程完整快照 / 多跳 / 输入型 SQL E 沿用成功；缺源、伪造继承输入、来源失效均拒绝。`lineage probe` 的 `VALID_A_REUSE` 由 `completed=stale_task_inputs` 变为 `completed: ok`；其 `consume_reused_a` 现报 `task_not_writable`（探针在已 completed 的 T1 上再次 analyze，属探针前提过期，正式用例改用下游 T2 覆盖） |
| **R3-02 P2** 占位符查库后才发现输入非法 | 成立，已修 | `services/sql_pipeline.py:116` 起 `resolve_placeholder_lineage` 副作用前共享核验（validate 与 execute 两处调用点均前置）；`make_ledger_view()` 注入归档/携带 | `test_unselected_placeholder_rejected_before_any_side_effect`（未选成果）、`test_invalid_ancestor_placeholder_rejected_before_side_effect`（已选成果的缺源祖先，故障注入绕过登记后仍由 SQL 入口拒绝） | `test_selected_upstream_placeholder_succeeds`、`test_same_task_exploration_placeholder_succeeds` | 非法输入 `validate=error, execute=error`；新增 EXPLAIN / DB 执行 / `db_validations` 授权 / `db_executions` 结果四类计数增量均为 0（`before == after` 断言）；合法已选上游与同任务探索继续成功。`lineage probe` 的 `UNSELECTED_PLACEHOLDER_SIDE_EFFECT` 因探针假设 execute 成功而在取 `e['data']` 处中止，属断言过期 |
| **R3-03 P2** 畸形血缘被静默跳过 | 成立，已修 | `services/reuse_qualification.py:53` `evidence_edges`（`_checked_evidence_ref` / `_checked_task_ref` 严格校验）；`persistence/maintenance.py:585` 起 catch `SourceUnqualified` → problem | `test_malformed_sealed_source_rejected_by_real_tool_path`（3 例：旧协议裸字符串、缺 evidence_ref 的 input、缺 evidence_ref 的 direct，均经真实 ToolNode `reuse` 拒绝且不写沿用成果）、`test_wrong_container_and_missing_task_ref_rejected`、`test_four_entries_reject_malformed_outcome`（completed 登记 / 提交快照 / Runner 一致拒绝）、`test_malformed_edge_stop_actual_cleanup`、`test_malformed_edge_does_not_break_runner_fallback` | `test_normal_empty_and_full_edges_accepted`、`test_input_bearing_sql_result_reuse_succeeds`、R3-01 全路径用例 | 三类故障注入来源 `reuse` 返回受控 `error`；错误容器/缺任务引用明确拒绝；正常 `[]`/缺省/合法完整边仍成功；清理控制流记录「血缘解析失败」并零实删候选；Runner 遇畸形边返回 `failed` + 系统兜底正文，无异常穿透破坏 6.2 兜底 |
| **R3-04 P2** 历史阻塞决策规则不一致 | 成立，已修 | `services/task_completion.py:129` 起共享决策事实规则、`:509` 起 `evaluate_submission` 改用 `validate_blocked_decision`；`services/task_ledger.py:340` `_resolve_blockage_root` 改固定版本推导并复用 `derive_decision_root`、`:1075` `_apply_void` 封存 `pre_void_terminal` | `test_c_empty_snapshots_and_forged_plan_rejected`（`dependency_snapshots=[]` + `decision_plan_version="forged"`）、`test_d_forged_second_blocked_by_rejected`（追加未被原决策覆盖的第二个 blocked_by）、`test_shared_helpers_used_by_generation_and_review`（伪造决策版本逐项拒绝） | `test_a_upstream_later_void_keeps_decision_and_accepts`、`test_b_new_downstream_traverses_fixed_root`、`test_batch_order_independent_and_reducer_applied`（正反序，经真实 `merge_task_records`） | `gate r3 probe` 四例全部转为期望：A `accepted=true` 且 `decision_context_unchanged=true`；B `accepted=true` 且 T3 保存 `T1@1/时间不明确`（不再追到 T1@2/NEW_ROOT）；C `accepted=false`（两条 `invalid_task_dependency`）；D `accepted=false`。旧决策 termination_context 深比较不变；void 封存 `pre_void_terminal` 保留原 NC 事实 |
| **R3-05 P2** 无保留根旧证据链无法回收 | 成立，已修 | `persistence/maintenance.py:496` 起 `_protected_closure` 两阶段分离；`_cleanup_session_resources` 的保守停止逻辑保留 | `test_read_failure_and_bad_graph_still_fail_closed`（保护根读取失败、缺源 → 零实删候选并报告）、`test_malformed_edge_stop_actual_cleanup` | `test_orphan_input_chain_is_recoverable`、`test_orphan_direct_chain_is_recoverable`、`test_rooted_chain_recursively_protected`、`test_remove_last_root_then_recoverable` | 无根 input/direct 链：`protected == set()`、快照为孤儿、实际清理 dry-run 产生两个到期删除候选；有 QA 根时递归保护 `{resA,resB}` 且零删除候选；移除最后根后重新清理可回收；读取失败/未知协议/缺源/成环仍报告并保守停止。`lifecycle r3 probe` 在 `assert protected == {SOURCE_REF, DERIVED_REF}` 处中止，属该断言编码了修复前行为 |
| **M1** 异常参数化未真正注入异常 | 采纳，已补 | `src/tests/db_deepagent/unit/test_doc63_r3_regressions.py::TestM1RunnerExceptionPaths` | — | `test_model_exception_uses_fallback_and_priority`（真实 `FakeAgent.invoke_side_effect=RuntimeError`）、`test_budget_stop_uses_fallback_and_status`（真实 `budget.close_run` 返回 `stop_reason="model_call_limit"`）、`test_unverified_completion_not_claimed_under_exceptions`（模型异常 + 缺成果） | 模型异常 → `failed` + 系统兜底正文 + 「未取得可确认的查询或分析结果」+ P1 runtime primary + run_errors 归档；预算停止 → `budget_exhausted` + 「已达到运行次数或时间限制」兜底；缺成果时兜底不把未核验 completed 说成成功。R2 的 `TestR208Fallback` 参数化纯规则用例保留，报告明确区分「纯规则」与「真实 Runner 异常路径」 |

### 关于 /tmp 四份 R3 离线探针（如实披露）

- `doc63_gate_r3_probe.py` 完整可跑，4 例全部显示修复后行为（见上表 R3-04）。
- `doc63_r3_closed_paths_probe.py` 完整可跑：guard 拒绝、E 直接沿用完整路径（`runner_accepted=true`）、沿用失败归属、携带 thread 身份等既有修复保持有效。
- `doc63_r3_lineage_probe.py`：`VALID_A_REUSE` 显示 R3-01 修复（`completed: ok`），`consume_reused_a` 因探针在已 completed 的 T1 上重复 analyze 触发 `task_not_writable`（正确准入行为，探针缺少下游任务），随后在 `UNSELECTED_PLACEHOLDER_SIDE_EFFECT` 处因探针假设 execute 成功而 `KeyError` 中止——R3-02 修复已使 execute 返回 error。
- `doc63_r3_lifecycle_probe.py`：在 `assert protected == {SOURCE_REF, DERIVED_REF}` 处中止，因 R3-05 修复后无根链 `protected=set()`，该断言编码了修复前行为。
- **未修改任何 /tmp 探针**，也未以探针退出码/断言成立与否宣称实现正确；上述反例与合法路径已全部转为 `test_doc63_r3_regressions.py` 中的正式回归断言。

## 3. 完整路径与相邻场景验证

| 要求 | 用例 | 实际 |
| --- | --- | --- |
| `query→A→completed→reuse A→completed→submit→Runner` | `test_query_analysis_reuse_completed_submit_runner_full_path` | 真实 ToolNode 累积 Command 更新；reuse ok、completed ok、submit 写入 `db_final_answers`、`build_completion_snapshot.reuse_failures == {}`、Runner `_task_completion_decision.accepted=true` |
| 真实 Runner 收尾/交付/归档（不只纯判定） | `test_a_reuse_real_runner_finalize_delivery_archive` | 真实 `DbAgentRunner.run()`（memory guard + FakeAgent + FakeStore）：`status=completed`，交付正文含合法 finding 文本「沿用分析结论：销售额123万元」，`archived=true`，写入 qa 而非 run_errors |
| TaskLedger 输出 → 实际 reducer | `TestR304HistoricalBlockedContract` 全部用例、`test_batch_order_independent_and_reducer_applied` | 经 `_apply` + `merge_task_records` 真实应用；void / 正反序批量终止 / 历史 void 均通过 |
| 实际 cleanup 控制流 | `TestR305CleanupRootSet` 全部用例 | 直接调 `_cleanup_session_resources`（真实 `InMemoryStore`、临时结果目录、dry-run 不删真实资源），断言 `report.result_dirs` 与 `report.skipped` |
| 副作用前置 | `TestR302PlaceholderPrecheck` | 断言 `db.explained` / `db.executed` / `db_validations` / `db_executions` 四类计数增量 = 0 |

## 4. 精确测试命令与结果

后端全量（`nl2sql` 目录，进程内隔离代理变量）：

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  -u http_proxy -u https_proxy \
  .venv/bin/python -m pytest src/tests/db_deepagent/unit \
  src/tests/unit/services/test_llm_factory.py src/tests/unit/api/v2 \
  -q --disable-warnings
# 1335 passed, 4 skipped in ~13s
```

R3 专项：

```bash
.venv/bin/python -m pytest \
  src/tests/db_deepagent/unit/test_doc63_r3_regressions.py -q --disable-warnings
# 36 passed
```

格式 / lint（本轮改动文件；存量债未动）：

```bash
.venv/bin/black --check <9 个本轮改动/新增 .py>       # 9 files would be left unchanged
.venv/bin/ruff check \
  src/modules/db_deepagent/services/reuse_qualification.py \
  src/modules/db_deepagent/services/task_completion.py \
  src/tests/db_deepagent/unit/test_doc63_r3_regressions.py   # All checks passed!
cd /opt/projects/insightweave && git diff --check        # clean
```

前端（`frontend` 目录；本轮未改前端源码/测试，TypeScript 类型与既有行为测试随全量复跑）：

```bash
node_modules/.bin/vue-tsc --noEmit                      # 退出 0，0 errors
node --test $(find src/modules/nl2sql -name '*.test.cjs' -o -name '*.test.ts' | grep nl2sql | sort)
# 57 pass / 0 fail
```

四份 /tmp 探针重放结果见 §2 末；`gate_r3` 与 `closed_paths` 完整通过，另两份因修复前断言过期中止。

## 5. 未验证边界（如实保留，不伪装为实现错误）

1. **真实 PG 跨进程锁 / DELETE 未连库执行**：清理验证使用 `InMemoryStore`/`FakeStore`、临时结果目录与 dry-run；未验证真实 PG 上的跨连接互斥与 DELETE。
2. **真实模型题集与预算余量未执行**：未发起付费模型调用；未在默认预算下跑简单题、登记修正、状态重提与紧预算中止的真实模型题集。M1 的预算/模型异常为 `BudgetSnapshot.stop_reason` 注入与 `FakeAgent.invoke_side_effect` 注入，不是真实模型耗尽。
3. **仓库外归档消费者未核查**：未检查外部读取 QA/run_errors/run_snapshots 的消费者兼容性。
4. **浏览器人工验收未执行**：前端 Vue 渲染行为测试是有效证据，但不等同浏览器人工验收。
5. **未提交、未推送、未合并、未部署、未重启服务、未清理真实历史/结果**；未调用 Orca（由用户人工传递完成通知）。
6. `ruff` 在部分存量文件仍有既有告警（`maintenance.py` 的 `UP035`/`BLE001`/`UP017`、`result_tools.py` 的 `ISC004` 等），本轮新增/重写文件 0 告警，存量债未改动。
7. R3-04 契约把 `blocked_by_root` 视为单一根因快照，因此复核要求「每个 blocked_by 推导出的根因都与已封存根一致」；这与现行生成端（仅从首个 blocked_by 派生单一根）一致。若后续批准方案要求多独立根，需另行扩展生成与复核字段——本轮按已批准契约实现，未擅自扩展。

## 6. git 基线 / 方案哈希 / 重点复审位置

- HEAD `50067ce2bc7dbd273c42f81f31fce0eab255ebf5`，分支 `feature/multi-source-knowledge-isolation`；66 个未提交变更全部保留（含 R1/R2/R3 各轮实现、报告与测试）；本轮未提交任何内容。
- 方案 SHA256 `c393d2ea5fe558964467e14f82fe45839d290d7cae808578b2300547ea021593`（未变化）。
- 重点复审位置：
  - `services/reuse_qualification.py`（全文：严格 `evidence_edges`、consumer scope 的沿用 direct 分支、`verify_input_edges` 共享规则、`verify_consumed_edges`）
  - `services/sql_pipeline.py`（`LedgerView.state/archive/carried`、`make_ledger_view`、`resolve_placeholder_lineage` 副作用前置）
  - `tools/result_tools.py`（删除裸 ID 递归，改用共享规则）、`tools/sql_tools.py` / `tools/guard_tools.py`（装配注入）
  - `services/task_completion.py`（共享决策时事实与 `validate_blocked_decision`）、`services/task_ledger.py`（`_resolve_blockage_root` 固定版本、`pre_void_terminal`）
  - `persistence/maintenance.py`（`_protected_closure` 两阶段分离、严格解析错误收口）
  - `src/tests/db_deepagent/unit/test_doc63_r3_regressions.py`（36 例验收清单，含 M1 真实异常路径）
  - `src/tests/db_deepagent/unit/test_doc63_r2_regressions.py`（两处 fixture 补完整 `source_task_ref` 以符合 R3-03 严格解析）

## 7. 轮次与状态

本次为 **R4/4 请求**（已完成 R3/4，更换工具或内部自测不重置轮次）。**现已暂停业务写入**，等待用户人工通知 Codex 进行 R4 独立复审；自查结果不算独立外审，不自行宣布审核通过。未使用 Orca 发送状态消息，也未伪造送达回执。R4 后若仍有未解决的正确性问题或需要改变保证范围，停止自动循环，明确列出剩余问题、修复障碍与缩减功能的具体代价，交由用户裁定，**不进入 R5**。
