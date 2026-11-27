# db_deepagent 工具调用日志增强 Implementation Plan

> 状态：代码实施与离线验证已完成；本地真实 CLI 验收受 Milvus 连接环境限制未完成，联合发布后的容器 INFO 验收待执行。
>
> 实施与回归记录以当前代码和测试为准；发布后的验收按 §1.4 执行。

**Goal:** 在现有工具调用拦截器中补充开始、结束、耗时、结果摘要和异常日志，使 INFO 日志即可还原工具执行顺序与耗时，并将完整参数及较大输出限制在 DEBUG。

**Architecture:** 复用 `RunBudgetMiddleware.wrap_tool_call()` 这一现有统一入口，不新建日志模块、不逐个修改业务工具。每次调用使用局部单调时钟计算耗时，使用现有 `audit_logger` 输出；日志格式增加毫秒，便于按 `tool_call_id` 配对并分析串行、并行和执行区间。

**Tech Stack:** Python 3.12、标准库 `logging` / `time.monotonic()`、LangChain Agent Middleware、pytest。

**Spec:** 本文即设计与实施依据；实施时同时遵守 `nl2sql/AGENTS.md`。

## Global Constraints

- 运行时不创建新的日志模块、Logger、Handler 或日志文件，复用现有 `audit_logger` 和 `nl2sql.log`；测试专用捕获 Handler 用完即移除。
- 本次新增的 RunBudgetMiddleware INFO 只写一行式摘要；完整工具参数、完整返回、原始异常文本、异常堆栈和其他较大内容只写 DEBUG。本次新增的 ERROR 默认也不通过 `exc_info` 附带原文或堆栈；范围外既有日志不在本约束内。
- 日志时间精确到毫秒；每次工具调用记录 `elapsed_ms`，耗时使用 `time.monotonic()` 计算。
- 不修改工具参数、返回值、预算计数、任务账本、SQL 执行、异常抛出及最终答案行为。
- 日志构造或序列化失败不得影响工具执行，不得把原工具异常替换为日志异常。
- 本地与容器均以 `logging.yaml` 为默认配置。业务文件日志为 `DEBUG`，console 与 `nl2sql.api` 为 `INFO`。可在运行环境中显式覆盖业务日志级别。
- 本次不依赖 LangSmith；LangSmith 开启或关闭均不改变本地日志行为。
- 未经用户另行授权，不提交、不推送、不部署。

## Review Focus

- **同名工具并行或交错执行：**开始与结束必须通过不同 `tool_call_id` 正确配对，不能使用全局唯一“当前工具”状态；由 Task 2 的交错执行测试按 ID 断言不同的精确耗时，而非仅检查非负。
- **业务错误、框架错误与未处理异常不同：**JSON envelope 业务错误及 `ToolMessage.status="error"` 框架错误都写 `event=finished status=error`，但分别带 `error_source=business/framework`，即使 `error_type` 相同也可区分；未处理异常才写 ERROR 并原样重抛。由 Task 2 对应测试覆盖。
- **非文本内容块：**Base64 等内容块不得按块数计字符或强行 JSON 解析；终态照常记录、`output_chars=-`、DEBUG 跳过二进制正文，由 Task 2 非文本测试覆盖。
- **预算拒绝：** handler 未执行也要形成可识别的拒绝记录和耗时，不能误记为工具执行成功；由 Task 2 拒绝测试覆盖。
- **非 JSON 返回：**`submit_final_answer` 的“已接收最终答案”应记 `status=-`；参数校验失败、未注册工具等非 JSON 框架错误必须根据 `ToolMessage.status="error"` 记为错误，不能都归为 `-`。由 Task 2 两类非 JSON 测试覆盖。
- **本次新增 INFO/ERROR 的大结果与异常：**完整参数、返回、异常原文和堆栈不得进入本次新增的 INFO/ERROR，且 INFO 模式下不应付出完整 DEBUG 内容序列化成本；由 Task 2 日志级别与异常测试覆盖，不验收范围外既有日志。
- **观测代码自身失败：**DEBUG 排版异常不得导致工具重试、改变成功返回对象或替换原工具异常；由 Task 2 两个排版失败测试直接覆盖。

---

## 1. 当前事实与问题

### 1.1 现有统一工具入口

`nl2sql/src/modules/db_deepagent/middleware/run_budget.py:90-168` 的 `wrap_tool_call()` 已取得：

- `ctx.request_id`、`ctx.run_id`；
- `tool_call["name"]`；
- `tool_call["id"]`；
- 工具 handler 的返回或异常。

当前日志只表达“预占是否成功、返回 Command 还是 ToolMessage、是否 exception”，没有完整开始时间、结束时间和耗时。这一入口同时覆盖项目业务工具和 Deep Agents 内置文件工具，适合作为唯一改动点。

