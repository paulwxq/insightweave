# doc 6.3 实施复审请求（R7）

- 日期：2026-11-13
- 实施者：接手会话（用户明确授权）
- 基线：`50067ce2bc7dbd273c42f81f31fce0eab255ebf5`，分支 `feature/multi-source-knowledge-isolation`，全部为**未提交工作区 diff**；未 reset / clean / 覆盖 / 清理任何既有实现或测试
- 权威方案：`docs/6.3_db_deepagent已登记任务终态与答案覆盖门控方案.md`（含 §13.7），SHA256 `c393d2ea5fe558964467e14f82fe45839d290d7cae808578b2300547ea021593`，**未修改、哈希未变**
- 上轮结论：[codex_doc63_impl_review_r6.md](codex_doc63_impl_review_r6.md) **PASS**：R5-01 关闭、无新增必修正确性缺陷、doc 6.3 代码审核循环闭环
- 本轮性质：**R6 PASS 之后的收尾与局部防御性增量**，不是重新开发 6.3、不重写账本/来源核验/清理体系、不扩大需求
  - **M1**（收尾文档同步实现状态）：成立，已处理（仅文档）
  - **M2**（删除排序失败时明确拒绝执行）：合理，已核实并作局部防御性改进（1 个业务文件 + 1 个新增测试文件）
- 轮次状态：累计 **R1–R7**（不清零、不重置；R6 PASS 为已通过历史事实，不因本轮重新待验）。本次请求 **R7 增量独立复审**。**未自行宣布 Codex 审核通过**，自查不算独立外审

## 1. 逐项核实与处理

### M1 · 收尾文档应同步实现状态

**判断：成立，已处理（纯文档，不改业务代码）。** 核实到 R6 报告 §5 M1 指出的状态差异属实：6.1 的 02 仍为「当前缺口」、03/04 仅标「配套改造」；doc 6.3 §1「新账本和门控仍未实施」为方案编写时的 `50067ce` 历史基线措辞；v4 详细设计未描述 `db_sub_tasks`、完成门控、运行来源快照等契约。

| 处理 | 修改位置 | 内容 |
| --- | --- | --- |
| 6.1 状态同步 | `docs/6.1_db_deepagent能力补齐问题清单与实施建议.md` §2 P0 表 02—04 | 性质列由「当前缺口／配套改造」改标「**代码已实现、离线审核通过**」，附 [收尾记录](doc63_implementation_closeout.md) 与 [R6 报告](codex_doc63_impl_review_r6.md) 链接；**保留原「问题说明」「补充建议」两列历史背景**，并加注说明这些是 `50067ce` 分析基线、不代表 R6 后仍存在缺口。**06—13 条目状态未改动**，不随本次宣告完成 |
| 新增收尾记录 | `docs/reviews/doc63_implementation_closeout.md`（新建） | 实现范围及与 6.1 的 02—04 对应、关键代码/持久化/对外契约、R1—R6 审核索引（明确 R6 PASS）、已验证结果、本次 M1/M2 处理、未验证边界、未提交未部署状态；不复制审核报告正文 |
| v4 详细设计同步 | `docs/v4/5_db_deepagent详细设计.md` | 顶部「实施状态补记」；在 §5.2 / §5.3 / §7.11 / §7.12.1 / §8 / §13.3 / §13.5 / §14.3 / §15.2 各加一行实际契约交叉引用；**新增 §22** 综合同步（22.1 6.1 对应、22.2 状态/计划版本/专用 reducer、22.3 任务身份与来源血缘、22.4 共享完成门控与 Runner 交付资格、22.5 运行来源快照/保留根/清理互斥/依赖删除、22.6 API/历史展示契约、22.7 审核索引与验证边界）。只补受影响内容，未大规模改写无关设计 |
| 历史措辞说明 | 6.1、v4 §22、收尾记录 | 明确 doc 6.3 内「当前／尚未实施／当前代码没有…」等措辞描述的是 `50067ce` 分析基线，不代表 R6 之后工作区实现状态；为保持批准稿固定哈希**不改写批准稿** |
| 口径区分 | 6.1 加注、v4 §22 提示、收尾记录 §1/§5/§6 | 明确「代码已实现、离线审核通过」≠「已上线／真实环境已验收」；真实 PG、模型题集、仓库外消费者、浏览器验收仍为未验证 |

