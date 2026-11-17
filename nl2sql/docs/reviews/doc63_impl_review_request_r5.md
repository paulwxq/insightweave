# doc 6.3 实施复审请求（R5）

- 日期：2026-11-13
- 实施者：接手会话（用户明确授权）
- 基线：`50067ce2bc7dbd273c42f81f31fce0eab255ebf5`，分支 `feature/multi-source-knowledge-isolation`，全部为**未提交工作区 diff**；未 reset / 覆盖 / 清理任何既有实现或测试
- 权威方案：`docs/6.3_db_deepagent已登记任务终态与答案覆盖门控方案.md`（含 §13.7），SHA256 `c393d2ea5fe558964467e14f82fe45839d290d7cae808578b2300547ea021593`，**未修改**
- 上轮报告：[`codex_doc63_impl_review_r4.md`](codex_doc63_impl_review_r4.md)（R4：REQUEST_CHANGES，3 项必修 1 P1 + 2 P2，另非阻塞 M1）
- 本轮处理：**R4-01 / R4-02 / R4-03 与 M1 全部逐项核实成立并修复**；新增回归 `test_doc63_r4_regressions.py`（26 例）
- 轮次状态：已完成 R1–R4；本次请求 **R5**（用户明确更新「四轮后停止」为按事实与可行修法继续，保持真实累计轮次、不清零）。**未自行宣布 Codex 审核通过**，自查不算独立外审。

## 1. 共享根因与修复策略

- **R4-01**：`_finalize()` 中 `_task_completion_decision()` 的来源快照读取位于既有 P3 归档异常处理**之前**，且该 I/O 异常（psycopg.OperationalError / PoolTimeout）未被任何受控边界捕获，直接穿透 `run()` 不返回 `DbAgentResult`。修复在 Runner 收尾为「最终来源装配/门控 I/O」建立受控异常边界，**接入既有 `is_persistence_error` 分类**；把分类器下沉到零项目内依赖的 `errors.py`，供 runner 与 `final_answer` 共用而不产生循环导入。
- **R4-02**：`validate_blocked_decision()` 要求**每个** `blocked_by` 的推导根因都等于单一 `blocked_by_root`（仅从首项派生）。这不是批准契约；修复为 `blocked_by_root` 仅绑定首项，其余合法阻塞项逐项核验身份/快照/决策时事实/可推导根因，不强制同根；登记端 `task_ledger` 与复核端共享 `derive_decision_root`，规则保持一致。
- **R4-03**：孤儿运行快照原按各自 `sealed_at` 独立删除，跨周期中先删仍被未到期 dependent 引用的 source，下一轮全图缺源后永久保守停止。修复在 `_protected_closure` 现有来源图上暴露快照级来源边，`_cleanup_session_resources` 规划**依赖一致的删除集合**：保留快照引用的到期来源递归延迟，全部到期时一起回收。
- **M1**：`verify_node` 仅有 `visiting` 环检测、无完成节点缓存，共享祖先指数级重复遍历。新增按 `(完整身份, 核验角色)` 的实例级缓存，缓存命中仍逐边核对 expected task/result 约束。

工具、内部服务、completed 登记、最终提交、Runner 与清理复用同一解析/核验/分类规则，不各写一套。

## 2. 逐项判断、修改位置、成功/失败用例与实际结果

