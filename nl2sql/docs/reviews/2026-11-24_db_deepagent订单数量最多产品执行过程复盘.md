# db_deepagent“订单数量最多的产品”执行过程复盘

## 1. 文档目的

本文基于 2026-11-24 的一次真实 Web 问数运行，按时间顺序还原 db_deepagent 从接收到问题到生成、校验、执行 SQL，再到保存结果和提交最终答案的完整过程。

用户问题：

> 请问订单数量最多的产品是哪个？

最终答案：

> 订单数量最多的产品是「商品05」，共有 19 笔订单。

本文重点记录：

- 每次主模型调用选择了什么工具；
- 每次工具调用的完整参数；
- 工具返回中与后续决策有关的原始结果；
- SQL、执行计划、查询结果及证据之间的对应关系；
- 工具调用是否并行；
- 最终答案、运行归档和结果文件如何形成。

## 2. 分析对象与证据来源

### 2.1 运行标识

| 项目 | 值 |
| --- | --- |
| 数据源 | `db_src_1`，PostgreSQL `orders` 库 |
| 数据源指纹 | `pg:877bcf0e30aee888` |
| 会话 ID | `s_924686a27e3c414fbb67f0f99f444943` |
| LangGraph 线程 ID | `dbda:s_924686a27e3c414fbb67f0f99f444943:1` |
| 请求 ID | `req_862fb000d6264634b9a76a4c5a99b4fa` |
| 运行 ID | `run_8f7623e000754e128142ad5d4bae37e4` |
| 主模型 | `qwen3.8-max`，OpenAI 兼容调用方式 |
| 运行状态 | `completed` |
| 总耗时 | 43,683 ms，页面显示为 43.7 秒 |
| 主模型调用 | 7 次 |
| 工具调用 | 7 次，涉及 6 种工具 |
| Token | 输入 90,304；输出 720；统计完整 |

这是同一会话中的第二轮问题。第一轮“当前数据库中有多少张表”的检索结果和答案已经存在于会话上下文中；本轮第一次模型调用的审计记录为 `input_messages=6`。本轮没有沿用第一轮的业务查询结果，因为第一轮没有执行 SQL。

### 2.2 核对过的原始记录

1. nl2sql 容器日志：`/app/nl2sql/logs/nl2sql.log`，用于核对时间、模型调用序号、工具名称、工具调用 ID 和运行结束时间。
2. 会话模型审计：`sessions/s_924686a27e3c414fbb67f0f99f444943/audit.jsonl`，用于核对每次模型调用的耗时、输入消息数和 Token。
3. LangGraph checkpoint：上述线程从 step 17 到 step 64 的记录，用于还原 AI 消息中的工具参数及 ToolMessage 中的原始返回。
4. 运行归档：`sessions/s_924686a27e3c414fbb67f0f99f444943/runs/req_862fb000d6264634b9a76a4c5a99b4fa.json`，用于核对最终答案、证据和运行统计。
5. 结果文件：`results/res_dc0405c1aa4242979be6d583533eed0b/meta.json` 与 `rows.jsonl`，用于核对实际执行 SQL、字段类型和保存行。

上述第 2～5 项在容器内均位于 `/app/nl2sql/var/db_deepagent/` 下。

## 3. 全流程总览

实际调用链如下。`M` 表示一次主模型调用，`T` 表示紧随其后的工具执行。

```text
接收问题并完成数据源、知识版本、平台表和 Agent 装配检查
  ↓
M1：决定读取 query-planning 技能
  ↓
T1：read_file(/skills/query-planning/SKILL.md, limit=1000)
  ↓
M2：根据上下文确定检查 orders 和 products
  ↓
T2：inspect_tables([orders, products], include_live_columns=true)
  ↓
M3：登记一个业务任务 T1
  ↓
T3：record_sub_tasks(add T1) → plan_version=1，T1=pending
  ↓
M4：生成 SQL 并请求校验
  ↓
T4：validate_sql(SQL, T1@1) → all_passed，附顺序扫描警告和执行计划
  ↓
M5：使用完全相同的 SQL 请求执行
  ↓
T5：execute_sql(SQL, T1@1) → 商品05 / 19，生成 E-06f9384c 和结果文件
  ↓
M6：把 T1 标记为完成并选定执行证据
  ↓
T6：record_sub_tasks(status=completed) → plan_version=2
  ↓
M7：提交最终答案
  ↓
T7：submit_final_answer → 已接收最终答案，FinalAnswerGate 结束运行
  ↓
运行结果归档；Web 返回答案、SQL 证据和结果引用
```

