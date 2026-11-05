# 手工重置平台存储

三个脚本相互独立，只供管理员手工调用。服务启动、采集、发布流程均不调用它们，没有“一键全部重置”入口。

| 脚本 | 执行范围 |
| --- | --- |
| `scripts/manual_reset_platform_pg.py` | 删除整个平台 PG 库，创建同名空库，再执行现有 `db/rebuild_platform_schema.sql` |
| `scripts/manual_reset_platform_neo4j.py` | 保留 Neo4j 数据库，清空其中全部节点和关系；保留约束、索引 |
| `scripts/manual_reset_platform_milvus.py` | 删除目标库中的所有集合，再删除数据库并创建同名空库；不创建集合、索引 |

PG 初始化只重建现有 SQL 定义的平台结构，不恢复原库的其他 schema 或授权。Neo4j 缺失的约束、节点和关系由后续关系发布创建；Milvus 的集合、索引由各自发布步骤创建。

## 配置与预览

脚本固定读取 `metaweave/.env` 和 `metaweave/configs/metadata_config.yaml`，不随运行目录改变。已导出的进程环境变量优先于 `.env`；预览会显示实际目标，请先核对地址和库名。

- PG：使用 `PLATFORM_DB_*`，不会使用业务源库的 `DB_*`。
- Neo4j：使用 `loaders.cql_loader.neo4j`。
- Milvus：使用 `vector_database.providers.milvus`，且 `active` 必须为 `milvus`。
- Neo4j、Milvus 只展开各自配置段中的环境变量，不要求无关的 LLM 密钥。

在 `metaweave/` 目录执行，以下命令**只预览，不建立数据库连接**：

```bash
.venv/bin/python scripts/manual_reset_platform_pg.py
.venv/bin/python scripts/manual_reset_platform_neo4j.py
.venv/bin/python scripts/manual_reset_platform_milvus.py
```

也可显式追加 `--dry-run`，效果相同。预览不查询远端状态或验证连接权限。

## 真正执行

先停止后端、发布任务及其他会访问目标库的服务，再执行需要的脚本。必须同时指定 `--execute` 和 `--confirm-database`；确认值必须与预览显示的库名完全一致。以下命令会真实删除数据，每次仅选择需要重置的存储；日常检查使用上面的预览命令。

PG（当前配置的库名为 `metaweave`）：

```bash
.venv/bin/python scripts/manual_reset_platform_pg.py --execute --confirm-database metaweave
```

Neo4j（当前配置的库名为 `neo4j`）：

```bash
.venv/bin/python scripts/manual_reset_platform_neo4j.py --execute --confirm-database neo4j
```

Milvus（当前配置的库名为 `nl2sql`）：

```bash
.venv/bin/python scripts/manual_reset_platform_milvus.py --execute --confirm-database nl2sql
```

PG 拒绝 `postgres`、`template0`、`template1`；Neo4j 拒绝 `system`；Milvus 拒绝删除系统 `default` 库。PG 删除前先检查当前账号有建库权限，没有权限则保持原库不动；脚本不会自动终止 PG 活动连接，有活动连接时删库失败。

PG 删库、建库和 Milvus 的各项删除不能整体回滚；中途失败可能留下空库或部分清理状态，须按终端阶段提示核对。Neo4j 清理和零剩余核验在同一事务内完成，失败回滚。脚本不清理本地 `output/` 等文件，也不操作其他存储。

## 不删除真实数据的测试

```bash
.venv/bin/python -m pytest tests/unit/test_manual_platform_resets.py -q
```

测试默认拦截真实驱动入口。执行分支使用模拟连接，检查库名确认、执行顺序、作用范围、失败处理及连接释放，不创建或删除真实库。