文档验证：3 个被编辑/新建文档的相对链接逐一解析通过（除本报告 `doc63_impl_review_request_r7.md` 作为本次产物）；文档引用的代码符号（`db_sub_tasks`、`merge_task_records`、`EvidenceGraphVerifier`、`_order_orphan_deletions`、`OrphanDeletionOrderError`、`RUN_SNAPSHOT_SCHEMA_VERSION`、`snapshotTurn` 等）逐一 grep 确认存在；doc 6.3 SHA256 与开头一致、未变。

### M2 · 删除排序函数的环兜底改成明确拒绝

**判断：合理，采纳为局部防御性改进。** 核实 `maintenance.py::_order_orphan_deletions()` 原在无法覆盖整个 `deletion_set` 时执行 `ordered.extend(sorted(within - done))`，把剩余节点按名称拼接返回。R6 报告已说明正常支持路径无可达反例（`_protected_closure` 阶段 1 已对成环保守停止），故 M2 不是推翻 PASS 的新缺陷，而是避免未来调用路径变化后把「确定性顺序」误当「满足依赖的顺序」的防御。

**修改位置与关键控制流**（仅 `src/modules/db_deepagent/persistence/maintenance.py`）：

1. 新增模块局部专用异常 `OrphanDeletionOrderError`（不改动共享 `errors.py` 异常分层）。
2. `_order_orphan_deletions()`：删除「按名称拼接剩余节点」兜底，改为在 `len(ordered) != len(within)` 时抛出 `OrphanDeletionOrderError`，消息含覆盖数与未解决节点名——**明确失败，不返回带未满足依赖节点的部分顺序**；正常无环图仍返回消费者先于来源的完整稳定拓扑顺序（共享来源等全部 consumer 处理完才入列）。
3. `_cleanup_session_resources()` 调用排序处：**窄捕获** `OrphanDeletionOrderError`，向 `report.skipped` 记录清晰原因（`孤儿快照删除排序失败：跳过该会话本轮全部孤儿运行快照删除（不使用部分顺序，等待下次重试）`），并令 `deletion_order = []`——**本轮零孤儿运行快照 delete、不使用部分排序结果、异常不穿透维护流程**。

**采用的理由（同等可靠且最简）**：专用局部异常 + 窄捕获比广义 `except Exception` 更不易掩盖无关错误；调用方直接把顺序置空，比「抛异常后靠外层兜底」更确定地保证「排序失败后不发生任何孤儿运行快照 delete」。未新增通用图框架、未重写现有血缘解析。**范围如实**：该检查只作用于本阶段孤儿运行快照 delete，不撤销本方法此前已完成的结果目录 / PG 快照清理，也未为整个维护过程新增事务；消费者先于来源、删除失败停止、TTL 规划、保留根、会话锁及既有坏图停止规则全部保留。

## 2. 测试要求覆盖与结果

新增 `src/tests/db_deepagent/unit/test_doc63_r7_regressions.py`（6 例），复用 `test_doc63_r5_regressions.py` 的真实维护夹具（`QaArchive` + `InMemoryStore` + 禁实删钩子 + memory lease），不连真实 PG / 模型；正常链/共享来源的顺序断言只补一处纯函数精度，其余复用既有 R5-01 回归，不重复新增。