**并行性结论：本轮没有并行工具调用。** 每次 AI 消息只有一个 `tool_call`，对应工具返回后才开始下一次模型调用。`inspect_tables` 一次检查两张表是单次工具调用，不是两个并行调用。

## 4. 时间线总表

日志时间精确到秒；模型耗时和 Token 来自 `audit.jsonl`。工具调用发生在对应模型调用结束之后。

| 顺序 | 时间（2026-11-24） | 模型耗时 | 输入/输出 Token | 模型选择的工具 | 工具结果摘要 |
| --- | --- | ---: | ---: | --- | --- |
| 准备 | 21:29:46～21:29:47 | — | — | — | 数据源、Milvus、平台表核对完成，装配 16 个工具 |
| M1/T1 | 21:29:47～21:29:57 | 10,020 ms | 10,562 / 48 | `read_file` | 返回 query-planning 技能全文，共 54 行 |
| M2/T2 | 21:29:57～21:30:03 | 5,634 ms | 12,015 / 50 | `inspect_tables` | 返回 orders、products 的表卡片和实时列结构 |
| M3/T3 | 21:30:03～21:30:08 | 4,437 ms | 12,897 / 79 | `record_sub_tasks` | 新增 T1，`plan_version=1`，状态 `pending` |
| M4/T4 | 21:30:08～21:30:13 | 5,755 ms | 13,088 / 100 | `validate_sql` | `all_passed`，返回执行计划和 Seq Scan 警告 |
| M5/T5 | 21:30:13～21:30:17 | 3,756 ms | 13,531 / 100 | `execute_sql` | 返回 1 行：商品05、19；SQL 本身耗时 9 ms |
| M6/T6 | 21:30:17～21:30:22 | 4,864 ms | 14,010 / 80 | `record_sub_tasks` | T1 完成，绑定证据，`plan_version=2` |
| M7/T7 | 21:30:22～21:30:31 | 9,027 ms | 14,201 / 263 | `submit_final_answer` | 最终答案被接受，运行结束 |

7 次模型调用耗时合计 43,493 ms；与运行总耗时 43,683 ms 相差约 190 ms，包含工具执行和运行收尾等开销。

## 5. 分步还原

### 5.0 接收请求与 Agent 装配

在进入主 Agent 循环前，Web 执行链完成会话、数据源和知识状态检查，并构造本轮运行所需的上下文。容器日志显示：

- 21:29:46～21:29:47：识别业务库为 `orders`，数据源指纹为 `pg:877bcf0e30aee888`；
- 初始化 Embedding 客户端并连接 Milvus；
- 平台库表核对通过：`checkpoints`、`checkpoint_blobs`、`checkpoint_writes`、`checkpoint_migrations`、`store`、`store_migrations`；
- 命中模型配置 `openai:qwen3.8-max`；
- 21:29:47：`db_deepagent 已装配`，会话正确，工具数为 16；
- 第一次模型调用预算预占成功，`remaining=39`。

本轮可用工具为：

```text
ls, read_file, glob, grep,
search_schema, inspect_tables, plan_joins,
resolve_time_range, probe_dimension_values, reuse_evidence,
validate_sql, execute_sql, read_result, analyze_result,
record_sub_tasks, submit_final_answer
```

### 5.1 第 1 次模型调用：读取查询规划技能

模型没有立即生成 SQL，而是先读取与本问题匹配的 `query-planning` 技能。

工具调用：

```json
{
  "name": "read_file",
  "id": "call_9c27fae0f3fe4115a4849d07",
  "args": {
    "file_path": "/skills/query-planning/SKILL.md",
    "limit": 1000
  }
}
```

工具于 21:29:57 成功返回：

```text
@@ lines 1-54 of 54 @@
---
name: query-planning
description: 识别指标、时间、维度、比较基准与粒度，并规划查询步骤；...
---
```

返回的是完整 54 行技能文本。与本次后续执行直接相关的规则包括：

