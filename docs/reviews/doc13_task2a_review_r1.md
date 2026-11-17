# doc 13 任务 2A 实施审核 R1

结论：**REQUEST_CHANGES**。门闩与两个 API/执行器的基本接线已完成，现有相关测试通过；但初始化提交、只读模式、结构兼容检查和实际冒烟入口仍有缺口，当前不能将任务 2A 标为全部完成。

- 审核日期：2026-11-17。
- 基线：`6b0c396e971bfbdbeb13884b26dbcc4655e95731`；审核当前未提交工作树及新增文件。
- 依据：[doc 13](../13_项目Docker容器化与本地CI_CD发布设计.md) §5.1、§6、§7.5、§8.1、§9、任务 2A；[doc 13.1](../13.1_镜像与依赖安装明细.md)。
- 本次不修改业务实现，不重写设计；未启动数据库、执行真实数据清理或调用付费模型。

## R1 — P1：MetaWeave 初始化报告 created，但关闭时仍有未提交事务

代码：[init_platform.py:64](../../deploy/scripts/init_platform.py:64)、[init_platform.py:72](../../deploy/scripts/init_platform.py:72)、[init_platform.py:125](../../deploy/scripts/init_platform.py:125)。

生产连接由 `psycopg.connect()` 默认以 `autocommit=False` 创建。第一次 `_existing_public_tables()` 查询已开启外层隐式事务，随后 `conn.transaction()` 成为保存点。保存点成功释放不提交外层事务；`_connection()` 最后直接关闭连接。因此全新库初始化可以打印 `created`，DDL 实际却随未提交连接关闭而回滚。

主审复验使用已安装 psycopg 的真实 `Connection.transaction()` 控制逻辑，仅隔离 PG 传输和无真实连接时的标识符转义，观测到：

```text
Returned: created
implicit BEGIN from first SELECT
SAVEPOINT "_pg3_1"
CREATE TABLE probe
RELEASE "_pg3_1"
CLOSE (INTRANS: True)
```

没有 `COMMIT`。这不是实库持久化验收，但已经直接证明当前事务边界缺失。

最小修改建议：生产连接显式 `autocommit=True`，使现有 `conn.transaction()` 拥有顶层 BEGIN/COMMIT；或调整连接上下文，让首次核验和 DDL 的外层事务由该初始化入口明确提交。不要只增加一个在保存点内无效或非法的 commit。

补回归：首次读表后执行 DDL 的成功路径确有顶层提交；DDL 失败整体回滚。任务 3 再验证实库重连后对象确实保留。

## R2 — P2：完整性／兼容性核验仅看表名，未知结构仍会被接受

代码：[init_platform.py:70](../../deploy/scripts/init_platform.py:70)、[init_platform.py:85](../../deploy/scripts/init_platform.py:85)、[init_platform.py:93](../../deploy/scripts/init_platform.py:93)。相关被复用入口：[platform_pool.py:179](../../nl2sql/src/modules/db_deepagent/persistence/platform_pool.py:179)。

MetaWeave 只查询 `pg_tables` 并比较目标表名。全部同名表存在即返回 `verified`，`check` 也报告完整；没有检查必要列/类型、关键约束及 DDL 中的序列、索引、视图等目标对象。错误结构会被误判为可继续使用。

NL2SQL 封装仅把原入口抛出的异常转换为冲突，不能证明原入口会拒绝不支持的结构。主审复验执行实际安装的 `PostgresSaver.setup()`、`PostgresStore.setup()` 和项目 `init_platform()`，只替换数据库连接/游标边界：当迁移表 `v=999`，且七个目标表名齐全时，初始化仍成功返回；当前两个 SDK 的最高迁移版本分别是 9 和 3。SDK 对更高版本跳过迁移，不会自动抛错。

最小修改建议：

- 在部署初始化封装中提供与本次固定 DDL/SDK 对应的只读契约检查，MetaWeave 的 initialize 前后与 check 复用它；结构冲突明确拒绝，不把“同名表齐全”当作完整兼容。
- NL2SQL 在调用现有 SDK 初始化前拒绝不支持的迁移版本和明确不兼容的已有结构；已知兼容的缺失对象继续交原入口幂等补齐，保持原 SDK 的事务要求。
- 集中维护本次所需的受控契约，不开发通用迁移引擎、任意版本转换或自动重置。

