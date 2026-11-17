# 最新 DeepAgents 基础上的 NL2SQL 依赖升级评估与实施建议

- 评估日期：2026-11-09
- 适用范围：整个 `nl2sql` Python 项目，包括 `db_deepagent`、旧父图/子图、模型与 embedding、数据库适配、API、Web 和开发工具
- DeepAgents 版本：评估与 §2 各表核对时模块锁定 `0.7.25`；本次目标 `0.7.34` 已于 2026-11-09 同日实施，模块 `.venv`/锁文件已切换为 0.7.34（§2 各表"当前锁定"列保留评估时点数字，不回填）
- 升级策略：**保持 DeepAgents 最新，在兼容性允许时尽量升级其余依赖**
- 实施状态（分层，2026-11-09）：**依赖与代码实施完成**（声明/锁/必改代码/最小测试，见 [implementation_2026-11-09_run01](upgrade/implementation_2026-11-09_run01/README.md)）；**实际验证已完成其可执行子集**（同范围回归零新增失败、后端启动/CORS、doctor env/all、PG checkpoint 受控跨版本全状态探针 v2、真实 embedding/Fernet、验收题集 3×12+106——逐项证据与范围见实施 README §D/§F）；**未宣布可发布**：旧暂停（interrupt）会话恢复、真实长线程在升级后环境的继续运行（真实线程仅只读验证）、PyMilvus 官方推荐配对组合等未验证项见实施 README §F，是否发布由用户裁定。§5 与历史候选仍为实施前评估证据，未改写（§5.4 所列待验项的完成状态以 §6 复选框标注与实施 README 为准，未逐条回填）。此前已单独清理项目未使用的依赖，见下方说明
- Streamlit 验收范围调整（用户 2026-11-09 决定）：`frontend/app.py` 旧 Streamlit 页面即将删除、功能已迁移到仓库现有前端；其页面/表格验收**排除出本次升级验收范围，不作为阻塞项**，也不顺带删除模块或依赖、不改动仓库根前端。升级期间已运行的 AppTest 无头冒烟记录保留为历史结果（非阻塞、不据此宣称页面兼容），见实施 README §D10
- 配套文档：[SDK 能力与自研替代分析](2_DeepAgents_0.7.34新特性与项目收益分析.md)

**模块边界与 uv 管理约定（两份文档共同适用）：** 本文及配套 doc 2 指导的依赖调整、代码改造和测试仅限仓库内的 `nl2sql/` 模块。该模块由 uv 独立管理，以 `nl2sql/pyproject.toml` 声明依赖、`nl2sql/uv.lock` 锁定版本、`nl2sql/.venv` 运行。文中“项目根”“项目依赖”“整个依赖图”均指此模块；未带模块前缀的 `src/`、`tests/`、`frontend/` 路径均相对 `nl2sql/`，其中 `frontend/app.py` 是 `nl2sql/frontend/app.py`。不扩展到同仓库的 `metaweave/`、仓库根的 `frontend/` 或它们各自的依赖与环境，也不授权升级共享数据库服务端。

在当前工作区执行模块命令前，先进入模块目录：

```bash
cd /opt/projects/insightweave/nl2sql
```

后续 `uv lock`、`uv sync`、`uv run` 均在此目录执行；测试使用 `uv run --extra dev pytest ...`，工具脚本使用 `uv run python ...`。`uv pip check` 应检查此模块环境，可显式指定 `uv pip check --python .venv/bin/python`。不要通过全局 pip 或其他模块环境维护本模块依赖；更新声明后通过 uv 更新锁文件并同步部署实际需要的 extras。文中 `/tmp` 候选验证是例外：在独立的 NL2SQL 候选目录及其虚拟环境中执行，不能将结果用于其他模块。

**审核后依赖清理：** 用户确认不使用且不计划使用 Redis、pytest-asyncio、pytest-cov，项目已删除这三项声明及 Redis 版本约束，pytest 仅保留在 dev extra（`>=8.4.2`）。当前锁文件同步移除这三包及其不再需要的 coverage，其余包版本未变。下表“当前锁定”、41 项元数据、99/2/5 差异和 §5 回归仍描述清理前的评估基线；候选目录为历史快照，不能照搬并重新引入已删除的依赖。正式升级应从清理后的项目声明重新生成候选和验证记录。

## 1. 结论与范围修正

此前文档采用“只升级 DeepAgents 及其最低必要依赖”的最小联动策略。根据用户澄清，该策略不再作为主方案：LangGraph、checkpoint、PostgreSQL 持久化、SDK、模型适配和其他已声明依赖均重新评估，不因为旧版已能运行就默认保留。

本次已查询 41 项直接依赖、显式约束及重点联动包的最新稳定版本，制作扩大升级候选，完成全新临时环境安装与同范围回归。当前证据支持候选包能共同安装，且所选测试没有出现新增失败；**这不是所有真实服务行为均兼容或已经可上线的结论**。

主要候选包括：DeepAgents `0.7.34`、LangChain `1.4.9`、Core `1.7.6`、LangGraph `1.2.20`、checkpoint `4.3.1`、prebuilt `1.1.1`、SDK `0.4.14`、checkpoint-postgres `3.2.0`。

PyMilvus 按服务端匹配选择：最新版 `3.0.2` 不是无需条件就能替换现有 `2.6.9`；主候选先使用最新 2.6 系列 `2.6.18`，正式实施前确认 Milvus 服务端版本。其他“候选”也须完成下文相关验收后才能成为生产目标。

## 2. 版本清单

“当前锁定”取自本次开始时的项目锁文件；“最新稳定版”来自 PyPI 发布元数据，排除预发布和无可用发布文件的撤回版本。“主候选”是本次实际安装、回归的组合。已是最新的包继续保持，是核对后的结果，不是沿用旧的暂缓清单。

### 2.1 Agent 与编排生态