- 识别指标、时间、维度、粒度、排名和并列要求；
- 单条 SQL 能完成时优先一次完成，不预设“简单／复杂”两套流程；
- 需要取数时先调用 `record_sub_tasks` 登记业务任务；
- 编写 SQL 前通过表卡片和字段确认结构及关联方式；
- SQL 必须先 `validate_sql`，再用相同文本 `execute_sql`；
- 任务完成后，单独调用 `submit_final_answer`。

本次模型输出只有工具调用，没有面向用户的自然语言正文。

### 5.2 第 2 次模型调用：检查订单表和产品表

模型随后在一次调用中指定检查 `orders` 和 `products`，并要求合并业务库实时列结构。

工具调用：

```json
{
  "name": "inspect_tables",
  "id": "call_12aa51923d3943e4af786f12",
  "args": {
    "table_ids": [
      "orders.public.orders",
      "orders.public.products"
    ],
    "include_live_columns": true
  }
}
```

工具于 21:30:03 成功返回，顶层状态为 `ok`，`warnings=[]`。两张表的关键返回如下。

#### `public.orders`

- 三段表标识：`orders.public.orders`；
- SQL 显示名：`public.orders`；
- 类别：`fact`，类别组：`fact`；
- 时间字段提示：`order_date`；
- 表卡片业务说明：订单交易明细事实表；
- 实时列结构与表卡片一致，`card_only_columns=[]`、`live_only_columns=[]`、表级 `warnings=[]`。

| 列名 | 实时类型 | 是否可空 | 表卡片含义 |
| --- | --- | --- | --- |
| `order_id` | `integer` | 否 | 订单唯一标识 ID |
| `order_date` | `date` | 是 | 订单创建日期 |
| `user_id` | `integer` | 是 | 下单用户 ID |
| `product_id` | `integer` | 是 | 购买产品 ID |
| `quantity` | `integer` | 是 | 商品购买数量 |
| `amount` | `numeric` | 是 | 订单总金额 |

#### `public.products`

- 三段表标识：`orders.public.products`；
- SQL 显示名：`public.products`；
- 类别：`dim`，类别组：`dimension`；
- 表卡片业务说明：产品信息维度表；
- 实时列结构与表卡片一致，无缺失列或表级警告。

| 列名 | 实时类型 | 是否可空 | 表卡片含义 |
| --- | --- | --- | --- |
| `product_id` | `integer` | 否 | 产品唯一标识 |
| `product_name` | `character varying` | 否 | 产品名称 |
| `category_id` | `integer` | 是 | 所属分类 ID |

这次返回确认了后续 SQL 所需的全部结构：

- 在 `public.orders` 中统计 `order_id`；
- 通过两表共有的 `product_id` 关联；
- 从 `public.products` 取 `product_name`。

本轮没有再次调用 `search_schema`，也没有调用 `plan_joins`；第一轮的表清单已经在会话上下文中，本轮直接使用 `inspect_tables` 核对两张目标表。

### 5.3 第 3 次模型调用：登记业务任务 T1

模型把用户问题登记为一个业务任务，明确采用“订单行数”口径，输出产品名称与订单数。

工具调用：

```json
{
  "name": "record_sub_tasks",
  "id": "call_34f0cfde019b475db82b5e68",
  "args": {
    "expected_plan_version": 0,
    "operations": [
      {
        "op": "add",
        "sub_task_id": "T1",
        "content": "查询订单数量最多的产品（按订单行数统计，返回产品名称与订单数）"
      }
    ]
  }
}
```

工具于 21:30:08 返回：

```json
{
  "status": "ok",
  "data": {
    "plan_version": 1,
    "no_op": false,
    "operations": [
      {
        "op": "add",
        "sub_task_id": "T1",
        "no_op": false,
        "current_status": "pending"
      }
    ]
  },
  "warnings": []
}
```

checkpoint 中的任务记录进一步确认：

- `T1` 的 `revision=1`；
- 无依赖任务，`dependencies=[]`；
- 创建时间为 `2026-11-24T21:30:08+08:00`；
- 后续 SQL 校验和执行都必须携带 `sub_task_id=T1`、`sub_task_revision=1`。

### 5.4 第 4 次模型调用：生成并校验 SQL

模型生成如下 SQL，并调用 `validate_sql`：

```sql
SELECT p.product_name, COUNT(o.order_id) AS order_count
FROM public.orders o
JOIN public.products p ON p.product_id = o.product_id
GROUP BY p.product_name
ORDER BY order_count DESC
LIMIT 1;
```

工具参数：