补回归：同名但必要列/类型错误、必要对象缺失、未来 SDK 迁移版本拒绝；同时保留完整合法结构和已知兼容部分初始化的成功路径。

## R3 — P2：infra 冒烟及 NL2SQL check 仍是未完成入口

代码：[smoke.py:93](../../deploy/scripts/smoke.py:93)、[init_platform.py:145](../../deploy/scripts/init_platform.py:145)。

`build_default_infra_checkers()` 的 PG、Neo4j、Milvus 三个检查器全部抛 `NotImplementedError`；`init_platform.py nl2sql --mode check` 仅提示改用 smoke，直接返回 2，而被指向的 infra 入口同样未实现。

主审直接运行当前 CLI：

```text
smoke.py --scope infra：三个“任务 3”占位错误，返回 1
init_platform.py nl2sql --mode check：没有查询，返回 2
```

文档明确要求任务 2A 提供真实初始化和冒烟逻辑，任务 2B 将其打包，任务 3 只做真实环境联调。不能把首次实现这些运行逻辑延迟到任务 3。当前测试只给 `run_infra_smoke()` 注入几个任意函数，没覆盖默认入口，因而没有发现占位代码。

最小修改建议：补真实的只读检查器，复用 R2 的核验逻辑；实现 NL2SQL check 的实际配置装配和只读核验，保证在计划的目标镜像中具备需要的依赖与文件。离线测试隔离网络/驱动边界即可，不要求现在启动数据库。

补回归：直接调用 CLI/default builder，而不只测试注入的函数；合法对象通过、缺库/缺表/缺 APOC/依赖不可达均非零，且 check 不调用创建或 setup。

## R4 — P2：Milvus 默认 check 模式实际会创建数据库

代码：[init_platform.py:171](../../deploy/scripts/init_platform.py:171)，核心调用位于 [init_platform.py:184](../../deploy/scripts/init_platform.py:184)。

`_run_milvus(mode)` 忽略 `mode`，总调用 `ensure_milvus_database()`，后者固定 `client.connect(create_database=True)`。CLI 默认模式正是 check，因此只读核验会建库并返回成功。

主审复验使用真实项目 `MilvusClient` 和 `main(['milvus'])`，只替换 Milvus SDK 边界：

```text
milvus check: 数据库 probe_missing 可用
SDK calls: connect → CREATE DATABASE probe_missing → use
exit: 0
```

最小修改建议：check 使用 `connect(create_database=False)`，缺库应非零；只有 initialize 才允许创建，复用现有客户端即可。

补回归：通过 CLI 运行默认 check 和显式 initialize，断言实际 SDK 边界调用及返回码，覆盖缺库与已存在两种情况。

## R5 — P2：apps 冒烟只检查 JSON 语法，可将健康响应误当业务读取

代码：[smoke.py:33](../../deploy/scripts/smoke.py:33)、[smoke.py:67](../../deploy/scripts/smoke.py:67)。

当前任意 HTTP 200、`application/json`、合法 JSON 都被接受。主审用隔离 HTTP transport 将三个 API 路径统一返回 `{"status":"ok"}`，`run_apps_smoke()` 仍返回空失败列表。也就是说，若 Nginx 把业务列表错误转发到 health 等能返回 JSON 的路径，冒烟无法证明平台读取及 Store 读取实际发生。

最小修改建议：按已有端点分别做最小响应契约检查。例如 MetaWeave 数据源列表应有 `items` 列表；NL2SQL 应有成功 envelope，health 包含正常状态，sessions 的 `data` 包含其实际列表/分页结构。无需复制完整业务模型或增加通用验证框架。

补回归：两个业务路径返回 health JSON、空对象、错误成功码或不符的 data 形状必须失败；真实合法空列表仍应通过。现有测试中的成功响应也应对齐实际 API，而不是以任意 JSON 作为成功样本。

## 非阻塞建议与边界

### M1 — P3：按原设计将门闩检查与登记合并为同锁原子操作

代码：[lifecycle.py:163](../../deploy/runtime/lifecycle.py:163)、[lifecycle.py:172](../../deploy/runtime/lifecycle.py:172)。

当前 `is_maintaining()` 和 `_acquire()` 分别加锁。独立审核用确定性线程交错证明：读取 False 后暂停请求，另一线程 enter 并读到 active=0，再恢复请求，它仍能进入业务。建议以一个小的原子准入方法合并这两步，按 §7.5 收敛契约。

