# db_deepagent 执行过程追踪：2026年9月份订单数查询

> 生成日期：2026-11-15。来源：`logs/nl2sql.log`（doc 13 上下文日志）+ 运行封存记录。
> 问题：**"请问2026年9月份的订单有多少个？"**
> run_id：`run_17d1dd668c584f14be4f634ca055112b`；request_id：`req_2a85704efe9c40e982948b454e57df25`；
> 线程：`dbda:s_f331217c70a942d48f787b5634bcf0a8:1`；模型：qwen3.8-max。
> 结果：**33 个订单，status=completed，一次提交通过**。全程 12:25:44 → 12:26:00（约 16 秒），
> **8 次模型调用、7 次业务工具调用、零拒绝零重试**。本文内容为日志原文完整摘录，无删减。

---

## 一、概要步骤列表

| # | 时刻 | 步骤 | 调用的工具 | 结果要点 |
|---|---|---|---|---|
| 1 | 12:25:44 | 模型调用①：检索表列 | search_schema | 命中表 public.orders（0.62）+ 4 列 |
| 2 | 12:25:46 | 模型调用②：确认表结构 | inspect_tables | 卡片+实时列一致，order_date/date、order_id/integer |
| 3 | 12:25:47 | 模型调用③：解析时间 | resolve_time_range | T-be52c4d4：[2026-09-01, 2026-10-01) 半开区间+sql_hint |
| 4 | 12:25:49 | 模型调用④：登记任务 | record_sub_tasks(add) | T1"查询2026年9月份的订单数量"，plan_version=1 |
| 5 | 12:25:51 | 模型调用⑤：**生成 SQL 并校验** | validate_sql | valid=True（一次过，guard 时间字面值溯源通过） |
| 6 | 12:25:53 | 模型调用⑥：执行 | execute_sql | 33，evidence E-8f3d0259，7ms |
| 7 | 12:25:55 | 模型调用⑦：任务终态 | record_sub_tasks(status=completed) | plan_version=2 |
| 8 | 12:25:57 | 模型调用⑧：提交答案 | submit_final_answer | 门控通过，运行结束 |

**未调用的工具**（本问题不需要，懒加载省略）：read_file（未读 skill 文件）、plan_joins（单表）、
search_schema(dim_values)（无维度过滤值）、probe_dimension_values、search_schema(sql_examples)、
read_result / analyze_result（单行完整结果）、reuse_evidence（新线程无历史）。

## 二、调用的工具列表

| 工具 | 调用次数 | 本次用途 |
|---|---|---|
| search_schema | 1 | kinds=[tables,columns]，query="订单 order 时间 日期" |
| inspect_tables | 1 | 确认 public.orders 列结构（卡片×实时双源） |
| resolve_time_range | 1 | kind=calendar, year=2026, month=9 |
| record_sub_tasks | 2 | add T1 → status completed（挂 evidence） |
| validate_sql | 1 | 三层校验+guard，写授权记录 |
| execute_sql | 1 | 只读执行，同文本握手 |
| submit_final_answer | 1 | 最终答案（findings 引 evidence） |

---

## 三、每次调用都携带的固定输入

每次模型调用输入 = 系统提示词（下文，8.1KB，含 Skills System 段）+ 16 个工具定义（26KB，
名单见下，完整 JSON 见 `logs/model_input_sqlgen_0915.txt` 末尾 [Tools] 段）+ 截至该时刻的消息历史（第四节逐次列出）。

### 3.1 系统提示词（全文，日志原文）