```json
{
  "name": "validate_sql",
  "id": "call_8cfa6aea8a3544e49590bb9d",
  "args": {
    "sql": "SELECT p.product_name, COUNT(o.order_id) AS order_count\nFROM public.orders o\nJOIN public.products p ON p.product_id = o.product_id\nGROUP BY p.product_name\nORDER BY order_count DESC\nLIMIT 1;",
    "sub_task_id": "T1",
    "sub_task_revision": 1
  }
}
```

工具于 21:30:13 返回 `status=ok`，校验层级为 `all_passed`。这意味着 SQL 可以进入执行阶段，但校验工具同时给出了性能警告：

```json
{
  "layer": "all_passed",
  "warnings": [
    "检测到顺序扫描（Seq Scan），可能影响性能"
  ]
}
```

返回的完整执行计划为：

```text
Limit  (cost=127.36..127.36 rows=1 width=226)
  ->  Sort  (cost=127.36..127.86 rows=200 width=226)
        Sort Key: (count(o.order_id)) DESC
        ->  HashAggregate  (cost=124.36..126.36 rows=200 width=226)
              Group Key: p.product_name
              ->  Hash Join  (cost=17.20..114.20 rows=2032 width=222)
                    Hash Cond: (o.product_id = p.product_id)
                    ->  Seq Scan on orders o  (cost=0.00..22.70 rows=1270 width=8)
                    ->  Hash  (cost=13.20..13.20 rows=320 width=222)
                          ->  Seq Scan on products p  (cost=0.00..13.20 rows=320 width=222)
```

执行计划表明 PostgreSQL 计划采用：

1. 分别顺序扫描 `orders` 和 `products`；
2. 以 `o.product_id = p.product_id` 做 Hash Join；
3. 按 `p.product_name` 做 HashAggregate；
4. 按 `COUNT(o.order_id)` 降序排序；
5. 最终只取 1 行。

校验记录中的规范化 SQL 与后续执行 SQL 的 SHA-256 均为：

```text
8977e06a0a4b640460c8d8b82a8529394d22cff09a9e0dd7e3ffec5b0c64d0ee
```

### 5.5 第 5 次模型调用：执行同一条 SQL

校验通过后，模型使用完全相同的 SQL、任务 ID 和修订号调用 `execute_sql`：

```json
{
  "name": "execute_sql",
  "id": "call_426701c929c541dd93ab3ef0",
  "args": {
    "sql": "SELECT p.product_name, COUNT(o.order_id) AS order_count\nFROM public.orders o\nJOIN public.products p ON p.product_id = o.product_id\nGROUP BY p.product_name\nORDER BY order_count DESC\nLIMIT 1;",
    "sub_task_id": "T1",
    "sub_task_revision": 1
  }
}
```

工具于 21:30:17 成功返回：

```json
{
  "status": "ok",
  "data": {
    "evidence_id": "E-06f9384c",
    "result_ref": "res_dc0405c1aa4242979be6d583533eed0b",
    "columns": [
      {"name": "product_name", "pg_type": "varchar"},
      {"name": "order_count", "pg_type": "int8"}
    ],
    "saved_row_count": 1,
    "source_truncated": false,
    "preview_rows": [
      ["商品05", 19]
    ],
    "preview_truncated": false,
    "elapsed_ms": 9,
    "source_evidence_ref": {
      "session_id": "s_924686a27e3c414fbb67f0f99f444943",
      "thread_id": "dbda:s_924686a27e3c414fbb67f0f99f444943:1",
      "run_id": "run_8f7623e000754e128142ad5d4bae37e4",
      "evidence_id": "E-06f9384c"
    }
  },
  "warnings": []
}
```

关键点：

- 实际结果只有 1 行：`商品05 / 19`；
- 源结果未截断，预览也未截断，因此后续不需要 `read_result` 分页读取；
- SQL 在业务库中的执行耗时为 9 ms，不等于整轮 43.7 秒；
- 工具同时生成执行证据 `E-06f9384c` 和可读取的结果引用 `res_dc0405c1aa4242979be6d583533eed0b`。

由于返回预览已经完整且问题只需直接读出 Top 1，本轮没有再调用 `analyze_result`。

### 5.6 第 6 次模型调用：将 T1 标记为完成

模型选择刚才生成的执行证据，把 T1 标记为完成。

工具调用：

