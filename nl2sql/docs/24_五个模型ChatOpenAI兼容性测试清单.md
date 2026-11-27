# 七个官方模型的 ChatOpenAI 兼容性测试清单

> 状态：2026-11-25 已按本清单实测七个目标模型。下表是测试前根据供应商文档整理的预期，保留作对照；实际结果、失败与未验证项见 [测试报告](reviews/2026-11-25_Doc24_ChatOpenAI兼容性测试报告.md)，§5 的实测记录以报告为准。

这份清单用于判断模型能否通过当前 NL2SQL 锁定的 `langchain_openai.ChatOpenAI` 请求 Chat Completions，常用参数和关闭思考设置是否生效，以及能否承担主 Agent、摘要或日期解析。开启思考的主 Agent 还要检查推理内容在请求、checkpoint 和摘要中的去向。单轮文本调用成功，只能证明这次文本请求可用。

本清单为 [23_ 模型配置与 LLM 适配改造方案](23_NL2SQL模型配置与LLM适配改造方案.md) 提供选型依据。先用纯 `ChatOpenAI` 验证接口，再用 NL2SQL 的真实装配验证角色。测试使用合成数据。Qwen embedding 仍用现有配置，业务链路验收时核对它是否继续提供检索向量。

## 1. 模型、端点与预期

本轮只测各提供商官方服务上的自家模型。百炼只测 Qwen，GLM 使用智谱国内 Coding Plan，Kimi 使用国内 Kimi Code，DeepSeek 使用自家官方 API。GLM 和 Kimi 的套餐地址见 §1.4。验收以 **提供商、区域或套餐、端点、实际模型 ID** 为单位。表中关闭参数指最终 HTTP 请求体中的字段。

