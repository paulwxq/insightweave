# NL2SQL 模型配置与 LLM 适配改造方案

> 状态：代码实施与离线验证已完成；§5.3 真实联调、生产启用及联合发布后的容器验收待完成。

`config.yaml` 登记提供商连接、模型及其默认参数。主 Agent、摘要和日期解析在各自的配置段用 `provider + llm_name` 选择聊天模型，按需用 `params` 覆盖模型默认值。旧链路五个节点迁移到同一配置结构，沿用已在用的 Qwen 模型和参数。Qwen embedding 继续由自己的配置选择。

新链路的 Qwen、DeepSeek、GLM 和 Kimi 共用 `langchain_openai.ChatOpenAI`，按模型映射实际需要的参数。GLM 和 Kimi 默认省略思考参数。各角色沿用现有 Qwen 默认配置，可在自己的业务段选择其他模型。 [兼容性测试报告](reviews/2026-11-25_Doc24_ChatOpenAI兼容性测试报告.md)

## 1. 当前配置与调用链

NL2SQL 加载自己模块内的 `src/configs/config.yaml`。`llm_providers` 定义连接，`llm_profiles` 用另一个名称把 provider、实际模型 ID 和默认参数组合起来，节点用 `llm_profile` 引用这组配置。工厂当前已经支持参数浅合并，节点传入的值优先。配置结构的改动是把模型默认值放到对应 provider 下，并让调用点用 provider 和模型名直接定位。 [配置加载器](../src/services/config_loader.py) · [全局配置](../src/configs/config.yaml) · [LLM 工厂](../src/services/llm_factory.py)

| 调用点 | 当前选择 | 当前用途 |
| --- | --- | --- |
| `model` | `qwen3_8_max` | 主 Agent 绑定工具并处理多轮消息 |
| `summarization` | `qwen3_8_flash` | 上下文摘要，单独构建模型 |
| `time_resolution` | `qwen3_8_flash` | `resolve_time_range` 使用的日期解析服务，要求 JSON 对象 |
| 旧链路 `router`、`planner`、`summarizer` | `qwen_turbo`、`qwen_plus` | 分流、拆解任务、总结结果 |
| 旧链路 `question_parsing`、`sql_generation` | `qwen_plus` | 问题解析、SQL 生成 |

当前三个 `db_deepagent` 聊天模型走 `dashscope_openai` 对应的 `ChatOpenAI`。旧链路的 `qwen-turbo` 和 `qwen-plus` 走 `dashscope` 对应的 `ChatTongyi`。工具通过自身服务调用模型时才需要模型配置。例如日期解析工具使用 `time_resolution`，`execute_sql` 直接运行程序逻辑。 [Agent 配置](../src/modules/db_deepagent/config/db_deepagent.yaml) · [日期解析工具](../src/modules/db_deepagent/tools/guard_tools.py) · [旧父图配置](../src/modules/nl2sql_father/config/nl2sql_father_graph.yaml) · [旧子图配置](../src/modules/sql_generation/config/sql_generation_subgraph.yaml)

## 2. 目标配置

### 2.1 提供商连接及模型默认参数

`config.yaml` 中的 `llm_providers.<provider>.models.<llm_name>` 定位一个聊天模型。`provider` 是连接名，`llm_name` 使用该接口实际接受的模型 ID。同一提供商可以配置多个模型，同名模型也可以出现在不同提供商下。`adapter` 由注册表指定，使连接名称和协议选择可以分别调整。下文的 YAML 为拟议配置节选。

```yaml
llm_providers:
  dashscope:
    adapter: dashscope_native
    api_key: ${DASHSCOPE_API_KEY}
    models:
      qwen-turbo: {}
      qwen-plus: {}
      qwen-max: {}

  dashscope_openai:
    adapter: openai_chat_completions
    api_key: ${DASHSCOPE_API_KEY}
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    models:
      qwen3.8-max:
        params:
          enable_thinking: false
      qwen3.8-flash:
        params:
          temperature: 0
          max_tokens: 8192
          enable_thinking: false
      qwen3.5-plus:
        params:
          enable_thinking: false
      qwen4-max: {}

  deepseek:
    adapter: openai_chat_completions
    api_key: ${DEEPSEEK_API_KEY}
    base_url: ${DEEPSEEK_BASE_URI:https://api.deepseek.com}
    models:
      deepseek-flash: {}
      deepseek-chat:
        params:
          temperature: 0
          max_tokens: 8192
          timeout: 60
      deepseek-reasoner:
        params:
          max_tokens: 32768

  glm_coding:  # 国内编程套餐技术验证通过，生产使用待确认
    adapter: openai_chat_completions
    api_key: ${GLM_API_KEY}
    base_url: https://open.bigmodel.cn/api/coding/paas/v4
    models:
      glm-5.4: {}
      glm-5.3: {}
      glm-5.3-flash: {}

  kimi_code:  # 国内编程套餐技术验证通过，生产使用待确认
    adapter: openai_chat_completions
    api_key: ${KIMI_CODE_API_KEY}
    base_url: https://api.kimi.com/coding/v1
    models:
      k3.1-pro: {}

  minimax:
    adapter: openai_chat_completions
    api_key: ${MINIMAX_API_KEY}
    base_url: https://api.minimaxi.com/v1
    models:
      MiniMax-M2.1:
        params: {temperature: 1.0}
      MiniMax-M2.5:
        params: {temperature: 1.0}
```