### 1.2 现有日志时间只有秒

`nl2sql/src/configs/logging.yaml:5-6` 当前为：

```yaml
format: "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
datefmt: "%Y-%m-%d %H:%M:%S"
```

显式 `datefmt` 使实际输出不含毫秒，不能直接可靠地区分同一秒内开始、结束或交错的工具调用。

### 1.3 DEBUG 的实际去向

`logging.yaml` 将 `nl2sql` 业务 Logger 和 `nl2sql_file` Handler 配置为 `DEBUG`，将 console Handler 和 `nl2sql.api` 配置为 `INFO`。`logger.py` 在运行环境显式设置 `NL2SQL_LOG_LEVEL` 时，仅覆盖前两项。[日志配置](../src/configs/logging.yaml) [初始化代码](../src/utils/logger.py)

工具调用的 DEBUG 详情写入 `nl2sql.log`。本地验收按 Task 3 分别运行 INFO 和 DEBUG 用例。

### 1.4 容器日志级别与验收

**调整容器日志级别。**在开发 `nl2sql/.env` 显式设置 `NL2SQL_LOG_LEVEL` 后，按 Doc 16 §3.1 发布，重建容器时载入新值。删除这一项后再发布，恢复 YAML 中的 `DEBUG`。[Doc 16](../../docs/16_后端环境配置随发布同步设计.md)

**INFO 模式验收。**在开发 `nl2sql/.env` 显式设置 `NL2SQL_LOG_LEVEL=INFO` 后发布，核对新容器内的变量值及业务 Logger、文件 Handler 的有效级别。复用**包含 Doc 22 改动的新容器**中已实际触发工具的问数，或用该容器中的第一条实际工具问数补验。在 `/app/nl2sql/logs/nl2sql.log` 按 `request_id` 核对工具 started、对应终态、`elapsed_ms` 和毫秒时间戳，并确认 INFO 记录只有摘要。

**DEBUG 模式验收。**先核对业务 Logger 和文件 Handler 的有效级别是 `DEBUG`。排障时从原入口复现一次，按同一 `request_id` 和同一工具调用核对 `TOOL_INPUT` 及 `TOOL_OUTPUT`，工具抛异常时核对 `failed` 和对应的 DEBUG 堆栈。DEBUG 详情在 `nl2sql.log` 中查看。

**排障后恢复。**如果为排障改过环境变量，在开发 `nl2sql/.env` 恢复先前的显式设置或删除临时覆盖，按 Doc 16 §3.1 发布后核对新容器的有效级别。恢复到 `INFO` 必须显式设置 `NL2SQL_LOG_LEVEL=INFO`。

## 2. 修改范围

| 文件 | 计划修改 | 不负责的内容 |
| --- | --- | --- |
| `nl2sql/src/modules/db_deepagent/middleware/run_budget.py` | 在 `wrap_tool_call()` 增加开始、结束、拒绝、异常及 DEBUG 详情日志 | 不改变预算与工具业务逻辑 |
| `nl2sql/src/configs/logging.yaml` | standard formatter 增加毫秒，console、`nl2sql_file`、`api_file` 三处输出同步变化 | 不改变 Handler、日志级别和滚动策略 |
| `nl2sql/src/tests/db_deepagent/unit/test_budget_middleware.py` | 增加工具日志行为测试 | 不重新测试所有业务工具 |
| `nl2sql/src/tests/db_deepagent/unit/test_tool_output_offload.py` | 复用现有离线大结果用例，增加 RunBudget 日志断言 | 不新增真实 LLM 调用，不改变转存行为 |
| `nl2sql/src/tests/unit/utils/test_log_level_env.py` | 增加毫秒格式测试，保留现有 INFO/DEBUG 覆盖测试 | 不改变环境变量覆盖逻辑和默认级别 |

不修改 `tools/*.py`，不修改 `model_observer.py`，不新增 `model_decision` 日志，不新增结构化 `audit.jsonl` 事件；这些都超出本次审核范围。特别是 `inspect_tables` 在 `schema_tools.py:324` 已有的 `logger.exception()` 保持原样；INFO/ERROR 内容限制与测试断言只针对本次 RunBudgetMiddleware 新增的记录，不把既有工具或其他模块的堆栈日志判作本次回归。

格式变化同时影响控制台、`nl2sql.log` 和 `api.log`。遗留脚本 `nl2sql/tests/test_q_777b0b84_log_analysis.py:34-36` 仍按秒解析特定历史日志，新格式下不能解析新日志；它属于旧 `nl2sql_father` 单次历史运行分析，依赖固定 `QUERY_ID` 和历史文件，且不在 `pytest.ini` 的默认 `testpaths` 中。本次不修改该遗留脚本，也不为其扩大范围。