| 项 | 判断 | 修改位置（相对 `src/modules/db_deepagent/`） | 反例 / 失败用例 | 合法成功用例 | 实际结果 |
| --- | --- | --- | --- | --- | --- |
| **R4-01 P1** 来源快照 I/O 故障穿透 Runner 收尾 | 成立，已修 | `errors.py:12/25`（`persistence_error_types` / `is_persistence_error` 下沉）；`runner.py:686–733`（`_finalize` 受控边界：persistence 最高优先级 / runtime 收敛 / SourceUnqualified 受控业务拒绝 / 提交无交付资格）；`tools/final_answer.py:116–123`（广义 except 对持久化故障原样上抛）；`services/reuse_qualification.py:187–196`（`snapshot()` 注释明确 I/O 故障上抛、ValueError→SourceUnqualified） | `TestR401RunnerIOBoundary::test_plain_query_snapshot_io_fault_persistence_error`、`test_reuse_snapshot_io_fault_persistence_error`、`test_pool_timeout_classified_as_persistence`（真实 Runner.run + Store GET 注入） | `test_unknown_runtime_error_converges_to_failed_fallback`（runtime→failed+兜底）、`test_deterministic_bad_source_is_gate_failure_not_persistence`（确定性坏来源→受控门控拒绝）、`test_build_completion_snapshot_reraises_persistence` / `...keeps_deterministic_business_rejection`、`test_original_p0_model_exception_budget_behavior_preserved` | runner_persistence 探针 `returned=false,exception=OperationalError` → `returned=true,status=persistence_error,answer=系统兜底,evidence_count=1,invoke_count=1`；lineage 探针 `RUNNER_SNAPSHOT_IO_FAULT` → `persistence_error,archived=false,兜底正文`。status / primary=persistence / 兜底正文 / 已有证据 / archived=False 均校验；不对失败后端写 qa/run_errors、不追加模型调用。P0 跳过 invoke、模型异常优先级保持 |
| **R4-02 P2** 多个独立阻塞根被误拒 | 成立，已修 | `services/task_completion.py:225–318`（`validate_blocked_decision`：`blocked_by_root` 仅绑定 `index==0`，其余项逐项核验、不强制同根；保留 C/D 防御与 docstring） | `test_forged_root_still_rejected`（伪造首项根因）、`test_empty_snapshots_and_forged_plan_rejected`（旧 C）、`test_appended_forged_second_blocked_by_rejected`（旧 D，依赖快照与决策时事实不符仍拒绝） | `test_two_independent_failed_roots_accepted`、`test_failed_and_needs_clarification_two_roots_accepted`、`test_same_root_multiple_branches_accepted`（均经真实 TaskLedger→reducer→submit→Runner 全路径）、`test_historical_void_positive_kept`（历史 void 正例）、`test_real_runner_run_multi_root_delivery`（真实 Runner.run 交付/归档） | gate_r4 探针两例 `decision_accepted=false/submit=false/runner=false` → 全部 `true`，`tool_content=已接收最终答案`，`saved_root` 仍为首项 T1 摘要（不新增多根展示协议）。合法路径经真实 submit 工具写入 `db_final_answers` 与 Runner 共享判定接受；C/D/伪造根因/历史 void 全部保持 |
| **R4-03 P2** 跨周期清理制造缺源、随后永久无法回收 | 成立，已修 | `persistence/maintenance.py:502/521/675–692`（`_protected_closure` 可选 `edge_out` 暴露快照级来源图，复用阶段 1 已核验边）；`:696–733`（`_plan_orphan_snapshot_deletions` 依赖一致删除集：递归延迟 + 全部到期一起回收）；`:776/843–910`（`_cleanup_session_resources` 改用删除集 + 锁内重读 + 延迟报告） | `test_bad_protocol_and_missing_source_still_fail_closed`（坏协议/真实缺源保守停止）、`test_cycle_still_fail_closed`（成环保守停止） | `test_two_cycles_defer_then_reclaim_both`（真实 InMemoryStore snapshot delete，连续两次清理）、`test_input_multihop_deferral`、`test_direct_multihop_deferral`、`test_rooted_protection_then_remove_last_root` | 独立两周期验证：第一次 `remaining=['reuse','source']`（source 延迟、无悬空引用）、`skipped=延迟清理`；第二次 `remaining=[]`（一起回收）、无「缺源」、无「跳过该会话全部」。input/direct 多跳递归延迟成立；有根保护与移除最后根成立；坏协议/缺源/环仍报告并停止；未刷新 created_at、未延长 TTL。注：r4_lifecycle 探针在 `assert remaining_first == ['reuse']`（编码修复前行为）处中止，未修改探针，正确语义由正式回归覆盖 |
| **M1 非阻塞** 共享祖先重复遍历 | 采纳，已修 | `services/reuse_qualification.py:180–183`（实例级 `_verified_nodes`）；`:401–440`（`verify_node` 按 `(visit_key, role)` 缓存，命中前仍逐边核对 expected 约束、保留 `visiting`）；`:490/501`（成功后标记完成） | `test_role_transition_input_then_source_still_strict`（input 验证过不跳过 source 资格）、`test_each_edge_still_validates_expected_constraints`（缓存命中后伪造 expected 仍拒绝）、`test_cache_is_per_view_not_global`（不跨视图共享） | `test_shared_ancestor_visit_count_bounded`（10/14/18 节点访问次数近线性、failures 全空） | graph_dedup 探针 `verify_node` 调用 109/753/5167 → **17/25/33**，`failures={}`；角色转换与逐边约束回归通过。缓存仅实例级、不跨 run 全局共享 |