```text
你是数据库查询与分析 Agent，数据源为 PostgreSQL 的 orders 库，只读访问。

## 角色与证据
- 所有数据结论必须来自本次实际执行的查询或结果计算，或经 reuse_evidence
  受控沿用并标注来源的历史结果；时间／维度证据不能单独支撑业务数值结论。
- 禁止编造数值、SQL、行数、result_ref、evidence_id。

## 工作方式
- 根据问题与工具反馈自主决定下一步；优先一条合理的 SQL，存在真实的结果
  依赖时再分步；检索不足时补充检索；校验失败时修改 SQL、检查结构或补充
  证据，而不是重复提交同一 SQL。

## 表与字段
- 只使用 inspect_tables 确认过的表和列；SQL 中使用两段式 schema.table。
- JOIN 依据 plan_joins 返回的条件；多表查询中所有列都带表别名，过滤条件
  写在基表所在的查询层。

## 时间
- 时间过滤边界只能来自 resolve_time_range，或经 reuse_evidence 沿用的
  已确认证据（如线程交接清单中的口径）。
- 有限区间使用 `>= start AND < end`；单边区间仅使用证据非空端对应的
  `>= start` 或 `< end`，不擅自补另一端。
- 不使用 CURRENT_DATE、now() 等实时时钟；裸“当前／目前／现在”不是时间
  范围，需要范围时向用户澄清。

## 维度值
- 过滤值必须来自维度值命中、探针确认或前序结果；已确认文本列的
  `= ''`／`<> ''`／IS NULL／IS NOT NULL 检查除外。
- 未命中的词不能直接写进条件，也不能改用模糊匹配或替换为相似值；无法
  确认时如实说明或向用户澄清。

## 业务任务登记（doc 6.3）
- 回答任何问题前先用 record_sub_tasks 登记本轮业务任务（简单问题也
  登记一项）；任务 = 需要交付的业务问题或必要中间结果，不是工具步骤。
- record_sub_tasks 必须单独调用；每次携带返回的当前 plan_version，
  版本冲突时按返回的任务摘要重新确认后整批重提。
- validate_sql／execute_sql／analyze_result 必须携带目标任务的
  sub_task_id 与 sub_task_revision；下游任务的声明依赖全部 completed
  后才能校验／执行／分析。
- 任务取得成果后用 status=completed 登记选定成果 evidence_ids；不能
  继续推进时按实际情况登记 failed（工具失败附 failure_refs、主动放弃
  用 agent_abandoned、上游阻塞用 dependency_blocked+blocked_by）、
  needs_clarification（附 clarification_question）或 void（计划替换附
  replacement）。全部任务进入终态后才能提交。
- 修改任务内容或依赖用 revise（新版本 pending、成果清空）；终态不能
  原地恢复。作废的任务 ID 不再复用。

## SQL 流程
- 先 validate_sql，通过后用完全相同的文本调用 execute_sql；校验与执行
  分两步串行调用；SQL 改动后重新校验；两步使用同一 sub_task_id/
  sub_task_revision。
- 依赖前一步多行结果时，用结果占位符 {{result:<result_ref>:<列名>}}，
  不手抄值。

## 结果
- 注意 preview_truncated（预览不完整）与 source_truncated（源结果不完整）
  的区别；源结果截断时总体统计用 analyze_result 下推，不用已保存样本
  冒充总体；下推结果带"重新执行"警告，在限制中说明。
- 预览不足时用 read_result 分页读取；占比／环比显式传 round_digits。
- 用户要求严格原时点结论时，analyze_result 用 allow_pushdown=false；
  源结果截断会返回 incomplete_source，如实告知无法恢复完整原快照。
- 工具返回 /artifacts/large_tool_results/ 路径时，先看附带预览；信息
  不足再用 grep/read_file 定位并分窗口读取（大文件不要一次整读）。
  文件路径本身不是 result_ref/evidence_id，引用仍用返回中的真实标识。
- 分析结果被转存时，先从预览或文件取得 result_ref/evidence_id 再继续
  分页、分析或提交；跨运行沿用仍遵循 reuse_evidence 规则。结果分页
  和计算继续用业务工具，不能把已保存部分冒充总体。

## 表述
- 空结果、查询失败、数据缺失、业务异常分别表述；空结果不等于数值为 0。

## 结束
- 证据足够、全部任务终态后单独调用 submit_final_answer 提交最终答案
  （同一条消息中不要调用其他工具）。
- 每条发现带 finding_id（F-1、F-2…，提交内唯一）与 task_refs，并引用
  已选入对应任务成果的 evidence_id；写明口径、限制、未完成事项；只
  引用 execute_sql／analyze_result／reuse_evidence 返回过的
  evidence_id（探针的执行记录不能引用）。
- unresolved_task_refs 恰列本轮全部 failed／needs_clarification 任务；
  open_questions 只放非阻塞补充问题，不放阻塞事项。
- status 必须与业务判定一致（全部成功且全部主成果为未截断零行时才
  报 no_data）；被拒时按返回的 expected_status／错误修正提交，不需要
  重新执行 SQL；不要用普通文本结束回答。
```

系统提示词第二个内容块为框架注入的 Skills System 段：披露三个技能
（query-planning / result-analysis / schema-retrieval，各自一行描述 + SKILL.md 路径），
并说明渐进披露用法（需要时用 read_file 读全文）。**本次运行模型未读任何 skill 文件**——
系统提示内的技能摘要已足够支撑这个简单问题。

