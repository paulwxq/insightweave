# doc 6.3 实施复审请求（R6）

- 日期：2026-11-13
- 实施者：接手会话（用户明确授权）
- 基线：`50067ce2bc7dbd273c42f81f31fce0eab255ebf5`，分支 `feature/multi-source-knowledge-isolation`，全部为**未提交工作区 diff**；未 reset / 覆盖 / 清理任何既有实现或测试
- 权威方案：`docs/6.3_db_deepagent已登记任务终态与答案覆盖门控方案.md`（含 §13.7），SHA256 `c393d2ea5fe558964467e14f82fe45839d290d7cae808578b2300547ea021593`，**未修改**
- 上轮报告：[`codex_doc63_impl_review_r5.md`](codex_doc63_impl_review_r5.md)（R5：REQUEST_CHANGES，仅 1 项 P2：R5-01）
- 本轮处理：**仅 R5-01**，逐项核实成立并修复；新增回归 `test_doc63_r5_regressions.py`（8 例）。R4-01 / R4-02 / R4-03 / M1 成果原样保留，未扩大功能或重构范围。
- 轮次状态：累计 **R1–R6**（不清零、不重置）。用户已更新「四轮后停止」为按事实与可行修法继续；本次请求 **R6**。**未自行宣布 Codex 审核通过**，自查不算独立外审。

## 1. 对 R5-01 的判断及证据

**判断：成立，已修。** 审核报告定位的 `maintenance.py` 执行阶段（当时 `:863` 按 `sorted(deletion_set)` 键名排序；`:871` 锁内复核不满足条件仅 `continue`；`:891–899` 删除异常仅记录后继续其它删除）确实存在「删除集合已考虑 TTL 与来源依赖，但逐项执行时部分失败，仍会删掉失败 consumer 的来源」。

修复前用真实 `MaintenanceService._cleanup_session_resources()` + 真实 `QaArchive` + 内存 `InMemoryStore.delete` 一次性失败注入，重放 `/tmp/doc63_r5_lifecycle_probe.py`，两例均复现：

| 场景 | 修复前首轮实际结果 | 修复前次轮 |
| --- | --- | --- |
| source=`a-source`、consumer=`z-child`（source 名称在前） | `actual_successful_deletes=["a-source"]`，`z-child` 删除失败 → `remaining_first=["z-child"]`（缺源） | `来源证据 a-source:E-a-source 缺失（缺源）` + `跳过该会话全部` → `remaining_second=["z-child"]` 持续残留 |
| source=`z-source`、consumer=`a-child`（consumer 名称在前） | `a-child` 先删除失败，仍继续删 `z-source` → `remaining_first=["a-child"]`（缺源） | 同上，`z-source:E-z-source` 缺失 → 持续残留 |

根因：删除顺序按名称而非依赖，且删除失败 / 锁内复核需保留时都只 `continue`，没有把「该 consumer 本轮实际保留」反馈到它的来源祖先。

## 2. 修改文件、函数与关键控制流

本轮只改 1 个业务文件、新增 1 个测试文件：

| 文件（相对 `src/modules/db_deepagent/`） | 修改 |
| --- | --- |
| `persistence/maintenance.py` | 新增 `@staticmethod _order_orphan_deletions()`（`:736`）；重写 `_cleanup_session_resources()` 中孤儿快照执行循环（`:909–976`） |
| `src/tests/db_deepagent/unit/test_doc63_r5_regressions.py` | 新增（8 例验收清单） |

关键控制流：

1. `_plan_orphan_snapshot_deletions()`（既有，未改）仍负责 TTL 删除集合规划、递归延迟、全部到期一起回收；`graph_out["snapshot_deps"]` 仍来自 `_protected_closure` 阶段 1 已核验的来源图，未另写血缘解析。
2. 新增 `_order_orphan_deletions(deletion_set, snapshot_deps)`：对 `snapshot_deps[consumer] = {source…}` 做**稳定拓扑排序**，保证每条 consumer→source 边 consumer 排在 source 之前；共享来源要等其全部 consumer 处理完才进入顺序；对环做确定性兜底（阶段 1 已有环检测并保守停止，不会进入删除）。
3. 执行循环改用 `deletion_order` 而非 `sorted(deletion_set)`，对每个节点严格区分三种结果：
   - **已确认节点不存在**（锁内整份重读 `live_by_key` 无此键）→ `continue`（等同已删，不会产生悬空引用）；
   - **节点仍存在但不应删除 / sealed_at 未知**（`sealed is None or sealed >= archive_before`）→ 记 `abort_reason` 并 `break`（保留性跳过，绝不删其来源祖先）；
   - **删除失败 / 状态未知**（`delete_run_snapshot` 抛异常）→ 记录 `孤儿快照清理失败` 并 `break`（绝不把失败当成删除成功）。
   - `apply=False`（dry-run）仍逐项报告 `孤儿快照待清理 (dry-run)`、不实删、不触发停止，保持既有 dry-run 行为。
