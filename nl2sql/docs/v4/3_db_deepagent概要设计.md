# db_deepagent 概要设计

创建日期：2026-10-14  
设计对象：`src/modules/db_deepagent/`，基于 Deep Agents SDK 的数据库查询与分析 Agent。  
本篇状态：已核对现有代码、根环境与依赖，完成版本兼容性解析；业务功能、模型调用及数据库连接尚未开展实现验证。

> **核心改造目标：使用 Deep Agents 的自主工具调用，替代原父图和 SQL 子图预先固定的业务执行步骤。由 Agent 根据当前任务、已有证据和工具反馈决定下一步做什么、是否补充检索、如何修复 SQL，以及何时完成任务。旧父子图保留为对照实现，已有检索、校验与执行能力按需复用。**

## 1. 设计目标与约束

第一阶段构建一个可通过命令行独立运行的数据库查询与分析 Agent，覆盖当前父图和 SQL 子图提供的业务能力：理解需求、检索数据库知识、生成并验证 SQL、执行一次或多次查询、利用前一步结果继续查询，以及组织最终答案。

Agent 使用 Deep Agents 的自主工具调用循环，根据检索结果、校验错误和执行结果决定下一步行动。既可以一步完成任务，也可以重新检索、调整 SQL、改变查询方案并继续分析。

与[文档 2](2_父图的DeepAgents改造方案.md)相比，本篇推进到更深入的重构：新 Agent 自己编写和修改 SQL，Milvus 检索、Neo4j JOIN 规划、SQL 校验和执行作为独立能力开放。文档 2 的“两个工具封装原 SQL 子图”方案保留为早期讨论记录，新实现以本篇为准。

### 1.1 本阶段约定

| 项目 | 约定 |
| --- | --- |
| 新模块路径 | `src/modules/db_deepagent/`，不带版本号 |
| 编排方式 | `create_deep_agent` 替代原父子图固定业务步骤；Agent 根据证据动态安排工具调用 |
| 向量数据库 | 只使用 Milvus |
| 业务数据库 | PostgreSQL，数据库名 `orders` |
| 平台数据库 | PostgreSQL，数据库名 `lg_persist`，承载 checkpoint 与问答归档 |
| 图数据库 | Neo4j，复用现有表关系与 JOIN 路径数据 |
| 第一阶段入口 | 命令行单次查询和交互查询 |
| 已有代码 | 可引用；需要改造时复制到 `db_deepagent` 内再修改 |
| 运行环境 | 新旧实现共用项目根目录 `.venv`，依赖统一由根目录 uv 项目管理 |
| 新旧共存 | 原父图、原子图和原 CLI 保持可用 |

第一阶段不建设 FastAPI、Web UI、网络搜索或主分析 Agent，也不新增业务文档知识库。现有 Milvus Schema／维度值／SQL 案例检索和 Neo4j 关系检索属于本阶段能力，与后续版本考虑的业务文档 RAG 分开。

## 2. 技术版本与兼容性确认

### 2.1 当前版本与 SDK 接入版本

项目已经完成 LangChain、LangGraph 及相关组件升级，下表中的核心框架版本就是根目录 `.venv` 当前在用的版本。本次重构直接复用该环境；Deep Agents SDK 接入所需依赖也统一在根项目中管理。

| 依赖 | 使用版本 | 当前状态与处理方式 |
| --- | --- | --- |
| Python | 3.12 | 沿用项目要求；本次解析使用 3.12.13 |
| `deepagents` | **0.7.25** | 本次选定的 SDK 接入版本；当前根依赖清单、锁文件及 `.venv` 尚未发现该包，实施时在根项目补齐 |
| `langchain` | **1.4.5** | 当前项目约定版本 |
| `langgraph` | **1.2.15** | 当前项目约定版本 |
| `langchain-core` | 1.6.10 | 当前项目约定版本 |
| `langchain-openai` | 1.7.1 | 当前项目约定版本 |
| `langgraph-checkpoint` | 4.3.0 | 当前项目约定版本 |
| `langgraph-checkpoint-postgres` | 3.1.3 | 当前项目约定版本 |
| `pymilvus` | 2.6.9 | 当前锁定的 Milvus 客户端版本 |
| `neo4j` | 6.1.0 | 当前锁定的图数据库客户端版本 |
| `langsmith` | **目标 0.14.8；当前 0.14.1** | 接入 Deep Agents 时在根项目统一对齐依赖约束 |

