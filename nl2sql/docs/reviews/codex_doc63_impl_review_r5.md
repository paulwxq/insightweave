# doc 6.3 实施审核 R5

日期：2026-11-13。审核者：Codex；实施者：用户授权的 CodeBuddy / hy4 接手会话。反馈由用户人工转交，本轮未使用 Orca。

**结论：REQUEST_CHANGES，仅剩 1 项 P2。** 上轮 R4-01、R4-02、R4-03 的原反例及 M1 缓存建议已验证修复；本轮发现的是 R4-03 的相邻异常路径：删除集合正确，但逐项执行时发生部分失败，仍会留下缺源快照。

建议针对这一处继续修完，保留已验证成立的功能，不扩大方案、不重写清理体系，也不因累计轮次放弃。修复后提交 R6 复审请求；这不是自动进入新一轮开发或部署的通知，完成通知继续由用户人工传递。

## 1. 审核基线与实际验证

- 实施请求：[doc63_impl_review_request_r5.md](doc63_impl_review_request_r5.md)。
- 分支：`feature/multi-source-knowledge-isolation`；HEAD：`50067ce2bc7dbd273c42f81f31fce0eab255ebf5`。审核包含未提交修改和未跟踪的新实现、测试文件。
- 批准方案 SHA256：`c393d2ea5fe558964467e14f82fe45839d290d7cae808578b2300547ea021593`，核对未变。
- 本轮只审业务代码与测试；写入审核文档、协调记录及 `/tmp` 离线探针，没有修改业务实现或测试，没有提交、推送、部署、重启、付费模型调用、真实业务库访问或真实资源清理。

Codex 实际复跑：

| 检查 | 结果 |
| --- | --- |
| db_deepagent unit + llm_factory + API v2 | **1361 passed / 4 skipped**，14.12 秒 |
| 前端 nl2sql 全部 `.test.cjs` / `.test.ts` | **57 passed / 0 failed** |
| `vue-tsc --noEmit` | 退出 0，无错误 |
| `git diff --check` | 通过 |
| 原反例、真实工具图故障、缓存约束及清理失败探针 | 已独立重放，结果见下文 |

后端命令（`nl2sql` 目录）：

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

本轮独立重放的离线探针（`nl2sql` 目录，以 `PYTHONPATH=. .venv/bin/python` 执行；代理变量按上列清除）：

```text
/tmp/doc63_r4_runner_persistence_probe.py
/tmp/doc63_gate_r4_probe.py
/tmp/doc63_r4_lineage_probe.py
/tmp/doc63_r4_graph_dedup_probe.py
/tmp/doc63_r5_tool_graph_probe.py
/tmp/doc63_r5_cache_probe.py
/tmp/doc63_r5_lifecycle_probe.py
```

均完成、退出 0。探针会捕获并打印预期业务拒绝或残留反例，退出 0 不等于所有业务结果成功。使用内存 Store、临时隔离目录及模型/业务 DB 替身；生命周期探针只删除合成内存快照，并以禁止实删钩子防止真实目录删除。

## 2. 上轮逐项复核

| 上轮编号 | 本轮判断 | 复核证据 |
| --- | --- | --- |
| R4-01，持久化异常穿透收尾 | **原问题关闭** | Runner 来源快照读取注入 `OperationalError` / `PoolTimeout`，返回 `persistence_error`、系统兜底、`archived=False`；收尾阶段已取得的证据保留。另以实际 `build_db_agent/create_deep_agent` 图、脚本模型替身运行至提交工具，只在该次快照 GET 注入一次故障并立即恢复，确认异常从工具到 Runner 正确分类，不依赖后续再次失败。 |
| R4-02，独立多根阻塞被误拒 | **关闭** | 实际 TaskLedger/reducer→submit→Runner 接受独立上游失败；首项摘要绑定与其余项独立事实核验成立，伪造决策仍拒绝。实际 Runner 交付覆盖在全量回归中通过。 |
| R4-03，TTL 不同造成悬空引用 | **原正常路径关闭，尚有下节相邻异常路径** | 多跳孤儿链前三个清理周期分别剩余 `[child,middle,source]`、`[child,middle,source]`、`[]`；有根保护、移除最后根后可回收成立。 |
| M1，合法 DAG 重复核验 | **关闭** | 10/14/18 个节点的最后成果核验调用次数为 17/25/33，分别约 0.000/0.001/0.001 秒。缓存命中仍核验边上的任务/result_ref；预热 input 不绕过 source 的完成要求；环检测与实例隔离成立。 |

