# db_deepagent 执行过程追踪：订单数量最多的产品名称查询

> 生成日期：2026-11-26。来源：线上容器 `logs/nl2sql.log`（doc 22 工具调用日志，日志级别 DEBUG）+ 运行封存记录。
>
> 问题：**"请问订单数量最多的那个产品名称是什么"**
>
> request_id：`req_217f49eae43d4357ae806ef5d7b53d89`；run_id：`run_2a781656c92a4918ba7f941d2494a2b3`；
> 会话：`s_843345509769473288af5c0cbca302dc`；线程：`dbda:s_843345509769473288af5c0cbca302dc:1`；模型：qwen3.8-max。
>
> 结果：**商品05，19 笔订单，status=completed，一次提交通过**。运行耗时 40,509 ms（约 40.5 秒），
> **8 次模型调用、9 次工具调用，零拒绝、零失败**。
>
> 9 次工具调用里只有第 1 轮的 `read_file` 和 `search_schema` 是并行的，其余 7 次依次串行。
> 工具耗时合计 797 ms，模型调用耗时合计 39,602 ms。
>
> 各工具的入参和返回都是日志 `TOOL_INPUT` / `TOOL_OUTPUT` 块的原文，无删减。

---

## 一、工具调用时间顺序总表

按开始时刻排序。耗时取日志里的 `elapsed_ms`，口径见文末"附"。

| 序号 | 开始时刻 | 工具 | 入参要点 | 耗时 | 模型轮次 | 调用方式 | 返回要点 |
|---|---|---|---|---|---|---|---|
| 1 | 21:35:45.786 | `read_file` | 读取 `/skills/query-planning/SKILL.md`，`limit=1000` | 4 ms | 第 1 轮 | **并行**，与 #2 同时执行 | `query-planning` 技能文件全文，54 行 |
| 2 | 21:35:45.788 | `search_schema` | `query="订单 产品 名称 数量"`，`kinds=[tables, columns]` | 674 ms | 第 1 轮 | **并行**，与 #1 同时执行 | 命中 6 张表、10 个列，`orders`、`products` 居前 |
| 3 | 21:35:50.644 | `inspect_tables` | `orders.public.orders`、`orders.public.products` | 34 ms | 第 2 轮 | 串行 | 两张表的表卡片和实时列，无冲突 |
| 4 | 21:35:55.684 | `plan_joins` | `orders` → `products` | 28 ms | 第 3 轮 | 串行 | 找到 1 条路径：`INNER JOIN`，`product_id` 对 `product_id` |
| 5 | 21:35:59.405 | `record_sub_tasks` | 新增 T1：查询订单数量（订单行数）最多的产品名称 | 1 ms | 第 4 轮 | 串行 | `plan_version=1`，T1 为 `pending` |
| 6 | 21:36:04.014 | `validate_sql` | 两表 JOIN，按产品名分组计数，降序取 1 行（SQL 全文见 5.6） | 35 ms | 第 5 轮 | 串行 | `all_passed`；1 条警告：检测到顺序扫描 |
| 7 | 21:36:10.512 | `execute_sql` | 与 #6 逐字相同的 SQL | 12 ms | 第 6 轮 | 串行 | 1 行：`商品05`，19；证据 `E-c7ab531b` |
| 8 | 21:36:14.278 | `record_sub_tasks` | T1 置为 `completed`，挂证据 `E-c7ab531b` | 6 ms | 第 7 轮 | 串行 | `plan_version=2` |
| 9 | 21:36:20.251 | `submit_final_answer` | `status=completed`，最终答案和 1 条发现 F-1 | 3 ms | 第 8 轮 | 串行 | 已接收最终答案 |

调用顺序（`∥` 表示两侧并行）：

```text
read_file ∥ search_schema → inspect_tables → plan_joins → record_sub_tasks → validate_sql → execute_sql → record_sub_tasks → submit_final_answer
```

## 二、并行调用与串行调用

### 2.1 并行：第 1 轮的 `read_file` 和 `search_schema`

这两个调用出自同一次模型输出（第 1 轮，`attempt_id=01a21811-a738-7650-90da-11c0cc5786bf`），模型在一条消息里同时发起了它们。

| 工具 | 开始时刻 | 结束时刻 | 耗时 |
|---|---|---|---|
| `read_file`（#1） | 21:35:45.786 | 21:35:45.790 | 4 ms |
| `search_schema`（#2） | 21:35:45.788 | 21:35:46.462 | 674 ms |

