# db_deepagent 详细设计

创建日期：2026-10-14  
设计依据：[3_db_deepagent概要设计](3_db_deepagent概要设计.md)（下称“概要设计”）、[4_db_deepagent固定验收题集与通过标准](4_db_deepagent固定验收题集与通过标准.md)（下称“验收题集”）。  
本篇状态：详细设计，尚未实施。现有代码与 SDK 行为已按下文“核对基准”静态核对；模型、数据库、Milvus 与 Neo4j 的实际调用尚未验证。

> 本篇把概要设计的决策落实为可直接编码的模块、接口、数据结构、调用时序和测试清单。概要设计已确定的方向不再讨论；凡与概要设计不一致之处，以本篇“§20 对概要设计的细化与补充”列出的条目为准，其余一律服从概要设计。

## 0. 核对基准

| 对象 | 基准 | 说明 |
| --- | --- | --- |
| 现有项目代码 | 当前仓库 `src/` | 除概要设计与验收题集外，其他历史文档与代码存在脱节，**一律以代码为准** |
| Deep Agents | `/opt/projects/lang-stu/deepagents-0.7.25`（`libs/deepagents/deepagents/`） | 与选定的 `deepagents==0.7.25` 一致 |
| LangChain | `/opt/projects/lang-stu/langchain-1.4.5`（`libs/langchain_v1/`） | 与根 `.venv` 一致 |
| LangGraph | `/opt/projects/lang-stu/langgraph-1.2.15`（`libs/langgraph/`、`libs/prebuilt/`、`libs/checkpoint-postgres/`） | 与根 `.venv` 一致 |

本篇依赖的 SDK 关键事实（均已在上述源码中逐项确认）：

| 编号 | 事实 | 源码位置 |
| --- | --- | --- |
| K1 | `create_deep_agent` 主栈依次为 `FilesystemMiddleware`、（有内联子 Agent 时）`SubAgentMiddleware`、Deep Agents 版 `SummarizationMiddleware`、`PatchToolCallsMiddleware`；**0.7.25 默认栈不含 `TodoListMiddleware`** | `deepagents/graph.py` `create_deep_agent` |
| K2 | 传入 `middleware` 中与默认实例同名者原位替换；新名称的 middleware 插在核心栈之后、尾部（Skills、Prompt Caching、Memory、`UnsupportedContentMiddleware`）之前 | `graph.py` `_apply_custom_middleware` |
| K3 | 只有 HarnessProfile 的 `general_purpose_subagent.enabled is False` 且未传同步子 Agent 时，才不创建 `SubAgentMiddleware`，模型不会看到 `task` | `graph.py` 第 822 行附近 |
| K4 | 预初始化模型的 profile 查找顺序：`provider:identifier` 精确匹配 → 含冒号的 identifier → provider 回退；provider 取自 `_get_ls_params()["ls_provider"]`，identifier 取自 `model_name` 或 `model` | `profiles/harness/harness_profiles.py` `_harness_profile_for_model`、`_models.py` |
| K5 | 现有 `llm_factory` 创建的实例：`ChatTongyi` 报告 `ls_provider="tongyi"`、`model_name=<模型名>`；`ChatOpenAI`（含 DashScope 兼容模式）报告 `ls_provider="openai"`。因此 `qwen3_max` profile 的匹配键为 `tongyi:qwen3-max`，`qwen3_5_plus` 为 `openai:qwen3.5-plus` | 在根 `.venv` 中构造实例实测 |
| K6 | `FilesystemMiddleware(tools=[...])` 只构造白名单内的工具，名单外工具不进入可分派集合；列表必须包含 `read_file` | `middleware/filesystem.py` `FilesystemMiddleware.__init__` |
| K7 | 权限规则按声明顺序首条命中，无命中默认允许；匹配标志为 `wcglob.BRACE \| wcglob.GLOBSTAR`，**不含 `DOTMATCH`**。实测 `/**` 能匹配 `/`、`/skills`、`/x/y`，但匹配不到 `/.env`、`/artifacts/.hidden`；需要额外的 `/**/.*` 与 `/**/.*/**` | `filesystem.py` `_FS_WCMATCH_FLAGS`、`_check_fs_permission`；wcmatch 实测 |
| K8 | `FilesystemBackend` 在虚拟模式下解析符号链接后若越出根目录则抛错，权限层将其报告为 `cannot resolve`；同时对请求路径与解析后路径都做 deny 检查 | `backends/filesystem.py` `_resolve_path`、`filesystem.py` `_resolved_permission_error` |
| K9 | 文件 middleware 与摘要 middleware 都以 `CompositeBackend.artifacts_root` 为前缀：大工具结果写入 `<root>/large_tool_results/`（默认超过 20000 Token），摘要历史写入 `<root>/conversation_history/<session_id>.md` | `filesystem.py` `__init__`、`middleware/summarization.py` |
| K10 | 摘要 middleware 构造为 `SummarizationMiddleware(model, *, backend, trigger, keep, token_counter, summary_prompt, trim_tokens_to_summarize, truncate_args_settings)`；构造函数的 `trim_tokens_to_summarize` 默认 4000，仅摘要待摘要历史末尾的部分，传 `None` 才取消这层裁剪；`summary_prompt` 默认值 `DEEPAGENTS_DEFAULT_SUMMARY_PROMPT` 必须保留 `<messages>` 标记行和 `{messages}` 占位符 | `summarization.py`、LangChain `agents/middleware/summarization.py` `_trim_messages_for_summary` |
| K11 | `DeepAgentState.messages` 使用 `DeltaChannel(..., snapshot_frequency=50)`；自定义 `state_schema` 必须是 `DeepAgentState` 子类 | `graph.py` `DeepAgentState` |
| K12 | 只要 backend（含 Composite 的 default 或任一路由）是 `StateBackend`，文件 middleware 会在状态中加入 `files` 通道 | `filesystem.py` `_uses_state_backend` |
| K13 | `PrivateStateAttr` 字段不进入图的输入／输出 Schema，但仍是普通通道，`get_state().values` 可读取 | LangChain `agents/middleware/types.py`、LangGraph `graph/state.py` `stream_channels` |
| K14 | LangChain 的 `ModelCallLimitMiddleware`／`ToolCallLimitMiddleware` 的 `run_*` 计数是 `UntrackedValue`，运行结束后不能从状态读出 | LangChain `model_call_limit.py`、`tool_call_limit.py` |
| K15 | `ToolRuntime` 提供 `state`、`context`、`config`、`tool_call_id`、`store` 等字段 | LangGraph `prebuilt/tool_node.py` |
| K16 | `PostgresSaver(conn)` 可接受 `ConnectionPool`；`from_conn_string` 使用 `autocommit=True, prepare_threshold=0, row_factory=dict_row`；提供 `delete_thread(thread_id)` | `checkpoint-postgres/langgraph/checkpoint/postgres/__init__.py` |
| K17 | Skill 目录名必须与 `SKILL.md` frontmatter 中的 `name` 一致，只允许小写字母、数字和单个连字符 | `middleware/skills.py` |
| K18 | `ToolStrategy` 的结构化输出工具只在绑定模型时追加，不在 `ToolNode` 与 `request.tools` 中；有结构化输出工具时每次模型请求都带 `tool_choice="any"`。`ChatTongyi.bind_tools` 原样传递该值，`ChatOpenAI` 映射为 `"required"` | LangChain `agents/factory.py` `_get_bound_model`；`langchain_community` `tongyi.py` |
| K19 | 工具执行后的路由：本批已执行工具**全部** `return_direct=True` 即结束循环，不检查 `ToolMessage` 是否为错误；混合批次不结束。`after_model` 钩子声明 `can_jump_to=["model", ...]` 后可跳回模型节点 | `factory.py` `_make_tools_to_model_edge`、`_get_can_jump_to` |
| K20 | `ToolNode` 并行执行同一条 AI 消息中的多个工具调用，每个工具的 `runtime.state` 都是本步开始时的状态；工具返回的 `Command(update=...)` 在本步全部工具结束后才由图合并 | LangGraph `prebuilt/tool_node.py` |
| K21 | 最新摘要事件位于私有状态键 `_summarization_event`，包含 `summary_message: HumanMessage`、`cutoff_index: int`、`file_path: str \| None`；`_summarization_session_id` 是历史转存文件标识，不是摘要文本，也不是业务 `session_id`。这些字段标记为 `PrivateStateAttr`，读取应使用 `get_state().values`（K13），不能依赖 `invoke` 返回值 | `middleware/summarization.py` `SummarizationEvent`、`SummarizationState`、`_get_session_id` |
| K22 | 摘要调用的 `_summary_config()` 只显式提供 metadata；callbacks 经节点设置的 `var_child_runnable_config` 与 `ensure_config` 继承。显式 metadata 整体替换上下文同名键，摘要回调不保证有 invoke 层的业务 ID；模型实例 metadata 后加入，可覆盖同名用途标记 | LangChain `middleware/summarization.py` `_summary_config`；`langchain_core/runnables/config.py` `ensure_config`、`callbacks/manager.py` `_configure` |

现有代码中与新设计直接相关的事实：

| 编号 | 事实 | 代码位置 |
| --- | --- | --- |
| C1 | `SQLValidationTool.__init__` 固定调用全局 `get_pg_client()`；语义层通过 `PGClient.explain_query` 在池化连接上执行会话级 `SET statement_timeout`，不复位 | `src/tools/validation/sql_validation.py`、`src/services/db/pg_client.py` |
| C2 | `PGClient.execute_query` 直接 `fetchall()`，不限制行数，也不使用只读事务；父图配置中的 `max_rows_per_query: 10000` 未被该方法使用 | `pg_client.py`、`src/modules/nl2sql_father/config/nl2sql_father_graph.yaml` |
| C3 | `MilvusSearchAdapter.__init__` 固定调用全局 `get_embedding_client()`；`EmbeddingClient` 只有在读取全局 active profile 时才设置 `dashscope.api_key` | `src/services/vector_adapter/milvus_adapter.py`、`src/services/embedding/embedding_client.py` |
| C4 | `Neo4jClient` 固定使用全局 `get_neo4j_manager()`；`_find_path_apoc_dijkstra` 与 `_find_path_shortest` 遇到 `Neo4jError` 都返回 `None`，与“无路径”无法区分 | `src/services/db/neo4j_client.py` |
| C5 | `SchemaRetriever` 构造时绑定全局 PG、Neo4j、Embedding 单例，`retrieve()` 是固定全流程 | `src/tools/schema_retrieval/retriever.py` |
| C6 | `build_optimized_filters` 对未命中的 `role="value"` 维度仍输出 `value=<原文>`；`_format_time_hints` 不检查 `start < end` | `src/tools/schema_retrieval/value_matcher.py`、`src/modules/sql_generation/subgraph/nodes/sql_generation.py` |
| C7 | `sqlparse` 对 `SELECT 1;`、`SELECT 1; `、`SELECT 1;\n` 均解析为 1 条语句；`SELECT 1;;` 与 `SELECT 1; DELETE ...` 解析为 2 条 | 在根 `.venv` 中实测 |
| C8 | 当前 `.env` 的 `DB_NAME=orders`、`LANGGRAPH_DB_NAME=lg_persist`；全局 `config.yaml` 的业务库默认值为 `${DB_NAME:nl2sql}`，持久化 Schema 为 `public` | `.env`、`src/configs/config.yaml` |
| C9 | 现有日志器统一为 `logging.getLogger("nl2sql.<module>")`，文件处理器在 `src/configs/logging.yaml` | `src/utils/logger.py` |
| C10 | `src/tests/conftest.py` 已提供 `--run-db`／`--run-llm` 开关，未开启时跳过 `requires_db`／`requires_llm` 用例 | `src/tests/conftest.py` |
| C11 | `psycopg_pool` 在 `configure` 回调返回后检查连接状态，不是 `IDLE`（例如 `autocommit=False` 时执行查询后留下 `INTRANS`）即丢弃该连接，持续发生时表现为取连接超时 | `psycopg_pool/pool.py` `_connect` |
| C12 | `llm_factory` 对 DashScope 拒绝 `timeout` 覆盖参数；但 DashScope SDK 的 `_build_api_request` 接收 `request_timeout`（默认 300 秒），`ChatTongyi` 把 `model_kwargs` 原样并入 `Generation.call` 参数，`TextEmbedding.call` 同样经过该函数 | `src/services/llm_factory.py`；`dashscope/api_entities/api_request_factory.py`、`common/constants.py` |

## 1. 范围

本篇覆盖概要设计第一阶段的全部实现内容：

- 新模块 `src/modules/db_deepagent/` 的目录、文件职责与依赖装配。
- 10 个业务工具的接口和服务实现（概要设计 §6.1 的 7 个核心工具，以及落实 §8.1 守卫所需的 `resolve_time_range`、`probe_dimension_values`、`reuse_evidence`），外加最终答案提交工具 `submit_final_answer`。
- SQL 校验、可信交接、只读执行、结果存储与分析下推的完整管线。
- 时间／维度守卫的规则与算法。
- Deep Agent 装配：模型、HarnessProfile、backend、权限、middleware、Skill、提示词、最终答案提交。
- PG checkpoint／Store 的 Schema 隔离、归档、线程轮换和清理。
- Runner、结果协议、CLI。
- 测试布局，以及与验收题集 30 项用例的对应关系。

不覆盖：FastAPI、Web UI、网络检索、主分析 Agent、业务文档 RAG、子 Agent 委派、通用代码执行环境（与概要设计 §1.1 一致）。

## 2. 依赖与环境准备

### 2.1 根项目依赖变更

只修改根目录 `pyproject.toml` 与 `uv.lock`，通过 `uv` 同步到根 `.venv`。新模块不建独立环境。

| 变更 | 内容 | 原因 |
| --- | --- | --- |
| 新增 | `deepagents==0.7.25` | 概要设计 §2.1 |
| 修改 | `monitoring` 可选依赖中的 `langsmith==0.14.1` → `langsmith==0.14.8` | `deepagents==0.7.25` 要求 `langsmith>=0.14.8`，概要设计 §2.2 已验证不对齐会解析失败 |
| 新增 | `sqlglot`（版本在依赖准备时与上两项一起 `uv lock` 后精确固定；本篇的 AST 节点名与占位符哨兵行为已用 30.22.0 实测） | §9 的 SQL 守卫需要 PostgreSQL 方言的 AST；现有 `sqlparse` 只能分词，不足以可靠识别谓词与字面量 |

`deepagents==0.7.25` 还会带入 `langchain-anthropic`、`langchain-google-genai`、`wcmatch`、`packaging` 等传递依赖（来自其 `pyproject.toml`）。`deepagents/graph.py` 在模块顶层导入 `langchain_anthropic`，因此这些传递依赖必须安装，但本项目不使用相应模型服务。

### 2.2 其他根目录改动

- `.gitignore` 增加 `var/`，覆盖新模块默认工作目录 `var/db_deepagent/`（结果文件、会话 artifacts、运行记录）。
- 不修改 `pytest.ini`：`testpaths = src/tests` 已能发现 `src/tests/db_deepagent/`。

### 2.3 依赖变更前后的检查

按概要设计 §11.1 执行，交付物写入 `var/db_deepagent/acceptance/baseline/`：

1. **依赖变更前**冻结旧入口基线，写入 `baseline/`：
   - 两个旧 CLI 的真实查询冒烟记录（命令、输入、输出、时间）。
   - 旧测试清单：保存准确的收集命令与执行命令（含 `--run-db`／`--run-llm` 等开关）、`pytest --collect-only -q` 得到的 node ID 清单（`node_ids.txt`）、JUnit 报告（`junit.xml`）、已知失败与跳过项及原因（`known_issues.md`）、`uv pip freeze` 输出。
   - “167 项”只是待核对的范围描述：实际收集数量不是 167 时，记录实际数量、范围与差异来源，不补数量，也不把 167 当作已验证的成绩。
2. 执行依赖变更与 `uv sync`；`sqlglot` 以 `uv lock` 解析出的版本精确锁定（`==`），并在锁定后重跑 §9.9 的守卫用例确认该版本的 PostgreSQL 方言解析行为。
3. 运行 `python -m src.modules.db_deepagent.cli doctor --stage env`（§15.3）确认：`deepagents.__version__ == "0.7.25"`；LangChain、LangGraph、`langchain-core`、`langgraph-checkpoint-postgres` 版本与概要设计 §2.1 一致；`sqlglot` 版本等于锁定值。
4. 用第 1 步保存的同一命令与同一 node ID 清单复测；原通过项不得新增失败，结果与基线逐项对比后存档。

## 3. 模块结构与文件职责

```text
src/modules/db_deepagent/
├── __init__.py                  # 导出 DbAgentRunner、DbAgentResult、build_db_agent
├── agent.py                     # 组装 Deep Agent：模型、profile、backend、middleware、工具
├── runner.py                    # 单次请求执行、结果组装、归档、线程轮换
├── cli.py                       # 命令行入口（query / chat / doctor / maintenance / acceptance）
├── schemas.py                   # 工具返回包、账本记录、最终答案与结果协议（Pydantic / TypedDict）
├── state.py                     # DbDeepAgentState（继承 DeepAgentState）与 reducer
├── context.py                   # DbAgentContext（运行级不可变上下文）
├── config.py                    # 配置模型、加载、校验、指纹
├── errors.py                    # 模块异常与工具错误类型常量
├── config/
│   ├── db_deepagent.yaml        # 运行参数
│   └── prompts.yaml             # 系统提示、摘要补充要求、工具说明补充
├── tools/
│   ├── __init__.py              # build_business_tools(services) -> list[BaseTool]
│   ├── _envelope.py             # 统一返回包与 Command 构造工具函数
│   ├── schema_tools.py          # search_schema、inspect_tables、plan_joins
│   ├── guard_tools.py           # resolve_time_range、probe_dimension_values、reuse_evidence
│   ├── sql_tools.py             # validate_sql、execute_sql
│   ├── result_tools.py          # read_result、analyze_result
│   └── final_answer.py          # submit_final_answer
├── services/
│   ├── __init__.py              # DbAgentServices 容器与生命周期
│   ├── business_db.py           # orders 只读连接池、结构查询、EXPLAIN、只读执行
│   ├── embedding.py             # 复制改造的 Embedding 客户端（显式 profile 与 api_key）
│   ├── embedding_consistency.py # 查询侧与入库侧模型、集合维度一致性检查
│   ├── milvus_retrieval.py      # 复制改造的 Milvus 适配器（注入 Embedding 客户端）
│   ├── table_catalog.py         # 表分类、表键转换、表卡片与实时结构合并
│   ├── join_graph.py            # 复制改造的 Neo4j JOIN 路径服务（区分无路径与失败）
│   ├── sql_validation.py        # 复制改造的三层校验（注入 BusinessDbService）
│   ├── sql_guard.py             # 时间／维度证据守卫（sqlglot）
│   ├── sql_pipeline.py          # 规范化、占位符展开、授权记录、校验与执行编排
│   ├── time_resolver.py         # 时间表达识别与半开边界计算
│   ├── dimension_probe.py       # 受控维度值探针 SQL 生成
│   ├── result_store.py          # 业务结果落盘、引用、会话检查、分页读取
│   ├── result_analysis.py       # 确定性本地计算与下推 SQL 生成
│   └── run_budget.py            # RunBudgetLedger：运行级计数器（加锁预占）
├── middleware/
│   ├── __init__.py
│   ├── run_budget.py            # 模型／工具调用预算与协作式运行时限
│   ├── request_audit.py         # 模型请求日志、实际工具列表捕获
│   ├── model_observer.py        # 公开模型回调：主模型／摘要调用尝试、用量与耗时
│   └── final_answer_gate.py     # 最终答案提交后结束运行；未提交时提醒一次
├── persistence/
│   ├── __init__.py
│   ├── platform_pool.py         # lg_persist 连接池（固定 search_path）与 Schema 核对
│   ├── checkpoint.py            # PostgresSaver / InMemorySaver 创建
│   ├── archive.py               # PostgresStore 问答归档、错误记录、会话索引
│   └── maintenance.py           # 过期线程与结果清理（支持预览）
├── skills/
│   ├── query-planning/SKILL.md
│   ├── schema-retrieval/SKILL.md
│   └── result-analysis/SKILL.md
├── memory/
│   └── memory_context.md        # 可选，默认不加载
└── README.md
```

测试目录见 §18。

### 3.1 复用与复制清单

遵循概要设计 §4 的复用规则；复制的文件在模块文档字符串首行写明来源路径。

| 新文件 | 来源 | 处理方式 | 改造点 |
| --- | --- | --- | --- |
| `services/embedding.py` | `src/services/embedding/embedding_client.py` | 复制改造 | 构造时显式接收 profile 与 `api_key`，调用 `TextEmbedding.call(..., api_key=...)`；不写 `dashscope.api_key` 全局变量；去掉 `print` 改用日志 |
| `services/milvus_retrieval.py` | `src/services/vector_adapter/milvus_adapter.py`、`src/services/vector_db/milvus_client.py` | 复制改造 | 构造时注入 Embedding 客户端；提供集合向量字段维度读取；不实例化 pgvector 适配器；连接别名固定为 `db_deepagent`，避免与旧链路的 `default` 别名互相影响 |
| `services/join_graph.py` | `src/services/db/neo4j_client.py` | 复制改造 | 注入 `Neo4jConnectionManager(config)` 实例；路径查询返回三态（找到、无路径、失败）及所用策略 |
| `services/table_catalog.py` | `SchemaRetriever._classify_table_category` 与类别映射配置 | 复制改造 | 分类映射来自新模块配置；不调用 `SchemaRetriever.retrieve()` |
| `services/sql_validation.py` | `src/tools/validation/sql_validation.py` | 复制改造 | 注入 `BusinessDbService`；EXPLAIN 在只读事务内用 `SET LOCAL statement_timeout`；返回结构增加 `error_codes` |
| `services/business_db.py` | `PGClient.explain_query`、`execute_query`、`get_table_columns` | 复制所需方法 | 独立连接池；只读事务；服务端游标与读取上限；不含 pgvector 方法 |
| 直接导入 | `src/tools/schema_retrieval/table_names.to_display_name` | 直接复用 | 纯函数 |
| 直接导入 | `src/services/llm_factory.get_llm`、`src/services/config_loader.ConfigLoader`／`get_config` | 直接复用 | 只读取全局配置，不修改 |
| 直接导入 | `src/services/db/neo4j_connection.Neo4jConnectionManager` | 直接复用类 | 用显式配置实例化，不使用 `get_neo4j_manager()` 单例 |

不复制、不调用：原父图、原 SQL 子图、`SchemaRetriever.retrieve()`、`build_optimized_filters`、`SafeCheckpointer`。

## 4. 配置设计

### 4.1 `config/db_deepagent.yaml`

使用现有 `ConfigLoader(path).load()` 读取（支持 `${VAR:default}`，并加载根 `.env`）。下例即默认值；带“需实测调整”注释的数值在实现阶段按实际模型确定。

```yaml
model:
  llm_profile: qwen3_8_max               # 引用全局 config.yaml 的 llm_profiles（2026-10-15 变更：qwen3-max 已下线，改用
                                          # qwen3.8-max；实测该模型仅在 compatible-mode 服务，profile 走 dashscope_openai/ChatOpenAI，
                                          # 匹配键 openai:qwen3.8-max，request_timeout 经 overrides.timeout 映射（§11.1 备选路径转正）
  overrides:
    temperature: 0
  # 可选：显式声明期望的 SDK 匹配键；声明后与实际解析结果不一致则初始化失败
  expected_harness_key: null             # 例如 "openai:qwen3.8-max"（K5）
  request_timeout_seconds: 120           # 单次模型 HTTP 请求超时（§11.1），需实测调整

embedding:
  profile: qwen3_7_text_embedding        # 引用全局 embedding_profiles，不跟随 active
  release_record: src/modules/db_deepagent/config/embedding_release.yaml
  request_timeout_seconds: 30

business_db:
  host: ${DB_HOST:localhost}
  port: ${DB_PORT:5432}
  database: ${DB_NAME}
  user: ${DB_USER}
  password: ${DB_PASSWORD}
  expected_database: orders              # 解析后与 database、current_database() 双重核对
  table_key_prefix: orders               # Milvus / Neo4j 三段表键的首段
  timezone: Asia/Shanghai                # 连接级 TimeZone
  pool_min_size: 1
  pool_max_size: 5
  explain_timeout_seconds: 5
  statement_timeout_seconds: 30
  max_saved_rows: 10000                  # 单次查询最多保存行数，超出即 source_truncated

platform_db:
  host: ${LANGGRAPH_DB_HOST:localhost}
  port: ${LANGGRAPH_DB_PORT:5432}
  database: ${LANGGRAPH_DB_NAME}
  user: ${LANGGRAPH_DB_USER:postgres}
  password: ${LANGGRAPH_DB_PASSWORD:}
  expected_database: lg_persist
  schema: db_deepagent
  sslmode: disable
  connect_timeout_seconds: 5
  statement_timeout_ms: 5000
  pool_min_size: 1
  pool_max_size: 4

maintenance:
  statement_timeout_ms: 60000            # 清理专用连接，每条语句的初始超时，待实测
  lock_timeout_ms: 5000                  # 不长时间等待锁；不修改交互平台池的 5000 ms 超时

neo4j:
  uri: ${NEO4J_URI:bolt://localhost:7687}
  user: ${NEO4J_USER:neo4j}
  password: ${NEO4J_PASSWORD}
  database: ${NEO4J_DATABASE:neo4j}
  join_max_hops: 5
  join_strategy: apoc_dijkstra           # apoc_dijkstra | shortest_path
  allow_shortest_path_fallback: true
  connection_timeout_seconds: 10
  query_timeout_seconds: 15

milvus:
  host: ${MILVUS_HOST:localhost}
  port: ${MILVUS_PORT:19530}
  database: ${MILVUS_DATABASE:nl2sql}
  user: ${MILVUS_USER:}
  password: ${MILVUS_PASSWORD:}
  alias: db_deepagent
  timeout: 30
  search_params:
    metric_type: COSINE
    params: {ef: 100}
  collections:
    table_schema: table_schema_embeddings
    dim_value: dim_value_embeddings
    sql_example: sql_example_embeddings

retrieval:
  table_top_k: 10
  table_min_score: 0.45
  column_top_k: 10
  column_min_score: 0.45
  dim_value_top_k: 3
  dim_value_min_score: 0.75
  sql_example_top_k: 3
  sql_example_min_score: 0.6
  max_top_k: 30                          # 模型可传的 top_k 上限
  table_category_mapping:                # 与旧子图配置取值一致，独立维护
    fact: [fact, 事实表, 交易表, 明细表, 流水表, 快照表, 汇总表]
    dimension: [dim, dimension, 维度表, 实体表, 枚举表, 查找表]
    bridge: [bridge, 桥接表, 关联表]

validation:                              # 三层校验，键名与旧子图 validation 段一致
  enable_syntax_check: true
  enable_security_check: true
  enable_semantic_check: true
  syntax: {allow_multiple_statements: false}
  security:
    allowed_statement_types: [SELECT, WITH, UNKNOWN]
    forbidden_keywords: [DROP, DELETE, TRUNCATE, ALTER, CREATE, INSERT, UPDATE, GRANT, REVOKE, EXEC, EXECUTE]
    allow_comments: false
  semantic:
    explain_analyze: false               # 固定 false；配置为 true 时初始化报错
    warnings: {seq_scan_warn: true, nested_loop_warn: true, estimated_rows_threshold: 100000}
  guard:
    enabled: true
    forbid_realtime_clock_in_filters: true
    require_time_evidence: true
    require_dimension_evidence: true

results:
  preview_rows: 20                       # execute_sql 返回的预览行数
  read_max_rows: 200                     # read_result 单次最多行数
  max_tool_output_chars: 12000           # 业务工具返回文本上限，保证不触发 SDK 大结果转存
  placeholder_max_values: 1000           # 结果占位符展开的最大值个数
  probe_max_values: 50

budget:
  max_model_calls_per_run: 40            # 主 Agent 模型节点的步骤预占上限，不是全部模型／HTTP 请求上限
  max_tool_calls_per_run: 80             # 需实测调整
  max_execute_calls_per_run: 20          # 数据库执行次数，含探针与下推（§11.1）
  run_timeout_seconds: 300               # 协作式时限（§11.1）
  final_answer_reminders: 1              # 模型未提交就结束时的提醒次数（§11.3）

summarization:                           # qwen3_8_max（qwen3.8-max）的初始默认值，待实测；更换模型时重新确定
  summary_trigger_tokens: 96000          # 须低于模型实际输入上限并留余量
  recent_context_tokens: 24000           # 必须小于 summary_trigger_tokens
  trim_tokens_to_summarize: null         # 显式传 None，所选待摘要历史不再按 SDK 默认 4000 Token 裁剪

filesystem:
  tool_token_limit_before_evict: 20000   # SDK 默认值，显式写出便于核对

thread:
  rotate_after_messages: 400             # 线程消息数超过该值时，下一轮前轮换线程
  carry_max_evidence: 200                # 轮换时携带的证据条数上限（§13.4）
  carry_recent_executions: 3             # 轮换时携带的最近执行／分析记录条数（0 关闭，§7.12.1、§13.4）
  checkpoint_retention_days: 30
  archive_retention_days: 180
  result_retention_days: 30              # 结果保留期；同时是复用保留期（§7.12.1，沿用登记不续期）

workspace:
  root: ${DB_DEEPAGENT_WORKSPACE:var/db_deepagent}

clock:
  timezone: Asia/Shanghai
  fixed_now: null                        # 测试时设为 "2026-10-13T12:00:00+08:00"

features:
  todo_list: false                       # 为 true 时追加 LangChain TodoListMiddleware（新增 write_todos 工具）
  memory: false                          # 为 true 时加载 /memory/memory_context.md

persistence:
  mode: postgres                         # postgres | memory（memory 仅调试，必须显式选择）
```

`embedding_release.yaml` 是 RAG 入库／发布侧提供的发布记录，本项目只保存冻结副本，不自行生成。内容示例：

```yaml
release_batch: "rag-2026-10-xx-01"       # 发布批次标识，由发布侧给出
milvus_database: nl2sql                  # 目标 Milvus 数据库
collections:
  table_schema_embeddings: {model: qwen3.7-text-embedding, dimensions: 1024, vector_field: embedding,           released_at: "2026-10-xx"}
  dim_value_embeddings:    {model: qwen3.7-text-embedding, dimensions: 1024, vector_field: col_value_embedding, released_at: "2026-10-xx"}
  sql_example_embeddings:  {model: qwen3.7-text-embedding, dimensions: 1024, vector_field: embedding,           released_at: "2026-10-xx"}
```

入库模型只能取自该记录；不能根据“查询侧 profile 配置为 1024 维”或“集合向量字段为 1024 维”反推入库模型，维度一致只是必要条件。该文件缺失、字段为空或与查询侧、目标库不一致时，初始化失败（§6.3）。

