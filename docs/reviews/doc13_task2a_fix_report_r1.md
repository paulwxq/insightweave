# doc 13 任务 2A 审核修复报告（R1 轮）

- 日期：2026-11-17；基线 `6b0c396e971bfbdbeb13884b26dbcc4655e95731`（全部改动仍为未提交工作树）。
- 输入：[审核报告 R1](doc13_task2a_review_r1.md)（REQUEST_CHANGES，R1–R5 + M1/M2）及 `/tmp/doc13-2a-init-review/` 四个探针。
- 原则：逐项核实后修复；探针中针对旧缺陷的断言按预期不再满足，以仓库内新回归证明正确行为；原审核报告保持原样。

## R1（P1）MetaWeave DDL 不提交 — 成立，已修复

- 结论：审核复现属实。修复采用建议的 autocommit 方案：`_run_metaweave` 生产连接 `psycopg.connect(..., autocommit=True)`，使 `conn.transaction()` 成为真顶层 BEGIN…COMMIT；未在保存点内塞 commit。另将事务出口异常（含提交失败）统一包装为 `DeployInitError`，保证"提交或执行失败不打印初始化成功"。
- 修改：`deploy/scripts/init_platform.py`（`_run_metaweave`/`ensure_metaweave_schema`）；替身 `FakeConnection` 忠实建模 psycopg3（任何语句含 SELECT 开隐式事务、非 IDLE 事务块降级 SAVEPOINT、close 丢弃未提交），不再依赖朴素 fake。
- 回归：提交持久 `test_created_ddl_commits_and_persists_past_close`、真实 SQL 全量 `test_cli_real_sql_initialize_persists`、执行失败回滚 `test_ddl_failure_rolls_back_and_raises`、提交失败不打印成功 `test_commit_failure_raises_not_created` + `test_cli_commit_failure_is_error_without_success_print`、CLI 必带 autocommit `test_cli_connects_with_autocommit_and_ddl_persists`。
- 未验证：真实 PG 重连/中断恢复（任务 3 实测，doc 13 §6.1）。

## R2（P2）完整性/兼容性仅看表名 — 成立，已修复

- 部署侧新增集中只读契约检查：`build_metaweave_contract()` 从静态 DDL 解析表→列→规范化类型（含 timestamptz/numeric/精度剥离等映射）+ 序列 + 视图 + 索引；`verify_metaweave_objects()` 以 information_schema/pg_indexes 只读核验。`ensure_metaweave_schema` 在 created 事务内与 verified 路径均复用；`check` 报告结构问题。
- 契约固化于 `deploy/contract.json`（`metaweave_schema` + `nl2sql_tables`），漂移由 `TestContractSpecDrift` 钉死（对静态 DDL 重解析、对 `platform_pool.PLATFORM_TABLES + SNAPSHOT_TABLE`），同时满足"契约文件随镜像分发、不依赖宿主完整仓库"。
- NL2SQL：`check_nl2sql_migration_versions()` 只读探测 `checkpoint_migrations`/`store_migrations`（to_regclass），版本 > `len(SDK.MIGRATIONS)-1`（惰性取自所装 SDK，当前 9/3）即拒绝；`ensure_nl2sql_persistence` 先兼容检查再交原幂等入口，已知兼容缺失照旧补齐，不外包大事务。
- 回归：未来版本 v=999 拒绝且 `init_platform` 未被调用（拒绝前零写操作）；同名列类型漂移/缺列/缺序列/缺视图/缺索引均拒绝；合法完整 verified；兼容部分补齐成功；契约漂移两测。
- 边界（对应审核 M-7 建议，留任务 4 README 记录）：核验为"名字+列+规范化类型+四类对象"级，不含约束/默认值/长度精度全等。

## R3（P2）infra 冒烟与 NL2SQL check 占位 — 成立，已补齐

- PG 检查器：两目标库存在 + MetaWeave 结构契约（复用 R2 `verify_metaweave_objects` + contract.json）+ NL2SQL 持久化表齐全；Neo4j：`RETURN 1` + `apoc.version()`（无 APOC 明确失败，不改业务图）；Milvus：目标库连接 + `list_collections`（绝不建库）。生产路径惰性导入真实驱动（psycopg/neo4j/pymilvus 均在两个后端锁定依赖内，doc 13.1 §5.2）。
- `init_platform.py nl2sql --mode check` 真实装配：版本兼容 + db_deepagent 表齐全（复用 platform_pool 常量），退出 0/3/4；不再是"提示改用 smoke"。
- CLI/默认 builder 真实接线测试：`main(["--scope","infra"])` 经 sys.modules 级驱动替换 + 真实 contract.json（通过/Neo4j 宕两例）；`main(["nl2sql","--mode","check"])` 三例。check 全程无 setup/建库调用（`test_ensure_rejects_before_any_setup_on_future_version` 等断言）。
- 2B 落实项：Dockerfile 按 §5.1 复制 `deploy/contract.json` 进两个后端镜像。