```json
{
  "name": "record_sub_tasks",
  "id": "call_6ff9831905c24303a17bf187",
  "args": {
    "expected_plan_version": 1,
    "operations": [
      {
        "op": "status",
        "sub_task_id": "T1",
        "status": "completed",
        "evidence_ids": ["E-06f9384c"]
      }
    ]
  }
}
```

工具于 21:30:22 返回：

```json
{
  "status": "ok",
  "data": {
    "plan_version": 2,
    "no_op": false,
    "operations": [
      {
        "op": "status",
        "sub_task_id": "T1",
        "no_op": false,
        "status": "completed"
      }
    ]
  },
  "warnings": []
}
```

此时 checkpoint 中 T1 的状态变为 `completed`，证据列表为 `["E-06f9384c"]`，更新时间为 `2026-11-24T21:30:22+08:00`。全部已登记任务进入终态，满足提交最终答案的前置条件。

### 5.7 第 7 次模型调用：提交最终答案

模型调用 `submit_final_answer`，直接回答用户问题，同时提交结构化发现、统计口径和限制。

主要参数为：

```json
{
  "status": "completed",
  "answer": "订单数量最多的产品是「商品05」，共有 19 笔订单。",
  "findings": [
    {
      "text": "订单数量最多的产品是「商品05」，共有 19 笔订单（按订单行数 COUNT(order_id) 统计）。",
      "finding_id": "F-1",
      "task_refs": [
        {"sub_task_id": "T1", "revision": 1}
      ],
      "evidence_ids": ["E-06f9384c"]
    }
  ],
  "metric_definitions": [
    "订单数量：按 public.orders 表的 order_id 计数（COUNT(order_id)），即订单行数；未限定时间范围，统计全量数据。"
  ],
  "limitations": [
    "未限定时间范围，结果为历史全量订单统计。",
    "若存在并列第一的情况，仅返回其中一条（LIMIT 1）。"
  ],
  "open_questions": [],
  "unresolved_task_refs": []
}
```

工具调用 ID 为 `call_9fa6a9db78bb44b0adf8280c`。21:30:31 的日志依次记录：

```text
最终答案已接受: status=completed findings=1
tool=submit_final_answer ... 结果=command
最终答案已登记，运行结束
模型观测：total=7 summary=0 tokens=90304/720 complete=True
```

ToolMessage 返回内容为“已接收最终答案”。FinalAnswerGate 随后结束 Agent 循环，不再发起第 8 次模型调用。

## 6. SQL、结果文件与最终答案的对应关系

### 6.1 执行证据

运行归档中的证据记录为：

| 字段 | 值 |
| --- | --- |
| `evidence_id` | `E-06f9384c` |
| `kind` | `sql_query` |
| 来源工具调用 | `call_426701c929c541dd93ab3ef0`，即 `execute_sql` |
| `result_ref` | `res_dc0405c1aa4242979be6d583533eed0b` |
| `row_count` | 1 |
| `preview_truncated` | `false` |
| `source_truncated` | `false` |
| `warnings` | `[]` |

最终发现 `F-1` 引用该证据，T1 也选择该证据作为任务成果，因此形成以下可追溯链：

```text
用户问题
  → T1@1
  → validate_sql
  → execute_sql / call_426701c929c541dd93ab3ef0
  → E-06f9384c
  → res_dc0405c1aa4242979be6d583533eed0b
  → F-1
  → 最终答案
```

### 6.2 结果元数据

`meta.json` 核实：

- `purpose=query`；
- `source_task.sub_task_id=T1`，`revision=1`；
- `sql_template` 与 `executed_sql` 完全一致；
- `executed_sql_sha256=8977e06a0a4b640460c8d8b82a8529394d22cff09a9e0dd7e3ffec5b0c64d0ee`；
- 字段为 `product_name/varchar` 和 `order_count/int8`；
- `saved_row_count=1`；
- `source_truncated=false`；
- 创建时间为 `2026-11-24T21:30:17+08:00`。

`rows.jsonl` 中保存的唯一一行是：

```json
[
  {"kind": "text", "value": "商品05"},
  {"kind": "integer", "value": 19}
]
```

因此，页面展示的“商品05、19”和“收起 SQL”中的 SQL 均来自同一条 `execute_sql` 证据，而不是模型另外编写的展示内容。

## 7. 并行调用与未使用工具核查

### 7.1 为什么判定没有并行调用