`search_schema` 开始时 `read_file` 还没有返回，两个执行区间重叠 2 ms，所以判定为并行调用。框架侧的行为与此一致：LangGraph 的 `ToolNode` 对同一批工具调用用 `asyncio.gather` 并发执行（`langgraph/prebuilt/tool_node.py`，langgraph-prebuilt 1.1.1）。

`read_file` 4 ms 就返回了，`search_schema` 跑了 674 ms。两者都返回之后，第 2 轮模型调用才开始（21:35:46.470）。

### 2.2 串行：其余 7 次

第 2 到第 8 轮，模型每轮只发起 1 个工具调用。后一个工具开始时，前一个工具早已返回，中间隔着一次模型调用。相邻两次调用之间的间隔如下，全部为正，没有任何重叠：

| 前一个工具 | 结束时刻 | 后一个工具 | 开始时刻 | 间隔 |
|---|---|---|---|---|
| #2 `search_schema` | 21:35:46.462 | #3 `inspect_tables` | 21:35:50.644 | 4,182 ms |
| #3 `inspect_tables` | 21:35:50.678 | #4 `plan_joins` | 21:35:55.684 | 5,006 ms |
| #4 `plan_joins` | 21:35:55.712 | #5 `record_sub_tasks` | 21:35:59.405 | 3,693 ms |
| #5 `record_sub_tasks` | 21:35:59.407 | #6 `validate_sql` | 21:36:04.014 | 4,607 ms |
| #6 `validate_sql` | 21:36:04.050 | #7 `execute_sql` | 21:36:10.512 | 6,462 ms |
| #7 `execute_sql` | 21:36:10.524 | #8 `record_sub_tasks` | 21:36:14.278 | 3,754 ms |
| #8 `record_sub_tasks` | 21:36:14.284 | #9 `submit_final_answer` | 21:36:20.251 | 5,967 ms |

有三处串行顺序是系统提示词明确要求的，本次运行都遵守了：

- `validate_sql` 通过后，用完全相同的文本调用 `execute_sql`，"校验与执行分两步串行调用"（#6 → #7，两次传入的 SQL 逐字相同）；
- `record_sub_tasks` "必须单独调用"（#5、#8 各占一轮）；
- `submit_final_answer` "单独调用……同一条消息中不要调用其他工具"（#9 独占第 8 轮）。

## 三、模型轮次与工具调用的对应关系

| 模型轮次 | 模型调用起止 | 模型耗时 | 本轮发起的工具调用 | 工具耗时合计 |
|---|---|---|---|---|
| 第 1 轮 | 21:35:39.833 → 21:35:45.782 | 5,952 ms | `read_file`、`search_schema`（并行） | 678 ms |
| 第 2 轮 | 21:35:46.470 → 21:35:50.643 | 4,176 ms | `inspect_tables` | 34 ms |
| 第 3 轮 | 21:35:50.686 → 21:35:55.681 | 4,997 ms | `plan_joins` | 28 ms |
| 第 4 轮 | 21:35:55.719 → 21:35:59.403 | 3,719 ms | `record_sub_tasks` | 1 ms |
| 第 5 轮 | 21:35:59.414 → 21:36:04.012 | 4,599 ms | `validate_sql` | 35 ms |
| 第 6 轮 | 21:36:04.057 → 21:36:10.510 | 6,454 ms | `execute_sql` | 12 ms |
| 第 7 轮 | 21:36:10.531 → 21:36:14.275 | 3,746 ms | `record_sub_tasks` | 6 ms |
| 第 8 轮 | 21:36:14.292 → 21:36:20.249 | 5,959 ms | `submit_final_answer` | 3 ms |
| 合计 | 8 次 | 39,602 ms | 9 次 | 797 ms |

时间构成：运行耗时 40,509 ms 里，模型调用合计 39,602 ms（约 97.8%），工具调用合计 797 ms（约 2.0%），其余 110 ms 是运行内的其他开销。

## 四、调用的工具

| 工具 | 调用次数 | 本次用途 |
|---|---|---|
| `read_file` | 1 | 读取 `query-planning` 技能文件（框架内置的文件工具） |
| `search_schema` | 1 | 用"订单 产品 名称 数量"检索相关的表和列 |
| `inspect_tables` | 1 | 核对 `orders`、`products` 两张表的结构（表卡片 + 实时列） |
| `plan_joins` | 1 | 查 `orders` 到 `products` 的 JOIN 路径 |
| `record_sub_tasks` | 2 | 登记 T1；之后把 T1 置为 completed 并挂上证据 |
| `validate_sql` | 1 | 校验 SQL，返回 EXPLAIN 执行计划 |
| `execute_sql` | 1 | 执行同一条 SQL，得到结果和证据 |
| `submit_final_answer` | 1 | 提交最终答案 |