### 3.2 工具定义（16 个，按日志 [Tools] 段顺序）

ls、read_file、glob、grep（框架文件工具，只读）｜search_schema、inspect_tables、
plan_joins、resolve_time_range、probe_dimension_values、reuse_evidence、validate_sql、
execute_sql、read_result、analyze_result、record_sub_tasks、submit_final_answer（12 业务工具）。
完整 JSON Schema 见 `logs/model_input_sqlgen_0915.txt` 的 [Tools] 段（与代码
`agent.py` 装配一致）。

---

## 四、逐步详细执行过程

以下"思考"为该次模型调用输出中的 AI 消息正文原文；"返回"为下一轮输入中的 Tool 消息原文。

### 步骤 1｜模型调用①（12:25:44 → 12:25:46，attempt 01a1dd74-3aaf）

输入消息：[System, Human]。

**思考（原文）**：

> 我需要先了解可用的表结构，然后查询2026年9月份的订单数量。让我先检索相关的表和列信息。

**工具调用**：

```json
search_schema
{"query": "订单 order 时间 日期", "kinds": ["tables", "columns"]}
```

服务端日志：`search_schema: kinds=['tables','columns'] effective_min_scores={'tables':0.45,'columns':0.45} dim_evidence=0 unmatched=0`；`seq=1 ... elapsed_ms=1848`。

**返回（完整原文）**：

```json
{
  "status": "ok",
  "data": {
    "effective_min_scores": {"tables": 0.45, "columns": 0.45},
    "tables": [
      {
        "object_id": "orders.public.orders",
        "object_type": "table",
        "similarity": 0.6216325759887695,
        "grain_hint": null,
        "time_col_hint": "order_date",
        "table_category": "fact",
        "category_group": "fact",
        "display": "public.orders"
      }
    ],
    "columns": [
      {
        "object_id": "orders.public.orders.order_date",
        "parent_id": "orders.public.orders",
        "object_type": "column",
        "similarity": 0.7564107775688171,
        "grain_hint": null,
        "parent_display": "public.orders"
      },
      {
        "object_id": "orders.public.orders.user_id",
        "parent_id": "orders.public.orders",
        "object_type": "column",
        "similarity": 0.5474910140037537,
        "grain_hint": null,
        "parent_display": "public.orders"
      },
      {
        "object_id": "orders.public.orders.order_id",
        "parent_id": "orders.public.orders",
        "object_type": "column",
        "similarity": 0.5461751818656921,
        "grain_hint": null,
        "parent_display": "public.orders"
      },
      {
        "object_id": "orders.public.orders.amount",
        "parent_id": "orders.public.orders",
        "object_type": "column",
        "similarity": 0.5255391001701355,
        "grain_hint": null,
        "parent_display": "public.orders"
      }
    ]
  },
  "warnings": []
}
```

### 步骤 2｜模型调用②（12:25:46 → 12:25:47，attempt 01a1dd74-428b）

输入消息：[System, Human, AI①, Tool(search_schema)]。

**思考（原文）**：

> 我找到了订单表 `public.orders`，其中有 `order_date` 列用于时间过滤，`order_id` 列用于统计订单数量。让我先确认表结构，然后解析时间范围。

**工具调用**：

```json
inspect_tables
{"table_ids": ["orders.public.orders"]}
```

**返回（完整原文，双源合并：卡片 + 实时列，零冲突）**：

```json
{
  "status": "ok",
  "data": {
    "tables": [
      {
        "table_id": "orders.public.orders",
        "display": "public.orders",
        "table_category": "fact",
        "category_group": "fact",
        "time_col_hint": "order_date",
        "card_truncated": false,
        "card_text": "# public.orders [table]（订单交易明细表，记录用户购买产品的详细信息及金额）\n## 字段列表：\n- order_id (integer) - 订单唯一标识ID [示例: 1, 2]\n- order_date (date) - 订单创建日期 [示例: 2025-11-08, 2026-09-03]\n- user_id (integer) - 下单用户ID [示例: 82, 29]\n- product_id (integer) - 购买产品ID [示例: 8, 9]\n- quantity (integer) - 购买数量 [示例: 1, 1]\n- amount (numeric(10,2)) - 订单总金额 [示例: 18.0, 19.0]\n## 字段补充说明：\n- amount 使用numeric(10,2)存储，精确到小数点后2位",
        "live_columns": [
          {"name": "order_id", "pg_type": "integer", "nullable": false},
          {"name": "order_date", "pg_type": "date", "nullable": true},
          {"name": "user_id", "pg_type": "integer", "nullable": true},
          {"name": "product_id", "pg_type": "integer", "nullable": true},
          {"name": "quantity", "pg_type": "integer", "nullable": true},
          {"name": "amount", "pg_type": "numeric", "nullable": true}
        ],
        "card_only_columns": [],
        "live_only_columns": [],
        "warnings": [],
        "status": "ok"
      }
    ]
  },
  "warnings": []
}
```