4. 循环后若 `abort_reason` 非空，追加一条会话级 `停止该会话本轮剩余孤儿快照删除…等待下次重试` 记录；`deferred`（TTL 延迟）报告循环保持不变。
5. 既有会话锁（`acquire_memory_lease` / PG advisory lock）、锁内重读 `live_by_key`、保护根闭包、严格血缘、坏图保守停止、R2 结果目录 / R3 PG 快照清理、Web 命名空间清理均**未改动**。

## 3. 删除顺序与失败停止如何共同保证来源不被误删

- 「只改依赖顺序但失败后继续」不足：consumer 删除失败后仍会走到（排在后面的）来源，重现第二个反例。
- 「只在异常时停止但仍按键名排序」不足：source 名称在前时会在 consumer 失败之前先被删，重现第一个反例。
- 两者结合才成立：**consumer 先于来源**的依赖顺序保证任何来源都排在引用它的 consumer 之后；**第一次失败/保留即停止**保证一旦某 consumer 本轮实际没被删除，执行游标停在该 consumer 处，它的全部来源祖先（拓扑序中更靠后）本轮一次都不会进入 `delete`。因此「本轮保留的快照不会指向本轮刚删除的来源」，恢复后下一轮重新规划即可从干净图继续回收，不会因清理自身制造的缺源而永久保守停止。这允许暂时延迟独立分支回收，未新增事务框架或持久化体系。

## 4. 失败反例、合法成功路径、相邻场景与对应测试

新增 `test_doc63_r5_regressions.py`（8 例，全部使用真实 `_cleanup_session_resources` + 真实 `InMemoryStore.delete`，`_FaultStore` 一次性失败注入，`shutil.rmtree` 被 patch 禁止实删，memory lease 持锁；不连真实 PG / 模型）：

| 验收项 | 用例 | 断言 | 结果 |
| --- | --- | --- | --- |
| 1/2/3/4：source 名称在前、consumer 名称在后，consumer 一次删除失败 | `test_consumer_delete_failure_keeps_source_and_recovers[source_sorts_first]` | 首轮 `a-source`、`z-child` 均在，`a-source` 不在 delete attempts，记录 `清理失败` + `停止该会话本轮剩余`，无 `缺源` / `跳过全部`；恢复后次轮 `remaining=[]`，无 `缺源` / `跳过全部` | 通过 |
| 1/2/3/4：consumer 名称在前、source 名称在后，同样失败 | `test_consumer_delete_failure_keeps_source_and_recovers[consumer_sorts_first]` | 同上（`z-source` 保住） | 通过 |
| 5：多跳依赖，保留 consumer 时来源祖先仍保留 | `test_multihop_retained_consumer_keeps_all_source_ancestors` | `z-source←middle←z-child`，`z-child` 失败 → 三者都在，`middle`/`z-source` 不在 attempts，无 `缺源`；次轮全部回收 | 通过 |
| 6：共享来源，仍有未删除 consumer 时来源不能被删 | `test_shared_source_kept_while_consumer_undeleted` | `a-source` 被 `b-child1`/`c-child2` 共享，`b-child1` 失败 → `a-source` 仍在、未进 attempts，无 `缺源`；次轮全部回收 | 通过 |
| 7：consumer 先删成功、来源删除失败，下次仍恢复 | `test_source_delete_failure_after_consumer_success_recovers` | 首轮 `remaining=["z-source"]`（consumer 已不存在，不形成缺源引用），记录失败 + 停止，无 `缺源`；次轮 `remaining=[]`、无 `缺源` / `跳过全部` | 通过 |
| 8：锁内复核发现 consumer 仍需保留，不删其来源 | `test_lock_recheck_retention_does_not_delete_source` | patch 第 2 次 `list_run_snapshots`（锁内重读）使 `a-child` sealed_at 变为未到期 → 复核判定需保留 → `store.attempts==[]`（一次 delete 都不执行），`a-child`/`z-source` 均在，记录 `锁内复核发现孤儿快照` + 停止，无 `缺源`；补丁移除后次轮正常回收 | 通过 |
| 9 + 合法成功路径：dry-run 行为不变 | `test_dry_run_reports_all_pending_without_deleting` | dry-run 报告 `a-child`/`z-source` 两条 `待清理 (dry-run)`，无实删、无停止 | 通过 |
| 合法成功路径：无故障时依赖顺序同轮一起回收 | `test_clean_expired_chain_reclaimed_in_one_cycle` | `remaining=[]`，delete attempts 顺序 `["a-child", "z-source"]`（consumer 先于来源），无 `缺源` / 停止 | 通过 |

