# doc 6.3 收尾与增量审核 R7

日期：2026-11-13。审核者：Codex；实施者：用户授权的 CodeBuddy / hy4 接手会话。反馈由用户人工传递，本轮未使用 Orca。

**结论：PASS。M1 文档收尾及 M2 排序失败拒绝已完成，本次增量没有新增必修代码问题。** R6 PASS 保留为先前代码审核闭环的历史事实，R7 只审之后的文档与局部防御性改进，不重置轮次，不重新打开已关闭的功能修复。

## 1. 基线及范围

- 实施请求：[doc63_impl_review_request_r7.md](doc63_impl_review_request_r7.md)。
- 分支 `feature/multi-source-knowledge-isolation`；HEAD `50067ce2bc7dbd273c42f81f31fce0eab255ebf5`，实际核对一致。
- 批准 6.3 方案 SHA256 `c393d2ea5fe558964467e14f82fe45839d290d7cae808578b2300547ea021593`，实际核对未变。
- 业务代码增量集中在 `maintenance.py` 的局部 `OrphanDeletionOrderError`、排序覆盖不足时抛错及调用方窄捕获；新增 `test_doc63_r7_regressions.py`。
- 文档范围为 6.1 的 02—04 状态、v4 详细设计受影响章节及新收尾记录。

Codex 没有改业务代码或测试；仅补审核记录、校正文档细节及编写隔离 `/tmp` 探针，没有提交、部署、重启、真实资源清理、真实 DB 或付费模型调用。

## 2. M1 文档复核

6.1 的 02—04 已标注“代码已实现、离线审核通过”，保留历史问题背景，06—13 未随之宣告完成。详细设计通过交叉引用和 §22 同步任务、门控、血缘、快照、清理和展示契约；批准方案原文及哈希保留。收尾记录明确实现范围、R6 PASS、未提交/未部署状态和外部验证边界。

本次复核发现以下轻微文档偏差，**Codex 已直接校正**，不需要再修改业务代码或增加一轮修复：

1. 收尾记录及详细设计将 `replacement` 列为独立操作；实际工具仅支持 `add/revise/status/void`，`replacement` 是 `void` 的可选参数。
2. 两份文档把 `build_completion_snapshot()` 归到 `services/task_completion.py`；已改为实际位置 `tools/final_answer.py`，并明确供工具与 Runner 共用。
3. 详细设计顶部曾把 6.2 和 6.3 一并描述为 `50067ce` 之后实现；已明确 6.2 在该提交完成收尾、6.3 在其后工作区实现，避免历史基线混淆。

以实际代码核对上述接口；对 6.1、详细设计、收尾记录、R7 请求及本报告检查 **51 条本地相对链接，0 个失效**，并逐行比较 HEAD 确认 6.1 的 06—13 条目未变。

## 3. M2 代码及故障边界复核

- `_order_orphan_deletions()` 无法覆盖删除集时，抛出专用 `OrphanDeletionOrderError`，不再拼接未满足依赖的剩余节点。
- `_cleanup_session_resources()` 只捕获该确定的错误，报告原因并使用空删除顺序；没有执行部分排序结果，也没有将其他无关错误一并吞掉。
- 正常消费者先于来源、共享来源、多跳、删除失败停止与后续恢复行为继续成立。
- 该保护只针对本阶段孤儿运行快照删除，不声称撤销此前已经执行的其他资源清理。

## 4. 实际验证结果

| 检查 | 本轮结果 |
| --- | --- |
| Codex 后端全量：db_deepagent unit + llm_factory + API v2 | **1375 passed / 4 skipped**，13.38 秒 |
| 新增 M2 正式回归 | **6 项通过**，包含在全量中 |
| 两个改动 Python 文件 black 检查 | 通过，2 files would be left unchanged |
| 新增测试文件 ruff | All checks passed |
| `git diff --check` | 通过 |
| HEAD 与批准方案 SHA256 | 未变 |

后端命令（`nl2sql` 目录）：

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  -u http_proxy -u https_proxy \
  .venv/bin/python -m pytest src/tests/db_deepagent/unit \
  src/tests/unit/services/test_llm_factory.py src/tests/unit/api/v2 \
  -q --disable-warnings
```

格式及 lint（`nl2sql` 目录）：

```bash
.venv/bin/black --check \
  src/modules/db_deepagent/persistence/maintenance.py \
  src/tests/db_deepagent/unit/test_doc63_r7_regressions.py
.venv/bin/ruff check src/tests/db_deepagent/unit/test_doc63_r7_regressions.py
```

### 独立实际排序器边界与既有行为重放

Codex 新建 `/tmp/doc63_r7_sort_boundary_probe.py`，在旧审查者的内存维护夹具中对已装配的 `graph_out` 人工注入二节点依赖环，并保留一个可排序的独立节点。调用**真实排序器**及实际维护控制流，不直接模拟排序函数抛错：

```text
actual_sorter_failure=controlled
failure_phase_deletes=0
coverage=1/3
recovered=[]
```

确认即使已算出独立节点，也不执行部分顺序；维护受控返回并说明原因，恢复正常图后下一轮全部回收。该图是防御性输入，不表述为正常工具链已发生的业务故障。

同时重放 `/tmp/doc63_r6_independent_probe.py`：两种字典序的删除失败恢复、共享来源中第二消费者失败、多跳 DELETE 生效后返回异常、未知 sealed_at、原多周期 TTL 和保留根/移除最后根全部通过。

命令（`nl2sql` 目录，代理变量按上列清除）：

```bash
PYTHONPATH=. .venv/bin/python /tmp/doc63_r7_sort_boundary_probe.py
PYTHONPATH=. .venv/bin/python /tmp/doc63_r6_independent_probe.py
```

两份探针均完成、退出 0；仅操作合成内存快照，临时目录内用钩子禁止文件实删。另由独立只读审查者复跑维护专项 **153 passed**，未发现具体正确性回归。

## 5. 最终状态

M1/M2 均关闭，没有仍需交回实现者的修改项，不建议为了收尾继续扩大改造。代码与文档审核完成，后续可按用户安排进入提交及隔离集成验收准备；本报告没有执行或额外授权提交、部署、重启或真实资源清理。

真实 PG 跨进程锁/DELETE、真实模型题集与预算余量、仓库外消费者、浏览器人工验收仍未执行。本轮未改前端，未重复运行前端检查；最近一次独立前端验证仍为 R5 的 57 项及 vue-tsc 通过。存量 lint 债未表述为全部清零。这些边界不被 PASS 隐去，也不自动视为新实现缺陷。