| 包 | 当前锁定 | PyPI 最新稳定版 | 本次主候选 | 说明 |
| --- | --- | --- | --- | --- |
| [`deepagents`](https://pypi.org/project/deepagents/0.7.34/) | `0.7.25` | `0.7.34` | `0.7.34` | 纳入扩大范围升级 |
| [`langchain`](https://pypi.org/project/langchain/1.4.9/) | `1.4.5` | `1.4.9` | `1.4.9` | 纳入扩大范围升级 |
| [`langchain-core`](https://pypi.org/project/langchain-core/1.7.6/) | `1.6.10` | `1.7.6` | `1.7.6` | 纳入扩大范围升级 |
| [`langgraph`](https://pypi.org/project/langgraph/1.2.20/) | `1.2.15` | `1.2.20` | `1.2.20` | 纳入扩大范围升级 |
| [`langgraph-checkpoint`](https://pypi.org/project/langgraph-checkpoint/4.3.1/) | `4.3.0` | `4.3.1` | `4.3.1` | 纳入扩大范围升级 |
| [`langgraph-prebuilt`](https://pypi.org/project/langgraph-prebuilt/1.1.1/) | `1.1.0` | `1.1.1` | `1.1.1` | 纳入扩大范围升级 |
| [`langgraph-sdk`](https://pypi.org/project/langgraph-sdk/0.4.14/) | `0.4.7` | `0.4.14` | `0.4.14` | 纳入扩大范围升级 |
| [`langgraph-checkpoint-postgres`](https://pypi.org/project/langgraph-checkpoint-postgres/3.2.0/) | `3.1.3` | `3.2.0` | `3.2.0` | 纳入扩大范围升级 |
| [`langchain-openai`](https://pypi.org/project/langchain-openai/1.7.4/) | `1.7.1` | `1.7.4` | `1.7.4` | 纳入扩大范围升级 |
| [`openai`](https://pypi.org/project/openai/3.46.0/) | `3.29.0` | `3.46.0` | `3.46.0` | 纳入扩大范围升级 |
| [`langsmith`](https://pypi.org/project/langsmith/0.14.17/) | `0.14.8` | `0.14.17` | `0.14.17` | 纳入扩大范围升级 |
| [`langchain-anthropic`](https://pypi.org/project/langchain-anthropic/1.7.10/) | `1.7.6` | `1.7.10` | `1.7.10` | 纳入扩大范围升级 |
| [`langchain-google-genai`](https://pypi.org/project/langchain-google-genai/4.4.3/) | `4.4.2` | `4.4.3` | `4.4.3` | 纳入扩大范围升级 |
| [`langchain-protocol`](https://pypi.org/project/langchain-protocol/0.0.21/) | `0.0.19` | `0.0.21` | `0.0.21` | 纳入扩大范围升级 |
| [`langchain-community`](https://pypi.org/project/langchain-community/0.4.2/) | `0.4.2` | `0.4.2` | `0.4.2` | 已为最新稳定版 |
| [`langchain-classic`](https://pypi.org/project/langchain-classic/1.0.8/) | `1.0.8` | `1.0.8` | `1.0.8` | 已为最新稳定版 |
| [`langchain-text-splitters`](https://pypi.org/project/langchain-text-splitters/1.1.3/) | `1.1.3` | `1.1.3` | `1.1.3` | 已为最新稳定版 |

### 2.2 数据访问与驱动

| 包 | 当前锁定 | PyPI 最新稳定版 | 本次主候选 | 说明 |
| --- | --- | --- | --- | --- |
| [`psycopg`](https://pypi.org/project/psycopg/3.3.8/) | `3.3.6` | `3.3.8` | `3.3.8` | 纳入扩大范围升级 |
| [`psycopg-binary`](https://pypi.org/project/psycopg-binary/3.3.8/) | `3.3.6` | `3.3.8` | `3.3.8` | 纳入扩大范围升级 |
| [`psycopg-pool`](https://pypi.org/project/psycopg-pool/3.3.3/) | `3.3.3` | `3.3.3` | `3.3.3` | 已为最新稳定版 |
| [`pymilvus`](https://pypi.org/project/pymilvus/3.0.2/) | `2.6.9` | `3.0.2` | `2.6.18` | 实测服务端 2.5.2：2.6.18 为跨系列组合（官方推荐 2.5.x 配 2.5.x），非官方配对；按实测检索验收保留，边界见 §3.2 |
| [`neo4j`](https://pypi.org/project/neo4j/6.4.1/) | `6.1.0` | `6.4.1` | `6.4.1` | 纳入扩大范围升级 |
| [`pgvector`](https://pypi.org/project/pgvector/0.5.1/) | `0.4.2` | `0.5.1` | `0.5.1` | 纳入扩大范围升级 |

### 2.3 模型、SQL、API、Web 与通用运行依赖

| 包 | 当前锁定 | PyPI 最新稳定版 | 本次主候选 | 说明 |
| --- | --- | --- | --- | --- |
| [`dashscope`](https://pypi.org/project/dashscope/2.0.4/) | `1.27.5` | `2.0.4` | `2.0.4` | 跨主版本候选，真实调用/服务行为仍待验收 |
| [`sqlglot`](https://pypi.org/project/sqlglot/30.30.0/) | `30.22.0` | `30.30.0` | `30.30.0` | 需同步更新 cli.py 的 doctor 版本常量 |
| [`sqlparse`](https://pypi.org/project/sqlparse/0.6.1/) | `0.5.5` | `0.6.1` | `0.6.1` | 纳入扩大范围升级 |
| [`fastapi`](https://pypi.org/project/fastapi/0.162.2/) | `0.143.0` | `0.162.2` | `0.162.2` | 纳入扩大范围升级 |
| [`pydantic`](https://pypi.org/project/pydantic/2.14.3/) | `2.14.1` | `2.14.3` | `2.14.3` | 纳入扩大范围升级 |
| [`uvicorn`](https://pypi.org/project/uvicorn/0.54.0/) | `0.54.0` | `0.54.0` | `0.54.0` | 已为最新稳定版 |
| [`streamlit`](https://pypi.org/project/streamlit/1.67.0/) | `1.65.0` | `1.67.0` | `1.67.0` | 纳入扩大范围升级 |
| [`python-dotenv`](https://pypi.org/project/python-dotenv/1.2.4/) | `1.2.4` | `1.2.4` | `1.2.4` | 已为最新稳定版 |
| [`pyyaml`](https://pypi.org/project/pyyaml/6.0.3/) | `6.0.3` | `6.0.3` | `6.0.3` | 已为最新稳定版 |
| [`rich`](https://pypi.org/project/rich/15.0.0/) | `15.0.0` | `15.0.0` | `15.0.0` | 已为最新稳定版 |
| [`requests`](https://pypi.org/project/requests/2.34.2/) | `2.34.2` | `2.34.2` | `2.34.2` | 已为最新稳定版 |
| [`redis`](https://pypi.org/project/redis/8.1.0/) | `7.3.0` | `8.1.0` | `8.1.0`（历史候选快照） | 项目未使用；仅因遗留 optional 声明进入全 extras 安装记录，不属于本次业务升级或验收对象 |
| [`certifi`](https://pypi.org/project/certifi/2026.10.23/) | `2026.2.25` | `2026.10.23` | `2026.10.23` | 纳入扩大范围升级 |

### 2.4 测试与开发依赖

| 包 | 当前锁定 | PyPI 最新稳定版 | 本次主候选 | 说明 |
| --- | --- | --- | --- | --- |
| [`pytest`](https://pypi.org/project/pytest/9.2.0/) | `9.0.2` | `9.2.0` | `9.2.0` | 纳入扩大范围升级 |
| [`pytest-asyncio`](https://pypi.org/project/pytest-asyncio/1.4.1.post1/) | `1.3.0` | `1.4.1.post1` | `1.4.1.post1`（历史快照） | 审核后已从项目删除，不再纳入升级 |
| [`pytest-cov`](https://pypi.org/project/pytest-cov/7.1.0/) | `7.0.0` | `7.1.0` | `7.1.0`（历史快照） | 审核后已从项目删除，不再纳入升级 |
| [`black`](https://pypi.org/project/black/26.10.1/) | `26.3.0` | `26.10.1` | `26.10.1` | 纳入扩大范围升级 |
| [`ruff`](https://pypi.org/project/ruff/0.17.4/) | `0.15.5` | `0.17.4` | `0.17.4` | 纳入扩大范围升级 |

### 2.5 间接依赖也纳入解析

在隔离副本中更新直接声明和约束后，执行 `uv lock --upgrade`，不是只更新前次 8 个包。相对原锁文件，主候选 **99 项版本更新、2 项新增、5 项移除**；新锁文件共 138 个包条目，实际 Linux/Python 3.12 环境安装并检查 135 个包。

完整差异见 [resolved_changes.csv](upgrade/candidate_2026-11-09/resolved_changes.csv)。历史候选中较大的间接版本变化包括 Pandas `2.3.3 → 3.1.1`、Starlette `0.52.1 → 1.10.0`、cryptography `46.0.5 → 50.0.2`、SQLAlchemy `2.0.48 → 2.1.8`、Anthropic SDK `1.13.0 → 2.6.0` 和 Google GenAI `2.30.1 → 3.0.0`。其中前三包已在审核后按实际直接 import 补为显式依赖，现状及升级决策见 §2.6，不能继续按间接依赖处理。包元数据允许安装和 mock 测试通过，不能替代它们各自的真实接口验收。

QuickJS、浏览器或沙箱等尚未使用的额外集成，不因“尽量更新已有依赖”而自动加入本次安装。未来启用时另行评估。

### 2.6 审核后补齐的三项直接依赖：保留声明，分别决定目标版本

以下三包未列入历史 41 项版本表，但代码直接使用，已按 §11 补齐项目声明。该表补充当前实施决策，不改写历史 41 项元数据。历史候选形成于补齐声明之前，没有这三行，不应解读为有意删除直接依赖。

| 包 | 当前显式声明 | 历史候选实际解析 | 正式升级时的决策与验收 |
| --- | --- | --- | --- |
| starlette | 主依赖 `==0.52.1` | `1.10.0`，当时为间接依赖 | 保留主依赖；将 `1.10.0` 作为待验候选，与目标 FastAPI 一起解析并验证生命周期、响应封装、异常及 Host/认证约束；验收后才改精确版本 |
| pandas | web extra `==2.3.3` | `3.1.1`，当时由 Streamlit 带入 | 保留 web 直接依赖；将 `3.1.1` 作为待验候选，验证实际 DataFrame 构造、数据类型和 Streamlit 表格展示；验收后才改精确版本 |
| cryptography | 主依赖 `==46.0.5` | `50.0.2`，当时为间接依赖 | 保留主依赖；将 `50.0.2` 单列为跨主版本待验候选，核对 Fernet API、Python/平台 wheel 支持，验证升级前登记密文的解密及现有错误处理；验收后才改精确版本 |

这里给出的是待验目标，不是本次已经修改的版本。当前精确锁继续有效，直接对当前声明执行 `uv lock --upgrade` 不会越过这三个 `==`；须显式修改所选约束，再生成候选。若保留旧版本，应同时确认它与目标 FastAPI/Streamlit 等约束能共同解析；不满足时记录阻断并调整组合，不能删除直接声明来掩盖冲突。鉴于三包均被直接使用，本方案不采用“有意改为传递依赖”。

## 3. 已核对的约束与服务端边界

### 3.1 Agent 生态能够共同解析

- DeepAgents `0.7.34` 要求 LangChain `>=1.4.9,<2`、Core `>=1.7.6,<2`，以及相应 Anthropic、Google 和 LangSmith 最低版本。
- LangChain `1.4.9` 要求 LangGraph `>=1.2.12,<1.3`，最新稳定版 `1.2.20` 在范围内。
- LangGraph `1.2.20` 要求 checkpoint `>=4.3.0,<5`、prebuilt `>=1.1.0,<1.2`、SDK `>=0.4.6,<0.5`；本次分别选择 `4.3.1`、`1.1.1`、`0.4.14`。
- checkpoint-postgres `3.2.0` 要求 checkpoint `>=4.3.0,<5`，与主候选一致。
- 主候选以 Python `3.12.13` 真实安装验证，无需仅为本方案升级解释器。

来源：上述版本的 PyPI `Requires-Dist` 发布元数据及候选锁文件。最新版本是在上游约束内选择的，不把 DeepAgents 声明的最低要求当作所有依赖的固定目标。

### 3.2 数据库 SDK 与服务端分别决定

[PyMilvus 官方兼容表](https://github.com/milvus-io/pymilvus#compatibility)按主版本对应推荐：Milvus 2.5.x ↔ PyMilvus 2.5.x、Milvus 2.6 ↔ PyMilvus 2.6.x、Milvus 3.0 ↔ PyMilvus 3.0.x。**当前部署为服务端 2.5.2 + 客户端 2.6.18，属跨系列组合，不是官方推荐配对**；该偏离并非本次升级引入（升级前即为客户 2.6.9 配 2.5.2，同为跨系列），本次在"原客户端同系列内取最新 2.6.18"（模块声明下限 `>=2.6.5`）。

该跨系列组合的实测覆盖：升级后 doctor all 真实检索计数正常、验收题集 3 轮 × 12 例真实检索全部通过。保留理由与剩余风险：当前项目调用面（连接/集合/检索计数）实测可用且与升级前同系列最小变化；官方推荐配对（2.5.x 客户端，或升级服务端到 2.6/3.0）未在本环境验证，不能声称官方匹配或全兼容；后续若出现检索层异常或计划升级服务端，应回到本节按官方配对重新验证，不盲降级或盲升级。全最新版候选 `pymilvus==3.0.2` 的依赖解析同样成功，但本次未安装和验证该组合；不能仅因解析成功就绕过服务端匹配。

`neo4j==6.4.1`、`pgvector==0.5.1` 已进入主候选，不再因旧评估暂缓就排除。但实际 Neo4j 服务端、PostgreSQL/pgvector 扩展版本和调用行为仍须验收。Python `pgvector` 包升级不等于数据库扩展已升级；PostgreSQL 服务端和数据迁移不由 `uv sync` 完成。

### 3.3 跨主版本依赖的真实调用门槛

DashScope `2.0.4` 已在主候选中安装并通过本次所选 mock/单元级检查。其 `TextEmbedding` 与 ChatTongyi 使用路径、自定义模型 ID/参数/超时/重试仍须真实调用。没有真实验收结果前，不应把候选写成已经保证兼容。

## 4. 必须处理的代码与行为影响

### 4.1 DeepAgents 公开权限参数

在 [agent.py](../src/modules/db_deepagent/agent.py) 的 `build_filesystem_middleware()` 将 `_permissions=permissions` 改为 `permissions=permissions`，其余 backend、只读工具集合和阈值保留。旧参数在 `0.7.34` 仍兼容，但从 `0.7.31` 起弃用，计划在 `0.9.0` 移除；不能同时传新旧参数。

目标 SDK 对自定义文件中间件覆盖实例继承 Agent 权限并检查冲突。保留 Agent 级 `permissions`，并让 `FilesystemMiddleware` 传同一组规则；不要只改一边或传不同列表。实施时验证隐藏文件、只读路径、写入拒绝及装配工具集合。

### 4.2 SQLGlot doctor 的精确版本约束

**本次扩大范围新发现的配套修改：** [cli.py](../src/modules/db_deepagent/cli.py) 的 `_LOCKED_VERSIONS` 仍为 `{"sqlglot": "30.22.0"}`。在主候选中直接运行不接触数据库的 `_collect_dependency_report()`，实际返回 `30.30.0（要求 30.22.0）` 的失败结果。

因此，正式升级到 `sqlglot==30.30.0` 时须同步更新该常量及相关版本说明，并保留 AST、授权 SQL 文本一致、时间/维度守卫回归。不能只改 pyproject 和锁文件，也不能以单元测试总体未新增失败代替 doctor 验收。

### 4.3 LangGraph 与持久化

- `1.2.16` 修复恢复时 Command 更新重复应用、已完成子任务被重跑等路径；`1.2.20` 为同任务内每次 interrupt 提供独立 ID，并增加严格恢复模式。已有暂停会话的 ID、重复恢复与父子图恢复应做专项验证。
- `1.2.18` 将一般 DeltaChannel 默认快照频率从 1000 更新为 100。但 DeepAgents `0.7.25` 与 `0.7.34` 均为 messages 显式设置 `snapshot_frequency=50`；当前项目继承该通道，因此不能声称本次消息快照频率自动从 1000 变为 100。
- checkpoint-postgres `3.2.0` 增加 `prune()`。**`keep_latest` 明确拒绝 DeltaChannel 线程，不能替换当前 DeepAgents 活跃线程清理。** `strategy="delete"` 仅可在现有会话锁、保留期、归属和事务约束之下评估低层复用，不能替代整个维护服务。
- 本次静态对比 PostgresSaver 的 10 条迁移和 PostgresStore 的 4 条迁移，列表与原版本一致。这不等于已经验证数据库副本恢复、并发、prune 或生产迁移。

能力细节与使用边界见配套文档 §9。来源：[LangGraph 1.2.16](https://github.com/langchain-ai/langgraph/releases/tag/1.2.16)、[1.2.18](https://github.com/langchain-ai/langgraph/releases/tag/1.2.18)、[1.2.20](https://github.com/langchain-ai/langgraph/releases/tag/1.2.20)、[PostgreSQL 3.2.0](https://github.com/langchain-ai/langgraph/releases/tag/checkpointpostgres%3D%3D3.2.0) 发布记录及已安装 wheel 源码。

### 4.4 标准拒答、摘要与私有接口

DeepAgents `0.7.33` 起默认安装标准拒答中间件。目标版中间件发现标准 refusal 块会抛 `ModelRefusalError`，但应用验收必须覆盖完整主 Agent 图，确认异常没有被其他中间件或图内处理转换为正常返回。若异常逃出 `invoke()`，现有 Runner 将其记为 P1 runtime，`_evaluate()` 优先按 runtime 判失败，不会先归为“未提交最终答案”；已打开的预算条目仍在 finally 中调用 `close_run()`。应构造标准 refusal 块，核对异常是否传播、失败分类、预算关闭、日志和 Web 返回；若 `invoke()` 正常返回，检查是否被转换及无最终答案的实际判定，不能伪称已覆盖异常收尾。普通文本拒答不是同一种协议，不要求 Qwen 必然返回标准块。

摘要事件新增 `cutoff_message_id`：SDK 按命中 ID 优先定位、找不到时回退索引。项目 `read_summary_for_handoff()` 在线程轮换时读取摘要，但同时严格检查事件结构：`summary_message` 必须是 HumanMessage，`cutoff_index` 必须是非 bool 的非负整数且不大于消息数，其他可选字段也有类型检查。事件缺失或为 None 才返回“尚无摘要”；结构非法会抛 `AssemblyVerificationError` 并导致轮换失败，不会静默跳过。当前读取器不消费 `cutoff_message_id`，也未发现常规代码主动裁剪原线程消息列表，因此新增字段本身不是当前必改故障。升级仍需触发一次由目标 SDK 实际生成的摘要事件，验证旧字段与消息类型、持久化后冷读及轮换交接；未来实施历史裁剪时再验证 ID 命中与索引回退。

模型标识、Harness Profile 与摘要私有状态依赖仍应集中在现有 adapter。前次最小方案对源码兼容的判断不能自动覆盖所有本次新增依赖；本轮回归只证明实际覆盖的调用路径。

## 5. 本次实际验证结果

### 5.1 安装与依赖

在 `/tmp` 隔离副本中更新候选约束，两个候选均 `uv lock --upgrade` 成功；主候选执行 `uv sync --locked --all-extras --no-install-project`，创建全新 Python 3.12.13 环境。`uv pip check` 检查实际安装的 135 个包，未发现依赖冲突。项目原 `.venv` 没有被替换。

这里“全最新版候选”只做解析；“主候选”才做完整安装和下列回归。不要混用两者证据。

隔离的是候选依赖声明、锁文件和虚拟环境；两套环境的回归均针对 `/opt/projects/insightweave/nl2sql` 下同一份当时工作区源码执行，而非独立代码副本。结果绑定该次工作区状态，不代表后续源码修订已验证。

### 5.2 同范围回归

| 环境 | 收集 | 通过 | 失败 | 跳过 |
| --- | --- | --- | --- | --- |
| 当前原环境 | 1166 | 1152 | 10 | 4 |
| 扩大升级主候选 | 1166 | 1152 | 10 | 4 |

两环境失败用例 ID 完全相同，均在旧 `tests/unit/api/test_api_routes.py`。这些测试用 TestClient 默认的 `testserver` 访问接口，被现有 `HostAllowlistGuard` 拒绝为 HTTP 400；不是候选版本新增失败。未为了获得全通过结果而放开应用 Host 白名单或修改测试。

所选范围包含 db_deepagent 单元测试、API v2、旧父图、SQL 子图核心用例、LLM 工厂、embedding、历史读取、向量适配、检索工具和旧 API 路由。准确路径与失败 ID 见 [validation_summary.json](upgrade/candidate_2026-11-09/validation_summary.json)。**这不是整个仓库测试库全部通过；4 项数据库相关跳过不算真实数据库验收。**

复跑时须继续显式传入该 JSON 的完整 `test_paths` 列表。`pytest.ini` 默认 `testpaths = src/tests` 不包含 `tests/unit/api/test_api_routes.py` 等模块根 `tests/` 路径；仅运行默认 `uv run --extra dev pytest` 无法复现这里的范围和 10 个已知失败。

测试子进程移除了代理环境变量，避免本机 SOCKS 环境干扰；未打印凭据，也未将该环境问题当作升级缺陷。

### 5.3 内存 checkpoint 跨版本探针

使用无模型、无数据库的 StateGraph 与 DeepAgents 消息 DeltaChannel：原版本写入一轮消息，保存内存 Saver 的原始存储；新版恢复后追加一轮并从新编译图冷读；最后由旧版本读取新版存储。三步均通过（消息数分别为 2、4、4）。

探针只加载自己生成的本地样本，证明该样本的序列化/增量重放可跨版本，不覆盖真实 PostgreSQL、所有私有账本、长线程、正在暂停的旧会话或任意降级路径。

### 5.4 尚未完成的兼容性验收

（本节为实施前评估快照，保留原样；下列各项的实际完成状态以 §6 复选框标注与 [实施 README](upgrade/implementation_2026-11-09_run01/README.md) §D/§F 为准，未在此逐条回填。）

- 在数据库副本上恢复旧 PostgreSQL checkpoint、完整证据账本、长线程和线程轮换；验证升级后写入、冷读与回退。
- 真实 Qwen、DashScope embedding、自定义 base_url、超时及工具调用；跨主版本模型 SDK 的真实请求行为。
- 确认 Milvus、Neo4j、PostgreSQL 与 pgvector 扩展实际版本，并验证相应驱动。
- 将 cryptography `46.0.5 → 50.0.2` 按 §2.6 单独验收：在受控环境配置 `SOURCE_CREDENTIAL_KEY`，使用升级前生成的有效登记密文验证解密，覆盖缺失密钥、错误密钥和损坏密文的现有错误路径，确认不泄露密钥或明文。普通 mock 路径通过不能替代 Fernet 兼容验证。
- 在启用 PgVector 的部署中，验证 `pg_connection.py` 初始化时的 `register_vector` 及后续池连接上的实际向量参数适配/结果解码。当前仅对初始化时借出的一条连接注册，`get_connection()` 不重复注册；应同时持有多条连接并核对不同 PostgreSQL backend PID，覆盖另一条物理连接及扩池新建连接。简单归还再借可能复用原连接，启动日志成功不足以证明池中所有连接都已注册；若复现缺失适配，另行修复连接初始化策略并回归，不能直接归因于升级新增故障。
- 触发目标 SDK 实际摘要，验证 `_summarization_event.cutoff_index`、HumanMessage 类型及 §4.4 的结构约束，覆盖持久化冷读和线程轮换；构造非法事件确认按既有协议明确失败。以标准 refusal 块覆盖完整主 Agent 图到 Runner 的异常传播及预算收尾，记录 `invoke()` 是抛错还是正常返回。
- 对两处 `sqlparse` 校验入口执行 SQL 解析回归。DashScope `2.0.4` 与 Starlette `1.10.0` 属跨主版本候选，优先完成真实模型/embedding 调用与 API 生命周期/响应验收，不能以“0 个新增单测失败”代替这些路径覆盖。
- 验收 Pandas `3.1.1` 下 Streamlit 页面和数据表格行为。
- 新 FastAPI/Starlette 生命周期、所有对外 API、Streamlit 页面、数据表格及认证/Host 约束。
- 修复或隔离旧 API 测试的 Host 前提，并完成固定验收题集。相关真实服务与回退验证通过后，才能确认生产兼容性。

## 6. 面向 Agent 的实施步骤与完成条件

本节是执行计划，所有复选框初始均为未完成，不代表已经实施。执行者应逐步落实并更新状态；无需重新执行 §10–§11 已完成的依赖清理和补齐，但必须检查其未被覆盖。本文的模块/uv 约定、§2.6 的直接依赖决策和 §5.4 的验收边界共同约束所有步骤。

**目标：** 在 `nl2sql/` 内完成 DeepAgents 0.7.34 及兼容依赖升级、必要配套修订和分层验收，保留可恢复的原环境与记录。

**实施方式：** 从当前模块声明重建候选，先验证解析与代码，再在受控服务和数据副本上验收；doc 2 的可选能力接入不随本次依赖升级实施。

**技术与输入：** uv、Python 3.12、当前模块源码/锁文件、本文件 §2–§5、当前部署服务版本与所用 extras。历史候选仅用于比较版本和复核旧证据。

### 6.1 保存当前基线并确认验收环境

- [x] 在 `nl2sql/` 目录检查当前 `pyproject.toml`、`uv.lock`、`.venv` 版本与工作区修改，保留用户已有内容。将当前源码提交号、未提交差异、依赖文件副本、Python/uv 版本和实际启用的 extras 写入本次实施记录；仅记录依赖版本，不导出环境变量或凭据。 （已执行：HEAD 21ed263 + 未提交 §10/§11 改动，记录于 implementation_2026-11-09_run01/git-state.txt）
- [x] 新建本次记录目录，例如 `docs/upgrade/implementation_2026-11-09_run01/`，已有同名目录则使用新后缀。下文以 `NL2SQL_REPORT_DIR` 表示该目录；历史 `candidate_2026-11-09/` 不覆盖、不充当升级补丁。 （目录 docs/upgrade/implementation_2026-11-09_run01/，历史候选未动）
- [x] 确认 Milvus、Neo4j、PostgreSQL、pgvector 扩展实际版本，以及模型、embedding、数据库副本和 Web 验收入口；检查所需密钥可通过既有配置机制提供。缺少真实服务时，先完成不依赖它的步骤，并将对应验收标记“未执行/阻塞”，不能写成通过。 （PG 18.1×3 库且 pgvector 扩展未装、Milvus 2.5.2、Neo4j 5.26.19，见 service-versions.md；密钥经既有 .env 机制可用）
- [x] 在原环境运行下方同范围回归并保存为 baseline；记录测试开始时的源码状态。当前 10 个旧 API Host 失败允许作为对照基线保留，不允许以关闭生产 Host 守卫获得全绿。 （baseline 1166/1152/10/4，失败 ID 与历史基线一致）

**基线产物：** 当前依赖/源码状态、原环境版本清单、服务版本及可用性、baseline XML/日志。切换环境前应保留可复建原环境的锁文件及安装来源；涉及数据副本时保存可恢复备份，禁止默认对共享生产库试运行。

正式模块环境将在 §6.3 删除重建。删除前应在独立基线目录用保存的旧声明/锁文件重建并验证旧环境，记录其 Python 路径，供后续原/新环境对照和跨版本探针使用；不要依赖即将删除的 `nl2sql/.venv` 作为唯一旧环境。

**同范围回归命令（baseline 与升级后 candidate 各运行一次）：** 先在模块目录设置本次新目录与标签；升级后使用同一目录，将 `NL2SQL_TEST_LABEL` 改为 `candidate`。以下命令仅运行现有显式测试列表，退出码按实际 pytest 结果保留。

```bash
export NL2SQL_REPORT_DIR=docs/upgrade/implementation_2026-11-09_run01
export NL2SQL_TEST_LABEL=baseline
uv run --locked --extra dev python - <<'PYTEST_RUN'
import json
import os
from pathlib import Path
import subprocess
import sys

report = Path(os.environ["NL2SQL_REPORT_DIR"])
report.mkdir(parents=True, exist_ok=True)
label = os.environ["NL2SQL_TEST_LABEL"]
if label not in {"baseline", "candidate"}:
    raise ValueError("测试标签必须为 baseline 或 candidate")
paths = json.loads(Path(
    "docs/upgrade/candidate_2026-11-09/validation_summary.json"
).read_text())["test_paths"]
xml = report / f"{label}-tests.xml"
log = report / f"{label}-tests.log"
if xml.exists() or log.exists():
    raise FileExistsError("本标签已有记录，请使用新的实施目录")
with log.open("w") as stream:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *paths, "-q", f"--junitxml={xml}"],
        stdout=stream, stderr=subprocess.STDOUT,
    )
print(f"pytest exit={result.returncode}; XML={xml}; log={log}")
raise SystemExit(result.returncode)
PYTEST_RUN
```

执行者须读取日志并解析 XML，比较收集/通过/失败/跳过数量及完整失败 ID 集合，不只比较最后一行。若代理等本机环境问题干扰测试，定位并记录必要的子进程环境调整，两次采用相同条件。历史 1166 项是参考；新增必要测试后数量可以增加，须说明差异。

### 6.2 落定版本并重建候选

- [x] 从当前模块声明制作隔离的 NL2SQL 候选目录，复制当前 pyproject 和锁文件；候选使用独立 `.venv`，不借用其他模块环境。按 §2 表及 §2.6 写下每个目标版本和保留旧版的理由；这一步先确定发布目标，不在执行中无记录地改追另一批“最新版”。 （候选 /tmp/nl2sql-upgrade-impl-run01，从清理后声明重建）
- [x] 保留 starlette、cryptography 主依赖和 pandas 的 web 直接声明；分别显式选择新精确版本或记录旧版限制。保留 pytest 仅在 dev；不恢复 Redis、pytest-asyncio、pytest-cov，也不添加 QuickJS、异步 Agent Server 等可选集成。 （starlette 1.10.0 / cryptography 50.0.2 主依赖、pandas 3.1.1 web 直接声明，均已升级为待验目标并完成后续验收）
- [x] 在候选目录更新直接版本与 uv 约束，再执行 `uv lock --upgrade`、`uv sync --locked --all-extras --no-install-project`，并用候选 `.venv/bin/python` 显式执行依赖检查。全部 extras 用于本次开发/Web 验证；部署安装另以实际所需组为准。解析失败须定位冲突链，按实际约束调整组合，不通过删除在用直接依赖规避冲突。 （lock/sync/pip check 全过，131 包）
- [x] 对比当前与新锁文件，输出直接依赖决策表、全包增删/版本差异、安装和 `uv pip check --python .venv/bin/python` 日志；核对 Python/平台支持及 §3 的服务端边界。 （candidate_resolved_changes.csv：95 更新/2 新增/5 移除；40 项版本精确核对）

**完成条件：** 新组合可安装且依赖检查通过；三项直接依赖决策明确；原环境仍可用于对照。不宣称真实业务已经兼容。

### 6.3 修改必要代码并纳入当前模块

下表列出本步的改动与验证对应关系，不表示改完一项就立即在旧环境测试。新公开权限参数和 doctor 目标版本依赖新环境；表中运行验证必须等本节 `.venv` 删除重建且版本检查通过后执行，不将旧 SDK/旧依赖中的预期不匹配当作升级回归失败。

| 文件（相对 `nl2sql/`） | 必要改动 | 对应验证 |
| --- | --- | --- |
| `pyproject.toml`、`uv.lock` | 将 §6.2 已确认的声明与新锁纳入，保留当前依赖分组与清理结果 | 锁文件一致性、全包差异、直接 import 和实际安装版本 |
| `src/modules/db_deepagent/agent.py` | `build_filesystem_middleware()` 改用公开 `permissions=`；Agent 与文件中间件继续传同一规则 | `test_assembly.py`、`test_backend_permissions.py`，核对默认 15 工具与隐藏文件/只读/写拒绝 |
| `src/modules/db_deepagent/cli.py` | `_LOCKED_VERSIONS` 的 SQLGlot 值与最终声明版本一致 | `test_cli_wiring.py`、`test_cli_json.py` 及 `_collect_dependency_report()` 的实际输出 |
| `AGENTS.md`、`docs/v4/5_db_deepagent详细设计.md` | 更新已选择 SDK 版本/可用源码位置，标明旧基线实测范围，保留原设计 | 引用指向存在且版本对应的材料，不把历史结果改写为新结果 |

- [x] 准备权限、doctor 中确实缺失的最小行为测试及表中代码改动；已有覆盖可复用。此时可先落盘测试，运行验证留到下述目标环境重建并核对版本后；对拒答、摘要结构、连接池适配按下一步补行为验证，不为简单依赖行增删编写同构测试。 （新增 permissions 公开参数与 doctor 版本一致性最小测试；拒答/摘要专项在 §6.4 落地）
- [x] 确认 §6.1 的基线记录和独立旧环境已保存、§6.2 的新锁已验证后，停止使用本模块环境的开发/测试进程，退出已激活的旧虚拟环境。明确删除 **`/opt/projects/insightweave/nl2sql/.venv`**，再由 uv 按新锁文件创建全新环境；仅执行同步旧环境不满足本步骤。 （基线与独立旧环境 var/upgrade_old_env 就绪后 rm -rf .venv 并全新重建）
- [x] 重建后核对 `sys.executable`、Python 版本、安装包版本，运行 `uv lock --check`、`uv pip check --python .venv/bin/python`，将结果保存至本次实施目录。依赖已切换时，使用 `uv run --locked --extra dev ...` 运行测试与工具，避免执行期间隐式重解锁文件。 （sys.executable/3.12.13/uv lock --check/pip check 131 包全过，见 venv-rebuild.md）
- [x] 在已验证的新环境中运行表中对应测试与 `_collect_dependency_report()`。需要新增行为测试时，先在该目标环境确认测试能暴露待修行为，再完成实现并确认通过；已提前准备的实现须核对测试能识别原问题，避免在旧依赖环境中错误归因。完成本步定向验证后，再执行 §6.4 的同范围回归。 （定向 38 项通过；_collect_dependency_report() 实际输出全 OK，doctor-dependency-report-newenv.txt）
- [x] 不为升级顺带启用 Todo、Memory、技能动态工具、Rubric、子 Agent、QuickJS、审批暂停或新 prune 策略；它们属于 doc 2 的独立能力实验。发现真实兼容缺陷时才扩展代码修复，并记录原因和回归。 （未启用 doc 2 可选能力；无升级引发的额外代码修复）

**正式模块环境删除与重建命令（仅在执行本步骤时运行）：**

```bash
cd /opt/projects/insightweave/nl2sql || exit 1
test "$(pwd -P)" = /opt/projects/insightweave/nl2sql || exit 1
test -f pyproject.toml && test -f uv.lock || exit 1
rm -rf -- .venv
uv sync --locked --all-extras --no-install-project --python 3.12.13
uv run --locked --extra dev python -c 'import sys; print(sys.executable); print(sys.version)'
uv lock --check
uv pip check --python .venv/bin/python
```

上述删除范围仅是 `nl2sql/.venv`，不是文件系统根目录的 `/.venv`，也不涉及其他模块的环境。Python `3.12.13` 沿用本次已验证解释器；若正式环境需要不同补丁版本，应先记录并重新验证。任一命令失败时停止后续验收，处理原因后重建，不把不完整安装作为通过；执行者应逐条检查退出码。该命令块写入文档不代表已经执行删除。

**完成条件：** 正式模块的旧 `.venv` 已删除，uv 从新锁创建的全新 `.venv` 安装与依赖检查通过；必改代码与最终依赖声明一致，目标版本可构建 Agent，当前预算、证据守卫、工具集合及最终答案协议保持有效。

### 6.4 完成离线与受控场景回归

- [x] 将同范围回归标签改为 `candidate`，在升级后的模块环境执行 §6.1 命令，归档新 XML/日志。逐项确认失败 ID；基线相同失败可保留为已知问题，任何新增失败必须定位和处理。若修订旧 API 测试前提，使用受允许的 Host，并对修订后的同一源码范围重新做原/新依赖对照，不放开生产白名单。 （candidate 1174/1160/10/4，失败 ID 与 baseline 完全一致，零新增；首轮 1169 归档为 round1-pre-special-topics）
- [x] 用已安装目标 SDK 和受控 Fake 模型触发实际摘要，验证 §4.4 的合法事件、非法结构错误、冷读与线程轮换。优先扩展 `src/tests/db_deepagent/unit/test_summary_context.py` 和相关 Runner 测试；仅手工拼合法字典不足以证明 SDK 事件兼容。 （真实 SDK 事件结构/cutoff_message_id/冷读/轮换读取 3 项测试；非法结构沿用既有测试）
- [x] 用标准 refusal 块走完整主图至 Runner，确认 `ModelRefusalError` 是否传播、P1 runtime 与无最终答案的判定优先级、`close_run()` 和 Web 返回；同时保留普通文本拒答的对照。可扩展 `test_runner_persistence.py` 及 API v2 测试，不能只向 Runner 人工抛一个异常代替目标图路径。 （标准 refusal 经完整主图传播为 P1 runtime，close_run 恰好一次；普通文本拒答对照为 no_final_answer；Web 返回由既有 failed→query_failed 测试组合覆盖）
- [x] 复用历史内存 checkpoint 探针，在本次旧/新环境和全新样本上重跑旧写→新读写→旧读；保存本次脚本、版本、断言和输出。其通过只覆盖本地内存样本。 （内存探针 + PG 真库探针均三步通过，pg-checkpoint-recovery.log）
- [x] 运行 SQL AST、授权文本、时间/维度守卫及 source_registry 的真实 Fernet 算法单元测试；确认两处 sqlparse 使用路径均在回归内。 （146 项聚焦全过；两处 sqlparse：db_deepagent 校验器在回归内，旧管线校验器无活测试覆盖——其唯一测试文件 31/31 新旧环境同样 setup 失败（既有陈旧），以双环境行为探针验证 0.5.5→0.6.1 逐例一致，old-validator-sqlparse-probe.log）

**完成条件：** 没有未解释的新增失败；每项记录明确测试环境、覆盖范围和结果。尚未具备的集成场景留待 §6.5，不拿单元通过替代它们。

### 6.5 在副本与真实服务上完成兼容性验收

- [x] 在 §6.3 重建模块 `.venv`、§6.4 回归完成后，按 §6.5.1 使用用户提供的命令实际启动后端并完成 HTTP/CORS 冒烟检查；不能仅凭导入成功或 TestClient 测试通过认定服务可启动。 （首次启动 PID 2580688；第 2 轮已由主协调者接管，当前 uvicorn PID 2602001、主会话终端 71891，见实施目录 parent-review-round2-service.json）
- [x] 用已有模块 CLI 在受控环境运行 `uv run --locked --extra dev python -m src.modules.db_deepagent.cli doctor --stage env --json`；该阶段包含环境/服务检查，不是纯离线版本查询。再对已验证归属的实际源运行 `doctor --stage all --source-id <实际短ID> --json`，保存脱敏报告，不能把示例 ID 当真实源。 （doctor env 11/11；doctor all --source-id db_src_1 16/16，真实登记源非示例 ID）
- [ ] 完成 §5.4 的全部持久化验收（**部分完成，保留未勾选**）：旧 PostgreSQL 会话、完整证据账本、长线程、轮换、升级后写入/冷读及回退。对需要的旧暂停会话记录恢复结果；没有此类样本时披露未覆盖，不能推定全部兼容。 （**v2 探针**（`pg_checkpoint_probe_v2.py`/`pg-checkpoint-v2.log`，主协调者第 1 轮 I1-01 后补强）：以版本对应装配图（create_deep_agent + DbDeepAgentState 全部 8 个账本通道 + 真实中间件栈，permissions 参数按 SDK 版本分支）在独立测试线程完成——旧环境(0.7.25)生成含账本+真实摘要事件的受控样本 → 新环境(0.7.34)冷读全量断言→追加→再冷读 → 旧环境回退读全部新写入并**继续运行**一轮（含反向 reducer 追加），消息序列/账本记录/新旧结构摘要事件交叉读取全部断言通过；另对真实升级前业务线程（50 消息、7 校验/7 执行/4 答案）做只读冷读结构断言通过。**未覆盖（如实披露）**：旧暂停（interrupt）会话恢复（库中无样本）；真实长线程在升级后环境的继续运行（真实线程仅只读验证，续跑仅在受控样本上验证）；摘要轮换读取的新事件由既有真实 SDK 单测（结构/冷读/轮换）组合覆盖、未在真实旧线程上触发）
- [x] 完成真实 Qwen、DashScope embedding、自定义模型参数/超时/工具调用；核对 Milvus/Neo4j/pgvector 对应服务。若启用 PgVector，按不同 backend PID 覆盖池内及扩池连接的向量适配；若复现注册缺陷，最小修复后重跑相关测试。 （真实 Qwen 经验收题集、真实 DashScope embedding 直连成功；Milvus/Neo4j 服务经 doctor all 实测；PgVector 不适用——三库扩展均未安装）
- [x] 在受控密钥下验证升级前登记密文的实际解密及 §5.4 错误路径；验收最终选定的 pandas/Streamlit 页面和 FastAPI/Starlette 全部对外接口、生命周期、异常封装及认证/Host 约束。 （升级前登记密文在 50.0.2 下解密成功——密文登记于 2026-11-04（早于升级日）且未重登记，脚本与复跑输出已归档 `fernet_probe.py`/`embedding-fernet-probes.log`，错误路径单元覆盖；pandas 3.1.1 构造行为与 2.3.3 一致（dtype 命名 str，探针脚本已归档）；**Streamlit 页面/表格验收按用户 2026-11-09 决定排除出验收范围**（模块将删除、功能已迁现有前端），升级期间已运行的 AppTest 无头冒烟保留为历史记录、非阻塞、不据此宣称页面兼容；FastAPI/Starlette 实服务冒烟+API v2 回归通过）
- [x] 检查验收题集所需源与服务配置后，运行现有 `uv run --locked --extra dev python -m src.modules.db_deepagent.cli acceptance run --rounds 3`，检查其实际业务/静态子进程结果和 `workspace/acceptance/` 报告。该命令会调用真实模型/数据库；只在本步受控环境执行。它不覆盖所有上列专项，不能代替逐项记录。
 （业务 3 轮 × 12 例全过 + 静态 106 项全过（含 --run-db），exit 0，输出 var/db_deepagent/acceptance/20261109-185508/，日志零异常）
**完成条件：** 所有实际部署路径的必需验收有可复核结果。未启用的可选后端写明“不适用”及依据；缺少服务、样本或配置写明“未验证”，保持上线条件未满足。

#### 6.5.1 升级后实际启动 NL2SQL 后端（必做）

用户已停止原后端，要求升级后重新启动服务。在本步骤执行时确认端口 8002 可用，再从 `nl2sql/` 目录使用以下原始启动命令：

```bash
cd /opt/projects/insightweave/nl2sql || exit 1
NL2SQL_CORS_ORIGINS='http://127.0.0.1:5173,http://localhost:5173' uv run --no-sync uvicorn src.api.main:app --host 127.0.0.1 --port 8002
```

`--no-sync` 要求 §6.3 已完成新环境安装，启动过程不自动同步依赖。用可持续运行的终端/进程会话承载服务并记录 PID、命令、实际解释器、启动日志与 HTTP 检查结果；测试请求从另一终端发出。不添加 reload 或多 worker 来改变本次运行条件，端口被占用时先确认占用进程归属，不直接终止未知进程。

- [x] 观察启动完成和应用 lifespan 日志，确认没有导入失败、版本不兼容、配置异常或端口绑定错误；HTTP 检查后进程仍存活，无新增异常 traceback。 （lifespan 正常、无导入/绑定错误，检查后存活无新增异常）
- [x] 请求真实监听地址的 `/api/v2/health`，验证 HTTP 200、JSON 统一封装及 trace_id；该接口不连接数据库，成功仅证明服务启动和 HTTP 基础链路可用。 （200+统一封装+trace_id）
- [x] 分别对两个允许的 Origin 发起 GET 与 OPTIONS 预检，验证响应的 `Access-Control-Allow-Origin` 与请求 Origin 一致；对未允许 Origin 检查既有 HTTP 403 拒绝行为，保留 Host/Origin 守卫。 （双允许 Origin GET/预检一致；不允许 Origin GET/预检均 403 origin_not_allowed）
- [x] 启动通过后继续执行本节真实服务、业务接口与题集验收，不能把 health 成功当作数据库、模型或运行提交路径已通过。交付时保留用户要求启动的后端，报告地址、PID/会话及日志位置；若后续检查要求重启，只管理本次启动的进程。用户另行要求停止时按其指示处理。
 （health 后已完成 doctor env/all、PG checkpoint（含 v2 受控跨版本全状态探针）、真实模型/embedding、Fernet、pandas 构造行为与验收题集；Streamlit 页面按用户决定排除（非阻塞，历史冒烟记录保留）；后端现由主会话保留运行：uvicorn PID 2602001，日志 var/upgrade_logs/backend-8002-main-session.log（旧进程信息保留在历史启动日志））
可用以下请求检查基础链路；对 `http://localhost:5173` 重复 Origin 检查，未允许 Origin 采用测试值 `http://localhost:5999`：

```bash
curl --noproxy '*' -i http://127.0.0.1:8002/api/v2/health
curl --noproxy '*' -i -H 'Origin: http://127.0.0.1:5173' http://127.0.0.1:8002/api/v2/health
curl --noproxy '*' -i -X OPTIONS -H 'Origin: http://127.0.0.1:5173' -H 'Access-Control-Request-Method: GET' http://127.0.0.1:8002/api/v2/health
```

本节记录的是升级后的执行要求；当前编辑文档时不提前启动服务。

### 6.6 汇总结果并完成交付

- [x] 将本次精确依赖、版本差异、必改代码、验证命令、XML/日志、专项报告和未验证项汇总到本次实施目录。将 doc 1 的实施状态更新为真实进度，不覆盖 §5 与旧候选的历史结果。
 （汇总见 implementation_2026-11-09_run01/README.md 与本节状态更新；§5 与旧候选历史结果未改写）- [x] 检查 `git diff --check` 和本次变更范围；只包含 `nl2sql/`，保留用户其他修改，不夹带凭据、数据库备份或可解密的业务数据。运行与实际变更相关的格式/静态检查，记录输出。
 （git diff --check 通过；变更仅 nl2sql/；black/ruff 见 lint-checks.md——改动文件全过，既有偏差未触碰）- [x] 交付时分别说明“依赖与代码实施完成”“真实环境验收完成”“可发布条件是否满足”，附遗留失败/阻塞。资料不足时交付已完成成果和准确阻塞项，不能标记整个升级已完成。
 （依赖与代码实施完成；真实环境验收完成其可执行子集（acceptance 全过、PG checkpoint v2 受控跨版本探针全过、Streamlit 按用户决定排除）；**未验证项未视为通过**：旧暂停会话、真实长线程续跑、PyMilvus 官方配对等见记录 §F；可发布与否含遗留既有失败与未验证项，交用户裁定）
### 6.7 发布与回退

发布通过既有流程进行，实施本文件不自动包含切换共享生产环境。发布前确认验收与备份完整，保留原源码/锁文件及可恢复数据。回退演练须核对旧版本读取新写入 checkpoint/证据数据的能力；不能只执行旧锁文件同步就宣称数据格式已恢复。若无法证明可逆，使用已验证的备份恢复方案并记录其数据时间边界。

用户要求的“尽量更新”不等于无条件使用每个包的最高版本；仅当存在实际上游约束、服务端匹配或已证实的行为问题时，才保留较低版本，并写明限制及复查条件。

## 7. 可复核的候选与证据

- [候选说明](upgrade/candidate_2026-11-09/README.md)、[候选 pyproject](upgrade/candidate_2026-11-09/pyproject.toml)、[候选锁文件](upgrade/candidate_2026-11-09/uv.lock)。这些位于文档目录，尚未成为项目实际依赖。
- [41 项版本元数据摘要](upgrade/candidate_2026-11-09/latest_versions.json)、[全依赖变更 CSV](upgrade/candidate_2026-11-09/resolved_changes.csv)、[验证摘要](upgrade/candidate_2026-11-09/validation_summary.json)。
- 两份 pytest XML、checkpoint 探针脚本与结果已原样归档至 [evidence](upgrade/candidate_2026-11-09/evidence/README.md)，可独立核对 §5 的统计与断言；对应 `.log` 控制台记录与安装日志保留在工作区，按根 `.gitignore` 不纳入版本控制；该目录另存本轮复核的 pip check 和依赖 doctor 输出，并区分历史原件与新增检查。
- 原始 PyPI 响应、两个临时候选、虚拟环境及发布记录仍在 `/tmp/insightweave-full-upgrade-amqb1psp/`；临时目录可能被清理。归档未包含完整虚拟环境及旧源码快照，正式实施应生成自己的长期验收记录，不能仅凭本次记录认定重新运行会得到相同结果。
- Context7 用于了解 LangGraph 持久化与迁移机制；版本、依赖要求和本项目适用性以固定发布元数据、安装包源码和实际检查为准。
- 本次 GLM-5.3 独立审核及主协调者逐项处置见 [审核记录](upgrade/candidate_2026-11-09/glm_review.md)：共两轮，第二轮确认修订与不修改理由合理，无新增实质问题，无需第三轮；不替代 §5.4 的正式验收。

## 8. 前次最小联动验证的地位

前次仅升级 DeepAgents 及 8 个必要联动包，LangGraph 保留 `1.2.15`，相关测试共 1011 通过、4 跳过。那是旧范围的历史结果，不能当作本次扩大组合已经验收的证明。

此前审核针对该最小范围提出的权限、拒答、摘要边界等结论仍可作为参考，不能替代扩大范围后的独立审核。当前兼容性判断以 §5 的新证据和待验收边界为准。

## 9. 本次扩大范围的 Kimi 独立审核

本次通过 Orca 启动 Kimi Code，同时审核 doc 1 与 doc 2；与此前仅针对 doc 2 的两轮审核分开计数，最多三轮。Kimi 实际查询 Context7 的 DeepAgents / LangGraph 文档，并核对固定版本源码、当前项目调用链、候选元数据和原始测试 XML。Context7 的主分支索引用于发现能力，精确版本结论仍以发布包和发布记录为准。

第一轮：doc 1 无单独必改项；doc 2 的 E1-01 指出 QuickJS PTC 缺少异步入口前提。主协调者核对固定标签 README、eval 工具实现及当前 Runner 后采纳，补入 doc 2 §5.9、§8.2，未因此引入异步执行框架。

第二轮：Kimi 复核修订及两文审核记录，确认 E1-01 已关闭，未发现新增问题，文档审核通过。本次共两轮，无遗留争议，无需第三轮。文档审核不代替 §5.4 的真实服务验收。

## 10. 审核后已实施的未使用依赖清理（2026-11-09）

用户确认 Redis、pytest-asyncio、pytest-cov 当前未使用且未来不计划使用，并要求将 pytest 从主依赖移至 dev extra。本项已实际修改项目文件并同步本地环境，独立于尚未实施的 DeepAgents 扩大版本升级；没有修改业务 Python 代码。

| 清理项 | 核实依据 | 已实施修改 |
| --- | --- | --- |
| Redis | 未发现运行时代码导入；遗留缓存配置默认关闭，用户明确不计划使用 | 从 `pyproject.toml` 的 monitoring extra 删除 `redis>=5.0.0`，从 uv 约束删除 `redis==7.3.0`；保留未使用的旧缓存配置，本次没有改配置加载逻辑 |
| pytest-asyncio | 测试中没有对应导入、异步测试/fixture 或 asyncio 标记 | 从 dev extra 删除 `pytest-asyncio>=0.24.0` |
| pytest-cov | 仅有 README、AGENTS.md 中的覆盖率命令及 pytest.ini 注释，没有自动执行入口；用户明确不计划使用 | 从 dev extra 删除 `pytest-cov>=4.1.0`，同步删除上述覆盖率命令与注释 |
| pytest 重复声明 | 主依赖 `>=8.4.2` 与 dev extra `>=7.4.0` 重复，属于开发测试工具 | 删除主依赖声明，仅保留 dev extra 的 `pytest>=8.4.2`，不降低原有效版本下限 |
| coverage 间接依赖 | 删除 pytest-cov 后不再被依赖 | 随锁文件重算和环境同步移除，无新增直接依赖声明 |

实际修改文件为项目 `pyproject.toml`、`uv.lock`、`pytest.ini`、`README.md`、`AGENTS.md`，以及本次相关评估文档。通过 `uv lock --offline` 更新锁文件，再执行 `uv sync --locked --all-extras --no-install-project --offline` 同步项目 `.venv`。锁文件从 141 个包条目变为 137 个，仅移除 redis、pytest-asyncio、pytest-cov、coverage，其余保留包版本未变；未实施 DeepAgents 或其他包的版本升级。开发环境须启用 dev extra 才会按项目声明安装 pytest。

清理后验证：

- `uv lock --check --offline` 通过；`uv pip check` 检查本地 134 个已安装包，依赖兼容。
- 移除插件后重跑 §5.2 的同范围 1166 项回归：1152 通过、10 失败、4 跳过；10 个失败 ID 与历史基线集合完全一致，均为旧 API 路由的 Host 白名单前提失败，没有新增失败。本项不代表全仓库或真实服务验收通过。
- 核对新旧锁文件，确认仅删除上述 4 个包且没有改变保留包版本；`git diff --check` 通过。

`docs/upgrade/candidate_2026-11-09/` 中的候选 pyproject、锁文件、41 项元数据、变更 CSV 和原始测试记录保留为清理前的历史证据，未改写历史结果。正式升级须从清理后的项目声明重新生成候选，不能从该快照恢复已删除的依赖。本节是用户后续要求的依赖清理记录，不属于此前 GLM 两轮文档审核的已审范围。

## 11. 审核后已补齐的直接依赖与版本脚本修复（2026-11-09）

用户指出以下包被业务代码直接导入，但此前仅通过其他包间接安装。已在项目 `pyproject.toml` 中显式声明，使用当前锁定版本，不在本次修补中升级：

| 包 | 当前直接使用位置 | 已补声明 |
| --- | --- | --- |
| pandas | `frontend/app.py` 的 Streamlit 数据展示 | web extra：`pandas==2.3.3` |
| starlette | `src/api/main.py` 的 Headers、`src/api/v2/envelope.py` 的 HTTPException | 主依赖：`starlette==0.52.1` |
| cryptography | `src/modules/db_deepagent/source_registry.py` 的 Fernet 源库凭据解密 | 主依赖：`cryptography==46.0.5` |

同时将 `tests/check_langgraph_version.py` 中两处 `pkg_resources.get_distribution(...).version` 替换为标准库 `importlib.metadata.version(...)`，包不存在时捕获 `PackageNotFoundError`。该脚本的版本查询不再依赖 setuptools 提供的遗留 API，后续数据库检查逻辑保留。

已重新执行 `uv lock --offline` 和锁定环境同步；锁文件仍为 137 个包条目、本地仍为 134 个已安装包，补齐声明没有新增包或改变包版本。`uv pip check` 通过；离线执行脚本中数据库 URI 构造之前的实际版本报告代码，成功读取 LangGraph `1.2.15` 与 checkpoint-postgres `3.1.3`，没有运行脚本后半段的数据库检查。正式扩大升级时应从当前直接依赖声明出发更新这些版本，历史候选快照不代表已经补齐本节声明。本节亦不属于此前 GLM 两轮审核范围。

本次修改后再次执行 §5.2 的同范围回归（包含 API v2 与真实 Fernet 算法的源登记解密单元测试）：1152 通过、4 跳过、10 个旧 API Host 白名单失败，失败 ID 集合与基线完全一致，没有新增失败；未运行真实数据库、模型或 Streamlit 页面验收。

## 12. 本文实施计划的 GLM-5.3[1m] 专项审核

本次专项审核覆盖 doc 1 全文，重点是后续补充的 §6 实施计划及 §10–§11 已实施记录，与 §7 所引历史两文审核分开计数。共两轮，由主协调者结合当前代码独立核实反馈后修订；没有执行依赖升级、删除 `.venv` 或启动服务。

- 第一轮发现 D1R1-01（P2）：§6.3 原文可能引导执行者在旧环境中改用公开 `permissions=`、更新 doctor 版本常量后立即测试。当前 DeepAgents 0.7.25 只有 `_permissions`，因此旧环境的预期不匹配会被误当作升级失败。主协调者检查实际安装签名后采纳，将顺序明确为准备改动→保存基线并重建模块 `.venv`→核对目标版本→定向测试→同范围回归；本轮无拒绝项。
- 第二轮复核确认 D1R1-01 已关闭，未发现新增实质问题，文档审核通过，无需第三轮。审核通过只说明计划与当前代码、依赖及证据边界一致，不代表 §6 已执行或 §5.4 的真实环境验收通过。

## 13. 升级实施结果的主协调者审核

本次升级实施后独立审核共两轮，与 §12 的计划审核分开计数。第 1 轮独立复跑显式测试范围：1160 通过、10 个与旧基线完全一致的失败、4 跳过；确认新锁、必改代码及实际后端启动，要求补强 PG 业务状态跨版本证据、收窄过度完成表述并归档专项证据。GLM 已逐项处理，无拒绝项；用户明确排除 Streamlit，未追加旧页面验证。

第 2 轮核查补充脚本及输出，独立重新读取真实升级前线程（仅只读），检查依赖兼容与 health/CORS，并以用户原样命令将 8002 服务转由主会话运行。依赖与必要代码升级通过审核，无需第 3 轮；未覆盖的真实长线程续跑、旧暂停态恢复、Milvus 官方推荐配对等保持披露，不宣称全部验收或生产可发布。完整记录见 [主审核第 2 轮](upgrade/implementation_2026-11-09_run01/parent-review-round2.md)。