## 3. 日志内容设计

### 3.1 INFO：工具开始

每次进入 `wrap_tool_call()` 时记录，放在预算预占之前，使预算拒绝也有对应的开始记录：

```text
2026-11-25 10:30:15.120 - INFO - ... - [request_id=req_x run_id=run_x] tool=execute_sql id=call_x event=started
```

字段仅限：

- `request_id`；
- `run_id`；
- `tool`；
- `id`（即 `tool_call_id`）；
- `event=started`。

INFO 开始日志不记录 SQL、文件正文、Schema 或其他参数值。

### 3.2 INFO：正常返回的业务结果与框架错误

handler 正常返回后立即计算 `elapsed_ms` 并先记录一条结束日志，再排版或输出 DEBUG OUTPUT，避免输出详情排版时间被计入工具执行区间：

```text
2026-11-25 10:30:15.146 - INFO - ... - [request_id=req_x run_id=run_x] tool=execute_sql id=call_x event=finished elapsed_ms=23 result_kind=command status=ok error_type=- error_source=- output_chars=512
```

摘要函数先取得与本次调用对应的 ToolMessage：handler 直接返回 ToolMessage 时直接读取；返回 Command 时，从 `update["messages"]` 中选择 `tool_call_id` 与本次调用匹配的 ToolMessage。项目业务工具的 `tool_command()` 已按此结构返回，见 `tools/_envelope.py:90-101`。当前 Command 均带匹配消息；若异常输入导致找不到，则 `status`、`error_type`、`error_source`、`output_chars` 均记 `-`，不得猜测或抛出新异常。只有 `content` 为字符串时才尝试 JSON envelope 解析和字符计数；内容块列表不做 JSON 解析。

字段定义：

- `elapsed_ms`：从进入 `wrap_tool_call()` 到 handler 返回的整数毫秒，包含 INFO started、预算预占、DEBUG INPUT 和 handler 执行开销；不包含终态之后的 DEBUG OUTPUT 或异常堆栈排版时间；
- `result_kind`：`command` 或 `tool_message`；
- `status`：字符串 JSON envelope 按现有 `status` 读取 `ok`、`empty` 或 `error`；非 JSON 且 `ToolMessage.status="error"` 时记为 `error`；其余非 JSON 返回（包括非文本内容块）记为 `-`；
- `error_type`：优先从 envelope 的 `error.error_type` 读取，但只接受非空字符串错误码，对象、列表或其他类型记为 `-`；非 JSON 字符串框架错误以 `invalid_argument:` 开头时记为 `invalid_argument`，其他 `ToolMessage.status="error"`（含非文本错误）记为 `tool_error`；其余情况记为 `-`；
- `error_source`：业务工具 JSON envelope 错误记 `business`，非 JSON `ToolMessage.status="error"` 框架错误记 `framework`，RunBudget 自己产生的预算拒绝记 `middleware`，其他情况记 `-`。因此同为 `error_type=invalid_argument` 时也能区分业务必填校验与框架参数校验，原错误码不变；
- `output_chars`：本次调用匹配的 ToolMessage **字符串文本**长度，取值发生在**工具自身分页或截断之后、FilesystemMiddleware 外层转存之前**，不输出文本本身。它不是工具底层未分页数据的总量，也不是模型最终收到的预览长度；`content` 为内容块列表或其他非字符串值时记 `-`，不得使用块数冒充字符数。

`RunBudgetMiddleware` 位于 `FilesystemMiddleware` 之内，后者在 RunBudget 返回后才处理大结果。因此 `read_file` 的 `output_chars` 是本次读取窗口的文本长度，其他工具也只测量其实际返回窗口或截断后的文本。当前 `filesystem.tool_token_limit_before_evict=20000`，SDK 按约 4 字符/token 估算；只有工具适用转存、文本超过约 80000 字符且转存成功时，模型才收到预览和文件路径。工具被排除、未配置阈值或转存失败时保留原文；SDK 转存本身不记日志。

业务 envelope 错误：handler 正常返回，envelope `status=error`，`ToolMessage.status` 保持默认 `success`，记 `error_source=business`。框架层错误（参数校验失败、未注册工具、内置工具参数错误）：handler 正常返回，`ToolMessage.status="error"`，内容非 JSON，记 `error_source=framework`。两类都记 `event=finished status=error`，不写 ERROR、不伪造 Python 异常堆栈。

非文本内容块不阻碍终态记录：仍写 started/finished、`result_kind`、耗时及适用的 `status/error_source`；`output_chars=-`，DEBUG 不输出 Base64 或其他二进制正文。