### 步骤 3｜模型调用③（12:25:47 → 12:25:49，attempt 01a1dd74-488b）

**思考（原文）**：

> 表结构已确认。`public.orders` 表有 `order_date` (date类型) 和 `order_id` 列。现在我需要解析"2026年9月份"这个时间范围。

**工具调用**（source_text 逐字取自用户问题，由服务端核对一致性）：

```json
resolve_time_range
{"source_text": "2026年9月份", "kind": "calendar", "year": 2026, "month": 9}
```

服务端日志：`seq=3 ... elapsed_ms=1863`。

**返回（完整原文——确定性规则解析的半开区间 + 现成 SQL 片段）**：

```json
{
  "status": "ok",
  "data": {
    "time_evidence_id": "T-be52c4d4",
    "grain": "month",
    "start": "2026-09-01",
    "end": "2026-10-01",
    "timezone": "Asia/Shanghai",
    "sql_hint": "<时间列> >= '2026-09-01' AND <时间列> < '2026-10-01'"
  },
  "warnings": []
}
```

### 步骤 4｜模型调用④（12:25:49 → 12:25:51，attempt 01a1dd74-4fda）

**思考（原文）**：

> 时间范围已解析：2026-09-01 到 2026-10-01（半开区间）。现在我需要登记业务任务，然后编写并执行SQL查询订单数量。

**工具调用**（单独调用，一次通过——未触发"必须单独调用"拒绝）：

```json
record_sub_tasks
{"expected_plan_version": 0, "operations": [{"op": "add", "sub_task_id": "T1", "content": "查询2026年9月份的订单数量"}]}
```

**返回（完整原文）**：

```json
{
  "status": "ok",
  "data": {
    "plan_version": 1,
    "no_op": false,
    "operations": [
      {"op": "add", "sub_task_id": "T1", "no_op": false, "current_status": "pending"}
    ]
  },
  "warnings": []
}
```

### 步骤 5｜模型调用⑤（12:25:51 → 12:25:53，attempt 01a1dd74-576a）——SQL 生成

输入消息：[System, Human, ①-⑩ 全部历史]（约 1.9K tokens，完整文本见
`logs/model_input_sqlgen_0915.txt`）。

**思考（原文）**：

> 任务已登记，plan_version=1，T1 revision=1。现在我需要编写SQL查询2026年9月份的订单数量，然后先校验再执行。

**工具调用（生成的 SQL——时间边界逐字采用 T-be52c4d4 的 sql_hint 模式）**：

```json
validate_sql
{"sql": "SELECT COUNT(*) AS order_count FROM public.orders WHERE order_date >= '2026-09-01' AND order_date < '2026-10-01'", "sub_task_id": "T1", "sub_task_revision": 1}
```

服务端：语法/只读安全/EXPLAIN 三层 + guard（两个时间字面值 '2026-09-01'/'2026-10-01' 溯源到
T-be52c4d4）全部通过；日志 `validate_sql: valid=True codes=[]`；`seq=5 ... elapsed_ms=2204`；
授权记录写入状态。

**返回（完整原文）**：

```json
{
  "status": "ok",
  "data": {
    "layer": "all_passed",
    "warnings": ["检测到顺序扫描（Seq Scan），可能影响性能"],
    "explain_plan": "Aggregate  (cost=29.07..29.08 rows=1 width=8)\n  ->  Seq Scan on orders  (cost=0.00..29.05 rows=6 width=0)\n        Filter: ((order_date >= '2026-09-01'::date) AND (order_date < '2026-10-01'::date))"
  },
  "warnings": []
}
```

### 步骤 6｜模型调用⑥（12:25:53 → 12:25:55，attempt 01a1dd74-6022）——执行

**思考（原文）**：