| 编号 | 模型与端点 | 显式关闭参数 | 文档预期 |
| --- | --- | --- | --- |
| Q4M | 阿里云百炼，`qwen4-max` 正式版，`https://dashscope.aliyuncs.com/compatible-mode/v1`（中国内地） | `{"reasoning_effort":"none"}` | 官方将 `none` 定义为关闭思考。省略时默认 `high`。对照组用 `high`，两组均省略 `thinking_budget`，避免参数冲突。 [正式版模型页](https://help.aliyun.com/zh/model-studio/qwen4-max) · [兼容接口参数](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions) |
| Q38F | 阿里云百炼，`qwen3.8-flash`，`https://dashscope.aliyuncs.com/compatible-mode/v1`（中国内地） | `{"enable_thinking":false}` | 与当前 NL2SQL 摘要和日期解析使用的关闭配置一致。该模型仍跑默认、开启和关闭三组，作为已有路径的回归。开启组用 `enable_thinking: true`。 [兼容接口参数](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions) · [NL2SQL 模型配置](../src/configs/config.yaml) |
| G54 | 智谱国内 Coding Plan，`glm-5.4` | `{"thinking":{"type":"disabled"}}` | 套餐文档将关闭请求映射为 `low`。若实测仍有轻度思考，严格关闭判不通过。 [思考档位映射](https://docs.bigmodel.cn/cn/coding-plan/latest-model) |
| G53 | 智谱国内 Coding Plan，`glm-5.3` | `{"thinking":{"type":"disabled"}}` | 先确认当前套餐能用这个模型 ID。模型文档说明无法关闭，按套餐端点实测记录关闭请求是被拒绝、降档还是有其他表现。 [模型说明](https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3) |
| G53F | 智谱国内 Coding Plan，`glm-5.3-flash` | `{"thinking":{"type":"disabled"}}` | 模型文档只列出思考开启，关闭请求按套餐端点实测。模型 ID 与 `glm-5.3-flashx` 区分。 [Flash 模型说明](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash) · [套餐接入示例](https://docs.bigmodel.cn/cn/coding-plan/latest-model) |
| K31-C | 国内 Kimi Code，`k3.1-pro` | 按官方 Chat Completions 端点发送关闭参数，优先核对 `reasoning_effort: "none"` | 文档明确该模型只能开启思考，关闭请求预期返回 400。账户缺少对应权限时记录阻塞。 [端点与模型 ID](https://www.kimi.com/code/docs/) · [关闭行为](https://www.kimi.com/code/docs/kimi-code/models.html) |
| DSF | DeepSeek 官方 API，`deepseek-flash`，`https://api.deepseek.com` | `{"thinking":{"type":"disabled"}}` | 文档允许关闭并支持非思考模式工具调用，优先实测。对照组显式开启思考。 [思考模式](https://api-docs.deepseek.com/guides/thinking_mode/) · [工具调用](https://api-docs.deepseek.com/guides/tool_calls/) |

每个用例直接请求该模型对应的官方端点。遇到套餐、地域或模型权限限制时记录阻塞。

### 1.1 常用参数的官方支持范围

下表针对 Chat Completions。支持某个字段、支持某个参数值、以及参数会实际改变结果，是三项不同的检查。未明确确认的组合在目标端点实测。

| 模型或端点 | 采样与输出上限 | 流式、工具调用和 JSON |
| --- | --- | --- |
| Q4M | 百炼文档说明 `max_tokens` 与 `max_completion_tokens` 均限制思考和正文的总输出。用默认采样值起测，再单独测角色会传入的 `temperature` 和 `top_p`。 | 模型页列出工具调用和结构化输出。分别验收 `tool_choice: auto`、流式工具参数和日期解析的 `json_object`。开思考时按实际端点核对 `preserve_thinking` 与推理字段回传。 [模型能力](https://help.aliyun.com/zh/model-studio/qwen4-max) · [兼容接口参数](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions) |
| Q38F | 按当前 profile 复测 `temperature: 0` 和 `max_tokens: 8192`。逐项验证 `top_p`、输出上限字段和实际截断行为。 | 验收当前摘要与日期解析的 JSON 设置，并跑主 Agent 的多轮工具用例。开思考时单独核对 `preserve_thinking` 与推理字段回传。 [兼容接口参数](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions) |
| G54 | Z.ai 模型页标明最大输出 128K。通用 Chat Completions 文档列出 `temperature`、`top_p` 和 `max_tokens`，在 GLM-5.4 实际使用的 Coding Plan 端点核对字段名、取值和效果。 | 模型页列出流式、函数调用和 JSON 输出。用 `tool_choice: auto` 起测，并在实际端点独立验证 `response_format: {type: json_object}`、工具参数流和结束原因。 [模型能力](https://docs.z.ai/guides/llm/glm-5.4) · [Chat Completions 参数](https://docs.z.ai/api-reference/llm/chat-completion) |
| G53 | Z.ai 通用接口给出 `temperature` 0–1、`top_p` 0.01–1、`max_tokens` 1–131072。`do_sample: false` 会让采样参数失效。 | 支持 `stream`、`tools`、`tool_stream`，工具选择先用 `auto`。接口文档列出 JSON 模式 `json_object`。分别用日期解析的 JSON 和主 Agent 的工具测试验收。 [Chat Completions 参数](https://docs.z.ai/api-reference/llm/chat-completion) |
| G53F | Flash 文档说明文本参数沿用 GLM-5.3，并推荐 `temperature: 1`、`top_p: 0.95`。先用服务默认值，再单独试项目的 `temperature: 0`。 | 模型页列出工具调用和 JSON 输出，并建议流式调用组合 `stream: true` 与 `tool_stream: true`。通用接口对 `response_format` 的说明限定文本模型，因此对 Flash 的文本请求单独实测 JSON 字段。 [Flash 接口说明](https://docs.z.ai/guides/vlm/glm-5.3-flash) · [Chat Completions 参数](https://docs.z.ai/api-reference/llm/chat-completion) |
| K31-C | Kimi Code 的 CLI 文档列出 `temperature`、`top_p` 和 `max_completion_tokens` 设置。CLI 能配置这些值，仍需用 K3.1 Pro 自己的 Chat Completions 请求逐项核对端点行为。 | 官方列出 Chat Completions，工具调用消息在思考开启时必须附带 `reasoning_content`。针对这个模型和端点实测流式工具参数、强制工具选择和 `json_object`。 [Kimi Code 请求设置](https://www.kimi.com/code/docs/en/kimi-code-cli/configuration/env-vars.html) · [工具回传错误说明](https://www.kimi.com/code/docs/en/kimi-code/error-reference.html) |
| DSF | 官方 Chat Completions 支持 `max_tokens` 和非思考模式的 `temperature`（0–2）。思考开启时温度无效。`top_p` 在非思考时固定为 1，思考时有效值为 0.95–1。`presence_penalty` 和 `frequency_penalty` 已弃用，发送也无效。 | 支持 SSE、函数工具、`json_object` 和 `stop`。思考模式用 `tool_choice: auto` 验收，强制或指定工具会报错。JSON 输出还要写清 JSON 要求并校验非空正文。 [Chat Completions 参数](https://api-docs.deepseek.com/api/create-chat-completion/) · [JSON 输出说明](https://api-docs.deepseek.com/guides/json_mode/) |

`timeout` 和 `max_retries` 属于项目或 SDK 客户端设置，另核对实际请求时限和尝试次数。对 `stop`、`parallel_tool_calls`、强制工具选择、严格 JSON Schema 等可选能力，仅在使用它们的调用点验收，按目标端点逐项加入。记录每个参数的实际请求字段、值及测试状态，区分“接受且生效”“接受但忽略或固定”“拒绝”和“未验证”。

### 1.2 流式开关与供应商差异

Chat Completions 的请求体用 `stream: true` 请求 SSE 流，用 `stream: false` 请求一次性 JSON。测试中把 `stream` 与思考开关分别控制。在端点允许的同一种思考模式下，分别测 `stream: true`、`stream: false` 和省略 `stream`。判断流式是否实际生效要看 HTTP 响应格式和事件，不能只看调用端是否拿到了迭代器。

| 模型或端点 | 官方文档确认及待测项 |
| --- | --- |
| Q4M、Q38F | 两个千问模型都走百炼兼容接口。分别实测思考允许模式与 `stream` 开启、关闭、省略的组合。开启流式时核对 `tool_stream`、SSE 中推理、正文、工具参数及用量字段。 [百炼兼容接口](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions) |
| G54 | 模型页列出流式输出支持。用实际 Coding Plan 端点验证 `stream: true`、`false` 和省略时的行为，另外验证 `tool_stream`。 [GLM-5.4 模型页](https://docs.z.ai/guides/llm/glm-5.4) |
| G53、G53F | Z.ai Chat Completions 明确列出 `stream`，默认 `false`，开启时按事件分片。Flash 文档建议流式工具请求用 `stream: true` 加 `tool_stream: true`，并记录后者省略或为 `false` 时工具参数是否仍能收到。 [Chat Completions 参数](https://docs.z.ai/api-reference/llm/chat-completion) · [GLM-5.3-Flash](https://docs.z.ai/guides/vlm/glm-5.3-flash) |
| K31-C | 官方列出 Chat Completions 端点。保留思考开启，分别实测 `stream: true`、`false` 和默认行为。根据实测记录工具参数是否分片、终止事件和用量所在位置。 [Kimi Code 端点及模型 ID](https://www.kimi.com/code/docs/en/) |
| DSF | 官方明确支持 `stream: true` 和 `false`，默认 `false`。`stream_options.include_usage` 单独测试，仅在 `stream: true` 时发送。用量可随最后一个 `choices` 非空的结束事件返回。 [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/) |

`tool_stream` 控制特定提供商的工具参数是否按片返回。`stream_options.include_usage` 控制流式请求的用量字段。这两个字段和 `stream` 分开验收。当前 NL2SQL 工厂支持 YAML 的 `stream`，将其映射为 `ChatOpenAI(streaming=...)`。工厂白名单还没有 `tool_stream` 或 `stream_options`。需要这些字段时，先用直接客户端验证，再登记是否需要工厂补适配。 [工厂参数映射](../src/services/llm_factory.py)

用项目锁定的版本做了离线 HTTP 模拟，验证以下客户端行为：

| 工厂中 `stream` 的值 | 调用方式 | HTTP 模拟结果 |
| --- | --- | --- |
| 省略 | `invoke()` 或 `stream()` | 无流式回调时，前者发送 `stream: false`，后者发送 `stream: true`。 |
| `false` | `invoke()` 或 `stream()` | 两种写法都发送 `stream: false`。调用 `stream()` 时仍可迭代完整响应。 |
| `true` | `invoke()` 或 `stream()` | 都发送 `stream: true`。`invoke()` 返回拼接后的完整消息，`stream()` 产出分片。 |
| `true` | `invoke(..., stream=False)` | 单次调用发送 `stream: false`。 |

Agent 模型请求是否流式，与 NL2SQL Web 是否逐字交付给前端分别核对。

### 1.3 中文官方文档入口

测试前先用模型页核对 `model` ID、能力和账户权限，再用 API 参考核对实际请求字段。查思考开关、流式工具和消息回传时，使用相同提供商和套餐对应的文档。

| 编号 / 模型 | 中文模型或平台文档 | 中文 API 参考与功能说明 |
| --- | --- | --- |
| Q4M / `qwen4-max` | [Qwen4-Max 正式版](https://help.aliyun.com/zh/model-studio/qwen4-max) | [百炼 Chat Completions](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions) · [深度思考与回传](https://help.aliyun.com/zh/model-studio/deep-thinking) |
| Q38F / `qwen3.8-flash` | [Qwen3.8-Flash 模型页](https://help.aliyun.com/zh/model-studio/qwen3-8-flash) | [百炼 Chat Completions](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions) · [深度思考与回传](https://help.aliyun.com/zh/model-studio/deep-thinking) |
| G54 / `glm-5.4` | [智谱 GLM-5.4 模型页](https://docs.bigmodel.cn/cn/guide/models/glm-5.4) | [国内 Coding Plan 接入及档位映射](https://docs.bigmodel.cn/cn/coding-plan/latest-model) · [对话补全参数](https://docs.bigmodel.cn/api-reference/%E6%A8%A1%E5%9E%8B-api/%E5%AF%B9%E8%AF%9D%E8%A1%A5%E5%85%A8) |
| G53 / `glm-5.3` | [智谱 GLM-5.3 模型页](https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3) | [对话补全参数](https://docs.bigmodel.cn/api-reference/%E6%A8%A1%E5%9E%8B-api/%E5%AF%B9%E8%AF%9D%E8%A1%A5%E5%85%A8) · [思考模式与工具回传](https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode) |
| G53F / `glm-5.3-flash` | [智谱 GLM-5.3-Flash 模型页](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash) | [对话补全参数](https://docs.bigmodel.cn/api-reference/%E6%A8%A1%E5%9E%8B-api/%E5%AF%B9%E8%AF%9D%E8%A1%A5%E5%85%A8) · [思考模式与工具回传](https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode) |
| K31-C / `k3.1-pro` | [Kimi Code 模型配置](https://www.kimi.com/code/docs/kimi-code/models.html) | [Kimi Code API 接入](https://www.kimi.com/code/docs/) · [关思考和工具回传错误](https://www.kimi.com/code/docs/kimi-code/error-reference.html) |
| DSF / `deepseek-flash` | [DeepSeek 模型列表](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/) | [对话补全](https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/) · [思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/) · [工具调用](https://api-docs.deepseek.com/zh-cn/guides/tool_calls/) · [JSON 输出](https://api-docs.deepseek.com/zh-cn/guides/json_mode/) |

### 1.4 国内编程套餐的请求地址

查阅这两家的 API 文档时，以国内套餐的 **OpenAI Chat Completions** 接入说明为准。把下表的 Base URL 传给 `ChatOpenAI(base_url=..., use_responses_api=False)`，由客户端拼接 `/chat/completions`。

| 套餐 | `ChatOpenAI` 的 `base_url` | 实际 `POST` URI | 官方接入说明 |
| --- | --- | --- | --- |
| 智谱国内 GLM Coding Plan（G54、G53、G53F） | `https://open.bigmodel.cn/api/coding/paas/v4` | `https://open.bigmodel.cn/api/coding/paas/v4/chat/completions` | [智谱 Coding Plan 模型切换与 OpenAI 兼容配置](https://docs.bigmodel.cn/cn/coding-plan/latest-model) |
| 国内 Kimi Code（K31-C） | `https://api.kimi.com/coding/v1` | `https://api.kimi.com/coding/v1/chat/completions` | [Kimi Code API 接入与服务地址](https://www.kimi.com/code/docs/#服务地址) |

出现鉴权失败、套餐限制或目标模型不可用时，记录实际 URI、模型 ID 和错误码，标记为阻塞。本轮仅以表中套餐端点的实测结果验收，普通开放平台、海外端点和其他协议的结果都在本轮范围之外。

## 2. 测试环境与取证

- [ ] 使用 NL2SQL 的项目虚拟环境，记下实际安装的 Python、`langchain-openai`、`openai`、`langchain-core`、`deepagents` 版本。当前仓库锁定 `langchain-openai==1.7.4`、`openai==3.46.0` 和 `deepagents==0.7.34`。 [依赖定义](../pyproject.toml)
- [ ] 直接构造 `ChatOpenAI(model=..., base_url=..., api_key=..., use_responses_api=False, max_retries=0)`，按下文读取该供应商的密钥。核对真实请求为 `POST {base_url}/chat/completions`。首轮只传必要字段，后续再逐项加业务参数，便于定位哪一项导致失败。
- [ ] 对于 `thinking`、`enable_thinking` 等扩展字段，通过 `extra_body` 传递。在发送前检查它们确实出现在最终请求体顶层。对照组只改思考设置。需要设置推理档位时核对 `reasoning_effort` 在实际请求中的值。
- [ ] 在 HTTP 层统计 **原始响应**中的 `message.reasoning_content`、流式 `delta.reasoning_content`、其他推理块和供应商提供的推理 token 数。记录字段是否存在、是否有非空内容和 token 计数。普通 `ChatOpenAI` 会过滤第三方的扩展响应字段，不能用它返回的 `AIMessage` 缺少字段来证明关闭成功。流式观测须包括结束块和使用量块。 [ChatOpenAI 兼容说明](https://docs.langchain.com/oss/python/integrations/chat#chat-completions-api)
- [ ] 记录请求模型、响应所报模型、HTTP 状态及错误码、请求 ID、耗时、结束原因、token 用量和已发送的模型参数。报告仅保留脱敏元数据。确认目标模型身份，出现别名或服务侧换模时先核验映射再判断。

### API 密钥来源

从仓库根目录看，密钥文件路径是 `./nl2sql/.env`。NL2SQL 的 `ConfigLoader` 按模块路径定位该文件，加载后再解析 YAML 中的环境变量引用。独立测试脚本直接构造 `ChatOpenAI` 时，应显式读取该文件或使用测试环境预先注入的对应变量，并将读到的值传给 `api_key`。 [配置加载代码](../src/services/config_loader.py)

| 供应商与测试模型 | 测试读取的环境变量 | 编写本清单时本地 `./nl2sql/.env` 的核对结果（实测当时的取值见测试报告 §1.2） |
| --- | --- | --- |
| 阿里云百炼 `qwen4-max`、`qwen3.8-flash` | `DASHSCOPE_API_KEY` | 已有非空值 |
| DeepSeek `deepseek-flash` | `DEEPSEEK_API_KEY` | 已有非空值 |
| 智谱 `glm-5.4`、`glm-5.3`、`glm-5.3-flash` | `GLM_API_KEY`（国内 GLM Coding Plan 账号的密钥） | 已预留空项，待填写 |
| Kimi Code `k3.1-pro` | `KIMI_CODE_API_KEY`（国内 Kimi Code 控制台的密钥） | 已预留空项，待填写 |

执行前重新核对变量。变量缺失或为空时，将该端点标为「阻塞：缺少密钥」。模型访问权限以该供应商端点的实际请求结果为准。日志、请求取证和测试报告只记录变量名与状态，须遮盖 `Authorization` 请求头及密钥值。

### 关闭思考的判定

使用固定的简单文本题和一道可重复的多步推理题，例如计算 `(37 × 19 − 48) ÷ 5`，答案为 `131`。按相同提示词运行默认、显式开启和显式关闭三组。显式关闭的关键用例至少用三个独立会话重复。

| 结果 | 判定 |
| --- | --- |
| 通过 | 端点文档支持关闭，客户请求中的关闭参数有效。显式关闭组返回正常正文，HTTP 原始响应的推理字段或计数与关闭一致。开启组可用同样的观测方式检出推理。 |
| 不通过 | 服务明确拒绝关闭、将关闭映射为轻度思考、返回非空推理内容或非零推理 token，或为满足关闭请求改用其他模型。 |
| 无法判定 | HTTP 成功但缺少可区分开关的服务证据，例如开启和关闭都没有可观察的推理数据，或响应模型身份不清楚。 |
| 阻塞 | 账户权限、额度、区域、请求路由或网络导致用例未执行到目标模型。记录原因。 |

### 输出格式与 `<think>` 标签位置

对每个端点的默认、开启、关闭三组，分别记录原始 HTTP 响应和 LangChain 消息解析后的字段位置。使用未要求模型输出标签的提示词。表中的 `R`、`A` 分别是离线用的推理和正式答案标记。真实接口可能只出现其中一种格式。未观察到的格式用模拟响应验证解析规则，结果标为离线覆盖。

| 响应形态 | 检查内容 |
| --- | --- |
| 推理是独立字段 | 普通响应检查 `choices[].message.reasoning_content` 与 `message.content`，流式检查 `choices[].delta.reasoning_content` 与 `delta.content`。分别确认哪个字段是推理、哪个是正文，以及 `ChatOpenAI` 在解析时保留了什么。 |
| 推理是内容块 | 记录块类型、位置以及与 `text` 块的顺序。最终正文只保留正确顺序的可见文本，推理块单独处理。 |
| 标签嵌在正文字符串 | 检查 `<think>R</think>A`、`A1<think>R</think>A2` 和 `A<think>R</think>`。记录标签在正文前、中、后及是否成对。清理时保留标签外的全部文本，包括标签前后的内容。 |
| 流式标签被切开或响应截断 | 用跨 SSE 分片的起止标签验证拼接后能正确分离推理和正文。未闭合标签或 `finish_reason: length` 单独记录为不完整输出，不能把推理残片作为正式答案交付。 |
| 只返回工具调用 | 这一轮正文为空或 `null` 可以正常。仍须取得完整工具名、参数和 ID。收到工具结果后，应在后续轮得到有效的最终提交。 |

如果关闭组出现非空推理字段、推理块或标签内推理，按 T02 判为关闭不通过。空字段、空标签和计数缺失分别记录，按 §2 的判定表综合判断。日期解析用例检查最终参与解析的正文能直接通过 JSON 和字段校验。推理标签、推理文本或 JSON 之外的内容进入该正文时，记录格式失败。

## 3. 测试用例与放行条件

按顺序执行。**用 Q38F 建立已有路径基线。Q4M 和 DSF 接着跑完整的关闭思考验收。** G54、G53、G53F 和 K31-C 先跑关闭对照与开启基线。确认端点不支持关闭后，无需继续跑该端点的关闭模式角色用例。若要考虑允许思考的用途，另开结果行验收。

| 优先级 / 编号 | 用例 | 通过标准 |
| --- | --- | --- |
| P0 / T01 | 直接 `ChatOpenAI.invoke()` 单轮文本 | 请求到达正确端点并由目标模型返回非空且正确的文本。区分 401／403 等鉴权、权限错误和模型的参数错误。 |
| P0 / T02 | 默认、开启、关闭三组对照 | 按 §2 的证据判定关闭结果，同时保留每个模式的文本可用性。GLM 和 Kimi 按各自套餐的实测结果区分关闭、降档和拒绝。 |
| P0 / T03 | 流式文本和多轮工具往返 | 分别跑普通和流式请求。用 `tool_choice: auto` 先让模型调用合成工具 `lookup_month` 得到 `2026-09`，再用这个结果调用 `count_orders` 得到 `1234`，最后调用 `submit_final_answer` 提交包含 `1234` 的答案。核对每轮 `ToolMessage.tool_call_id`、后续 HTTP 请求里的消息顺序以及工具结果是否被实际使用。思考关闭时还须通过 T02。思考开启时核对下一轮 HTTP 请求含端点要求的原始推理字段。 |
| P0 / T04 | 复测业务参数与工厂装配 | 按 §1.1 逐项加入参数。用短输出上限和要求长正文的提示验证输出截断，核对 `max_tokens` 与 `max_completion_tokens`。检查现有主模型覆盖 `temperature: 0`、摘要默认参数与日期解析的 JSON 请求能否适用于每个候选。切到工厂后再次检查最终 HTTP 请求。 |
| P0 / T05 | 主 Agent | 通过当前 Agent 装配，用合成工具跑完查询及提交，再用测试数据完成一条完整的 NL2SQL 问数。核对项目重试与 SDK 重试次数、单次请求超时、SQL 结果和 Qwen embedding 检索。当前主 Agent 的单次请求超时为 120 秒。 |
| P1 / T06 | 摘要 | 让候选模型作为独立摘要器，在当前 96,000 token 触发阈值附近触发摘要，保留最近 24,000 token 的行为按当前配置核对。摘要保留预置的关键事实，后续问答能正确使用，并在 120 秒内完成。 |
| P1 / T07 | 日期解析 | 通过实际 `TimeResolver`，带 `response_format: {type: json_object}`。固定参考时间为 `2026-11-15T14:00:00`、时区 `Asia/Shanghai`，输入“前天”须解析为 `[2026-11-13, 2026-11-14)`。输入“最近一段时间”应要求澄清。响应通过现有结构校验，单次模型请求按当前 15 秒超时验证。 |
| P0 / T08 | 思考模式的保存、回传与隔离 | 按 §4 检查联调、工具后恢复、下一用户轮、摘要、历史文件与模型切换。保留当前端点要求的协议字段和工具对应关系，通用历史中无推理标记。 |
| P0 / T09 | 输出格式和 `<think>` 标签位置 | 按 §2 的格式表记录响应中的推理字段、内容块和标签位置。用普通及流式响应核对最终正文完整且无推理内容，工具参数及 ID 完整。需要 JSON 的角色用 T07 验证最终正文。 |
| P0 / T10 | 流式开关及工具参数分片 | 按 §1.2 在直调和项目装配分别跑开启、关闭、省略三组。开启时确认真实 SSE、可组装的正文与工具参数、结束事件和可用的用量字段。关闭时确认 JSON 整包。单测中模拟中途断流，验证残缺工具参数不能触发执行。 |

**现有用例可复用。** [模型工具往返测试](../src/tests/db_deepagent/integration/test_model_capability.py) 已有 `count_orders` 和 `submit_final_answer`，可以复用其业务断言。它目前只检查 `enable_thinking` 和 `AIMessage.additional_kwargs`，按 §2 补 HTTP 层证据之后才能用于本清单的关闭判定。摘要可复用 [长窗口集成用例](../src/tests/db_deepagent/integration/test_model_summary_window.py)。日期解析的格式及异常用例见 [日期解析测试](../src/tests/db_deepagent/unit/test_time_resolver_llm.py) 和 [当前提示词](../src/modules/db_deepagent/config/time_resolution_prompts.yaml)。

**工厂和接口的结果分开记录。** 当前工厂对 `thinking` 有 provider 限制，也没有把 `reasoning_effort` 纳入参数白名单。Q4M 先用 `ChatOpenAI(reasoning_effort="none", ...)` 直测，再测工厂能否表达同一请求。直接 `ChatOpenAI` 能调用且当前工厂拦截时，标记为“待工厂适配”。直接测试失败时，再用相同有效参数做原始 Chat Completions 对照，区分是客户端改写字段还是服务端限制。当前 `ChatOpenAI` 版本会把其 `max_tokens` 构造参数序列化为 `max_completion_tokens`，这项差异以实际请求为准。 [LLM 工厂](../src/services/llm_factory.py) · [Agent 装配](../src/modules/db_deepagent/agent.py) · [角色配置](../src/modules/db_deepagent/config/db_deepagent.yaml)

## 4. 思考模式的 checkpoint 和上下文隔离

### 4.1 当前行为

生产默认使用 PostgreSQL 的 `PostgresSaver`，Agent 状态包含消息。普通 `ChatOpenAI` 丢弃来自第三方的独立 `reasoning_content` 字段，所以该字段当前通常到不了 checkpoint。但如果模型把推理混在正文里，或专用适配器将字段保存在 `AIMessage` 的正文块或 `additional_kwargs`，这些数据会随消息保存。后续请求是否回传某个字段由消息适配器决定。写入 checkpoint 和进入后续模型上下文需分别核对。 [持久化构造](../src/modules/db_deepagent/persistence/checkpoint.py) · [Agent 装配](../src/modules/db_deepagent/agent.py)

用项目锁定的依赖做了内存图和序列化离线探针。三种合成推理标记都随消息序列化保留。现有摘要和历史转存使用的 XML 格式会包含正文里的 `reasoning` 块或 `<think>` 文本，独立放在 `additional_kwargs` 的 `reasoning_content` 不进入这份 XML。能否隔离推理与适配器返回的消息形态有关。

| 推理存放位置 | checkpoint 序列化 | 当前摘要及历史转存所用的 XML 格式 |
| --- | --- | --- |
| `AIMessage.additional_kwargs["reasoning_content"]` | 保留 | 不包含 |
| `AIMessage.content` 中的 `reasoning` 块 | 保留 | 包含 |
| `AIMessage.content` 字符串中的 `<think>…</think>` | 保留 | 包含 |

### 4.2 建议的处理边界

1. **保存可复用的业务历史。** 写入通用 checkpoint 消息之前，将供应商推理字段和已识别的推理块从可见正文投影中分离，保留用户问题、最终文本、工具调用、工具结果、消息 ID 和证据。摘要输入、历史文件、线程交接、前端和日志使用这份业务历史。判断和剥离推理使用协议字段、块类型以及已确认的服务端正文格式，避免误删用户或工具输出里的普通文本。 [当前线程交接](../src/modules/db_deepagent/runner.py) · [模型日志格式](../src/modules/db_deepagent/middleware/model_observer.py)
2. **在当前供应商的工具往返中保留协议数据。** 适配器在收到模型响应时暂存原始推理字段，按会话、线程、当前轮、端点、模型和消息 ID 绑定。构造发往同一端点的后续请求时，只按该端点的要求回传，并维持顺序及工具调用 ID。GLM 在允许时显式验证 `thinking.clear_thinking: true`，用来减少上一用户轮的推理进入下一轮。它不会关闭当前轮思考，也不能代替本轮工具往返所需的字段。Kimi Code 明确要求思考模式下的 assistant 工具调用消息带 `reasoning_content`，不能把这些消息的推理删掉后按原样重发。 [Z.ai 参数说明](https://docs.z.ai/api-reference/llm/chat-completion) · [Z.ai 交错思考](https://docs.z.ai/guides/capabilities/thinking-mode) · [Kimi Code 错误说明](https://www.kimi.com/code/docs/en/kimi-code/error-reference.html)
3. **把是否跨进程恢复作为启用条件。** 只要求在一次问答执行中继续工具调用时，协议数据保存在本次运行的内存中。若要支持进程重启后从工具阶段续跑，使用受权限保护、加密且有明确保留期限的独立协议存储，checkpoint 只存引用。恢复时核对范围和顺序。若端点还要求回传先前工具轮的推理，须先验证它接受只含已完成业务摘要的历史，再删除相应协议数据。恢复时缺少必需数据应给出明确失败状态，同一工具调用的已执行结果沿用现有记录。切换端点或模型时，从业务历史或线程交接启动新的协议上下文。

近期可先让通过关闭思考验收的模型承担主 Agent。始终思考的模型可独立验收摘要和日期解析，读取最终正文并核对 JSON 或摘要耗时。将这类模型启用为主 Agent 前，完成 T08 的同轮回传、上下文隔离和所承诺的恢复能力。

### 4.3 T08 验收清单

- [ ] 用唯一合成标记覆盖独立字段、结构化推理块和服务端确认的内嵌推理格式。检查通用 checkpoint、摘要请求、转存文件、线程交接、Web 输出与 DEBUG 日志无标记，且同一端点的必需工具后续 HTTP 请求包含精确的协议数据。
- [ ] 完成第一轮工具调用后再发起第二条用户问题，检查上一轮推理是否按该端点约定被省略或保留。对 Q4M、Q38F 比较 `preserve_thinking: false` 与默认值，对 GLM 比较 `clear_thinking: true` 与默认值，对 Kimi Code 验证历史中工具消息及其推理字段仍符合协议。
- [ ] 分别验证流式、普通调用和实际支持的断点恢复路径。恢复时校验引用及工具结果，数据缺失时用例应报告明确的恢复失败。压缩历史后检查助理消息和 `ToolMessage` 仍正确成对。
- [ ] 用不同 provider 或模型续接业务对话，核对新端点只收到它可用的业务历史或摘要。

## 5. 实测记录

结果只适用于所测模型 ID、端点、客户端版本、思考模式和请求参数组合。对同一模型的不同区域、套餐、思考模式分别填行。在证据列附 T09 的普通或流式结果、推理字段或标签位置、正文是否完整，以及 T03 的工具调用顺序和下一轮回传结论。

| 编号 | 直调 ChatOpenAI | 关思考 | 流式开 / 关 / 默认 | 工厂装配 | 主 Agent | 摘要 | 日期解析 | 思考隔离与恢复 | 证据或错误 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Q4M | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | |
| Q38F | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | |
| G54 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | |
| G53 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | |
| G53F | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | |
| K31-C | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | |
| DSF | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | 待测 | |

- [ ] 严格关闭思考的角色，仅登记 T02、T09 及对应角色用例都通过的组合。
- [ ] 有流式要求的角色，还需 T10 的相应开关组合通过，并标明是否支持工具参数流式和用量统计。
- [ ] 必须思考的模型若通过某个角色的测试，标记为“思考开启可用”。工具往返出现缺字段、400 或回错模型时，记录实际请求差异和要补的适配。
- [ ] 思考开启时，主 Agent 的可用结论还需要 T03、T08 和 T09 通过。报告列明已验证的恢复方式。
- [ ] 汇总时单列“端点可用”“ChatOpenAI 可用”“NL2SQL 角色可用”三个结论，并注明依赖版本和实测日期。