模型本次可用 16 个工具，另外 8 个没有用到：`ls`、`glob`、`grep`（只读了路径明确的一个技能文件）、`resolve_time_range`（问题没有时间条件）、`probe_dimension_values`（问题没有要过滤的具体值）、`reuse_evidence`（本次是会话首轮，没有历史证据）、`read_result`、`analyze_result`（结果只有 1 行且预览完整）。

## 五、各工具调用的入参与返回

按时间顺序，每次调用一节。入参是 `TOOL_INPUT` 块原文，返回是 `TOOL_OUTPUT` 块原文；行号指 `nl2sql.log` 中的行。

### 5.1 #1 `read_file`（第 1 轮，与 #2 并行）

- 时刻：21:35:45.786 → 21:35:45.790，耗时 **4 ms**
- `tool_call_id=call_c448bbb589224e0db1d8a03b`
- 日志行：started 第 126 行，入参第 127 行，finished 第 143 行，返回第 144 行
- 首行 `@@ lines 1-54 of 54 @@` 是框架文件工具加的行号范围标记，说明 54 行全部返回。

**入参**（TOOL_INPUT 原文）：

```json
{
  "file_path": "/skills/query-planning/SKILL.md",
  "limit": 1000
}
```

**返回**（TOOL_OUTPUT 原文，55 行）：

```text
@@ lines 1-54 of 54 @@
---
name: query-planning
description: 识别指标、时间、维度、比较基准与粒度，并规划查询步骤；适用于首次查询、多轮追问、指标比较、多个子问题，以及时间或业务口径存在歧义的问题。
---

# 查询规划

## 适用场景

首次查询、多轮追问、指标比较、多个子问题，以及时间或业务口径存在歧义的问题。

## 判断方法

围绕下列要素形成简洁的工作口径，随检索反馈更新；无需输出固定格式的解析 JSON：

| 要素 | 要确认的内容 | 未确认时的处理 |
| --- | --- | --- |
| 指标与聚合 | 要什么指标、金额字段与单位、计数对象、支付／退款等口径 | 查表卡片和字段，必要时参考历史 SQL；存在影响结果的多种合理口径且无明确约定时向用户澄清 |
| 时间 | 用户原文、周期、统计时间字段；新计算还是沿用前文 | 明确表达交给 resolve_time_range；沿用已确认区间走 reuse_evidence；裸“当前”不推定为今天 |
| 维度名称 | 分组或筛选涉及什么属性（如“服务区”“客户”“渠道”） | 用于定向列检索，不当成过滤值 |
| 维度值候选 | 用户具体指定的对象（如“杭州湾”） | 原文作为 value_texts；规范值与归属列由检索或探针确认 |
| 粒度与比较 | 输出粒度、比较基准、分母、排名与并列要求 | 结合表卡片和结构核对；不能把订单行数直接当订单数，也不能把日汇总重复相加 |
| 多轮关系 | 本轮新增、替换或明确沿用哪些条件 | 以用户最新明确要求为准；旧证据沿用仍须按 reuse_evidence 登记，摘要描述本身不构成执行证据 |

## 查询安排

- 需要取数、结果分析或沿用数据成果时，先用 `record_sub_tasks` 登记本轮业务任务：任务 = 需要交付的业务问题或必要中间结果（如"取得 A""取得 B""比较 A/B"），不是工具调用步骤。检索结构、修正 SQL、读取分页属于同一任务内的工作，不单独登记。纯解释类问题（答案不依赖库内数据）与向用户澄清的问题无需登记任务，收集必要信息后直接单独调用 `submit_final_answer` 回答（findings 留空）；混合问题只为需要数据成果的部分登记任务，解释部分直接写入 answer。
- 单条 SQL 能清楚表达且没有真实结果依赖时，优先一次完成；不预先划分“简单／复杂”两条流程。
- 必须根据前一步数据决定下一步对象、范围或查询策略时，再拆为多步。前序编号列表用结果占位符 `{{result:<result_ref>:<列名>}}` 传递，避免手抄。下游任务在 dependencies 中声明依赖（固定版本）；依赖任务 completed 后才能校验／执行／分析。
- 同一请求中的独立子问题分别跟踪完成情况，说明各自的时间与口径；不能用一项成功掩盖其他项未完成，也不能把口径不同的结果直接合并。每项任务用 `record_sub_tasks(status=...)` 登记终态与选定成果 evidence_ids。
- 检索和校验反馈显示原计划不成立时，先判断缺少结构、维度值、关联路径还是业务定义，再调用对应工具补足；不盲目重复同一调用。计划变更用 revise（内容/依赖变化）或 void（计划替换/重复合并），作废任务附 replacement 指向替代任务。

## 失败与歧义处理

- 能通过结构或受控检索解决的疑点先查证；会改变用户明确范围、指标含义或对象选择且无依据裁决时，向用户澄清。
- 不自动删除未匹配的条件，不把“查不到指定对象”改成“查询全部对象”。
- “某日前／截至某日／从某日起”的单边需求逐字传入原文（如 `resolve_time_range(source_text="截至今天中午12点")`），不补另一端；年份、方向或是否包含当天不明确时澄清，不把“某日之后”擅自改成“从某日至今天”。
- 任务无法推进时登记真实终态：工具失败用 failed+failure_refs（引用工具错误包返回的 failure_ref）；未取得前置信息等主动放弃用 agent_abandoned，不谎称 SQL 执行失败；上游待澄清/失败导致下游无法执行用 dependency_blocked+blocked_by，一次批量登记整条阻塞链，不替下游编造独立澄清问题。

## 完成条件

准备编写 SQL 时，所需指标、时间、筛选、输出粒度和关联方式已有可说明的依据；无需使用的要素不强行补齐。仍有关键歧义时，把该任务登记为 needs_clarification 并写明 clarification_question，最终提交 status=needs_clarification，不声称已完成查询。全部任务进入终态（completed/failed/needs_clarification/voided）后单独提交最终答案；findings 逐项覆盖全部 completed 任务，unresolved_task_refs 恰列全部 failed/needs_clarification 任务。

## 示例

问题“今年杭州湾服务区的销售额是多少”：

1. 指标=销售额（口径待确认），时间原文=“今年”，维度名称=“服务区”，维度值候选=“杭州湾”。
2. `search_schema(query="销售额 服务区", kinds=["tables", "columns", "dim_values"], value_texts=["杭州湾"])`；列命中不足时补查 `query="服务区", kinds=["columns"]`；对候选表调用 `inspect_tables`。
3. `resolve_time_range(source_text="今年")` 得区间与证据 ID——传原文即可，语义由工具内部的日期模型结合服务端当前时间解析。
4. “杭州湾”未命中时在已确认列上 `probe_dimension_values(mode="exact")`；多个可能对象时澄清。
5. 需要关联时 `plan_joins` 并补查桥接表；单表已有全部字段则不用查图。
6. 写 SQL（时间用 `>= 'start' AND < 'end'`），先 validate_sql 再用同一文本 execute_sql。
7. 口径与证据充分时单独 submit_final_answer；歧义未决时提交 needs_clarification。
```