`deepagents==0.7.25` 的发行元数据要求 `langchain>=1.4.5,<2.0.0`、`langchain-core>=1.6.10,<2.0.0` 和 `langsmith>=0.14.8`。当前核心框架版本满足该约束。[PyPI 发行元数据](https://pypi.org/pypi/deepagents/0.7.25/json)

Deep Agents 还会引入自己的模型集成等传递依赖。它们作为安装依赖存在，不代表本项目必须使用相应模型服务；实际模型通过现有模型配置选择。

### 2.2 已完成的验证及边界

在 `/tmp/nl2sql-v4-version-check` 中，复制根项目完整依赖清单（包含可选依赖和当前 uv 版本约束），加入 `deepagents==0.7.25`，并将临时清单中的 `monitoring` 依赖调整为 `langsmith==0.14.8`，独立 `uv lock` 成功解析 139 个包。结果保留 LangChain `1.4.5`、LangGraph `1.2.15`、LangChain Core `1.6.10` 及 PostgreSQL checkpoint `3.1.3`，LangSmith 解析为 `0.14.8`。保留原 `langsmith==0.14.1` 时，解析明确失败，因此该依赖对齐是接入条件。

**本文统一以 `deepagents==0.7.25` 为设计和源码核对基准。** 已直接检查 PyPI 发布的该版本 wheel，并校验其 SHA-256 与发行元数据一致。§8.2／§8.3 涉及的默认子 Agent 装配、文件工具白名单、`_permissions` 参数及权限作用范围、摘要 middleware 必需的 `backend` 参数均已对该发行包进行静态源码核对；没有将其他版本的行为直接视为本版本结论。实现时使用[官方版本源码](https://github.com/langchain-ai/deepagents/tree/deepagents%3D%3D0.7.25)，并按验收要求验证实际工具列表及运行行为。

上述临时目录仅用于依赖兼容性检查，不是新模块的安装或运行环境。本次验证确认包级依赖可以共存，未修改根目录依赖文件或 `.venv`，也未验证实际模型或数据库调用。

根项目的 `monitoring` 可选依赖目前固定为 `langsmith==0.14.1`，需要在接入 SDK 时与 `deepagents==0.7.25` 的要求统一。后续依赖准备统一修改根目录 `pyproject.toml` 与 `uv.lock`，通过 uv 同步到根目录 `.venv`，并验证原父子图入口仍可运行。本篇只记录该接入事项，不执行安装或依赖变更。

新模块不创建专属虚拟环境、独立 `pyproject.toml` 或独立 `uv.lock`。

### 2.3 模型选择、HarnessProfile 匹配与基础上下文配置

通过可配置的模型 profile 选择模型，优先复用现有模型创建能力。选定模型必须实际支持工具调用、多步工具反馈和需要的结构化输出；已有普通文本生成调用成功不能代替这些验证。

需要区分三个不同概念：

| 配置 | 用途 |
| --- | --- |
| 项目 `llm_profiles` | 选择模型服务、模型名称及调用参数，配置名可以是业务别名 |
| SDK `HarnessProfile` | 控制默认子 Agent、工具可见性及模型相关提示等行为，按 SDK 识别的模型键匹配 |
| 模型实例 `model.profile` | SDK 可读取的模型能力信息；本阶段的摘要控制使用固定 Token 阈值，不依赖自动窗口识别 |

对于经现有 `llm_factory` 创建的 Qwen／自定义 `BaseChatModel`，**创建 Agent 前注册并确认 HarnessProfile 命中是本项目的必做步骤**，因为默认子 Agent 的禁用依赖它。应用的模型 profile 别名或服务配置名不能直接当作 SDK 匹配键。[HarnessProfile 匹配说明](https://docs.langchain.com/oss/python/deepagents/profiles#lookup-order-for-preconfigured-model-instances)

匹配键按实际模型对象核对：SDK 从 `_get_ls_params()` 中取得 `ls_provider`，从 `model_name` 或 `model` 取得模型标识；预初始化实例优先精确匹配 `provider:identifier`，其次匹配本身含冒号的 identifier，最后按 provider 回退。Qwen 通过不同的 LangChain 模型类接入时，不能预设 provider 必然是 `qwen` 或项目配置中的服务名。

初始化日志记录模型类名、SDK 识别的 provider、identifier 和注册键，不输出 API key 或完整连接配置。锁定版本下的键排查示意：

```python
from deepagents._models import get_model_identifier, get_model_provider

provider = get_model_provider(model)
identifier = get_model_identifier(model)
if not provider or not identifier:
    raise ValueError("无法确认 HarnessProfile 匹配键，需要适配模型标识")
db_model_key = f"{provider}:{identifier}"
```

这里的 `_models` 是 SDK 私有诊断接口，使用应集中在新模块的版本适配层。可临时开启 `deepagents._models` 与 `deepagents.profiles.harness.harness_profiles` 日志，排查未命中及 provider 回退；必要时用该版本的 `_harness_profile_for_model(model, None)` 检查解析后的 `general_purpose_subagent.enabled`、`excluded_tools` 和提示后缀。注册后必须验证结果，不能把未命中当作成功。

涉及模型工厂的适配若不能通过新增封装完成，则复制相关实现到新模块后修改。

本阶段的 Token 控制采用固定摘要阈值和近期上下文保留量两个配置项，按实际使用的模型给出合适默认值，运行后按需调整。具体方式见 §8.2。

## 3. 核心架构变更：自主工具调用替代固定父子图步骤

### 3.1 执行决策交给 Agent

原实现通过节点和条件边预先确定业务推进方式：父图选择简单或复杂路径，子图依次执行问题解析、Schema 检索、SQL 生成和校验，验证失败时主要返回生成节点。

新实现把这些专业能力开放给数据库 Deep Agent。Agent 在每次获得工具反馈后决定后续行动，工具之间的业务调用顺序不再由原父子图确定。

| 执行决策 | 原固定编排 | 新 Deep Agent |
| --- | --- | --- |
| 查询需要多少步 | 先分类，复杂路径预先拆分子查询 | 根据任务与实际结果逐步确定 |
| 何时检索 Schema | 进入 SQL 子图后按固定步骤检索 | 按需检索，并可在生成、校验或执行反馈后补充检索 |
| 何时查询 JOIN 路径 | 由检索器内部流程推进 | 根据候选表与关联需求调用图检索工具 |
| SQL 校验失败后做什么 | 按条件边回到生成节点或结束 | 选择修改 SQL、检查表结构、补充检索或调整查询方案 |
| SQL 执行完成后做什么 | 进入预定的完成度检查或总结节点 | 根据数据发现决定继续分析、补充查询或形成回答 |
| 何时结束 | 由图内完成条件和预定出口推进 | Agent 判断证据是否足够，代码落实预算与执行约束 |

新链路直接使用或复制改造底层专业能力，不调用原父图或完整 SQL 生成子图来驱动任务步骤。若只是让新 Agent 反复调用完整旧子图，内部仍沿用原固定执行过程，尚未达到本篇确定的重构目标。

SQL 必须先通过校验才能执行、数据库必须只读、运行不能超过预算等约束仍由代码保障。这些是工具的执行条件，不要求所有任务沿用同一套检索和分析步骤。

### 3.2 一个数据库 Deep Agent

第一阶段使用一个数据库 Deep Agent 完成数据库范围内的规划、查询和分析。未来主分析 Agent 可将其作为 subagent 调用。

Agent 可以在检索 Schema、查看表卡片、查询 JOIN 路径、编写 SQL、验证和执行之间动态选择行动。首次检索不足时可以再次检索；校验失败时可以检查结构；结果异常时可以补充核对数据。

不重建原父图的简单／复杂分类，也不要求一次性生成所有子查询。原子图中的专业实现按工具需要复用或复制改造，原子图本身继续用于旧入口和对照测试。

Deep Agents 使用 LangChain Agent 能力与 LangGraph 运行时。本项目通过工具、Skill、middleware 和状态扩展配置其行为，不再手工复刻原有父子图的业务拓扑。[官方概览](https://docs.langchain.com/oss/python/deepagents/overview)

### 3.3 任务示例

对于“先找出昨日客流最高的服务区，再查询其库存”，Agent 可以：

1. 检索客流相关表及服务区维度，必要时查询 Neo4j 关系。
2. 编写第一条 SQL，校验通过后执行，取得真实服务区编号。
3. 根据编号和库存需求检索库存相关结构。
4. 编写第二条 SQL，校验后执行。
5. 根据两个结果组织回答，并给出查询依据。

这是任务可能采取的行动，不是写死的流程。如果结果为空、Schema 不足或存在其他可行的单 SQL 方案，Agent 可以调整行动。

## 4. 新模块组织与旧代码保护

建议目录如下，文件在后续实施中创建：

```text
src/modules/db_deepagent/
├── __init__.py
├── agent.py                  # 创建及配置 Deep Agent
├── runner.py                 # 任务入口与结果归档
├── cli.py                    # 独立命令行入口
├── schemas.py                # 工具、状态及最终结果结构
├── config.py                 # 新模块配置加载与依赖装配
├── config/
│   ├── db_deepagent.yaml      # 模型、连接及运行参数
│   └── prompts.yaml           # 统一管理项目维护的 Agent 提示词
├── tools/                    # Agent 可调用的业务工具
├── services/                 # 新封装或复制后修改的服务实现
├── middleware/               # 上下文、运行预算和日志等控制
├── persistence/              # 新模块的 PG checkpoint 与归档接入
├── skills/                   # 查询规划与分析 Skill
├── memory/                   # 可选：人工维护的稳定业务说明
│   └── memory_context.md
└── README.md                 # 安装、启动及对照测试说明
```

新测试统一放在 `src/tests/db_deepagent/`，按需要划分 `unit/` 和 `integration/`，不放在业务模块目录内。现有 [pytest.ini](../../pytest.ini) 的 `testpaths = src/tests` 会默认发现这些测试，无需修改测试发现配置。测试使用现有 `unit`、`integration`、`requires_db` 和 `requires_llm` 等标记。

新旧实现均使用项目根目录的 `.venv/`，依赖由根目录 `pyproject.toml` 和 `uv.lock` 统一管理；`.venv` 已被现有 `.gitignore` 忽略。新模块不再创建环境。运行时结果与日志存放在明确配置的工作目录，并避免纳入代码交付。

复用规则：

- 无需修改的函数或类可以直接导入。
- 需要改变行为、依赖注入、配置来源或返回结构时，在 `db_deepagent` 内复制后改造，记录原始来源。
- 复制代码后检查其内部导入，确保需要改造的依赖也指向新实现。
- 不通过 monkey patch 或覆盖原模块全局配置／单例来改变新链路的行为。
- 原业务模块、旧 CLI 和原配置文件不在本阶段修改。新增 SDK 所需依赖在根依赖文件中统一管理，依赖调整后验证新旧入口兼容。

## 5. 数据源与配置装配

### 5.1 数据源职责

| 数据源 | 负责内容 | 新链路使用方式 |
| --- | --- | --- |
| Milvus | 表、列、维度值、历史 SQL 案例和表卡片 | 使用现有 Embedding 与 Milvus 客户端能力 |
| PostgreSQL `orders` | 业务数据、实时表结构和 EXPLAIN | 只读查询、结构核对、SQL 校验 |
| Neo4j | 表关系、JOIN 条件、桥接路径等 | 按需获取关联证据 |
| PostgreSQL `lg_persist` | Agent 工作状态与问答归档 | 独立持久化配置，与业务连接分开 |

业务库连接固定到 `orders`，平台连接固定到 `lg_persist`。Agent 的工具参数不能让模型自行更改目标数据库或连接信息。

已核对当前 `.env` 的有效配置为 `DB_NAME=orders`。现有全局配置中的业务库默认名是环境变量表达式，新模块在解析配置后以 `orders` 为基准核对实际目标，配置不符合约定时返回明确错误。

### 5.2 Milvus 检索

复用当前集合与字段映射：

- `table_schema_embeddings`：表、列语义检索及表卡片读取。
- `dim_value_embeddings`：维度值匹配。
- `sql_example_embeddings`：历史 SQL 案例检索。

新模块只实例化 Milvus 适配器，不设置 pgvector 的备用检索分支。Milvus 不可用时返回具体错误，不能悄悄切换检索后端。

**查询侧 Embedding 必须与三个集合入库时使用的模型及向量维度一致，同维度不代表同一向量空间。** 当前 [embedding_profiles 配置](../../src/configs/config.yaml) 激活 `qwen3_7_text_embedding`，模型为 `qwen3.7-text-embedding`、1024 维，新模块显式沿用该配置，不因重构而回退到 `text-embedding-v3`。实施时结合 RAG 发布记录核对实际入库模型，并检查集合向量字段维度；仅看到当前查询配置不能证明入库模型一致。无法确认或发现不一致时返回明确配置错误，不静默混用，也不在本阶段自动重建集合。

表卡片来自 Milvus 的现有 `fetch_table_cards` 能力，字段 `object_desc` 映射为 `text_raw`。当前 Milvus 映射中缺失的粒度信息不能由代码伪造，必要时通过结构核对或已有业务说明补充。

内部保留 `db.schema.table` 标识，以匹配 Milvus 和 Neo4j；SQL 中使用 PostgreSQL 需要的 `schema.table`。可复用现有表名转换函数，保留原始标识便于追溯。

### 5.3 Neo4j 检索

复用现有关系数据、JOIN 条件和路径规划方法。Agent 明确提供候选表后，可以获取关联路径并判断是否需要补充桥接表结构。

“Neo4j RAG”在本阶段具体指利用现有关系图为 SQL 提供 JOIN 证据，不新增通用知识图谱检索产品。路径不存在、查询失败及可用回退方式需要在工具返回中区分。

## 6. 工具设计

### 6.1 核心工具

| 工具 | 输入概念 | 职责与结果 |
| --- | --- | --- |
| `search_schema` | 查询意图、检索类型、必要筛选与检索参数 | 从 Milvus 获取候选表／列、维度值或 SQL 案例；类型可按需选择 |
| `inspect_tables` | 明确的表标识列表 | 获取 Milvus 表卡片，并按需从 `orders` 核对真实列、类型和结构 |
| `plan_joins` | 基表、目标表及必要约束 | 从 Neo4j 获取关联路径、JOIN 条件及相关表标识 |
| **`validate_sql`** | SQL | 执行现有三层校验，返回通过状态、错误层级、警告和 EXPLAIN 信息 |
| `execute_sql` | SQL | 确认已验证且内容一致后，在 `orders` 执行，返回真实数据、状态和结果引用 |
| `read_result` | 结果引用、列选择与分页范围 | 读取前序查询的必要结果，用于依赖查询和后续分析 |
| `analyze_result` | 结果引用与明确的分析操作 | 对已保存结果做基础统计、比较与计算，返回可复核的计算结果 |

`analyze_result` 第一阶段采用受控的确定性操作，必要时将计算下推为 SQL；暂不引入任意 shell 或通用代码执行环境。图表和更丰富的计算环境可在后续扩展。

**所有面向业务数据的 SQL，包括 `analyze_result` 内部生成的下推 SQL，必须经过同一三层校验及只读执行管线。** 下推服务调用共享的校验／执行服务，不能直接使用数据库连接绕过 §6.2 的交接、超时和读取上限；仅对已保存结果进行本地确定性计算时不需要 SQL 校验。校验器内部的 EXPLAIN 和服务固定的结构查询由同一只读连接边界约束，不递归调用校验器。

Agent 自己编写 SQL，`generate_sql` 不作为新核心链路的必需工具。原 SQL 子图仍通过原入口使用，便于对照结果。

### 6.2 校验与执行的约定

`validate_sql` 独立存在，校验失败交给 Agent 选择如何处理。它可以修改 SQL、补充检索、检查字段值或调整需求，而非只按固定次数重复生成。

保留现有语法、安全、语义三层校验。这里的语义校验主要通过 EXPLAIN 发现数据库层面的错误，不代表指标口径或分析结论必然正确。默认使用 EXPLAIN，不使用 EXPLAIN ANALYZE 作为额外执行。

校验成功时，工具通过受控的状态更新记录通过验证的 SQL，关联当前请求、业务数据源与校验配置。`execute_sql` 在服务端确认接收的 SQL 与该记录一致，不能信任模型提供的 `valid=true`。

SQL 仍直接作为工具参数传递，不要求向模型暴露 `validated_sql_id`，也不为交接单独建立 SQL 存储表。SQL 改变后必须重新验证；执行工具不重复整套三层校验。

一致性判定采用以下固定约定：

- **匹配任一有效记录，不只匹配最近一条。** 同一运行可同时校验多条 SQL；执行时匹配当前 `thread_id`、请求标识、服务端 `run_id`、业务数据源标识及校验配置指纹下的任一有效成功记录。数据源标识包含业务库、Schema／搜索路径和执行角色等实际连接边界，不能只比较显示名称。
- **只做最小文本规范化。** 仅去掉整条 SQL 首尾的 ASCII 空白（空格、制表符、回车、换行），保留内部全部字符、大小写、注释、引号和末尾分号。校验与执行使用同一规范化文本，逐字比较；不做格式化、注释移除或 SQL 语义等价推断。内部空白、条件、字面量或分号变化也要重新校验。未来若支持绑定参数，参数值与类型必须一并参与匹配。
- **授权只在本次运行有效。** 记录只能由服务端校验成功后更新，模型参数不能创建或修改它；同一作用域内重新校验失败时，撤销该文本先前的成功记录。记录可在本次运行内重复使用，运行结束、取消、配置或数据源切换即失效。多轮会话与暂停恢复均创建新的服务端 `run_id`，旧 checkpoint 中的记录只作追溯，重新执行前必须重验；无须另建 SQL 交接表。

执行端还负责只读事务、超时和结果读取上限。依赖参数或查询范围变化导致 SQL 文本改变时，同样重新校验。数据库及 Schema 在两次调用间变化而导致执行错误时，返回错误交给 Agent 处理。

### 6.3 结果与错误接口

工具统一返回结构化信息，包括 `status`、必要数据或 `result_ref`、预览、警告，以及失败时的 `error_type`、`message` 和是否适合重试。SQL 结果记录同时保存实际 SQL、列信息、行数、耗时及是否截断。

较大结果由自定义结果服务直接写入业务结果目录，不经过 Deep Agents backend，也不挂载到内建文件工具可见的文件空间。State 和工具返回中保留不透明的 `result_ref`、受控预览及口径，真实宿主文件路径由服务内部管理。

Agent 通过 `read_result` 读取指定列或分页，通过 `analyze_result` 完成受控计算。两个工具均按当前会话核对结果引用，限制读取范围并保留完整性标记。区分“工具预览截断”和“数据库结果本身被读取上限截断”：前者可继续分页，后者不能通过翻页恢复未保存数据，也不能当作完整总体统计。

前后步骤通过结果引用和明确字段传递依赖。较大编号列表应由程序提取和校验，避免要求模型手工抄写整份结果。第一阶段优先串行调用，消除验证和执行之间以及有依赖查询之间的竞争。

LangChain 的工具运行上下文与状态更新能力用于隐藏连接信息、传递请求信息和关联查询结果。[Tools 文档](https://docs.langchain.com/oss/python/langchain/tools)

## 7. 已有能力的复用方式

| 已有实现 | 新模块处理方式 |
| --- | --- |
| [Milvus 适配器](../../src/services/vector_adapter/milvus_adapter.py)与 Embedding 客户端 | 优先直接复用可显式传入配置的能力 |
| [SchemaRetriever](../../src/tools/schema_retrieval/retriever.py) | 复用检索、分类及维度匹配逻辑；需要拆解时复制到新模块，不直接调用固定全流程作为唯一检索入口 |
| [Neo4jClient](../../src/services/db/neo4j_client.py)及 JOIN 辅助函数 | 复用路径算法和结果格式；需要连接注入时复制适配 |
| [表名转换](../../src/tools/schema_retrieval/table_names.py) | 直接复用纯函数 |
| [SQLValidationTool](../../src/tools/validation/sql_validation.py) | 复用三层检查；因其当前绑定全局 PG 客户端，必要时复制后注入新业务连接 |
| [PGClient](../../src/services/db/pg_client.py) | 复用查询、EXPLAIN 和结构核对方法；复制需要的方法到新业务服务，移除对 pgvector 元数据查询的依赖 |
| [LLM 工厂](../../src/services/llm_factory.py) | 优先通过既有 profile 取得模型；需要工具调用适配时在新模块封装或复制修改 |
| 原父图和原 SQL 子图 | 保持原状，作为独立对照基线 |

当前 `PGClient` 包含 pgvector 元数据访问方法，不能因为复用了该类就让新检索链路依赖这些方法。新业务 PG 服务只承担 `orders` 的结构、校验及业务查询。

## 8. Skill、上下文与运行控制

### 8.1 Skill

在新模块配置查询规划、Schema 检索及数据库分析 Skill，指导 Agent：

- 识别指标、时间范围、维度、比较基准及数据粒度。
- 优先使用合理的一条 SQL，确有结果依赖时再分步。
- 发现检索不足时补充检索，使用真实维度值和 JOIN 证据。
- 区分空结果、查询失败、数据缺失与业务异常。
- 基于已执行查询形成回答，并说明未完成事项和口径限制。

Skill 按 Deep Agents 支持的方式按需读取。系统指令提供稳定职责，执行约束由工具代码落实。[Skills 文档](https://docs.langchain.com/oss/python/deepagents/skills)

[问题解析误判备忘录](../100_问题解析误判备忘录.md)中的“当前”时间误判和“系统”伪维度值属于本阶段必须解决的遗留问题。新链路不依赖旧解析节点，但仍必须在新模块落实三层防线：

1. **指令与 Skill 层明确证据要求。** 裸“当前／目前／现在”不能自动解释为“今天”；结合用户原文、已确认的多轮需求和业务口径消歧。“当前月／本月／今天”等明确时间表达才展开对应周期，歧义不能消除时追问。维度候选记录原文证据及绑定列，不把“系统／平台”等泛称自动视为筛选值，也不用停用词表删除真实业务值。
2. **工具与数据通路层做确定性守卫。** 由代码按已确认周期、时区计算半开边界并检查 `start < end`；无效区间返回可修复错误，不直接照抄进 SQL，也不静默去掉用户明确的过滤。维度值需有明确列绑定和 Milvus 命中、受控数据库探针或前序真实结果等证据；未命中候选不再通过旧兜底分支透传为过滤提示。明确指定但未匹配的值应补检索或澄清，不能悄悄放宽条件。确定性探针本身同样走 §6.2 的校验与只读执行管线。
3. **最终 SQL 守卫与回归层防止重新引入。** 新校验服务在原三层检查之外，对照受控时间／维度证据检查实际 SQL：拒绝可确定的同列空半开区间、未经证据支持的候选值过滤；证据不足时要求补充确认。不能仅凭 EXPLAIN 成功认定业务语义正确。固定回归覆盖裸“当前”、显式“今天／本月”、无命中“系统”、真实值“业务系统”及向量未召回后探针命中的情况。

上述守卫通过新模块的服务和受控状态传递落实；需要修改已有解析、匹配或校验实现时复制到新模块，保留旧父子图行为供对照。

### 8.2 Middleware

使用 **Deep Agents 版 `SummarizationMiddleware`**，从 `deepagents.middleware` 导入，显式替换 SDK 默认的同名实例。该版本必须传入 `backend`，并与 §8.3 的文件 middleware 和 `create_deep_agent` 共用同一个会话后端对象；不能分别创建后端导致历史写入后无法读取。

Token 控制保持简要，只提供两个可配置值：

| 配置项 | 作用 |
| --- | --- |
| `summary_trigger_tokens` | 达到该 Token 数时，触发 SDK 自动摘要 |
| `recent_context_tokens` | 摘要后保留的近期消息 Token 数 |

分别对应 `trigger=("tokens", summary_trigger_tokens)` 和 `keep=("tokens", recent_context_tokens)`，其余摘要参数使用 SDK 默认。触发值低于实际模型输入上限并留有余量，保留值小于触发值。当前版本不引入比例切换、分阶段 Token 配额或独立的摘要输入预算。[上下文压缩说明](https://docs.langchain.com/oss/python/deepagents/context-engineering#summarization)

同名覆盖会替换整个 middleware 实例，不继承被替换实例的参数。初始化代码明确传入模型、共享后端和上述两个配置值，避免重复安装两套摘要逻辑。

其他 middleware 仅提供基础的模型／工具调用次数上限、超时和请求日志。上下文压缩交给 SDK 摘要机制，摘要中保留用户目标、指标口径、已确认发现、结果引用和待解决问题。

可选启用 `TodoListMiddleware`，让较长任务维护可更新的计划。一次查询任务不强制创建多步计划。第一阶段不启用数据库 Agent 自行委派子 Agent，避免无必要的额外编排层。

在 `deepagents==0.7.25` 中，仅传 `subagents=[]` 不能关闭默认的 `general-purpose` 子 Agent。SDK 默认会补入该子 Agent，并通过 `SubAgentMiddleware` 暴露 `task` 工具。第一阶段必须在创建 Agent 前，为实际生效的模型注册 `HarnessProfile`，显式设置 `general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)`，并且不传入任何同步或异步子 Agent。[官方禁用方式](https://docs.langchain.com/oss/python/deepagents/subagents#running-without-subagents)

配置示意如下，`db_model_key` 必须与 SDK 实际解析的模型标识匹配：

```python
from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)

register_harness_profile(
    db_model_key,
    HarnessProfile(
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    ),
)
# 在上述注册生效后，调用 create_deep_agent(..., subagents=[])。
```

`db_model_key` 是 SDK 识别的 `provider:model` 等规范标识，不是项目 `llm_profiles` 中任意命名的配置别名。传入预初始化模型实例时，也必须核对 SDK 的模型标识解析与 profile 匹配结果。注册表具有进程级作用域，应针对数据库 Agent 实际使用的模型配置注册，避免把禁用要求无差别施加到未来主分析 Agent 使用的所有模型。

验收时捕获实际发给模型的工具列表，确认不含 `task`，且未装配同步或异步子 Agent 委派能力。不能仅依赖 `subagents=[]` 或提示词要求“不委派”，也不通过 `excluded_middleware` 强行删除 SDK 不允许删除的 `SubAgentMiddleware`。

默认暴露委派工具会增加工具与相关提示的 Token 开销；只有实际调用委派后才产生子 Agent 的额外模型调用。禁用它可以明确落实第一阶段单 Agent 的范围。

### 8.3 文件后端与工作目录

**本方案选择自传 `FilesystemMiddleware(tools=[...])` 的白名单机制**，替换默认同名 middleware；不只依赖 HarnessProfile 的 `excluded_tools` 从模型请求中隐藏工具。白名单在构建时只注册允许的文件工具，未列出的工具不进入该 middleware 的可分派工具集合。[官方白名单说明](https://docs.langchain.com/oss/python/deepagents/overview#restricting-filesystem-tools)

第一阶段允许的内建文件工具为 **`ls`、`read_file`、`glob`、`grep`**，用于发现 Skill、读取说明和检索 SDK 已转存的历史及工具消息。业务结果文件由 `read_result`／`analyze_result` 管理，不通过这些内建工具访问。`read_file` 是此白名单机制的必含项。

| 工具 | 第一阶段处理 |
| --- | --- |
| `ls`、`read_file`、`glob`、`grep` | 显式列入文件工具白名单 |
| `write_file`、`edit_file`、`delete` | 不注册给模型；结果写入及清理由受控业务服务承担 |
| `execute` | 不注册；同时不使用 `LocalShellBackend` 或其他 shell／sandbox 执行后端 |
| `task` | 属于子 Agent middleware，按 §8.2 的 HarnessProfile 配置禁用，与文件白名单分开处理 |

文件工具白名单不限制通过 `create_deep_agent(tools=...)` 添加的自定义业务工具。业务工具仍需落实自己的数据库及结果读写边界，且不得重新引入通用文件删除或 shell 执行接口。

**文件能力分工选择方案甲：业务结果由自定义服务直接写盘，Agent 通过受控业务工具读取。** 不采用“将业务结果挂载到 backend，再配置 deny-read”的方案乙，避免内建 `read_file` 绕过业务结果工具的分页、截断标记和会话检查。

通过同一个 `CompositeBackend` 对象映射新模块只读的 `/skills/` 和当前会话的 `/artifacts/` 文件空间，设置 `artifacts_root="/artifacts"`。按 §8.4 可选增加只读 `/memory/` 路由。底层 `FilesystemBackend` 使用 `virtual_mode=True` 和明确根目录，不将整个项目目录、`.env` 或其他会话目录暴露给模型。业务结果目录放在这些 backend 根目录之外，不设置 `/results/` 路由，也不通过符号链接或其他映射暴露。

**权限规则按顺序首条命中生效，无命中时默认允许。** 本方案先为所需读取范围配置 `operations=["read"]` 的 allow 规则，再以覆盖全部路径的 `operations=["read", "write"]` deny 规则兜底，拒绝其他路径读取及所有模型文件写入。SDK 的操作类型只有 `read`／`write`，删除归入 `write`；仅兜底拒绝写入不足以限制读取范围。不能把未配置的路径当作默认拒绝。兜底 glob 必须验证覆盖目录自身和隐藏路径；该版本还检查符号链接解析后的路径，允许规则也需覆盖合法目标的对应路径，并验证正常读取不会被误拒绝。[权限匹配实现](https://github.com/langchain-ai/deepagents/blob/deepagents%3D%3D0.7.25/libs/deepagents/deepagents/middleware/filesystem.py)

`FilesystemPermission` 控制的是内建文件工具调用，不能视为 backend 或自定义业务服务的统一访问控制。SDK 源码明确指出直接使用 backend 不应用这些权限。因此结果服务自行实施会话归属、路径边界和读取限制；其直接磁盘操作也不依赖 SDK 权限规则。[文件权限说明](https://docs.langchain.com/oss/python/deepagents/permissions)

替换文件 middleware 时必须显式传入后端及权限规则。`deepagents==0.7.25` 的构造参数为 `_permissions`，属锁定版本的私有适配点，应集中封装；只在 `create_deep_agent(permissions=...)` 传规则不会自动补入替换后的实例。

以下为统一装配示意，变量由会话初始化代码提供，HarnessProfile 应已按 §2.3／§8.2 注册并验证：

```python
from deepagents import create_deep_agent
from deepagents.middleware import FilesystemMiddleware, SummarizationMiddleware

file_middleware = FilesystemMiddleware(
    backend=file_backend,
    tools=["ls", "read_file", "glob", "grep"],
    _permissions=file_permissions,
)
summary_middleware = SummarizationMiddleware(
    model=model,
    backend=file_backend,
    summary_prompt=summary_prompt,
    trigger=("tokens", summary_trigger_tokens),
    keep=("tokens", recent_context_tokens),
)
agent = create_deep_agent(
    model=model,
    system_prompt=prompt_config["agent"]["system_prompt"],
    tools=business_tools,
    backend=file_backend,
    permissions=file_permissions,
    middleware=[file_middleware, summary_middleware],
    skills=["/skills/"],
    subagents=[],
    checkpointer=checkpointer,
    store=store,
)
```

关闭模型的文件写工具，不禁止 SDK 内部通过 backend 自动转存工具消息或摘要历史。业务结果的写入与清理则由自定义服务直接操作磁盘。业务工具只向模型返回受控预览及引用，不将完整原始结果作为工具消息交给 SDK 转存，避免另一条读取路径绕过业务约束。

业务结果目录与 SDK artifacts 目录分开，均保留在磁盘上，不使用默认 `StateBackend` 承载这些大内容。结果服务保留可恢复的引用映射及完整性元信息，重启后仍可按 `result_ref` 读取；文件清理与会话及归档保留策略协调。

验收同时检查实际模型请求和可分派工具集合：文件工具只有白名单四项；无 `write_file`、`edit_file`、`delete`、`execute`，并按 N1 无 `task`。还需验证 Skill 可读、历史转存后可回读、业务结果可以保存，以及内建文件工具不能读取原始业务结果、跨会话引用不能访问、分页后仍保留源结果截断标记。

### 8.4 可选的稳定业务说明 memory

可将经过确认的指标定义、单位、业务术语和稳定说明放在新模块 `memory/memory_context.md` 中人工维护，通过只读 backend 路径配置 `memory=["/memory/memory_context.md"]`。Deep Agents 会将这些文件内容加载到系统提示，适合少量经常使用的稳定知识；查询方法继续放在按需读取的 Skill 中。[Memory 文档](https://docs.langchain.com/oss/python/deepagents/memory)

该项为可选能力，未配置时不加载 memory 文件。第一阶段不开放 Agent 自动修改记忆，也不直接加载项目根目录面向开发者的 `AGENTS.md`。memory 文件、checkpoint 会话状态和 Store 问答档案各自承担不同职责。

### 8.5 提示词统一使用 YAML 管理

新模块不创建 `agent.md`。项目维护的 Agent 角色、职责、工具使用规则、回答要求、摘要补充要求及需要自定义的模型提示后缀，统一存放在 `config/prompts.yaml`，使用 YAML 多行文本便于编辑。

`config/db_deepagent.yaml` 保存运行参数，`config/prompts.yaml` 保存提示词，`memory/memory_context.md` 保存稳定业务知识，Skill 保存按需加载的任务方法。

建议最小结构如下，具体提示词在实现时完善：

```yaml
agent:
  system_prompt: |
    你是数据库查询与分析 Agent。
    根据用户需求和工具反馈自主安排查询，使用真实执行结果回答。

summarization:
  extra_instructions: |
    保留研究目标、指标口径、已确认发现、结果引用和待解决问题。
```

初始化时使用安全 YAML 加载读取 `prompt_config`，将 `agent.system_prompt` 传给 `create_deep_agent(system_prompt=...)`。摘要补充要求与 SDK 默认摘要模板组合为示例中的 `summary_prompt`，保留默认模板需要的占位符；后续增加的项目提示词也统一在该 YAML 中定义并由代码引用。

提示词在创建 Agent 时加载，修改后重新创建 Agent 或重启 CLI 生效，第一阶段不实现热更新。

## 9. Checkpoint 与问答归档选择

用户已授权按需要选择。本方案第一阶段选择 **PG checkpoint 管理会话工作状态，PG Store 显式保存问答档案**。

### 9.1 Checkpoint

为 `create_deep_agent` 配置 `PostgresSaver`，连接平台库 `lg_persist`。同一交互会话使用稳定 `thread_id`，下一轮只提交新增用户消息，由框架恢复已有消息和工作状态。

State 保留消息、必要的分析状态、验证记录及结果引用，不保存整份业务结果集。配置独立的平台 Schema `db_deepagent`，不与旧实现默认使用的持久化表空间混用。初始化及必要迁移由新模块显式完成。

`PostgresSaver`／`PostgresStore` 的 SQL 使用未限定 Schema 的表名，因此需先创建 `db_deepagent`，再为两者每条连接（含连接池新建连接）设置连接级 `search_path=db_deepagent`，并在相同搜索路径下执行 `setup()`、运行读写和清理；不回退到 `public`，初始化时核对 `current_schema()` 及实际建表位置。

初版 CLI 以同步调用为主，使用同步 saver。不直接复用旧 `SafeCheckpointer` 的 fail-open 行为：启用持久化后若保存或读取失败，应返回可见的持久化错误，避免用户以为会话状态已可靠保存。调试可显式选择内存模式，但不自动静默降级。

### 9.2 Store

使用同一平台库中独立配置的 `PostgresStore`，由新入口显式保存本轮用户问题、最终回答、请求标识和结果引用。沿用成功查询才进入问答归档的业务意图：有成功 SQL 执行的任务可归档，并明确标记完整或部分完成；纯失败任务保留运行错误记录，不作为成功问答。

同一线程的模型记忆以 checkpoint 中的消息为来源，不每轮再从 Store 取相同问答注入。Store 用于 CLI 历史展示、回查和后续归档需求，作用与模型工作上下文分开。

### 9.3 存储增长控制

Deep Agents 版摘要保留原始 `messages`，通过摘要事件改变模型使用的上下文，不自动缩小原始消息日志或清除旧 checkpoint。初版采用可配置的过期会话整线程清理策略；活动任务和等待恢复的任务不清理。初始保留期建议为最近活动后 30 天，实施时作为配置项提供。

`deepagents==0.7.25` 默认 `DeepAgentState.messages` 使用 `DeltaChannel`，以增量写入和周期快照恢复消息，并非每个 super-step 都保存完整消息列表；默认消息快照频率为 50 次通道更新。沿用该默认状态，扩展状态时继承 `DeepAgentState`，避免将消息通道覆盖回普通全量聚合。当前 PostgreSQL saver 支持该增量恢复机制，但增量记录、周期快照及 checkpoint 元数据仍会累积，不能把摘要或增量存储视为存储体量上限。[默认消息状态与装配](https://github.com/langchain-ai/deepagents/blob/deepagents%3D%3D0.7.25/libs/deepagents/deepagents/graph.py)

**线程长度本身驱动 checkpoint 体量，长交互会话应主动轮换线程。** 轮换前先显式保存分析摘要和结果引用，再带入新线程。禁止通过“只保留最新一行”的 SQL 随意删除 checkpoint 祖先或共享数据。清理结果文件前还要确认其不再被保留状态或归档引用。

问答档案的保留期独立配置，checkpoint 过期不等于删除问答档案。具体清理命令应支持预览待清理范围。

## 10. CLI 与结果交付

### 10.1 独立入口

新入口为 `src/modules/db_deepagent/cli.py`，支持：

- 单次自然语言查询。
- 同一会话内的交互查询。
- 指定请求与线程标识。
- Rich 或纯文本显示，以及结构化 JSON 输出。
- 查看实际工具调用、SQL、执行结果和必要的计划状态。

现有 [父图 CLI](../../scripts/nl2sql_father_cli.py)直接导入旧入口，并依赖 `complexity`、`path_taken` 等旧结构，不适合直接作为新 Agent 的运行入口。可将其参数处理和显示代码复制到新模块适配，原脚本保持原样。

以下是待实现后的运行示例，在仓库根目录执行；本篇不表示入口已经存在：

```bash
# 新 Agent：使用项目根目录现有环境
.venv/bin/python -m src.modules.db_deepagent.cli "查询今年各服务区销售额"

# 新 Agent：交互模式
.venv/bin/python -m src.modules.db_deepagent.cli

# 旧父图：使用同一根环境及原有入口
.venv/bin/python scripts/nl2sql_father_cli.py "查询今年各服务区销售额"
```

安装和运行说明统一基于仓库根目录的 uv 项目及 `.venv`。以上为 Linux 命令；Windows 使用根目录 `.venv/Scripts/python.exe` 调用相同模块或脚本。

### 10.2 结果协议

新 Agent 返回明确的任务状态、最终回答、查询证据、数据引用、口径限制及统计信息。使用 Pydantic 定义可验证的结果结构，为后续 subagent 接入提供接口。

SQL、行数和结果引用由工具执行记录提供；最终回答中的数据应能够回查。可以为对照显示增加兼容字段，但不强制保留旧简单／复杂路径字段作为新运行逻辑。

返回结构明确包含 `evidence: list[EvidenceItem]`，第一阶段仅支持数据库查询与结果计算两类证据：

| 字段 | 内容及约束 |
| --- | --- |
| `evidence_id`、`kind` | 返回结果内唯一证据标识；类型为 `sql_query` 或 `result_analysis` |
| `tool_call_id`、`source` | 关联真实工具执行记录及业务数据源标识；不包含连接凭据 |
| `sql` | 实际执行的 SQL；纯本地结果计算为 null，不能把未执行的 SQL 标为查询证据 |
| `result_ref`、`input_result_refs` | 查询结果或分析输出的受控引用；计算证据同时记录其输入结果引用，沿用会话访问检查 |
| `operation` | 计算证据记录确定性分析操作及参数；普通查询可为 null |
| `row_count`、`preview_truncated`、`source_truncated` | 实际保存行数和两种截断状态；不把保存行数冒称数据库总体行数 |
| `warnings` | 数据缺失、口径或结果完整性等工具已确认的限制 |

字段按证据类型由 Pydantic 校验必填与可空条件，事实字段由服务端执行记录组装，模型不能自行补造 SQL、行数或引用。最终回答或结构化发现引用 `evidence_id`；服务端检查引用存在，计算证据可沿输入引用追溯到查询证据。失败调用进入错误记录，不伪装成成功数据证据。

## 11. 实施步骤与验收

### 11.1 实施步骤

1. 复用根目录 `.venv`，补齐 SDK 依赖及新模块配置，核对四类数据源和模型工具调用；确认 Qwen 模型的 HarnessProfile 注册键及生效结果，配置两个固定摘要阈值，并验证旧入口兼容。
2. 封装 Milvus 检索、表结构查看及 Neo4j JOIN 工具。
3. 封装 `validate_sql`、`execute_sql` 及可信交接，落实时间／维度守卫和分析下推统一管线，完成真实查询闭环。
4. 建立 Deep Agent、Skill、middleware 和 CLI，支持动态多步查询。
5. 完成结果读取与基础分析、会话状态与归档，按固定验收题集冻结数据及参考结果后进行新旧入口对照验证。

步骤 1 在依赖变更前留存旧入口基线，步骤 5 在变更后以相同输入复测，具体交付物为：

- **旧 CLI 冒烟记录**：分别运行 `scripts/nl2sql_father_cli.py` 和 `scripts/nl2sql_subgraph_cli.py`，保存固定问题、配置／数据版本、运行命令、输出和日志。父图检查生成并执行 SQL 的真实结果，子图检查返回通过验证的 SQL；不能只以进程退出码或 `--help` 成功判定。问题从冻结题集中选择两个旧入口均支持的单次查询，前后使用相同题面。
- **167 项旧测试基线核对与报告**：按评审提出的 167 项范围，实施前冻结准确命令、pytest node ID 清单及 JUnit 报告，依赖变更后运行同一清单，原通过项不得新增失败；不以新增测试凑足数量。目前仓库未找到与“167 项”对应的清单或报告，因此该数字标为待核对，不能写成已实测通过；若实际范围不同，需留存差异并修正文档。已有[依赖升级测试记录](../upgrade/5_LangChain_LangGraph生态组件版本升级清单.md)覆盖另一组更大范围，不能替代这份指定清单。

测试从仓库根目录使用根 `.venv` 执行，必要时显式设置 `PYTHONPATH=.`；冒烟输出、node ID 清单和 JUnit 报告均归入本次验收记录。外部服务不可用的冒烟项记为未完成，不能算通过。

### 11.2 验收任务

| 类别 | 验收重点 |
| --- | --- |
| 单表聚合与过滤 | 指标、时间、维度值正确 |
| 多表及桥接表查询 | Milvus 候选结构与 Neo4j JOIN 证据正确使用 |
| 历史 SQL 案例 | 能按需检索并结合当前口径使用 |
| Embedding 一致性 | 查询模型与入库模型一致，集合维度匹配；未知或不一致时明确报错 |
| 依赖查询 | 后续 SQL 使用前一步真实结果，支持标量及多行依赖 |
| 独立的多个数据问题 | 能自行安排查询并汇总，不强制进入复杂路径 |
| 验证失败 | Agent 能修改 SQL 或补充检索，执行端拒绝未验证及已修改 SQL |
| 校验记录作用域 | 同轮多条 SQL 可分别执行；内部文本变化、跨请求／运行及配置切换均不能复用旧授权 |
| 分析下推 | 下推 SQL 与普通查询走同一校验及只读执行管线，无直接连接绕过路径 |
| 遗留解析误判 | “当前”不凭空增加日期过滤；无匹配维度不兜底透传，真实值不被泛称规则误删 |
| 动态调整执行步骤 | 首次检索不足或结果异常时，能根据反馈改变行动；新链路不依赖原父子图推进任务 |
| 单 Agent 范围 | 实际模型请求不含 `task`，无默认、显式或异步子 Agent 委派能力 |
| 模型配置生效 | 自定义 Qwen 的 HarnessProfile 确认命中，错误注册键不能静默通过初始化 |
| 文件工具范围 | 内建文件工具仅四项只读白名单，禁止的文件写入、删除和 shell 工具不可分派 |
| 文件权限顺序 | 允许范围可正常读取；未列路径、隐藏路径及越界符号链接目标不能绕过兜底拒绝规则 |
| 业务结果访问 | 原始结果不在 backend 可见范围；业务结果工具执行会话检查、分页及源结果截断标记约束 |
| 摘要与后端一致性 | 两个固定 Token 配置生效；摘要和文件 middleware 共用会话后端，历史转存后可回读 |
| 空结果、缺失与执行错误 | 反馈准确，补充分析不编造数据 |
| 多轮交互 | 同一线程延续工作状态，历史不重复注入 |
| 消息持久化 | 保留默认 DeltaChannel；跨周期快照后可恢复消息，轮换线程时摘要和结果引用可继续使用 |
| 平台 Schema 隔离 | Saver／Store 的 setup、连接池读写及清理均落在 `db_deepagent`，不修改旧持久化表 |
| 大结果 | 引用可读取，截断明确，不用不完整样本冒充总体 |
| 结构化证据 | evidence 字段来自真实执行记录，回答引用有效，计算输入可回查，失败不冒充证据 |
| 新旧共存 | 旧父图、旧子图和原 CLI 继续可用 |
| 测试发现 | `src/tests/db_deepagent/` 下的新测试由现有 pytest 配置默认发现 |

工具交接、只读执行、数据源绑定和结果完整性属于必要测试范围。真实业务验证应同时记录正确性、工具调用路径、时延和 Token 消耗，不以调用次数增加作为分析能力提升的证据。

固定题集、测试前置条件及通过线见 [4_db_deepagent固定验收题集与通过标准](4_db_deepagent固定验收题集与通过标准.md)：12 项业务任务、6 项遗留误判回归、12 项协议与边界用例，共 30 项。业务任务连续三轮每轮至少通过 11／12，遗留回归及协议边界每轮全部通过；旧入口原有必须通过的兼容性检查不得新增失败。题集和断言本次固定，参考 SQL、预期结果及数据快照在实现验收前独立复核并冻结，尚未冻结或无法执行的用例不能计为通过。

第一阶段不要求达到网络研究和完整报告 Agent 的范围，但输出应足以让未来的主分析 Agent 继续使用：包含数据发现、SQL 依据、数据引用及限制。

## 12. 本篇确定的设计决策

- 用 Deep Agents 自主工具调用替代原父子图固定业务步骤，检索、修复、分析和结束时机由 Agent 根据证据决定。
- 使用 `deepagents==0.7.25`，复用当前已在用的 LangChain／LangGraph 核心版本；新旧实现共用根目录 `.venv`，所需依赖统一在根项目中对齐。
- 新实现位于 `src/modules/db_deepagent/`，通过复制或封装复用代码，原实现保持可运行。
- Agent 自主生成与修复 SQL，Milvus／Neo4j 检索、校验、执行和分析作为工具提供。
- `validate_sql` 独立开放，执行必须关联可信校验结果，避免重复整套校验。
- PostgreSQL `orders` 专用于业务查询，`lg_persist` 专用于会话状态及归档，检索只使用 Milvus 和 Neo4j。
- 第一阶段通过独立 CLI 交付，支持与旧 CLI 对比，后续再接入主分析 Agent。