风险需准确限定：当前约定单 Uvicorn worker/事件循环，两步间没有 await，普通 HTTP enter 无法在同一事件循环插入；未将其判为当前默认部署下已证实的高风险并发故障。无需因此引入其他并发框架。

### M2 — P3：浏览器 Origin 验收尚未由当前 apps 冒烟覆盖

`run_apps_smoke()` 不发送 Origin，也不核对允许头；不能用它的成功声称两个浏览器入口的 CORS 已验证。按已有 §9.2 在后续明确的验收入口覆盖 `localhost:18080`、`127.0.0.1:18080` 两种 Origin；也可在同一冒烟脚本中补最小检查，不增加浏览器自动化平台。真实 Windows 浏览器验收仍独立保留。

## 本次验证

| 范围 | 结果 |
| --- | --- |
| NL2SQL 环境：`deploy/tests` + `nl2sql/src/tests/unit/api` | 242 passed |
| MetaWeave 环境：新增门闩接线、API 请求日志、采集作业 API、全局作业列表 API | 54 passed；仅已有 FastAPI startup 弃用等警告 |
| 独立门闩审核：HTTP task 取消后 future 保持计数 | active 2 → 1，future 真正完成后 → 0 |
| 事务保存点、SDK 未来迁移版本、Milvus 默认 check 探针 | 主审已读探针并分别复跑，均确认问题 |
| infra/NL2SQL check 默认入口、错误业务 JSON 冒烟探针 | 主审复现上述行为 |
| `git diff --check` 与 Python 编译检查 | 通过 |

主要测试命令：

```bash
env -u ALL_PROXY -u all_proxy \
  PYTHONPATH=/opt/projects/insightweave:/opt/projects/insightweave/nl2sql \
  nl2sql/.venv/bin/python -m pytest deploy/tests nl2sql/src/tests/unit/api -q

env -u ALL_PROXY -u all_proxy \
  PYTHONPATH=/opt/projects/insightweave:/opt/projects/insightweave/metaweave \
  metaweave/.venv/bin/python -m pytest \
  metaweave/tests/unit/test_deploy_gate_wiring.py \
  metaweave/tests/unit/test_api_request_logging.py \
  metaweave/tests/unit/test_collection_job_api.py \
  metaweave/tests/unit/test_global_job_list_api.py -q
```

临时证据：

- `/tmp/doc13-2a-review-nl-tests.log`、`/tmp/doc13-2a-review-meta-tests.log`。
- `/tmp/doc13-2a-init-review/transaction_probe.py`、`future_version_probe.py`、`milvus_check_probe.py`。
- 主审复跑输出：`/tmp/doc13-2a-review-transaction.log`、`/tmp/doc13-2a-review-future_version.log`、`/tmp/doc13-2a-review-milvus_check.log`。
- `/tmp/doc13-2a-review-smoke-probe.py` 及对应 `.log`。

## 未判为问题或未验证的范围

- 默认部署开关关闭时的惰性导入基本接线、新增 API/执行器回归均通过；未发现需要回退整项改造的证据。
- future 完成回调覆盖运行成功、异常及取消；未要求为任意非法 fake future 增加防御。正常 ASGI finally 包含 BackgroundTasks，控制/健康免计数符合设计。
- PG 建库脚本自包含、使用管理库和 Unix socket 的方向合理，现有相关测试及独立审核的 source/独立 Bash 错误传播探针通过；不因未设置 PGPASSWORD 就认定默认官方容器的本地 socket 模式不可用。
- 任务 1 的证据报告已阅读，未在本轮重新启动 Milvus 复跑其持久化测试，不把实现者报告当成本轮独立实测。
- Dockerfile、Compose、发布脚本及 GitHub Runner 尚属后续任务，不将其未创建本身列作 2A 缺陷。
- 未做真实 PG 持久化/中断恢复、真实服务器 TCP 断连、Docker 替换、Windows 浏览器或付费模型测试；这些继续按任务 3/4/6 验收。上述实现缺陷可通过现有代码和隔离探针先修复，不需要增加部署平台或通用迁移体系。

建议先修复 R1–R5，保留默认开发行为及合法重入路径，再复核任务 2A；M1/M2 的风险边界如上，不将其与首次安装的确定性故障混为同等严重性。