### 4.2 配置模型与校验（`config.py`）

配置加载后转为 Pydantic 模型 `DbAgentConfig`，`model_config = ConfigDict(extra="forbid")`，在加载阶段做以下检查，任一失败抛 `DbAgentConfigError`，消息只含字段名，不含密码：

| 检查 | 规则 |
| --- | --- |
| 业务库目标 | `business_db.database == business_db.expected_database == "orders"` |
| 平台库目标 | `platform_db.database == platform_db.expected_database == "lg_persist"`；`schema` 为合法标识符且不是 `public` |
| 摘要阈值 | `0 < recent_context_tokens < summary_trigger_tokens` |
| 摘要前裁剪 | `trim_tokens_to_summarize` 为 `None` 或正整数，默认 `None`；不省略构造参数而回落 SDK 默认值 |
| 检索阈值 | 四类 `retrieval.*_min_score` 均在 `[0, 1]`，仅由服务端配置提供，不接受模型覆盖 |
| 清理超时 | `maintenance.statement_timeout_ms`、`lock_timeout_ms` 均为正整数，且锁超时小于语句超时；仅用于清理连接 |
| 语义校验 | `validation.semantic.explain_analyze` 必须为 `false` |
| 持久化模式 | `persistence.mode` 只能是 `postgres` 或 `memory` |
| 固定时钟 | `fixed_now` 若设置必须带时区 |
| Embedding profile | 名称存在于全局 `embedding_profiles`，且不是 `active` 键 |

`config.py` 还负责计算两个指纹：

- `validation_fingerprint = sha256(canonical_json(validation 段) + GUARD_RULES_VERSION)`，其中 `GUARD_RULES_VERSION` 是 `sql_guard.py` 中的常量，守卫规则变更时递增。
- `datasource_id` 由 `BusinessDbService` 启动时从实际连接读出后计算（§6.1），不由配置推断。

### 4.3 `config/prompts.yaml`

使用 `yaml.safe_load` 读取，结构固定如下，代码按键名引用，缺键即初始化失败：

```yaml
agent:
  system_prompt: |
    （§12.1 的完整内容）
summarization:
  extra_instructions: |
    保留：用户目标；已确认的指标口径、时间范围及其 time_evidence_id；
    已确认的维度值及其证据来源；已执行查询的 evidence_id、result_ref 与是否截断；
    已得到的数据结论；尚未完成的事项与需要澄清的问题。
    不要编造或改写任何数值、SQL、result_ref 或 evidence_id。
tools:
  # 可选：对业务工具 description 的补充，追加在工具自带说明之后
  validate_sql: ""
  execute_sql: ""
```

摘要提示的组装：在 `DEEPAGENTS_DEFAULT_SUMMARY_PROMPT` 中 `"\n<messages>\n"` 标记之前插入 `<project_requirements>…</project_requirements>` 块，保留 `{messages}` 占位符。组装函数断言结果中 `<messages>` 与 `{messages}` 各出现一次，否则初始化失败（K10）。

**摘要输入范围**：显式设置 `trim_tokens_to_summarize=None`，对 `keep` 保留窗口之外、SDK 本次选中的全部有效历史生成摘要。后续摘要的有效历史可能含前一次摘要，并非每次重读整条线程原始消息。SDK 构造函数默认的 4000 Token 会在这一步只保留待摘要历史的末尾，早期目标、口径及前次摘要可能因此未送入摘要模型；本设计不采用该默认值。

`None` 取消这层主动裁剪，不保证摘要模型无遗漏，也不绕过模型输入上限或 SDK 的溢出重试行为。实施时用接近触发阈值的历史验证实际摘要请求能容纳历史、摘要提示和输出余量；超限时优先调低 `summary_trigger_tokens`，不静默改回 4000。结构化账本与结果快照仍是证据依据；原始消息保留在 checkpoint，成功转存的历史可经 `read_file` 回读，但存在于存储中不代表模型自动可见。开发者若显式改为正整数，意味着接受待摘要历史先裁剪的取舍，应记录生效值并重新评测。

## 5. 运行时上下文与状态

### 5.1 运行级上下文 `DbAgentContext`（`context.py`）

每次 `agent.invoke` 由 Runner 新建，通过 `context=` 传入，工具经 `runtime.context` 读取。模型无法修改。

```python
@dataclass(frozen=True)
class DbAgentContext:
    session_id: str                 # CLI 会话；线程轮换时保持不变；结果归属以它为准
    thread_id: str                  # 当前 checkpoint 线程
    request_id: str                 # 本轮用户请求
    run_id: str                     # 服务端每次 invoke 新建（uuid4），恢复执行也新建
    datasource_id: str              # 业务数据源指纹（§6.1）
    validation_fingerprint: str     # 校验配置指纹（§4.2）
    now: datetime                   # 带时区；fixed_now 或实时时钟
    timezone: str                   # Asia/Shanghai
    user_texts: tuple[str, ...]     # 本线程全部用户原文，最后一项为本轮问题
    carried_evidence: tuple[CarriedEvidence, ...]  # 线程轮换时由服务端携带的已确认证据（§13.4），非轮换线程为空
    deadline_monotonic: float       # 运行时限（time.monotonic 基准）
```

`user_texts` 由 Runner 从 checkpoint 中已有的 `HumanMessage` 内容加本轮问题组成，只计用户真实输入。系统注入的 `HumanMessage`（线程交接消息 §13.4、最终答案提醒 §11.3）在 `additional_kwargs["dbda_kind"]` 中带标记（`handoff`／`final_answer_reminder`），Runner 构造 `user_texts` 时排除。

### 5.2 扩展状态 `DbDeepAgentState`（`state.py`）

按概要设计 §9.3 与 K11，继承 `DeepAgentState`，不覆盖 `messages`。新增字段全部标注 `PrivateStateAttr`（K13）：调用方不能通过 `invoke` 输入注入，模型也看不到；Runner 通过 `get_state().values` 读取。

```python
class DbDeepAgentState(DeepAgentState):
    db_validations: NotRequired[Annotated[dict[str, ValidationRecord], PrivateStateAttr, merge_by_key]]
    # append_dedup_*：按 evidence_id 去重追加（§7.12 沿用幂等——同 ID 同事实保留首条、
    # 同 ID 异事实抛一致性错误；比较事实字段、排除 tool_call_id 等登记调用字段）
    db_executions: NotRequired[Annotated[list[ExecutionRecord], PrivateStateAttr, append_dedup_execution]]
    db_analyses: NotRequired[Annotated[list[AnalysisRecord], PrivateStateAttr, append_dedup_analysis]]
    db_time_evidence: NotRequired[Annotated[list[TimeEvidence], PrivateStateAttr, operator.add]]
    db_dim_evidence: NotRequired[Annotated[list[DimEvidence], PrivateStateAttr, operator.add]]
    db_tool_errors: NotRequired[Annotated[list[ToolErrorRecord], PrivateStateAttr, operator.add]]
    db_run_status: NotRequired[Annotated[dict[str, RunStatusRecord], PrivateStateAttr, merge_by_key]]
    db_final_answers: NotRequired[Annotated[dict[str, FinalAnswerRecord], PrivateStateAttr, merge_by_key]]
```

实现期勘误（2026-10-14，已实测定稿）：metadata 顺序必须是 `[PrivateStateAttr, reducer]`（reducer 垫后）。LangGraph `_is_field_binop` 只把 metadata 末元素识别为 reducer（`graph/state.py`），`PrivateStateAttr` 是 `OmitFromSchema` 实例（非 callable），若垫在末位 reducer 会被静默丢弃，通道退化为 LastValue，同超步双写直接 `InvalidUpdateError`；LangChain 侧 `OmitFromSchema` 剔除扫描全部 metadata（`agents/factory.py` `_resolve_schema`），marker 在前不影响输入／输出 Schema 剔除。另 `NotRequired` 应包在 `Annotated` 外层（`NotRequired[Annotated[...]]`），与 LangChain 自身 `AgentState.jump_to` 写法一致。

- `merge_by_key(left, right)`：浅合并字典，右值覆盖左值，用于授权记录的创建与撤销、运行状态与最终答案（键为 `run_id`）。
- 列表字段只追加，记录内含 `run_id`，读取时按当前 `run_id` 过滤；旧运行的记录只作追溯。
- 所有记录是可 JSON 序列化的 `TypedDict`（不放 `datetime`／`Decimal` 对象，时间用 ISO 字符串），保证 checkpoint 序列化稳定。
- 只有本模块的工具通过返回 `Command(update={...})` 写这些字段；没有任何工具参数能直接写入它们。

### 5.3 账本记录结构（`schemas.py`）

```python
class ValidationRecord(TypedDict):
    key: str                    # sha256(thread|request|run|datasource|validation_fp|normalized_sql)
    status: Literal["valid", "revoked"]
    normalized_sql: str         # 授权匹配文本（规范化后的原文，可含结果占位符）
    expanded_sql_sha256: str    # 占位符展开后的文本摘要；无占位符时为 normalized_sql 的摘要
    placeholder_refs: list[str] # 占位符引用的 result_ref
    purpose: Literal["query", "probe", "pushdown"]
    thread_id: str; request_id: str; run_id: str
    datasource_id: str; validation_fingerprint: str
    layer: str                  # all_passed / syntax / security / semantic / output_columns / guard
    guard_evidence: GuardEvidenceSnapshot  # 守卫实际采用的证据完整内容，执行时直接作为结果快照（§9.7）
    warnings: list[str]
    validated_at: str
    tool_call_id: str

class ExecutionRecord(TypedDict):
    evidence_id: str            # "E-" + sha256(tool_call_id + ":" + purpose) 前 8 位十六进制
    tool_call_id: str
    run_id: str; request_id: str
    purpose: Literal["query", "probe", "pushdown"]
    datasource_id: str          # 执行时的数据源
    sql_template: str           # 授权匹配文本（规范化原文，可含结果占位符）
    executed_sql: str           # 占位符展开后实际提交数据库的 SQL 全文
    executed_sql_sha256: str
    result_ref: str
    columns: list[ColumnMeta]
    saved_row_count: int
    source_truncated: bool
    elapsed_ms: int
    warnings: list[str]
    reused_from: NotRequired[EvidenceOrigin]   # 经 reuse_evidence 沿用登记时的链首原始来源（§7.12）
    result_generated_at: NotRequired[str]      # 结果生成时间 ISO（ResultMeta.created_at；沿用时为原结果时间，见 §7.12）

class AnalysisRecord(TypedDict):
    evidence_id: str            # "A-" + sha256(tool_call_id) 前 8 位十六进制
    tool_call_id: str; run_id: str; request_id: str
    pushdown_execution_id: str | None   # 下推时对应 ExecutionRecord 的 evidence_id，仅作轨迹
    operation: dict             # 操作名与参数（已校验）
    input_result_refs: list[str]
    output_result_ref: str
    executed_sql: str | None    # 下推时为实际执行的 SQL 全文，本地计算为 None
    saved_row_count: int
    source_truncated: bool      # 输出是否受截断影响（下推后通常为 False）
    warnings: list[str]
    reused_from: NotRequired[EvidenceOrigin]   # 经 reuse_evidence 沿用登记时的链首原始来源（§7.12）
    result_generated_at: NotRequired[str]      # 结果生成时间 ISO（沿用时为原结果时间，见 §7.12）

class TimeEvidence(TypedDict):
    time_evidence_id: str; run_id: str
    source_text: str            # 用户原文中的时间表达
    grain: str                  # day / week / month / quarter / year / range
    start: str | None           # 包含的下界；None 表示无下界
    end: str | None             # 不包含的上界；None 表示无上界；两端不可同时为 None（§7.5）
    timezone: str
    datasource_id: str          # 首次取得证据时的数据源；跨数据源沿用时原样保留，仅作追溯（§7.12）
    inherited_from: EvidenceOrigin | None   # 经 reuse_evidence 沿用时的原始来源（§7.12）

class DimEvidence(TypedDict):
    dim_evidence_id: str; run_id: str
    source: Literal["milvus_dim_hit", "db_probe", "prior_result"]
    table: str                  # 两段式 schema.table
    column: str
    values: list[str]           # 已确认存在的真实值
    patterns: list[str]         # 已探测的 ILIKE 模式（仅 db_probe 的 contains 模式）
    query_text: str | None      # 触发证据的用户表达
    ref: str | None             # Milvus 命中分数与生效阈值摘要 / 探针 evidence_id / 来源 result_ref
    datasource_id: str          # 取得该证据时的数据源（prior_result 为来源结果的 datasource_id）；沿用时原样保留
    inherited_from: EvidenceOrigin | None

class EvidenceOrigin(TypedDict):
    evidence_id: str            # 原始 time_evidence_id / dim_evidence_id / 链首原始 E-/A- 证据 ID（§7.12 执行证据沿用）
    thread_id: str; request_id: str; run_id: str
    datasource_id: str          # 原始证据的数据源，不被当前数据源覆盖（执行／分析沿用时取原结果 ResultMeta）
    user_text: str | None       # 原始证据对应的用户原文（时间证据为该轮问题全文；执行／分析沿用为 None）

class GuardEvidenceSnapshot(TypedDict):
    time: list[TimeEvidence]    # 完整记录，不只是 ID
    dim: list[DimEvidence]      # 含占位符生成的 prior_result 证据与下推输入快照中的证据

class CarriedEvidence(TypedDict):
    kind: Literal["time", "dim", "execution", "analysis"]   # 执行／分析记录携带见 §7.12、§13.4
    record: TimeEvidence | DimEvidence | ExecutionRecord | AnalysisRecord   # 旧线程中的原始记录
    origin: EvidenceOrigin

class RunStatusRecord(TypedDict):
    run_id: str
    stop_reason: str | None     # model_call_limit / tool_call_limit / execute_limit / run_timeout / tool_set_mismatch
    counters: dict[str, int]    # 写入时 RunBudgetLedger 的计数快照，仅作轨迹（§11.1）
    final_answer_reminders: int # 已发出的提交提醒次数（§11.3）

class FinalAnswerRecord(TypedDict):
    run_id: str; request_id: str; tool_call_id: str
    answer: dict                # DbAgentFinalAnswer.model_dump()，已通过 §7.11 的检查
    submitted_at: str
```

### 5.4 授权记录的作用域

授权键 `key = sha256("\x1f".join([thread_id, request_id, run_id, datasource_id, validation_fingerprint, normalized_sql]))`。由此直接满足概要设计 §6.2 的约定：

- 同一运行可同时存在多条有效记录（P02）。
- 线程、请求、运行、数据源、校验配置任一不同，键不同，旧记录不可用（P05）。
- 每次 `invoke` 新建 `run_id`，所以运行结束、取消、暂停恢复后都必须重验（P06）。
- 文本任何内部变化都产生不同键（P03、P04）。

## 6. 服务层

所有服务在 `services/__init__.py` 的 `DbAgentServices` 容器中按依赖顺序构造，进程内每个 CLI 会话共享一份；容器提供 `close()` 释放连接池、Milvus 连接、Neo4j 驱动。服务不读取模型提供的连接参数。

```python
@dataclass
class DbAgentServices:
    config: DbAgentConfig
    business_db: BusinessDbService
    embedding: DbEmbeddingClient
    milvus: DbMilvusRetrieval
    catalog: TableCatalog
    join_graph: JoinGraphService
    validator: DbSqlValidator
    guard: SqlEvidenceGuard
    pipeline: SqlPipeline
    time_resolver: TimeResolver
    probe: DimensionProbeBuilder
    results: ResultStore
    analysis: ResultAnalysisService
    budget: RunBudgetLedger
```

### 6.1 `BusinessDbService`（`services/business_db.py`）

**连接池**：`psycopg_pool.ConnectionPool`，`conninfo` 由配置构建，`options` 固定加入 `-c default_transaction_read_only=on -c TimeZone=Asia/Shanghai`；`kwargs={"row_factory": tuple_row, "autocommit": False}`。不注册 pgvector。

业务结果一律按列位置处理：psycopg 的 `dict_row` 实现为 `dict(zip(names, values))`，`SELECT o.id, r.id` 这类同名输出列会互相覆盖。结构查询等服务固定 SQL 可在各自的游标上单独指定 `row_factory=dict_row`；平台库连接池仍用 `dict_row`（K16）。

`configure` 回调必须让连接以 `IDLE` 状态返回（C11）。`autocommit=False` 时任何查询都会隐式开启事务，所以检查放在显式事务上下文中：

```python
def _configure(conn: psycopg.Connection) -> None:
    with conn.transaction():                    # 退出时提交，连接回到 IDLE
        db, _role, _search_path = conn.execute(          # 连接级 row_factory 为 tuple_row，按位置解包
            "SELECT current_database(), current_user, current_setting('search_path')"
        ).fetchone()
    if db != expected_database:
        raise DatasourceMismatchError(f"业务库应为 {expected_database}，实际为 {db}")
```

回调抛异常时连接池丢弃该连接并记录错误。单元测试用 `tuple_row` 的连接替身执行该回调，断言两点：数据库名正确时正常返回，且返回后 `conn.info.transaction_status == TransactionStatus.IDLE`；数据库名不符时抛 `DatasourceMismatchError`。任何在业务连接上按列名取值的代码，都必须在自己的游标上显式指定 `row_factory=dict_row`，不能依赖连接默认值。

**数据源指纹**：启动时（同样按位置解包）读取 `current_database()`、`current_user`、`current_setting('search_path')`、`inet_server_addr()`、`inet_server_port()`，计算 `datasource_id = "pg:" + sha256(...)[:16]`。日志只输出数据库名、角色名和指纹，不输出密码。连接目标或执行角色变化都会改变 `datasource_id`；它随证据与结果保存，用于 §7.12、§8.3、§7.10 的来源检查。

**只读执行原语**（所有业务 SQL 的唯一出口）：

```python
def run_readonly(self, sql: str, *, timeout_s: int, max_rows: int) -> ReadonlyResult:
    """在只读事务和服务端游标内执行，读取 max_rows + 1 行以判定 source_truncated。"""
```

执行步骤：

1. `with pool.connection() as conn, conn.transaction():`
2. `SET TRANSACTION READ ONLY`（事务内第一条语句）；`SET LOCAL statement_timeout = <timeout_ms>`（事务结束自动复位，修正 C1 的会话级泄漏）。`timeout_ms` 取配置超时与本运行剩余时限（§11.1）中的较小值。
3. 去掉末尾一个 `;` 后（仅用于游标包装，不影响授权文本），使用命名游标 `conn.cursor(name=f"dbda_{uuid}")` 执行，`fetchmany(max_rows + 1)`。
4. 执行后、读取行之前检查 `cursor.description` 的列名：有重复（包括 PostgreSQL 给未命名表达式自动生成的 `?column?`）时，不读取行、不保存结果，抛 `DuplicateColumnNamesError`，工具层转换为 `duplicate_column_names` 错误（列出重复的列名，提示“为输出列添加唯一别名”）。
5. 返回列名、列的 PostgreSQL 类型名（`cursor.description` 的 `type_code` 经 `pg_type` 映射缓存）、按位置排列的元组行（最多 `max_rows`）、`source_truncated = 取到行数 > max_rows`、耗时。

输出列名唯一后，`read_result` 的 `columns` 子集、占位符 `{{result:<ref>:<column>}}`、`analyze_result` 的列参数都可以按名称无歧义地映射到列位置。`ResultStore` 在 `meta.columns` 中按位置保存列名与类型，所有按名称的访问都先转换为位置。

**EXPLAIN 原语**：`explain(sql, timeout_s)` 同样在只读事务内执行 `EXPLAIN <sql>`，从不使用 `ANALYZE`。

**结构查询**（服务固定 SQL，参数化执行，不经过校验器，概要设计 §6.1）：`get_columns(schema, table)`（`information_schema.columns`：列名、类型、是否可空、序号）、`table_exists(schema, table)`、`column_type(schema, table, column)`。

**错误映射**：

| SQLSTATE／异常 | `error_type` | `retryable` |
| --- | --- | --- |
| `42xxx`（语法、未定义表／列、类型错误） | `db_semantic_error` | true |
| `22xxx`（数据异常，如无效日期） | `db_data_error` | true |
| `57014`（`statement_timeout` 取消） | `timeout` | true（提示缩小范围或下推聚合） |
| `25006`（只读事务内写操作） | `readonly_violation` | false |
| 连接失败、池超时 | `datasource_unavailable` | false |

### 6.2 `DbEmbeddingClient`（`services/embedding.py`）

复制 `EmbeddingClient`，构造签名改为 `DbEmbeddingClient(profile: dict, api_key: str, request_timeout_s: int)`；`profile` 来自全局 `embedding_profiles[<配置名>]`，`api_key` 来自全局 `llm_providers[profile.provider].api_key`。调用 `TextEmbedding.call(model=..., input=..., dimension=..., api_key=api_key, request_timeout=request_timeout_s)`（C12），重试逻辑沿用原实现。返回向量长度与 `dimensions` 不一致时抛 `EmbeddingDimensionError`。

### 6.3 Embedding 一致性检查（`services/embedding_consistency.py`）

初始化时执行，任一失败抛 `EmbeddingConsistencyError`，CLI 显示具体集合与字段，不进入 Agent 创建：

1. 读取 `embedding_release.yaml`；文件缺失，或 `release_batch`、`milvus_database`、任一集合的 `model`／`dimensions`／`vector_field` 为空，视为“入库模型未知”。
2. 目标一致：`milvus_database` 等于配置 `milvus.database`；记录中的集合名集合等于配置 `milvus.collections` 的三个值。
3. 模型一致：三个集合的 `model` 必须等于查询侧 profile 的 `model`，`dimensions` 必须等于 profile 的 `dimensions`。入库模型只认发布记录，不由维度反推。
4. 通过 `Collection(name, using=cfg.milvus.alias)` 读取记录中 `vector_field` 的 `params["dim"]`，必须等于 profile 的 `dimensions`。必须显式传入新模块的连接别名 `db_deepagent`，避免检查到旧链路的 `default` 连接（可能指向别的库）。
5. 用 profile 对固定文本生成一次查询向量，长度必须等于 `dimensions`。

检查通过后，日志与 `doctor` 输出 `release_batch`、`milvus_database` 与三个集合名，便于核对评测所用的入库批次。

不自动重建集合，也不回退到其他 profile（概要设计 §5.2）。

### 6.4 `DbMilvusRetrieval`（`services/milvus_retrieval.py`）

复制 `MilvusSearchAdapter` 的 `search_tables`、`search_columns`、`search_dim_values`、`search_similar_sqls`、`fetch_table_cards`、`fetch_table_categories`，字段映射、COSINE 分数处理、JSON 序列化表达式保持不变；改动：

- 构造时注入 `DbEmbeddingClient` 和配置中的集合名、搜索参数、连接别名；所有 `Collection(...)`、`utility.*` 调用都显式传 `using=<别名>`，搜索传 `timeout=cfg.milvus.timeout`。
- `search_dim_values` 的结果增加 `query_value`，`dim_table` 保持 Milvus 原值（三段表键）。
- 新增 `vector_field_dim(collection_name, field_name) -> int` 供 §6.3 使用。
- Milvus 异常向上抛出 `RetrievalBackendError`，不吞掉、不降级为空列表（与旧实现对历史 SQL 检索“失败降级为空”不同，新链路要求显式区分失败与无命中）。

### 6.5 `TableCatalog`（`services/table_catalog.py`）

- `normalize_table_id(s) -> TableKey`：接受两段 `schema.table` 或三段 `db.schema.table`。两段时补 `table_key_prefix` 得到三段键；三段时首段必须等于 `table_key_prefix`，否则报 `invalid_table_id`。`TableKey` 同时保存 `key`（三段）与 `display`（`to_display_name(key)`）。
- `classify(category) -> "fact" | "dimension" | "bridge"`：沿用旧逻辑，映射来自新配置；未配置的类别归为 `dimension` 并记日志。
- `describe(table_keys, include_live_columns)`：合并 Milvus 表卡片与 `BusinessDbService.get_columns`；输出每张表的 `table_category`、`category_group`、`time_col_hint`、`card_text`（截断到 2000 字符，标注是否截断）、`live_columns`、`card_only_columns`／`live_only_columns` 差异提示。卡片的列信息来自 `object_desc` 文本，无法可靠逐列解析时只给出实时列清单，并在 `warnings` 中说明“卡片未提供结构化列信息”。Milvus 无 `grain_hint`，返回 `null`，不伪造（概要设计 §5.2）。

### 6.6 `JoinGraphService`（`services/join_graph.py`）

复制 `Neo4jClient.find_join_path`、`_find_path_apoc_dijkstra`、`_find_path_shortest`、`_extract_path_info`、`check_table_exists`，改为返回 `JoinPathOutcome`：

```python
@dataclass
class JoinPathOutcome:
    base: str; target: str
    status: Literal["found", "no_path", "table_not_in_graph", "error"]
    strategy: Literal["apoc_dijkstra", "shortest_path"] | None
    fallback_used: bool
    edges: list[dict]           # src_table, dst_table, join_type, cardinality, on, cost
    total_cost: float | None
    error: str | None
```

驱动以 `connection_timeout=cfg.neo4j.connection_timeout_seconds` 创建；每条 Cypher 以 `neo4j.Query(text, timeout=cfg.neo4j.query_timeout_seconds)` 执行，超时按 `error` 处理。

判定规则：先 `check_table_exists` 两端，缺失即 `table_not_in_graph`；APOC 调用抛 `Neo4jError` 时记录错误，若允许回退则调用 `shortestPath`，`fallback_used=True`；`shortestPath` 抛错为 `error`；两种查询都正常执行但无结果为 `no_path`。

### 6.7 `DbSqlValidator`（`services/sql_validation.py`）

复制 `SQLValidationTool` 的三层检查逻辑与配置键，改动：

- 构造签名 `DbSqlValidator(config: ValidationConfig, db: BusinessDbService)`；语义层调用 `db.explain(...)`。
- 返回 `ValidationOutcome(valid, layer, errors, warnings, explain_plan, error_codes)`；`error_codes` 为机器可读的错误码（如 `multiple_statements`、`forbidden_keyword:DELETE`、`comment_not_allowed`、`explain_failed`），便于测试断言。
- 继承旧安全检查的已知局限：关键字匹配作用于整条 SQL 的大写文本，字符串字面量中出现 `DELETE` 等词也会被拒绝。第一阶段保持该行为，不在复制版中放宽；只读事务与只读角色作为后备防线。

### 6.8 `ResultStore`（`services/result_store.py`）

**目录**：`<workspace>/results/<result_ref>/`，与 backend 根目录（§10.3）互不包含，也不通过符号链接暴露。`result_ref = "res_" + uuid4().hex`。

**文件**：

- `rows.jsonl`：每行一个 JSON 数组；值按类型编码：`Decimal` → `{"$dec": "123.45"}`，`date`／`datetime` → ISO 字符串 + 列类型标注，`bytes` → 不保存原文，记为 `{"$bin": <长度>}`。
- `meta.json`：`result_ref`、`session_id`、`thread_id`、`request_id`、`run_id`、`purpose`、`datasource_id`（执行查询时的数据源；本地计算结果取输入结果的值）、`sql_template`、`executed_sql`（实际执行的 SQL 全文）、`executed_sql_sha256`、`columns`（名称与 PG 类型）、`saved_row_count`、`source_truncated`、`created_at`、`rows_sha256`、`derived_from`（分析结果的输入引用）、`guard_evidence`。
- `guard_evidence`：该结果的 SQL 通过守卫时实际用到的时间证据与维度证据**完整记录**（§9.7），本地计算结果则为各输入结果 `guard_evidence` 的并集。结果在保留期内即可独立追溯口径与做下推校验，不依赖原线程 checkpoint 是否仍在。

**接口**：

```python
def save(self, *, ctx: DbAgentContext, purpose: str, sql_template: str | None,
         executed_sql: str | None, columns, rows, source_truncated: bool,
         derived_from: list[str], guard_evidence: GuardEvidenceSnapshot) -> ResultMeta
def load_meta(self, result_ref: str, *, session_id: str) -> ResultMeta
def read_rows(self, result_ref: str, *, session_id: str, columns: list[str] | None,
              offset: int, limit: int) -> RowPage
def column_values(self, result_ref: str, *, session_id: str, column: str) -> list[Any]
```

- `result_ref` 必须匹配 `^res_[0-9a-f]{32}$`，否则 `invalid_result_ref`；目录不存在为 `result_not_found`。
- `meta.session_id != session_id` 时返回 `result_access_denied`，不泄露该引用是否存在于其他会话（P10）。
- 读取时校验 `rows_sha256`，不一致返回 `result_integrity_error`。
- 读取接口**不做保留期判断**（只查文件与会话）；复用保留期（`thread.result_retention_days`）由 `reuse_evidence` 沿用服务显式检查，判定用存储生命周期时钟（注入，禁用 `ctx.now`／`--fixed-now`），统一转 UTC 后 `age >= timedelta(days=retention)` 即过期，沿用登记不续期（§7.12）。
- 写入先写临时目录再原子重命名，避免半成品被读取。
- `meta.datasource_id` 写入后不再修改。读取接口原样返回它，由调用方按 §7.10、§8.3 的规则与当前数据源比较；`read_result` 在两者不同时附加 `warnings`：“该结果来自另一数据源（<原 datasource_id>）”。

### 6.9 其他服务

`SqlPipeline`、`SqlEvidenceGuard`、`TimeResolver`、`DimensionProbeBuilder`、`ResultAnalysisService`、`RunBudgetLedger` 分别在 §8、§9、§7.5／§7.6、§7.10、§11.1 中说明。

## 7. 工具详细设计

### 7.1 通用约定

**构造方式**：`tools/__init__.py` 的 `build_business_tools(services) -> list[BaseTool]` 用闭包注入服务，工具函数使用 `@tool` 装饰，参数用 Pydantic `args_schema` 描述；需要上下文和状态的工具声明 `runtime: ToolRuntime[DbAgentContext, DbDeepAgentState]` 参数（K15，不进入模型可见 Schema）。

**严格参数模型**：全部自定义业务工具与 `submit_final_answer` 的显式 `args_schema` 必须继承统一严格基类，设置 `ConfigDict(extra="forbid")`；嵌套参数对象也使用严格 Pydantic 模型，不依赖默认忽略额外字段的行为。模型传入 `min_score`、`valid` 等未声明字段时，校验失败且业务函数不执行。SDK 内建文件工具的参数模型保持 SDK 定义。

需要 `runtime` 的工具还须兼容 ToolNode 的服务端注入：

```python
from typing import Annotated, Any
from langchain_core.tools import InjectedToolArg
from pydantic import BaseModel, ConfigDict, Field

class StrictToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

class RuntimeToolArgs(StrictToolArgs):
    runtime: Annotated[Any, InjectedToolArg] = Field(exclude=True)

# 需要 runtime 的实际参数模型继承 RuntimeToolArgs，业务函数仍使用具体 ToolRuntime 类型注解。
# 不需要 runtime 的参数模型继承 StrictToolArgs。
```