| 测试要求 | 用例 | 断言 | 结果 |
| --- | --- | --- | --- |
| 1 正常链与共享来源仍正确依赖顺序 | `test_normal_chain_and_shared_source_full_order` | 顺序覆盖整个删除集，链 `consumer<middle<ancestor`，共享来源排在所有 consumer 之后 | 通过（集成层顺序/共享来源行为复用既有 R5-01 回归） |
| 2 无法完整排序明确失败、不返回按名称拼接剩余节点 | `test_cyclic_deps_raise_instead_of_partial_order`、`test_partial_dependency_gap_also_refused` | 抛 `OrphanDeletionOrderError`，消息含 `未满足来源依赖` / 覆盖数 / 未解决节点；独立节点 + 环的整批也拒绝 | 通过 |
| 3 实际 `_cleanup_session_resources` 排序失败受控 | `test_order_failure_reports_and_skips_all_deletes`、`test_order_failure_dry_run_also_no_delete` | 局部 `patch` 注入排序异常后：不抛穿透、`store.attempts == []`（零 delete）、快照全部保留、报告含清晰原因、无「已清理」；dry-run 同样零 delete | 通过 |
| 3 恢复与不使用部分结果 | `test_recovers_next_cycle_after_transient_order_failure` | 注入轮零 delete 且报告；下一轮按正常依赖顺序回收、无「缺源」 | 通过 |
| 4 保留既有 R5-01 正式回归 | `test_doc63_r5_regressions.py`（8 例，未改动） | 两种名称排序、消费者删除失败、共享来源、多跳、恢复后回收、锁内复核、dry-run 全部仍通过 | 通过 |

**局部异常注入的边界如实说明**：成环 / 排序失败的图是**人工构造**的防御性输入，用于验证排序函数与调用方边界；当前正常工具链在 `_protected_closure` 阶段 1 已对成环保守停止，不会把这种图送到排序函数。不把它表述为正常业务可产生的真实场景。

## 3. 精确测试命令与实际结果

维护相关专项（先跑，含 `test_maintenance_cleanup.py` 与 R2/R3/R4/R5/R7 维护回归）：

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
  src/tests/db_deepagent/unit/test_doc63_r7_regressions.py \
  -q --disable-warnings
# 153 passed in 1.14s
```

M2 专项：

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  -u http_proxy -u https_proxy \
  .venv/bin/python -m pytest \
  src/tests/db_deepagent/unit/test_doc63_r7_regressions.py -q --disable-warnings
# 6 passed
```

既有后端全量回归（任务指定命令）：

```bash
cd /opt/projects/insightweave/nl2sql
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  -u http_proxy -u https_proxy \
  .venv/bin/python -m pytest src/tests/db_deepagent/unit \
  src/tests/unit/services/test_llm_factory.py src/tests/unit/api/v2 \
  -q --disable-warnings
# 1375 passed, 4 skipped in 13.22s   （R6 Codex 基线 1369 passed → 新增 M2 回归 6 例）
```

格式 / lint / 差异（本轮改动范围）：

```bash
.venv/bin/black --check \
  src/modules/db_deepagent/persistence/maintenance.py \
  src/tests/db_deepagent/unit/test_doc63_r7_regressions.py
# 2 files would be left unchanged

.venv/bin/ruff check src/tests/db_deepagent/unit/test_doc63_r7_regressions.py
# All checks passed!

cd /opt/projects/insightweave && git diff --check
# clean
```

**存量与新问题分开记录**：`maintenance.py` 全量 `ruff check` 仍有 15 项**存量**告警（均在本轮未改动行：`I001`、`UP035`、`BLE001`×4、`RUF034`、`UP017`×6、`PLR1730`、`RUF100`，以及既有全角括号 `# noqa` 的 invalid 指令 warning；行号因新增异常类整体 +11，内容不变）。本轮新增代码区（`OrphanDeletionOrderError`、`_order_orphan_deletions` 失败分支、调用方 `try/except`）在完整 ruff 配置下**零新增告警**；新增测试文件 ruff 0 告警、black 通过。按「不借机大规模修整无关文件」要求，未改动这些存量行。