补充：真实工具图在 P1 调用阶段遇到持久化错误时，既有策略跳过 P2 checkpoint 读取，因此该路径未返回 checkpoint 内证据。此行为符合现行 v4 §14.3；不与“P3 收尾已取得证据应保留”的要求混淆，不另列缺陷。

## 3. R5-01 · P2 · 孤儿快照删除部分失败后仍删除来源，后续清理无法恢复

**代码证据**：`src/modules/db_deepagent/persistence/maintenance.py:847` 规划依赖一致的删除集合；`:863` 按键名 `sorted(deletion_set)` 执行；`:871` 锁内重读不满足条件时仅 `continue`；`:891–899` 删除异常仅记录后继续其他删除。

删除集合只考虑执行前“哪些节点保留”。实际删除失败或复核跳过后，该消费者成为本轮保留节点，但它的来源仍在待删集合中。当前也没有消费者先于来源的执行顺序保证。

**独立复现**：`/tmp/doc63_r5_lifecycle_probe.py` 使用真实 `MaintenanceService._cleanup_session_resources()`、真实 QaArchive 和内存 Store。source 与 consumer 都已过期、无保留根；仅让 consumer 的实际 Store.delete 第一次抛 `RuntimeError`，随后后端恢复。分别交换二者名称，覆盖两个字典序：

| 场景 | 首轮实际结果 | 后端恢复后的第二轮 |
| --- | --- | --- |
| source=`a-source`，consumer=`z-child` | 先删 source，consumer 删除失败；只剩 `z-child` | 缺失 `a-source:E-a-source`，停止该会话全部资源删除 |
| source=`z-source`，consumer=`a-child` | consumer 先失败，仍继续删 source；只剩 `a-child` | 缺失 `z-source:E-z-source`，同样停止该会话全部资源删除 |

正常的临时删除故障因此变成持续残留：即使后端已经恢复，后续每次完整性检查仍会因清理自身制造的缺源而保守停止。这是回收的恢复性问题，不代表当前问答主路径错误，也没有证据说明真实业务资源已经发生该问题。

**建议的最小修法**：

1. 复用已有 `snapshot_deps`，对待删子图采用**消费者先于来源**的依赖顺序，不能只按键名排序。
2. 消费者删除失败，或锁内复核发现它仍存在但不应删除时，必须保留其全部待删来源祖先。简单可行的首版做法：在上述依赖顺序下，第一次失败/保留性跳过即停止该会话剩余的孤儿快照删除，记录原因，等待下次重试。无需新增事务框架或整套清理机制。
3. 如果希望失败后继续删除独立分支，也可只撤销该节点的来源闭包资格；但必须覆盖共享祖先。首版不必为此增加复杂度。已确认不存在的节点与仍存在但需保留的节点应按真实结果区分，不能把未知读取失败当作删除成功。
4. 保留正常 TTL 规划、保留根、会话锁、严格血缘和坏图保守停止规则。

**两种单独修改都不足够**：只加依赖顺序但失败后继续，会重现第二个反例；只在失败时停止但仍按字典序，会重现第一个反例。

**验收**：

- 在实际内存 Store.delete 中注入一次 consumer 失败，覆盖两个名称排序；首轮保留的 consumer 不得因本轮操作失去来源。
- 后端恢复后的下一轮正常回收，无“来源缺失”导致的持续停止。
- 多跳/共享来源中，尚未成功删除的消费者必须保住来源；已删除消费者后再遇到来源删除失败，下一轮仍可恢复。
- 复核发现消费者仍需保留时，同样不能继续删其来源。保留既有跨周期 TTL、有根/移除最后根、坏图停止用例。
- 跑相关维护测试及既有后端回归，不要求借此调用真实 PG、模型或清理真实数据。

**取舍**：这是已有 R4-03 删除保证的执行失败补齐，有现成依赖图和明确修复位置。不存在必须削减批准能力的 SDK 障碍；建议继续修复，不建议放弃。

## 4. 验证边界与下一步

真实 PG 跨进程锁及 DELETE、真实模型题集与预算余量、仓库外消费者、浏览器人工验收仍未验证；本报告不把内存故障注入表述为这些外部集成已经通过，也不把未执行检查当作新增代码缺陷。

接手工具只需核实并处理 **R5-01**，补对应正式回归后写 `doc63_impl_review_request_r6.md`，暂停业务写入，由用户人工通知 Codex 复审。若不同意，应给出实际删除失败控制流的反证。累计轮次保持 R1–R5，不重置，不自动宣布通过。