`runtime` 是内部隐藏字段，由 ToolNode 注入、不进入模型可见工具 Schema、也不写入参数模型的序列化结果。不能将它作为未声明额外字段一起拒绝。ToolNode 会剥离调用方伪造的注入值并使用服务端真实值；通过图执行的集成式单元测试须确认合法注入成功、模型可见 Schema 无 runtime **且包含全部约定业务字段**、额外业务字段仍被拒绝。不用放开 `extra` 来兼容注入。

两处注解各有职责：args_schema 中的 `InjectedToolArg` 标记负责隐藏字段，`StructuredTool` 对注入参数的识别及执行时恢复还依赖业务函数签名。因此业务函数的 `runtime` 必须使用可解析的 `ToolRuntime[DbAgentContext, DbDeepAgentState]` 类型注解，不写成 `Any`、`str` 或无法解析的局部前向引用。只检查“不含 runtime”不足以发现 Schema 整体为空或业务字段丢失；装配验证还须核对全部业务字段及必填项（§10.6、§18.3），并实际验证一次合法注入调用。

**直接调用与单测入口**：只传业务参数的 `.invoke(dict)` 不会自动获得必填 runtime，出现参数校验失败是预期行为；仅把字典包装成完整 ToolCall 也不会自动注入。注入／协议测试须通过 ToolNode 或编译后的 Agent 执行完整 ToolCall（错误消息断言见下文“参数校验错误”）。直接调用调试须由可信测试代码显式提供运行时对象，不能据此认为模型需要或可以提供 runtime。

**参数校验错误**：为每个自定义工具设置公开的 `BaseTool.handle_validation_error = format_tool_validation_error`。统一格式化函数只输出公开字段路径、错误类型和修复提示，文本以 `invalid_argument:` 开头，不回显输入值、runtime 对象或 Python 栈。BaseTool／ToolNode 在业务函数执行前完成校验，返回对应 `tool_call_id` 的 `ToolMessage(status="error")`，模型据此修正参数；不是业务函数内生成的 JSON 错误包，不要求此路径写 `db_tool_errors`。缺失字段、错误类型、约束不符和额外字段都走该路径，不将所有 ToolNode 异常一概吞掉。普通直接 `.invoke(dict)` 调试可能只得到格式化文本，协议测试须通过 ToolNode 的完整 ToolCall 断言错误 ToolMessage。

**返回包**：业务函数实际执行后的返回为 JSON 文本（`ensure_ascii=False`），结构统一为（框架层参数校验错误见上文）：

```json
{
  "status": "ok | empty | error",
  "data": {},
  "warnings": [],
  "error": {"error_type": "...", "message": "...", "retryable": true, "hint": "..."}
}
```

- 需要写状态的工具返回 `Command(update={<账本字段>: ..., "messages": [ToolMessage(content=<返回包>, tool_call_id=runtime.tool_call_id)]})`。
- 返回文本超过 `results.max_tool_output_chars` 时，工具自行截短预览并在 `warnings` 标注，保证业务工具输出永远不会触发 SDK 大结果转存（K9），也就不会产生绕过业务工具的第二条读取路径（概要设计 §8.3）。
- 工具函数内部异常一律转换为 `status="error"`，同时向 `db_tool_errors` 追加记录；不把 Python 栈或连接信息返回给模型。进入函数前的参数校验错误以框架错误 ToolMessage 和审计轨迹记录。

**错误类型总表**：

| `error_type` | 来源 | `retryable` |
| --- | --- | --- |
| `invalid_argument` | 框架参数校验错误 ToolMessage 的语义标记；或业务函数内参数检查的 JSON 错误类型 | true |
| `invalid_table_id`、`table_not_found`、`column_not_found` | 表／列标识 | true |
| `retrieval_backend_error` | Milvus 失败 | false |
| `graph_backend_error` | Neo4j 失败 | false |
| `ambiguous_time_reference` | 裸“当前／目前／现在” | false（改为不加时间过滤或澄清） |
| `time_text_not_in_question` | `source_text` 不在用户原文中 | true（沿用前文口径时改用 `reuse_evidence`） |
| `evidence_not_found` | `reuse_evidence` 的 ID 不在可沿用来源中 | true |
| `evidence_datasource_mismatch` | 沿用的维度证据来自其他数据源 | true（在当前数据源上重新取得证据） |
| `result_datasource_mismatch` | 下推的输入结果来自其他数据源，或两个输入结果的数据源不同 | true（在当前数据源上重新查询） |
| `time_params_mismatch` | 参数与原文时间表达不一致 | true |
| `validation_failed` | 三层校验或守卫未通过（`data.layer`、`data.error_codes` 给出细节） | true |
| `not_validated` | 执行时找不到有效授权记录 | true（先调用 `validate_sql`） |
| `placeholder_error` | 占位符格式错误、位置不受支持、引用无效、源结果被截断或值过多（`data.error_codes` 区分） | true |
| `db_semantic_error`、`db_data_error`、`timeout` | 执行期 | true |
| `duplicate_column_names` | 输出列名重复（校验期静态检查或执行期 `cursor.description` 检查） | true（添加唯一别名） |
| `readonly_violation`、`datasource_unavailable` | 执行期 | false |
| `invalid_result_ref`、`result_not_found`、`result_access_denied`、`result_integrity_error` | 结果读取 | false |
| `result_expired` | `reuse_evidence` 沿用的执行／分析结果超过 `thread.result_retention_days`（存储时钟判定，§7.12） | true（重新查询） |
| `incomplete_source` | 截断结果上请求只能在完整数据上成立的本地计算 | true（改用下推） |
| `budget_exhausted` | 运行预算 | false |
| `final_answer_not_alone` | `submit_final_answer` 与其他工具出现在同一条模型消息中 | true（单独再提交一次） |
| `invalid_evidence_reference` | 最终答案引用了本运行不存在的 `evidence_id` | true |
| `final_answer_already_submitted` | 本运行已有被接受的最终答案 | false |

### 7.2 `search_schema`

用途：Milvus 检索候选表、列、维度值、历史 SQL 案例。

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `query` | str，1–500 字符 | 检索意图文本 |
| `kinds` | list，取值 `tables` / `columns` / `dim_values` / `sql_examples`，默认 `["tables","columns"]` | 检索类型 |
| `value_texts` | list[str]，最多 10 项 | `dim_values` 的待匹配原文；`kinds` 含 `dim_values` 时必填 |
| `top_k` | int，可选，≤ `retrieval.max_top_k` | 覆盖各类型默认值 |

处理：对 `query` 生成一次向量，用于表、列、SQL 案例检索；对每个 `value_texts` 分别检索维度值。表结果附加 `category_group`（§6.5）和两段式 `display`；列结果附加父表 `display`。

阈值固定取自服务端配置的 `table_min_score`、`column_min_score`、`dim_value_min_score`、`sql_example_min_score`。工具参数不含 `min_score`，其显式 `args_schema` 按 §7.1 设置 `extra="forbid"`；模型传入该字段时，框架返回包含 `invalid_argument` 与字段路径 `min_score` 的错误 ToolMessage，业务函数不执行、不访问 Milvus、不登记证据，不假定返回业务 JSON 包。模型可改写 `query`、选择 `kinds` 或在上限内调整 `top_k`，不能调整阈值。每次正常返回与日志记录 `effective_min_scores`（按实际检索类型列出），命中项保留原有 `similarity`，便于解释与排查。

状态写入：维度值命中（分数 ≥ 阈值）按 `(dim_table, dim_col)` 分组写入 `db_dim_evidence`，`source="milvus_dim_hit"`，`values` 为 `matched_text`。未命中的 `value_texts` 不产生任何证据，返回中列入 `data.unmatched_values`，并给出 `hint`：“未命中值不能直接作为过滤条件；可用 probe_dimension_values 在明确的候选列上探测，或向用户澄清”（R04、R06、F10）。

返回 `data`：`tables[]`、`columns[]`、`dim_values[]`（含 `dim_evidence_id`）、`unmatched_values[]`、`sql_examples[]`（`question`、`sql`、`similarity`，SQL 截断到 1500 字符）。

### 7.3 `inspect_tables`

| 参数 | 说明 |
| --- | --- |
| `table_ids` | 1–10 个两段或三段表标识 |
| `include_live_columns` | bool，默认 true |

调用 `TableCatalog.describe`。部分表失败不影响其他表，逐表给出 `status`。返回中 `display` 是 SQL 中应使用的 `schema.table`。

### 7.4 `plan_joins`

| 参数 | 说明 |
| --- | --- |
| `base_table` | 基表标识 |
| `target_tables` | 1–10 个目标表 |
| `max_hops` | 可选，1–5 |

对每个目标调用 `JoinGraphService`，返回逐目标的 `status`、`strategy`、`fallback_used`、`edges`（表名为 `display`；`on` 子句原样返回，并说明其中表名可能是三段键，SQL 中需改用两段名）、`bridge_tables`（路径中出现但不在输入中的表，提示用 `inspect_tables` 补查结构）。全部目标为 `error` 时整体 `status="error"`；部分无路径时 `status="ok"` 并在 `warnings` 中列出。

### 7.5 `resolve_time_range`

用途：由代码计算时间区间并登记时间证据（概要设计 §8.1 第 2 层）。有限区间统一为 `[start, end)`；单边区间允许一个边界为 `None`。SQL 中的时间过滤只能使用本工具返回的非空边界（§9.4）。

| 参数 | 说明 |
| --- | --- |
| `source_text` | 用户原文中的时间表达，逐字引用，如“2026 年 9 月”“本月”“今天” |
| `kind` | `relative` / `calendar` / `explicit_range` / `open_range` |
| `relative` | `kind=relative` 时：`today`、`yesterday`、`this_week`、`last_week`、`this_month`、`last_month`、`this_quarter`、`last_quarter`、`this_year`、`last_year`、`last_n_days` |
| `n` | `last_n_days` 时的天数，1–366 |
| `year`、`quarter`、`month`、`day` | `kind=calendar` 时使用，组合决定粒度 |
| `start_date`、`end_date_inclusive` | `kind=explicit_range` 时使用（用户说“9 月 1 日到 15 日”） |
| `boundary_date` | `kind=open_range` 时必填，ISO 日期 `YYYY-MM-DD`；第一阶段单边边界仅支持日期，不支持任意时刻 |
| `relation` | `kind=open_range` 时必填：`before`（不含边界日）、`through`（截至且含边界日）、`from`（从边界日起且含当天）、`after`（边界日之后且不含当天） |

校验顺序（`TimeResolver`）：

1. `source_text` 必须是 `runtime.context.user_texts` 中某一项的子串（去掉首尾空白后比较），否则 `time_text_not_in_question`。
2. **裸“当前”判定**：`source_text` 含“当前／目前／现在”，且不含时间单位证据，返回 `ambiguous_time_reference`，`hint`：“裸‘当前’不能解释为今天；状态类问题不加时间过滤，确需时间范围时向用户澄清”。时间单位证据正则：
   `\d{4}-\d{1,2}-\d{1,2}|\d{4}\s*年|\d{1,2}\s*月|\d{1,2}\s*[日号]|季度|Q[1-4]|[本上下这]\s*(?:个)?\s*(?:月|周|星期|季度|年)|今[天日年]|昨[天日]|前天|最近|过去|近\s*\d+\s*[天日周月年]|当前\s*(?:月|周|季度|年)|当月|当年|当天|当日`
3. `source_text` 本身必须匹配上面的时间单位证据正则，否则 `time_params_mismatch`。
4. **参数与原文一致性**：原文中出现的相对词与 `relative` 必须对应（“今天／当天／当日”→`today`，“本月／这个月／当月／当前月”→`this_month`，“上月／上个月”→`last_month`，以此类推）；原文中出现的显式年、月、日数字必须与参数相同。不一致返回 `time_params_mismatch`。
   **`last_n_days` 的 N 由代码从原文提取**，不信任模型参数：
   - 用 `(?:最近|过去|近)\s*(\d+|[零一二两三四五六七八九十百]+)\s*(?:个)?\s*(?:天|日)` 在 `source_text` 中匹配，中文数字转换为整数（支持到“三百六十六”）。
   - 恰好匹配到一个 N 时，参数 `n` 必须等于 N，否则返回 `time_params_mismatch`，并在 `hint` 中给出提取到的 N。例如原文“最近 7 天”、参数 `n=30` 必须拒绝。
   - 没有匹配到 N（如“最近几天”“最近一段时间”），或匹配到多个，返回 `time_params_mismatch`，提示向用户确认天数。
   - 原文为“最近 N 周／个月／年”时，第一阶段不映射到 `last_n_days`（周、月的起止口径有多种理解），同样返回 `time_params_mismatch`，提示向用户确认具体范围。
   **`open_range` 的方向、日期与包含关系同样由代码核对**：`source_text` 必须包含日期与单边关系表达，不能只引用日期而由模型自行选择方向。`boundary_date` 须与原文日期一致；年份缺省时只接受用户真实上下文中已明确且能唯一确定的年份，否则返回 `time_params_mismatch` 并提示补充年份。方向及是否包含当天有歧义时返回同一错误并提示澄清，不能仅凭模型传入 `relation` 放行；“之后”未说明是否包含当天且上下文也不能确定时同样澄清。
5. 以 `runtime.context.now`（固定时钟或实时）和 `timezone` 计算 `[start, end)`：day → +1 天；ISO 周（周一开始）→ +7 天；month → 下月 1 日；quarter → 下季度首日；year → 次年 1 月 1 日；`last_n_days` → `[today - (n-1), today + 1)`（含今天，共 n 天；如“最近 7 天”在 10-13 得 `[2026-10-07, 2026-10-14)`）；`explicit_range` → `[start_date, end_date_inclusive + 1 天)`。
   `open_range` 以边界日 D 在证据时区中的起点计算：`before` → `(None, D)`；`through` → `(None, D + 1 天)`；`from` → `(D, None)`；`after` → `(D + 1 天, None)`，`grain="range"`。这里的 None 只表示无界，不生成 SQL 的 NULL 或 infinity 字面量。
6. 有限区间必须两端非空且 `start < end`；`open_range` 必须恰有一端非空。否则返回 `time_params_mismatch`。不补数据最早日期、当前日期或其他猜测边界来凑成有限区间。

返回 `data`：`time_evidence_id`、`grain`、`start`、`end`（非空时为 `YYYY-MM-DD`；含时刻时为 `YYYY-MM-DD HH:MM:SS`）、`timezone`、`sql_hint`（有限区间如 `<时间列> >= '2026-09-01' AND <时间列> < '2026-10-01'`；单边区间只返回对应的 `>= start` 或 `< end`）。同时写入 `db_time_evidence`。

单边示例（用户已明确包含关系）：

| 原文 | `relation` / `boundary_date` | `start` / `end` | SQL 条件 |
| --- | --- | --- | --- |
| 2026 年 10 月 1 日之前，不含当天 | `before` / `2026-10-01` | `None` / `2026-10-01` | `d < '2026-10-01'` |
| 截至 2026 年 10 月 1 日，含当天 | `through` / `2026-10-01` | `None` / `2026-10-02` | `d < '2026-10-02'` |
| 从 2026 年 10 月 1 日起，含当天 | `from` / `2026-10-01` | `2026-10-01` / `None` | `d >= '2026-10-01'` |
| 2026 年 10 月 1 日之后，不含当天 | `after` / `2026-10-01` | `2026-10-02` / `None` | `d >= '2026-10-02'` |

在固定时钟 `2026-10-13 12:00:00+08:00` 下：R02“今天”得 `[2026-10-13, 2026-10-14)`；R03“本月”得 `[2026-10-01, 2026-11-01)`；F01“2026 年 9 月”得 `[2026-09-01, 2026-10-01)`；R01“当前订单状态分布”调用本工具会被拒绝。

### 7.6 `probe_dimension_values`

用途：受控数据库探针，在明确的候选列上确认真实维度值（概要设计 §8.1、R06）。

| 参数 | 说明 |
| --- | --- |
| `table_id` | 候选表 |
| `column` | 候选列 |
| `mode` | `exact`（等值确认）/ `contains`（包含匹配）/ `distinct`（列出低基数列的取值） |
| `value_text` | `exact`、`contains` 时必填，1–100 字符 |
| `limit` | 1–`results.probe_max_values`，默认 20 |

处理：

1. 用 `BusinessDbService` 结构查询确认表、列存在，列类型属于文本类（`text`、`varchar`、`char`、`citext`）或枚举；否则 `column_not_found`／`invalid_argument`。
2. `DimensionProbeBuilder.build(probe_spec)` 用 `psycopg.sql` 生成 SQL：标识符用 `sql.Identifier`，值用 `sql.Literal` 渲染为文本。三种模式的形态固定：

   | 模式 | 生成的 SQL 形态 | 过滤谓词 |
   | --- | --- | --- |
   | `exact` | `SELECT DISTINCT "c" AS value FROM "s"."t" WHERE "c" = '<值>' ORDER BY 1 LIMIT n+1` | 恰好一个等值谓词 |
   | `contains` | `SELECT DISTINCT "c" AS value FROM "s"."t" WHERE "c" ILIKE '%<转义值>%' ESCAPE '\' ORDER BY 1 LIMIT n+1` | 恰好一个 `ILIKE`；`%`、`_`、`\` 先转义 |
   | `distinct` | `SELECT DISTINCT "c" AS value FROM "s"."t" WHERE "c" IS NOT NULL ORDER BY 1 LIMIT n+1` | 零个取值谓词，只有 `IS NOT NULL` |

   其中 `n` 为参数 `limit`，SQL 中写入的是计算后的整数 `n+1`。

3. 通过 `SqlPipeline.validate_and_execute(sql, purpose="probe", probe_spec=...)` 完成三层校验、授权登记与只读执行（§8.4）；不直接使用连接执行。`run_readonly` 的 `max_rows=n`，它会读取 `n+1` 行：SQL 返回第 `n+1` 行时 `source_truncated=true`，即 `values_truncated=true`；最多保存、返回 `n` 个值。多出的那一行只用来判断截断，不写入证据。
4. 匹配到的值写入 `db_dim_evidence`（`source="db_probe"`）；`contains` 模式同时记录模式本身到 `patterns`；`distinct` 模式取值被截断时返回 `warnings`：“该列取值多于 limit，只有列出的值有证据”。0 行时 `status="empty"`，不写证据，`hint` 说明“该列无此值，不要放宽条件或替换为相似值”（F10）。

返回 `data`：`values[]`、`values_truncated`、`evidence_id`（执行记录）、`dim_evidence_id`、`sql`。

### 7.7 `validate_sql`

| 参数 | 说明 |
| --- | --- |
| `sql` | 待校验的 SQL，可包含结果占位符（§8.3） |

调用 `SqlPipeline.validate(sql, ctx, LedgerView(runtime.state), purpose="query")`（§8.2；`LedgerView` 是状态账本的只读包装）。成功返回 `data`：`layer="all_passed"`、`warnings`、`explain_plan`（截断到 3000 字符）、`placeholder_expansion`（若有：每个占位符展开的值个数）。失败返回 `error_type="validation_failed"`，`data.layer`、`data.errors`、`data.error_codes`。不论成功失败都按 §8.2 更新授权记录。

### 7.8 `execute_sql`

| 参数 | 说明 |
| --- | --- |
| `sql` | 要执行的 SQL，必须与某条有效授权记录的规范化文本逐字相同 |

调用 `SqlPipeline.execute(sql, ctx, LedgerView(runtime.state), purpose="query")`（§8.3）。成功返回 `data`：`evidence_id`、`result_ref`、`executed_sql`（仅当含占位符时返回，截断到 3000 字符）、`columns`、`saved_row_count`、`source_truncated`、`preview_rows`（前 `preview_rows` 行）、`preview_truncated`、`elapsed_ms`；`saved_row_count == 0` 时 `status="empty"`。工具说明中明确：“`source_truncated=true` 表示数据库结果超过保存上限，未保存部分无法通过翻页取得，不能把已保存行当作总体”。

### 7.9 `read_result`

| 参数 | 说明 |
| --- | --- |
| `result_ref` | 结果引用 |
| `columns` | 可选，列名子集 |
| `offset` | ≥ 0，默认 0 |
| `limit` | 1–`results.read_max_rows`，默认 50 |

调用 `ResultStore.read_rows`，会话检查按 `runtime.context.session_id`。返回 `rows`、`offset`、`next_offset`（无更多行时为 null）、`saved_row_count`、`preview_truncated`（本页之后仍有已保存行）、`source_truncated`（源结果截断标记，任何分页都原样保留）。

### 7.10 `analyze_result`

| 参数 | 说明 |
| --- | --- |
| `operation` | 操作名，见下表 |
| `inputs` | 1–2 个 `result_ref` |
| `params` | 操作参数，按操作名用 Pydantic 判别联合校验 |
| `allow_pushdown` | bool，默认 true |

| 操作 | 参数 | 本地计算条件 | 下推方式 |
| --- | --- | --- | --- |
| `aggregate` | `group_by[]`、`metrics[{column, func: sum/avg/min/max/count/count_distinct}]` | 输入未截断 | `SELECT <group_by>, <agg> FROM (<源SQL>) AS src GROUP BY <group_by>` |
| `top_n` | `order_by`、`descending`、`n`（≤ 100）、`ties: include/strict` | 输入未截断 | `SELECT * FROM (<源SQL>) AS src ORDER BY ... LIMIT n`；`include` 时用 `RANK()` 包装 |
| `share` | `value_column`、`group_by?`、`round_digits?` | 输入未截断 | 窗口函数 `x / NULLIF(SUM(x) OVER (), 0)` |
| `compare` | `a: {ref, column, row_index?}`、`b: {...}`、`round_digits?` | 两个输入都未截断且各取一个标量 | 不下推（标量需来自已完成的聚合结果） |
| `distinct_values` | `column`、`max_values`（≤ 1000） | 输入未截断 | `SELECT DISTINCT <col> FROM (<源SQL>) AS src LIMIT max+1` |
| `describe` | `columns[]` | 任何时候（结果明确标注基于已保存行） | 不下推 |

规则：

- 数值计算使用 `Decimal`；`compare` 返回 `diff = a - b`、`pct_change = (a - b) / b`，`b == 0` 时 `pct_change = null` 并给出 `warnings: ["分母为零，环比无定义"]`（F06）；舍入为 `ROUND_HALF_UP`，仅在传入 `round_digits` 时执行。
- 输入 `source_truncated=true` 且操作需要完整数据时：`allow_pushdown=true` 走下推；否则返回 `incomplete_source`。
- **数据源规则**：
  - 两个输入结果的 `meta.datasource_id` 不同时，返回 `result_datasource_mismatch`，不混合计算。
  - 本地计算不访问数据库，允许在来自其他数据源的结果上进行；输出结果的 `datasource_id` 取输入结果的值，不改写为当前值。
  - 下推会在当前数据源上重新执行源 SQL，所以输入结果的 `datasource_id` 必须等于当前 `datasource_id`，否则返回 `result_datasource_mismatch`，提示在当前数据源上重新查询。
- 下推 SQL 由服务生成（源 SQL 取自输入结果的 `meta.executed_sql`，即已展开占位符的实际执行文本，去掉末尾 `;` 后作为子查询；标识符用双引号转义），然后经 `SqlPipeline.validate_and_execute(sql, purpose="pushdown", source_metas=[...])` 走同一校验、授权与只读执行（P07），不直接连接数据库。守卫所需证据来自本运行账本与输入结果的 `guard_evidence` 快照（§9.6）。下推失败（校验失败、超时、只读违例）按错误返回，不改走本地计算。
- 输出保存为新的 `result_ref`（`derived_from` 记录输入），写入 `db_analyses`（下推时 `executed_sql` 为下推 SQL 全文，`pushdown_execution_id` 指向管线产生的执行记录），返回 `evidence_id`；本地计算的记录 `executed_sql=None`，不伪造校验记录。同一工具调用的全部账本更新（授权、执行、分析）在一个 `Command` 中返回。

#### 7.10.1 本地计算与下推的一致性规则（doc 7 折入，2026-10-15 评审通过）

**NULL／空集**（本地逐条镜像 PG）：`aggregate` 无分组键空输入输出 1 行（count=0、sum/avg/min/max=NULL）、有分组键 0 行；分组键 NULL 为独立组；指标跳过 NULL 且组内全 NULL 时 SUM/AVG/MIN/MAX 为 NULL 不填 0；`top_n` 复刻 PG 默认（ASC→NULLS LAST、DESC→NULLS FIRST）；`DISTINCT` 保留 NULL 一次；`share` 分母经 `NULLIF`，总和 0 或无有效值→全 NULL＋警告；`compare` 行不存在/越界→`invalid_argument`、单元格 NULL→空值＋警告、b=0→`pct_change=null`＋F06；`describe` 基于（单路径）已保存行，显式 null_count。

**类型白名单与 typname 归一化**（`meta.columns[].pg_type` 为 `pg_type.typname`）：整数/numeric 全能力精确；浮点判等排序可（`Decimal(str)` 保序保等）但**算术仅下推**；**文本族第一阶段整族仅下推**（判等也不放行——ColumnMeta 无法证明输出表达式有效 collation）；`bpchar` 仅下推（尾随空格判等语义）；`date/timestamp/timestamptz` 恢复类型后判等排序；`time/timetz` 第一阶段不支持本地；`citext/bytea/json/数组/uuid/枚举` 仅下推；列含 NaN/Infinity → 仅下推（compare/describe 内按单路径规则省略统计并警告）。`describe` 均值仅数值白名单列（求和精确，均值按除法精度规则）。

**精度**：求和精度按指数跨度动态上界＝最大整数位数＋最大小数位数＋ceil(log10(n))＋2（覆盖 `1E+28+1E-28`）；除法/量化显式局部精度＝操作数位数上界＋目标小数位＋2；仅显式 `round_digits` 时量化（`ROUND_HALF_UP` 与 PG 一致）；无除法的精确类型操作两路径精确相等，除法结果无 `round_digits` 时按 10 位小数量化容忍比较；`aggregate` 不新增 `round_digits` 参数。

**下推 SQL 形态**：统一**聚合前转换** `SUM(x::numeric)`／`AVG(x::numeric)`；`top_n` 次排序键只含可比较列（全列可比较才加，否则承认边界并列不确定并返回 warning；`include` 用 `RANK() OVER (ORDER BY k)` ≤ n，rank 别名冲突时递增后缀，外层显式投影原始列）；`distinct_values` `ORDER BY 1 LIMIT max+1` 判截断剔除多读行、一致性按值集合断言。

**下推重查非原快照**：`AnalysisRecord.result_generated_at`＝输出 `ResultMeta.created_at`；返回固定 warning"结果由源 SQL 重新执行获得，非原结果快照"；严格原时点场景用 `allow_pushdown=false`，源截断返回 `incomplete_source`，重查不得冒充原时点答案；一致性测试用冻结数据。

**路由决策**（三步）：第 0 步统一数据源检查——两输入异源→`result_datasource_mismatch`（最高优先，本地也查）；下推另要求同当前源（同码）。第 1 步单路径操作——`describe` 任何时候本地（截断不下推；类型不支持仅省略 min/max/均值）；`compare` 不下推、截断→`incomplete_source`、非数值列/NaN→`invalid_argument`。第 2 步通用表（aggregate/top_n/share/distinct_values）：本地支持+完整→本地；截断+可下推（allow_pushdown+源 SQL+同源）→下推；截断+不可下推→`incomplete_source`；本地不支持+可下推→下推；本地不支持+不可下推→`invalid_argument` 明确报因；下推失败按错误返回不回退本地。错误优先级：数据源→参数→截断→下推执行。

### 7.11 `submit_final_answer`

用途：提交本运行的最终答案。只约束交付格式；何时提交、之前调用哪些工具仍由 Agent 自主决定。

| 参数 | 说明 |
| --- | --- |
| （`args_schema = SubmitFinalAnswerArgs`） | 模型可见字段与 `DbAgentFinalAnswer` 相同：`status`、`answer`、`findings[]`（每条带 `evidence_ids`）、`metric_definitions`、`limitations`、`open_questions`，见 §14.1；内部额外声明隐藏的 runtime，按 §7.1 注入 |

工具设置 `return_direct=False`：K19 表明 `return_direct` 不看提交是否被拒，被拒时也会结束运行；所以结束运行交给 `FinalAnswerGateMiddleware`（§11.3）判断。

检查顺序（全部通过才登记）：

1. **单独调用**：读取 `runtime.state["messages"]` 中最后一条 `AIMessage`（即发出本调用的消息，K20），其 `tool_calls` 中还有别的工具时返回 `final_answer_not_alone`，不登记。同一批中的其他工具照常执行，结果由模型在下一轮看到后再单独提交。
2. **未重复提交**：`db_final_answers` 中已有当前 `run_id` 的记录时返回 `final_answer_already_submitted`。
3. **证据引用**：`findings[*].evidence_ids` 每一项都必须是本运行 `db_executions`（`purpose="query"`，含 `reuse_evidence` 沿用登记）或 `db_analyses`（含沿用登记）中的 `evidence_id`；不存在的引用逐项列出，返回 `invalid_evidence_reference`，模型修正后重新提交。探针与下推执行记录（仅作轨迹）不能作为引用。
4. **状态与证据一致**：`status="completed"` 时 `findings` 不能为空；`status` 为 `needs_clarification` 或 `no_data` 时允许没有 `findings`。

通过后返回 `Command(update={"db_final_answers": {run_id: FinalAnswerRecord(...)}, "messages": [ToolMessage("已接收最终答案", ...)]})`。记录按 `run_id` 作键，Runner 只读当前 `run_id` 的答案，不会读到上一轮的答案。Runner 仍会再做一次证据核对（§14.3），作为后备。

### 7.12 `reuse_evidence`

用途：把已确认的时间／维度证据登记到本运行，用于沿用前文口径，尤其是线程轮换后新线程原文不含原时间表达的情况（§13.4）。

| 参数 | 说明 |
| --- | --- |
| `evidence_ids` | 1–20 个 `time_evidence_id` 或 `dim_evidence_id` |

可沿用的来源只有两类，都由服务端持有：

1. 当前线程账本中早于本运行的 `db_time_evidence`／`db_dim_evidence`（`run_id` 不等于当前运行）。
2. `runtime.context.carried_evidence`（轮换时携带的证据）。

处理：

- 每个 ID 在上述来源中查找；找不到返回 `evidence_not_found`（列出 ID），不登记任何一条。`prior_result` 证据不可沿用（返回 `invalid_argument`，提示重新使用占位符）。
- **数据源检查**：
  - 维度证据：记录的 `datasource_id` 必须等于 `runtime.context.datasource_id`，否则整批返回 `evidence_datasource_mismatch`（列出 ID 与原数据源），提示用 `search_schema`／`probe_dimension_values` 在当前数据源上重新取得证据。
  - 时间证据：允许跨数据源沿用。它由用户原文、时钟和时区计算得出，不依赖数据库内容。复制时保留原 `datasource_id` 与 `inherited_from.datasource_id`，不改写为当前值。跨数据源沿用仍须通过本工具显式登记，只能使用上述服务端持有且属于当前会话访问范围的来源，不扩大跨会话访问权限。
- 找到的记录复制为本运行的新证据：新 ID、`run_id` 为当前运行，`inherited_from` 记录原始 `EvidenceOrigin`。时间证据保留原 `start`／`end`／`grain`／`timezone`／`source_text`，**不按当前时钟重新计算，也不替换为当前配置的时区**；维度证据保留原表、列、取值与模式。
- 单边时间证据按原样保留空边界与方向，不填补缺失端点。结果的 `guard_evidence` 快照同样保存可空边界，后续下推按 §9.4 检查。
- 写入 `db_time_evidence`／`db_dim_evidence`，返回新旧 ID 对照及每条的区间或取值，提示在回答中写明沿用的口径。

时间证据只证明所采用的区间与时区，不证明它应绑定到新数据源的哪个时间字段；Agent 仍须通过 `inspect_tables` 和业务口径确认时间字段。例如旧证据中的“今年”已解析为 `[2026-01-01, 2027-01-01)`，即使在 2027 年沿用，仍使用该原始区间；用户明确要求重新计算“今年”时，应调用 `resolve_time_range` 取得新证据。

模型只能提交 ID，不能提交证据内容，因此不能借此伪造证据。`resolve_time_range` 返回 `time_text_not_in_question` 时，`hint` 中提示：如需沿用前文或交接清单中已确认的时间口径，使用 `reuse_evidence`。

#### 7.12.1 执行／分析证据沿用（历史结果作为最终答案引用）

`evidence_ids` 额外接受两类 ID：`E-`（执行记录，仅 `purpose="query"`）与 `A-`（分析记录）；同批输入先按 ID 去重。可沿用来源同上两类：本线程账本（`run_id` 不等于当前运行）与 `carried_evidence`（`kind` 扩为 `time｜dim｜execution｜analysis`）。沿用只支持**引用型**（引用历史结果说明结论、口径与截断情况）；重新计算（如“再按 X 分组”）须走 `analyze_result` 或新查询，沿用本身不产生新数据；允许引用截断结果，但不代表可据已保存样本推断完整总体（§12.1 第 7 点仍适用）。

**链首解析（不回查原线程）**：沿用源本身可能是沿用登记。“解析链首”只是身份解析——沿 `reused_from` 链取最初的原始 `evidence_id` 与 `EvidenceOrigin`，不要求原线程 checkpoint 或原账本仍存在；登记使用**当前服务端持有的完整记录副本**（本线程账本记录或 `carried_evidence` 副本）＋该副本已有的 `reused_from` ＋原结果元数据（`ResultMeta`）完成。`EvidenceOrigin.evidence_id` 取链首解析结果（`ResultMeta` 无此字段）；thread_id／request_id／run_id／datasource_id 以 `ResultMeta` 为准。连续沿用保留链首 `reused_from`，不改写为上一运行的登记。

**检查顺序**（在前述 `evidence_not_found`／`invalid_argument` 之后，任一失败整批拒绝）：

1. **可读性**：`result_ref` 目录不存在 → `result_not_found`；会话不符 → `result_access_denied`；`rows_sha256` 校验失败 → `result_integrity_error`。分析记录只核验 `output_result_ref`（`input_result_refs` 允许已不可读——分析在原运行已经发生）。
2. **复用保留期（显式，不依赖 load_meta）**：以存储生命周期注入时钟（禁用 `ctx.now`／`--fixed-now`）统一转 UTC 计算 `age = now − created_at`，`age >= timedelta(days=thread.result_retention_days)` 即过期 → `result_expired`（重新查询）。沿用登记不续期（新记录引用原 `result_ref`，后续沿用按同一原结果生成时间判定）。
3. **数据源（以元数据为准）**：`ResultMeta.datasource_id` 必须等于当前 `datasource_id`，否则 `evidence_datasource_mismatch`。执行／分析证据不允许跨数据源（数据事实来自原库）；执行记录账本副本与元数据不一致同样拒绝；`AnalysisRecord` 无 `datasource_id` 字段，一律以 `output_result_ref` 元数据为准。

**登记形态**：登记为本运行新记录（事实字段照抄当前副本，不改写）：执行 → `db_executions` 追加新 `ExecutionRecord`（新 ID、当前 `tool_call_id`／`request_id`／`run_id`，`purpose` 保持 `"query"`，`reused_from`=链首来源、`result_generated_at`=原 `ResultMeta.created_at`）；分析 → `db_analyses` 同构。新 ID 确定性：`"E-"+sha256(run_id+":"+链首原始 evidence_id)[:8]`（分析 `"A-"` 同构）——同一运行重复沿用同一原始证据返回同一登记 ID。`source_truncated` 的历史结果允许沿用，截断标记随记录带入 `EvidenceItem`，模型须在 `limitations` 说明。

**幂等（账本合并层）**：`db_executions`／`db_analyses` 的 reducer 按 `evidence_id` 去重追加，**两类记录分别比较事实字段**——执行记录：`reused_from`、`result_ref`、`executed_sql_sha256`、`saved_row_count`、`source_truncated`、`columns`、`result_generated_at`；分析记录：`reused_from`、`output_result_ref`、`operation`、`input_result_refs`、`executed_sql`（全文）、`saved_row_count`、`source_truncated`、`result_generated_at`；都排除 `tool_call_id`（保留首登 ID，后续调用轨迹由工具调用审计记录）。同 ID 同事实 → 去重；**同 ID 异事实 → reducer 抛一致性错误**，不静默保留首条。

**返回**：新旧 ID 对照（`kind` 含 `execution`／`analysis`）、`result_ref`／行数／截断标记／原结果生成时间；提示沿用须在回答中写明原结果生成时间与数据源。

**统计与归档解耦**：本轮有效回答证据允许含受控沿用（`submit_final_answer` 引用、`EvidenceItem`、状态判定的“有成功证据”均认沿用记录）；**本轮实际执行次数只统计真实执行**（`reused_from` 为空）——沿用不调 `run_readonly`、不占 `db_execute` 预算，任何执行耗时汇总不得计入沿用记录的 `elapsed_ms`（历史事实转录）。

## 8. SQL 校验与执行管线（`services/sql_pipeline.py`）

### 8.1 规范化

```python
_ASCII_WS = " \t\r\n"
def normalize_sql(sql: str) -> str:
    return sql.strip(_ASCII_WS)