> SQL校验通过。现在执行相同的SQL获取结果。

**工具调用（与 validate 完全相同的文本——授权握手）**：

```json
execute_sql
{"sql": "SELECT COUNT(*) AS order_count FROM public.orders WHERE order_date >= '2026-09-01' AND order_date < '2026-10-01'", "sub_task_id": "T1", "sub_task_revision": 1}
```

**返回（完整原文——结果 33，7ms，未截断）**：

```json
{
  "status": "ok",
  "data": {
    "evidence_id": "E-8f3d0259",
    "result_ref": "res_82d6024a7eeb4e0f9bd1ffa19d36307a",
    "columns": [{"name": "order_count", "pg_type": "int8"}],
    "saved_row_count": 1,
    "source_truncated": false,
    "preview_rows": [[33]],
    "preview_truncated": false,
    "elapsed_ms": 7,
    "source_evidence_ref": {
      "session_id": "s_f331217c70a942d48f787b5634bcf0a8",
      "thread_id": "dbda:s_f331217c70a942d48f787b5634bcf0a8:1",
      "run_id": "run_17d1dd668c584f14be4f634ca055112b",
      "evidence_id": "E-8f3d0259"
    }
  },
  "warnings": []
}
```

### 步骤 7｜模型调用⑦（12:25:55 → 12:25:57，attempt 01a1dd74-6766）——任务终态

**思考（原文）**：

> 查询成功，2026年9月份的订单数量为33个。证据ID为E-8f3d0259。现在我需要将任务标记为completed，然后提交最终答案。

**工具调用**：

```json
record_sub_tasks
{"expected_plan_version": 1, "operations": [{"op": "status", "sub_task_id": "T1", "status": "completed", "evidence_ids": ["E-8f3d0259"]}]}
```

**返回（完整原文）**：

```json
{
  "status": "ok",
  "data": {
    "plan_version": 2,
    "no_op": false,
    "operations": [
      {"op": "status", "sub_task_id": "T1", "no_op": false, "status": "completed"}
    ]
  },
  "warnings": []
}
```

### 步骤 8｜模型调用⑧（12:25:57 → 12:26:00，attempt 01a1dd74-6f50）——最终答案

输入消息：全部 16 条历史（System、Human、8 组 AI/Tool 往返）+ 16 工具定义。

**工具调用（一次通过门控；答案含口径说明）**：

```json
submit_final_answer
{"status": "completed",
 "answer": "2026年9月份的订单数量为33个。\n\n查询口径：统计 public.orders 表中 order_date 在 2026-09-01（含）至 2026-10-01（不含）范围内的订单记录数。",
 "findings": [
   {"text": "2026年9月份共有33个订单",
    "finding_id": "F-1",
    "task_refs": [{"sub_task_id": "T1", "revision": 1}],
    "evidence_ids": ["E-8f3d0259"]}]}
```

服务端收尾日志：
`最终答案已接受: status=completed findings=1` → `最终答案已登记，运行结束`（FinalAnswerGate）→
`seq=8 ... elapsed_ms=2653`。Runner 随后按常规流程封存运行快照并归档。

---

## 五、行为要点

1. **懒加载**：无过滤值 → 零维度检索；单表 → 零 JOIN 查询；简单问题 → 未读 skill 文件。
   对比前一日两次查询（51KB/40KB 输入、含 skill 阅读、含值反查），本次是最精简路径。
2. **确定性时间链**：模型只做"识别表达 + 选 kind/参数"，区间计算和 sql_hint 由规则器完成，
   模型逐字采用——时间边界零误差，guard 溯源一次通过。
3. **三个硬关卡全部一次过**：record（单独调用）、validate（授权）、submit（门控），
   无重试无拒绝——提示词遵守度最好的一次运行。
4. **证据链完整**：T-be52c4d4（时间）→ E-8f3d0259（执行）→ F-1/T1（findings 引用），
   答案口径说明按 result-analysis skill 要求书写。

## 附：日志定位

- 完整日志：`logs/nl2sql.log`（本次 run 约第 24-1414 行）；
- 生成 SQL 那次调用的完整输入（System+消息+工具定义，40KB）：`logs/model_input_sqlgen_0915.txt`；
- 最后一轮完整消息历史（16 条，等价于 checkpoint 状态）：`logs/nl2sql.log` 第 1130-1397 行
  的 MODEL_INPUT 块（attempt 01a1dd74-6f50）。
