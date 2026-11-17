# doc 6.3 真实 PostgreSQL 隔离验收

日期：2026-11-13。执行者：Codex。代码基线：`6a98f4d`。

**结果：7 项通过，无需修改业务代码。** 验证的是应用的锁及清理实现，不是 PostgreSQL 自身的隔离级别。没有启动 Docker、重启现有服务、调用模型或读写业务数据。

## 1. 环境与操作范围

- 复用用户现有 PostgreSQL **18.1**，端口 **5432**。
- 用户明确授权后新建专用数据库：`doc63_test_20261113_c619c845`。
- 给新库设置所有权注释：`insightweave:doc63-isolated-integration:v1`。
- 测试只在该库创建/删除 `db_deepagent` schema；每例独占 schema 初始化锁，使用真实平台初始化、`PostgresStore`、`QaArchive` 和 `MaintenanceService.cleanup()`。
- 连接参数读取现有平台配置，但连接库名和 expected_database 强制指向专用测试库；不修改 `.env`、YAML 或业务连接配置。
- 合成结果文件仅位于 pytest 创建的临时目录。实际执行的文件删除及 SQL DELETE 均针对测试数据。

测试需要同时满足 `--run-db`、显式 `DBDA_PG_TEST_DATABASE`、测试库名前缀、实际库名和专用库注释检查。上述检查在任何 schema DDL 前执行；普通测试默认跳过，不会隐式使用正式库。子进程通过私有 stdin 管道获取连接参数，不在命令行或报告输出密码。

## 2. 实际场景与结果

测试文件：[`test_postgres_maintenance_isolation.py`](../../src/tests/db_deepagent/integration/test_postgres_maintenance_isolation.py)。

| 用例 | 实际验证 | 结果 |
| --- | --- | --- |
| 运行入口同型持锁凭据→清理 | 独立 Python 进程持有真实 `SessionAdvisoryLock` 并校验 lease；主进程调用实际 cleanup，同一会话全部跳过，PG 行、快照和测试文件仍在；其他会话锁可取得。释放后可正常回收。 | 通过 |
| 清理持锁→运行入口同型竞争者 | 独立进程调用实际 cleanup，在资源清理入口设置同步握手；主进程同会话取锁失败。清理完成退出后可取得锁，PG 行和运行快照已回收。 | 通过 |
| 保留根与来源递归保护 | fresh QA 的 snapshot_ref 保护旧 consumer 及 source、结果目录和 PG 结果行；独立过期孤儿被删除。移除最后 QA 根后，原来源链与结果均可回收。 | 通过 |
| dry-run | 实际 PG SELECT 返回删除候选数，但 PG 行、运行快照和文件均不删除。 | 通过 |
| source 名称在前，consumer 删除失败 | 真正 PG `BEFORE DELETE` 触发器对 consumer 第一次删除抛出 SQLSTATE `40001`；source/consumer 快照均保留，不制造缺源。下一轮恢复并全部回收。 | 通过 |
| consumer 名称在前，同样失败 | 交换字典序，仍先尝试 consumer；失败后保留来源，下一轮正常回收。 | 通过 |
| 来源图缺源 | 合成缺源快照导致实际 cleanup 保守停止；PG 结果行、运行快照和测试文件全部保留。 | 通过 |

删除故障由数据库触发器产生，不是模拟 Store.delete 抛错。测试序列在事务回滚后仍记录第一次 nextval，第二轮值变为 2，确认异常与恢复两条实际数据库路径均执行。

## 3. 可复跑命令与输出

在 `nl2sql` 目录，使用本次保留的专用测试库：

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  -u http_proxy -u https_proxy \
  DBDA_PG_TEST_DATABASE=doc63_test_20261113_c619c845 \
  .venv/bin/python -m pytest \
  src/tests/db_deepagent/integration/test_postgres_maintenance_isolation.py \
  --run-db -q --disable-warnings
```

最终测试版本实际输出：**7 passed in 6.38s**，退出 0。

普通默认模式也已执行：省略 `--run-db` 和数据库环境变量，结果 **7 skipped in 0.02s**，证明新增测试不会在默认测试流程中执行数据库操作。

```bash
.venv/bin/black --check \
  src/tests/db_deepagent/integration/test_postgres_maintenance_isolation.py
.venv/bin/ruff check \
  src/tests/db_deepagent/integration/test_postgres_maintenance_isolation.py
```

格式与 lint 检查通过；`git diff --check` 通过。新文件经过独立只读安全审查，没有发现具体的目标库隔离或子进程回收问题。审查者没有连接数据库，运行结果由 Codex 主执行者核实。

## 4. 清理与剩余范围

最终回读确认：测试库只剩默认 public schema，**测试表 0、测试 advisory lock 0、其他测试连接 0**。测试 schema、触发器、序列及临时结果目录均已释放；专用空测试库保留，便于复跑。没有删除现有业务库或平台库的资源。

本次没有实际调用 `DbAgentRunner.run()` 或真实模型。持锁方使用的是生产运行入口共用的 `SessionAdvisoryLock` 和 lease 校验；清理方为真实 `MaintenanceService.cleanup()`。竞争握手位于资源清理入口，没有逐条暂停 DELETE 进行观测。没有覆盖真实 checkpoint 线程删除、网络断连、应用进程强杀、完整模型题集或全面浏览器验收。

因此，可以将原“真实 PG 清理锁/DELETE 完全未验证”的边界收敛为：**上述七项在专用 PG 库中通过，其余故障和完整运行场景仍按实际范围保留**。本次只增加集成测试及验收记录，不重开 6.3 业务实现审核，也不改变 06 设计范围。