```

只去掉首尾 ASCII 空格、制表符、回车、换行；保留内部全部字符、注释、大小写、引号、末尾分号；不做格式化或等价推断（概要设计 §6.2、P03）。校验与执行使用同一函数。

### 8.2 校验流程

管线分为不读写图状态的核心函数和返回账本更新的外层函数。核心函数只接收显式传入的只读视图（授权记录字典、证据列表），不修改框架提供的 `runtime.state`；外层函数把账本更新收集成一个 `LedgerUpdate`，由工具一次性放进 `Command`。

```text
validate(sql, ctx, ledger: LedgerView, purpose, probe_spec=None, source_metas=()) -> (ValidationOutcome, ValidationRecord | None, LedgerUpdate)
 1. text = normalize_sql(sql)；为空 → invalid_argument
 2. expanded, refs, ph_evidence = expand_placeholders(text, ctx)   # §8.3，失败 → placeholder_error
 3. outcome = DbSqlValidator.validate(expanded)                     # 语法 / 安全 / EXPLAIN
    outcome.valid 时再做输出列名静态检查：最外层 SELECT 的投影名（别名或列名）重复 → 失败（layer="output_columns"，
    error_codes=["duplicate_column_names"]）；含 * 或 t.* 时无法静态确定，留给执行期检查（§6.1）
 4. 若 outcome.valid 且 guard.enabled：
        evidence = ledger.evidence_of_run(ctx.run_id) ∪ ph_evidence ∪ snapshots(source_metas)
        g = SqlEvidenceGuard.check(expanded, evidence, purpose, probe_spec)
        g 失败 → outcome = 失败(layer="guard", errors=g.errors, error_codes=g.codes)
 5. key = auth_key(ctx, text)
 6. 成功：record = ValidationRecord(status="valid", guard_evidence=g.used_evidence, ...)
          # g.used_evidence 是守卫实际采用的证据完整内容，含 ph_evidence 与 source_metas 快照中的条目
          update.db_validations[key] = record
    失败：record = None；若 ledger.validations[key] 存在且为 valid → update.db_validations[key] = revoked
 7. 返回 outcome、record、update
```

第 6 步的撤销只作用于同一作用域、同一文本的记录（概要设计 §6.2“同一作用域内重新校验失败时，撤销该文本先前的成功记录”，P06）。`validate_sql` 工具把 `update` 与 `ToolMessage` 放进同一个 `Command` 返回。

### 8.3 执行流程与结果占位符

```text
execute(sql, ctx, ledger: LedgerView, purpose, *, trusted_record: ValidationRecord | None = None) -> (ExecOutcome, LedgerUpdate)
 1. text = normalize_sql(sql)
 2. rec = trusted_record（仅 §8.4 内部传入） 或 ledger.validations.get(auth_key(ctx, text))
    rec 不存在、status != "valid"、或 rec.key != auth_key(ctx, text) → not_validated（不执行任何数据库语句）
 3. expanded, refs, _ = expand_placeholders(text, ctx)
    sha256(expanded) != rec.expanded_sql_sha256 → placeholder_error（源结果被替换或损坏）
 4. 预算预占：RunBudgetLedger.reserve(ctx.run_id, "db_execute")，超限 → budget_exhausted（§11.1）
 5. r = BusinessDbService.run_readonly(expanded, timeout=min(配置, 剩余时限), max_rows)
 6. meta = ResultStore.save(sql_template=text, executed_sql=expanded, guard_evidence=rec.guard_evidence, ...)
 7. update.db_executions += ExecutionRecord(sql_template=text, executed_sql=expanded, ...)
 8. 返回预览与 update