`dashscope` 与 `dashscope_openai` 分别对应原生与兼容接口，即使它们引用同一个密钥环境变量。`models` 只登记项目要使用的模型。每个模型的 `params` 可省略，由适配器或服务端使用默认值。

历史 profile `qwen3_max` 对应的 `qwen3-max` 已下线。迁移时删除该注册项，保留上面仍在用的 `qwen3.8-max`。新配置若填写 `llm_name: qwen3-max`，按未知模型在构造客户端前报错。 [现有模型配置的历史说明](../src/configs/config.yaml)

`glm_coding` 和 `kimi_code` 是本文给国内编程套餐连接使用的配置名。密钥由配置加载器从 NL2SQL 模块的 `.env` 加载，从仓库根目录看路径为 `./nl2sql/.env`。`base_url` 填套餐的 Chat Completions 基础地址，`ChatOpenAI` 追加 `/chat/completions`。 [Doc 24 密钥与端点说明](24_五个模型ChatOpenAI兼容性测试清单.md)

**两个编程套餐端点的接口兼容性已验证，生产使用待确认。** 智谱将 Coding Plan 限于官方指定的工具和产品环境，现行公开名单未明确覆盖自建 NL2SQL。Kimi Code 的公开说明将订阅用于个人交互式场景，并指向开放平台处理产品集成。生产启用前，确认所用账号和端点明确允许实际业务场景，或改用允许该场景的正式业务端点，并按 §5.3 验证。通用适配器可先开发和用模拟传输测试。 [智谱适用工具](https://docs.bigmodel.cn/cn/coding-plan/tool/others) · [Kimi Code 适用范围](https://www.kimi.com/help/kimi-code/community-guidelines) · [Kimi 平台说明](https://www.kimi.com/help/kimi-code/membership-guide)

### 2.2 在现有业务段选择模型

`provider`、`llm_name` 和可选的 `params` 放在现有业务段内。主 Agent、摘要和日期解析可以填写不同的 provider，也可以使用同一模型的不同参数。业务段继续放各自的超时、重试和摘要窗口设置。

```yaml
model:
  provider: dashscope_openai
  llm_name: qwen3.8-max
  params:
    temperature: 0
  expected_harness_key: null
  request_timeout_seconds: 120
  retry:
    enabled: true
    max_retries: 2

summarization:
  provider: dashscope_openai
  llm_name: qwen3.8-flash
  request_timeout_seconds: 120
  summary_trigger_tokens: 96000
  recent_context_tokens: 24000
  trim_tokens_to_summarize: null

time_resolution:
  provider: dashscope_openai
  llm_name: qwen3.8-flash
  params:
    response_format: {type: json_object}
    max_retries: 2
  request_timeout_seconds: 15

embedding:
  profile: qwen3_7_text_embedding
  release_record: src/modules/db_deepagent/config/embedding_release.yaml
  request_timeout_seconds: 30
```

摘要沿用 `qwen3.8-flash` 的模型默认参数。日期解析也继承 `temperature: 0`、`max_tokens: 8192` 与 `enable_thinking: false`，再加上自己的 JSON 输出和客户端重试设置。主 Agent 的 `temperature: 0` 只影响主 Agent。

下面的组合仅用于演示跨供应商配置。只替换各业务段的模型字段和 `params`，其余业务字段沿用上例。GLM 和 Kimi 的空 `params` 继承各自注册项，原 Qwen 的关闭思考参数和 `temperature: 0` 不随模型切换带入。Kimi 的长窗口摘要实测曾耗时 112.4 秒，接近现有 120 秒超时。实际选用摘要模型时按 §5.3 复测长窗口。

```yaml
model:
  provider: glm_coding
  llm_name: glm-5.4
  params: {}

summarization:
  provider: kimi_code
  llm_name: k3.1-pro
  params: {}

time_resolution:
  provider: deepseek
  llm_name: deepseek-flash
  params:
    thinking: {type: disabled}
    response_format: {type: json_object}
    max_retries: 2
```

### 2.3 参数继承与校验

1. 工厂用 `provider + llm_name` 精确解析注册项。业务段必须同时提供这两个字段。引用不存在时报出业务段和配置路径。
2. 按参数键浅合并模型默认值与调用点 `params`。省略字段、`params` 或写 `params: {}` 时继承对应默认值。`0`、`false` 按显式值处理。`thinking`、`response_format` 等对象按整项替换。`params` 本身必须是映射，参数值能否为 `null` 由适配器按字段校验。
3. 工厂按具体接口与模型校验合并结果，并把参数映射到实际请求。两层都省略的字段由 SDK 或服务端决定。思考参数和采样参数只属于声明它们的模型或调用点。未知或已知不支持的参数应报出该参数的配置路径；业务显式值使用 `params.<key>`，注册表继承值使用完整的 `llm_providers.<provider>.models.<llm_name>.params.<key>`，角色包装器只增加 `model`、`summarization` 或 `time_resolution` 的中性角色前缀。密钥只在构造实际使用的客户端时校验，错误信息使用字段名。
4. `db_deepagent` 三个调用点以 `request_timeout_seconds` 为单次请求超时的来源。角色 YAML 的字面值必须是数字，布尔值和引号数字字符串在配置解析时拒绝，不能被 Pydantic 或 `ConfigLoader` 转换成数字后放行；单独的 `${VAR}` 引用允许在环境替换后按同一数字规则校验。调用点在 `params` 里再填 `timeout` 时报告冲突。注册表的 `timeout` 默认值只用于未提供业务超时的调用点，并由适配器判断支持情况。OpenAI 兼容调用必须从 `request_timeout_seconds`、业务 `params.timeout` 或注册表默认 `params.timeout` 获得请求超时；三者都缺失时在导入客户端类之前报错，不使用当前 LangChain 默认的无超时客户端。实际生效的值必须是有限且大于 0 的 `int` 或 `float`，布尔值、字符串、容器、`null`、`nan` 和 `inf` 均在导入客户端类之前拒绝。旧链路五个节点没有 `request_timeout_seconds` 字段，切换兼容接口时必须配置后两种来源之一。主 Agent 启用项目重试时，客户端重试继续设为 `0`，调用点若显式要求非零值则报错。日期解析的客户端重试和主 Agent 的项目重试各自生效。
5. 日期解析在装配时检查合并后的输出设置能否满足 JSON 对象约定，运行时继续校验返回内容。摘要模型切换后，验收时应核对 `summary_trigger_tokens`、保留窗口、实际输入上限及超时。

## 3. 开发改动清单

实现 `provider + llm_name` 需要同时改 YAML 和 Python。现有工厂可以通过 `base_url` 创建兼容客户端，入口仍然要求 `llm_profile`，参数也依赖当前的 provider 名称判断。此处变更集中在注册表、工厂和模型调用点，沿用项目锁定的 LangChain、DeepAgents 与 DashScope 依赖。

### 3.1 配置和环境变量

| 文件 | 开发时修改 |
| --- | --- |
| [模型注册表 `src/configs/config.yaml`](../src/configs/config.yaml) | 把现有 `llm_profiles` 中仍可用的注册项迁入 §2.1 的 `llm_providers.<provider>.models.<llm_name>`，沿用这些模型的默认参数。删除已下线的 `qwen3_max` 注册项，登记新模型。若启用 OpenAI 或 OpenRouter 样例，按同一结构填写 `adapter` 和明确的 `base_url`。 |
| [新链路 `db_deepagent.yaml`](../src/modules/db_deepagent/config/db_deepagent.yaml) | 在 `model`、`summarization`、`time_resolution` 段用 §2.2 的默认配置替换 `llm_profile` 和 `overrides`。业务超时、项目重试、摘要窗口和日期 JSON 参数按上例迁移。按 §4.3 为角色换模型时，同时核对本段参数。 |
| [旧父图 `nl2sql_father_graph.yaml`](../src/modules/nl2sql_father/config/nl2sql_father_graph.yaml) 和 [旧子图 `sql_generation_subgraph.yaml`](../src/modules/sql_generation/config/sql_generation_subgraph.yaml) | 按 §3.3 的表替换五个节点的 `llm_profile`，将原来平铺的模型参数移入本节点 `params`。节点的路由回退、重试、提示词和 SQL 设置仍由原字段控制。 |
| [开发环境模板 `.env.example`](../.env.example) 和 [部署环境模板 `nl2sql.env.example`](../../deploy/env/nl2sql.env.example) | 增加 `GLM_API_KEY`、`KIMI_CODE_API_KEY` 的空占位。部署模板也列出 `DEEPSEEK_API_KEY` 与可选的 `DEEPSEEK_BASE_URI`。可选地址写成注释示例，未设置时使用 `https://api.deepseek.com`。当前 `ConfigLoader` 的默认值只在变量未设置时生效，现有环境若把 `DEEPSEEK_BASE_URI` 留成空字符串，需要去掉该环境变量或填入有效地址。 |
| [模块 README](../README.md) 和 [摘要回归说明](../tests/unit/db_deepagent/README.md) | 将仍对外展示的 `llm_profiles` 示例改为 `llm_providers` 与业务段选择。说明 `params` 覆盖规则、模型切换时要调整原角色参数、密钥环境变量和三个供应商的端点。 |

本地沿用 `ConfigLoader.load()` 加载 `./nl2sql/.env`。容器沿用 Compose 的 `NL2SQL_ENV_FILE` 注入，部署环境按实际选中的供应商填写密钥。模板只登记变量名。现有 `ConfigLoader` 已能递归替换新增的 `${VAR}`，复用其加载路径和替换行为。 [配置加载器](../src/services/config_loader.py) · [Compose 的 NL2SQL 服务](../../deploy/compose.yaml)

### 3.2 注册项解析与工厂

以下改动集中在 [llm_factory.py](../src/services/llm_factory.py)。

1. 增加 `resolve_llm_config(provider, llm_name)`，返回 `(provider_config, model_config)` 两个字典。先取 `get_config().get("llm_providers")`，再用字典精确查 `provider`、`models` 和 `llm_name`。`qwen3.8-flash`、`k3.1-pro` 等 ID 含点号，按完整键名查询。解析只验证注册项与结构，供配置检查和工厂共同调用；注册表 `params` 中的未知参数键也在此拒绝，错误使用完整的 `llm_providers.<provider>.models.<llm_name>.params.<key>` 路径。解析错误包含缺失项的路径，业务层加上如 `time_resolution` 的段名。
2. 把入口改为 `get_llm(provider, llm_name, *, params=None, request_timeout_seconds=None) -> LLMWithMeta`。按 §2.3 在新字典中合并默认参数和调用点参数。组装所选模型时，验证密钥与兼容接口的非空 `base_url`。注册未使用的 GLM、Kimi 或 DeepSeek 时允许对应密钥为空。保持 `LLMWithMeta.llm`、`.provider`、`.model`，其中 `.model` 使用实际 ID。
3. 把 `_PROVIDER_MAP` 的客户端选择改为按注册项的 `adapter` 选择。`dashscope_native` 使用 `ChatTongyi`，`openai_chat_completions` 使用 `ChatOpenAI`，兼容路径设置 `use_responses_api=False`。`_build_params` 负责 §4.1 的字段映射，连接名只用于选择已核对的参数规则，不用于判断 LangChain 类。
4. 兼容路径将角色超时映射为 `request_timeout`，将 `stream` 映射为 `streaming`，显式设置的 `max_retries`、`response_format` 等沿用对应构造参数。百炼扩展字段和 DeepSeek 的 `thinking` 放入 `extra_body`。`deepseek-flash` 和三款 GLM 只有显式配置 `max_tokens` 时才通过同一个 `extra_body` 发同名字段，同时省略会被改写的构造参数。其余现有模型保留原映射。Kimi 的默认请求省略 `temperature: 0` 和思考开关。已知无效的显式值报出配置路径。`reasoning_effort` 按 §4.1 支持已验证的模型和值。
5. 全部调用方迁移后，删除旧 `llm_profiles` 解析及 `extract_overrides()`。旧业务 YAML 的 `llm_profile`、`overrides` 或五个旧节点平铺的模型参数报出位置及替换字段，避免误以为已应用。保留旧节点使用的 `extract_llm_content()`，继续处理原有字符串响应。

### 3.3 角色和节点的调用迁移

| 调用点和代码 | 新调用和应保留的行为 |
| --- | --- |
| [角色模型定义 `db_deepagent/config.py`](../src/modules/db_deepagent/config.py) | `ModelConfig`、`SummarizationConfig`、`TimeResolutionConfig` 共用 `provider: str`、`llm_name: str`、`params: dict[str, Any]`。两项标识必填且非空，`params` 默认 `{}`。`run_config_checks()` 校验三个角色的注册项，复用 `resolve_llm_config()`，摘要窗口校验继续独立执行。`load_base_db_agent_config()` 保留只做结构校验的入口，Web 应用数据源配置后继续执行完整检查。 |
| [主 Agent 与摘要 `agent.py`](../src/modules/db_deepagent/agent.py) | `build_model()` 和 `build_summarizer_model()` 使用各自的 `provider`、`llm_name`、`params` 和超时，通过 `resolve_llm_config()` 取得 `adapter`。`build_model()` 在构造客户端前检查合并后的 `stream`，按 §4.2 拒绝开启模型流式。主 Agent 选用 `dashscope_native` 时，要求 `model.retry.enabled: false`。如果启用了项目重试，在调用工厂构造客户端前报 `DbAgentConfigError`，提示设置 `model.retry.enabled=false`。兼容接口启用项目重试时，仍强制并核对同步及异步 SDK 客户端 `max_retries=0`，拒绝调用点显式设置的非零值。Harness 匹配继续用实际模型，若显式设置 `expected_harness_key`，切换时要更新。摘要独立构造，保留 `dbda_call_kind=summarization`。原生 DashScope 仍在构造后通过 `apply_request_timeout()` 设置超时。 |
| [日期解析 `services/time_resolver.py`](../src/modules/db_deepagent/services/time_resolver.py) | `build_time_resolver()` 以 `time_resolution` 的模型选择、参数和超时调用工厂。保留 `dbda_call_kind=time_resolution`、提示词、JSON 对象约定和解析结果校验。原生 DashScope 对该路径不支持的请求超时仍明确报错。 |
| [路由 `router.py`](../src/modules/nl2sql_father/nodes/router.py) | 从 `router` 段传 `get_llm("dashscope", "qwen-turbo", params={"temperature": 0})` 对应的配置值。保留节点现有异常回退。 |
| [规划 `planner.py`](../src/modules/nl2sql_father/nodes/planner.py) | 从 `planner` 段传 `dashscope + qwen-plus` 与 `params: {temperature: 0}`。 |
| [旧链路总结 `summarizer.py`](../src/modules/nl2sql_father/nodes/summarizer.py) | 从 `summarizer` 段传 `dashscope + qwen-plus` 与 `params: {temperature: 0.3}`。保留节点自身的 `llm_retry`。 |
| [问题解析 `question_parsing.py`](../src/modules/sql_generation/subgraph/nodes/question_parsing.py) | 从 `question_parsing` 段传 `dashscope + qwen-plus` 与 `params: {temperature: 0, max_tokens: 1500}`。 |
| [SQL 生成 `sql_generation.py`](../src/modules/sql_generation/subgraph/nodes/sql_generation.py) | 从 `sql_generation` 段传 `dashscope + qwen-plus` 与 `params: {temperature: 0, max_tokens: 2000}`。 |

五个旧节点一律从各自 YAML 段读取 `provider`、`llm_name`、`params`。本期保留表中的 Qwen 模型和参数，回归现有行为。移除它们对 `extract_overrides()` 的调用，并保留原有 `llm_meta.provider`、`llm_meta.model` 日志和异常处理。Doc 24 的角色实测范围是新链路。以后要切换旧节点时，只复验该节点的输出契约和异常回退，例如路由的 `simple`／`complex` 分类、问题解析的 `parse_result` 对象，或 SQL 生成清理后能通过现有校验的 SQL。模型选择通过公共装配传播到 CLI doctor 和 Web 请求，验收时核对 CLI 的模型、超时和 Harness，以及 Web 按数据源装配后的调用。 [CLI doctor](../src/modules/db_deepagent/cli.py) · [Web 请求](../src/api/v2/runs.py)

仓库配置、工厂和调用方在同一变更中更新。自定义 YAML 也按原 `llm_profile` 查出实际 provider 与模型 ID，原 `overrides` 转入所选角色的 `params`。`request_timeout_seconds` 作为三类角色的超时来源，旧 `overrides.timeout` 如果曾用来设置实际超时，迁移时将该值写入对应业务段的 `request_timeout_seconds`。旧节点则写入所选模型注册项或本节点的 `params.timeout`。

旧字段的报错继续服从各节点原有异常策略：Router 回退到默认复杂度并在 `router_reason` 保留错误，Summarizer 等节点按各自路径抛出或回退，不为了统一配置错误而改变业务异常行为。上线前必须单独检查服务器上的自定义 YAML 已完成迁移，不能把 Router 的默认回退当作旧配置仍然生效。

### 3.4 Embedding 与业务链路

Embedding 沿用 `DbEmbeddingClient` 和 DashScope `TextEmbedding`。新链路仍从 `embedding.profile` 读取 `qwen3_7_text_embedding`，旧链路仍由 `embedding_profiles.active` 选择。注册表迁移保留两个客户端依赖的 `llm_providers.dashscope.api_key`。继续使用现有 `qwen3.7-text-embedding`、1024 维和 Milvus 集合，沿用入库一致性检查，切换聊天模型无需重建向量。 [新链路 Embedding 客户端](../src/modules/db_deepagent/services/embedding.py) · [旧链路 Embedding 客户端](../src/services/embedding/embedding_client.py) · [一致性检查](../src/modules/db_deepagent/services/embedding_consistency.py)

## 4. 聊天模型适配与角色选择

### 4.1 工厂的参数映射

`openai_chat_completions` 适配器使用 `ChatOpenAI`，显式设置 `use_responses_api=False`。工厂负责传入选中连接的密钥和 `base_url`，以及合并后的角色参数。兼容性验收检查实际 HTTP 请求的路径、字段和值。主 Agent 沿用当前 SDK 客户端核验、项目重试和超时配置。 [当前模型构造与重试核验](../src/modules/db_deepagent/agent.py)

| 模型或端点 | 参数与实测行为 |
| --- | --- |
| 百炼 Qwen | 迁移现有 Qwen 配置时沿用已有参数。`qwen4-max` 直调时实测可用 `reasoning_effort: "none"` 关闭思考，角色装配采用的参数见 §4.3。 |
| DeepSeek 官方 `deepseek-flash` | 默认可省略 `thinking`，需要显式关闭时通过 `extra_body` 发送 `thinking: {type: disabled}`。显式 `thinking` 必须是映射，`type` 只接受 `enabled` 或 `disabled`，其他形态在导入客户端类之前报 `params.thinking.type` 或对应的注册表路径。思考开启时实测允许 `tool_choice: auto`，强制或指定工具可能收到 400。 |
| 智谱国内 Coding Plan 的三款 GLM | 常规配置省略 `thinking` 和 `reasoning_effort`，由模型使用默认思考模式。关闭请求虽然返回 200，实测仍有推理。工厂只接受显式 `thinking: {type: enabled}`，其他值在构造客户端前报 `params.thinking.type`，不发送名不副实的关闭请求。 |
| 国内 Kimi Code `k3.1-pro` | 常规配置省略思考参数，同时省略会被端点拒绝的 `temperature: 0`。思考模式下如显式设置温度，只接受数值 `1`，其他值在构造客户端前报 `params.temperature`。如需指定思考强度，可用已测的 `reasoning_effort` 值 `low`、`high` 或 `max`。`thinking.type: disabled` 返回 400。`reasoning_effort: "none"` 返回过 200，应答模型身份无法确认。两种关闭写法均不加入默认配置，工厂也不对它们做自动转换或回退。 |

`deepseek-flash` 和智谱国内 Coding Plan 的 `glm-5.4`、`glm-5.3`、`glm-5.3-flash` 在实测中忽略了 `ChatOpenAI` 发出的 `max_completion_tokens`。特殊映射按 `provider + llm_name` 判定：仅 `deepseek + deepseek-flash` 与 `glm_coding` 下三款 GLM 这四个组合在显式配置 `max_tokens` 时通过 `extra_body` 发送原字段，省略时使用供应商默认值。其他 provider 即使注册同名模型，也沿用普通构造映射，不受这四个组合的实测结论影响。Doc 24 未验证 `deepseek-chat` 和 `deepseek-reasoner` 的这项差异。这两个旧注册项沿用原有 `ChatOpenAI` 构造参数映射。 [参数实测报告 §4.4](reviews/2026-11-25_Doc24_ChatOpenAI兼容性测试报告.md)

### 4.2 思考草稿与流式请求

关闭思考是模型调用选项。`ChatOpenAI` 和 DeepAgents 都可以运行开启思考的模型。三款 GLM 和 Kimi `k3.1-pro` 在实测中把草稿放在独立的 HTTP `reasoning_content` 字段里。当前 `ChatOpenAI` 丢弃该字段，已检查的同进程 checkpoint 和下一轮请求没有推理草稿。

**首版接受不回传推理草稿的限制。** 在 Doc 24 的对照中，`k3.1-pro` 带草稿回传时，中间两个工具步分别返回了 18 和 53 字符的可见推理。不回传时两步均未返回可见推理，工具往返和最终提交仍成功。后续工具步骤因此拿不到上一轮的推理草稿，现有结果也无法确认服务端实际如何推理。报告引用的 Kimi 和 DeepSeek 文档要求在相应的思考模式工具往返中回传 `reasoning_content`，并记载缺少该字段时会返回 400。本次三步合成工具场景未复现该错误，结论限于所测端点和场景。主 Agent 默认沿用 Qwen。若业务要求保留跨工具步骤的推理连续性，本期暂缓选择 `k3.1-pro` 担任主 Agent，方案沿用 `ChatOpenAI` 现有消息路径。 [工具回传对照 §4.8](reviews/2026-11-25_Doc24_ChatOpenAI兼容性测试报告.md)

离线模拟中，正文内嵌的 `<think>…</think>` 或结构化推理块会随正文进入 checkpoint、摘要或历史文件。以后遇到这种响应形态，或目标端点实测要求回传独立推理字段时，按实际场景单独设计和验证。现有实测覆盖同进程和序列化，正式 PostgreSQL checkpoint 与跨进程恢复尚未验证。 [思考隔离实测 §4.8、§7.3](reviews/2026-11-25_Doc24_ChatOpenAI兼容性测试报告.md)

本次主 Agent 沿用非流式模型请求。项目的合成断流测试发现，残缺的工具参数可能让全可选参数的工具按默认值执行。首版在 `build_model()` 构造客户端前检查模型默认值与 `model.params` 合并后的 `stream`。省略或布尔 `false` 可用，有效值为 `true` 时抛出 `DbAgentConfigError`，指明 `model.params.stream`，从注册表继承时同时指出来源。非布尔值按字段类型错误处理。工厂仍为其他调用点保留 `stream` 到 `streaming` 的映射，Web 事件输出沿用现有路径。主 Agent 的断流保护另立任务，通过后再开放模型流式。 [流式与断流测试 §4.10、§7.2](reviews/2026-11-25_Doc24_ChatOpenAI兼容性测试报告.md)

### 4.3 角色候选与实测范围

| 模型 | 主 Agent、摘要与日期解析 |
| --- | --- |
| Qwen3.8-Max、Qwen3.8-Flash | 沿用现有主 Agent 与辅助模型组合。Flash 通过了关闭思考的三个角色用例。 |
| Qwen4-Max | 主 Agent 和摘要经工厂使用 `enable_thinking: false` 通过，日期解析未通过结构校验。若改用直调验证过的 `reasoning_effort: "none"`，按 §5.3 复测具体角色与请求参数的组合。 |
| DeepSeek Flash | 关闭思考时主 Agent、摘要和日期解析均通过，可作为三个角色的候选。 |
| GLM-5.4、GLM-5.3、GLM-5.3-Flash | 三个角色均在思考开启时通过。GLM-5.3-Flash 的长窗口摘要两次分别耗时 96.4 秒和 35.0 秒。编程套餐生产使用待确认。 |
| Kimi `k3.1-pro` | 省略 `temperature: 0` 后，主 Agent 和摘要通过。作为主 Agent 候选时需接受 §4.2 的推理连续性限制。长窗口摘要曾耗时 112.4 秒，接近 120 秒单次超时。日期解析出现 15 秒单次超时，默认仍选 Qwen。编程套餐生产使用待确认。 |

表中结果记录真实模型请求在隔离环境中的表现。Kimi `k3` 在报告中用于思考行为对照，Kimi 的角色结果对应 `k3.1-pro`。生产角色的启用条件见 §5.3。 [测试报告 §4.6、§5、§6、§10](reviews/2026-11-25_Doc24_ChatOpenAI兼容性测试报告.md)

## 5. 开发顺序与验收

### 5.1 注册表和工厂

- [ ] 扩展 `src/tests/unit/services/test_llm_factory.py`，验证含点号的完整模型 ID、两个 provider 注册同名模型时选择正确连接、空 `params` 继承、`0` 与 `false` 覆盖、嵌套对象整项替换及注册表参数未被调用修改。
- [ ] 迁移 `src/configs/config.yaml` 并实现 §3.2 的两个入口。更新 `src/tests/unit/services/test_config_loader.py` 中创建模型的用例。测试应覆盖未选用的供应商缺密钥时仍可构造 Qwen、选用时缺密钥报配置路径、DeepSeek 地址未设置时取官方默认，以及地址显式为空时拒绝构造。用 `qwen3-max` 验证未知模型在客户端构造前被拒绝。
- [ ] 用本地 HTTP 模拟验证客户端实际发出的 JSON 和 URI。保留原生 DashScope、现有百炼兼容路径及默认 SDK 重试的回归。覆盖 OpenAI 兼容调用的三种超时来源及全部缺失时的构造前报错。对新增候选检查 DeepSeek Flash 显式关闭参数，GLM 与 Kimi 的默认请求省略思考参数，Kimi 请求体不带旧 Qwen 的 `temperature: 0`，并拒绝 Kimi 非 `1` 的显式温度及 GLM `thinking.type: disabled`。在 `deepseek-flash` 请求中同时配置 `thinking` 和 `max_tokens`，验证两个字段都进入最终请求且没有 `max_completion_tokens`。用 GLM 的一个显式 `max_tokens` 请求验证字段映射，另核对省略参数时请求体没有输出上限字段。保留 `deepseek-chat` 与 `deepseek-reasoner` 的原构造参数映射断言。

### 5.2 八个调用点、环境模板和文档

- [ ] 按 §3.1、§3.3 更新三个角色、五个旧节点及对应 YAML。迁移 `src/tests/db_deepagent/unit/test_config.py`、`test_assembly.py`、`test_summarizer_model.py`、`test_time_resolver_llm.py`，让测试使用新模型字段。检查未知 provider、未知模型和非法参数能定位到角色及字段。确认各角色参数互不继承、超时与 SDK 重试实际生效、日期 JSON 结构校验通过，以及 Web 数据源校验流程仍然可用。
- [ ] 在 `test_assembly.py` 增加主 Agent 流式防误开用例。覆盖调用点直接设 `true`、从注册表继承 `true`，两种情况均在构造客户端前报配置错误。覆盖显式 `false` 覆盖注册表的 `true`，以及两层均省略，确认默认仍可装配。运行 `test_harness_profile.py` 和 `test_model_retry.py`，回归实际客户端超时、Harness 精确匹配、单层 SDK／项目重试、步骤额度与摘要恢复重入。
- [ ] 迁移现有用例 `test_assembly.py::TestRetryModelConstruction::test_native_dashscope_enabled_rejected`，按 `adapter: dashscope_native` 注入配置，断言 `model.retry.enabled: true` 在工厂调用前报错。沿用原生路径的重试行为。
- [ ] 迁移 `src/tests/unit/nl2sql_father/` 和 `src/tests/unit/subgraph/` 中使用旧字段的配置与 mock，核对五个节点的模型和采样参数与 §3.3 一致。更新仍使用旧入口的集成用例，包括 `src/tests/integration/services/test_llm_factory_integration.py`、`src/tests/integration/nl2sql_father/test_nl2sql_father_integration.py`、`src/tests/integration/sql_generation_subgraph/test_subgraph.py` 与 `src/tests/db_deepagent/integration/` 下的模型能力、摘要窗口和工具输出转存测试。
- [ ] 更新 §3.1 的两个环境模板和文档。清理现行代码、配置和测试中对 `llm_profile`、`llm_profiles` 与 `extract_overrides()` 的旧引用。Doc 24 的报告和探针留作原版本证据，复跑历史探针时按报告的 Git 基线执行。

从 `nl2sql/` 执行本地回归，使用项目 `.venv`。用模拟凭据和传输测试客户端，断言失败时定位到对应调用点或字段。

```bash
.venv/bin/python -m pytest -q \
  src/tests/unit/services/test_llm_factory.py \
  src/tests/unit/services/test_config_loader.py \
  src/tests/db_deepagent/unit/test_config.py \
  src/tests/db_deepagent/unit/test_assembly.py \
  src/tests/db_deepagent/unit/test_harness_profile.py \
  src/tests/db_deepagent/unit/test_model_retry.py \
  src/tests/db_deepagent/unit/test_summarizer_model.py \
  src/tests/db_deepagent/unit/test_time_resolver_llm.py \
  src/tests/unit/nl2sql_father \
  src/tests/unit/subgraph/test_question_parsing.py \
  src/tests/unit/subgraph/test_sql_generation.py
```

### 5.3 实际装配与生产启用条件

- [ ] 用默认 Qwen 配置做一次最小回归，确认主 Agent 工具往返及最终提交、摘要、日期解析和现有 Qwen 向量检索均可用。
- [ ] 用模拟传输跑一次 §2.2 的跨供应商组合，检查工具往返、摘要和日期解析的请求各自落到配置的模型和端点，工具 ID 正确关联。复用 Doc 24 的直调证据。
- [ ] 生产使用 GLM 或 Kimi 前，按 §2.1 确认目标账号和端点允许实际业务场景。改用正式业务端点时，用目标模型和端点验证请求参数及所选角色。此项只影响生产启用。
- [ ] 用实际部署所选的模型、端点和参数在测试环境做角色联调。主 Agent 核对工具往返、业务库查询结果和 Qwen 向量检索。摘要用现有触发阈值与保留窗口跑长窗口用例，记录耗时，确认现有超时有余量且后续问答能使用摘要。日期解析检查现有超时内返回的 JSON 结构和语义。若 Qwen4-Max 的主 Agent 或摘要改用 `reasoning_effort: "none"`，核对实际请求体并复跑对应角色用例。
- [ ] 生产启用思考开启的主 Agent 前，用隔离的 PostgreSQL 平台库和现有 `persistence.mode: postgres` 完成一次问数，记录实际 `session_id` 和 `thread_id`。该轮结束后重新装配 Agent 和持久化客户端，在同一会话中复用同一 `thread_id` 发起依赖上一轮结果的追问。使测试历史低于轮换阈值，核对追问实际使用的仍是原线程。检查 PostgreSQL 读回的 checkpoint 保留业务消息和工具结果，存储内容与后续模型请求里都没有推理草稿。此项沿用已完成轮次的正常会话续接，推理草稿存储和跨进程继续未完成的工具调用留在本期范围之外。 [PostgreSQL 持久化构造](../src/modules/db_deepagent/persistence/checkpoint.py) · [Runner 的线程选择](../src/modules/db_deepagent/runner.py)

只回退某个角色时，从 §2.2 的默认示例恢复该段的 `provider`、`llm_name` 和 `params`。回退整个配置结构时，工厂代码与对应版本的 YAML 一同恢复。
