# LangChain / LangGraph 及关联依赖版本升级清单

> 状态：已保留升级前依赖快照，更新项目依赖与锁文件，最终在 `.venv` 安装全部可选依赖并通过本地回归；此前确认一条真实简单查询运行成功。完整上线验证仍待完成。版本核对与升级日期：2026-10-13。

## 一、版本清单

“升级前版本”来自 [`baseline_2026-10-13/uv.lock`](baseline_2026-10-13/uv.lock)，当时已与旧 `.venv` 的安装版本核对；“已安装目标版本”是当前 `.venv` 中的版本。`pyproject.toml` 中直接声明的依赖标记为“直接”，由依赖解析器引入的标记为“间接”。本次范围包括以下原有 LangChain / LangGraph / PostgreSQL 持久化组件，以及后文新增的模型 SDK、API、配置和 Web 组件。这里的 PostgreSQL 升级指 Python 驱动和 LangGraph 持久化包；PostgreSQL 服务端升级另行决定。

### 1.1 LangChain / LangGraph / PostgreSQL 持久化组件

| 组件 | 升级前版本 | 已安装目标版本 | 依赖来源 | 用途 |
|---|---:|---:|---|---|
| [langchain](https://pypi.org/project/langchain/1.4.5/) | 1.2.10 | 1.4.5 | 直接 | LangChain 主包 |
| [langchain-core](https://pypi.org/project/langchain-core/1.6.10/) | 1.2.17 | 1.6.10 | 间接 | 消息、模型、Runnable 等基础接口 |
| [langchain-openai](https://pypi.org/project/langchain-openai/1.7.1/) | 1.1.10 | 1.7.1 | 直接 | OpenAI 及兼容接口的模型适配 |
| [langchain-community](https://pypi.org/project/langchain-community/0.4.2/) | 0.4.1 | 0.4.2 | 直接 | 社区集成 |
| [langchain-classic](https://pypi.org/project/langchain-classic/1.0.8/) | 1.0.2 | 1.0.8 | 间接 | 旧版 Chain 等接口 |
| [langchain-text-splitters](https://pypi.org/project/langchain-text-splitters/1.1.3/) | 1.1.1 | 1.1.3 | 间接 | 文本切分 |
| [langgraph](https://pypi.org/project/langgraph/1.2.15/) | 1.0.10 | 1.2.15 | 直接 | 图编排与执行 |
| [langgraph-checkpoint](https://pypi.org/project/langgraph-checkpoint/4.3.0/) | 3.0.1 | 4.3.0 | 间接 | Checkpoint 基础接口与序列化 |
| [langgraph-checkpoint-postgres](https://pypi.org/project/langgraph-checkpoint-postgres/3.1.3/) | 3.0.2 | 3.1.3 | 直接 | PostgreSQL 中的 `PostgresSaver` 和 `PostgresStore` |
| [langgraph-prebuilt](https://pypi.org/project/langgraph-prebuilt/1.1.0/) | 1.0.8 | 1.1.0 | 间接 | LangGraph 预构建组件 |
| [langgraph-sdk](https://pypi.org/project/langgraph-sdk/0.4.7/) | 0.3.9 | 0.4.7 | 间接 | LangGraph SDK |
| [psycopg](https://pypi.org/project/psycopg/3.3.6/) | 3.3.3 | 3.3.6 | 直接，通过 `psycopg[binary]` | PostgreSQL 驱动 |
| [psycopg-binary](https://pypi.org/project/psycopg-binary/3.3.6/) | 3.3.3 | 3.3.6 | 间接，通过 `psycopg[binary]` | 与 `psycopg` 同版本的二进制组件 |
| [psycopg-pool](https://pypi.org/project/psycopg-pool/3.3.3/) | 3.3.0 | 3.3.3 | 直接 | PostgreSQL 连接池 |

`langgraph-sdk` 的目标版本采用 2026-10-12 发布的 **0.4.7**，不是此前查到的 0.4.6。

### 1.2 本次追加的组件

用户明确指定将 OpenAI SDK、DashScope、LangSmith、FastAPI、Uvicorn、Pydantic、python-dotenv、Rich、Streamlit 和 Requests 纳入本次升级。重复提到的 FastAPI 只列一次。

| 组件 | 升级前版本 | 已安装目标版本 | 依赖来源 | 用途 |
|---|---:|---:|---|---|
| [openai](https://pypi.org/project/openai/3.29.0/) | 2.26.0 | 3.29.0 | 原为间接；本次在 `pyproject.toml` 中直接声明 | OpenAI Python SDK，含兼容接口调用 |
| [dashscope](https://pypi.org/project/dashscope/1.27.5/) | 1.25.13 | 1.27.5 | 直接 | DashScope embedding 接口 |
| [langsmith](https://pypi.org/project/langsmith/0.14.1/) | 0.7.14 | 0.14.1 | 间接；也在 `monitoring` 可选组中直接声明 | LangChain tracing / LangSmith 集成 |
| [fastapi](https://pypi.org/project/fastapi/0.143.0/) | 0.135.1 | 0.143.0 | 直接 | API 框架 |
| [uvicorn](https://pypi.org/project/uvicorn/0.54.0/) | 0.41.0 | 0.54.0 | 直接 | ASGI 服务 |
| [pydantic](https://pypi.org/project/pydantic/2.14.1/) | 2.12.5 | 2.14.1 | 直接 | 请求、响应及配置模型 |
| [python-dotenv](https://pypi.org/project/python-dotenv/1.2.4/) | 1.2.2 | 1.2.4 | 直接 | `.env` 配置加载 |
| [rich](https://pypi.org/project/rich/15.0.0/) | 14.3.3 | 15.0.0 | 直接 | 终端输出 |
| [streamlit](https://pypi.org/project/streamlit/1.65.0/) | 1.55.0 | 1.65.0 | `web` 可选组 | Web 界面 |
| [requests](https://pypi.org/project/requests/2.34.2/) | 2.32.5 | 2.34.2 | `web` 可选组 | HTTP 请求 |

以上目标版本已通过隔离环境中的完整依赖解析和部分本地回归测试；结果见第四节。特别是 `openai` 从 2.x 升到 3.x、`rich` 从 14.x 升到 15.x，仍需以实际调用和完整回归结果确认。`streamlit`、`requests` 位于可选 `web` 组，已纳入隔离环境的解析和安装。

### 1.3 暂缓决定的模块

前述讨论中未被指定纳入本次升级的 `neo4j`（6.1.0）、`pgvector`（0.4.2）、`pymilvus`（2.6.9）、`sqlparse`（0.5.5）和 `redis`（7.3.0；因安装全部可选依赖，现也安装在 `.venv` 中）暂不设新目标版本，均保持原锁定版本。待 Neo4j、pgvector / PostgreSQL、Milvus 相关服务端升级方案明确后，再决定这些模块的升级范围与版本。Milvus 官方建议服务端 2.6 对应 PyMilvus 2.6.x、服务端 3.0 对应 PyMilvus 3.0.x，因此 `pymilvus` 目标尤其需要依据服务端版本确定。[PyMilvus 兼容性表](https://github.com/milvus-io/pymilvus#compatibility)。

## 二、Python 版本

| 项目 | 当前值 | 本次目标 |
|---|---|---|
| `.python-version` | 3.12 | 保持 3.12 |
| 当前 `.venv` | Python 3.12.13 | 保持 Python 3.12 系列 |
| `pyproject.toml` 的 `requires-python` | `>=3.12` | 暂不修改 |

本次升级**不要求提高 Python 版本**。目标 `langchain`、`langgraph` 和 `langgraph-checkpoint-postgres` 的声明均允许 Python 3.12；新增组件也须在依赖解析时验证对 Python 3.12 的支持。是否另行升级到 Python 3.13，应作为独立工作在项目全部依赖和测试通过后决定。[LangChain 依赖声明](https://raw.githubusercontent.com/langchain-ai/langchain/master/libs/langchain_v1/pyproject.toml)、[LangGraph 依赖声明](https://raw.githubusercontent.com/langchain-ai/langgraph/main/libs/langgraph/pyproject.toml)、[PostgreSQL 持久化包依赖声明](https://raw.githubusercontent.com/langchain-ai/langgraph/main/libs/checkpoint-postgres/pyproject.toml)。

## 三、版本关联与实施核对

1. `langchain 1.4.5` 要求 `langchain-core >=1.6.10,<2` 和 `langgraph >=1.2.11,<1.3`；`langchain-openai 1.7.1` 也要求 `langchain-core >=1.6.10,<2`。[LangChain 声明](https://raw.githubusercontent.com/langchain-ai/langchain/master/libs/langchain_v1/pyproject.toml)、[OpenAI 适配包声明](https://raw.githubusercontent.com/langchain-ai/langchain/master/libs/partners/openai/pyproject.toml)。
2. `langgraph 1.2.15` 与 `langgraph-checkpoint-postgres 3.1.3` 均要求 `langgraph-checkpoint >=4.3.0,<5`。这意味着 PostgreSQL 持久化包升级时，checkpoint 基础包也需一并升级。[LangGraph 声明](https://raw.githubusercontent.com/langchain-ai/langgraph/main/libs/langgraph/pyproject.toml)、[PostgreSQL 持久化包声明](https://raw.githubusercontent.com/langchain-ai/langgraph/main/libs/checkpoint-postgres/pyproject.toml)。
3. `langchain-openai 1.7.1` 要求 `openai >=2.45.0,<4.0.0`；目标 `openai 3.29.0` 落在该声明范围内，但这只能证明包元数据允许安装，不能替代对项目的 OpenAI、OpenRouter、DeepSeek 等兼容接口的实际验证。[OpenAI 适配包声明](https://raw.githubusercontent.com/langchain-ai/langchain/master/libs/partners/openai/pyproject.toml)。
4. 已更新 `pyproject.toml` 中的直接依赖与必要的间接依赖约束，并使用 `uv lock` 重新生成 `uv.lock`；24 个目标版本均在 Python 3.12.13 下解析和安装成功。`web`、`dev`、`monitoring` 可选依赖也已安装验证。
5. 项目的 `SafeCheckpointer` 当前按 `langgraph-checkpoint-postgres==3.0.2` 的接口编写。升级后需验证父图与子图的编译、checkpoint 写入及恢复、`PostgresStore` 对话记录写入及读取，以及已有 PostgreSQL 数据上的迁移。模型 SDK 升级需验证 embedding、模型调用及自定义 `base_url`；API 组件升级需验证路由和请求 / 响应模型；Web 组件升级需验证 Streamlit 页面和 HTTP 请求。

相关代码：`src/services/langgraph_persistence/safe_checkpointer.py`、`src/services/langgraph_persistence/postgres.py`、`src/services/langgraph_persistence/chat_history_writer.py`、`src/services/langgraph_persistence/chat_history_reader.py`、`src/services/llm_factory.py`、`src/services/embedding/embedding_client.py`、`src/api/`。

## 四、隔离环境兼容性评估（2026-10-13）

评估时先在 `/tmp` 中建立候选 `pyproject.toml`、`uv.lock` 和 Python 3.12.13 虚拟环境；当时未修改项目原有依赖文件或 `.venv`。候选环境将本表 24 个目标版本精确约束，并启用了 `dev`、`web`、`monitoring` 可选依赖。`uv lock` 成功解析 128 个包，`uv sync` 成功安装；逐项核对确认 24 个目标版本均已安装。暂缓的 `neo4j 6.1.0`、`pgvector 0.4.2`、`pymilvus 2.6.9`、`sqlparse 0.5.5`、`redis 7.3.0` 保持原锁定版本。此结果证明这些版本能够在 Python 3.12 下共同安装，不能单独证明业务行为正确。后续实际实施结果见第五节。

| 检查范围 | 当前环境 | 候选环境 | 结论 |
|---|---:|---:|---|
| 父图、子图、LLM 工厂、embedding 封装、历史读取和向量适配等核心单元测试 | 377/377 通过 | 377/377 通过 | 未发现新增失败 |
| 父图与 SQL 子图的模拟集成测试 | 32/32 通过 | 32/32 通过 | 未发现新增失败；外部依赖被模拟 |
| FastAPI 路由测试（模型和数据库通过 mock 隔离） | 10/10 通过 | 10/10 通过 | 未发现新增失败 |
| 配置加载器测试与上述模拟集成测试同进程运行 | 13/15 通过 | 13/15 通过 | 两边相同的 2 项失败：测试先加载真实 `.env`，后续用例预期密钥为空 |
| `SafeCheckpointer` 包装内存 checkpointer：两轮执行、写入、读取及恢复 | 通过 | 通过 | 基本同步接口可用；不代表 PostgreSQL 验证 |
| 旧版 `JsonPlusSerializer` 写入含消息对象的样例，候选版读取 | — | 通过 | 样例数据可跨版本反序列化；不代表所有现存 checkpoint |
| Streamlit 版本命令检查 | — | 1.65.0 可运行 `--version` | 尚未验证应用启动和页面交互 |

现有测试库还存在与本次升级无关的阻碍：`src/tests/unit/tools/test_join_planner.py` 导入了不存在的 `build_multi_base_join_plans`，导致收集失败；`src/tests/unit/tools/test_sql_validation.py` 的 fixture patch 了不存在的 `PGConnectionManager`，导致执行前报错。两项在当前环境和候选环境表现一致。直接收集 `tests/langgraph_persistence/` 还会在收集阶段连接 `.env` 指向的 PostgreSQL，因此未继续运行该目录，也未对现有数据库执行 `setup()`。

检查了两版 `PostgresSaver` 和 `PostgresStore` 的项目所用方法签名：`from_conn_string`、`setup`、Saver 的 `get_tuple` / `put` / `put_writes` / `list`，Store 的 `put` / `search` / `get` 均保持兼容。Saver 的 10 条、Store 的 4 条迁移 SQL 静态比对一致；向量迁移的 SQL 内容也一致。**这仍不能代替在数据库副本上验证旧 checkpoint、历史记录和迁移实际执行。**

### 尚未通过的上线门槛

1. 在独立 PostgreSQL 测试库或现有库的副本上，使用旧版本写入样例，再用候选版本执行 `PostgresSaver.setup()` / `PostgresStore.setup()`；验证旧 checkpoint 恢复、新 checkpoint 写入、父子图多轮会话、Store 历史写入与读取，并直接查询数据库确认记录存在。`SafeCheckpointer` 和持久化工厂会吞掉部分异常，单看 API 返回成功不足以判定持久化成功。
2. 使用测试用模型密钥和非生产数据验证 DeepSeek / DashScope 等自定义 `base_url` 的模型调用与 embedding；现有单元测试主要覆盖参数和模拟响应，没有验证 OpenAI SDK 3.x 对真实兼容服务的请求行为。
3. 对候选环境运行完整 API 生命周期和 Streamlit 页面交互，并执行至少一条真实 NL2SQL 查询，检查生成 SQL、执行结果与多轮上下文。当前 FastAPI 路由测试只验证 mock 后的路由和响应模型。
4. 修复或隔离现有测试的收集错误、错误 patch 和 `.env` 测试污染，使完整回归结果可解释。`pytest.ini` 默认只收集 `src/tests`，执行回归时应显式包含 `tests/unit`；数据库脚本应改造成可控的测试后再纳入。

**阶段结论：**本次目标版本可在 Python 3.12 下共同安装；419 项可比较的本地测试在升级前后均通过，未发现新增的单元级或模拟集成级回归。真实 PostgreSQL 持久化、外部模型接口和前端端到端行为尚无通过证据，当前不能据此认定升级可直接上线。

## 五、依赖升级实施记录（2026-10-13）

1. 已确认仓库根目录的 `pyproject.toml`、`uv.lock`、`.python-version` 都受 Git 管理；升级前原样复制到 [`baseline_2026-10-13`](baseline_2026-10-13/README.md)，并校验 SHA-256。额外快照尚未提交到 Git。
2. 已将本次升级范围内的直接依赖改为目标版本精确声明，其中 `openai` SDK 改为直接依赖；通过 `[tool.uv].constraint-dependencies` 固定 7 个间接目标版本和 5 个暂缓模块的现有版本。
3. 已执行 `uv lock`，更新根目录 `uv.lock`。没有使用全量 `uv lock --upgrade`，以免将暂缓模块一起升级；直接声明和约束已经使旧目标版本不再符合解析条件。[uv 锁定与升级说明](https://docs.astral.sh/uv/concepts/projects/sync/)。
4. 实施升级时先执行 `UV_PROJECT_ENVIRONMENT=.venv-wsl uv sync --locked --extra dev --extra web --extra monitoring`，当时保留了旧 `.venv`；随后应用户要求将两套环境替换为新的 `.venv`，见第七节。
5. 已验证 24 个目标版本和 5 个暂缓版本与清单一致；在先前的 `.venv-wsl` 中，`uv pip check` 检查 125 个已安装包，未发现依赖冲突，并通过 377 项核心单元测试、32 项模拟集成测试和 10 项 FastAPI 路由测试。

依赖文件和本地环境已经升级；真实服务的首次运行结果见第六节。旧 PostgreSQL 数据迁移、其他模型服务与复杂查询、Streamlit 页面完整交互仍是上线前检查项。

## 六、启动后的真实请求与日志核查（2026-10-13）

当时用户启动的后端与前端均运行于 `.venv-wsl`，分别监听 `127.0.0.1:8008` 和 `127.0.0.1:8501`。后端 `/api/v1/health` 与前端首页均返回 HTTP 200。

19:19 的一条真实简单问题走完 Router、SQL 生成子图、Milvus 检索、Neo4j 路径规划、SQL 执行和 DashScope 总结：SQL `SELECT COUNT(*) FROM public.orders` 成功返回 1 行，应用日志记录结果为 500；整条查询耗时约 6.37 秒。`PostgresSaver (father)` 和 `PostgresStore` 均完成 `setup()`，对话历史写入成功。随后通过 API 只读检查，能列出对应会话并读回 1 轮问答；对同一线程的 PostgreSQL `checkpoints` 表进行只读计数，查到 13 条记录。

本次启动后，`logs/api.log` 无 ERROR；`logs/nl2sql.log` 无 ERROR，有 4 条 Neo4j WARNING：两个无关候选表找不到 JOIN 路径，系统回退搜索后仍未找到路径。该简单查询最终成功，警告没有阻止执行。前端的页面交互、复杂问题、多轮上下文、其他模型提供方和旧 checkpoint 跨版本恢复尚未验证。

## 七、最终虚拟环境调整（2026-10-13）

用户停止服务后，确认 `8008` 和 `8501` 端口已关闭，再删除 `.venv-wsl` 与旧 `.venv`。在项目根目录执行 `uv sync --locked --all-extras`，按现有锁文件重新创建 `.venv`，安装全部三个可选依赖组：`dev`、`web`、`monitoring`。

新 `.venv` 使用 Python 3.12.13，安装 125 个包；24 个升级目标版本与 5 个暂缓版本符合清单。`uv pip check --python .venv/bin/python` 未发现依赖冲突。新环境再次通过 377 项核心单元测试、32 项模拟集成测试和 10 项 FastAPI 路由测试。后续从项目根目录直接使用 `uv run`，默认即使用这个 `.venv`。