## 3. 完整路径与相邻场景验证

| 要求 | 用例 | 实际 |
| --- | --- | --- |
| 真实 Runner.run() 收尾（普通查询 + 历史沿用，两种持久化故障） | `TestR401RunnerIOBoundary` 前 3 例 | 返回 `DbAgentResult`（不穿透），persistence_error + primary persistence + 系统兜底 + 证据保留 + archived=False + invoke_count=1 + 无失败后端归档 |
| 真实 TaskLedger/reducer→submit→Runner | `TestR402MultipleIndependentRoots` 前 3 例 + `test_real_runner_run_multi_root_delivery` | 生成端接受、submit 写入、Runner 共享判定接受；真实 Runner.run 交付正文含业务结论、archived=True、无 `invalid_task_dependency` |
| 实际 `_cleanup_session_resources` 控制流（真实 Store 删除） | `TestR403DependencyConsistentDeletion` 全部 | apply=True + 真实 InMemoryStore delete + 隔离临时目录（rmtree 被 patch 禁止）；两周期、多跳、根保护、坏图保守停止均校验 |
| 异常边界分类（持久化/runtime/确定性业务） | `TestR401RunnerIOBoundary` 中后段 | OperationalError/PoolTimeout→persistence；未知 RuntimeError→failed 兜底；SourceUnqualified/畸形血缘→受控门控拒绝；均不穿透 |
| 6.2 既有保证 | `test_original_p0_model_exception_budget_behavior_preserved`、既有 `test_runner_persistence.py` 全套 | P0 持久化跳过 invoke、模型异常优先级、预算停止、归档条件 a/b、单层重试保持；后端全量通过 |

## 4. 精确测试命令与结果

后端全量（`nl2sql` 目录，进程内隔离代理变量）：

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  -u http_proxy -u https_proxy \
  .venv/bin/python -m pytest src/tests/db_deepagent/unit \
  src/tests/unit/services/test_llm_factory.py src/tests/unit/api/v2 \
  -q --disable-warnings
# 1361 passed, 4 skipped in 13.51s   （R4 基线 1335 passed → 新增 26 例）
```

R4 专项：

```bash
.venv/bin/python -m pytest \
  src/tests/db_deepagent/unit/test_doc63_r4_regressions.py -q --disable-warnings
# 26 passed
```

五份 R4 离线探针（`nl2sql` 目录，`PYTHONPATH=. .venv/bin/python /tmp/<文件>`）：
- `doc63_r4_runner_persistence_probe.py`：`returned=true,status=persistence_error,evidence_count=1,invoke_count=1`（修复前 `returned=false,exception=OperationalError`）
- `doc63_gate_r4_probe.py`：两例 `decision_accepted/submit_record_written/runner_accepted` 均 `true`
- `doc63_r4_lineage_probe.py`：`LEGAL_A_FULL_RUNNER` 全通、`RUNNER_SNAPSHOT_IO_FAULT` → `persistence_error/archived=false/兜底正文`；`UNSELECTED_PLACEHOLDER_FIXED`、`INVALID_ANCESTOR_PRECHECK_FIXED`、3 例 `MALFORMED_SOURCE_FIXED` 保持
- `doc63_r4_lifecycle_probe.py`：在 `assert remaining_first == ['reuse']` 处中止（该断言编码修复前「source 首轮被删」行为；修复后 source 延迟、两轮后一起回收）。**未修改探针**；正确语义见 `test_two_cycles_defer_then_reclaim_both`
- `doc63_r4_graph_dedup_probe.py`：`GRAPH_REVISITS` 10/14/18 节点 = 17/25/33 次，`failures={}`

格式 / lint（本轮改动文件；存量债未改动）：

```bash
.venv/bin/black --check <7 个本轮改动/新增 .py>       # 7 files would be left unchanged
.venv/bin/ruff check \
  src/modules/db_deepagent/errors.py \
  src/tests/db_deepagent/unit/test_doc63_r4_regressions.py   # All checks passed!
