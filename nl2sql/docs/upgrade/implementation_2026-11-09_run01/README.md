# DeepAgents 0.7.34 升级实施记录（implementation_2026-11-09_run01）

- 实施日期：2026-11-09
- 执行者：GLM-5.3[1m]（实施子代理，依 doc 1 §6 执行）
- 审批文档：[doc 1](../../1_DeepAgents_0.7.34升级评估与实施建议.md)（GLM 两轮审核通过；§2.6/§4/§5.4 为边界）
- 上游输入：当前清理后的模块声明（doc 1 §10/§11 已实施、未提交状态），历史候选 `candidate_2026-11-09/` 仅作版本对照
- 修订记录：2026-11-09 晚依主协调者第 1 轮独立审核（[parent-review-round1.md](parent-review-round1.md)）逐项处置：I1-01 补 PG checkpoint v2 全状态跨版本探针（§D4）、I1-02 统一实施状态分层与 Streamlit 排除（本表/§D10/§F/doc 1 首页）、I1-03 归档专项探针脚本与复跑（§H）、Milvus 跨系列边界如实标注（§B）；处置明细见 §H

## 结论速览

| 层面 | 状态 |
| --- | --- |
| 依赖与代码实施 | **完成**（声明/锁/必改代码/最小测试全部落地，版本 40 项精确核对一致） |
| 离线与受控回归 | **完成**（baseline 1166 与 candidate 1174 失败 ID 完全一致，零新增失败） |
| 真实环境验收 | **可执行子集完成**（后端启动+CORS、doctor env/all 16/16、**PG checkpoint v2 受控跨版本全状态探针（账本/消息/摘要事件，旧→新→回退续跑）**、Fernet 升级前密文解密、真实 embedding、pandas 构造行为、acceptance 题集 §D6；Streamlit 页面按用户 2026-11-09 决定**排除出验收范围**（非阻塞，历史冒烟记录保留 §D10）） |
| 未验证项 | 旧暂停会话恢复、真实长线程在升级后环境的继续运行（仅只读验证）、PyMilvus 官方推荐配对、prune/DeltaChannel 维护——见 §F，均**未视为通过** |
| 可发布 | **不宣布**——遗留既有失败/未验证项见 §F，由用户裁定 |

## A. 基线（§6.1）

| 项 | 值 |
| --- | --- |
| 源码基线 | git HEAD `21ed263` + 未提交改动（§10/§11 依赖清理与三项直接依赖补齐，见 `worktree-diff-vs-head.patch`） |
| 旧锁/声明副本 | `pyproject.baseline.toml`、`uv.lock.baseline.toml`（137 锁条目 / 134 安装包） |
| 旧环境包清单 | `old-env-packages.txt` |
| 工具版本 | uv 0.11.2；模块与重建环境均为 CPython 3.12.13 |
| 服务端版本 | `service-versions.md`：PostgreSQL 18.1 ×3 库（pgvector 扩展均未安装）、Milvus v2.5.2、Neo4j 5.26.19（探测脚本 `probe_services_versions.py`，I1-03 后自 /tmp 原样归档） |
| 独立旧基线环境 | `var/upgrade_old_env/.venv`（deepagents 0.7.25，134 包，pip check 通过），供跨版本对照 |
| 基线回归 | `baseline-tests.{xml,log}`：**1166 收集 / 1152 过 / 10 失败 / 4 跳过**，失败 ID 与历史基线完全一致（旧 API Host 白名单前提失败，非升级引入） |

## B. 候选与版本决策（§6.2）