### 5.2 #2 `search_schema`（第 1 轮，与 #1 并行）

- 时刻：21:35:45.788 → 21:35:46.462，耗时 **674 ms**
- `tool_call_id=call_569a6be13375479285327bc9`
- 日志行：started 第 133 行，入参第 134 行，finished 第 204 行，返回第 205 行

**入参**（TOOL_INPUT 原文）：

```json
{
  "query": "订单 产品 名称 数量",
  "kinds": [
    "tables",
    "columns"
  ]
}
```

**返回**（TOOL_OUTPUT 原文，154 行）：

```json
{
  "status": "ok",
  "data": {
    "effective_min_scores": {
      "tables": 0.45,
      "columns": 0.45
    },
    "tables": [
      {
        "object_id": "orders.public.orders",
        "object_type": "table",
        "similarity": 0.7142060995101929,
        "grain_hint": null,
        "time_col_hint": "order_date",
        "table_category": "fact",
        "category_group": "fact",
        "display": "public.orders"
      },
      {
        "object_id": "orders.public.products",
        "object_type": "table",
        "similarity": 0.6512952446937561,
        "grain_hint": null,
        "time_col_hint": "",
        "table_category": "dim",
        "category_group": "dimension",
        "display": "public.products"
      },
      {
        "object_id": "orders.public.categories",
        "object_type": "table",
        "similarity": 0.5451496839523315,
        "grain_hint": null,
        "time_col_hint": "",
        "table_category": "dim",
        "category_group": "dimension",
        "display": "public.categories"
      },
      {
        "object_id": "orders.public.mv_category_sales",
        "object_type": "table",
        "similarity": 0.5046213269233704,
        "grain_hint": null,
        "time_col_hint": "",
        "table_category": "fact",
        "category_group": "fact",
        "display": "public.mv_category_sales"
      },
      {
        "object_id": "orders.public.test1",
        "object_type": "table",
        "similarity": 0.4555586576461792,
        "grain_hint": null,
        "time_col_hint": "",
        "table_category": "unknown",
        "category_group": "dimension",
        "display": "public.test1"
      },
      {
        "object_id": "orders.public.cinema_halls",
        "object_type": "table",
        "similarity": 0.45271050930023193,
        "grain_hint": null,
        "time_col_hint": "",
        "table_category": "dim",
        "category_group": "dimension",
        "display": "public.cinema_halls"
      }
    ],
    "columns": [
      {
        "object_id": "orders.public.orders.quantity",
        "parent_id": "orders.public.orders",
        "object_type": "column",
        "similarity": 0.7958557605743408,
        "grain_hint": null,
        "parent_display": "public.orders"
      },
      {
        "object_id": "orders.public.products.product_name",
        "parent_id": "orders.public.products",
        "object_type": "column",
        "similarity": 0.7790874242782593,
        "grain_hint": null,
        "parent_display": "public.products"
      },
      {
        "object_id": "orders.public.orders.product_id",
        "parent_id": "orders.public.orders",
        "object_type": "column",
        "similarity": 0.7316120862960815,
        "grain_hint": null,
        "parent_display": "public.orders"
      },
      {
        "object_id": "orders.public.orders.order_id",
        "parent_id": "orders.public.orders",
        "object_type": "column",
        "similarity": 0.7159935832023621,
        "grain_hint": null,
        "parent_display": "public.orders"
      },
      {
        "object_id": "orders.public.orders.amount",
        "parent_id": "orders.public.orders",
        "object_type": "column",
        "similarity": 0.6985393166542053,
        "grain_hint": null,
        "parent_display": "public.orders"
      },
      {
        "object_id": "orders.public.orders.order_date",
        "parent_id": "orders.public.orders",
        "object_type": "column",
        "similarity": 0.6854430437088013,
        "grain_hint": null,
        "parent_display": "public.orders"
      },
      {
        "object_id": "orders.public.mv_category_sales.total_quantity",
        "parent_id": "orders.public.mv_category_sales",
        "object_type": "column",
        "similarity": 0.6827265620231628,
        "grain_hint": null,
        "parent_display": "public.mv_category_sales"
      },
      {
        "object_id": "orders.public.categories.category_name",
        "parent_id": "orders.public.categories",
        "object_type": "column",
        "similarity": 0.6762696504592896,
        "grain_hint": null,
        "parent_display": "public.categories"
      },
      {
        "object_id": "orders.public.orders.user_id",
        "parent_id": "orders.public.orders",
        "object_type": "column",
        "similarity": 0.6390278339385986,
        "grain_hint": null,
        "parent_display": "public.orders"
      },
      {
        "object_id": "orders.public.products.product_id",
        "parent_id": "orders.public.products",
        "object_type": "column",
        "similarity": 0.638561487197876,
        "grain_hint": null,
        "parent_display": "public.products"
      }
    ]
  },
  "warnings": []
}
```