cd /opt/projects/insightweave && git diff --check          # clean
```

前端（`frontend` 目录；本轮未改前端源码/测试，随全量复跑）：

```bash
node_modules/.bin/vue-tsc --noEmit                      # 退出 0，0 errors
node --test $(find src/modules/nl2sql -name '*.test.cjs' -o -name '*.test.ts' | grep nl2sql | sort)
# 57 pass / 0 fail
```

说明：`runner.py` / `maintenance.py` 仍有既有存量告警（`runner.py` 的 `SUB_TASK_ID_PATTERN` F401、`submitted_payload`/`snapshot_ref` F841、既有行的全角 `# noqa`、I001；`maintenance.py` 的 UP035/BLE001/UP017/I001），均为 R2/R3 轮次已存在、本轮未引入；本轮新增/重写文件 `errors.py` 与 `test_doc63_r4_regressions.py` 的 ruff 为 0 告警。

## 5. 未验证边界（如实保留，不伪装为实现错误）

1. **真实 PG 跨进程锁 / DELETE 未连库执行**：R4-03 清理验证使用 `InMemoryStore` 的真实 `delete`，但 `result_snapshots` 的 PG `DELETE` 与跨连接互斥仍未在真实 PG 上执行；文件 `rmtree` 在回归中被 patch 禁止，不删除真实资源。
2. **真实模型题集与预算余量未执行**：未发起付费模型调用；未跑默认预算下的真实模型题集、状态重提与紧预算中止。R4-01 异常注入为 Store GET 故障与 `_task_completion_decision` 桩，非真实模型耗尽。
3. **仓库外归档消费者未核查**：未检查外部读取 QA/run_errors/run_snapshots 消费者的兼容性。
4. **浏览器人工验收未执行**：前端 Vue 渲染行为测试是有效证据，但不等同浏览器人工验收。
5. **未提交、未推送、未合并、未部署、未重启服务、未清理真实历史/结果**；未调用 Orca（由用户人工传递完成通知）。
6. **R4-01 相邻入口**：`final_answer.build_completion_snapshot` 的持久化故障上抛已单测覆盖；但「真实 ToolNode 中 submit 工具内触发该 I/O 故障→运行层 P1 分类」的完整链路仅由单测 + 既有 P1 分类代码保证，未额外构造端到端故障注入探针（正常 submit 链路探针已验证不受影响）。

## 6. git 基线 / 方案哈希 / 重点复审位置

- HEAD `50067ce2bc7dbd273c42f81f31fce0eab255ebf5`，分支 `feature/multi-source-knowledge-isolation`；70 个未提交变更全部保留（含 R1–R4 各轮实现、报告与测试）；本轮未提交任何内容。
- 方案 SHA256 `c393d2ea5fe558964467e14f82fe45839d290d7cae808578b2300547ea021593`（未变化）。
- 重点复审位置：
  - `errors.py`（`is_persistence_error` / `persistence_error_types` 下沉，零项目内依赖）
  - `runner.py:686–733`（`_finalize` 受控异常边界：persistence/runtime/SourceUnqualified 三路分类、提交无交付资格、P3 归档跳过条件）
  - `tools/final_answer.py:116–123`（广义 except 对持久化故障 `raise`）
  - `services/task_completion.py:225–318`（`validate_blocked_decision` 首项根因绑定、逐项核验保留）
  - `persistence/maintenance.py:502/675–733/843–910`（`edge_out` 来源图、`_plan_orphan_snapshot_deletions`、依赖一致删除集与锁内重读）
  - `services/reuse_qualification.py:180–183/401–501`（角色键缓存、逐边约束、环检测、不跨视图共享）
  - `src/tests/db_deepagent/unit/test_doc63_r4_regressions.py`（26 例验收清单）

## 7. 轮次与状态

本次为 **R5 请求**（已完成 R1–R4，保持真实累计轮次、不清零；用户已按最新裁定更新「四轮后自动停止」为按事实与可行修法继续）。**现已暂停业务写入**，等待用户人工通知 Codex 进行 R5 独立复审；自查结果不算独立外审，不自行宣布审核通过。未使用 Orca 发送状态消息，也未伪造送达回执。R5 后若仍有未解决的正确性问题或需要改变保证范围，按用户裁定处理，不因轮次自动放弃可行修法。