### 3.3 INFO：预算拒绝

预算预占失败时保留现有拒绝返回，并记录：

```text
... tool=validate_sql id=call_x event=rejected elapsed_ms=0 result_kind=command status=error error_type=budget_exhausted error_source=middleware stop_reason=tool_call_limit output_chars=...
```

`rejected` 明确表示 handler 未执行，不能与 `finished status=ok` 混淆；`stop_reason` 保留预算拒绝的具体原因。只将现有 `audit_logger` 的“预占=rejected:<stop_reason>”INFO 替换为该结束记录，级别仍为 INFO；`run_budget.py:104-109` 现有的 `logger.warning` 原样保留，不降低预算耗尽的 WARNING 信号。

### 3.4 ERROR：未处理异常

handler 抛出异常时：

- 使用相同 `request_id`、`run_id`、`tool` 和 `id`；
- 捕获异常后立即计算 `elapsed_ms`，先通过 `audit_logger.error()` 记录 `event=failed`、`elapsed_ms` 和 `exception_type`，再处理 DEBUG 堆栈；
- ERROR 不记录 `str(exc)`，不得使用 `exc_info=True`，因为格式化器会附加原始异常和堆栈，可能把 SQL 或工具数据带入默认 INFO 日志；
- 在 DEBUG 已启用时，于活动 `except` 块内另用 `audit_logger.debug(..., exc_info=True)` 记录原始异常和完整堆栈；
- 原异常不变、原样重抛。

默认日志不强制保留原始堆栈：简短 ERROR 已保留调用身份、失败结局、耗时和异常类型，能够关联同一请求；需要原文和堆栈时临时开启 DEBUG。原始异常仍抛给上层，不被日志行为吞没。

预期由工具 envelope 表达的业务错误不走此分支。框架将参数校验失败、未注册工具或内置工具参数错误转换为 `ToolMessage.status="error"` 并正常返回时，也不走此分支，而按 §3.2 记录 `event=finished status=error`。

### 3.5 DEBUG：完整参数和返回

仅在 `audit_logger.isEnabledFor(logging.DEBUG)` 为真时序列化并输出：

```text
===== TOOL_INPUT_BEGIN request_id=req_x run_id=run_x tool=execute_sql id=call_x =====
<完整工具参数>
===== TOOL_INPUT_END request_id=req_x run_id=run_x tool=execute_sql id=call_x =====
```

```text
===== TOOL_OUTPUT_BEGIN request_id=req_x run_id=run_x tool=execute_sql id=call_x =====
<完整工具返回>
===== TOOL_OUTPUT_END request_id=req_x run_id=run_x tool=execute_sql id=call_x =====
```

要求：

- 输入块、输出块分别拼成一个字符串，各自通过**一次** `audit_logger.debug()` 输出，与 doc 13 的 MODEL 块风格一致，不把 BEGIN、正文和 END 拆成三条日志；
- 大段 SQL、Schema、文件内容、查询结果和工具返回只出现在 DEBUG；
- INFO 模式不执行完整参数的 JSON 序列化，避免无意义 CPU 和内存开销；
- DEBUG 排版失败时降级记录类型、长度及排版异常，不影响原工具返回；
- TOOL_OUTPUT 与 `output_chars` 使用同一份本次调用匹配的 ToolMessage 字符串文本：工具自身分页或截断之后、FilesystemMiddleware 转存之前。`read_file` 记录的是本次读取窗口，不是目标文件全文；
- `content` 为内容块列表或其他非字符串值时，不尝试 JSON 解析，不序列化 Base64、图片、音频或其他二进制正文；DEBUG 仅记录块数量和块类型等不含内容数据的摘要，`output_chars=-`；
- 对适用且成功转存的大结果，DEBUG 仍能查看转存前正文；模型最终收到预览和文件路径。未转存时两者看到的是同一份工具返回，不把“超过阈值”误写成“必然转存”；
- doc 13 的 MODEL_OUTPUT 已包含工具名称、ID 和参数，TOOL_INPUT 会与其重复；本次为便于按同一 `tool_call_id` 成对阅读输入和输出，知情保留该重复，成本仅发生在 DEBUG；
- 不改变工具原本的结果转存机制，不调整 Middleware 顺序，不新增转存日志，不在 INFO 重复保存大结果。

## 4. 时间与并行分析约定

### 4.1 日志格式增加毫秒

计划将 standard formatter 调整为：

```yaml
format: "%(asctime)s.%(msecs)03d - %(levelname)s - %(name)s - %(message)s"
datefmt: "%Y-%m-%d %H:%M:%S"
```

目标形式：