相邻场景与既有保证（验收 9）由本仓库既有维护回归继续覆盖，本轮**未重复新增**无关功能测试：TTL 多周期 / 有根保护 / 移除最后根 / 坏协议·真实缺源·成环保守停止 / dry-run 见 `test_doc63_r3_regressions.py::TestR305*`、`test_doc63_r4_regressions.py::TestR403DependencyConsistentDeletion`、`test_maintenance_cleanup.py`；均在维护专项与后端全量复跑通过。

**独立两序验证脚本**（`nl2sql` 目录，`PYTHONPATH=. .venv/bin/python`，真实内存 Store 一次性失败注入，逐周期记录 delete attempts）输出：

```json
{"case":"source_sorts_first","cycle1_attempts":["z-child"],"cycle1_remaining":["a-source","z-child"],"cycle1_source_deleted":false,"cycle1_stop":true,"cycle1_missing_source":false,"cycle2_attempts":["z-child","a-source"],"cycle2_remaining":[],"cycle2_missing_source":false,"cycle2_skip_all":false}
{"case":"consumer_sorts_first","cycle1_attempts":["a-child"],"cycle1_remaining":["a-child","z-source"],"cycle1_source_deleted":false,"cycle1_stop":true,"cycle1_missing_source":false,"cycle2_attempts":["a-child","z-source"],"cycle2_remaining":[],"cycle2_missing_source":false,"cycle2_skip_all":false}
```

即首轮只尝试 consumer、来源一次都不碰、两者都保留、停止记录存在、无缺源；恢复后次轮按 `consumer→source` 顺序一起回收、无缺源、无 `跳过该会话全部`。

**离线探针说明**：`/tmp/doc63_r5_lifecycle_probe.py` 前两例（R4-03 多周期、有根/移除最后根）仍通过并打印 `remaining_by_cycle=[["child","middle","source"],["child","middle","source"],[]]`、`remaining_after_last_root_removed=[]`；后两例的 `assert rem_first == [child] and rem_second == [child]` 及 `缺失`/`跳过该会话全部` 断言**编码修复前行为**，修复后 `rem_first` 为 `[source, child]`、次轮 `[]`，故探针在该断言处中止（exit 1）。按任务要求**未修改探针的问题断言**，修复后正确语义由上述仓库内正式回归固定。

## 5. 精确测试命令与实际结果

维护专项（先跑，含既有 R2/R3/R4/R5 维护回归）：

```bash
cd /opt/projects/insightweave/nl2sql
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  -u http_proxy -u https_proxy \
  .venv/bin/python -m pytest \
  src/tests/db_deepagent/unit/test_maintenance_cleanup.py \
  src/tests/db_deepagent/unit/test_doc63_r2_regressions.py \
  src/tests/db_deepagent/unit/test_doc63_r3_regressions.py \
  src/tests/db_deepagent/unit/test_doc63_r4_regressions.py \
  src/tests/db_deepagent/unit/test_doc63_r5_regressions.py \
  -q --disable-warnings
# 147 passed in 1.16s
```

R5 专项：

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  -u http_proxy -u https_proxy \
  .venv/bin/python -m pytest \
  src/tests/db_deepagent/unit/test_doc63_r5_regressions.py -q --disable-warnings
# 8 passed
```

既有后端全量回归（任务指定命令）：

```bash
cd /opt/projects/insightweave/nl2sql
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  -u http_proxy -u https_proxy \
  .venv/bin/python -m pytest src/tests/db_deepagent/unit \
  src/tests/unit/services/test_llm_factory.py src/tests/unit/api/v2 \
  -q --disable-warnings