## R4（P2）Milvus 默认 check 建库 — 成立，已修复

- `_run_milvus(mode, client_factory=None)`：check 走 `client.connect()`（无 `create_database`），缺库/不可达清晰报错 exit 3；仅 initialize 走 `ensure_milvus_database`（create_database=True）。复用现有 MilvusClient，未重写客户端。
- CLI 回归：`main(["milvus"])` 默认 check（库存在→0；缺库→3 且断言无 create_database 调用）、显式 check、initialize（断言 SDK 边界实参）。

## R5（P2）apps 冒烟误报业务成功 — 成立，已修复

- `_payload_problem()` 按现有端点最小契约：`/api/data-sources` 须有 `items` 列表（DataSourceListResponse）；NL2SQL 须成功 envelope（顶层 `code == 200`）；`/api/v2/health` 的 `data.status == "ok"`；`/api/v2/sessions` 的 `data.items` 列表 + 分页结构（SessionListData 的 `total_all`）。未复制完整业务模型、未加通用框架。
- 测试成功样本全部对齐实际 API 契约（替换旧任意 JSON）。
- 反例回归：三业务路径统一返回 health JSON 失败、空对象失败、code=500 失败、data 形状/分页缺失失败；public 恒不携带令牌、maintenance 受控读取行为保持（既有用例）。

## M1（P3）门闩原子准入 — 已实施（未扩大范围）

- `DeploymentGate.try_admit(during_maintenance=...)`：同一把锁内完成维护判定与计数登记；中间件改用（维护期读取冒烟经 `during_maintenance=True` 放行并计数）。风险表述与审核一致：当前单 worker 事件循环两步间无 await、普通 HTTP enter 无抢占窗口；本修复兑现 §7.5 字面契约并防跨线程 enter（工具/测试场景），未引入并发框架。
- 回归：`TestTryAdmit` 三例 + 既有 lifecycle 21 例全绿。

## M2（P3）Origin 验收 — 记录，不在本轮实施

- apps 冒烟保持无 Origin；smoke.py 文档已显式声明"无 Origin 的 HTTP 冒烟成功不等价于浏览器 CORS 已验证"。两个 Origin（`http://localhost:18080`、`http://127.0.0.1:18080`）的验收落点：doc 13 §9.2"浏览器 Origin 配置"矩阵行（任务 4 的 deploy.sh 携 Origin 步骤）与 Windows 真实浏览器验收（§9.1）。不新增浏览器自动化平台。

## 验证（实际命令与结果）

| 命令 | 结果 |
| --- | --- |
| `env -u ALL_PROXY -u all_proxy PYTHONPATH=…insightweave:…nl2sql nl2sql/.venv/bin/python -m pytest deploy/tests nl2sql/src/tests/unit/api -q` | **284 passed**（审核基线 242，新增 42 例均实际收集执行） |
| `env -u ALL_PROXY -u all_proxy PYTHONPATH=…insightweave:…metaweave metaweave/.venv/bin/python -m pytest metaweave/tests/unit/test_deploy_gate_wiring.py test_api_request_logging.py test_collection_job_api.py test_global_job_list_api.py -q` | **54 passed**（与基线一致） |
| `deploy/tests` 单独 | 90 passed（lifecycle 21 / init_platform 33 / smoke 21 / contract 15） |
| `ruff check deploy/`、`black --check deploy/`、`git diff --check` | 全部通过 |

存量已知问题（与本轮分开，未在本轮处理）：nl2sql `tests/unit/api` 旧 /api/v1 用例 10 失败（受维护清单外）；`src/tests/unit/services/test_config_loader.py` 组合跑 2 例顺序污染；`runtime.py` 既有 TRY203；ruff 0.17.4 默认规则扩张的仓库级 lint 债。M-4（连接错误与冲突的退出码区分）、M-7（README 记录核验边界）按用户裁定留任务 4。

过程说明：修复期间一次用户复跑（78 passed / 4 failed）命中 `test_smoke.py` 重写半途的中间态（旧 JSON 成功样本/旧形状替身），随后已同步完整并全绿。

## 未验证的真实环境边界

真实 PostgreSQL 提交/重连/中断恢复、真实 Milvus/Neo4j(APOC) 运行时行为、镜像内实际运行与依赖可用性——按计划留任务 2B（打包与镜像内预检）和任务 3（真实联调）验收；本轮探针文件保留于 `/tmp/doc13-2a-init-review/` 未改动。