```text
2026-11-25 10:30:15.123 - INFO - ...
```

该格式复用 LogRecord 的 `msecs`，不引入自定义 Formatter。

### 4.2 耗时与时间戳分工

- 日志时间戳：用于真实时间排序、开始/结束配对、判断执行区间是否重叠；finished/failed/rejected 必须先于对应的 DEBUG OUTPUT 或异常堆栈写入，防止输出详情排版期间另一个工具开始而造成虚假重叠；started 后的 DEBUG INPUT 位于预算预占成功之后、handler 前，其开销属于本次中间件执行区间，但不得改变预占判定；
- `time.monotonic()`：用于计算单次调用 `elapsed_ms`，避免系统校时影响；
- 每次调用的时间状态保存在 `wrap_tool_call()` 局部变量中，不在 Middleware 实例上保存“当前调用”，以支持并行或交错执行。

### 4.3 耗时口径

本方案的 `elapsed_ms` 是 **RunBudgetMiddleware 工具拦截耗时**，不是模型耗时，也不替代 SQL 核心执行耗时：

- 模型生成工具调用的耗时仍由现有模型审计提供；
- SQL 本身的 `elapsed_ms` 仍由 SQL pipeline 和证据记录提供；
- FilesystemMiddleware 的大结果转存发生在 RunBudget 返回之后，因此本次工具拦截耗时不包含外层转存时间；
- 模型耗时、SQL 耗时与工具拦截耗时可以用于区分模型等待、工具框架/状态更新和数据库执行。

并行判断必须比较同一轮日志中的开始、结束区间；仅看到多个工具名称不能直接断定并行。

## 5. 实施任务

### Task 1: 日志时间精确到毫秒

**Files:**
- Modify: `nl2sql/src/configs/logging.yaml:5`
- Test: `nl2sql/src/tests/unit/utils/test_log_level_env.py`

**Interfaces:**
- Consumes: 现有 `setup_logging_from_yaml()` 和 standard formatter。
- Produces: `YYYY-MM-DD HH:MM:SS.mmm` 格式；不改变任何 Logger、Handler 接口或默认级别。

- [ ] **Step 1: 编写失败测试 `test_standard_formatter_includes_milliseconds`**

从现有 YAML 构造 standard formatter，格式化固定 `LogRecord`，断言时间部分匹配 `^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$`。保留现有 INFO/DEBUG 环境变量覆盖测试。

- [ ] **Step 2: 验证测试在旧格式下失败**

Run: `cd nl2sql && .venv/bin/python -m pytest src/tests/unit/utils/test_log_level_env.py::TestLogLevelEnv::test_standard_formatter_includes_milliseconds -v`

Expected: FAIL，旧值只有秒。

- [ ] **Step 3: 修改 standard formatter**

按 §4.1 修改 `format`，保留现有 `datefmt` 和所有 Handler 配置。确认 console、`nl2sql.log`、`api.log` 均变为毫秒格式。

- [ ] **Step 4: 运行完整日志配置测试**

Run: `cd nl2sql && .venv/bin/python -m pytest src/tests/unit/utils/test_log_level_env.py -v`

Expected: 全部 PASS。未设置环境变量时，业务 Logger 和文件 Handler 使用 YAML 的 `DEBUG`。显式设为 `INFO` 时，两者使用 `INFO`。

### Task 2: 在现有工具拦截器补充日志

**Files:**
- Modify: `nl2sql/src/modules/db_deepagent/middleware/run_budget.py:90-168`
- Test: `nl2sql/src/tests/db_deepagent/unit/test_budget_middleware.py`
- Test: `nl2sql/src/tests/db_deepagent/unit/test_tool_output_offload.py`（复用现有离线大结果用例）

**Interfaces:**
- Consumes: 现有 `RunBudgetMiddleware.wrap_tool_call(request, handler)`、`ToolMessage`、`Command` 和 `audit_logger`。
- Produces: 不新增公共接口；可在同文件增加私有结果摘要函数，返回仅含 `result_kind`、`status`、`error_type`、`error_source`、`output_chars` 的摘要。

- [ ] **Step 1: 编写正常返回的失败测试 `test_tool_logs_start_end_with_elapsed_and_summary`**

参照 `test_model_observer.py:138-168` 的 `_DebugCapture` 增加本测试文件私有的 `_AuditCapture(logger_name: str, level: int)`：把捕获 Handler 直接挂到指定 Logger，显式设置所需级别，并在退出时移除 Handler、恢复原级别。捕获器保存完整 `LogRecord` 对象，而不是只保存 `record.getMessage()`；普通正文断言可使用 `getMessage()`，异常断言必须使用 `logging.Formatter().format(record)` 检查最终格式化结果。普通工具日志用例传入 `nl2sql.nl2sql.db_deepagent.audit`；需要核验预算 WARNING 时，另用实例捕获 `nl2sql.nl2sql.db_deepagent.middleware.run_budget`。这样无论 `propagate` 当前值、是否已加载 YAML、环境变量为何都稳定，且不依赖 root 传播、不会重复捕获。