```

`trusted_record` 不是模型可传的参数，`execute_sql` 工具调用时它恒为 `None`，只能从图状态中的账本取授权。

执行端不接收 `source_metas`，也不重新查找证据来源：结果快照直接取授权记录中的 `guard_evidence`。授权记录在校验时就保存了守卫实际采用的证据完整内容，包括临时的占位符证据与下推输入快照中的条目，所以执行成功后保存快照不会失败。第 3 步重新展开占位符只为得到实际执行文本并核对摘要，展开得到的证据不再使用。

执行端不重复三层校验与守卫（概要设计 §6.2），但只读事务、`statement_timeout`、读取上限每次都生效。模型参数中的 `valid=true` 之类字段不存在于 Schema，即使模型在 SQL 外附带也会被参数校验拒绝（P04）。

**结果占位符**：落实概要设计 §6.3“较大编号列表应由程序提取和校验”（F08）。

- 语法：`{{result:<result_ref>:<column>}}`，正则 `\{\{result:(res_[0-9a-f]{32}):([A-Za-z_][A-Za-z0-9_]*)\}\}`。文本中出现 `{{` 却不能完整匹配该正则时，返回 `placeholder_error`（`placeholder_malformed`）。例：`WHERE c.customer_id IN {{result:res_3f...:customer_id}}`。
- **位置限定**：占位符只能作为 `IN`／`NOT IN` 的整个值集合，不能出现在字符串、注释、SELECT 表达式或其他任意位置。检查方法：
  1. 原文中已有 `__dbda_ph_` 字样时拒绝（避免与哨兵冲突）。
  2. 把第 i 个占位符替换为哨兵 `(__dbda_ph_i__)`，用 `sqlglot` 解析。
  3. 每个哨兵必须恰好对应一个 `exp.Column` 节点；该节点的父节点是 `exp.In`，且是 `expressions` 中的唯一元素；`exp.In` 的左侧是普通列（可带表别名）。否则返回 `placeholder_error`（`placeholder_position_invalid`）。占位符位于字符串或注释中时，哨兵不会成为列节点，因此会被拒绝。
  4. 位置全部合法后，才对原文做文本替换。每个替换点都已确认在 `IN` 值集合中，替换不会改变其他文本。
- 展开：读取该结果该列的全部已保存值，去重并保持首次出现顺序，按列的 PG 类型渲染为 SQL 字面量列表 `('C001', 'C007', 'C019')`（文本用 `psycopg.sql.Literal`，数值原样，日期加类型转换）。
- 约束：结果必须属于当前会话；结果的 `meta.datasource_id` 必须等于当前 `datasource_id`，否则返回 `placeholder_error`（`placeholder_datasource_mismatch`），提示在当前数据源上重新查询前序结果；结果 `source_truncated=true` 时拒绝（不能用不完整集合限定范围）；值个数为 0 时拒绝并提示“前序结果为空”；超过 `placeholder_max_values` 时拒绝并提示改用子查询或单条 SQL。
- 证据：展开的值自动生成 `prior_result` 维度证据（`ph_evidence`）参与守卫（§9.5），表与列绑定为 `IN` 左侧列经 §9.2 解析出的归属；归属无法确认时同样返回 `column_owner_unresolved`。
- 授权匹配仍以含占位符的规范化原文逐字比较；展开结果用摘要绑定，防止源结果被替换。执行记录与结果元数据同时保存 `sql_template`（原文）和 `executed_sql`（展开后全文）。

### 8.4 服务生成 SQL 的统一入口

```python
def validate_and_execute(self, sql: str, ctx, ledger: LedgerView, *,
                         purpose: Literal["probe", "pushdown"],
                         probe_spec: ProbeSpec | None = None,
                         source_metas: Sequence[ResultMeta] = ()) -> tuple[PipelineOutcome, LedgerUpdate]:
    outcome, record, update = self.validate(sql, ctx, ledger, purpose, probe_spec, source_metas)
    if record is None:
        return PipelineOutcome.failed(outcome), update
    exec_outcome, exec_update = self.execute(sql, ctx, ledger, purpose, trusted_record=record)
    return PipelineOutcome.from_exec(outcome, exec_outcome), update.merge(exec_update)
```

工具在一次调用内先校验后执行。校验返回的 `Command(update=...)` 要等工具返回后才由图合并（K20），执行端从状态读取授权会看不到刚生成的记录。所以本方法直接把校验刚产生的可信记录 `record` 传给执行端，并把两次更新合并，最后由工具一次性返回：`db_validations[key]` 与 `db_executions` 追加项在同一个 `Command` 中（下推时再加 `db_analyses`）。全程不修改 `runtime.state`。

探针与下推调用该方法，产生的 `ValidationRecord` 与 `ExecutionRecord` 和普通查询一样写入状态（`purpose` 区分），保证“所有面向业务数据的 SQL 走同一管线”（概要设计 §6.1、P07）。该方法不对模型开放。

### 8.5 并发与顺序

LangChain Agent 会并行执行同一条模型消息中的多个工具调用，工具读取的是该步开始时的状态（K20）。若模型在同一消息中同时调用 `validate_sql(A)` 与 `execute_sql(A)`，执行端看不到同一步产生的授权记录，返回 `not_validated`；这是安全的失败方向。系统提示要求校验、执行和有依赖的查询分步串行调用（概要设计 §6.3）。

- 账本字段的 reducer（追加或按键合并）保证并行写入不丢失**记录**，但不能解决**计数**竞争：并行工具读到的是同一个旧计数。预算计数因此不放在状态里判定，而由进程内加锁的 `RunBudgetLedger` 预占（§11.1），状态只记录结果。
- 同一步中对同一文本重复调用 `validate_sql` 时，两条更新的键相同，以图合并后的记录为准，影响范围只限该文本；不依赖这种情况下的先后顺序。

## 9. 时间／维度证据守卫（`services/sql_guard.py`）

落实概要设计 §8.1 第 3 层。使用 `sqlglot.parse_one(sql, read="postgres")`；解析失败返回 `guard_unparseable`（可重试，提示简化写法），不放行。

基本原则：AST 解析成功不等于守卫完整。守卫无法确定含义的形态（列归属不明、谓词形态不受支持、条件过于复杂）一律返回可修复的错误，由 Agent 改写；不放行，也不退回到更宽松的匹配。下文所有错误都以 `validation_failed`（`layer="guard"`）返回，`error_codes` 为规则码，`retryable=true`。

### 9.1 作用域与检查位置

**作用域**：用 `sqlglot.optimizer.scope.traverse_scope` 把语句拆成查询作用域：主查询、每个 CTE、派生表、标量／`IN`／`EXISTS` 子查询、`UNION` 的各分支。列归属（§9.2）与条件分组（§9.3）都在单个作用域内进行，不跨作用域合并。

**检查位置**（条件表达式）：

| 位置 | 说明 |
| --- | --- |
| `WHERE` | 作用域的行过滤 |
| `HAVING` | 分组后过滤 |
| `JOIN ... ON` | 连接条件 |
| `CASE WHEN <条件>` | 出现在 SELECT、HAVING、ORDER BY 等任意位置的 `WHEN` 条件；它决定行进入哪个统计口径 |
| 聚合 `FILTER (WHERE <条件>)` | 决定行是否进入该聚合 |

在 sqlglot AST 中，`CASE WHEN` 的分支是 `exp.Case` 下的 `exp.If` 节点（条件在 `this`），聚合过滤是 `exp.Filter` 节点。

非条件位置（SELECT 表达式的值、`CASE` 的 `THEN`／`ELSE` 结果、`GROUP BY`、`ORDER BY` 表达式、表函数参数）的字面量**本身**不检查；但只要它经 CTE、派生表、子查询或表函数流入某个条件的比较，就按 §9.2 的来源追溯处理，视同写在条件中。只作输出、不参与任何条件的字面量（如显示标签）不受限制。

**谓词识别**：`EQ`、`NEQ`、`GT`、`GTE`、`LT`、`LTE`、`Between`、`In`、`Like`、`ILike`、`SimilarTo`、`RegexpLike`／`RegexpILike`（`~`、`~*`）。

**字面量分类**：

- **时间字面量**：字符串匹配 `^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?$`，或 `DATE '...'`、`TIMESTAMP '...'`、`'...'::date` 等带时间类型的字面量。
- **维度字面量**：其他字符串字面量；空字符串仅在 §9.5 明确允许的空值检查中豁免证据，不全局豁免。
- 数值、布尔、`NULL`、区间字面量（`INTERVAL '1 day'`，sqlglot 的 `exp.Interval`）不检查；区间字面量内部的字符串不算维度字面量。

### 9.2 谓词形态与列归属

**受支持的形态**：含时间或维度字面量的谓词，一侧必须是普通列（可带表别名，可带 `CAST`／`::` 类型转换），另一侧是字面量或字面量列表。列被函数或表达式包裹时（如 `date_trunc('month', d) = '2026-09-01'`、`lower(name) = 'abc'`、`COALESCE(c, 'x') = 'y'`）返回 `unsupported_predicate_form`，提示改为“列 与 字面量”直接比较；有限时间条件改为 `>= start AND < end`，已登记的单边区间仅使用对应非空端的条件。

**列归属**：每个此类谓词的列必须解析为唯一的 `(schema.table, column)`：

1. 列带限定名：在当前作用域的 `sources` 中查找该别名。
   - 来源是基表：归属即该表。
   - 来源是 CTE 或派生表：若对应投影是同名列的直接透传（`exp.Column`，可带别名），递归追到基表；投影是表达式时无法确认归属。
2. 列不带限定名：作用域只有一个来源时归属该来源（再按第 1 条处理）；有多个来源时，用 `BusinessDbService.get_columns` 的实时列清单（每次运行缓存）找出含该列名的来源，恰好一个时归属该来源，否则无法确认。
3. 无法确认时返回 `column_owner_unresolved`，提示“为列加上表别名，并把过滤写在基表所在的查询层”。**不退回“只按列名匹配”**，避免误用另一张表同名列的证据。

**来源追溯**（列与列比较、`IN (子查询)`、标量子查询比较）：条件两侧都是列，或一侧是子查询时，对非基表一侧的值来源逐层追溯，防止通过常量关系绕过证据。例如：

```sql
WITH f AS (SELECT '系统'::text AS v)
SELECT COUNT(*) FROM sales.orders o JOIN f ON o.status = f.v;
```

| 追溯到的来源 | 处理 |
| --- | --- |
| 基表列 | 普通列与列比较，数据驱动，不要求证据 |
| CTE／派生表／子查询中的直接透传列 | 继续向内追溯 |
| 字面量投影（可带类型转换），如上例的 `'系统'::text` | 视同该字面量直接写在条件中：上例等价于 `o.status = '系统'`，按 §9.4／§9.5 检查（上例无证据，命中 `unsupported_dimension_value`） |
| 含字符串或时间字面量的表达式投影（拼接、`CASE`、函数调用等） | `constant_source_unsupported` |
| 不含字符串／时间字面量的表达式投影（聚合、算术等） | 数据派生值，不要求证据 |
| `UNION`／`UNION ALL` | 逐分支追溯，任一分支按上述规则不通过即拒绝 |
| `VALUES (...)`、`unnest(ARRAY[...])`、以字符串或时间字面量为参数的其他表函数 | `constant_source_unsupported` |
| `generate_series` 时间序列 | 只放行 §9.8 的受控形态，否则 `constant_source_unsupported` |

追溯在作用域之间进行（从条件所在作用域进入被引用的 CTE、派生表或子查询），深度上限 8 层；超过时返回 `guard_too_complex`。

实现注意（sqlglot 30.22.0 实测）：`scope.sources` 中，基表与 `generate_series(...) AS d(day)`、`unnest(...)` 这类表函数**都表现为 `exp.Table` 节点**，CTE／派生表则是子作用域。必须检查 `exp.Table` 的 `this` 是否为函数调用来区分，不能把所有 `exp.Table` 当作基表；否则表函数会按“基表列”放行。

### 9.3 条件分组

时间区间与维度值都按“合取组”判断。一个合取组是一组必须同时成立的谓词。

1. **布尔分支**：对条件表达式做分支展开。`AND` 合并当前组；`OR` 的每个分支各自形成新组，并继承外层已有的合取项（`A AND (B OR C)` 得到 `{A, B}` 与 `{A, C}` 两组）。**不同 `OR` 分支的谓词绝不放进同一组**。展开后组数超过 64 时返回 `guard_too_complex`，提示拆分查询。
2. **`NOT`**：时间谓词位于 `NOT` 之下时返回 `time_predicate_under_not`（取反后区间含义改变，要求直接写成正向半开区间）。维度谓词在 `NOT` 之下仍按 §9.5 检查取值证据（取值必须真实存在，与正反向无关）。
3. **上下文合并**：`CASE WHEN` 条件与 `FILTER (WHERE ...)` 条件作用在已被 `WHERE` 过滤过的行上，因此它们的每个组再并入同一作用域 `WHERE` 的**必然项**（不在任何 `OR`／`NOT` 之下的顶层合取项）。`WHERE`、`HAVING`、`ON` 各自独立分组，相互不合并。

### 9.4 时间规则

时间证据集合 E = 本运行 `db_time_evidence` ∪ 下推输入结果的 `guard_evidence` 快照（§9.6）。有限证据为 `[start, end)`；单边证据中 `start=None` 表示无下界，`end=None` 表示无上界。空边界不参加日期字面量匹配与日期比较，不视为一条 SQL NULL 条件。

| 规则码 | 条件 | 处理 |
| --- | --- | --- |
| `realtime_clock_in_filter` | 任一检查位置中出现 `CURRENT_DATE`、`CURRENT_TIMESTAMP`、`CURRENT_TIME`、`LOCALTIMESTAMP`、`LOCALTIME`、`now()`、`clock_timestamp()`、`statement_timestamp()`、`transaction_timestamp()` | 拒绝：时间边界必须来自 `resolve_time_range`，保证与固定时钟和用户口径一致 |
| `unsupported_time_literal` | 时间字面量不等于 E 中任一证据的非空 `start` 或 `end` | 拒绝：先调用 `resolve_time_range` |
| `time_operator_not_half_open` | 时间字面量与 `>`、`<=`、`=`、`<>`、`IN`、`BETWEEN` 一起使用 | 拒绝：只用 `列 >= start` 与 `列 < end` |
| `time_lower_bound_mismatch` | `列 >= L`，但 E 中没有任何证据的 `start == L` | 拒绝 |
| `time_upper_bound_mismatch` | `列 < U`，但 E 中没有任何证据的 `end == U` | 拒绝 |
| `empty_time_range` | 同一合取组内、同一归属列同时存在下界和上界，且有效区间 `[max(L), min(U))` 满足 `max(L) >= min(U)` | 拒绝（防止备忘录问题 1 的 `>= 2026-10-13 AND < 2026-10-13`） |
| `time_range_not_evidenced` | 有效区间只有上界 U，但 E 中没有 `(None, U)`；只有下界 L，但没有 `(L, None)`；两端都有时，不等于 E 中一条有限证据，也不等于若干有限证据首尾相接的并集 | 拒绝：为实际所需范围登记匹配证据；有限证据不能仅取一端放宽范围 |
| `time_predicate_under_not` | 见 §9.3 | 拒绝 |

边界字面量按它在谓词中的角色判断，不按字面量本身判断。例如 `2026-09-01` 在已登记八月 `[08-01, 09-01)` 与九月 `[09-01, 10-01)` 时，既可以作为 `< '2026-09-01'`（八月的上界），也可以作为 `>= '2026-09-01'`（九月的下界）；只登记了八月时，`>= '2026-09-01'` 会被 `time_lower_bound_mismatch` 拒绝。

单边放行仍按 §9.3 的同列、同作用域合取组计算有效区间，再匹配方向与边界；不同 OR 分支不能拼接。第一阶段不推导单边证据之间的并集或交集授权：若另加条件形成有限区间，须为该有限区间登记证据，不把“有两个端点记录”直接视为已确认的有限口径。

示例（已登记八月、九月证据）：

- `WHERE (d >= '2026-08-01' AND d < '2026-09-01') OR (d >= '2026-09-01' AND d < '2026-10-01')`：两个 `OR` 分支各成一组，各自匹配一条证据，放行；不会把两个分支的边界合并成空区间。
- `WHERE d >= '2026-08-01' AND d < '2026-10-01'`：有效区间等于八月与九月首尾相接的并集，放行。
- 在上一条的基础上 `SUM(amount) FILTER (WHERE d < '2026-09-01')`：FILTER 组并入 WHERE 必然项，有效区间 `[08-01, 09-01)` 等于八月证据，放行。
- `CASE WHEN d >= '2026-09-01' THEN ...`，而 WHERE 中没有上界、只有上述有限月份证据：命中 `time_range_not_evidenced`。若另登记 `(2026-09-01, None)` 则该单边区间可放行。
- 只登记 `(None, 2026-10-01)`：`d < '2026-10-01'` 放行；反向 `d >= '2026-10-01'` 拒绝；擅自增加下界形成有限区间时，须补充对应有限证据。

R01：Agent 若为“当前订单状态分布”添加今天过滤，`resolve_time_range` 拒绝裸“当前”，没有时间证据，SQL 中的日期字面量命中 `unsupported_time_literal`，用 `CURRENT_DATE` 则命中 `realtime_clock_in_filter`。

### 9.5 维度规则

维度证据集合 D = 本运行 `db_dim_evidence` ∪ 占位符生成的 `prior_result` 证据 ∪ 下推输入结果的 `guard_evidence` 快照；只有 `datasource_id` 等于当前数据源的维度证据才进入 D（来源检查见 §7.12、§8.3、§9.6）。证据的表统一转换为两段式 `schema.table`（`milvus_dim_hit` 的三段 `dim_table` 经 `to_display_name` 转换）。

| 规则码 | 条件 | 处理 |
| --- | --- | --- |
| `unsupported_dimension_value` | 除下述空值检查例外外，任一检查位置中，`列 = '值'`、`列 <> '值'`、`列 IN ('a','b')`、`列 NOT IN (...)` 的任一值，在 D 中找不到“归属表与列都相同且 `values` 包含该值”的记录 | 拒绝：用 `search_schema(kinds=["dim_values"])` 或 `probe_dimension_values` 取得证据，或向用户澄清 |
| `unsupported_pattern_filter` | `LIKE`／`ILIKE`／正则匹配的模式，不在归属表与列都相同的 `db_probe` 证据的 `patterns` 中 | 拒绝（防止 `LIKE '%系统%'`，R04） |

**空值检查例外**：只为以下明确谓词免除维度值证据，不免除结构核对、列归属、三层校验与只读检查：

| 形态 | 条件与语义 |
| --- | --- |
| `列 = ''` | 归属列的实时类型确认为 `text`、`varchar` 或 `citext`，用于找空字符串 |
| `列 <> ''`／`列 != ''` | 同上，用于排除空字符串；SQL 的 NULL 语义仍生效，NULL 行也不会通过该条件 |
| `列 IS NULL`／`列 IS NOT NULL` | 列存在且归属明确；不要求维度值证据，不限文本类型 |

比较两侧顺序互换时按等价的 EQ／NEQ AST 处理，直接字面量来源的追溯仍适用 §9.2。类型无法确认时不放行空字符串例外；其他类型（包括定长字符列）不自动套用此例外。`' '` 不是 `''`，`IN ('')`、`LIKE ''`、`TRIM(列) <> ''`、`COALESCE(列, '') ...` 等不因出现空字符串而获得豁免，仍按原有证据和谓词形态规则检查。探针的固定形态也不因本例外而放宽。

NULL、空字符串、仅含空白的字符串须区分；`IS NOT NULL` 不能代替 `<> ''`。当用户要求同时排除 NULL 与空字符串时可写 `列 IS NOT NULL AND 列 <> ''`。不生成维度证据来声称空字符串实际存在；存在与否由最终查询结果体现。

`CASE WHEN o.status = '...'` 与 `COUNT(*) FILTER (WHERE o.channel = '...')` 中的取值同样要求证据（上述空值检查例外除外），因为它们决定统计口径。值比较为逐字比较（不做大小写或全半角折叠），确保 SQL 使用的就是证据中的真实值。对应关系：

- R04：“系统”在维度值检索中无命中，没有证据，`value='系统'` 或 `LIKE '%系统%'` 均被拒绝。
- R05：“业务系统”命中且列绑定明确，`milvus_dim_hit` 证据放行。
- R06：向量未命中，`probe_dimension_values(mode="exact" 或 "contains")` 在候选列上确认真实值后放行；探针 SQL 自身经 §8.4 管线执行。
- F07／F10：真实客户值有证据放行；不存在的值没有证据，Agent 只能如实说明或澄清。
- F02／F04 中的状态口径值（如支付状态）通过 `probe_dimension_values(mode="distinct")` 取得证据。
- F08：通过结果占位符展开的客户编号自动带 `prior_result` 证据。

### 9.6 探针与下推的守卫策略

**探针**（`purpose="probe"`）：探针本身是维度证据的来源，不按 §9.5 要求取值证据，改为严格的形态检查：

1. 用 `DimensionProbeBuilder.build(probe_spec)` 重新生成 SQL，必须与传入文本逐字相同。
2. AST 形态按模式检查（§7.6 表）：单表、单列投影、`DISTINCT`、`ORDER BY 1`、`LIMIT` 恰好等于 `probe_spec.limit + 1`；`exact` 恰好一个等值谓词且值等于 `probe_spec.value_text`；`contains` 恰好一个 `ILIKE ... ESCAPE` 谓词且模式等于转义后的 `%value%`；`distinct` 零个取值谓词，只允许 `列 IS NOT NULL`。
3. 不得含时间字面量或实时时钟函数。

任一条不符返回 `probe_shape_mismatch`。

**下推**（`purpose="pushdown"`）：外层由服务生成，只有聚合、排序、窗口与数值 `LIMIT`，不含新的取值谓词；内层是输入结果的 `executed_sql`。守卫对整条 SQL 重新执行 §9.4、§9.5 的检查，证据取本运行账本加输入结果 `meta.guard_evidence` 快照。快照随结果保存，因此不依赖原线程的 checkpoint，线程轮换或 checkpoint 过期清理后，只要结果仍在保留期内即可下推。输入结果缺少快照时返回 `pushdown_evidence_missing`。输入结果的 `datasource_id` 已在 §7.10 中核对与当前一致；快照中的维度证据再逐条核对 `datasource_id`，不一致按 `evidence_datasource_mismatch` 拒绝。

### 9.7 证据快照

`SqlEvidenceGuard.check` 通过时返回本次实际采用的证据**完整内容**（`used_evidence: GuardEvidenceSnapshot`），无论来源是本运行账本、占位符生成的 `prior_result` 证据、`reuse_evidence` 沿用的证据还是下推输入快照。它随可信校验记录保存在 `ValidationRecord.guard_evidence` 中；执行时原样写入结果的 `meta.guard_evidence`（§6.8），不再按 ID 回查来源。

体积：占位符证据最多 `placeholder_max_values` 个值，其余证据均为少量区间与取值，放在 checkpoint 中可以接受。快照用于：

- 结果的口径追溯：回答中引用的结果能独立说明其时间区间与维度取值的来源。
- 对该结果下推时的守卫证据（§9.6）。

### 9.8 守卫限制

- **受控的日期序列**是唯一放行的常量来源（F03 需要）。条件中的列追溯到 `generate_series` 时，必须同时满足：
  1. 出现在 `FROM`／`JOIN` 中作为表函数（带别名），不是 SELECT 列表中的集合返回函数。
  2. 形如 `generate_series(<L1>, <L2>, interval '<step>')`，`step` 只能是 `1 day`、`1 week`、`1 month`。
  3. 使用的时间证据必须两端非空：`L1` 等于某条有限时间证据（或首尾相接的一组有限证据）的 `start`；`L2` 等于同一区间的 `end` 减一个 `step`，可以写成计算后的字面量（如九月按天为 `'2026-09-30'`），也可以写成 `'<end>'::date - interval '<step>'`。单边证据不能直接用于生成无界日期序列。
  4. 用到的时间证据计入 `used_evidence`，随结果快照保存。

  不满足即 `constant_source_unsupported`。参数全为数值的 `generate_series`（如 `generate_series(1, 10)`）不涉及时间或维度取值，按“数据派生值”处理。
- 守卫只识别 §9.1 列出的谓词；`ANY(ARRAY[...])`、`EXISTS (VALUES ...)` 等其他把字面量当过滤值的写法返回 `unsupported_predicate_form`，而不是忽略。

### 9.9 守卫反例测试（`test_sql_guard.py`）

已登记证据：八月 `[2026-08-01, 2026-09-01)`、九月 `[2026-09-01, 2026-10-01)`（部分用例只登记其一）；维度证据：`sales.orders.status ∈ {'已支付'}`。`sales.orders` 与 `sales.refunds` 都有 `status` 列。

| 用例 | 期望 |
| --- | --- |
| 两个 `OR` 分支分别为八月、九月区间 | 放行，不报 `empty_time_range` |
| `d >= '2026-09-01' AND d < '2026-09-01'` | `empty_time_range` |
| 分别校验 `d >= '2026-08-01' AND d < '2026-09-01'` 与 `d >= '2026-09-01' AND d < '2026-10-01'` | 都放行（`2026-09-01` 分别作上界与下界） |
| 只登记八月时 `d >= '2026-09-01' AND d < '2026-09-15'` | `error_codes` 含 `time_lower_bound_mismatch`（`2026-09-01` 只是八月的 `end`）与 `unsupported_time_literal` |
| `d > '2026-09-01'`、`d <= ...`、`d = ...`、`BETWEEN` | `time_operator_not_half_open` |
| `WHERE d >= '2026-08-01' AND d < '2026-10-01'`（两段证据） | 放行（首尾相接并集） |
| 同上但只登记九月 | `unsupported_time_literal`（`2026-08-01` 不是任何证据的边界） |
| `CASE WHEN d >= '2026-09-01'` 且 WHERE 无上界 | `time_range_not_evidenced` |
| `FILTER (WHERE d < '2026-09-01')` 且 WHERE 为八、九月并集 | 放行 |
| `NOT (d >= '2026-09-01')` | `time_predicate_under_not` |
| `date_trunc('month', d) = '2026-09-01'` | `unsupported_predicate_form` |
| `FROM sales.orders o JOIN sales.refunds r ... WHERE status = '已支付'`（未限定） | `column_owner_unresolved` |
| `WHERE r.status = '已支付'`（证据属于 `orders`） | `unsupported_dimension_value` |
| `CASE WHEN o.status = '未登记值' THEN 1 END` | `unsupported_dimension_value` |
| `COUNT(*) FILTER (WHERE o.channel = '无证据值')` | `unsupported_dimension_value` |
| 派生表投影为表达式，外层按该列过滤字符串 | `column_owner_unresolved` |
| 子查询内的时间条件使用本作用域内完整区间 | 放行；子查询条件不与外层合并 |
| `x = ANY(ARRAY['a'])` | `unsupported_predicate_form` |
| `WITH f AS (SELECT '系统'::text AS v) ... JOIN f ON o.status = f.v` | `unsupported_dimension_value`（追溯为字面量） |
| 同上但 `f` 中为 `'已支付'::text` | 放行（追溯后有证据） |
| `JOIN (SELECT '系' \|\| '统' AS v) f ON o.status = f.v` | `constant_source_unsupported` |
| `JOIN (VALUES ('系统')) f(v) ON o.status = f.v`、`unnest(ARRAY['系统'])` | `constant_source_unsupported` |
| `o.status IN (SELECT '系统')`、`o.status = (SELECT '系统')` | `unsupported_dimension_value` |
| CTE 为 `SELECT status FROM sales.x UNION SELECT '系统'` 并参与 JOIN 条件 | `unsupported_dimension_value`（字面量分支） |
| `o.status = f.max_status`，`f` 为 `SELECT MAX(status) AS max_status FROM sales.x` | 放行（数据派生值） |
| F03：`FROM generate_series('2026-09-01'::date, '2026-09-30', interval '1 day') d(day) LEFT JOIN ... ON o.created_at >= d.day AND o.created_at < d.day + interval '1 day'`（已登记九月） | 放行 |
| 同上但 `L2` 为 `'2026-10-15'`，或九月证据未登记，或 `step` 为 `'2 days'` | `constant_source_unsupported` |
| `SELECT generate_series(...) AS day` 作派生列后参与条件 | `constant_source_unsupported` |
| 占位符位于字符串、注释、SELECT 列表、`=` 右侧 | `placeholder_error`（`placeholder_position_invalid`） |
| `distinct` 探针无取值谓词；`exact` 探针被改成 `ILIKE` | 前者放行，后者 `probe_shape_mismatch` |
| 线程轮换后对旧结果下推 | 使用快照放行；删除快照后 `pushdown_evidence_missing` |
| 轮换后新线程“再按客户分组”：先 `reuse_evidence(携带的九月证据 ID)` 再用九月区间 | 放行；新证据的 `inherited_from` 指向旧线程；不调用 `reuse_evidence` 直接用该区间 → `unsupported_time_literal` |
| 占位符查询与跨线程下推执行成功 | 结果 `meta.guard_evidence` 与授权记录中的快照逐项相同（执行端不回查来源） |
| 仅登记 `(None, 2026-10-01)`，执行 `d < '2026-10-01'` | 放行；快照保留 `start=None` |
| 仅登记有限九月区间，执行 `d < '2026-10-01'` | `time_range_not_evidenced`，不能省略有限证据下界 |
| 仅登记 `(None, 2026-10-01)`，执行 `d >= '2026-10-01'` | `time_lower_bound_mismatch`，单边方向不匹配 |
| 仅登记 `(2026-10-01, None)`，执行 `d >= '2026-10-01'` | 放行；快照保留 `end=None` |
| 只登记两个单边证据，SQL 组合成未登记的有限区间 | `time_range_not_evidenced`，需登记有限口径 |
| 使用仅有单边边界的证据构造日期 `generate_series` | `constant_source_unsupported` |
| 已核对 `name` 为文本列，`name = ''`、`name <> ''`、`name != ''`，无维度值证据 | 放行空值检查；不创建维度证据 |
| `name IS NULL`／`name IS NOT NULL` | 列归属与存在性核对后放行 |
| `name <> ' '`，无该值证据 | `unsupported_dimension_value`，空格不是空字符串 |
| `name LIKE ''`，无模式证据；`name IN ('')`，无值证据 | 分别为 `unsupported_pattern_filter`、`unsupported_dimension_value` |
| `TRIM(name) <> ''`／`COALESCE(name, '') = ''` | `unsupported_predicate_form`，不因空字符串而绕过形态检查 |

## 10. Agent 装配（`agent.py`）

### 10.1 装配顺序

```text
build_db_agent(services, session) -> DbAgentHandle
 1. model = get_llm(cfg.model.llm_profile, **cfg.model.overrides).llm
    apply_request_timeout(model, cfg.model.request_timeout_seconds)       # §11.1
 2. harness_key = register_and_verify_harness_profile(model, cfg)          # §10.2
 3. backend, permissions = build_backend(session, cfg)                     # §10.3
 4. file_mw = build_filesystem_middleware(backend, permissions, cfg)       # 私有参数封装，见 §10.2
 5. summary_model = model.model_copy()    # 仅给摘要模型副本加用途 metadata，不改共享 llm_factory 实例
    summary_model.metadata = {**(model.metadata or {}), "dbda_call_kind": "summarization"}
    summary_mw = SummarizationMiddleware(summary_model, backend=backend, summary_prompt=build_summary_prompt(prompts),
                                         trigger=("tokens", S), keep=("tokens", K),
                                         trim_tokens_to_summarize=cfg.summarization.trim_tokens_to_summarize)
 6. extra_mw = [FinalAnswerGateMiddleware(services.budget, cfg.budget), RunBudgetMiddleware(services.budget, cfg.budget),
               RequestAuditMiddleware(...)]
              + ([TodoListMiddleware()] if cfg.features.todo_list else [])
 7. agent = create_deep_agent(
        model=model,
        system_prompt=prompts["agent"]["system_prompt"],
        tools=build_business_tools(services),          # 含 submit_final_answer
        middleware=[file_mw, summary_mw, *extra_mw],
        subagents=[],
        skills=["/skills/"],
        memory=["/memory/memory_context.md"] if cfg.features.memory else None,
        permissions=permissions,
        backend=backend,
        # 不设置 response_format：避免 ToolStrategy 强制 tool_choice（K18、§20 第 9 条）
        state_schema=DbDeepAgentState,
        context_schema=DbAgentContext,
        checkpointer=checkpointer,
        store=store,
        name="db_deepagent",
    )
 8. verify_assembly(agent, expected_tools)                                 # §10.6
```

`file_mw`、`summary_mw` 与 `create_deep_agent(backend=...)` 共用同一个 `backend` 对象（概要设计 §8.2）。`file_mw` 与 `summary_mw` 的名称分别为 `FilesystemMiddleware` 与 `SummarizationMiddleware`，按 K2 原位替换默认实例，栈中不会出现第二套摘要逻辑。

### 10.2 HarnessProfile 注册与验证

SDK 私有依赖集中在 `agent.py` 的 `_sdk_adapter` 小节。装配与 Runner 只调用下表的适配函数，不在其他位置导入私有接口、传入私有参数或直接读取 SDK 私有状态键：

| 私有适配点 | 唯一入口 | 约定 |
| --- | --- | --- |
| `deepagents._models` 的模型标识函数、`_harness_profile_for_model` | `register_and_verify_harness_profile(model, cfg)` | 按实际模型标识注册并验证精确匹配键 |
| `FilesystemMiddleware` 的 `_permissions` 构造参数 | `build_filesystem_middleware(backend, permissions, cfg)` | 构造 §10.1 的白名单实例，显式传 `_permissions=permissions` 与配置的转存阈值，避免替换默认 middleware 后权限丢失 |
| `_summarization_event` 及其 `summary_message`／`cutoff_index`／`file_path` 结构；`_summarization_session_id` | `read_summary_for_handoff(state_values) -> str \| None` | 只读解析 `get_state().values`，返回可交接的摘要文本；不向 Runner 暴露 SDK 状态结构，不复制这些私有字段到新线程 |

上述约定按 `deepagents==0.7.25` 源码核对。SDK 升级时须先核对这些适配点，并通过 §18 的对应测试，再更新锁版。

HarnessProfile 适配函数：

```python
from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, register_harness_profile
from deepagents._models import get_model_identifier, get_model_provider
from deepagents.profiles.harness.harness_profiles import _harness_profile_for_model

def register_and_verify_harness_profile(model, cfg) -> str:
    provider = get_model_provider(model)
    identifier = get_model_identifier(model)
    if not provider or not identifier:
        raise HarnessProfileError("无法确认 HarnessProfile 匹配键，需要适配模型标识")
    key = f"{provider}:{identifier}"
    expected = cfg.model.expected_harness_key
    if expected is not None and expected != key:
        raise HarnessProfileError(f"配置的匹配键 {expected!r} 与模型实际解析键 {key!r} 不一致")
    register_harness_profile(key, HarnessProfile(
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)))
    resolved = _harness_profile_for_model(model, None)
    gp = resolved.general_purpose_subagent
    if gp is None or gp.enabled is not False:
        raise HarnessProfileError(f"HarnessProfile 未命中：{key}")
    logger.info("HarnessProfile 已命中: class=%s provider=%s identifier=%s key=%s",
                type(model).__name__, provider, identifier, key)
    return key
```

- 只注册 `provider:identifier` 精确键，不注册 `tongyi`、`openai` 这类 provider 级键，避免影响其他模型（概要设计 §8.2）。
- `ChatOpenAI` 兼容模式的键为 `openai:<模型名>`（K5）；它与真实 OpenAI 模型共用 `openai` provider 前缀，因此更不能注册 provider 级键。
- P12 的“错误键”场景：在测试中设置 `expected_harness_key` 为错误值，初始化必须抛 `HarnessProfileError`；另构造一个只注册错误键、不调用本函数的对照，验证 `_harness_profile_for_model` 未命中时 `general_purpose_subagent` 为空，说明“不能把未命中当作成功”。

**摘要读取适配**（K21）：

- 在旧线程的 `agent.get_state(old_config).values` 中读取 `_summarization_event`；键缺失或值为 `None` 表示尚无摘要，返回 `None`，交接消息仍可列出已有结果与携带证据，不额外调用模型生成摘要。
- 事件存在时，验证它为映射、`summary_message` 为 `HumanMessage`、`cutoff_index` 为非负整数且不超过该状态的消息数、`file_path` 为字符串或 `None`。从 `summary_message` 的文本内容提取摘要；不从 `messages` 中猜测“哪条消息是摘要”，也不根据 `cutoff_index` 删除旧消息。
- `_summarization_session_id` 缺失或为 `None` 时不影响已有摘要文本读取；非空时应为字符串。它仅标识 SDK 历史转存文件，不用于选择业务会话、授权或替代 `session_id`。
- 已存在的事件或标识结构不符合上述约定时抛 `AssemblyVerificationError`，由 Runner 在 P0 记录为运行失败并停止本轮，不静默当成“尚无摘要”。
- 新线程只接收带 `dbda_kind="handoff"` 标记的交接文本与本轮用户问题；不传入旧 `_summarization_event`、`cutoff_index` 或 `_summarization_session_id`。新线程的摘要事件与历史转存标识由 SDK 自行生成。

### 10.3 Backend 与权限

每个 CLI 会话一个会话工作目录 `<workspace>/sessions/<session_id>/`：

```text
<workspace>/
├── sessions/<session_id>/
│   ├── artifacts/        # /artifacts/ 路由：SDK 大结果转存、摘要历史
│   └── empty_root/       # CompositeBackend 默认后端根目录，始终为空
├── results/              # 业务结果（§6.8），不属于任何 backend
└── acceptance/           # 验收运行记录
```

```python
backend = CompositeBackend(
    default=FilesystemBackend(root_dir=session_dir / "empty_root", virtual_mode=True),
    routes={
        "/skills/": FilesystemBackend(root_dir=MODULE_DIR / "skills", virtual_mode=True),
        "/artifacts/": FilesystemBackend(root_dir=session_dir / "artifacts", virtual_mode=True),
        # features.memory 为 true 时：
        "/memory/": FilesystemBackend(root_dir=MODULE_DIR / "memory", virtual_mode=True),
    },
    artifacts_root="/artifacts",
)
```

- 默认后端使用空目录的 `FilesystemBackend`，不用 `StateBackend`，避免在 checkpoint 中出现 `files` 通道（K12），也保证 SDK 转存内容落盘。
- `/skills/` 与 `/memory/` 指向模块源码目录；模型没有写工具，权限规则也拒绝写入，所以对模型只读。

权限规则（顺序即生效顺序）：

```python
HIDDEN = ["/**/.*", "/**/.*/**"]
permissions = [
    FilesystemPermission(operations=["read", "write"], paths=HIDDEN, mode="deny"),
    FilesystemPermission(operations=["read"], paths=["/skills", "/skills/**"], mode="allow"),
    FilesystemPermission(operations=["read"], paths=["/artifacts", "/artifacts/**"], mode="allow"),
    # features.memory 为 true 时：
    FilesystemPermission(operations=["read"], paths=["/memory/memory_context.md"], mode="allow"),
    FilesystemPermission(operations=["read", "write"], paths=["/", "/**", *HIDDEN], mode="deny"),
]
```

- 隐藏路径单独前置拒绝：`/**` 不匹配点文件（K7），若只靠末尾兜底，`/artifacts/.x` 这类路径会落到“无命中即允许”。
- 末条兜底拒绝覆盖根目录、全部普通路径与隐藏路径的读写；删除属于 `write`（概要设计 §8.3）。
- 符号链接：各 `FilesystemBackend` 处于虚拟模式，指向根目录外的链接解析失败即被拒（K8）；根目录内的合法链接其解析目标也在允许范围内。实现时会话目录与 skills 目录中不放符号链接。
- 权限只约束内建文件工具；业务结果由 `ResultStore` 自行做会话与路径检查（概要设计 §8.3）。

### 10.4 最终工具集合

| 来源 | 工具 |
| --- | --- |
| 文件 middleware 白名单 | `ls`、`read_file`、`glob`、`grep` |
| 业务工具 | `search_schema`、`inspect_tables`、`plan_joins`、`resolve_time_range`、`probe_dimension_values`、`reuse_evidence`、`validate_sql`、`execute_sql`、`read_result`、`analyze_result` |
| 最终提交 | `submit_final_answer` |
| 可选 | `write_todos`（`features.todo_list=true` 时） |

不得出现：`write_file`、`edit_file`、`delete`、`execute`、`task`，以及异步子 Agent 工具；也不得出现 `ToolStrategy` 生成的结构化输出工具（本设计不设置 `response_format`）。

上表即 `ToolNode` 中的工具集合，也是 `wrap_model_call` 看到的 `request.tools` 集合，装配验证与运行期审计用同一份期望集合比对。

### 10.5 Skill 与 memory

- `skills=["/skills/"]`：SDK 将 `/skills/` 下每个子目录视为一个 Skill，把名称与描述写入系统提示，模型按需用 `read_file` 读取全文（K17）。三个 Skill 内容见 §12.2。
- `features.memory=true` 时才传 `memory=[...]`，默认不加载；不加载根目录 `AGENTS.md`（概要设计 §8.4）。

### 10.6 装配后验证

`verify_assembly` 在创建后立即执行，失败即抛错，不进入会话：

1. `agent.nodes["tools"].bound.tools_by_name` 的键集合等于**阶段清单常量**（按 `features` 计算，见下）。期望集合**在代码中定义**（装配模块常量 `STEP4_TOOLS`／`FULL_TOOLS`），不让配置任意指定“期望工具名”，否则错误装配可能被配置成验收通过；装配检查（本节、`doctor --stage all`、`test_assembly`）与运行期审计（§11.2 `RequestAuditMiddleware`、`tool_set_mismatch` 判定）引用同一常量。阶段口径（含文件 middleware 的 4 件）：
   - **步骤 4**：9（检索 3＋证据 3＋SQL 2＋`submit_final_answer`）＋文件 4＝**13 件**；
   - **步骤 5**：11（＋`read_result`、`analyze_result`）＋文件 4＝**15 件**；
   - 任一阶段 `features.todo_list=true` 时另加 `write_todos`（+1）。
   `create_deep_agent` 末尾的 `.with_config(...)` 返回的仍是 `Pregel` 副本，`nodes` 与 `channels` 可直接访问。
2. `agent.channels["messages"]` 是 `DeltaChannel`（K11、P11）。
3. `agent.channels` 中不存在 `files`（K12）。
4. `submit_final_answer` 存在且 `return_direct is False`（结束运行由 §11.3 的 middleware 判断，K19）。
5. 全部自定义工具的模型可见 Schema 的 `properties` 与 §7 约定业务字段集合一致，`required` 包含约定必填项且不含 runtime；不能只判断“不含 runtime”。使用 runtime 的工具函数签名须能解析为约定 ToolRuntime 类型。空 Schema、字段丢失或注解不符抛 `AssemblyVerificationError`；集成式单元测试另验证实际注入执行。

运行期由 `RequestAuditMiddleware` 再次核对每次模型请求实际携带的工具名（§11.2），两者同时作为 P12 的证据。

## 11. Middleware

### 11.1 `RunBudgetMiddleware`（`middleware/run_budget.py`）

LangChain 现成的调用上限 middleware 把运行级计数放在 `UntrackedValue` 通道中，运行结束后读不到（K14），Runner 无法据此判定“触达上限”。而把计数放进状态也不行：并行工具读到同一步的旧计数，可能全部通过检查，按键合并解决不了计数竞争（§8.5）。因此判定用进程内计数器，状态只记录结果。

**`RunBudgetLedger`**（`services/run_budget.py`，`DbAgentServices.budget`）：

```python
class RunBudgetLedger:
    """运行级计数器。判定与递增在同一把锁内完成（预占），不依赖状态合并。"""
    def open_run(self, run_id: str, limits: BudgetLimits, deadline_monotonic: float) -> None
    def reserve(self, run_id: str, kind: Literal["model_call", "tool_call", "db_execute"]) -> Reservation
        # with self._lock: 检查 deadline 与 counters[kind] < limit；通过则 counters[kind] += 1 并返回 ok；
        # 否则返回 exhausted(stop_reason)，并把 stop_reason 记入该运行（只记第一次）；
        # 该运行一旦记有 stop_reason，之后任何 kind 的预占都失败，下一次 before_model 即结束运行
    def stop(self, run_id: str, reason: str) -> None    # 记录 stop_reason（只记第一次）
    def stop_reason(self, run_id: str) -> str | None
    def remaining_seconds(self, run_id: str) -> float
    def close_run(self, run_id: str) -> BudgetSnapshot   # 返回计数与 stop_reason，并删除该运行的条目
```

- 锁为 `threading.Lock`：同步 `invoke` 下 `ToolNode` 用线程池并行执行工具，同一运行的预占操作互斥。预占后即使工具失败也不退还额度，计数表示“尝试次数”。
- `BudgetSnapshot.counters` 只有 `model_call`、`tool_call`、`db_execute` 三个总数，不提供按工具名计数。`RunStats.tool_calls` 取其中的 `tool_call` 标量；按名明细留在工具调用轨迹，不从 JSONL 反读重建结果统计。预占失败不增计数；已预占但参数校验失败的调用仍计数。
- `open_run` 的注册须原子完成：返回成功才认为已打开；异常时不能留下半初始化的运行条目，便于 Runner 用 `budget_opened` 判断是否需要关闭。
- Runner 在 `invoke` 前 `open_run`，结束后（含异常）在 `finally` 中 `close_run`；快照中的步骤计数写入 `DbAgentResult.stats.model_calls`，工具计数与停止原因写入结果与运行记录。模型调用尝试及 Token 的完整统计另由 §11.2.1 的回调提供。判定 `budget_exhausted` 以预算快照中的 `stop_reason` 为准，状态中的 `db_run_status` 只作轨迹。
- 预占点：

| 计数 | 预占位置 |
| --- | --- |
| `model_call` | `RunBudgetMiddleware.before_model`，主 Agent 模型步骤预占；不包含摘要内部调用 |
| `tool_call` | `RunBudgetMiddleware.wrap_tool_call`，在调用 `handler` 之前 |
| `db_execute` | `SqlPipeline.execute` 第 4 步（§8.3）。`execute_sql`、探针、下推的内部执行都经过这里，全部计入 `max_execute_calls_per_run` |

**`RunBudgetMiddleware`**（`middleware/run_budget.py`）：

| 钩子 | 行为 |
| --- | --- |
| `before_model`（`can_jump_to=["end"]`） | `reserve("model_call")` 失败（次数超限或已过时限）→ 写 `db_run_status[run_id].stop_reason`，追加一条说明性 `AIMessage`，`jump_to="end"` |
| `wrap_tool_call` | `reserve("tool_call")` 失败 → 不调用 `handler`，返回 `Command`：`budget_exhausted` 错误包的 `ToolMessage` 加 `db_run_status` 更新；预占成功后调用 handler，并记录工具名、tool_call_id、是否预占成功及返回状态的审计轨迹，含框架参数校验错误 ToolMessage，不依赖业务函数一定执行 |

Runner 读到 `stop_reason` 时，结果状态为 `budget_exhausted`，验收计分为该题失败（验收题集 §5 第 5 条）。

**预算范围**：`max_model_calls_per_run` 限制主 Agent 模型节点的步骤预占，不是全部 API 调用的硬上限。摘要 middleware 的内部模型调用和重试不经 `before_model`，不纳入该预算；第一阶段不另建摘要次数或 Token 预算。摘要使用相同模型请求超时、SDK 自身重试限制与协作式运行时限；正在进行的摘要不能被时限检查强制中断，下一预占点才会停止。统计与审计必须包含这些可观察调用（§11.2.1），不能把预算计数当成总调用数。

**时限是协作式的**：`deadline_monotonic` 只在预占点检查（模型调用前、工具调用前、数据库执行前），不能中断已经阻塞的模型请求或数据库语句。每类网络调用因此另设超时，保证单次阻塞有上限：

| 调用 | 超时设置 |
| --- | --- |
| 模型（`ChatTongyi`） | `llm_factory` 拒绝 `timeout` 覆盖（C12），所以由 `apply_request_timeout` 在构造后设置 `model.model_kwargs["request_timeout"] = cfg.model.request_timeout_seconds`，经 DashScope `_build_api_request` 生效（默认 300 秒）。注意 `ChatTongyi` 自身的重试会让总耗时成倍增加 |
| 模型（`ChatOpenAI`，备选 profile） | 通过 `overrides.timeout` 映射为 `request_timeout` |
| Embedding | `TextEmbedding.call(..., request_timeout=...)`（§6.2） |
| Milvus | 搜索与查询传 `timeout=cfg.milvus.timeout` |
| Neo4j | 驱动 `connection_timeout`、`Query(..., timeout=...)`（§6.6） |
| 业务库 | `SET LOCAL statement_timeout = min(配置, ledger.remaining_seconds(run_id))`（§6.1） |

单元测试断言 `apply_request_timeout` 后 `ChatTongyi` 的 `_invocation_params` 中有 `request_timeout`；实际生效在步骤 1 的真实模型用例中确认。

### 11.2 `RequestAuditMiddleware`（`middleware/request_audit.py`）

`wrap_model_call` 中记录：`request_id`、`run_id`、模型调用序号、`[t.name for t in request.tools]`、输入消息数、估算 Token（`count_tokens_approximately`）、模型返回的 `usage_metadata`、耗时。写入日志 `nl2sql.db_deepagent.audit`，并追加到 `<workspace>/sessions/<session_id>/audit.jsonl`。按 §10.4 期望集合比对；若工具名集合不一致，记录 `ERROR`，不调用 `handler`，执行 `ledger.stop(run_id, "tool_set_mismatch")`，返回一条不含工具调用的 `AIMessage`（“工具集合异常，运行终止”），运行随即结束，Runner 按快照中的 `stop_reason` 处理。不记录 API key、连接串和完整提示词正文。

#### 11.2.1 主模型与摘要的统一调用观测（`middleware/model_observer.py`）

`RequestAuditMiddleware` 负责主 Agent 步骤的工具集合与请求审计，不能覆盖外层摘要 middleware 的内部模型调用。另用 LangChain 公开的 `BaseCallbackHandler` 回调补齐两类调用的统计与审计，不读取 `_summarization_event` 推算次数，也不修改 SDK 私有摘要实现。

- 每次 Runner 运行创建独立 `ModelCallObserver`，经 `agent.invoke(..., config={"callbacks": [observer], "metadata": {"request_id": ..., "dbda_run_id": ..., "dbda_call_kind": "agent"}, ...})` 注入；摘要模型副本的用途 metadata 为 `summarization`（§10.1）。内部摘要／溢出重试虽然传入新的 `_summary_config()`，callbacks 仍通过节点 Runnable 上下文与 `ensure_config` 继承（K22）；不假定修改 middleware 顺序即可覆盖内部调用。
- **运行归属只取观测器实例**：构造时绑定不可变的业务 `request_id`／`run_id`。摘要显式 metadata 会替换 invoke 层 metadata，所以摘要回调可能没有 `request_id`／`dbda_run_id`；不得从回调 metadata 获取业务归属，也不得把回调事件参数 `run_id`（模型尝试 ID）当成业务 `run_id`。
- **用途取回调 metadata 的 `dbda_call_kind`**：主模型由 invoke 配置提供 `agent`，摘要由模型实例 metadata 提供 `summarization`，实例标记覆盖同名配置值。取值来源不同，值域保持一致；标记缺失或非法时记录观测异常、使统计不完整，不能默认归为主模型。内部摘要、正常调用及重试的传播与归属须通过测试验证，业务代码不直接操作 `var_child_runnable_config`。
- 记录 `on_chat_model_start`／`on_llm_start`、`on_llm_end`、`on_llm_error`；以回调的模型调用 ID 去重，用锁保护运行内计数。记录业务 `request_id`／`run_id`、用途、尝试 ID、开始／结束、成功／失败、耗时、可得的模型用量。不记录完整提示词、连接参数或密钥，审计日志走原有 JSONL／stderr 路径。
- `summary_model_calls` 是观察到的摘要模型开始次数，`total_model_calls` 是主模型与摘要模型的全部开始次数；失败尝试和框架层重新发起的模型调用也计数。`model_calls` 仍是预算中的主 Agent 步骤预占数，可能包含调用前被审计阻止的步骤；有重试时，一步也可能对应多个尝试。因此不要求 `total_model_calls == model_calls + summary_model_calls`。
- Token 只由这套回调聚合一次。优先取模型响应的 `usage_metadata`，必要时适配同一次响应的 `llm_output`，不能将两处用量重复相加；`RequestAuditMiddleware` 的用量记录仅供诊断，不再累计进统计。失败尝试或模型未提供用量时，不能以零填补未知消耗。
- 用量完整时返回输入／输出 Token 合计与 `token_usage_complete=True`；任一观察到的调用用量未知时，两个合计返回 `None`、完整性标志为 False，已知部分保留在审计事件中并提示统计不完整。未发生任何模型调用时，两个计数与 Token 合计均为 0，完整性为 True。
- Runner 在 `invoke` 成功或异常后都封存观测快照，用于结果、归档和本地记录。观测器不承担预算拦截；供应商客户端内部 HTTP 重试未必产生新回调，因此总数仅表示框架可观察的模型调用尝试数，不等于网络请求数或账单次数。

### 11.3 `FinalAnswerGateMiddleware`（`middleware/final_answer_gate.py`）

负责最终提交后的结束与漏提交的提醒，`submit_final_answer` 因此不需要 `return_direct`（K19）：

| 钩子 | 行为 |
| --- | --- |
| `before_model`（`can_jump_to=["end"]`） | `db_final_answers` 中已有当前 `run_id` 的记录 → `jump_to="end"`。提交工具执行后图回到模型节点，本钩子在调用模型前结束运行 |
| `after_model`（`can_jump_to=["model", "end"]`） | 模型返回的 `AIMessage` 没有工具调用，当前运行没有已接受的最终答案，且 `ledger.stop_reason(run_id)` 为空（预算耗尽或工具集合异常时不提醒）：若本运行提醒次数 < `budget.final_answer_reminders`，追加一条 `HumanMessage`（`additional_kwargs={"dbda_kind": "final_answer_reminder"}`，内容为“请调用 submit_final_answer 单独提交最终答案，并引用已有 evidence_id”），提醒次数 +1 写入 `db_run_status`，`jump_to="model"`；否则不干预，运行自然结束，Runner 按“未提交最终答案”处理（§14.3） |

被拒的提交（`final_answer_not_alone`、`invalid_evidence_reference`）不写记录，`before_model` 不会结束运行，模型在下一轮看到错误后修正。

### 11.4 middleware 最终顺序

按 K1、K2，实际顺序为：

1. `FilesystemMiddleware`（替换版）
2. `SummarizationMiddleware`（替换版）
3. `PatchToolCallsMiddleware`
4. `FinalAnswerGateMiddleware`、`RunBudgetMiddleware`、`RequestAuditMiddleware`、（可选）`TodoListMiddleware`。`FinalAnswerGateMiddleware` 排在 `RunBudgetMiddleware` 之前：已提交答案时，在预占模型调用额度之前就结束运行
5. `SkillsMiddleware`
6. Prompt Caching（对 Qwen 无操作）
7. （可选）`MemoryMiddleware`
8. `UnsupportedContentMiddleware`

不出现 `SubAgentMiddleware`、`AsyncSubAgentMiddleware`、`HumanInTheLoopMiddleware`（权限规则不含 `interrupt`）。

## 12. 提示词与 Skill 内容

### 12.1 系统提示（`prompts.yaml` 的 `agent.system_prompt`）

要点（实现时按此写成完整中文提示）：

1. **角色**：数据库查询与分析 Agent，数据源为 PostgreSQL `orders`；只读；所有数据结论必须来自本次实际执行的查询或结果计算，或经 `reuse_evidence` 受控沿用并标注来源的历史结果（时间／维度证据不能单独支撑业务数值结论）。
2. **工作方式**：根据问题与工具反馈自主决定下一步；优先一条合理的 SQL，存在真实的结果依赖时再分步；检索不足时补充检索；校验失败时修改 SQL、检查结构或补充证据，而不是重复提交同一 SQL。
3. **表与字段**：只使用 `inspect_tables` 确认过的表和列；SQL 中使用两段式 `schema.table`；JOIN 依据 `plan_joins` 返回的条件；多表查询中所有列都带表别名，过滤条件写在基表所在的查询层。
4. **时间**：时间过滤边界只能来自 `resolve_time_range`，或经 `reuse_evidence` 沿用的已确认证据（如线程交接清单中的口径）；有限区间使用 `>= start AND < end`，单边区间仅使用证据非空端对应的 `>= start` 或 `< end`，不擅自补另一端；不使用 `CURRENT_DATE`、`now()`；裸“当前／目前／现在”不是时间范围。
5. **维度值**：过滤值必须来自维度值命中、探针确认或前序结果，§9.5 明确允许的空值检查除外；未命中的词不能直接写进条件，也不能改用模糊匹配或替换为相似值；无法确认时如实说明或澄清。
6. **SQL 流程**：先 `validate_sql`，通过后用完全相同的文本调用 `execute_sql`；校验与执行分两步串行调用；SQL 改动后重新校验。依赖前一步多行结果时，用结果占位符，不手抄值。
7. **结果**：注意 `preview_truncated` 与 `source_truncated` 的区别；源结果截断时，总体统计用 `analyze_result` 下推，不用已保存样本冒充总体（**步骤 4 阶段 `analyze_result` 尚未交付**，提示词以“源结果截断时如实说明、不冒充总体”替代下推指引；§12.2 Skill 中涉结果分析的指引同样随步骤 5 交付后写入——提示词不得要求调用尚未提供的工具，§10.6）。
8. **区分情形**：空结果、查询失败、数据缺失、业务异常分别表述。
9. **结束**：证据足够时**单独**调用 `submit_final_answer` 提交最终答案（同一条消息中不要调用其他工具）；每条发现引用 `evidence_id`；写明口径、限制、未完成事项；只引用 `execute_sql`、`analyze_result` 返回过的 `evidence_id`（探针的执行记录不能引用）。提交被拒时按错误提示修正后重新提交。不要用普通文本结束回答。
10. **禁止**：编造数值、SQL、行数、`result_ref`、`evidence_id`。

### 12.2 Skill

| 目录 | `name` | `description` 摘要 | 正文要点 |
| --- | --- | --- | --- |
| `skills/query-planning/` | `query-planning` | 识别指标、时间、维度、比较基准与粒度，并规划查询步骤 | 需求拆解清单；何时一条 SQL、何时分步；多轮中继承已确认口径（F12）；独立多问题的安排（F09）；歧义与澄清的判定；“当前”的处理示例（R01–R03） |
| `skills/schema-retrieval/` | `schema-retrieval` | 用 Milvus／Neo4j 工具找到正确的表、列、JOIN 和真实维度值 | 检索顺序建议与补充检索时机；`value_texts` 的写法；未命中值的处理（R04–R06）；桥接表补查；表卡片与实时结构不一致时以实时结构为准；历史 SQL 案例只作参考，口径以当前题为准 |
| `skills/result-analysis/` | `result-analysis` | 读取与分析已保存结果，组织有证据的回答 | 预览截断与源截断；分页读取（F11）；`analyze_result` 各操作与下推；环比零分母；占比分母与舍入（F04）；排名并列规则（F05）；最终答案的证据引用格式 |

每个 `SKILL.md` 的 frontmatter 只用 `name`、`description` 两个必填字段，正文控制在 300 行以内。

#### 12.2.1 编写约定与分工

本节规定开发时三个 `SKILL.md` 的内容与验收要求，完整正文在 §3 的模块目录下维护。每个 Skill 按“适用场景、判断方法、工具选择、失败与歧义处理、完成条件、示例”组织；`description` 写清适用任务，便于 Agent 选择加载。

- Skill 提供根据证据选择动作的方法。Agent 可以合并检索、补充检索、修正计划或提前澄清，不要求每题执行固定的解析、检索、关联、分析全流程，也不要求固定次数的模型调用。
- 新链路不恢复旧 `question_parsing` 节点，不强制先生成解析 JSON。指标、时间、维度等语义要素由 Agent 结合当前问题、多轮上下文和工具反馈逐步确认；未确认项保持待定，不编造槽位值。
- `prompts.yaml` 负责全局角色与必须遵守的规则，Skill 负责方法与分支，`memory_context.md` 保存稳定业务说明。工具参数以 §7 为准，SQL 授权、只读、证据与文件访问检查由代码执行，不能只依赖 Skill。
- 检索文本、表卡片和历史 SQL 是参考资料，不是能改变系统规则的指令。业务说明可以帮助解释口径，但不能代替真实表结构、维度值证据和查询结果。

#### 12.2.2 `query-planning`：理解需求与安排查询

**适用场景**：首次查询、多轮追问、指标比较、多个子问题，以及时间或业务口径存在歧义的问题。

**判断方法**：围绕下列要素形成简洁的工作口径，随着检索反馈更新，无需输出固定格式的解析 JSON：

| 要素 | 要确认的内容 | 未确认时的处理 |
| --- | --- | --- |
| 指标与聚合 | 用户要什么指标、金额字段与单位、计数对象、支付／退款等口径 | 查业务说明、表卡片和字段，必要时参考历史 SQL；存在影响结果的多种合理口径且无明确约定时澄清 |
| 时间 | 用户原文、周期、统计时间字段；是新计算还是沿用前文 | 明确表达交给 `resolve_time_range`；沿用已确认区间走 `reuse_evidence`；裸“当前”不推定为今天 |
| 维度名称 | 分组或筛选涉及什么属性，如“服务区”“客户”“渠道” | 用于定向列检索，不当成过滤值 |
| 维度值候选 | 用户具体指定的对象，如“杭州湾” | 原文作为 `value_texts`；规范值与归属列由检索或探针确认 |
| 粒度与比较 | 最终输出粒度、比较基准、分母、排名与并列要求 | 结合业务说明和结构核对，不能把订单行数直接当订单数，也不能把日汇总重复相加 |
| 多轮关系 | 本轮新增、替换或明确沿用哪些条件 | 以用户最新明确要求为准；旧证据沿用仍须按 §7.12 登记，摘要描述本身不构成执行证据 |

**查询安排**：

- 单条 SQL 能清楚表达且没有真实结果依赖时，优先一次完成；不预先划分“简单／复杂”两条流程。
- 必须根据前一步数据决定下一步对象、范围或查询策略时，再拆为多步。前序编号列表用结果占位符传递，避免模型手抄。
- 同一用户请求中的独立子问题分别跟踪完成情况，说明各自的时间与口径。不能用一项成功掩盖其他项未完成，也不能把口径不同的结果直接合并。
- 检索和校验反馈显示原计划不成立时，先判断缺少结构、维度值、关联路径还是业务定义，再调用对应工具补足；不盲目重复同一调用。

**失败与歧义处理**：能通过结构或受控检索解决的疑点先查证；会改变用户明确范围、指标含义或对象选择且无依据裁决时，向用户澄清。不自动删除未匹配的条件，不把“查不到指定对象”改成“查询全部对象”。

“某日前／截至某日／从某日起”的单边需求用 `resolve_time_range(kind="open_range", ...)` 登记，不补另一端。年份、方向或是否包含当天不明确时按 §7.5 澄清；不把“某日之后”擅自改成“从某日至今天”。

**完成条件**：准备编写 SQL 时，所需指标、时间、筛选、输出粒度和关联方式已有可说明的依据；无需使用的要素不强行补齐。仍有关键歧义时，以 `needs_clarification` 提交具体问题，不声称已完成查询。

#### 12.2.3 `schema-retrieval`：从语义要素到检索证据

**适用场景**：找表找列、规范化维度值、多表关联、补查桥接表、结构冲突和 SQL 校验后的检索修正。

**检索对象与工具映射**：

| 需求或线索 | 动作 | 返回信息如何使用 |
| --- | --- | --- |
| 整体业务意图、指标词 | `search_schema(query=..., kinds=["tables", "columns"])` | 形成候选事实表、金额／时间／标识字段，不凭相似度直接认定指标口径 |
| 维度名称，如“服务区” | 必要时单独 `search_schema(query="服务区", kinds=["columns"])` | 列命中的 `parent_id`／父表标识加入候选，并用 `inspect_tables` 核对；无需等待独立表检索也命中该表 |
| 维度值候选，如“杭州湾” | `search_schema(query=..., kinds=["dim_values"], value_texts=["杭州湾"])` | 核对命中的维度表、列、规范值和 `dim_evidence_id`，不能只取值而丢掉列绑定 |
| 已有候选表 | `inspect_tables(table_ids=...)` | 核对真实列、类型、表类别和时间提示；卡片与实时结构冲突时，实际 SQL 使用实时结构 |
| 多表查询需要关联 | `plan_joins(base_table=..., target_tables=...)` | 使用返回的 JOIN 条件与路径，补查 `bridge_tables`；检查基表粒度和关联基数是否会重复累计指标 |
| 缺少可参考的 SQL 写法或指标实现 | 按需 `search_schema(query=..., kinds=["sql_examples"])` | 案例仅作参考，重新核对当前表结构、时间、对象和口径，并重新校验 SQL |

Milvus 三个集合分别是 `table_schema_embeddings`（表、列和卡片）、`dim_value_embeddings`（维度值）、`sql_example_embeddings`（SQL 案例）。Neo4j 通过 `plan_joins` 查询关系路径。保留全部能力，但不要求每题访问全部集合与 Neo4j。

`search_schema` 对一个 `query` 生成的向量用于该调用中的表、列和案例检索；每个 `value_texts` 单独检索维度值。整句检索召回不足时，Agent 应把“指标词”“维度名称”分别作为 `query` 补查，不把所有名词都放入 `value_texts`。列命中所属表的补查由 Agent 根据工具返回发起，不假定工具内部自动执行旧 `SchemaRetriever` 的全流程。

**维度值与失败分支**：

- 达到配置阈值的命中是候选证据，不等于已消除语义歧义。仍须确认归属表与列符合用户所指对象；多个候选可能代表不同实体时，补查或澄清，不能只取最高分。
- 未命中值不产生证据。先确认候选列，再用 `probe_dimension_values(mode="exact")` 查证；需要查找数据库中包含原文的规范写法时可用 `contains`。低基数状态／渠道值可用 `distinct` 确认。探针只能使用 §7.6 的受控接口。
- `contains` 返回多个真实值时，不能默认全部合并成用户指定对象；依据已有业务说明消歧，仍不明确则请用户选择。返回截断时不能声称已列出全部候选。
- 检索阈值由服务端配置，工具不接受模型侧 `min_score`；不通过降低阈值、原文透传、主查询中擅自改为模糊匹配或替换成相似实体来绕过未命中问题。返回的分数与生效阈值便于排查，但不能代替实体消歧。
- 空值检查按 §9.5 使用 `IS NULL`／`IS NOT NULL` 或已确认文本列的 `= ''`／`<> ''`，无需先检索空字符串；NULL、空字符串和空白字符串分别处理。不得把 `IS NOT NULL` 当成排除空字符串，也不为绕过校验改写成不受支持的函数条件。
- 检索成功但无命中、后端失败、Neo4j 无路径和表不在图中分别处理。后端失败不解释为业务不存在；无路径不编造 JOIN 键，也不做无依据的笛卡尔积。
- 多表关联可能放大行数时，根据确认的键、粒度和基数选择先聚合、存在性过滤或其他正确写法，不能直接对重复后的金额求和。无法确认关联口径时报告限制或澄清。

**完成条件**：所选表列已通过 `inspect_tables` 核对；所有需要的字符串过滤值都有绑定表列的证据（§9.5 的空值检查例外除外）；需要的 JOIN 路径和桥接表已核对；指标粒度与关联方式一致。证据足够即转入 SQL 编写，不为凑齐检索类型继续调用工具。

#### 12.2.4 `result-analysis`：结果读取、计算与回答

**适用场景**：SQL 执行后读取明细或汇总、比较多个结果、计算衍生指标，以及组织最终答案。

- 先区分 `preview_truncated`（预览不完整）和 `source_truncated`（源结果不完整）。预览不足用 `read_result` 按需读取；源结果截断时，不能用保存的样本计算总体总额、占比或排名，应按 §7.10 选择下推或重新查询。
- 本地计算只使用工具支持且满足输入条件的操作；NULL、空集、数值类型与 SQL 下推一致性的细则按 §21 门禁在步骤 5 前补齐，不由 Skill 自行约定一套不同规则。
- 比较前核对时间、单位、过滤、粒度与分母是否可比；环比基期为零时说明无法按通常公式计算，不编造百分比；排名并列规则、舍入与单位在答案中交代。
- 仅需一个汇总值且查询已返回完整、可解释的结果时，可直接组织答案，不强制再调用 `analyze_result`。多步结果依赖保留 `result_ref` 与执行证据关联。
- 探针用于确认值，不能作为最终业务结论的执行证据；表卡片、历史 SQL 和模型推断同样不能证明销售额等数值。历史结果引用扩展按 §21 补齐后使用，当前接口不假定支持。
- **完成条件**：每条数据结论都能引用当前接口允许的执行／分析 `evidence_id`；写清采用口径、截断或未完成限制；单独调用 `submit_final_answer`。空结果、执行失败和缺少数据不能统一表述为“销售额为 0”。

#### 12.2.5 贯穿示例：今年杭州湾服务区的销售额是多少

本例说明工具选择方法，不是固定编排。使用固定时钟 `2026-10-13T12:00:00+08:00`；以下不预设真实表名、字段名、规范值、销售额定义或结果数值。

1. **理解需求**：指标为“销售额”，时间原文为“今年”，维度名称为“服务区”，维度值候选为“杭州湾”，预期输出一个汇总金额。支付、退款口径和统计时间字段尚待确认；“服务区”不作为过滤值。
2. **按需检索**：可以先合并查找结构与值：

   ```python
   search_schema(
       query="销售额 服务区",
       kinds=["tables", "columns", "dim_values"],
       value_texts=["杭州湾"],
   )
   ```

   若未找到相关维度列，再单独检索 `query="服务区", kinds=["columns"]`；金额字段不明确时补查“销售额”相关结构或案例。将列命中的父表、维度值命中的表一并纳入结构核对，调用 `inspect_tables`。
3. **取得时间证据**：调用 `resolve_time_range(source_text="今年", kind="relative", relative="this_year")`，得到 `[2026-01-01, 2027-01-01)`、时区与证据 ID。可以在候选表尚未确定时先计算区间，但统计时间字段要在结构与业务口径确认后选择。
4. **处理维度值分支**：若“杭州湾”命中且实体明确，使用返回的规范值与绑定列；未命中则在已确认列上探针查询；探针出现多个可能对象时澄清；确无匹配时说明无法确认指定对象，不移除该条件查询全体服务区。
5. **确定口径与关联**：从业务说明和候选结构确认金额含义、单位、支付／退款规则及时间字段。若需要关联销售事实表和服务区维度表，调用 `plan_joins` 并补查桥接表、核对基数；单表已有全部所需字段时无需查询 Neo4j。已有口径依据充分时也无需检索 SQL 案例。
6. **编写与执行**：根据实际证据写 SQL，时间使用 `>= '2026-01-01' AND < '2027-01-01'`；串行执行 `validate_sql`、`execute_sql`，后者使用完全相同的 SQL 文本。校验失败时按错误类型修正结构、过滤或关联，不以放宽用户条件换取通过。
7. **组织答案**：结果完整且口径明确时，引用查询执行证据提交金额、单位、时间范围与销售额定义；未解决对象或指标歧义时提交 `needs_clarification`；空结果按实际查询语义解释，不直接认定为零。

#### 12.2.6 Skill 内容验收

Skill 属于模型行为指导，加载成功不能证明 Agent 一定遵循。开发时检查其与 §7 工具参数、§9 守卫和 §12.1 系统提示一致，并在现有固定题集的轨迹中观察：F12 的口径继承、R01–R03 的时间处理、R04–R06 的值检索与探针、F10 的空结果／对象未命中、F11 的截断处理。需要定向检索、父表补查或桥接表补查的用例应能看到相应动作；已有证据充分的用例不要求机械重复检索。最终通过线仍按文档 4，不以工具调用次数或是否输出解析 JSON 作为正确性标准。

## 13. 持久化

### 13.1 平台连接池（`persistence/platform_pool.py`）

```python
conninfo = make_conninfo(host=..., port=..., dbname="lg_persist", user=..., password=...,
                         sslmode=..., connect_timeout=...,
                         options=f"-c search_path={schema} -c statement_timeout={ms}")
pool = ConnectionPool(conninfo, min_size=..., max_size=...,
                      kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
                      configure=_verify_connection, open=False)
```

- `kwargs` 与 `PostgresSaver.from_conn_string` 使用的连接参数一致（K16）。
- `search_path` 通过 libpq `options` 在每条连接（包括连接池后续新建的连接）建立时设置；`search_path` 中只有 `db_deepagent`，不含 `public`。
- `_verify_connection(conn)`：执行 `SELECT current_database(), current_schema()`；数据库不是 `lg_persist` 或 `current_schema()` 不是 `db_deepagent` 时抛 `PlatformSchemaError`。Schema 不存在时 PostgreSQL 的 `current_schema()` 返回 NULL，此时报错而不是回退到 `public`。

**初始化**（`cli maintenance init-platform`，显式执行，不在每次启动时隐式执行）：

1. 用一条不设 `search_path` 的管理连接执行 `CREATE SCHEMA IF NOT EXISTS db_deepagent`（需要相应权限；没有权限时报错，由 DBA 预先创建）。
2. 打开连接池，`PostgresSaver(pool).setup()`、`PostgresStore(pool).setup()`。
3. 查询 `information_schema.tables` 确认 `checkpoints`、`checkpoint_blobs`、`checkpoint_writes`、`checkpoint_migrations`、`store`、`store_migrations` 均位于 `db_deepagent`，且 `public` 中旧表的行数与结构未变化（P08）。

常规启动只做核对（连接回调 + 表存在性检查），表缺失时提示先运行初始化命令。

### 13.2 Checkpoint（`persistence/checkpoint.py`）

- `persistence.mode=postgres`：`PostgresSaver(pool)`；读写异常原样抛出，Runner 将其转换为可见的 `persistence_error`，不 fail-open（概要设计 §9.1）。
- `persistence.mode=memory`：`InMemorySaver()` 与 `InMemoryStore()`，CLI 启动时显著提示“当前为内存模式，会话不会保存”；不会在 PG 失败时自动切换到该模式。
- `thread_id` 格式：`dbda:<session_id>:<序号>`，序号从 1 开始，轮换时递增。

### 13.3 Store 归档（`persistence/archive.py`）

`PostgresStore(pool)`，命名空间：

| 命名空间 | key | 内容 |
| --- | --- | --- |
| `("db_deepagent", "qa", session_id)` | `request_id` | 满足写入判定 a 或 b 的请求（见下）：问题、最终回答、状态（Runner 真实运行状态七值）、evidence 摘要（含沿用来源标记）、`result_ref` 列表、`thread_id`、`run_id`、耗时、Token |
| `("db_deepagent", "run_errors", session_id)` | `request_id` | 无成功执行的请求：问题、`status`、`stop_reason`、错误摘要 |
| `("db_deepagent", "sessions")` | `session_id` | 当前 `thread_id`、线程序列、创建与最近活动时间、是否活动中 |
| `("db_deepagent", "threads", session_id)` | `thread_id` | 线程活动轨迹：`seq`、`last_active_at`、`retired`、`retired_at`（doc 7 §2 折入；Runner P3 与 sessions 同批 upsert；`sessions.thread_seq` 语义＝会话已分配最大线程序号） |

**写入判定**（“登记成功”不等于“回答成功”——`reuse_evidence` 也能登记时间／维度证据，而它们不能单独支撑业务数值结论；E／A 登记后模型也可能未提交或提交被拒）：

- **qa 写入条件**（满足其一）：
  a. 存在成功真实执行（`db_executions` 中 `purpose` 为 `query` 或 `pushdown`、`reused_from` 为空）；或
  b. 最终答案已被接受（`db_final_answers` 有本 `run_id` 记录）且评估状态为 `completed`／`partial`，且其 `findings` 引用了有效登记的 E／A 沿用证据。
- 纯沿用请求只有满足 b 才进 `qa`；仅时间／维度沿用、没有最终答案、或引用无效 → `run_errors`。
- **`qa` 的 `status` 字段直接保存 Runner 判定的真实运行状态**（与 `DbAgentResult.status` 同一七值枚举），不另设 qa 专用枚举、不为凑枚举改写状态：SQL 成功后预算耗尽（满足 a、状态 `budget_exhausted`）、查询成功但空结果（`no_data`）、待澄清（`needs_clarification`）均按真实状态入 `qa`；`persistence_error` 按 §14.3 P3 规则归档本身失败、不写 `qa`。预算耗尽但无成功真实执行的请求不满足 a → `run_errors`。
- `qa` 的 evidence 摘要保留 `reused_from` 来源标记（§14.2 `EvidenceItem`）。

同一线程的模型上下文来自 checkpoint，不从 Store 读取问答再注入（F12“历史不重复注入”）。

### 13.4 线程轮换

触发：下一轮开始前，当前线程 `messages` 数量 ≥ `thread.rotate_after_messages`，或用户在交互模式输入 `/rotate`。

步骤：

0. **（doc 7 折入）入口与编号约束**：Runner 读取任何 checkpoint 前校验显式 `thread_id` 与 sessions 记录的 `current_thread_id` 均经 `parse_thread_id` 归属本会话（跨会话→P0 拒绝）；轮换新编号 `= max(会话已分配最大序号, 当前序号) + 1`，**会话锁内分配**且校验目标 `thread_id` 无既有 checkpoint 状态（存在则继续 +1）——恢复旧线程不回退最大序号，杜绝复用已有线程编号。
1. 通过 `agent.get_state(old_config).values` 读取当前线程状态；调用 §10.2 的 `read_summary_for_handoff(state_values)` 取得最近一次摘要事件的摘要文本（若有），同时从业务账本读取本线程全部成功执行与分析的 `evidence_id`／`result_ref`／口径要点。Runner 不直接读取 SDK 私有状态键。
2. **收集携带证据**（结构化，不依赖交接摘要中的自然语言）：
   - 来源：本线程成功执行（`purpose` 为 `query` 或 `pushdown`）的授权记录中的 `guard_evidence`，加上本线程自身携带的、仍在使用的证据（多次轮换时逐代传递）。
   - 每条转换为 `CarriedEvidence`：保存原始 `TimeEvidence`／`DimEvidence` 完整记录，以及 `EvidenceOrigin`（原 `thread_id`、`request_id`、`run_id`、原始证据 ID，和该证据对应的用户原文：时间证据取登记它的那一轮问题全文）。
   - 按内容去重（时间证据按 `start`、`end`、`grain`、`timezone`，保留 None 边界，不填补或参与日期大小比较；维度证据按表、列、取值集合），最多保留 `thread.carry_max_evidence`（默认 200）条，优先保留最近使用的。
   - **执行／分析记录携带**：另把最近 `thread.carry_recent_executions`（默认 3，0 关闭）条满足以下条件的执行（`purpose="query"`）／分析记录一并携带：输出结果元数据 `datasource_id` 等于当前数据源、结果仍可读（`load_meta` 成功且 `rows_sha256` 校验通过）且未过复用保留期（存储时钟判定，§7.12.1；不满足的不携带，也不阻塞轮换）。**按链首原始证据去重**——同一原始结果的多次登记（含多次轮换的沿用副本）只携带一份（以当前持有的最完整副本为准，链首身份由其 `reused_from` 解析），避免“最近三条全是同一结果的沿用副本”。原线程 checkpoint 已删除不影响携带与沿用（§7.12.1 链首解析不回查）。
   - `prior_result` 证据不携带（它绑定具体前序结果，新线程需要时重新使用占位符）。
3. 写入 `sessions` 记录：序号 +1，新 `thread_id`，`carried_evidence` 保存在新线程条目下。之后新线程的每次运行，Runner 都把它装入 `DbAgentContext.carried_evidence`（只读，模型不能修改）。
4. 新线程第一轮输入为两条消息：一条 `HumanMessage`（`additional_kwargs={"dbda_kind": "handoff"}`），内容以 `【会话交接】` 开头，列出上述摘要与引用，以及携带证据清单（每条的原始证据 ID、时间区间或表列取值、原文来源），并说明“需要沿用时调用 reuse_evidence”；随后是用户本轮真实问题。P0 将该消息作为 `handoff_message` 交给 P1 组装输入（§14.3）；普通新会话或未轮换的后续轮次不注入交接消息，已有消息由 checkpoint 恢复，不重复提交。
5. 结果引用属于 `session_id`，新线程可直接读取（P11）。证据账本仍按 `run_id` 作用：新查询要沿用旧口径时，Agent 用 `reuse_evidence`（§7.12）把携带证据登记到本运行，服务端按结构化记录复制，**不重新计算**（相对时间如“本月”保留原区间）。对旧结果的下推使用结果自带的 `guard_evidence` 快照（§9.6），不需要读取旧线程。

示例：旧线程“统计 2026 年 9 月销售额”登记了 `[2026-09-01, 2026-10-01)`；轮换后用户问“再按客户分组”，新线程原文不含时间表达，`resolve_time_range` 会拒绝。Agent 从交接清单中取得该时间证据的 ID，调用 `reuse_evidence` 登记到本运行，SQL 即可使用原区间。

禁止以删除 checkpoint 祖先或只保留最新行的方式压缩线程（概要设计 §9.3）。

### 13.5 清理（`persistence/maintenance.py`）

命令：`cli maintenance cleanup [--dry-run|--apply] [--before YYYY-MM-DD]`，默认 `--dry-run`；显式传 `--apply` 才执行清理，两个模式互斥。`--before` 为可选日期筛选参数，不绕过活动线程、待恢复任务与结果引用的保护检查。命令参数与 §15.1 保持一致。

1. **线程级判定（doc 7 §2 折入）**：候选＝序号推导（`dbda:<session_id>:<seq>`，按 `sessions.thread_seq` 反推）＋ checkpoint 枚举兜底（参数化＋LIKE 通配符转义 `\`/`%`/`_`，`ESCAPE '\'`）。逐线程：① 当前线程（`== sessions.current_thread_id`）保护；② `get_state(config).next` 非空保护待恢复任务，**`get_state` 异常时跳过该线程并报告"状态读取失败"**（时间兜底不替代待恢复检查）；③ 过期判定 `effective_at = max(last_active_at, retired_at, 最新 checkpoint 时间)`——checkpoint 时间优先公开 `StateSnapshot.created_at`，已确认无待恢复任务但 `created_at` 缺失时以 `checkpoints.checkpoint->>'ts'` 最大值兜底，统一 UTC 忽略空值，三来源全空不清理；`retired` 只作清理优先级排序；`--before` 只收紧不放宽。**会话归属复核**：候选 `thread_id` 经 `parse_thread_id(thread_id) -> tuple[str, int] | None` 解析（`rsplit(":", 1)`，`dbda:` 前缀＋正整数序号），调用方比较返回会话 ID 与预期会话精确一致才保留；会话 ID 字符集限制 `^[A-Za-z0-9_-]+$`（禁冒号）。
2. 预览列出线程数、checkpoint 行数估计、待删结果目录。
3. 执行时对每个线程调用 `PostgresSaver.delete_thread(thread_id)`（K16）。
4. 结果目录：早于 `result_retention_days`，且不被保留中的线程状态、`qa` 归档引用时才删除。
5. `qa` 与 `run_errors` 按 `archive_retention_days` 独立清理；checkpoint 过期不删除问答档案。

**运行与清理互斥（doc 7 §2.3 折入）**：会话级 advisory lock `pg_try_advisory_lock(hashtext('dbda_session:' || session_id))`。Runner **进入 P0 读取 sessions／checkpoint 之前**取锁（专用连接持有）至 P2 读取、P3 归档与轨迹写入完成，外层 `finally` 释放；取锁失败按 runtime 失败返回且**不做任何 sessions／threads／qa 写入**（防覆盖运行中进程指针），仍写 P4 本地记录。清理逐会话 `pg_try_advisory_lock`，拿不到跳过该会话（报告"运行中"），拿到后**重新核对**上述资格再删除，完成或异常后解锁。memory 持久化模式无 PG，跳过互斥。

**清理连接与事务**：

- `cleanup --apply` 使用独立、短生命周期的 PostgreSQL 连接，无需新建常驻池；复用平台连接参数、目标库与 schema 核对逻辑，固定同一个 `search_path=db_deepagent`，使用 `autocommit=True`、`prepare_threshold=0`、`dict_row`。不修改交互平台池的 `statement_timeout_ms=5000`，不在共享连接上执行会话级 `SET`。
- 每个线程建立一个显式事务，事务内以 `SET LOCAL` 设置 `maintenance.statement_timeout_ms`（初始 60000 ms）与 `maintenance.lock_timeout_ms`（初始 5000 ms），然后调用绑定**该连接**的 `PostgresSaver(conn).delete_thread(thread_id)`。不能改用绑定共享池的 Saver，否则删除可能发生在另一连接、脱离当前事务和超时设置。
- SDK 对 `checkpoints`、`checkpoint_blobs`、`checkpoint_writes` 的删除均在该事务内完成；事务提交后才报告该线程清理成功、更新其清理状态。任一句超时、锁等待超时或其他数据库错误，回滚该线程全部删除并记录失败；不把部分删除或未提交结果标为成功。
- 按线程独立提交，不将整个批次包在一个大事务中。已成功线程保留提交结果，失败线程可在后续维护命令中重试；连接失效时先关闭并重建同样配置的连接。批次有失败时维护命令以非零码结束，并列出成功／失败线程及原因。
- 每线程操作前重新核对活动状态与待恢复任务，结果文件和归档删除仍须满足引用与保留规则。时间限制是每条语句及锁等待的限制，不承诺整个批次 60 秒内结束；线程体量与删除耗时需实测，不能由消息数直接推算。

## 14. Runner 与结果协议

### 14.1 最终答案（模型通过 `submit_final_answer` 提交，§7.11）

```python
class FindingItem(StrictToolArgs):
    text: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1)

class DbAgentFinalAnswer(StrictToolArgs):
    """数据库 Agent 的最终答案。只引用工具返回过的 evidence_id。"""
    status: Literal["completed", "partial", "needs_clarification", "no_data", "failed"]
    answer: str = Field(min_length=1, max_length=8000)
    findings: list[FindingItem] = []
    metric_definitions: list[str] = []      # 采用的口径
    limitations: list[str] = []             # 截断、缺失、口径限制
    open_questions: list[str] = []          # 待澄清或未完成事项

class SubmitFinalAnswerArgs(DbAgentFinalAnswer):
    runtime: Annotated[Any, InjectedToolArg] = Field(exclude=True)  # 仅服务端注入，见 §7.1
```

### 14.2 返回协议

```python
class EvidenceItem(BaseModel):
    evidence_id: str
    kind: Literal["sql_query", "result_analysis"]
    tool_call_id: str
    source: str                              # datasource_id，不含凭据
    sql: str | None                          # 实际执行的 SQL 全文（executed_sql）
    sql_template: str | None = None          # 含结果占位符时的授权原文；无占位符时为 None
    result_ref: str | None
    input_result_refs: list[str] = []
    operation: dict | None = None
    row_count: int                           # 实际保存行数
    preview_truncated: bool
    source_truncated: bool
    warnings: list[str] = []
    reused_from: EvidenceOrigin | None = None   # 受控沿用登记的链首原始来源；本运行执行为 None（§7.12.1）
    result_generated_at: str                    # 结果生成时间 ISO；所有记录都填，沿用记录为原结果生成时间（§7.12.1）

    @model_validator(mode="after")
    def _check_kind(self):
        # sql_query：sql、result_ref 必填，operation 必须为 None，input_result_refs 为空
        # result_analysis：operation 必填，input_result_refs 至少一项，result_ref 必填；sql 可为 None
        ...

class RunStats(BaseModel):
    elapsed_ms: int
    model_calls: int                         # 主 Agent 模型步骤预占数，来自预算快照
    summary_model_calls: int                 # 摘要模型调用尝试数，来自回调
    total_model_calls: int                   # 主模型 + 摘要的可观察调用尝试数，非 HTTP 请求数
    tool_calls: int                          # 成功预占的工具调用总数，budget_snapshot.counters["tool_call"]
    input_tokens: int | None                 # 回调聚合；任一调用用量未知时为 None
    output_tokens: int | None
    token_usage_complete: bool               # 是否所有已观察调用都有可计入的用量

class DbAgentResult(BaseModel):
    session_id: str
    thread_id: str | None                    # P0 未能确定线程时为 None（§14.3）
    request_id: str; run_id: str
    status: Literal["completed", "partial", "needs_clarification", "no_data",
                    "failed", "budget_exhausted", "persistence_error"]
    answer: str
    findings: list[FindingItem]
    evidence: list[EvidenceItem]
    metric_definitions: list[str]
    limitations: list[str]
    open_questions: list[str]
    errors: list[ToolErrorRecord | RunFailureRecord]   # RunFailureRecord 含 phase、kind、primary
    warnings: list[str]
    archived: bool                                      # P3 归档是否成功（§14.3）
    stats: RunStats
```

### 14.3 Runner 流程（`runner.py`）

Runner 把一次请求分成五个阶段。每个阶段的异常都被捕获，按发生顺序记入 `RunFailures`，不会被后续处理覆盖。最终状态按下文的**优先级**判定；`primary` 标在“决定最终状态的那一类失败中最早发生的一条”上，不一定是全部失败中的第一条：

| 阶段 | 内容 | 涉及持久化 |
| --- | --- | --- |
| P0 准备 | 读 `sessions` 记录、确定线程与轮换、读取线程已有用户原文（`agent.get_state`）、装入携带证据 | 是 |
| P1 运行 | `agent.invoke` | 是（checkpoint 读写） |
| P2 读取 | `agent.get_state` 读取账本与最终答案 | 是 |
| P3 归档 | 写 Store `qa`／`run_errors`、更新 `sessions` 最近活动 | 是 |
| P4 本地记录 | 写 `<workspace>/sessions/<session_id>/runs/<request_id>.json` | 否（本地文件） |

```text
DbAgentRunner.run(question, session_id, thread_id=None, request_id=None) -> DbAgentResult
 初始化（进入 P0 之前，任何阶段失败都能据此组装结果）：
     started = time.monotonic()
     request_id = 传入值或 "req_" + uuid4().hex[:12]；run_id = "run_" + uuid4().hex
     thread_id = 传入值或 None（P0 确定后赋值）
     failures = RunFailures()
     budget_snapshot = BudgetSnapshot.not_started()   # counters 全为 0，stop_reason=None，started=False
     model_snapshot = ModelObservationSnapshot.not_started()   # 调用计数与 Token 均为 0，token_usage_complete=True
     values = None；answer = None；evidence = []；warnings = []；archived = False
     handoff_message = None
     observer = None；budget_opened = False；invoke_started = False
 P0  try: 确定线程（§13.4）；轮换时构造 handoff_message，其他情况保持 None；
          读取用户原文（排除带 dbda_kind 标记的系统注入消息）；构造 DbAgentContext
     except 持久化异常 → failures.add("persistence", phase="P0")；跳到 P4（不调用模型）
     except 其他异常   → failures.add("runtime", phase="P0")；跳到 P4
 P1  try:
         input_messages = ([handoff_message, HumanMessage(question)] if handoff_message is not None
                           else [HumanMessage(question)])   # 轮换首轮两条，普通轮次一条
         observer = ModelCallObserver(request_id, run_id, audit_path)   # 构造异常也属于 P1
         config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 200,
                   "callbacks": [observer],
                   "metadata": {"request_id": request_id, "dbda_run_id": run_id, "dbda_call_kind": "agent"}}
         services.budget.open_run(run_id, limits, ctx.deadline_monotonic)
         budget_opened = True
         invoke_started = True
         agent.invoke({"messages": input_messages}, config=config, context=ctx)
     except 持久化异常 → failures.add("persistence", phase="P1")
     except 其他异常   → failures.add("runtime", phase="P1")
     finally:
         if budget_opened:
             try: budget_snapshot = services.budget.close_run(run_id)
             except 其他异常 → failures.add("runtime", phase="P1.close_run")
         if observer is not None:
             try: model_snapshot = observer.finalize()   # 成功或异常均保留已观察尝试
             except 其他异常 → failures.add("runtime", phase="P1.observer_finalize")；标记模型用量不完整
         # 两个收尾动作独立捕获错误，不覆盖原异常，也不阻止另一个动作与 P4 执行
     P1 出现持久化异常 → 跳到 P4（不再调用 get_state，避免同一故障重复抛错）
     invoke_started 为 False → 跳过 P2 与答案评估，进入状态判定（不能读取旧答案冒充本轮结果）
 P2  try: values = agent.get_state(config).values
     except 持久化异常 → failures.add("persistence", phase="P2")；跳到 P4
     except 其他异常 → failures.add("runtime", phase="P2")；跳到 P4
     P1 为 runtime 异常时，P2 只为诊断读取已写入的账本，不读取最终答案
 评估（仅当 P1、P2 均无异常）：
     组装 evidence：只取 run_id == 本次的 db_executions（purpose="query"）与 db_analyses（两者均含
       §7.12.1 沿用登记：reused_from／result_generated_at 照抄入 EvidenceItem；沿用记录的 elapsed_ms
       不入本轮耗时汇总）；sql 取 executed_sql，含占位符时另填 sql_template；探针与下推执行记录只出现在运行轨迹中
     answer = values["db_final_answers"].get(run_id)；缺失 → 记“未提交最终答案”
     findings 中引用不存在的 evidence_id → 移除该引用、追加 warning（§7.11 已检查过，这里是后备）；
       某条 finding 没有剩余有效引用 → 该 finding 标注“无证据支持”，status 不得为 completed
 状态判定（按优先级取第一个成立的）：
     1. failures 中有 persistence                     → persistence_error
     2. failures 中有 runtime，或 stop_reason == tool_set_mismatch → failed
     3. budget_snapshot.stop_reason 为预算或时限      → budget_exhausted
     4. 未提交最终答案                                 → failed
     5. 模型 status，按证据检查降级；模型报 completed 但本次无任何成功证据且不是 needs_clarification/no_data → failed
     第 2 条由 stop_reason 触发、第 3、4 条成立时，各追加一条对应的 RunFailureRecord
     （kind = tool_set_mismatch／budget_exhausted／no_final_answer），使 primary 总有可标记的记录
 P3  仅当状态不是 persistence_error 时归档（§13.3）
     except 持久化异常 → failures.add("persistence", phase="P3")；状态改为 persistence_error，archived=False
       （答案与证据仍放入结果供查看，warnings 注明“本次结果未归档”）
     成功 → archived=True
 P4  外层 finally：所有路径按上述优先级更新最终状态与 primary（含 P0／P1／P2 直达 P4 的失败路径），
     再写本地运行记录（含 failures 全部条目、阶段、异常类型与消息摘要、budget_snapshot、model_snapshot、可得的答案与证据）
     写入失败只记 stderr 日志，不改变状态
 组装结果：
     primary = failures 中与最终状态对应的那一类里最早发生的一条（如 P1 runtime 后又有 P2 persistence，
               状态为 persistence_error，primary 标在 P2 那一条上）
     未取得的部分用初始化值：thread_id 可为 None；answer 为空串；findings、evidence 为空；
     stats.elapsed_ms = time.monotonic() - started；model_calls = budget_snapshot.counters["model_call"]；
     tool_calls = budget_snapshot.counters["tool_call"]（两者未启动时均为标量 0，按名明细留在轨迹）；
     summary_model_calls、total_model_calls、Token 与 token_usage_complete 取 model_snapshot；
     用量不完整时追加 warning，已知部分仅留审计，不冒充完整合计
 返回 DbAgentResult
```

- **持久化异常的判定**：`psycopg.Error`、`psycopg_pool.PoolTimeout`、`PlatformSchemaError`。业务库错误在工具内部全部转换为错误包（§7.1），不会从 `invoke` 抛出，所以从 `invoke`／`get_state`／Store 调用中冒出的这些异常只可能来自平台库。
- **优先级的含义**：平台库故障时，后续出现的“未提交答案”或预算停止都不能覆盖 `persistence_error`；归档失败也会把原本成功的请求标为 `persistence_error`，会话明确失败，本地记录保留诊断信息。
- **交互模式**：某一轮为 `persistence_error` 时，CLI 显示失败与本地记录路径，不自动切换到内存模式；下一轮重新从 P0 开始，平台库仍不可用就再次失败。
- **准备与收尾也受异常保护**：实际实现用外层 `try/finally` 保证 P4 执行；P1 的输入／配置构造、观测器构造与 `open_run` 全部位于 P1 的 try 内。仅成功打开预算才关闭，仅成功构造观测器才封存；准备阶段失败不调用模型和 `get_state`。收尾失败追加运行错误，不覆盖原始失败；没有拿到观测快照时标记统计不完整，不能沿用初始化的“完整且零用量”结论。

`evidence` 的事实字段全部来自服务端记录：`sql`、`row_count`、截断标记、`result_ref` 均取自 `ExecutionRecord`／`AnalysisRecord`；模型只提供 `evidence_ids` 引用（概要设计 §10.2）。失败的工具调用只进入 `errors`，不出现在 `evidence`。

## 15. CLI（`cli.py`）

### 15.1 命令

```bash
# 单次查询
.venv/bin/python -m src.modules.db_deepagent.cli query "统计 2026 年 9 月订单数量" [--json] [--plain] \
    [--session-id S] [--thread-id T] [--request-id R] [--show-trace]
# 兼容概要设计示例：直接给出问题即等价于 query
.venv/bin/python -m src.modules.db_deepagent.cli "统计 2026 年 9 月订单数量"
# 交互模式（无问题参数）
.venv/bin/python -m src.modules.db_deepagent.cli [chat] [--session-id S]
# 环境与装配自检
.venv/bin/python -m src.modules.db_deepagent.cli doctor [--stage env|retrieval|all] [--json]
# 维护
.venv/bin/python -m src.modules.db_deepagent.cli maintenance init-platform
.venv/bin/python -m src.modules.db_deepagent.cli maintenance cleanup [--dry-run|--apply] [--before YYYY-MM-DD]
# 固定题集验收（说明与产物见 §18.4）
.venv/bin/python -m src.modules.db_deepagent.cli acceptance run [--rounds 3] \
    [--fixed-now 2026-10-13T12:00:00+08:00]
```

通用参数：`--config <yaml>`（默认模块内配置）、`--fixed-now <ISO>`（覆盖 `clock.fixed_now`，用于验收）、`--memory-persistence`（显式选择内存模式）。

交互模式命令：`/rotate`（轮换线程）、`/trace`（显示上一轮工具调用轨迹）、`/result <ref> [offset]`（经 `ResultStore` 分页查看，同样做会话检查）、`/exit`。

### 15.2 显示

- Rich 模式（默认，检测到 TTY 时）：答案面板；发现列表（附 `evidence_id`）；证据表（SQL、行数、两种截断标记、`result_ref`；`reused_from` 非空的行标注“沿用（原 run_id、原结果生成时间）”，§7.12.1）；限制与待澄清事项；统计行（耗时、主 Agent 模型步骤数、摘要调用尝试数、总调用尝试数、工具调用、Token）。用量不完整时显示“未知／不完整”，不显示为零；总调用尝试不标成 HTTP 请求数。`--show-trace` 额外显示工具调用顺序、参数摘要与状态。
- `--plain`：同样内容的纯文本。
- `--json`：输出 `DbAgentResult.model_dump_json()`，不混入其他文本（日志走 stderr）。

**JSON 输出隔离**（`--json` 模式下 stdout 只出现一个完整 JSON 文档）：

1. **启动顺序**：①轻量预扫描 `argv` 识别输出模式——只判断 `--json` 是否出现（含位于子命令之后），**尊重 `--` 分隔符**（其后的 token 是位置参数，如 `cli query -- "--json"` 不是 JSON 模式），不产生任何 stdout 输出；`--help`／`--version` 的 `exit(0)` 属正常帮助退出，不进入错误出口；②`--json` 时初始化与运行全程用 `contextlib.redirect_stdout(sys.stderr)` 包住（覆盖旧 `ConfigLoader` 在 `.env` 加载时的 `[OK]／[WARN]` 直写及其他 stray print），全部 logger handler（含 RichHandler）定向 stderr；③最终序列化在重定向范围之外一次性输出单个 JSON 文档。
2. **参数解析**：主解析器子类化 `ArgumentParser` 覆写 `error()`／`exit()`——出错进入统一 JSON 错误出口，不向 stdout 打印普通用法文本（用法/诊断走 stderr），错误 JSON 也在重定向范围之外输出。
3. **错误协议**：`--json` 下参数错误与初始化失败只在 stdout 输出单个 JSON 错误文档（`{"status": "error", "error": {...}}`），非零码退出（参数错误 2、运行失败 1）；任何异常路径都保证 stdout 可整体 `json.loads`。
4. `--show-trace` 与 `--json` 同给时忽略前者（stderr 提示）；`--plain` 与 `--json` 同给时 `--json` 优先（stderr 提示）；交互模式（`chat`／无参）不支持 `--json`，同给按第 3 条退出（码 2）。
5. **测试**（独立子进程，断言分组）：真正启用 `--json` 的路径（首次启动含 `.env` 加载、未知参数、缺少必填值、非法选项值、`--json` 位于子命令后、初始化失败、正常查询）断言 stdout 可完整解析为单个 JSON 文档；`--` 之后的问题文本含 `--json` 断言**未启用** JSON 模式；`--help` 断言标准帮助输出与退出码 0。

显示代码参考 `scripts/nl2sql_father_cli.py` 的 Rich 渲染函数复制改造；旧脚本保持原样（概要设计 §10.1）。

### 15.3 `doctor`

逐项输出 通过／失败 与原因。按阶段分组，与 §19 的实施步骤对应，每个阶段只依赖该步骤之前已实现的组件：

| 阶段 | 检查项 | 可用时机 |
| --- | --- | --- |
| `env` | SDK 及核心依赖版本（含 `sqlglot` 锁定版本）；配置校验；业务库连接、`current_database()`、只读设置与连接池回调；平台库连接、`current_schema()` 与表位置；模型构造、`request_timeout` 设置、HarnessProfile 匹配键与命中结果 | 步骤 1 结束 |
| `retrieval` | Milvus 连接（别名 `db_deepagent`）与集合存在；Embedding 一致性（§6.3，输出发布批次）；Neo4j 连接与 APOC 可用性 | 步骤 2 结束 |
| `all`（默认） | 以上全部，加装配后验证（§10.6：工具集合按阶段清单常量——步骤 4 为 13 件、步骤 5 为 15 件（`write_todos` 条件 +1）、`DeltaChannel`、无 `files` 通道、`submit_final_answer` 设置） | 步骤 4 结束（13 件口径；完整 15 件核对随步骤 5 清单切换生效） |

不调用模型生成，不执行业务查询。

## 16. 错误处理与日志

### 16.1 异常分层

| 层 | 异常（`errors.py`） | 对外表现 |
| --- | --- | --- |
| 初始化 | `DbAgentConfigError`、`DatasourceMismatchError`、`PlatformSchemaError`、`EmbeddingConsistencyError`、`HarnessProfileError`、`AssemblyVerificationError` | CLI 打印原因并以非零码退出 |
| 工具 | 一律转换为错误包（§7.1） | 模型可见，可据此调整行动 |
| 运行 | 持久化异常、模型调用异常 | `DbAgentResult.status` 为 `persistence_error`／`failed` |

### 16.2 日志

- 日志器名前缀 `nl2sql.db_deepagent.<子模块>`，复用 `src/configs/logging.yaml` 的处理器（C9）；消息前缀 `[request_id/run_id]`，用 `with_query_id` 适配器实现。
- 记录内容：工具名、参数摘要（SQL 记录全文，问题文本截断到 200 字）、状态、耗时、错误类型；Milvus／Neo4j 查询参数、检索生效阈值与命中分数；摘要生效配置（含 `trim_tokens_to_summarize`）；授权记录的创建、匹配、撤销。
- 不记录：密码、API key、完整 conninfo、完整结果行（最多记录前 3 行，且仅在 DEBUG）。

### 16.3 运行记录

每个请求在 `<workspace>/sessions/<session_id>/runs/<request_id>.json` 保存：用例 ID（验收时）、问题、`DbAgentResult`、工具调用轨迹（来自账本与审计日志）、实际 SQL、预算步骤快照、模型回调观测快照、Token 完整性与时延。满足验收题集 §5 第 5 条的记录要求。Store 归档中的调用次数、Token 摘要使用同一组装结果，不从消息列表另算，避免漏掉摘要调用。

## 17. 安全边界汇总

| 边界 | 落实位置 |
| --- | --- |
| 业务库只能是 `orders`，模型不能改连接 | §4.2、§6.1；工具参数中没有连接字段 |
| 未经校验不能执行；修改后必须重验 | §8.2–§8.3 授权键 |
| 业务 SQL 只读 | 安全层关键字与语句类型检查、`default_transaction_read_only`、`SET TRANSACTION READ ONLY`、建议的只读数据库角色 |
| 超时与读取上限 | `SET LOCAL statement_timeout`、`fetchmany(max+1)`；模型、Embedding、Milvus、Neo4j 各自的请求超时（§11.1） |
| 运行预算不被并行调用突破 | `RunBudgetLedger` 加锁预占（§11.1） |
| 最终答案只引用本运行的有效证据 | `submit_final_answer` 检查与 Runner 后备核对（§7.11、§14.3） |
| 时间／维度语义守卫 | §9 |
| 下推与探针不绕过管线 | §8.4 |
| 文件工具只读、范围受限 | §10.3、§10.4 |
| 业务结果不经内建文件工具暴露 | 结果目录不在 backend 内；业务工具输出不触发 SDK 转存（§7.1） |
| 跨会话结果不可读 | §6.8 |
| 单 Agent，无委派 | §10.2、§10.6、§11.2 |
| 持久化不静默降级 | §13.2 |

## 18. 测试设计

### 18.1 目录

```text
src/tests/db_deepagent/
├── conftest.py                      # 公共 fixture：配置、临时工作目录、假服务、FakeQwen 模型
├── fixtures/
│   ├── acceptance_cases.yaml        # 30 项用例（验收题集 §1 规定位置）
│   ├── reference_results/           # 冻结的参考 SQL 与结果（实施时生成并人工复核）
│   └── retrieval_fixtures.yaml      # R04–R06 受控检索夹具
├── unit/
│   ├── test_config.py
│   ├── test_time_resolver.py
│   ├── test_sql_normalize_and_auth.py
│   ├── test_sql_guard.py
│   ├── test_result_store.py
│   ├── test_result_analysis.py
│   ├── test_placeholders.py
│   ├── test_dimension_probe.py
│   ├── test_join_graph_outcomes.py
│   ├── test_milvus_retrieval.py
│   ├── test_schema_tools.py            # 服务端阈值固定，额外 min_score 参数被拒，命中分数与阈值可追溯
│   ├── test_tool_arguments.py          # 全部自定义参数模型严格校验、隐藏 runtime 注入与错误 ToolMessage
│   ├── test_harness_profile.py
│   ├── test_sdk_adapter.py             # SDK 私有摘要状态解析、缺失／异常结构、转存标识不跨线程复制
│   ├── test_backend_permissions.py
│   ├── test_assembly.py
│   ├── test_run_budget.py               # 含并行预占、探针／下推计入执行次数、协作式时限、request_timeout 设置
│   ├── test_model_observer.py           # 主模型／摘要及重试回调、失败尝试、Token 完整性、去重与运行隔离
│   ├── test_maintenance.py              # 专用连接、SET LOCAL、逐线程提交／回滚与批次结果
│   ├── test_final_answer.py             # 提交检查、混合批次拒绝、按 run_id 隔离、提交后结束、漏提交提醒
│   ├── test_sql_pipeline_handoff.py     # 探针／下推单次调用内的可信记录传递与一次性账本更新
│   ├── test_business_db_pool.py         # configure 回调后连接为 IDLE；输出列名重复检查
│   ├── test_reuse_evidence.py
│   ├── test_runner_persistence.py       # 各阶段平台库故障与状态优先级
│   ├── test_thread_rotation.py          # 普通输入一条消息；轮换首轮 handoff + 用户问题；用户原文排除 handoff
│   ├── test_cli.py                      # acceptance run、cleanup 模式与 --before 参数解析
│   ├── test_runner_evidence.py
│   ├── test_summary_prompt.py
│   └── test_summary_context.py         # 超过 4000 Token 的待摘要历史及前次摘要均送入摘要模型
└── integration/
    ├── test_model_capability.py         # requires_llm：业务工具调用 → 接收反馈 → submit_final_answer
    ├── test_model_summary_window.py     # requires_llm：接近配置触发阈值的摘要请求能完成并记录实际输入与耗时
    ├── test_business_db_readonly.py     # requires_db
    ├── test_platform_persistence.py     # requires_db
    ├── test_maintenance_cleanup.py      # requires_db：三表删除的事务回滚、锁超时与重试
    ├── test_embedding_consistency.py    # requires_db（Milvus）
    ├── test_protocol_cases.py           # P01–P12
    ├── test_regression_cases.py         # R01–R06（requires_llm）
    └── test_business_cases.py           # F01–F12（requires_db + requires_llm）
```

所有测试使用现有标记 `unit`、`integration`、`requires_db`、`requires_llm`，由 `src/tests/conftest.py` 的 `--run-db`／`--run-llm` 控制（C10）。

### 18.2 关键测试替身

- **FakeQwen**：继承 LangChain 的 `GenericFakeChatModel`，覆写 `_get_ls_params` 返回 `ls_provider="tongyi"`，并设置 `model_name`，使其 HarnessProfile 匹配键与真实 `ChatTongyi` 同形；按脚本产出工具调用。`BaseChatModel.bind_tools` 默认抛 `NotImplementedError`，`GenericFakeChatModel` 也没有覆写，所以 FakeQwen 要覆写 `bind_tools`：记下传入的工具名与 `tool_choice` 供断言，然后返回自身。单元测试据此断言模型请求中工具集合等于 §10.4、且没有强制 `tool_choice`。用于装配、工具列表、协议、最终提交与 Runner 单元测试，不需要网络。
- **假业务库**：单元测试中 `BusinessDbService` 用接口替身；集成测试使用隔离的 PostgreSQL 测试库，结构与 `orders` 只读快照一致，测试账号只有 `SELECT` 权限。
- **固定时钟**：所有时间相关用例使用 `fixed_now=2026-10-13T12:00:00+08:00`（验收题集 §1）。

### 18.3 验收用例映射

| 用例 | 主要测试 | 核心断言 |
| --- | --- | --- |
| F01–F12 | `test_business_cases.py`（参数化读取 `acceptance_cases.yaml`） | 结果与冻结参考值按验收题集 §5 第 4 条比较；evidence 与工具记录一致；回答引用有效 |
| F08 | 另含 `test_placeholders.py` | 后续 SQL 的客户范围等于前序结果；截断源拒绝展开 |
| F11 | 另含 `test_result_store.py` | 分页、`next_offset`、`source_truncated` 跨页保留 |
| F12 | 另含 Runner 多轮测试 | 第二轮输入只有新增用户消息；口径继承 |
| R01–R03 | `test_time_resolver.py`、`test_sql_guard.py`、`test_regression_cases.py` | 裸“当前”被拒；“今天”“本月”边界精确；空区间与无证据时间字面量被守卫拒绝 |
| R04–R06 | `test_sql_guard.py`、`test_dimension_probe.py`、`test_regression_cases.py`（受控夹具） | 无证据值与模式过滤被拒；真实值放行；探针后放行且探针 SQL 有校验与执行记录 |
| P01 | `test_protocol_cases.py`、`test_business_db_readonly.py` | 未校验执行被拒；写入、DDL、多语句校验失败；绕过授权直接调用执行原语时只读事务报 `readonly_violation`；前后业务表行数与校验和不变 |
| P02–P06 | `test_sql_normalize_and_auth.py` | 多记录并存；首尾空白可通过、内部变化必须重验；伪造参数被 Schema 拒绝；作用域变化拒绝；重验失败撤销；新 `run_id` 需重验 |
| P07 | `test_result_analysis.py`、`test_protocol_cases.py` | 下推产生校验与执行记录；注入校验失败、超时、非只读 SQL 时返回错误且无直接执行；本地计算无 SQL 记录 |
| P08 | `test_platform_persistence.py` | 每条池连接 `current_schema()='db_deepagent'`；表与迁移记录位于该 Schema；`public` 旧表不变；Schema 缺失时报错不回退 |
| P09 | `test_backend_permissions.py` | 允许路径可读；`/`、未列路径、`/.env`、`/artifacts/.x`、越界符号链接被拒；写入、删除、`execute` 不在可分派工具中；结果目录不可经 `read_file` 访问 |
| P10 | `test_result_store.py` | 跨会话 `result_access_denied`；截断状态区分；截断结果上的总体本地计算返回 `incomplete_source` |
| P11 | `test_platform_persistence.py`、`test_assembly.py` | `messages` 为 `DeltaChannel`；超过 50 次通道更新后重启进程可恢复完整消息；触发摘要后 `/artifacts/conversation_history/*.md` 可读；轮换后新线程能读取旧结果引用 |
| P12 | `test_harness_profile.py`、`test_assembly.py` | 正确键命中；错误 `expected_harness_key` 初始化失败；实际模型请求工具名等于 §10.4 期望集合 |

验收用例之外的必测项：

| 项 | 测试 | 核心断言 |
| --- | --- | --- |
| 最终提交 | `test_final_answer.py` | 混合批次返回 `final_answer_not_alone` 且不登记、其他工具照常执行；无效引用被拒且运行不结束；接受后下一次 `before_model` 结束运行；第二轮读不到第一轮答案；漏提交时只提醒 `final_answer_reminders` 次 |
| 预算 | `test_run_budget.py` | 同一步 N 个并行工具在剩余额度 k < N 时恰好 k 个通过；`execute_sql`、探针、下推共用 `db_execute` 额度；超时后下一预占点结束运行 |
| 管线衔接 | `test_sql_pipeline_handoff.py` | 探针与下推单次调用即可成功执行；返回的 `Command` 同时含 `db_validations` 与 `db_executions`（下推再含 `db_analyses`）；`runtime.state` 未被修改 |
| SQL 与快照 | `test_result_store.py`、`test_placeholders.py` | 执行记录与 `meta.json` 同时有 `sql_template` 与 `executed_sql`；`meta.guard_evidence` 与授权记录中的 `guard_evidence` 相同（含占位符证据与下推输入快照条目）；evidence 的 `sql` 为展开后全文 |
| 连接池与结果列 | `test_business_db_pool.py`、`test_business_db_readonly.py` | `configure` 回调返回后 `transaction_status == IDLE`；`SELECT o.id, r.id` 静态检查返回 `duplicate_column_names`；`SELECT o.*, r.*` 在执行期被拦截且不保存结果；结果行按位置保存，同名列数据不丢失 |
| 探针截断 | `test_dimension_probe.py` | 取值数恰为 `n` 时 `values_truncated=false`，为 `n+1` 及以上时为 `true`；返回与证据最多 `n` 个值；生成 SQL 的 `LIMIT` 为 `n+1`，改为 `n` 时 `probe_shape_mismatch` |
| 证据沿用 | `test_reuse_evidence.py` | 只接受本线程早前运行与携带证据中的 ID，不接受当前会话访问范围之外的 ID；不存在的 ID 整体拒绝；在 2027 年沿用已解析的 2026 年“今年”证据时，区间仍为 `[2026-01-01, 2027-01-01)`；原 `timezone` 不受当前时区配置变化影响；`inherited_from` 完整；`prior_result` 不可沿用 |
| 执行／分析证据沿用（§7.12.1） | `test_reuse_evidence.py` | E／A 分别去重（同 ID 同事实保留首条、排除 `tool_call_id`）及同 ID 异事实的一致性错误；连续沿用（保留链首 `reused_from`）、并行沿用（reducer 幂等）与混批失败（任一证据不可用整批拒绝）；原 checkpoint 已删除、仅携带副本在场且原结果仍有效时沿用登记成功；`result_ref` 过期（存储时钟注入：恰好到期／过期一天／未到期边界）返回 `result_expired` 且登记不续期；分析记录以输出元数据核对数据源 |
| 沿用归档与状态 | `test_runner_persistence.py`、`test_archive.py` | 纯沿用请求满足条件 b（答案被接受＋引用有效 E／A）时入 `qa`；仅 T／D 沿用、无答案、引用无效入 `run_errors`；`budget_exhausted`／`no_data`／`needs_clarification` 按真实状态入 `qa` 不改写；沿用记录不占 `db_execute` 预算、`elapsed_ms` 不入本轮耗时汇总；`qa` evidence 摘要含 `reused_from` 标记 |
| CLI JSON 隔离 | `test_cli.py` | 独立子进程断言分组：真正启用 `--json` 的路径（首启含 `.env` 加载、未知参数、缺少必填值、非法选项值、`--json` 位于子命令后、初始化失败、正常查询）stdout 可完整解析为单个 JSON 文档；`--` 之后的问题文本含 `--json` 断言未启用 JSON 模式；`--help` 断言标准帮助输出与退出码 0（§15.2） |
| 数据源一致性 | `test_reuse_evidence.py`、`test_placeholders.py`、`test_result_analysis.py` | 用不同 `datasource_id` 构造上下文：沿用维度证据返回 `evidence_datasource_mismatch`；沿用时间证据成功且保留原 `datasource_id`；占位符引用他源结果返回 `placeholder_datasource_mismatch`；他源结果下推、两输入数据源不同均返回 `result_datasource_mismatch`；他源结果本地计算成功且输出保留原 `datasource_id` |
| 最近 N 天 | `test_time_resolver.py` | “最近 7 天”+`n=7` 得 `[2026-10-07, 2026-10-14)`；“最近 7 天”+`n=30` 返回 `time_params_mismatch`；“近十五天”提取为 15；“最近几天”“最近 2 周”返回 `time_params_mismatch` |
| 持久化生命周期 | `test_runner_persistence.py` | 分别在 P0、P1、P2、P3 注入平台库异常：状态均为 `persistence_error`；P0 故障时 `thread_id=None`、预算计数为 0、结果可正常序列化，且不调用 `open_run`／`close_run`；P1 故障后不调用 `get_state`；“未提交答案”与预算停止不覆盖原始错误；P1 runtime 后 P2 persistence 时 `primary` 标在 P2 条目上；P3 故障时 `archived=false` 且答案仍在结果中；P4 本地记录写入并包含全部失败条目 |
| 摘要私有适配与轮换输入 | `test_sdk_adapter.py`、`test_thread_rotation.py` | 缺失／None 摘要事件返回 None；有效事件提取文本，异常结构抛 `AssemblyVerificationError`；普通轮次仅输入本轮问题，轮换首轮依次输入带 handoff 标记的交接消息与用户问题；`user_texts` 不含交接消息；新线程不继承旧摘要事件与转存标识 |
| CLI 参数一致性 | `test_cli.py` | `acceptance run --rounds 3 --fixed-now ...` 可解析并传给验收入口；`cleanup --before YYYY-MM-DD` 可解析；无模式参数默认预览，`--apply` 执行，`--dry-run` 与 `--apply` 同传被拒，非法日期被拒 |
| 单边时间区间 | `test_time_resolver.py`、`test_sql_guard.py` | `before`／`through`／`from`／`after` 分别得到 §7.5 的边界；方向、包含关系、日期或年份依据不足时拒绝；恰有一端为 None；匹配单边证据放行，有限证据省略一端、反向使用边界、未登记的有限组合与跨 OR 拼接被拒 |
| 单边证据保存与沿用 | `test_reuse_evidence.py`、`test_thread_rotation.py`、`test_result_store.py` | 快照序列化、读取、轮换去重、跨数据源时间证据沿用均保留 None 边界和原时区；不填补端点、不对 None 做日期比较；有限与单边证据不被误合并 |
| 摘要输入完整性 | `test_summary_context.py`、`test_assembly.py` | 装配显式传 `trim_tokens_to_summarize=None`；用捕获输入的摘要模型替身验证：待摘要历史超过 4000 Token 时，早期口径与引用仍进入摘要请求，后续摘要也保留有效历史中的前次摘要；断言输入范围，不假定模型摘要逐字保留全部信息 |
| 摘要窗口实测 | `test_model_summary_window.py` | 在步骤 4 用真实配置模型完成一次接近触发阈值的摘要请求，记录输入规模、耗时及是否发生溢出重试；无法容纳时调低触发阈值后再通过，不以静默裁剪通过 |
| 检索阈值不可覆盖 | `test_schema_tools.py`、`test_milvus_retrieval.py` | 通过 ToolNode 调用：`min_score` 不在模型可见 Schema 中，额外传入时得到匹配 tool_call_id 的 `ToolMessage(status="error")`，文本含 invalid_argument 与 min_score，不按业务 JSON 包断言；业务函数／Milvus 调用次数为 0、不登记证据。正常调用各路使用配置阈值，低于阈值不登记证据；正常返回与日志含生效阈值、命中分数；`query`／`top_k` 调整不改变阈值 |
| 空值检查例外 | `test_sql_guard.py` | 确认文本列的 `= ''`／`<> ''`／`!= ''` 无值证据也放行，不创建证据；普通表列的 IS NULL／IS NOT NULL 放行；空格、IN、LIKE 不获豁免；TRIM／COALESCE 保持拒绝；列归属或类型不明不得套用例外 |
| 空值查询语义 | `test_business_db_readonly.py` | 隔离测试数据包含 NULL、空字符串、空格与普通文本，验证 IS NOT NULL 与 `<> ''` 返回范围不同，守卫没有把空白字符串自动当成空字符串 |
| 模型观测完整性 | `test_model_observer.py`、`test_summary_context.py` | 内部摘要及其重试继承本运行回调与用途；失败尝试计数、调用 ID 去重、不重复累计审计用量；一个主步骤对应多次尝试时 model_calls 与 total_model_calls 允许不同；缺失用量时合计 None 且标记不完整；P0 失败无调用时计数／Token 为 0；并发运行互不串号 |
| 清理连接与原子性 | `test_maintenance.py`、`test_maintenance_cleanup.py` |
| 本地／下推一致性（doc 7） | `test_result_analysis.py` | 精度（`1E+28+1E-28`、`10^28+1`、除法 10 位容忍）；空集/NULL 语义（无分组键 1 行、SUM 空集 NULL）；数据源检查优先级（输入异源/下推异源均 `result_datasource_mismatch`）；单路径规则（describe 截断仍本地、compare 截断 `incomplete_source` 不下推）；路由六分支；文本族整族仅下推；NaN/Infinity；下推非快照 warning；`allow_pushdown=false`＋截断→`incomplete_source`；top_n 无次键不确定性（数量/合法范围/并列保留/warning）；`__dbda_rank` 别名冲突 |
| 清理粒度与互斥（doc 7） | `test_maintenance_cleanup.py`、`test_runner_persistence.py` | 相似会话名（`s_1`/`sA1`、`a`/`a:b`）与冒号拒绝；跨会话 `--thread-id` 被拒；恢复线程 1 后轮换不复用线程 2；`effective_at` 三来源取 max（含轨迹落后于 checkpoint）；`get_state` 异常＋SQL 过期时间仍不删除；持锁会话被清理跳过、Runner 取锁失败不写元数据 | 清理只使用专用连接，SET LOCAL 和绑定该连接的 Saver 处于同一显式事务；注入删除异常、语句／锁超时后该线程三表内容全部保留；已成功线程独立提交，失败可重试；超时不泄漏到交互平台池，批次有失败返回非零码 |
| 严格工具参数与注入 | `test_tool_arguments.py`、`test_assembly.py` | 全部自定义 args_schema 及嵌套对象拒绝额外字段；ToolNode 内合法 runtime 注入成功，模型可见 Schema 不含 runtime **且包含全部约定业务字段与必填项**，按 §7 的独立期望清单核对；空 Schema、字段丢失、runtime 签名注解错误／无法解析的对照均不能通过装配；伪造注入值不能替换服务端真实值；仅业务参数的直接 invoke 或仅包装 ToolCall 不会自动注入 runtime；缺失／类型／额外字段错误均为可纠正的错误 ToolMessage，正文不回显输入或 runtime，业务函数未执行；最终提交的 findings 嵌套额外字段也被拒绝 |
| 回调运行归属 | `test_model_observer.py`、`test_summary_context.py` | 摘要回调 metadata 缺少业务 request_id／dbda_run_id 时，仍以 observer 实例绑定值记录正确归属；用途分别取 config 的 agent 与实例的 summarization；正常与溢出重试均继承回调；模型尝试 ID 不混作业务 run_id，并发运行不串号 |
| 工具总数来源 | `test_run_budget.py`、`test_runner_evidence.py` | RunStats.tool_calls 为标量、等于预算快照 tool_call；未启动为 0，预占失败不增加，已预占但参数校验失败仍计数；不从按名轨迹或 JSONL 重新累计 |
| P1 准备与收尾异常 | `test_runner_persistence.py` | 分别令 observer 构造、open_run、close_run、observer.finalize 抛错，均返回 failed 并执行 P4；构造／打开失败时不调用模型和 get_state，open_run 失败不调用 close_run；某收尾失败不阻止另一收尾；原失败保留，观测封存失败时统计标记不完整 |

上述单边时间、摘要输入、模型观测、阈值、空值与清理约束作为补充的服务／协议测试门禁，文档 4 的固定 30 题及其通过线保持原定义；不能因 30 题未覆盖这些分支而省略补充测试。

### 18.4 验收运行

`.venv/bin/python -m src.modules.db_deepagent.cli acceptance run --rounds 3 --fixed-now 2026-10-13T12:00:00+08:00`（实现于 `cli.py` 的 `acceptance` 子命令，与 §15.1 一致，内部调用 pytest 并汇总），输出每轮通过数、F 类正确率、时延中位数／P95、包含摘要的 Token 中位数，并写入 `<workspace>/acceptance/<时间戳>/`。Token 中位数只使用用量完整的请求，并同时报告完整请求数／总请求数，全部未知时显示不可计算，不以零填补。前置条件（参考结果冻结、Embedding 发布记录）缺失的用例记为“未完成”。

## 19. 实施顺序

对应概要设计 §11.1 的五个步骤，每步完成后对应测试必须通过才进入下一步。每步的自检只覆盖该步及之前已实现的组件（§15.3 的 `doctor --stage`），不要求提前具备后续步骤的组件。

| 步骤 | 交付 | 完成标志 |
| --- | --- | --- |
| 1 环境与配置 | 旧入口基线冻结（§2.3，依赖变更前）；依赖变更；`config.py`、`errors.py`、`context.py`、`state.py`；`BusinessDbService`、平台连接池；模型构造与 `request_timeout`；HarnessProfile 适配；`doctor --stage env`；真实模型能力用例 | `doctor --stage env` 通过；`test_config`、`test_harness_profile`、`test_business_db_pool` 通过；`test_model_capability`（用临时业务工具 + `submit_final_answer` 跑完“工具调用 → 接收反馈 → 最终提交”，检查思考模式设置与实际行为）通过；旧入口复测无新增失败 |
| 2 检索工具 | Embedding、一致性检查、Milvus、TableCatalog、JoinGraph；`search_schema`、`inspect_tables`、`plan_joins` | `doctor --stage retrieval` 通过；对应单元测试与 `test_embedding_consistency` 通过 |
| 3 校验执行闭环 | 校验器、守卫、管线、占位符、时间解析、探针；**基础 `ResultStore`**（`save`、`load_meta`、`read_rows`、`column_values`、会话检查、`guard_evidence` 快照）；`RunBudgetLedger`；`validate_sql`、`execute_sql`、`resolve_time_range`、`probe_dimension_values`、`reuse_evidence` | P01–P06、R01–R06 的服务级断言通过；§9.9 守卫反例全部通过；`test_sql_pipeline_handoff`、`test_placeholders`、`test_result_store`（基础部分）通过 |
| 4 Agent 与 CLI | `agent.py`、三个 middleware、公开模型观测回调、`submit_final_answer`、Skill、提示词、Runner、CLI | `doctor`（`all`）通过（工具集合按步骤 4 清单 13 件口径，§10.6）；`test_assembly`、`test_backend_permissions`、`test_run_budget`、`test_final_answer`、`test_tool_arguments`、`test_runner_persistence`、`test_summary_context`、`test_model_observer`、P09、P12 通过；真实模型完成 F01 及摘要窗口实测（§18.3） |
| 5 分析、持久化与验收 | `analyze_result`（本地计算与下推）、`read_result` 工具、归档、轮换、清理；冻结参考结果；三轮验收 | 装配清单切换为完整 15 件（§10.6）并补 `test_assembly` 断言；`test_maintenance`、`test_maintenance_cleanup` 通过；验收题集 §5 全部通过线达成 |

## 20. 对概要设计的细化与补充

以下是本篇在概要设计基础上新增或具体化的决定，均服务于概要设计已确定的目标：

1. **新增 `resolve_time_range`、`probe_dimension_values` 两个工具。** 概要设计 §8.1 要求代码计算时间边界、受控探针确认维度值，§6.1 的核心工具表未列出承载工具；本篇把它们作为独立工具开放给 Agent。
2. **新增结果占位符 `{{result:<ref>:<column>}}`。** 落实概要设计 §6.3“较大编号列表应由程序提取和校验”；授权仍按含占位符的原文逐字匹配，展开结果用摘要绑定。
3. **新增依赖 `sqlglot`。** §9 守卫需要识别谓词与字面量，`sqlparse` 不足以可靠完成；版本在依赖准备时锁定。
4. **时间字面量必须来自本运行的时间证据，谓词中禁止实时时钟函数。** 这是概要设计“由代码按已确认周期计算边界”在 SQL 守卫上的具体化，也是固定时钟验收可复现的前提。
5. **维度证据覆盖字符串过滤值**（包括状态、渠道等口径值），除 §9.5 明确允许的空值检查外，来源限定为 Milvus 命中、数据库探针、前序结果三种。
6. **运行预算使用自建 middleware。** LangChain 的调用上限 middleware 运行级计数不可在运行后读取（K14），无法支撑“触达上限按失败计分”。
7. **权限规则单独前置拒绝隐藏路径。** 实测 SDK 匹配标志下 `/**` 不覆盖点文件（K7）。
8. **CompositeBackend 默认后端使用空目录 `FilesystemBackend`。** 避免 `StateBackend` 引入 `files` 状态通道（K12）。
9. **最终答案通过 `submit_final_answer` 工具提交，不使用 `ToolStrategy`。** `ToolStrategy` 在主循环每次模型请求中设置 `tool_choice="any"`（K18）；按阿里云 [Function Calling 说明](https://help.aliyun.com/zh/model-studio/qwen-function-calling)，Qwen 非思考模式下 `required` 不能保证一定调用工具，思考模式下不支持，所以改用 `ChatOpenAI` 也不能解决。提交工具按 `run_id` 登记答案，要求单独调用并先核对证据引用（§7.11）；结束运行由 `FinalAnswerGateMiddleware` 判断（§11.3），不用 `return_direct`（K19：提交被拒时它也会结束运行）。这只约束交付格式，规划与工具选择仍由 Agent 自主决定。
10. **`session_id` 与 `thread_id` 分离。** 结果引用归属会话，线程轮换后仍可读取；授权与证据账本按 `run_id` 作用。
11. **服务内部“校验后立即执行”直接传递可信校验记录。** 探针与下推在一次工具调用内完成校验与执行，因图状态要等工具返回后才合并（K20），由管线把刚生成的记录直接交给执行端，账本更新合并后一次性返回（§8.4）。
12. **授权模板与实际 SQL 分开保存。** `sql_template` 用于授权匹配，`executed_sql` 是展开后实际提交的全文；结果元数据、evidence 与下推都用 `executed_sql`（§5.3、§6.8）。
13. **守卫证据快照随结果保存。** 下推与追溯不依赖原线程 checkpoint（§9.6、§9.7）。
14. **守卫按作用域、布尔分支与证据区间判断，列归属不明即拒绝**，并覆盖 `CASE WHEN` 与聚合 `FILTER` 条件（§9.1–§9.5）。
15. **结果占位符只能作为 `IN` 的整个值集合**，用 AST 哨兵检查位置（§8.3）。
16. **预算由加锁的运行级计数器预占**，探针与下推的内部执行计入数据库执行次数；运行时限是协作式的，每类网络调用另设超时（§11.1）。
17. **守卫证据的完整内容随可信校验记录保存**，执行端直接用它生成结果快照，不回查来源（§8.3、§9.7）。
18. **探针多取一行判断截断**：SQL 为 `LIMIT n+1`，最多保存与返回 `n` 个值（§7.6）。
19. **业务结果按列位置保存**：业务连接池用 `tuple_row`，输出列名必须唯一（校验期静态检查 + 执行期检查，§6.1、§8.2）。
20. **常量来源追溯**：条件中的列来自字面量投影时视同字面量检查；常量派生表、`VALUES`、`unnest` 等一律拒绝；只放行时间边界有证据的受控 `generate_series`（§9.2、§9.8）。
21. **线程轮换携带结构化证据**：新增 `reuse_evidence`，服务端按携带记录复制证据，不重算相对时间（§7.12、§13.4）。
22. **Runner 覆盖完整持久化生命周期**：分阶段捕获异常并按发生顺序记录；最终状态按优先级判定（`persistence_error` 最高），`primary` 标在决定状态那一类中最早的一条；进入 P0 前初始化全部默认值；归档失败也明确失败（§14.3）。
23. **证据与结果记录原始 `datasource_id`，复用时检查来源**：维度证据、占位符、下推要求与当前数据源一致，时间证据允许跨数据源沿用；历史来源不被当前值覆盖（§5.3、§7.10、§7.12、§8.3）。
24. **“最近 N 天”的 N 由代码从原文提取**，模型参数必须与之相等（§7.5）。
25. **支持日期级单边时间区间**：`resolve_time_range` 新增 `open_range`，时间证据恰有一端可空；守卫核对有效区间的方向与边界，复用、快照与轮换保留空端点，不擅自补齐（§7.5、§9.4）。
26. **取消摘要前默认 4000 Token 裁剪**：显式配置并传入 `trim_tokens_to_summarize=None`，保留现有触发与近期保留阈值；用实际模型验证摘要输入余量，不将结构化持久化等同于模型可见（§4.3、§10.1）。
27. **检索阈值由服务端配置决定**：从 `search_schema` 移除 `min_score`，严格 args_schema 拒绝额外参数，以框架错误 ToolMessage 呈现；正常返回和日志记录生效阈值及命中分数，低分未命中通过补检索或探针处理（§7.1、§7.2）。
28. **空值检查不强制维度检索**：已确认文本列的 `= ''`／`<> ''` 与合法列的 IS NULL／IS NOT NULL 可免值证据，空格、模式、列表与函数条件不全局豁免（§9.5）。
29. **区分模型步骤预算与全部调用观测**：预算限制主 Agent 步骤，公开回调覆盖主模型、摘要和框架可观察的重试；Token 统一聚合一次、未知不填零，总调用尝试不等同于 HTTP 请求数（§11.2.1、§14.2）。
30. **清理使用独立连接与逐线程事务**：清理初始语句／锁超时为 60000／5000 ms，绑定同一连接调用 Saver，失败线程全部回滚，已成功线程独立提交，不放宽交互平台池超时（§13.5）。
31. **模型观测归属与统计来源明确**：业务 ID 取 observer 实例，用途取主模型配置／摘要模型实例的 metadata；RunStats.tool_calls 使用预算快照标量，按名明细仅留轨迹（§11.1、§11.2.1、§14.2）。
32. **Runner 准备与收尾均受保护**：P1 try 覆盖观测器构造与 open_run，按成功标记关闭和封存，收尾异常不覆盖原错误或跳过本地记录（§14.3）。

## 21. 待确认事项与风险

| 事项 | 影响 | 处理 |
| --- | --- | --- |
| 初始模型 profile `qwen3_8_max`（2026-10-15 由 qwen3_max 改为 qwen3.8-max：qwen3-max 已下线） | HarnessProfile 键、摘要阈值、多步工具调用与最终提交能力 | 步骤 1 的 `test_model_capability` 确认实际行为与思考模式设置；96000／24000 为待实测的触发／保留默认值，另显式配置 `trim_tokens_to_summarize: null`；更换模型时重新检查摘要输入余量 |
| 摘要输入范围与模型容量 | SDK 构造函数默认只摘要待摘要历史末尾 4000 Token；关闭裁剪后单次摘要输入、时延增加，模型摘要仍可能遗漏 | 已选择显式传 None；步骤 4 前核对实际模型输入上限与输出余量，步骤 4 实测接近阈值的摘要请求；超限时调低触发阈值。证据保留结构化记录，历史文件回读作为补救，不将文件存在视为模型自动记住 |
| 单边日期语义 | “之后”是否含当天、缺省年份可能有歧义；单边范围查询也可能扫描更多历史数据 | 按 §7.5 核对用户依据，无法唯一确定时澄清；第一阶段边界仅支持日期。SQL 仍走同一只读、校验与超时管线，不人为补另一端缩小用户范围 |
| 检索相似度与实体误绑定 | 固定阈值仍可能命中与用户对象不同的真实值 | 模型不能降低阈值；Skill 核对表列归属、规范值和用户意图，有歧义时补查或澄清；开发者仅依据评测调整服务端阈值 |
| 模型预算与统计口径 | 摘要内部调用不受主 Agent 步骤预算限制，供应商内部 HTTP 重试可能不可观察 | 第一阶段保留简单步骤预算；公开回调补齐可观察摘要与重试的审计和统计，未知用量明确标记，摘要仍受请求超时与后续协作式检查约束 |
| 清理线程体量与锁竞争 | 交互平台池 5 秒超时不适合推定所有清理操作；长线程删除可能等待锁或超时 | 专用短连接、独立清理超时、逐线程显式事务；失败回滚并可重试。步骤 5 的集成测试核对原子性与配置，实际线程删除耗时据实调整 |
| 模型不调用 `submit_final_answer` 而以文本结束 | 该题记为未提交，`status=failed` | 系统提示要求单独提交；`FinalAnswerGateMiddleware` 提醒一次；验收中统计漏提交次数 |
| `embedding_release.yaml`（已确认由 RAG 入库／发布侧提供，本项目存冻结副本） | 一致性检查无法通过则不能开始业务评测 | 记录目标 Milvus 库、集合与发布批次；入库模型只认发布记录，不由维度反推 |
| “167 项”旧测试清单（已确认需在依赖变更前冻结） | 兼容性交付物 | 保存准确命令、node ID 清单、JUnit 报告、已知失败／跳过项（§2.3）；实际数量不是 167 时记录实际范围与差异，不补数量 |
| `sqlglot` 版本与 PostgreSQL 语法覆盖 | 守卫可能误拒合法 SQL；解析成功也不代表守卫完整 | 精确锁定版本，锁定后重跑 §9.9；不确定的形态一律按可修复错误拒绝；收集误拒样例补充测试 |
| 守卫的列归属依赖实时列清单 | 无限定名的列在多表查询中可能被要求加别名 | 错误提示明确要求加表别名；提示词中要求多表查询一律带别名 |
| 模型请求超时依赖设置 `ChatTongyi.model_kwargs` | 若 SDK 版本变化导致 `request_timeout` 不再生效，单次阻塞上限退回 300 秒默认值 | `doctor --stage env` 检查设置；步骤 1 用真实调用确认 |
| 除明确空值检查外的字符串过滤值需要证据 | 增加探针调用次数与时延 | 验收记录时延与 Token；必要时通过 Skill 引导先用 `distinct` 一次取全口径值；空字符串检查使用 §9.5 例外 |
| 旧安全层对字符串字面量中关键字的误拒 | 含“DELETE”等字样的业务值无法查询 | 第一阶段保持旧行为并记录为已知限制 |
| Neo4j `on` 子句中表名格式 | SQL 可能误用三段表名 | 工具返回中提示改用两段名；EXPLAIN 会拦截错误名称 |
| 并行工具调用 | 同一消息中的校验与执行会被拒绝一次；最终提交与其他工具混合会被拒绝 | 安全方向失败；提示词要求串行与单独提交；预算由加锁计数器保证不超额；观察真实模型行为 |
| 常量来源追溯的覆盖面 | 守卫可能误拒少见但合法的写法 | 不确定即拒绝并给出改写提示；收集误拒样例补充 §9.9 |

以下事项已在评审中确认，在对应模块开发前补入本篇：

补齐门禁按下表逐项执行：~~历史结果复用、完整装配验收安排和 JSON 输出隔离在步骤 4 前补齐~~（**三项已于 2026-10-15 补齐并折入本篇**：执行／分析证据沿用→§7.12.1、§5.2／§5.3、§13.3、§13.4、§14.2；装配验收按选项二收口→§10.6、§15.3、§19；`--json` stdout 隔离→§15.2；设计论证见 `docs/v4/6_db_deepagent步骤4前设计补齐.md`，经四轮评审通过）；旧线程清理粒度、本地计算与 SQL 下推的一致性在步骤 5 前补齐。无需将全部事项提前到步骤 4 前，也不因这些待细化项阻塞步骤 1–3 已明确的开发内容；依赖待细化规则的功能须在设计补齐后实施。

| 事项 | 补齐时机 | 方向 |
| --- | --- | --- |
| ~~历史结果作为最终答案引用~~（**已补齐**，2026-10-15 折入 §7.12.1 等） | 已收口 | `reuse_evidence` 扩展 E／A 执行证据受控复用登记：链首解析不回查原线程、原始来源取自 `ResultMeta`、确定性新 ID＋账本去重幂等、复用保留期显式检查（`result_expired`，存储时钟、不续期）、数据源以元数据为准、沿用与真实执行在统计／归档／提示词上解耦 |
| ~~旧线程清理粒度~~（**已补齐**，2026-10-15 折入 §13.5／§14.3／§13.4，设计见 doc 7） | 已收口 | threads 轨迹＋线程级判定（`max(last_active, retired, checkpoint 时间)`）＋advisory lock 互斥＋入口归属校验＋编号不复用 |
| ~~实施顺序中的完整装配验收~~（**已按选项二收口**：完整工具集合验收移到步骤 5，见 §10.6／§15.3／§19） | 已收口 | 步骤 4 按阶段清单常量核对 13 件；步骤 5 切换 15 件；清单在代码中定义，装配检查与运行期审计同源 |
| ~~本地计算与 SQL 下推的一致性~~（**已补齐**，2026-10-15 折入 §7.10.1，设计见 doc 7） | 已收口 | NULL/空集镜像 PG、类型白名单（文本族仅下推）、指数跨度动态精度、聚合前 `::numeric`、三步路由决策（数据源检查最优先） |
| ~~`--json` 的 stdout 隔离~~（**已补齐**，折入 §15.2） | 已收口 | 预扫描 `argv`（尊重 `--`）→`redirect_stdout` 包初始化运行（覆盖 `ConfigLoader` 的 `print`）→范围外单次序列化；argparse 错误出口输出单 JSON 文档；子进程测试断言分组 |
