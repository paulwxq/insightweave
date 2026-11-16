# db_deepagent 生成 SQL 前完整上下文日志修改方案

> 状态：方案待实施。本文只处理完整上下文日志；日期解析引入 LLM、是否独立 generate_sql 留待后续讨论。
> 依据：2026-11-13 当前代码，以及项目环境内安装的 DeepAgents 0.7.34、LangChain 1.4.9 源码。

## 1. 目标

用户执行查询后，可以在 `nl2sql/logs/nl2sql.log` 中直接找到：**模型生成某条 SQL 之前，实际接收到的完整上下文**。

日志应同时具备完整性和可读性：系统提示词、用户问题、历史消息、资料搜集工具的返回内容、工具定义集中展示，并能与随后生成的 SQL 对应。这里记录的是调用当时的输入，不根据最终状态、现有模板或数据库记录事后拼装一份提示词。

## 2. 当前代码事实

### 2.1 SQL 由主 Agent 模型生成

当前 `db_deepagent` 没有独立的 `generate_sql` 工具或 SQL 生成节点。主 Agent 读取资料后生成 SQL，通过 `validate_sql` 提交校验，随后决定是否调用 `execute_sql`。

因此，一次模型调用开始之前，程序不能预先确定模型会继续搜集资料、生成 SQL，还是整理答案。首版记录每次主 Agent 模型调用的完整输入，再通过该次调用返回的 `validate_sql` 及其参数定位 SQL 生成记录。

这不增加模型调用，也不要求模型遵循新增执行步骤。

### 2.2 现有审计没有提示词正文

[`RequestAuditMiddleware.wrap_model_call()`](../src/modules/db_deepagent/middleware/request_audit.py) 当前记录工具名称、消息数量、估算输入量、耗时和用量，没有记录系统提示词、消息正文或工具参数定义。

而且 DeepAgents 会将用户新增中间件放在部分框架中间件之前，后续 Skills 等中间件仍可能修改系统提示词和消息。直接在现有请求审计入口打印 `request`，不能保证拿到全部后续补充内容。

### 2.3 已有合适的模型回调

[`ModelCallObserver`](../src/modules/db_deepagent/middleware/model_observer.py) 已由 [`DbAgentRunner`](../src/modules/db_deepagent/runner.py) 注入模型调用配置，无需新增装配链路。

- `on_chat_model_start()` 可以取得经过 Agent 中间件处理后传给聊天模型的消息；回调 `messages` 的类型是 `list[list[BaseMessage]]`，外层是批次，内层才是逐条消息。
- 回调的 `kwargs["invocation_params"]` 可以取得当前绑定的工具定义等调用参数，包括实际存在的 `tool_choice`、`response_format`；当前 OpenAI 兼容模型实现会将绑定参数合入该字典。
- 结构化输出的追踪描述可能位于 `kwargs["options"]["ls_structured_output_format"]`，包含 `kwargs` 和 `schema`，其中生成方法等信息位于 `kwargs` 内；它与实际调用参数 `response_format` 不是同一字段，需分别读取。
- `on_llm_end()` 可以取得对应模型输出。
- `on_llm_error()` 可以取得调用失败事件。
- 观测器实例已经绑定业务 `request_id`、`run_id`；回调参数 `run_id` 是模型调用尝试 ID，二者必须区分。

记录范围是模型调用层的完整消息与工具定义，不将其称为供应商 HTTP 请求原始报文。LangChain 及供应商适配器仍会进行消息格式转换。

### 2.4 现有文件日志配置可以直接复用

[`logging.yaml`](../src/configs/logging.yaml) 已配置：文件 `logs/nl2sql.log` 接收 DEBUG，控制台接收 INFO。

完整提示词上下文及对应模型输出必须通过现有模块日志器的 `logger.debug()` 打印，写入现有日志文件，不使用 `print()` 或 `logger.info()` 打印这些正文。沿用现有日志轮换，无需修改日志配置或新增日志服务。

## 3. 记录内容与格式

### 3.1 完整输入

仅记录 `dbda_call_kind="agent"` 的主 Agent 调用，摘要模型调用沿用现有统计。

输入块包含：