通过受控 `time.monotonic()` 让 handler 返回固定耗时；断言同一 `tool_call_id` 有 started/finished、正确 `elapsed_ms`、`result_kind`、`status` 和 `output_chars`，并断言 finished LogRecord 先于对应 TOOL_OUTPUT LogRecord。

- [ ] **Step 2: 编写 INFO/DEBUG 隔离测试 `test_tool_payload_only_appears_at_debug`**

使用带明显长文本的参数和返回，分别通过 `_AuditCapture` 将级别固定为 INFO 和 DEBUG：INFO 下不得出现完整参数或返回，也不得调用完整 DEBUG 序列化；DEBUG 下出现 §3.5 的精确配对标记，且每个输入/输出块各为一条 LogRecord，内容完整。

- [ ] **Step 3: 编写错误、拒绝和非 JSON 测试**

分别覆盖：

- `test_tool_business_error_logs_finished_without_traceback`：按真实结构使用 `tool_command()` 携带 `error_type=invalid_argument` 的 error envelope，断言其中 ToolMessage 保持默认 `status="success"`，日志通过 envelope 得到 `event=finished status=error error_type=invalid_argument error_source=business`，证明实现没有误走框架错误分支；
- `test_framework_error_tool_message_logs_error_without_traceback`：非 JSON 且 `ToolMessage.status="error"`，内容以 `invalid_argument:` 开头，断言 `event=finished status=error error_type=invalid_argument error_source=framework`；与上一用例共同证明相同错误码也有不同来源；
- `test_framework_error_without_invalid_argument_prefix_uses_tool_error`：覆盖未注册工具或其他框架错误，断言 `error_type=tool_error error_source=framework`；
- `test_tool_exception_logs_elapsed_and_reraises`：异常文本放入 SQL 标记；先断言 ERROR LogRecord 的 `exc_info` 为空，再用 `logging.Formatter().format(record)` 验证 INFO/ERROR 的最终格式化文本只含 `exception_type`，不包含 SQL 标记、原始异常文本或 `Traceback`，避免 `getMessage()` 造成误判；DEBUG 记录则以同样方式验证确实输出原文和堆栈，failed ERROR 必须先于 DEBUG 堆栈，且重抛对象与原异常相同；
- `test_tool_rejected_logs_rejected_and_preserves_warning`：分别对 audit logger 和 `nl2sql.nl2sql.db_deepagent.middleware.run_budget` 使用一个 `_AuditCapture`，两边都显式设级别并在结束时恢复；断言 handler 未调用、audit 的 rejected 行含 `stop_reason error_source=middleware`，另一个 Logger 上现有 WARNING 保留；
- `test_non_json_tool_message_uses_unknown_business_status`：仅覆盖“已接收最终答案”这类非 JSON 成功返回，断言 `status=- error_source=-`；
- `test_non_text_tool_message_skips_binary_payload`：构造含 Base64 标记的内容块列表，断言 started/finished 终态仍存在、`output_chars=-`，不尝试 JSON 解析，DEBUG 只有块数量/类型摘要且不含 Base64 标记；另构造非文本 `ToolMessage.status="error"`，断言 `status=error error_type=tool_error error_source=framework`。

断言业务行为与原有返回完全一致，异常对象原样重抛。框架错误和预算拒绝不得输出异常堆栈。

- [ ] **Step 4: 编写日志排版失败测试**

在 DEBUG 已启用时分别注入两类故障，覆盖成功返回与工具异常后的不同日志路径：

- `test_debug_formatting_failure_does_not_change_success_result`：通过 monkeypatch 让本模块的 DEBUG JSON 序列化（或直接使用的私有排版函数）抛出受控异常；handler 恰好执行一次，返回对象保持同一身份，INFO 结束摘要仍正常；
- `test_exception_debug_logging_failure_preserves_original_exception`：handler 先抛出已保存的原异常对象，随后 monkeypatch `audit_logger.debug`，仅在收到 `exc_info=True` 时抛出受控日志异常，确保命中工具异常后的 DEBUG 堆栈记录而不是工具执行前的 INPUT 排版。断言该注入调用确实发生且 `exc_info=True`、handler 恰好执行一次，最终重抛对象仍是原工具异常，日志异常不得替换、包装或吞掉它。

- [ ] **Step 5: 复用现有离线大结果转存测试**