- 候选目录：`/tmp/nl2sql-upgrade-impl-run01`（从**当前清理后声明**重建，非历史快照；未恢复 redis/pytest-asyncio/pytest-cov/coverage，pytest 仅 dev extra）。
- `uv lock --upgrade` → `uv sync --locked --all-extras --no-install-project` → `uv pip check`：**131 包安装，依赖全兼容**。
- 锁差异：137→134 条目；95 更新 / 2 新增（opentelemetry-sdk、opentelemetry-semantic-conventions）/ 5 移除（greenlet、httptools、setuptools、six、toml），全量见 `candidate_resolved_changes.csv`。
- 40 项关键版本精确核对全部命中（deepagents 0.7.34、langchain 1.4.9、core 1.7.6、langgraph 1.2.20、checkpoint 4.3.1、prebuilt 1.1.1、sdk 0.4.14、checkpoint-postgres 3.2.0、dashscope 2.0.4、starlette 1.10.0、pandas 3.1.1、cryptography 50.0.2、pymilvus 2.6.18、neo4j 6.4.1、pgvector 0.5.1、sqlglot 30.30.0、fastapi 0.162.2、pydantic 2.14.3、pytest 9.2.0 等）。
- 保留/约束决策：uvicorn/requests/pyyaml/rich/langchain-community 等已为最新不变；**pymilvus 取 2.6.18 而非 3.0.2**——实测服务端 2.5.2，2.6 系列与升级前现状（2.6.9）同系列最小变化。**版本边界（主协调者第 1 轮）**：[官方兼容表](https://github.com/milvus-io/pymilvus#compatibility)推荐 Milvus 2.5.x ↔ PyMilvus 2.5.x；服务端 2.5.2 + 客户端 2.6.18 是**跨系列组合、非官方推荐配对**（升级前 2.6.9+2.5.2 已是同类偏离，非本次引入）。该组合的实测覆盖为 doctor all 真实检索计数与验收题集 3×12 真实检索全过；官方配对（2.5.x 客户端或服务端升级）未验证，不称官方匹配或全兼容，不盲改服务器（doc 1 §3.2 已同步）；**原生 ChatTongyi 非生产路径**（生产 chat=qwen3_8_max 走 dashscope_openai 兼容模式），详见 §D7。

## C. 代码与配套修改（§6.3）

| 文件 | 改动 |
| --- | --- |
| `pyproject.toml` / `uv.lock` | 候选声明与新锁纳入（§B） |
| `src/modules/db_deepagent/agent.py` | `build_filesystem_middleware()` 改公开 `permissions=`（0.7.31 起弃用 `_permissions`；与 Agent 级同一组规则），docstring 同步 |
| `src/modules/db_deepagent/cli.py` | `_LOCKED_VERSIONS` sqlglot `30.22.0 → 30.30.0` |
| `AGENTS.md` | SDK 源码引用更新：目标 0.7.34 以模块 `.venv` 安装源码为准；lang-stu 检出标注为历史基线 |
| `docs/v4/5_db_deepagent详细设计.md` | 顶部 SDK 版本注记（旧基线实测范围保留）；§10.2 适配点表 `_permissions` 行更新为公开参数并注明 0.7.34 复核 |
| `src/tests/db_deepagent/unit/test_backend_permissions.py` | 新增公开 permissions 参数行为测试（无弃用告警+同规则） |
| `src/tests/db_deepagent/unit/test_cli_doctor_versions.py` | 新增 doctor 锁定版本与实际安装一致性测试 + `_collect_dependency_report()` 全过断言 |
| `src/tests/db_deepagent/conftest.py` | 新增 `ScriptedChatModel` 受控假模型（供专项测试复用） |
| `src/tests/db_deepagent/unit/test_summary_context.py` | 新增目标 SDK 真实摘要事件专项 ×3（结构/冷读/轮换读取） |
| `src/tests/db_deepagent/unit/test_runner_persistence.py` | 新增标准拒答专项 ×2（完整主图传播 P1 runtime/close_run/优先级 + 普通文本拒答对照） |

- 正式环境删除重建（`venv-rebuild.md`）：`rm -rf .venv` → `uv sync --locked --all-extras --no-install-project --python 3.12.13` → 解释器/`uv lock --check`/`uv pip check`（131 包）全部通过后才运行业务验证。
- 定向验证：assembly/permissions/cli 接线/doctor **38 项通过**；`_collect_dependency_report()` 实际输出全 OK（`doctor-dependency-report-newenv.txt`）。

## D. 离线与受控回归、专项（§6.4/§6.5）

1. **同范围回归**：`candidate-tests.{xml,log}`——**1174 收集 / 1160 过 / 10 失败 / 4 跳过**；失败 ID 与 baseline 完全一致（旧 API Host 白名单前提），零新增失败、零新增跳过。（首轮 1169/1155 见 `candidate-tests-round1-pre-special-topics.*`，新增专项测试后重跑为本记录。）收集数 +8 = 新增测试（1 permissions + 2 doctor + 3 summary + 2 refusal）。
2. **摘要专项**（真实 SDK 0.7.34 SummarizationMiddleware + 受控 Fake 模型）：低阈值实际触发摘要——事件结构合法（`summary_message` HumanMessage、`cutoff_index` 非负 int ≤ 消息数、新字段 `cutoff_message_id` 为 str|None 且读取器不受影响）；同类型图重建冷读一致；Runner `_handoff` 轮换读取真实事件成功。非法结构错误路径由既有测试覆盖。冷读须用同类型装配图（裸 StateGraph 不含 SDK 私有通道，已在测试注释说明）。
3. **拒答专项**：标准 refusal 内容块经完整主图（0.7.34 默认 `ModelRefusalMiddleware`）→ `agent.invoke()` 抛 `ModelRefusalError` 传播至 Runner → **P1 runtime 判 failed（优先于 no_final_answer）**；`close_run()` 在 finally 恰好调用一次；普通文本拒答对照走自然结束 + no_final_answer，不触发协议异常。Web 返回映射由既有 `test_plain_failed_maps_query_failed`（failed→query_failed 封装）组合覆盖。
4. **checkpoint 跨版本**：
   - **v2 全状态探针（主协调者第 1 轮 I1-01 后补强，现行证据）**：`pg_checkpoint_probe_v2.py` / `pg-checkpoint-v2.log`。以**版本对应装配图**（`create_deep_agent` + `DbDeepAgentState` 全部 8 个账本通道 + FilesystemMiddleware/SummarizationMiddleware/PatchToolCallsMiddleware 真实中间件栈；旧 SDK 按版本分支用 `_permissions=`，不用当前 permissions 实现硬跑旧 SDK）在独立测试线程 `dbda:upgrade-verify-v2:1` 上执行五步，全部断言通过：
     1. **旧环境**（0.7.25）生成受控样本：2 轮 invoke 共 6 消息，账本含执行/校验/运行状态/时间证据记录（真实 `tool_command` 协议写入），真实旧 SDK 摘要事件（旧结构，无 `cutoff_message_id`）；
     2. **新环境**（0.7.34）冷读：消息序列/账本字段深度断言 + `read_summary_for_handoff` 读旧事件成功；追加 R3（执行/校验/维度证据/最终答案跨 reducer 写入）后再冷读（新连接+新装配）：合并账本全断言、新事件含 `cutoff_message_id` 且新旧读取器均兼容；
     3. **旧环境回退**：冷读新环境全部写入（含新结构事件）→ **继续运行** R4（反向 reducer 追加，含工具错误通道），最终 3 执行/3 校验/3 状态/1 时间/1 维度/1 答案/1 工具错误、14 消息，摘要可读；
     4. **真实升级前业务线程只读冷读**（`dbda:s_93138a3c178a4e20a453510431b69061:1`，50 消息、7 校验/7 执行/4 最终答案）：消息类型与账本结构断言通过，不 invoke 不写行；
     5. **清理**：探针线程三表行全删、零残留；既有 242 条线程与其行数不变。
     探针裁剪说明：不含 FinalAnswerGate/RunBudget/RequestAudit 与业务工具、用受控假模型（这两类为两环境同源的项目代码，跨版本行为由 §D1 回归覆盖）；探针会话目录 `var/db_deepagent/sessions/upgrade-verify-v2/`（与 `smoke-verify-v2/`）保留为运行痕迹，不入库数据。
   - **v1 短消息探针（历史，保留不删）**：内存探针（`checkpoint-probe.log`）与 PG 短线程探针（`pg-checkpoint-recovery.log`，裸 `StateGraph(DeepAgentState)`、仅消息维度）——I1-01 指出其不足以支撑完整持久化验收，已由 v2 取代为 §6.5 勾选依据。
   - **未覆盖（如实披露，见 §F）**：旧暂停（interrupt）会话恢复（库中无样本）；真实长线程在升级后环境的继续运行（真实线程仅只读验证，续跑验证仅受控样本）；摘要轮换读取由既有真实 SDK 单测组合覆盖（`test_summary_context.py` 结构/冷读/轮换），未在真实旧线程上触发。
5. **SQL/守卫/Fernet**：聚焦运行 146 项全过（`sql-fernet-guard-tests.{xml,log}`：AST/授权文本/时间/维度守卫 + source_registry 真实 Fernet 算法单元测试）。两处 sqlparse 入口：db_deepagent 校验器在回归范围内；旧管线校验器唯一测试文件 `src/tests/unit/tools/test_sql_validation.py` **31/31 在新旧环境同样 setup 失败**（monkeypatch `PGConnectionManager` 属性已不存在——既有陈旧测试，非升级引入），以直接功能探针（脚本 `old_validator_sqlparse_probe.py` + 日志 `old-validator-sqlparse-probe.log`，含真实业务库只读 EXPLAIN；I1-03 后脚本自 /tmp 原样归档，日志为历史原件）验证 sqlparse 0.5.5→0.6.1 行为逐例一致。
6. **验收题集**：`uv run --locked --extra dev python -m src.modules.db_deepagent.cli acceptance run --rounds 3`（真实 Qwen/Milvus/Neo4j/业务库），输出 `var/db_deepagent/acceptance/20261109-185508/`。结果：**业务 3 轮 × 12 例全部通过**（5:56 / 6:15 / 6:00，exit 0），**静态（回归 5 节点 + 协议 8 文件，含 `--run-db` 真库用例）106 项全过**（2.08s）；四份日志零 error/traceback/warning/deprecation 命中。
7. **真实模型/embedding**：DashScope 2.0.4 真实 embedding 调用成功（DbEmbeddingClient → qwen3.7-text-embedding，dim=1024，全部分量非零）；生产 chat 路径 qwen3.8-max 经 ChatOpenAI 兼容模式由验收题集真实调用。原生 ChatTongyi 直接调用在新旧环境同样失败（自定义 `DASHSCOPE_BASE_URI` 下原生 SDK url 报错）——既有环境特征、非升级回归，且无生产路径使用（config.yaml 注明 qwen3-max 已下线，现行 profile 均为兼容模式）。历史首次执行为内联脚本未落盘；**I1-03 后可重放脚本与复跑输出已归档**：`embedding_probe.py` / `embedding-fernet-probes.log`（本次新增执行）。
8. **Fernet 升级前密文**：cryptography 50.0.2 下经生产路径 `resolve_source_registration("db_src_1")` 解密升级前登记密文成功。密文登记时间 2026-11-04（早于升级日 2026-11-09）且 `updated_at` 未变（未重登记），升级前属性由库行时间戳佐证；仅显示长度，未输出明文/密钥/密文。**I1-03 后可重放脚本与复跑输出已归档**：`fernet_probe.py` / `embedding-fernet-probes.log`（本次新增执行）；缺失/错误密钥/损坏密文错误路径由单元测试覆盖且通过。
9. **PgVector 池**：**不适用**——三个 PG 库 pgvector 扩展均未安装（`service-versions.md`），`pg_connection.py` 初始化按"milvus 模式跳过 pgvector 注册"；未验证扩展启用场景（无该部署）。
10. **pandas 3.1.1 / Streamlit 1.67.0**：pandas——app.py 的 DataFrame 构造模式 5 组用例形状/列/isna/空表行为与 2.3.3 完全一致（唯一差异字符串列 dtype 命名 `object→str`，全仓无 `dtype==object` 依赖；探针脚本已归档 `pandas_probe.py`）。**Streamlit 页面/表格验收按用户 2026-11-09 决定排除出本次升级验收范围**（`frontend/app.py` 模块即将删除、功能已迁移到仓库现有前端）：不再补测旧页面、不列为验收阻塞、不顺带删除模块或依赖、不改仓库根前端。升级期间已运行的 AppTest 无头冒烟（全脚本无异常，预期"无法连接后端服务"警告，app 走旧 v1/8008）**保留为历史运行记录**（脚本 `streamlit_apptest.py` 已归档），其性质为历史冒烟通过、非阻塞、不据此宣称页面兼容；真实表格视觉渲染从未验证，随排除决定一并不再作为待办。
11. **后端启动（§6.5.1）**：原样命令启动，PID 2580688（`.venv` 新环境），日志 `var/upgrade_logs/backend-8002.log`；lifespan 正常、`/api/v2/health` 200+统一封装+trace_id、两允许 Origin GET/预检 `access-control-allow-origin` 一致、不允许 Origin GET/预检均 403（origin_not_allowed）、检查后进程存活无新增异常（`backend-startup-checks.log`）。

## E. 遗留既有失败（非升级引入，保留为已知问题）

- 10 个旧 `tests/unit/api/test_api_routes.py` 失败：TestClient 默认 `testserver` Host 被现有 `HostAllowlistGuard` 拒绝（HTTP 400）。与 doc 1 §5.2 基线一致，未放开生产白名单。
- 4 个真实业务库门控跳过（test_business_db_pool，需显式开关）。
- `src/tests/unit/tools/test_sql_validation.py` 31 个 setup 失败（陈旧 monkeypatch，双环境一致）。
- src/tests 下另有 22 个文件存在历史 black 格式偏差、既有 ruff 发现若干——未在本次范围处理。

## F. 未验证/排除项（均未视为通过）

**未验证（升级兼容性剩余风险，发布裁定输入）：**

- 旧暂停（interrupt）会话恢复：现有 lg_persist 242 条线程未发现显式暂停态样本，无受控样本可测，未覆盖（不推定兼容）。
- 真实长线程在升级后环境的继续运行：真实升级前业务线程（50 消息+完整账本）已在新环境**只读**冷读验证；"旧线程继续执行"仅在受控全状态样本上验证（§D4 v2 第 2/3 步），真实业务流量下的续跑未发生。
- PyMilvus 官方推荐配对（2.5.x 客户端配 2.5.2 服务端，或服务端升级 2.6/3.0）：未验证。当前 2.6.18+2.5.2 为跨系列组合，仅以当前调用面实测通过为依据（§B 版本边界）。
- checkpoint-postgres `prune()`/DeltaChannel 维护路径：doc 1 §4.3 界定为不适用/另行评估，未启用。
- 摘要轮换/事件在**真实旧线程**上的触发：真实线程样本无摘要事件；轮换读取由真实 SDK 0.7.34 单测组合覆盖（§D4）。

**按用户 2026-11-09 决定排除（非阻塞，不列入验收待办）：**

- Streamlit 旧页面（`frontend/app.py`）页面/表格/浏览器渲染与 v1 后端联动：模块即将删除、功能已迁仓库现有前端；历史 AppTest 冒烟记录保留（§D10），不再补测。

## G. 环境与产物索引

- **后续运行状态更新：** 主会话后台命令随后退出；当前由另一条启动命令运行的服务为 uvicorn PID **2603370**（uv launcher 2603367），health 200，服务环境无继承代理、模型构造通过。主协调者没有终止或重启该新进程。下方主会话 PID 信息为审核当时快照，最新核查见 [startup-followup.md](startup-followup.md)。
- 旧对照环境：`var/upgrade_old_env/.venv`（保留，供回退对照；.venv 已为新环境）。
- 后台服务：主协调者第 2 轮已将服务转由主会话运行，原样 `uv run --no-sync uvicorn ...`，uvicorn PID **2602001**（uv launcher 2601998，主会话终端 71891）；日志 `var/upgrade_logs/backend-8002-main-session.log`，HTTP/CORS 独立核验见 `parent-review-round2-service.json`。先前委派进程 2580688 已退出，2601192 已由主协调者确认归属后优雅停止，均不再作为当前 PID。验收输出 `var/db_deepagent/acceptance/20261109-185508/`。
- 本目录：git 状态/锁副本/两轮回归 XML+log/探针脚本与日志（v1/v2 checkpoint、sqlparse、embedding/Fernet、pandas/Streamlit、服务版本）/doctor 报告/venv 重建记录/全包差异 CSV/主协调者审核记录。
- 版本控制说明：本目录提交 XML、脚本、CSV/JSON 与 Markdown；`.log` 为本机运行记录，按根 `.gitignore` 不纳入版本控制，仅在当前工作区保留。

## H. 主协调者第 1 轮审核处置记录（2026-11-09 晚）

对 [parent-review-round1.md](parent-review-round1.md) 逐项处置；无拒绝项。

| 审核项 | 处置 |
| --- | --- |
| I1-01（P2）PG 探针覆盖不足 | **已补强**：新增 `pg_checkpoint_probe_v2.py`（版本对应装配图 + 全部账本通道 + 真实中间件栈，旧 SDK 按版本分支适配 `_permissions=`），独立测试线程上完成旧环境生成→新环境冷读/追加/再冷读→旧环境回退读+续跑→真实旧线程只读断言→零残留清理，全部断言通过（`pg-checkpoint-v2.log`）。v1 探针保留为历史证据并降级说明。doc 1 §6.5 勾选依据改为 v2，未覆盖子项如实披露（§F）。 |
| I1-02（P2）状态超出实际覆盖 | **已统一**：doc 1 首页改分层实施状态（版本时点注明 0.7.25 为评估时锁定值）、§5.4 加状态指针、§6.5/§6.6 勾选注记按"已验/未验"拆分；本 README 结论速览拆为四行（依赖与代码实施/受控回归/真实环境可执行子集/未验证项+可发布不宣布）。Streamlit 按用户决定改记"排除（非阻塞）"而非未完成阻塞，历史冒烟记录保留并注明性质。 |
| I1-03（P3）专项证据落盘 | **已归档**：历史 /tmp 脚本原样入库（`old_validator_sqlparse_probe.py`、`probe_services_versions.py`、`pandas_probe.py`、`streamlit_apptest.py`，对应日志/输出先前已在目录）；embedding 与 Fernet 历史原件为内联脚本未落盘，**补建脱敏可重放脚本并标注本次新增复跑**（`embedding_probe.py`/`fernet_probe.py` + `embedding-fernet-probes.log`），不补造历史、不打印密钥/密文/明文、不拷贝 .env；Fernet 升级前属性以登记行 created_at=2026-11-04 佐证。验收题集未重复运行（业务代码无变化，依审核指示）。 |
| Milvus 版本边界 | **已如实标注**：§B 与 doc 1 §2.2/§3.2 写明官方推荐 2.5.x↔2.5.x、当前 2.6.18+2.5.2 为跨系列非官方配对（升级前 2.6.9 已同类偏离）、实测覆盖范围（doctor all+题集检索）与剩余风险、不盲改服务器；不称官方匹配或全兼容。 |

## I. 主协调者第 2 轮独立审核

结论：依赖与必要代码升级通过审核，无新增业务实现问题，无需第 3 轮。I1-01 的 PG 受控样本兼容性证据已补强；I1-02 的结论范围已收窄，主协调者进一步将 doc 1 的聚合持久化验收项保留为未全部完成；I1-03 专项脚本与输出已归档。实际未覆盖项继续保留在 §F，不宣称全路径验收或可发布。详细核查见 [parent-review-round2.md](parent-review-round2.md)，服务运行记录以 §G 和 `parent-review-round2-service.json` 为准。

## J. 旧前端已清理后的重放边界（2026-11-10 追加）

本目录录制时的仓库仍含 Streamlit 旧前端（`nl2sql/frontend/`）与 `web` 可选依赖；两者已于 2026-11-10 按 [nl2sql/docs/3_NL2SQL旧前端清理设计.md](../../3_NL2SQL旧前端清理设计.md) 清理。由此产生的重放边界：

- `streamlit_apptest.py` 依赖当时存在的 `nl2sql/frontend/app.py` 与含 Streamlit 的旧依赖环境；在新环境不可直接运行，需恢复当时源码与依赖环境才能重放。
- `pandas_probe.py` 探测的 pandas 构造行为与 Streamlit 页面无直接关系，但同样以当时环境为前提。
- 本目录的 pyproject/lock 副本、包清单、日志与验收结论保留 2026-11-09 时点语义，不因后续清理改写；当时的 Streamlit 冒烟记录（§D10）是历史证据，不作为当前 Vue 验收依据。
- 不为重放旧脚本而重新安装 Streamlit。