1. 业务 `request_id`、业务 `run_id`、模型调用 `attempt_id`，以及模型名称。
2. 回调收到的全部消息，按“批次→消息”两层遍历，在每个批次内按原始顺序逐条展示，保留角色和完整正文。当前单批次调用的消息位于 `messages[0]`；不能把内层消息列表当成一条消息，也不能固定只取首批而丢失其他批次。
3. AI 消息中的工具调用名称、调用 ID 和完整参数。
4. Tool 消息中的工具调用关联 ID 和完整返回内容。
5. 本次模型可见的完整工具定义，包括描述和参数结构；从 `kwargs["invocation_params"]` 读取并记录实际存在的 `tools`、`tool_choice`、`response_format`。如 `kwargs["options"]` 中存在 `ls_structured_output_format`，另外按该原字段名记录其完整描述。

上述两个来源分别按实际字段记录，缺失时不推造，也不互相替代。不能将 `options` 中的 `ls_structured_output_format` 重命名为 `response_format`，或因它存在而省略实际绑定的 `response_format`。

只记录实际存在于此次输入中的资料。RAG、表结构、关联关系、日期范围或历史消息若未进入本次输入，日志不能自行补齐。消息已被摘要时，记录模型实际收到的摘要，不恢复成摘要前的历史。

完整保留回调实参中的消息正文、结构化内容块及工具调用，不自行截断，不使用省略号替换长内容，也不只提取字符串正文。中文按可读文本输出。

当前 SDK 会先通过 `_format_for_tracing()` 处理消息，再传入 `on_chat_model_start()`；这一步可能转换图片、文件内容块，或为缺少 `type` 的单键内容块补充类型。因此，本文“完整记录”的断言对象是回调实际收到的消息，不承诺与模型内部消息或供应商 HTTP 请求逐字段相同。

从调用参数和回调 `options` 中按上述字段选取日志内容，不整体打印模型对象、`serialized`、所有 `invocation_params` 或所有 `options`，避免顺带打印认证信息等无关配置。

### 3.2 对应输出

调用成功后，以相同 `attempt_id` 记录返回消息的完整正文及工具调用参数。模型返回 `validate_sql` 时，其 SQL 参数与前面的输入块直接对应。

`on_llm_end()` 没有调用开始时的用途标记，可从现有 `_attempts` 中读取该尝试已登记的 `call_kind`，只为主 Agent 输出正文，不新增一套用途跟踪表。

调用失败时输出同一 `attempt_id` 的失败标记及错误类型，保留已经写出的输入块。重试产生的新尝试分别记录，不覆盖之前的输入。

### 3.3 可读日志示例

```text
===== MODEL_INPUT_BEGIN request_id=req_… run_id=run_… attempt_id=… =====
模型：实际模型名称

[消息 1 / System]
完整系统提示词，包括框架追加的说明……

[消息 2 / Human]
当前问题或历史问题……

[消息 3 / AI / 工具调用]
tool_call_id=call_…
name=search_schema
args={完整参数}

[消息 4 / Tool]
tool_call_id=call_…
完整 RAG 返回内容……

其余消息按实际顺序完整输出

[Tools]
本次模型可见的完整工具定义
===== MODEL_INPUT_END request_id=req_… run_id=run_… attempt_id=… =====

===== MODEL_OUTPUT_BEGIN request_id=req_… run_id=run_… attempt_id=… =====
[AI / 工具调用]
name=validate_sql
args={包含完整 SQL 的实际参数}
===== MODEL_OUTPUT_END request_id=req_… run_id=run_… attempt_id=… =====
```

示例中的省略文字只是说明占位，实际日志不得截断。每个完整输入或输出块各使用一次 `logger.debug()` 写出，避免逐行打印造成并发记录难以阅读；调用间的对应关系仍以标识符为准。

## 4. 修改清单

首版预计修改两个文件：