修改 `test_tool_output_offload.py` 中现有的 `test_execute_sql_large_output_offloaded_with_command`：测试辅助 agent 在 FilesystemMiddleware 内层增加 RunBudgetMiddleware，并为 `run-test` 打开足够大的测试预算，`deadline_monotonic` 必须设为未来时间（例如 `time.monotonic() + 600`），不能沿用测试上下文的 `0.0`。沿用该用例已有的 `(raw_content,) = captured_raw["e2e-x"]` 解包出唯一原文，不直接对列表取长度，不引入第二次工具执行或真实 LLM。

在 DEBUG 捕获下保留原有“模型消息为预览和路径、转存文件逐字等于原文、Command 账本完整”断言，新增：

- `e2e-x` 的 TOOL_OUTPUT 正文逐字等于解包后的 `raw_content`；
- finished 摘要 `output_chars == len(raw_content)`，而不是列表长度 1，也不是最终预览长度；
- 日志中的 `tool_call_id` 与转存消息同为 `e2e-x`。

测试辅助函数可增加可选 Middleware 参数，其他既有转存用例保持原调用方式。日志捕获沿用直接挂 Handler、显式设级别并恢复的方式，不依赖 root 传播。

- [ ] **Step 6: 编写交错执行测试 `test_interleaved_tool_calls_keep_separate_identities`**

用两个受控线程和两个 `tool_call_id` 让调用区间重叠。仅替换 middleware 模块内的时钟，并按线程 ID 提供独立的起止序列：调用 A 为 `100.000 → 100.500`，调用 B 为 `100.125 → 100.375`，两个区间重叠但起止时间和耗时不同。按 `tool_call_id` 分别断言 A `elapsed_ms=500`、B `elapsed_ms=250`，不能只断言非负；同时断言日志可按 ID 配对、开始/结束顺序不被同名工具混淆。测试时钟不得影响预算服务自身的时钟。

- [ ] **Step 7: 运行新增测试并确认旧实现失败**

Run: `cd nl2sql && .venv/bin/python -m pytest src/tests/db_deepagent/unit/test_budget_middleware.py src/tests/db_deepagent/unit/test_tool_output_offload.py -v`

Expected: 新增日志断言 FAIL，原有行为与转存断言不应因测试本身失败。

- [ ] **Step 8: 实现最小日志改动**

只在 `wrap_tool_call()` 及同文件私有日志辅助函数中实现 §3—§4；保持预算预占、返回 Command、状态更新和异常路径不变。正常路径固定为 started → 预算预占成功 → DEBUG INPUT → handler → 计算耗时 → finished → DEBUG OUTPUT；异常路径为 started → 预算预占成功 → DEBUG INPUT → handler 抛错 → 计算耗时 → failed → DEBUG 堆栈 → 原样重抛；预算拒绝不排版 DEBUG INPUT，并先写 rejected 再处理任何 DEBUG OUTPUT 摘要。DEBUG INPUT 在 handler 前，其开销计入 `elapsed_ms`；不得把 DEBUG OUTPUT 或异常堆栈放在终态事件之前。

- [ ] **Step 9: 运行工具中间件与大结果完整测试**

Run: `cd nl2sql && .venv/bin/python -m pytest src/tests/db_deepagent/unit/test_budget_middleware.py src/tests/db_deepagent/unit/test_tool_output_offload.py -v`

Expected: 全部 PASS。

### Task 3: 回归与真实运行验收

**Files:**
- Verify only: 上述实现文件和现有日志；不新增报告或脚手架文件。

**Interfaces:**
- Consumes: Task 1—2 的日志行为。
- Produces: 可供后续按时间分析工具调用的真实 INFO/DEBUG 日志。

- [ ] **Step 1: 运行相关回归测试**

Run: `cd nl2sql && .venv/bin/python -m pytest src/tests/db_deepagent/unit/test_budget_middleware.py src/tests/db_deepagent/unit/test_model_observer.py src/tests/unit/utils/test_log_level_env.py -v`

Expected: 全部 PASS。

- [ ] **Step 2: 在本地 CLI 的 INFO 模式执行一次真实 db_deepagent 问数**

使用 `nl2sql/src/modules/db_deepagent/cli.py` 的单次 `query` 入口，显式设置 INFO：

Run: `cd nl2sql && REQ_ID="req-doc22-info-$(date +%Y%m%d%H%M%S)"; printf 'REQUEST_ID=%s\n' "$REQ_ID"; NL2SQL_LOG_LEVEL=INFO .venv/bin/python -m src.modules.db_deepagent.cli query --source-id db_src_1 --request-id "$REQ_ID" "请问订单数量最多的产品是哪个？"`

