# 运行状态汇总修复与发布实库测试

适用情况：运行整体仍为 `pending` / `running`，但其全部步骤已记录为终态。
例如唯一的 `db` 步骤成功，而运行历史仍显示“等待执行”。修复依据是数据库
保存的步骤结果，不重新核验外部存储，不重跑发布，也不回滚既有内容。

## 1. 先预览指定运行

在 `metaweave/` 目录执行。平台库连接凭据沿用 `.env` 或终端环境；
`--database` 显式指定目标平台库，防止误用 `.env` 中的默认库名。

```bash
.venv/bin/python scripts/resolve_orphaned_run.py publish <run_id> \
  --database metaweave_rehearsal_final \
  --finalize-only --reason "核对步骤已结束但整体未收尾的运行"
```

该命令只读。输出包含平台库名、源/工作区/作业/运行 ID、整体及逐步骤状态、
时间、`planned_final_status`、`actionable` 和拒绝原因。按明确的运行 ID
逐条核对，不根据数据源名称或创建时间自动修改一批记录。

仅汇总模式要求至少一个步骤，且每个步骤均为 `success` / `failed` /
`blocked`；空步骤、未知状态和任何 `pending` / `running` 步骤均拒绝。
拒绝预览返回退出码 1；已是终态返回无需处理，不规划任何步骤变更。

## 2. 确认执行者已停止后应用

停止所有连接同一平台库、可能继续执行该运行的 API/后台实例及相关子进程。
`--confirm-executors-stopped` 是操作人的确认，不是程序自动探测结果。
只关闭浏览器或只重启一个实例不能代替这项确认。

核对预览后，使用同一条命令添加：

```text
--apply --confirm-executors-stopped
```

脚本按源 → 工作区 → 作业 → 运行 → 步骤顺序加锁，并重新读取状态；
预览后状态发生变化时，以锁内读取为准。仅汇总模式保留所有步骤的结果、
时间、message 和 manifest，按原规则计算整体终态：

| 步骤结果 | 整体结果 |
| --- | --- |
| 全部成功 | `success` |
| 有成功，也有失败/阻断 | `partial_success` |
| 全部结束且没有成功 | `failed` |

整体结束时间记录本次修复时间；缺失的历史开始时间保持空值，不编造。
原运行 message 保留，追加原状态、处理时间、原因、执行者停止确认及
“外部写入结果未核验”的说明。同一事务提交，重复执行终态记录不再修改。

没有 `--finalize-only` 时保留原离线故障收尾模式：在同样确认无执行者后，
将步骤 `running→failed`、`pending→blocked`，然后汇总整体状态。
该模式与“仅补齐整体结果”用途不同，不要为了让删除通过而把成功步骤改成失败。

恢复服务后刷新运行历史和删除预览。已汇总为终态的运行不再阻断删除；
其他真正未结束的运行仍须按正常准入处理。

## 3. 运行发布内容实库测试

`tests/unit/test_publish_content_live.py` 默认跳过。显式启用并指定一个已按
`db/rebuild_platform_schema.sql` 初始化的隔离平台库：

```bash
METAWEAVE_RUN_LIVE_TESTS=1 METAWEAVE_TEST_DB_NAME=metaweave_test_publish \
  .venv/bin/python -m pytest tests/unit/test_publish_content_live.py -q
```

`METAWEAVE_TEST_DB_NAME` 必须与应用的 `PLATFORM_DB_NAME` 不同；连接主机、
端口和凭据仍来自平台配置。测试不自动创建数据库，也不修改应用配置。
以上两个开关只控制该测试文件，其他实库测试遵循各自的启用约定。

测试通过 `create_publish_run()` + `execute_publish_run()` 完整执行，
检查整体与 `db` 步骤均成功、时间顺序合法、内容来源及最后成功发布视图正确。
文件位于 pytest 临时目录。源创建后立即注册清理，即使工作区初始化失败或
断言失败，也清理本次创建的数据库记录和文件；清理错误直接报告。
进程被强制杀死时，测试 finalizer 仍可能来不及执行。

不连接实库的维护脚本与测试安全回归：

```bash
.venv/bin/python -m pytest tests/unit/test_resolve_orphaned_run.py \
  tests/unit/test_resolve_orphaned_run_real_orm.py \
  tests/unit/test_publish_content_live_safety.py -q
```