| 文件 | 修改位置 | 建议修改 |
| --- | --- | --- |
| `src/modules/db_deepagent/middleware/model_observer.py` | `ModelCallObserver.on_chat_model_start()` | 沿用 `_start()`，在调用开始时记录主 Agent 完整输入。 |
| 同上 | `ModelCallObserver.on_llm_end()` | 沿用 `_end()`，补充同一尝试的主 Agent 输出，包含 SQL 工具参数。 |
| 同上 | `ModelCallObserver.on_llm_error()` | 沿用 `_end()`，补充同一尝试的失败标记。 |
| 同上 | 新增私有辅助函数 | 建议增加 `_format_context_messages()`、`_log_model_input()`、`_log_model_output()`，分别负责消息排版、输入块和输出块，避免回调内堆积排版代码。失败标记可直接在现有错误回调中记录。 |
| `src/tests/db_deepagent/unit/test_model_observer.py` | 扩展现有测试 | 验证完整内容、输入输出关联及真实模型回调链路。 |

上述辅助函数名称作为实施建议，职责清晰即可，不为排版新增框架或公共抽象。

Runner 已注入回调，日志配置已支持 DEBUG，因此预计无需修改 `runner.py`、`agent.py`、`request_audit.py` 或 `logging.yaml`。

实现时保留原有调用计数、用量、重试、异常及 `audit.jsonl` 行为。完整正文只写现有文本日志，不再复制一份到 `audit.jsonl`。

排版前通过 `logger.isEnabledFor(logging.DEBUG)` 检查是否启用 DEBUG；该检查为假时不构造完整正文。它判断的是 logger 的有效级别，不检查 handler：提高 logger 的有效级别至 INFO 或 WARNING 可以跳过正文排版；若仅将文件 handler 的级别提高至 INFO，而 logger 仍允许 DEBUG，正文仍会排版，只是不写入该文件。沿用此标准日志行为，不增加 handler 探测机制。

日志辅助代码不修改消息或工具定义；排版、序列化异常按普通诊断记录处理，不改变查询执行结果。既有模型错误仍按原路径处理。

## 5. 验证与验收

使用现有测试文件补充以下验证，不调用付费模型或真实业务库：

1. 使用实际的批次包装 `[[System, Human, AI, Tool]]` 验证消息分别展示、角色及顺序正确，不能把整段列表当成一条记录；补充多批次输入，确认每批消息均保留且批次边界可辨。保留中文、工具参数、关联 ID、结构化内容及长正文末尾标记。
2. 记录完整工具定义，而不是只记录工具名称；可选调用字段按实际存在情况输出。覆盖 `invocation_params` 中的 `tool_choice`、`response_format`，以及 `options` 中的 `ls_structured_output_format`；验证两类结构化输出字段同时存在时分别保留，单独存在或缺失时不互相替代、不推造。
3. 主 Agent 输入和输出共享业务标识及尝试 ID；摘要调用不输出完整正文；失败尝试仍保留输入并产生对应失败标记。
4. 使用真实 LangChain 聊天模型回调链路和本地模拟响应，验证日志完整保留回调实参中的消息及绑定工具；再与模拟请求核对普通文本、工具调用及参数一致。结构化内容块按当前 SDK 的实际转换规则核对，加入一个会被 `_format_for_tracing()` 转换的内容块用例，明确允许且验证该格式差异，不能将“日志忠实记录回调实参”等同于“回调消息与 HTTP 请求逐字段相同”。同时确认 `validate_sql` 输出能定位到其生成前输入。不能只手工调用回调就声称完整链路成立。
5. 原有观测器测试继续通过，原消息和工具参数不被日志代码修改。
6. 完整输入、输出正文的日志级别均为 DEBUG；logger 有效级别不允许 DEBUG 时不排版正文、不打印这些日志，不影响模型调用。仅提高文件 handler 级别时，允许正文仍排版，但该 handler 不输出 DEBUG 正文，与标准日志行为一致。

实施后可在用户正常查询的日志中，搜索 SQL 或 `validate_sql`，取得该次 `MODEL_OUTPUT` 的 `attempt_id`，再定位相同标识的 `MODEL_INPUT`，直接查看生成它之前的完整上下文。

一次输入块是否包含全部所需资料，取决于该次实际模型输入；本文验收的是日志忠实记录，而不是通过日志功能改变 Agent 的资料搜集策略。

## 6. 范围

本次仅增加可读日志及对应验证，不新增模型调用、工具、执行门控、成本控制或调度机制，不修改 SQL 生成逻辑。日期工具引入 LLM、generate_sql 是否独立，继续按用户计划分别讨论。