### 5.3 #3 `inspect_tables`（第 2 轮，串行）

- 时刻：21:35:50.644 → 21:35:50.678，耗时 **34 ms**
- `tool_call_id=call_c83e7a966e524ef0814ab7af`
- 日志行：started 第 617 行，入参第 618 行，finished 第 626 行，返回第 627 行

**入参**（TOOL_INPUT 原文）：

```json
{
  "table_ids": [
    "orders.public.orders",
    "orders.public.products"
  ]
}
```

**返回**（TOOL_OUTPUT 原文，83 行）：

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
        "card_text": "# public.orders [table]（订单交易明细事实表）\n## 字段列表：\n- order_id (integer) - 订单唯一标识ID [示例: 1, 2]\n- order_date (date) - 订单创建日期 [示例: 2025-11-08, 2026-09-03]\n- user_id (integer) - 下单用户ID [示例: 82, 29]\n- product_id (integer) - 购买产品ID [示例: 8, 9]\n- quantity (integer) - 商品购买数量 [示例: 1, 1]\n- amount (numeric(10,2)) - 订单总金额 [示例: 18.0, 19.0]\n## 字段补充说明：\n- amount 使用numeric(10,2)存储，精确到小数点后2位",
        "live_columns": [
          {
            "name": "order_id",
            "pg_type": "integer",
            "nullable": false
          },
          {
            "name": "order_date",
            "pg_type": "date",
            "nullable": true
          },
          {
            "name": "user_id",
            "pg_type": "integer",
            "nullable": true
          },
          {
            "name": "product_id",
            "pg_type": "integer",
            "nullable": true
          },
          {
            "name": "quantity",
            "pg_type": "integer",
            "nullable": true
          },
          {
            "name": "amount",
            "pg_type": "numeric",
            "nullable": true
          }
        ],
        "card_only_columns": [],
        "live_only_columns": [],
        "warnings": [],
        "status": "ok"
      },
      {
        "table_id": "orders.public.products",
        "display": "public.products",
        "table_category": "dim",
        "category_group": "dimension",
        "time_col_hint": "",
        "card_truncated": false,
        "card_text": "# public.products [table]（产品信息维度表）\n## 字段列表：\n- product_id (integer) - 产品唯一标识 [示例: 1, 2]\n- product_name (character varying(100)) - 产品名称 [示例: 商品01, 商品02]\n- category_id (integer) - 所属分类ID [示例: 1, 2]",
        "live_columns": [
          {
            "name": "product_id",
            "pg_type": "integer",
            "nullable": false
          },
          {
            "name": "product_name",
            "pg_type": "character varying",
            "nullable": false
          },
          {
            "name": "category_id",
            "pg_type": "integer",
            "nullable": true
          }
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

### 5.4 #4 `plan_joins`（第 3 轮，串行）

- 时刻：21:35:55.684 → 21:35:55.712，耗时 **28 ms**
- `tool_call_id=call_2a19de7430234858991c4187`
- 日志行：started 第 1071 行，入参第 1072 行，finished 第 1080 行，返回第 1081 行

**入参**（TOOL_INPUT 原文）：

```json
{
  "base_table": "orders.public.orders",
  "target_tables": [
    "orders.public.products"
  ]
}
```

**返回**（TOOL_OUTPUT 原文，29 行）：

```json
{
  "status": "ok",
  "data": {
    "base": "public.orders",
    "targets": [
      "public.products"
    ],
    "plans": [
      {
        "target": "public.products",
        "status": "found",
        "strategy": "apoc_dijkstra",
        "fallback_used": false,
        "edges": [
          {
            "src_table": "public.orders",
            "dst_table": "public.products",
            "join_type": "INNER JOIN",
            "cardinality": "N:1",
            "on": "SRC.product_id = DST.product_id"
          }
        ],
        "total_cost": 1.0
      }
    ],
    "bridge_tables": []
  },
  "warnings": []
}
```

### 5.5 #5 `record_sub_tasks`（第 4 轮，串行）

- 时刻：21:35:59.405 → 21:35:59.407，耗时 **1 ms**
- `tool_call_id=call_996941ac2f364c29b15ee1b1`
- 日志行：started 第 1511 行，入参第 1512 行，finished 第 1525 行，返回第 1526 行

**入参**（TOOL_INPUT 原文）：

```json
{
  "expected_plan_version": 0,
  "operations": [
    {
      "op": "add",
      "sub_task_id": "T1",
      "content": "查询订单数量（订单行数）最多的产品名称"
    }
  ]
}
```

**返回**（TOOL_OUTPUT 原文，16 行）：

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

### 5.6 #6 `validate_sql`（第 5 轮，串行）

- 时刻：21:36:04.014 → 21:36:04.050，耗时 **35 ms**
- `tool_call_id=call_dc75636d666b41b7aff912df`
- 日志行：started 第 1974 行，入参第 1975 行，finished 第 1983 行，返回第 1984 行

**入参**（TOOL_INPUT 原文）：

```json
{
  "sql": "SELECT p.product_name, COUNT(o.order_id) AS order_count\nFROM public.orders o\nINNER JOIN public.products p ON o.product_id = p.product_id\nGROUP BY p.product_name\nORDER BY order_count DESC\nLIMIT 1",
  "sub_task_id": "T1",
  "sub_task_revision": 1
}
```

**返回**（TOOL_OUTPUT 原文，11 行）：

```json
{
  "status": "ok",
  "data": {
    "layer": "all_passed",
    "warnings": [
      "检测到顺序扫描（Seq Scan），可能影响性能"
    ],
    "explain_plan": "Limit  (cost=127.36..127.36 rows=1 width=226)\n  ->  Sort  (cost=127.36..127.86 rows=200 width=226)\n        Sort Key: (count(o.order_id)) DESC\n        ->  HashAggregate  (cost=124.36..126.36 rows=200 width=226)\n              Group Key: p.product_name\n              ->  Hash Join  (cost=17.20..114.20 rows=2032 width=222)\n                    Hash Cond: (o.product_id = p.product_id)\n                    ->  Seq Scan on orders o  (cost=0.00..22.70 rows=1270 width=8)\n                    ->  Hash  (cost=13.20..13.20 rows=320 width=222)\n                          ->  Seq Scan on products p  (cost=0.00..13.20 rows=320 width=222)"
  },
  "warnings": []
}
```

### 5.7 #7 `execute_sql`（第 6 轮，串行）

- 时刻：21:36:10.512 → 21:36:10.524，耗时 **12 ms**
- `tool_call_id=call_451da90b9527430d80ad9a78`
- 日志行：started 第 2447 行，入参第 2448 行，finished 第 2457 行，返回第 2458 行
- 返回里的 `elapsed_ms: 9` 是 SQL 流水线自身记录的执行耗时；中间件口径的耗时是 12 ms。

**入参**（TOOL_INPUT 原文）：

```json
{
  "sql": "SELECT p.product_name, COUNT(o.order_id) AS order_count\nFROM public.orders o\nINNER JOIN public.products p ON o.product_id = p.product_id\nGROUP BY p.product_name\nORDER BY order_count DESC\nLIMIT 1",
  "sub_task_id": "T1",
  "sub_task_revision": 1
}
```

**返回**（TOOL_OUTPUT 原文，34 行）：

```json
{
  "status": "ok",
  "data": {
    "evidence_id": "E-c7ab531b",
    "result_ref": "res_d9a72f0897c342eea37a623b6c320d68",
    "columns": [
      {
        "name": "product_name",
        "pg_type": "varchar"
      },
      {
        "name": "order_count",
        "pg_type": "int8"
      }
    ],
    "saved_row_count": 1,
    "source_truncated": false,
    "preview_rows": [
      [
        "商品05",
        19
      ]
    ],
    "preview_truncated": false,
    "elapsed_ms": 9,
    "source_evidence_ref": {
      "session_id": "s_843345509769473288af5c0cbca302dc",
      "thread_id": "dbda:s_843345509769473288af5c0cbca302dc:1",
      "run_id": "run_2a781656c92a4918ba7f941d2494a2b3",
      "evidence_id": "E-c7ab531b"
    }
  },
  "warnings": []
}
```

### 5.8 #8 `record_sub_tasks`（第 7 轮，串行）

- 时刻：21:36:14.278 → 21:36:14.284，耗时 **6 ms**
- `tool_call_id=call_83c300031d974738a3340429`
- 日志行：started 第 2993 行，入参第 2994 行，finished 第 3010 行，返回第 3011 行

**入参**（TOOL_INPUT 原文）：

```json
{
  "expected_plan_version": 1,
  "operations": [
    {
      "op": "status",
      "sub_task_id": "T1",
      "status": "completed",
      "evidence_ids": [
        "E-c7ab531b"
      ]
    }
  ]
}
```

**返回**（TOOL_OUTPUT 原文，16 行）：

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

### 5.9 #9 `submit_final_answer`（第 8 轮，串行）

- 时刻：21:36:20.251 → 21:36:20.255，耗时 **3 ms**
- `tool_call_id=call_9ecb8394ed8146ea825256b8`
- 日志行：started 第 3553 行，入参第 3554 行，finished 第 3579 行，返回第 3580 行

**入参**（TOOL_INPUT 原文）：

```json
{
  "status": "completed",
  "answer": "订单数量最多的产品名称是**商品05**，共有 19 笔订单。",
  "findings": [
    {
      "text": "订单数量（订单行数）最多的产品是商品05，订单数量为 19 笔。",
      "finding_id": "F-1",
      "task_refs": [
        {
          "sub_task_id": "T1",
          "revision": 1
        }
      ],
      "evidence_ids": [
        "E-c7ab531b"
      ]
    }
  ],
  "metric_definitions": [
    "订单数量：按产品分组统计的订单行数（COUNT(order_id)），不包含时间过滤条件"
  ]
}
```

**返回**（TOOL_OUTPUT 原文，1 行）：

```text
已接收最终答案
```

## 六、模型各轮的说明（原文）

每轮模型输出里、工具调用之前的说明文字，取自 `MODEL_OUTPUT` 块，用来对照各轮为什么调用这些工具。

**第 1 轮**（发起：`read_file`、`search_schema`）

> 我需要先了解数据库结构，找到订单表和产品相关的表，然后查询订单数量最多的产品名称。
>
> 让我先读取技能文件，然后检索相关表结构。

**第 2 轮**（发起：`inspect_tables`）

> 我找到了相关的表结构：
> - `public.orders` - 订单表，包含 `order_id`, `product_id`, `quantity` 等字段
> - `public.products` - 产品表，包含 `product_id`, `product_name` 等字段
>
> 用户问的是"订单数量最多的产品名称"，这里需要明确口径：
> - "订单数量"可能指订单行数（COUNT(order_id)）或订单中的商品数量总和（SUM(quantity)）
>
> 从问题表述来看，"订单数量最多"更可能指的是订单行数最多（即被购买次数最多的产品）。但为了准确，我需要先查看表结构确认字段含义。
>
> 让我先检查这两个表的详细结构，然后规划查询。

**第 3 轮**（发起：`plan_joins`）

> 现在我了解了表结构：
> - `public.orders` 是订单事实表，包含 `order_id`, `product_id`, `quantity` 等
> - `public.products` 是产品维度表，包含 `product_id`, `product_name`
>
> 用户问"订单数量最多的产品名称"，这里的"订单数量"应该理解为订单行数（即该产品被下单的次数），而不是 quantity 的总和。因为通常说"订单数量"指的是订单的笔数。
>
> 我需要：
> 1. 登记一个业务任务
> 2. 编写 SQL 查询：按产品分组，统计订单行数，取最多的那个产品名称
> 3. 需要 JOIN orders 和 products 表
>
> 先规划 JOIN 路径，然后登记任务。

**第 4 轮**（发起：`record_sub_tasks`）

> JOIN 路径已确认：`public.orders` INNER JOIN `public.products` ON `orders.product_id = products.product_id`
>
> 现在登记业务任务，然后编写 SQL。

**第 5 轮**（发起：`validate_sql`）

> 任务已登记，plan_version=1，T1 状态为 pending。
>
> 现在编写 SQL 查询订单数量（订单行数）最多的产品名称。SQL 逻辑：
> - JOIN orders 和 products
> - 按 product_name 分组
> - 统计 COUNT(order_id) 作为订单数量
> - 按订单数量降序排列，取第一条

**第 6 轮**（发起：`execute_sql`）

> SQL 校验通过。现在执行 SQL。

**第 7 轮**（发起：`record_sub_tasks`）

> 查询结果已返回：
> - 产品名称：商品05
> - 订单数量：19
>
> evidence_id 为 E-c7ab531b，source_truncated=false，结果完整。
>
> 现在需要将任务标记为 completed，然后提交最终答案。

**第 8 轮**（发起：`submit_final_answer`）

> 任务 T1 已标记为 completed。现在提交最终答案。

## 附：数据来源与统计口径

- **日志文件**：线上容器 `insightweave-local-nl2sql-1`（镜像 `insightweave/nl2sql:fc62985773d9-d45799527355-6`）内的 `/app/nl2sql/logs/nl2sql.log`，日志级别 `NL2SQL_LOG_LEVEL=DEBUG`。本次请求从 21:35:37.340 的服务初始化到运行结束，占第 82–3587 行。行号以 2026-11-26 21:41 的文件为准；按 `logging.yaml` 的滚动配置，次日零点后该文件会滚动为 `nl2sql.log.2026-11-26`。
- **时刻**：均为 2026-11-26 北京时间，精确到毫秒。
- **工具耗时**：日志里的 `elapsed_ms`，是 doc 22 定义的中间件拦截耗时，从进入 `wrap_tool_call()` 计到工具 handler 返回，包含预算预占和入参日志的开销，不包含返回正文的排版时间。它与 started、finished 两个时刻之差一致，毫秒取整后可能相差 1 ms。
- **模型耗时**：审计日志 `seq=N … elapsed_ms` 的值，从发起本次模型调用计到响应返回。
- **运行耗时**：40,509 ms，取自运行封存记录的 `stats.elapsed_ms`，从 runner 生成 request_id 起算。请求进入后先有约 2.4 秒的服务初始化（业务库连接、Embedding、Milvus、Agent 装配），不在运行耗时内。
- **并行判定**：比较同一次运行里各工具调用的开始、结束区间，区间重叠才算并行（doc 22 §4.3）；仅凭模型一轮里出现多个工具名不能下结论。
- **返回原文**：`TOOL_OUTPUT` 记录的是工具自身的返回，早于外层的大结果转存（doc 22 §3.5）。本次 9 次返回都没有触发转存。
