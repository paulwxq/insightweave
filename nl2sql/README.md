# NL2SQL v3

基于 LangGraph 多智能体编排的生产级**自然语言转 SQL** 系统。用户用中文提问，系统自动路由查询、通过 RAG 检索相关 Schema、生成并验证 SQL、在 PostgreSQL 上执行，最终返回自然语言摘要。

---

## 目录

- [项目概述](#项目概述)
- [系统架构](#系统架构)
- [技术栈](#技术栈)
- [RAG 数据依赖：MetaWeave](#rag-数据依赖metaweave)
- [环境要求](#环境要求)
- [安装](#安装)
- [配置](#配置)
- [启动服务](#启动服务)
- [API 文档](#api-文档)
- [项目结构](#项目结构)
- [测试](#测试)
- [开发路线图](#开发路线图)

---

## 项目概述

NL2SQL v3 将自然语言业务问题转换为 SQL 查询并返回易于理解的答案，核心能力如下：

- **复杂度路由** — 基于 LLM 的分类器判断问题是否可用单条 SQL 完成（Fast Path）或需要多条依赖 SQL（Complex Path）。
- **RAG 辅助 Schema 检索** — 向量检索（pgvector 或 Milvus）+ 图数据库遍历（Neo4j），找到相关表、字段、JOIN 路径和历史 SQL 示例。
- **迭代式 SQL 生成与验证** — LLM 生成 SQL，多层验证器（语法、安全、语义 EXPLAIN）最多自动重试 3 次。
- **自然语言总结** — 最终结果由 LLM 以自然语言解释并返回给用户。
- **多轮对话** — 基于 PostgreSQL 的会话管理，支持按用户隔离的对话历史。
- **多 LLM 支持** — DashScope（通义千问）、OpenAI、OpenRouter、DeepSeek 以及任何 OpenAI 兼容接口。

---

## 系统架构

### 整体流程

```
用户问题
    |
    v
[ Router 路由节点 ]  -----(complex)-----> [ Complex Planner ] (Phase 2 开发中)
    |
 (simple)
    |
    v
[ Simple Planner 简单规划节点 ]
    |
    v
[ SQL 生成子图 ] <------ 验证失败自动重试（最多 3 次）------+
  |-- 问题解析（实体、时间、维度、指标提取 + 指代消解）       |
  |-- Schema 检索（向量检索 + 图检索 + 维度值匹配）          |
  |-- SQL 生成（LLM）                                      |
  +-- 验证（语法 / 安全 / 语义 EXPLAIN）  ----------------+
      |
      v（验证通过的 SQL）
[ SQL 执行节点 ]（PostgreSQL）
      |
      v
[ Summarizer 总结节点 ]（LLM）
      |
      v
  自然语言答案
```

### 父图（Father Graph）

顶层 LangGraph 编排器，包含以下节点：

| 节点 | 职责 |
|---|---|
| **Router** | 调用轻量 LLM，将问题分类为 `simple` 或 `complex` |
| **Simple Planner** | 为 Fast Path 准备子查询参数（纯函数，<1 ms） |
| **SQL Gen Wrapper** | 调用 SQL 生成子图，将输出映射回父图 State |
| **SQL Execution** | 在 PostgreSQL 上执行验证通过的 SQL |
| **Summarizer** | 将执行结果（或错误）转换为用户友好的自然语言回答 |

### SQL 生成子图

每个子查询独立调用的 LangGraph 子图：

| 节点 | 职责 |
|---|---|
| **Question Parsing** | 结构化提取时间范围、维度、指标等信息，并做指代消解 |
| **Schema Retrieval** | 向量检索相关表/列，Neo4j 生成 JOIN 计划，模糊匹配维度值 |
| **SQL Generation** | 基于结构化上下文由 LLM 生成 SQL |
| **Validation** | 多层校验：语法（sqlparse）、安全（关键词白名单）、语义（PostgreSQL `EXPLAIN`） |

### 执行路径

- **Fast Path**（Phase 1，当前版本）：单条 SQL 查询，端到端约 1–3 秒。
- **Complex Path**（Phase 2，开发中）：多条依赖 SQL 查询，支持依赖解析和并行执行。

---

## 技术栈

| 层次 | 技术 |
|---|---|
| 开发语言 | Python 3.12+ |
| 智能体编排 | LangGraph 1.0+，LangChain 1.0+ |
| API 服务 | FastAPI + Uvicorn |
| 查询数据库 | PostgreSQL（psycopg3） |
| 向量数据库 | pgvector **或** Milvus（通过配置切换） |
| 图数据库 | Neo4j 5.x（JOIN 路径规划） |
| LLM 提供商 | DashScope（通义千问）、OpenAI、OpenRouter、DeepSeek |
| 向量模型 | DashScope `text-embedding-v3`（1024 维） |
| SQL 解析 | sqlparse |
| 数据校验 | Pydantic v2 |
| 会话持久化 | LangGraph PostgresSaver |
| 包管理器 | uv |
| 测试框架 | pytest |

---

## RAG 数据依赖：MetaWeave

NL2SQL v3 依赖预先构建好的 RAG 数据来理解业务数据库的 Schema。这些数据由配套项目 **[MetaWeave](https://github.com/your-org/metaweave)**（链接待更新）生成。

MetaWeave 分析源表的结构与关联关系，并生成以下 RAG 数据：

| 向量集合 / 表 | 内容 |
|---|---|
| `table_schema_embeddings` | 每张表及每个字段的描述文本，嵌入为 1024 维向量，存入 pgvector 或 Milvus |
| `dim_value_embeddings` | 维度列的枚举值嵌入，用于模糊值匹配（例如将"肯德基"映射到 `brand_id=42`） |
| `sql_example_embeddings` | 历史验证通过的 SQL 示例嵌入，用于少样本检索 |
| Neo4j 图数据库 | 表关系图，供 JOIN 规划器通过 APOC Dijkstra 算法查找多跳 JOIN 路径 |

**在运行 NL2SQL v3 之前，必须先运行 MetaWeave 填充上述数据。** 若数据缺失，Schema 检索将返回空结果，SQL 生成将失败。

---

## 环境要求

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) 包管理器
- PostgreSQL 14+（pgvector 模式下需安装 `pgvector` 扩展）
- Neo4j 5.x
- Milvus 2.x（可选，使用 Milvus 模式时需要）
- 至少一个 LLM 提供商的 API Key

---

## 安装

```bash
# 克隆仓库
git clone https://github.com/your-org/nl2sql-v3.git
cd nl2sql-v3

# 按锁定文件同步模块环境（按需选择 extras：dev=pytest/black/ruff，monitoring=langsmith）
uv sync --python 3.12 --locked --extra dev --extra monitoring
```

原迁移期保留的 `web` 可选依赖（Streamlit 旧页面）已于 2026-11-10 随旧前端清理移除，见 [docs/3_NL2SQL旧前端清理设计.md](docs/3_NL2SQL旧前端清理设计.md)。

---

## 配置

全局连接及旧链路配置位于 `src/configs/config.yaml`，`db_deepagent` 角色配置位于 `src/modules/db_deepagent/config/db_deepagent.yaml`。以下按文件给出最小配置示例。

`src/configs/config.yaml`：

```yaml
# 连接及模型默认参数
llm_providers:
  dashscope:
    adapter: dashscope_native
    api_key: "sk-..."
    models:
      qwen-turbo: {}
      qwen-plus: {}
  dashscope_openai:
    adapter: openai_chat_completions
    api_key: "sk-..."
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    models:
      qwen3.8-max:
        params: {enable_thinking: false}
      qwen3.8-flash:
        params: {temperature: 0, max_tokens: 8192, enable_thinking: false}

# 向量数据库（二选一）
vector_database:
  active: pgvector          # 或 "milvus"
  pgvector:
    host: localhost
    port: 5432
    dbname: nl2sql
    user: postgres
    password: "..."
  milvus:
    host: localhost
    port: 19530
    db_name: nl2sql

# PostgreSQL（业务数据库，用于执行 SQL）
postgresql:
  host: localhost
  port: 5432
  dbname: your_business_db
  user: postgres
  password: "..."

# Neo4j（用于 JOIN 路径规划）
neo4j:
  uri: bolt://localhost:7687
  user: neo4j
  password: "..."

# LangGraph 持久化（对话历史）
langgraph_persistence:
  enabled: true
  checkpoint:
    father_enabled: true
    subgraph_enabled: false
```

`src/modules/db_deepagent/config/db_deepagent.yaml`：

```yaml
# db_deepagent 各角色在自身业务段选择模型
model:
  provider: dashscope_openai
  llm_name: qwen3.8-max
  params: {temperature: 0}
  request_timeout_seconds: 120
summarization:
  provider: dashscope_openai
  llm_name: qwen3.8-flash
  params: {}
  request_timeout_seconds: 120
time_resolution:
  provider: dashscope_openai
  llm_name: qwen3.8-flash
  params:
    response_format: {type: json_object}
    max_retries: 2
  request_timeout_seconds: 15
```

`params` 按键覆盖模型默认值，嵌套对象整项替换；`0` 和 `false` 是显式值。切换角色模型时应同时检查该角色的采样、思考、输出格式、超时和重试参数，不继承旧角色参数。参数校验错误会指出实际来源配置路径。只有 `deepseek + deepseek-flash` 及 `glm_coding` 下的三款 GLM 这四个组合才把显式 `max_tokens` 通过 `extra_body` 发送；其他 provider 的同名模型沿用普通构造映射。DeepSeek 的显式 `thinking.type` 只接受 `enabled` 或 `disabled`。Kimi 默认省略思考开关和 Qwen 的 `temperature: 0`，思考模式下显式温度只接受 `1`。GLM 的显式 `thinking` 只接受 `{"type": "enabled"}`；`disabled` 实测仍产生推理，工厂会在构造客户端前拒绝。

OpenAI 兼容调用必须通过 `request_timeout_seconds`、业务 `params.timeout` 或注册表默认 `params.timeout` 之一显式设置请求超时，三者都缺失时会在构造客户端前报错。实际生效的值必须是有限且大于 0 的数，布尔值不合法。`db_deepagent` 三个角色使用各自的 `request_timeout_seconds`，YAML 中的布尔值和引号数字字符串会在配置解析时拒绝，单独的 `${VAR}` 引用可在替换后提供数字；旧链路五个节点没有该字段，切换为兼容接口时必须在节点或注册表的 `params.timeout` 中配置。

上线前必须检查部署环境中的自定义 YAML，将旧 `llm_profile`、`overrides` 和平铺模型参数迁移为 `provider`、`llm_name`、`params`。Router 对旧配置按原有异常策略回退到默认复杂度（默认 `complex`），其他节点也保留各自异常处理；这不表示旧字段仍然生效。

常用密钥为 `DASHSCOPE_API_KEY`、`DEEPSEEK_API_KEY`、`GLM_API_KEY`、`KIMI_CODE_API_KEY`。DeepSeek 未设置 `DEEPSEEK_BASE_URI` 时使用 `https://api.deepseek.com`，显式空字符串会拒绝构造；GLM Coding 使用 `https://open.bigmodel.cn/api/coding/paas/v4`，Kimi Code 使用 `https://api.kimi.com/coding/v1`。GLM/Kimi 编程套餐的生产适用范围按 Doc 23 单独确认。

各节点的详细配置（模型选择、重试次数、超时时间等）位于：

- `src/modules/nl2sql_father/config/nl2sql_father_graph.yaml` — Router、SQL 执行、Summarizer
- `src/modules/sql_generation/config/sql_generation_subgraph.yaml` — 问题解析、Schema 检索、SQL 生成、验证

---

## 启动服务

### 当前 Web 入口（统一 Vue 前端 + /api/v2）

产品前端为仓库根目录的统一 Vue 应用（查询页 `/query`），NL2SQL 后端仅挂载 `/api/v2`。启动命令、端口约定（NL2SQL `127.0.0.1:8008`）与环境变量见根目录 [docs/1_服务启动指南.md](../docs/1_服务启动指南.md)：

```bash
uv run --no-sync uvicorn src.api.main:app --host 127.0.0.1 --port 8008
```

接口为 `/api/v2/*`（health、按源 readiness、会话/问答/结果分页/CSV 导出），完整清单见启动指南接口表。

### 旧链路历史参考（/api/v1 与旧父图）

以下内容为迁移期之前的旧 `/api/v1` 用法，当前应用已不挂载这些路由（`src/api/main.py` 仅挂载 v2），仅作旧父图脚本对照的历史参考：

```bash
# 历史命令示例（路由已不挂载，不可用于当前验证）
uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
curl -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" \
  -d '{"query": "查询2024年各月的销售额", "user_id": "demo"}'
```

### Python 直接调用（旧父图脚本入口）

```python
from src.modules.nl2sql_father.graph import run_nl2sql_query

result = run_nl2sql_query(
    query="查询2024年各月的销售额",
    user_id="alice",
)

print(result["summary"])            # 自然语言答案
print(result["sql"])                # 执行的 SQL
print(result["execution_results"])  # 原始行列数据
```

---

## API 文档（历史：旧 `/api/v1`）

以下为迁移期之前的旧 `/api/v1` 接口记录，当前应用已不挂载（见上文旧链路历史参考）；当前 Web 接口为 `/api/v2/*`，清单见 [docs/1_服务启动指南.md](../docs/1_服务启动指南.md)。

### POST `/api/v1/query`

提交自然语言查询。

**请求体：**

```json
{
  "query": "查询2024年各月的销售额",
  "user_id": "alice",
  "thread_id": "alice:1700000000000"
}
```

**响应：**

```json
{
  "code": 200,
  "message": "success",
  "trace_id": "q_a1b2c3d4",
  "data": {
    "user_query": "查询2024年各月的销售额",
    "query_id": "q_a1b2c3d4",
    "thread_id": "alice:1700000000000",
    "complexity": "simple",
    "path_taken": "fast",
    "summary": "2024年各月销售额如下：1月 12万元，2月 9.5万元，……",
    "sql": "SELECT DATE_TRUNC('month', order_date) AS month, SUM(amount) AS total FROM orders WHERE ...",
    "sub_queries": [...],
    "execution_results": [
      {
        "success": true,
        "columns": ["month", "total_amount"],
        "rows": [["2024-01-01", 120000.0], "..."],
        "row_count": 12,
        "execution_time_ms": 48.3
      }
    ],
    "metadata": {
      "total_execution_time_ms": 1823.5,
      "router_latency_ms": 145.2
    }
  }
}
```

### GET `/api/v1/health`

健康检查接口，返回 `{"status": "ok"}`。

### GET `/api/v1/history`

获取指定会话的对话历史（需开启持久化）。

---

## 项目结构

```
nl2sql-v3/
├── src/
│   ├── api/                              # FastAPI 应用
│   │   ├── main.py                       # 应用入口，生命周期钩子
│   │   ├── routers/                      # query.py, history.py
│   │   └── schemas/                      # Pydantic 请求/响应模型
│   │
│   ├── modules/
│   │   ├── nl2sql_father/                # 父图（顶层编排器）
│   │   │   ├── graph.py                  # 图编译与 run_nl2sql_query()
│   │   │   ├── state.py                  # NL2SQLFatherState 定义
│   │   │   ├── nodes/                    # router, simple_planner, sql_execution, summarizer ...
│   │   │   └── config/                   # nl2sql_father_graph.yaml
│   │   │
│   │   └── sql_generation/               # SQL 生成子图
│   │       └── subgraph/
│   │           ├── create_subgraph.py    # 子图编译
│   │           ├── state.py              # SQLGenerationState 定义
│   │           └── nodes/               # question_parsing, schema_retrieval, sql_generation, validation
│   │
│   ├── services/
│   │   ├── llm_factory.py               # 多提供商 LLM 工厂（DashScope / OpenAI / DeepSeek）
│   │   ├── config_loader.py             # YAML 配置加载器（支持环境变量插值）
│   │   ├── embedding/                   # 向量嵌入客户端
│   │   ├── vector_adapter/              # pgvector / Milvus 适配器（工厂模式，可切换）
│   │   ├── db/                          # PostgreSQL 客户端，Neo4j 客户端
│   │   └── langgraph_persistence/       # PostgresSaver 检查点，对话历史读写
│   │
│   ├── tools/
│   │   ├── schema_retrieval/
│   │   │   ├── retriever.py             # SchemaRetriever 协调器
│   │   │   ├── join_planner.py          # 基于 Neo4j 的 JOIN 路径规划
│   │   │   └── value_matcher.py         # 维度值模糊匹配
│   │   └── validation/
│   │       └── sql_validation.py        # 多层 SQL 验证器
│   │
│   ├── prompts/                         # LLM 提示词模板
│   └── configs/                         # 全局 config.yaml
│
├── tests/                               # 测试工具与调试脚本
├── docs/                                # 设计文档
├── configs/                             # 额外配置文件
├── pyproject.toml
└── README.md
```

---

## 测试

```bash
# 运行所有单元测试
uv run pytest src/tests/unit/ -v

# 运行父图单元测试
uv run pytest src/tests/unit/nl2sql_father/ -v

# 运行集成测试（需要真实数据库连接）
uv run pytest src/tests/integration/ -v

```

---

## 开发路线图

- [x] **Phase 1 — Fast Path**：完整 RAG 流水线，支持单条 SQL 查询
- [x] 多 LLM 提供商支持（DashScope、OpenAI、DeepSeek、OpenRouter）
- [x] 向量数据库后端可切换（pgvector / Milvus）
- [x] 基于 PostgreSQL 的对话历史持久化
- [x] 多层 SQL 验证 + 自动重试
- [x] **Phase 2 — Complex Path**：多步依赖 SQL 查询，支持并行执行
- [ ] 流式 API 响应
- [x] Web 界面（统一 Vue 前端 `/query`；旧 Streamlit 页面已于 2026-11-10 清理，见 [docs/3](docs/3_NL2SQL旧前端清理设计.md)）
- [ ] LangSmith 可观测性集成

---

## 许可证

`Copyright (C) 2026 paulwxq <paul_wang77@hotmail.com>`

本项目采用 [GNU Affero General Public License v3.0 only](../LICENSE)（`AGPL-3.0-only`）。关于商业使用、网络服务的对应源码义务、第三方许可及历史授权，请参阅[许可证说明](../docs/许可证说明.md)。