# 1369 passed, 4 skipped in 13.74s   （R5 基线 1361 passed → 新增 8 例）
```

格式 / lint（仅本轮改动范围）：

```bash
.venv/bin/black --check \
  src/modules/db_deepagent/persistence/maintenance.py \
  src/tests/db_deepagent/unit/test_doc63_r5_regressions.py
# 2 files would be left unchanged

.venv/bin/ruff check \
  src/tests/db_deepagent/unit/test_doc63_r5_regressions.py
# All checks passed!

cd /opt/projects/insightweave && git diff --check
# clean
```

**存量与新问题分开记录**：`maintenance.py` 全量 `ruff check` 仍有 15 项**存量**告警，均位于本轮未改动的行（`I001:20`、`UP035:28`、`BLE001:162/487/1233/1351`、`RUF034:188`、`UP017:235/1062/1096/1309/1382/1383`、`PLR1730:237`、`RUF100:468`，以及 `:487/:996/:1218/:1233` 既有全角括号 `# noqa` 的 invalid 指令 warning）。本轮新增代码区（`_order_orphan_deletions` `:736`、执行循环 `:909–976`）在完整 ruff 配置下**零新增告警**（新增 `except Exception as exc:  # noqa: BLE001` 与既有同款、`BLE001` 在该配置下被抑制）；新增测试文件 ruff 0 告警、black 通过。按「不借机大规模修整无关文件」要求，未改动这些存量行。

**前端**：本轮未修改任何前端源码或测试，也无跨层关联影响，按任务「前端未修改则不必重复运行前端检查」未重跑前端。R5 Codex 独立基线（前端 57 passed / 0 failed、`vue-tsc --noEmit` 通过）仍为最近一次独立前端验证；本轮不把它表述为本轮新执行的检查。

## 6. 未验证边界（如实保留）

1. **真实 PG 跨进程锁 / DELETE 未连库执行**：R5-01 回归使用 `InMemoryStore` 的真实 `delete` 注入一次性失败，`result_snapshots` 的 PG `DELETE` 与跨连接互斥仍未在真实 PG 上执行；文件 `rmtree` 在回归中被 patch 禁止，不删除真实资源。
2. **真实模型题集与预算余量未执行**：未发起付费模型调用，未跑真实模型耗尽 / 紧预算中止题集。
3. **仓库外归档消费者未核查**：未检查外部读取 QA/run_errors/run_snapshots 消费者的兼容性。
4. **浏览器人工验收未执行**。
5. **6.2 既有兜底/状态/归档行为**由既有 `test_runner_persistence.py` 等后端全量回归覆盖通过；本轮未改 Runner/提交/reducer，未额外构造端到端模型故障探针。

## 7. git 基线 / 方案哈希 / 重点复审位置

- HEAD `50067ce2bc7dbd273c42f81f31fce0eab255ebf5`，分支 `feature/multi-source-knowledge-isolation`；73 个未提交变更全部保留（R1–R5 各轮实现、报告与测试 + 本轮 1 改 1 新增）；本轮**未提交任何内容**。
- 方案 SHA256 `c393d2ea5fe558964467e14f82fe45839d290d7cae808578b2300547ea021593`（未变化）。
- 重点复审位置：
  - `persistence/maintenance.py:736`（`_order_orphan_deletions` 稳定拓扑排序：consumer→source 边、共享来源、环兜底）
  - `persistence/maintenance.py:909–976`（`_cleanup_session_resources` 依赖顺序执行、三态区分、第一次失败/保留即停止、`abort_reason` 会话级记录、dry-run 不变）
  - `src/tests/db_deepagent/unit/test_doc63_r5_regressions.py`（8 例验收清单）

## 8. 状态声明

- **未提交、未推送、未合并、未部署、未重启服务**；未清理真实历史/结果，未调用付费模型或真实业务数据库，未使用 Orca 或旧终端/Dispatch。
- 开始修改前已确认无其他 Agent 同时写业务代码（源码最新 mtime 为上一轮 15:46、早于本轮开始 30 分钟以上；交互 pane 空闲 8 小时以上；无 pytest 进程）；结束时源码最新 mtime 为本轮 `maintenance.py` 16:32，git status 指纹稳定。
- **现已暂停业务写入**，完成通知由用户人工传递给 Codex；本会话未发送 Orca status 消息，也未伪造送达回执。自查结果不算独立外审，**不自行宣布 doc 6.3 已验收**。等待用户人工通知 Codex 进行 R6 复审。