使用本轮输出的唯一 REQUEST_ID 过滤真实 `nl2sql.log` 中本次 RunBudgetMiddleware 新增的记录。不预设固定工具数量或顺序，但必须**至少观察到同一 `tool_call_id` 的一对 started 和 finished**，零条工具日志或只有 started 均判定验收失败。其余每个出现的 ID 也必须有 finished/rejected/failed 结局，时间含毫秒、耗时非负，新增 INFO 记录无完整工具大结果；不使用范围外既有日志判定本次验收。

- [ ] **Step 3: 在本地 CLI 的 DEBUG 模式执行一次针对性验证**

在 shell 中显式设置 DEBUG 后运行同一 CLI：

Run: `cd nl2sql && REQ_ID="req-doc22-debug-$(date +%Y%m%d%H%M%S)"; printf 'REQUEST_ID=%s\n' "$REQ_ID"; NL2SQL_LOG_LEVEL=DEBUG .venv/bin/python -m src.modules.db_deepagent.cli query --source-id db_src_1 --request-id "$REQ_ID" "请问订单数量最多的产品是哪个？"`

按本轮 REQUEST_ID 至少找到一对同一 ID 的 started/finished，并确认该调用的普通字符串参数与实际 ToolMessage 返回进入 DEBUG，console 仍保持 INFO。该真实问数只验收真实日志链路，**不要求出现大结果转存**；转存前全文及 `output_chars` 的确定性验收由 Task 2 Step 5 的既有离线大结果用例完成。命令中的 `NL2SQL_LOG_LEVEL=DEBUG` 仅用于本次 CLI 调用。容器内的日志验收见 §1.4。

- [ ] **Step 4: 核对交付范围**

检查 Git 状态和差异，确认只有本方案批准的实现与测试变更，没有生成物、临时日志、密钥或无关修改。真实运行的模型选择可能不同，不为了复现旧工具数量而修改提示词或业务逻辑。

## 6. 审核验收标准

审核通过后，开发完成必须同时满足：

- [ ] INFO 能按 `request_id + run_id + tool_call_id` 还原每次工具调用的开始、结局和耗时；
- [ ] 日志时间精确到毫秒，且成功、业务 envelope 错误、框架层 `ToolMessage.status="error"`、预算拒绝、未处理异常均有明确区分；业务与框架错误分别带 `error_source=business/framework`，即使同为 `invalid_argument` 也不可混同；
- [ ] 非 JSON 成功返回保持 `status=-`，框架错误不得被误记为 `status=-`；非文本内容块照常记录终态、`output_chars=-`，DEBUG 不包含 Base64 或其他二进制正文；
- [ ] `output_chars` 和 DEBUG 输出明确对应工具自身分页或截断之后、外层转存之前的匹配 ToolMessage 文本，不把它们解释为底层未分页数据总量或模型最终预览长度；
- [ ] 本次 RunBudgetMiddleware 新增的 INFO/ERROR 记录不含完整 SQL、Schema、文件正文、大段查询结果、原始异常文本或异常堆栈；该断言不覆盖范围外既有日志；
- [ ] DEBUG 能按需查看完整工具输入输出及异常堆栈，且 INFO 模式不序列化这些大内容；
- [ ] 日志排版失败时工具只执行一次，成功返回对象不变，原工具异常原样重抛；
- [ ] 既有离线大结果用例证明 DEBUG 保留转存前正文、`output_chars` 等于原文长度，同时模型收到预览和路径；
- [ ] finished/failed/rejected 均先于对应 DEBUG OUTPUT 或异常堆栈；INFO started 与 DEBUG INPUT 开销计入 `elapsed_ms`，终态后的输出详情排版不延长工具执行区间；
- [ ] 并行或交错调用不共享计时状态，可根据 ID 和时间区间分析；双线程测试按 ID 分别验证不同的精确 `elapsed_ms`，不只检查非负；
- [ ] 工具返回、预算计数和异常行为与修改前一致；
- [ ] 相关测试通过；真实问数使用唯一 `--request-id` 过滤日志，至少核验到一对同一 `tool_call_id` 的 started/finished，零工具日志不得判为通过；
- [ ] 未设置 `NL2SQL_LOG_LEVEL` 时，容器按 YAML 使用业务文件 `DEBUG` 和 console/API `INFO`。
- [ ] INFO 模式验收显式设置 `NL2SQL_LOG_LEVEL=INFO`，并按 §1.4 在包含 Doc 22 改动的容器中核对级别及真实工具调用日志。
- [ ] 容器 DEBUG 排障按 §1.4 从原入口复现，按同一调用核对输入、输出或异常堆栈。若曾临时修改覆盖值，排障后恢复先前设置并重新发布，核对实际级别。