checkpoint 中本轮有 7 条带工具调用的 AI 消息，每条消息的 `tool_calls` 数组长度都为 1；对应存在 7 个工具返回，调用 ID 一一匹配：

| 次序 | 工具调用 ID | 工具 | 下一个动作 |
| --- | --- | --- | --- |
| 1 | `call_9c27fae0f3fe4115a4849d07` | `read_file` | 返回技能文本后才进入 M2 |
| 2 | `call_12aa51923d3943e4af786f12` | `inspect_tables` | 返回两张表结构后才进入 M3 |
| 3 | `call_34f0cfde019b475db82b5e68` | `record_sub_tasks` | 返回计划版本 1 后才进入 M4 |
| 4 | `call_8cfa6aea8a3544e49590bb9d` | `validate_sql` | 校验通过后才进入 M5 |
| 5 | `call_426701c929c541dd93ab3ef0` | `execute_sql` | 返回结果和证据后才进入 M6 |
| 6 | `call_6ff9831905c24303a17bf187` | `record_sub_tasks` | 返回计划版本 2 后才进入 M7 |
| 7 | `call_9fa6a9db78bb44b0adf8280c` | `submit_final_answer` | 接受答案并结束运行 |

特别是 `validate_sql` 和 `execute_sql` 严格串行，符合“先校验、再用相同文本执行”的约束。

### 7.2 本轮没有调用的工具

- 未调用 `search_schema`：本轮直接使用上下文中的候选表，再通过 `inspect_tables` 核对结构；
- 未调用 `plan_joins`：本轮没有单独请求 JOIN 图规划；
- 未调用 `resolve_time_range`：问题没有时间条件；
- 未调用 `probe_dimension_values`：问题没有指定需要过滤的产品值；
- 未调用 `reuse_evidence`：本轮产生了新的 SQL 执行证据；
- 未调用 `read_result`：结果仅 1 行且预览完整；
- 未调用 `analyze_result`：无需对保存结果做二次计算；
- 未调用 `ls`、`glob`、`grep`：只需读取一个明确的技能文件路径。

## 8. 统计口径与结果边界

本次“订单数量”的实际定义是：

```sql
COUNT(o.order_id)
```

即按 `public.orders` 中订单行的 `order_id` 计数，不是：

- `SUM(o.quantity)`：商品购买件数；
- `COUNT(DISTINCT o.order_id)`：去重订单数；
- `SUM(o.amount)`：订单金额。

SQL 先按产品名称分组，再按订单行数降序排列，最后 `LIMIT 1`。最终答案正确反映了本次 SQL 的结果，但也有两个已登记的边界：

1. 没有时间过滤，统计的是当时业务库中的历史全量数据；
2. 如果多个产品并列第一，`LIMIT 1` 只返回其中一条，不能据此列出全部并列产品。

## 9. 可重放的调用顺序

若只保留复现本次执行链所需的动作，顺序如下；不能调整 `validate_sql`、`execute_sql` 和任务完成登记的先后关系：

```text
1. read_file(file_path="/skills/query-planning/SKILL.md", limit=1000)

2. inspect_tables(
     table_ids=["orders.public.orders", "orders.public.products"],
     include_live_columns=true
   )

3. record_sub_tasks(
     expected_plan_version=0,
     operations=[add T1：查询订单数量最多的产品（按订单行数统计）]
   )

4. validate_sql(
     sql=本文第 5.4 节 SQL,
     sub_task_id="T1",
     sub_task_revision=1
   )

5. execute_sql(
     sql=与第 4 步完全相同的文本,
     sub_task_id="T1",
     sub_task_revision=1
   )

6. record_sub_tasks(
     expected_plan_version=1,
     operations=[status T1=completed, evidence_ids=["E-06f9384c"]]
   )

7. submit_final_answer(
     status="completed",
     answer="订单数量最多的产品是「商品05」，共有 19 笔订单。",
     findings=[F-1 引用 T1@1 和 E-06f9384c],
     metric_definitions=本文第 5.7 节内容,
     limitations=本文第 5.7 节内容
   )
```

本次真实运行表明，db_deepagent 并不是“收到问题后直接执行一条模型生成的 SQL”，而是经过技能读取、表结构确认、任务登记、SQL 校验、SQL 执行、证据绑定、任务完成和最终答案提交这一串有状态、可追溯的步骤。