**文档验证**：6.1 / v4 / 收尾记录 / 本报告的相对链接逐一解析通过；6.1 的 06—13 状态未被改动、完成状态未超出 02—04 已实现范围；批准方案 SHA256 未变；真实 PG、模型题集、仓库外消费者、浏览器人工验收仍明确为未验证；R6 PASS 未表述为已上线或全部外部集成验收完成。

**前端**：本轮未改任何前端源码或测试，无跨层关联影响，按任务「本轮未改前端则不重复运行前端检查」未重跑。最近一次独立前端验证为 **R5**（57 passed / 0 failed、`vue-tsc --noEmit` 通过），不表述为本轮新执行结果。

## 4. 未验证边界（如实保留）

1. **真实 PG 跨进程锁 / DELETE 未连库执行**：M2 回归使用 `InMemoryStore` 隔离 + 禁实删钩子，PG `result_snapshots` 的 DELETE 与跨连接互斥仍未在真实 PG 上执行。
2. **真实模型题集与预算余量未执行**：未发起付费模型调用、未跑真实模型耗尽 / 紧预算题集。
3. **仓库外归档消费者未核查**：未检查外部读取 QA/run_errors/run_snapshots/WebStore 的兼容性。
4. **浏览器人工验收未执行**：前端 Vue 行为测试不等同浏览器人工验收。
5. **上线状态**：R6 PASS 仅为当前工作区代码与已执行离线验证的代码审核闭环，不表示已部署或全部外部集成验收完成。
6. **M2 排序失败路径**：为人工异常注入的防御性验证，正常工具链阶段 1 已对成环保守停止，该异常图在当前正常业务中不可达。

## 5. git 基线 / 方案哈希 / 重点复审位置

- HEAD `50067ce2bc7dbd273c42f81f31fce0eab255ebf5`，分支 `feature/multi-source-knowledge-isolation`；79 个未提交变更全部保留（R1–R6 各轮实现、报告与测试 + 本轮文档 3 改 1 新增、业务 1 改、测试 1 新增）；本轮**未提交任何内容**。
- 批准方案 SHA256 `c393d2ea5fe558964467e14f82fe45839d290d7cae808578b2300547ea021593`（未变化；批准稿未改写，其中 §1 历史基线措辞保留）。
- 重点复审位置：
  - `persistence/maintenance.py`：`OrphanDeletionOrderError`（模块局部异常）、`_order_orphan_deletions()` 失败分支、`:916` 附近 `_cleanup_session_resources` 的窄捕获与 `deletion_order = []`
  - `src/tests/db_deepagent/unit/test_doc63_r7_regressions.py`（6 例 M2 验收清单）
  - `docs/6.1_…能力补齐问题清单与实施建议.md`（02—04 状态 + 历史措辞说明）
  - `docs/reviews/doc63_implementation_closeout.md`（新建收尾记录）
  - `docs/v4/5_db_deepagent详细设计.md`（顶部补记 + 各节交叉引用 + §22）

## 6. 状态声明

- **未提交、未推送、未合并、未部署、未重启服务**；未清理真实历史/结果，未调用付费模型或真实业务数据库，未使用 Orca 或旧终端/Dispatch，完成通知由用户人工传递。
- 开始修改前已确认无其他 Agent 同时写业务代码（源码最新 mtime 早于本轮开始、交互 pane 空闲、无 pytest 进程）；结束时源码最新 mtime 为本轮 `maintenance.py` 16:55，git status 指纹稳定。
- **R6 PASS 为已通过历史事实，本轮仅为之后的收尾与局部防御性增量，不把前面已审核功能描述为全部重新待验**。自查结果不算独立外审，**不自行宣布 doc 6.3 已验收**。等待用户人工通知 Codex 对本次增量做 R7 独立复审。
