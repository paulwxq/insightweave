# DeepAgents 能力调研：0.7.25 已有能力、后续新特性与自研功能替代分析

- 核对日期：2026-11-09
- 适用范围：`nl2sql` Python 项目，重点为 `src/modules/db_deepagent`
- 研究范围：`0.7.25` 已有但尚未使用或未充分使用的能力、`0.7.26`–`0.7.34` 的增量，以及扩大升级候选中的 LangChain / Core / LangGraph / 持久化能力
- 截止版本：核对日 PyPI 的 DeepAgents 最新版本为 `0.7.34`；本文件不推断此后版本
- 文档目的：说明能力如何使用，并突出可简化或替代现有自研实现的机会及边界
- 文档状态：代码对照与能力调研，不代表依赖升级、替代改造或真实业务验收已经完成
- 配套文档：[DeepAgents 0.7.34 升级评估与实施建议](1_DeepAgents_0.7.34升级评估与实施建议.md)
- 范围更新：根据用户澄清，以最新稳定版 DeepAgents 为前提，在兼容条件下尽量升级整个 NL2SQL 的已有依赖；此前的最小联动策略不再作为主方案

**模块边界与 uv 管理约定：** 本文及 doc 1 的全部实施建议仅限 `nl2sql/` 模块，由 uv 管理其 `pyproject.toml`、`uv.lock` 和 `.venv`。本文“项目根”指 `nl2sql/`；`src/`、`tests/`、`frontend/` 等路径均相对此目录。涉及执行时，先 `cd /opt/projects/insightweave/nl2sql`，再使用 `uv lock` / `uv sync` 管理依赖，使用 `uv run --extra dev pytest ...` 或 `uv run python ...` 运行测试与脚本；本文 Python 片段是能力接入示例。文中的扩大升级范围不包含同仓库的 `metaweave/`、仓库根 `frontend/` 及其环境，也不包含共享数据库服务端升级。候选隔离环境、extras 和具体实施顺序统一遵循 doc 1。

## 1. 研究基线与重点结论

本次核对了项目依赖文件、实际 `.venv` 安装版本、官方发布记录、官方文档及 Context7。核对时项目仍使用以下当前版本：

| 组件 | 当前版本 | 扩大范围主候选 |
| --- | --- | --- |
| DeepAgents | 0.7.25 | 0.7.34 |
| LangChain | 1.4.5 | 1.4.9 |
| LangChain Core | 1.6.10 | 1.7.6 |
| LangGraph | 1.2.15 | 1.2.20 |
| LangGraph checkpoint | 4.3.0 | 4.3.1 |
| LangGraph prebuilt | 1.1.0 | 1.1.1 |
| LangGraph SDK | 0.4.7 | 0.4.14 |
| PostgreSQL checkpoint | 3.1.3 | 3.2.0 |

本文件同时回答“升级后多了什么”和“现有 SDK 已经能做什么，而我们还在自行实现或尚未启用”。其中一些值得研究的能力在 `0.7.25` 就已经存在，无需等待升级。

当前查询链运行单主 Agent，没有启用子 Agent。因此，现有链路最直接的变化集中在摘要、权限和异常处理；异步协作能力需要另行接入。仅升级依赖，没有证据表明 SQL 生成准确率就会提高。

本次扩大候选已在全新临时环境完成安装与对照回归，完整清单和验证边界见 doc 1；`nl2sql/` 已按 doc 1 §10–§11 完成未使用依赖清理、直接依赖补齐与版本脚本修复，并于 2026-11-09 切换到 0.7.34 主候选（实施与验证范围见 [implementation_2026-11-09_run01](upgrade/implementation_2026-11-09_run01/README.md)）。新增 LangGraph 与持久化能力见 §9，不能继续把这些组件表述为本次默认保留不升级。

starlette、pandas、cryptography 现已按业务直接导入分别显式声明在主依赖或 web extra，当前精确版本为 `0.52.1`、`2.3.3`、`46.0.5`。历史候选没有这三行且保留旧测试依赖布局，不能当作当前项目的升级补丁；三包的待验目标与版本决策见 doc 1 §2.6。三包均保留直接依赖身份，不能为获得历史候选版本而删掉声明、退回传递安装。

### 1.1 重点：与现有自研实现重叠的能力

> **【重点：可替代或简化现有实现】下表是本文件的主要调研结论。除权限参数迁移外，多数是有条件的局部替代，不能直接删除整套业务实现。**

| SDK 能力 | 当前代码及职责 | 可替代的部分 | 必须保留或配套调整 | 详见 |
| --- | --- | --- | --- | --- |
| **`response_format` / `ToolStrategy`，0.7.25 已有** | `submit_final_answer`、`FinalAnswerGateMiddleware`：生成结构化答案、提醒提交、结束运行、验证证据 | **答案结构生成、Schema 校验与格式错误反馈** | `evidence_id`、run 归属、禁止引用探针、最终记录与错误协议；还要调整提交门控，并补充最终绑定工具的审计，不能只改期望清单 | §5.1 |
| **技能工具按需加载，0.7.25 已有** | `build_business_tools()` 一次装入业务工具；每轮固定暴露默认 15 个工具 | **工具清单与阶段工具暴露方式**，交给 `SkillsMiddleware` 管理 | 装配验证、披露前基础清单检查、最终出站工具审计与执行时授权；当前外层 RequestAudit 看不到后续披露结果 | §5.2 |
| **`ModelCallLimitMiddleware` / `ToolCallLimitMiddleware`，当前 LangChain 已有** | `RunBudgetMiddleware` + `RunBudgetLedger`：模型、工具、数据库执行额度及运行时限 | **通用的模型与工具调用次数限制** | 数据库执行额度、探针和下推计数、跨入口共享额度、加锁预占、deadline、稳定 stop_reason 和统计 | §7.1 |
| **SDK 工具大输出转存，0.7.25 已有** | `_envelope.finalize_output()` 缩减或截断工具返回；`ResultStore` 存数据库结果 | **通用长文本工具结果的缩减与转存**，可研究减少整体字符串硬截断 | 结果类型、分页、原始结果截断标记、证据和快照；不能整体替换 `ResultStore` | §5.3 |
| **公开 `permissions=`，0.7.31 新增** | `build_filesystem_middleware()` 传 `_permissions` 私有参数 | **直接替换私有构造参数** | 权限规则与只读工具白名单；本次最明确、范围最小的替代项 | §6.1 |
| **`summarizer=`，0.7.26 新增** | 已显式设置 `model=主模型, summarizer=主模型副本`（2026-11-10，[doc 5](5_独立摘要器接入修改方案.md)），副本保留摘要用途标记；尚未配置不同的摘要模型 | **将上下文管理模型与摘要生成模型分离** | 摘要用途 metadata、回调统计、配置与质量验收；复制模型设置 metadata 不是无意义的重复代码 | §6.2 |
| **`RubricMiddleware`，0.7.25 已有、0.7.33 退出 Beta** | 当前提交门控只做硬约束和漏提交提醒，没有独立的答案质量评分循环 | **可复用 SDK 的评分与修订循环**，避免未来另写一套；属于增补能力 | **不能直接替换证据守卫；直接追加会与已提交即结束、禁止重复提交冲突** | §5.4 |

### 1.2 当前已用、未用与部分使用的能力

| 能力 | 当前状态与代码证据 | 研究价值 |
| --- | --- | --- |
| 自动摘要、上下文溢出后的摘要恢复 | 已装配 DeepAgents `SummarizationMiddleware` | 继续使用，重点研究独立摘要器；不是本次首次引入摘要 |
| `CompositeBackend`、文件权限、只读文件工具 | 已用，见 `agent.py` 的 `build_backend()` / `build_permissions()` | 迁移公开接口，现有隔离与路由不必重写 |
| Skills 渐进披露 | 已用 `skills=["/skills/"]`，有 3 个项目技能 | 未使用技能工具延迟加载、`pinned_skills`、显式刷新元数据 |
| Harness Profile | 已用，禁用默认通用子 Agent | 不能把 Profile 本身算成未使用的新能力 |
| `DeltaChannel`、checkpoint、store、状态 reducer | 已用，装配还检查消息通道类型 | 不能把 SDK 持久化能力误写成首次获得，也不能据此删除项目会话锁与证据账本 |
| Todo | 已接入 `features.todo_list` 开关，默认关闭 | 可直接试用，无需另写计划工具 |
| Memory | 已接入 `features.memory` 开关，默认关闭；当前未提供对应 memory 目录和文件 | 需要准备内容，开关本身不代表完整功能已经可用 |
| 同步及异步子 Agent | 未用；`subagents=[]`，默认通用子 Agent 被禁用 | 研究专业分工、并行检索和后台分析 |
| 原生结构化输出、Rubric、人工中断恢复、v3 事件流 | 未在当前装配和 Runner 中启用 | 有复用价值，但都涉及现有控制流或接口适配 |
| 模型重试、工具重试、模型回退中间件 | 未在当前 Agent 中显式装配 | 可增强瞬时故障恢复；模型适配器内部重试不等于已经接入这些中间件 |
| QuickJS 解释器 / 程序化工具调用 | 未安装 `langchain-quickjs`、未启用 | 可研究批处理与组合分析，不能等同于 Python / pandas 或 PostgreSQL 语义执行器 |

代码证据主要来自 [Agent 装配](../src/modules/db_deepagent/agent.py)、[Runner](../src/modules/db_deepagent/runner.py)、[配置](../src/modules/db_deepagent/config/db_deepagent.yaml) 和 [状态定义](../src/modules/db_deepagent/state.py)。

## 2. DeepAgents SDK 的主要增量

| 变化 | 引入版本 | 相比 0.7.25 的增量 | 对当前项目的意义 |
| --- | --- | --- | --- |
| 自定义摘要器 | 0.7.26 | `SummarizationMiddleware` 新增 `summarizer=`，支持独立模型、Runnable 或函数 | 可用不同模型或自定义流程生成摘要，需要显式配置 |
| 异步子 Agent 状态与协作增强 | 0.7.26–0.7.30 | 报告等待人工输入、向主 Agent 通知状态、追加任务排队、配置通知与 webhook | 适合后台长任务及多个数据源的并行分析，需要额外接入 |
| 文件权限公开接口与继承检查 | 0.7.31 | 支持公开的 `permissions=`，覆盖文件中间件时继承 Agent 权限并检查冲突 | 可移除 `_permissions` 私有参数依赖，改善权限配置一致性 |
| 标准拒答处理 | 0.7.33 | 默认对标准拒绝内容块抛出 `ModelRefusalError`，并向主 Agent 报告子 Agent 拒答 | 直接影响异常流程，需要验证 Runner 和 Web API 收尾 |
| 可观测性增强 | 0.7.26、0.7.31、0.7.33 | 增强父子 Trace 关联、执行输出保留元数据、子 Agent checkpoint 的调用关联 | 方便排查委派任务及输出截断，收益取决于相关能力的使用情况 |
| Rubric 评分迭代退出 Beta | 0.7.33 | 已有评分与修订中间件退出 Beta | 可评估用于结果质量检查，但不属于本次首次新增的能力 |
| 摘要边界修复 | 0.7.34 | 增加消息 ID 辅助定位摘要边界 | 前序消息移除、位置变化时定位更稳健，属于可靠性修复 |

### 2.1 自定义摘要器：可以独立选择摘要模型或摘要流程

从 `0.7.26` 起，`SummarizationMiddleware` 支持 `summarizer=`：

- 传入模型名称或 `BaseChatModel` 实例：由指定模型按 `summary_prompt` 生成摘要。
- 传入 Runnable 或函数：由自定义流程直接生成摘要文本，此时不使用 `summary_prompt`。
- 原 `model` 仍负责模型上下文配置及 token 计数。
- 该配置同时用于自动摘要和 `compact_conversation` 工具。

当前项目已在 [agent.py](../src/modules/db_deepagent/agent.py) 中显式传入 `model=主模型, summarizer=主模型副本`（2026-11-10 按 [doc 5](5_独立摘要器接入修改方案.md) 实施）：主模型提供上下文依据，副本带 `dbda_call_kind=summarization` 用途标记执行摘要；尚未配置不同的摘要模型或自研摘要流程。升级与本次接线都不会自动降低摘要成本；后续如使用较便宜的模型承担摘要，需另做质量、成本与容量对照。

旧版本已经可以通过摘要中间件的 `model` 参数选择模型。本次新增的价值是进一步分离“管理上下文所依据的模型”和“实际生成摘要的模型或流程”。

来源：[官方自定义文档](https://docs.langchain.com/oss/python/deepagents/customization)、[0.7.26 发布记录](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.26)。

### 2.2 异步子 Agent：增强后台任务协作

异步子 Agent 在当前版本中已经存在，本次升级补充了以下能力：

- `0.7.26`：报告等待人工输入的状态；将异步运行关联到父 Trace，并记录发起它的 Agent。
- `0.7.27`：向主 Agent 提供异步子 Agent 状态更新。
- `0.7.30`：支持 `update_strategy="enqueue"`（Beta），将追加指令排在当前工作之后执行，避免立即中断正在运行的任务。
- `0.7.30`：增加 `notify_parent`（Beta），控制是否允许子 Agent 在结束后唤醒空闲的主 Agent。
- `0.7.30`：增加 `webhook`，由 Agent Server 在子 Agent 运行结束后向应用回调。

这些能力适合未来将复杂问题拆分为多个后台分析任务，让主 Agent 在任务执行期间继续与用户交互。异步子 Agent 需要支持 Agent Protocol 的服务端；仅安装新版 SDK 不会自动建立后台运行与回调基础设施。

当前项目显式使用 `subagents=[]`，并通过 Harness Profile 禁用默认通用子 Agent。这部分增强不会直接改变现有查询链行为。

来源：[0.7.26 发布记录](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.26)、[0.7.27 发布记录](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.27)、[0.7.30 发布记录](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.30)、[异步子 Agent 官方文档](https://docs.langchain.com/oss/python/deepagents/async-subagents)。

### 2.3 文件权限：公开参数与覆盖时的一致性检查

`0.7.31` 的主要变化是：

- `FilesystemMiddleware` 支持公开的 `permissions=` 参数。
- 自定义文件中间件覆盖默认实例时，继承 Agent 层配置的权限。
- 自定义中间件与 Agent 配置了冲突的非空权限规则时，拒绝该配置。

当前项目的 `build_filesystem_middleware()` 使用 `_permissions=permissions`，升级时可改用公开参数。旧参数在 `0.7.34` 中仍兼容，但已弃用，计划在 `0.9.0` 移除；不能同时传入新旧参数。

这属于接口和一致性完善，文件权限本身在当前版本已经存在。具体迁移与回归范围见配套升级评估文档。

来源：[0.7.31 发布记录](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.31)，以及已下载的 `0.7.34` 发布包源码。

### 2.4 标准拒答：升级后直接改变默认异常行为

`0.7.33` 默认加入标准拒答处理：模型返回标准拒绝内容块时抛出 `ModelRefusalError`；子 Agent 拒答也会报告给主 Agent。

这不是根据普通文字内容推断拒答。Qwen 等模型返回一段“无法回答”的文本，不一定触发该异常。

当前 Runner 已捕获执行异常并执行失败收尾，但仍应验证新增拒答路径的错误信息、预算收尾、日志和 Web API 返回，避免把拒答误判为查询成功或普通的未提交最终答案。

来源：[0.7.33 发布记录](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.33)。

### 2.5 可观测性与 Rubric 成熟度

可观测性增量包括父子 Trace 关联、执行输出的截断及输出文件元数据，以及子 Agent checkpoint 中的 `task` 调用 ID。后续使用委派任务或命令执行时，这些信息有助于追踪任务来源和识别输出是否完整。

`RubricMiddleware` 支持按评分标准检查结果，并要求 Agent 继续修订。该能力在 `0.7.25` 已经存在，`0.7.33` 的变化是退出 Beta。可将它列为后续质量控制的候选方案，但不能列为本次首次新增的功能。

来源：[0.7.26 发布记录](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.26)、[0.7.31 发布记录](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.31)、[0.7.33 发布记录](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.33)，以及两版 SDK 源码对比。

### 2.6 摘要边界：按消息 ID 辅助定位

`0.7.34` 的摘要事件增加 `cutoff_message_id`。SDK 优先按消息 ID 定位边界，找不到时回退到已有索引。

当摘要边界之前的消息被移除、消息位置变化，而边界消息 ID 仍能命中时，定位更稳健。这不能保证任意修改历史内容后，既有摘要仍与新历史一致。

当前项目未发现主动裁剪原线程消息列表的常规调用路径。因此，这项修复主要提供条件性的可靠性收益，不应表述为已经修复了项目中被复现的业务故障。项目 `read_summary_for_handoff()` 不读取新增 ID 字段，但严格校验既有 `cutoff_index` 和 `summary_message` 的 HumanMessage 类型；非法结构会抛 `AssemblyVerificationError`、使轮换失败，只有事件缺失或为 None 才作为尚无摘要。升级验收需使用目标 SDK 实际生成的摘要事件，验证旧字段、消息类型、持久化冷读和轮换交接，详见 doc 1 §4.4、§5.4；不因新增 ID 字段就提前改造交接协议。

来源：[0.7.34 发布记录](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.34)。

## 3. 相关依赖带来的能力与兼容性变化

### 3.1 LangChain：可选的逐工具调用人工审批中断

`langchain==1.4.6` 为人工审批中间件增加可选的逐调用中断，可以更细地控制一次模型返回多个工具调用时的审批过程。

这是依赖层提供的可选能力，需要主动配置。不能据此认为本项目升级后就会自动增加审批界面或改变当前工具执行流程。

```python
from langchain.agents.middleware import HumanInTheLoopMiddleware

approval_mw = HumanInTheLoopMiddleware(
    interrupt_on={"execute_sql": {"allowed_decisions": ["approve", "reject"]}},
    interrupt_mode="per_call",
)
```

将该中间件加入装配，并提供 checkpointer；本方案不要再通过顶层 `interrupt_on` 重复配置同一审批。逐调用模式应按中断 ID 恢复单个 Decision，例如 `Command(resume={interrupt_id: {"type": "approve"}})`，与 §5.7 的批量 `decisions` 协议不同。中断之前的工具中间件可能在恢复时再次执行，当前预算预占和审计副作用需要专门处理，避免重复计数。

来源：[LangChain 1.4.6 发布记录](https://github.com/langchain-ai/langchain/releases/tag/langchain%3D%3D1.4.6)。

### 3.2 LangChain Core：工具参数流改为增量片段

`langchain-core==1.7.0` 将相关工具参数流式事件改为 `tool-call-delta`，传递新增参数片段，而不是此前 `block-delta` 中的累计参数快照。

如果直接消费 v3 原始流式事件，应按增量拼接工具参数，不能把每个片段当作完整快照。官方说明 `.tool_calls.get()` 和 `.output` 保持不变。

这是需要关注的事件消费兼容性变化。不能将其等同于“首次支持流式输出”，也不意味着当前 Web API 会自动展示工具参数的实时变化。

来源：[LangChain Core 1.7.0 发布记录](https://github.com/langchain-ai/langchain/releases/tag/langchain-core%3D%3D1.7.0)。

### 3.3 本次升级范围的边界

此前最小方案保留 LangGraph `1.2.15`；根据用户澄清，本次扩大候选已纳入 `1.2.20`，并联动更新 checkpoint、prebuilt、SDK 和 PostgreSQL 持久化包。相关新增能力、默认行为变化和可替代边界见 §9。

其他联动依赖的版本及升级原因，以配套升级评估文档为准。安装了新版模型集成包，也不代表当前 Qwen 查询链会自动启用其他模型提供商的特性。

## 4. 对当前项目的应用建议

| 优先级 | 建议 | 预期价值 |
| --- | --- | --- |
| 本次升级一并处理 | 将 `_permissions` 迁移为公开的 `permissions`，验证标准拒答异常收尾 | 减少私有接口依赖，确认默认行为变化符合项目约定 |
| 升级后优先评估 | 使用独立 `summarizer`，对比摘要成本、延迟和信息保留质量 | 利用新增配置接口优化摘要流程 |
| 有后台分析需求时接入 | 异步子 Agent、任务排队、状态通知和 webhook | 支持长任务并行分析及执行期间的用户交互 |
| 有质量迭代需求时评估 | Rubric 评分与修订流程 | 探索结果质量控制；能力在旧版已经存在 |
| 消费 v3 原始事件时核对 | `tool-call-delta` 的增量拼接逻辑 | 避免将增量片段误作完整参数 |

以上为能力与应用分析。升级能否发布，仍应依据配套升级评估文档中的安装、模型、数据库、旧会话恢复和端到端验收结果判断；本文件不新增已完成测试或上线验证的声明。

## 5. 0.7.25 已具备、当前未用或未充分使用的能力

本节示例说明接入方式，不是可以整段覆盖 `build_db_agent()` 的完整项目代码。`model`、`backend`、`services`、`config`、`ctx` 等变量指项目已有对象；接入时还需保留现有隔离、审计、证据和持久化约束。除单独注明外，本节核心 DeepAgents API 已在 `0.7.25` 发布包源码中核对。

### 5.1 【重点：局部替代】原生结构化输出与最终答案提交

**当前实现。** [final_answer.py](../src/modules/db_deepagent/tools/final_answer.py) 定义提交工具和 Schema，并验证单独调用、未重复提交、证据归属及状态一致性；[final_answer_gate.py](../src/modules/db_deepagent/middleware/final_answer_gate.py) 提醒漏提交并结束运行；Runner 从 `db_final_answers[run_id]` 读取最终记录。

**SDK 用法。** DeepAgents 的 `response_format` 可复用 LangChain 的结构化输出策略。对支持工具调用的模型，可显式选择 `ToolStrategy`，避免依赖模型是否支持原生 JSON Schema：

```python
from deepagents import create_deep_agent
from langchain.agents.structured_output import ToolStrategy
from src.modules.db_deepagent.schemas import DbAgentFinalAnswer

# 独立方案示例：这里不再装入旧 submit_final_answer 工具和旧提交门控。
agent = create_deep_agent(
    model=model,
    tools=business_tools,
    backend=backend,
    response_format=ToolStrategy(DbAgentFinalAnswer, handle_errors=True),
)
result = agent.invoke({"messages": [{"role": "user", "content": question}]})
answer = result["structured_response"]
```

> **【重点：可减少代码，但不能直接删除提交工具】SDK 可承担结构生成、Schema 校验及格式错误反馈；它不会自动验证 `evidence_id` 是否来自本运行，也不会写入我们的 `FinalAnswerRecord`。**

若采用此方案，需要把业务校验提取为可复用函数，在结构化候选答案形成后执行；如需模型继续修正，还需接回修订循环。通过校验后，再按原约定保存 run 级最终记录并交给 Runner。不能将字段格式正确视为业务答案可接受。

同时必须调整 `TOOL_VISIBLE_FIELDS`、`FULL_TOOLS`、`verify_assembly()` 和提示词中的提交约定。移除旧提交工具会改变基础工具清单；但新输出工具不会以普通业务工具的方式直接出现在当前 `RequestAuditMiddleware` 的检查点。

**审计位置是另一项独立改造。** 当前 RequestAudit 在调用内层 handler 前读取 `request.tools`，而本版 LangChain 在内部最终绑定模型时才为 `ToolStrategy` 追加结构化输出工具，并设置对应的 `tool_choice`。因此，不能只将输出工具名加到 RequestAudit 的 expected set：这样可能误报缺失；仅改基础清单虽然可以让检查通过，也没有审计新输出工具实际使用的名称、Schema 和有效 tool choice。最终绑定信息应由能观察实际模型绑定或出站请求的适配层记录和核验，具体分层见 §5.2；仅把审计移到 Skills 后面仍不足以覆盖内部追加的输出工具。

**建议先做隔离验证，再决定是否值得替换现有稳定链路。** 此处描述的是接入前需要补齐的审计能力，不代表项目已经具备最终绑定审计。

来源：[结构化输出官方文档](https://docs.langchain.com/oss/python/langchain/structured-output)、[DeepAgents 0.7.25 装配 API](https://github.com/langchain-ai/deepagents/blob/deepagents%3D%3D0.7.25/libs/deepagents/deepagents/graph.py)。

### 5.2 【重点：替代全量工具暴露】Skills 工具延迟加载、固定加载与刷新

当前 3 个技能只有名称、描述和正文，没有 `metadata.include_tools`；所有业务工具都通过 `tools=` 直接注册。SDK 可以在技能正文被读取或固定加载后，才向模型提供该技能关联的工具。

例如，在现有 `result-analysis/SKILL.md` frontmatter 中增加：

```yaml
metadata:
  include_tools: read_result analyze_result
```

`include_tools` 是以空格分隔的字符串，不是 YAML 列表。然后把这两个工具交给同名的技能中间件覆盖实例管理，而不是继续放在主 Agent 的普通工具清单中：

```python
from deepagents.middleware.skills import SkillsMiddleware

all_tools = build_business_tools(services)
lazy_names = {"read_result", "analyze_result"}
always_tools = [t for t in all_tools if t.name not in lazy_names]
skill_tools = [t for t in all_tools if t.name in lazy_names]
skills_mw = SkillsMiddleware(
    backend=backend,
    sources=["/skills/"],
    tools=skill_tools,
)
# 在原装配中调整 tools=，并加入 skills_mw 覆盖默认 SkillsMiddleware。
# submit_final_answer 仍应按当前方案加入 always_tools。
```

以后工具很多、需要按数据源动态解析时，可将 `tools=` 换成 `SkillToolResolver`。它接收工具组名称和 runtime，返回该组工具；应保持同一线程内解析稳定，并按 runtime.context 中的数据源隔离缓存。当前 Runner 使用同步 `invoke`，不能直接接入仅异步的 resolver。

**固定加载与刷新：** 若应用已经知道需要哪个技能，可在 Runner 的图输入中增加 `pinned_skills`；技能修改后，可将 `skills_metadata` 设为 `None` 请求重新发现元数据：

```python
agent.invoke(
    {
        "messages": [{"role": "user", "content": question}],
        "pinned_skills": ["schema-retrieval", "query-planning"],
        "skills_metadata": None,  # 仅确实需要刷新时传入，不必每轮重扫
    },
    config=config,
    context=ctx,
)
```

`pinned_skills` 会把指定技能正文交给模型，不保证模型必然执行每条指令，数据库守卫仍要保留。可优先试用固定加载，降低关键流程说明未被模型主动读取的概率；也可将可选说明从 `prompts.yaml` 移入技能正文，保留全局硬约束。

> **【重点：存在明确接入冲突与审计盲区】当前 `verify_assembly()` 要求工具节点精确包含固定集合。把 `read_result` / `analyze_result` 从普通工具清单移出后，若仍使用原 expected set，装配或基础请求检查就会报缺失；这与技能何时被读取无关。当前 RequestAudit 又位于 Skills 之外，只把 expected set 改成 always-tools 并不能覆盖后续披露。**

源码顺序是：DeepAgents 把新用户中间件插在 core 之后、Skills 等尾部中间件之前；LangChain 将列表靠前的 `wrap_model_call` 作为外层。项目 RequestAudit 在进入内层 handler 前检查 `request.tools`，因此看到的是技能披露前的基础请求。同名覆盖 `SkillsMiddleware` 不会自动改变这个顺序。

实施时应分别覆盖以下层次：

1. **装配授权范围：** 明确基础工具、允许延迟加载的业务工具，以及获准的结构化输出 Schema；对静态工具检查注册清单，对 resolver 检查可返回范围。不能再要求所有工具都预先出现在同一 ToolNode 中。
2. **基础请求检查：** 当前 RequestAudit 可继续检查基础工具集合，但应明确日志和断言发生在披露前；不能将其通过表述为“最终模型工具集已通过审计”。
3. **最终绑定与执行授权：** 在能观察 Skills 修改之后、模型最终绑定阶段工具信息的位置核验出站内容，覆盖工具名、Schema、结构化输出工具和实际 tool choice；执行时保留 SDK 的技能披露门控及项目业务授权。若所选版本没有满足需求的公开观察点，应先验证模型适配层的方案，不能仅调整 expected set 就视为完成。

这三层是后续改造建议，不是现有完整实现。不建议为审计散布对 SDK 私有状态或 helper 的直接读取；确需版本适配时，应集中在现有 SDK adapter 并做针对性回归。

适合先试验结果分析工具；时间、维度守卫与提交入口宜保持可用。技能工具在相关读取记录离开有效上下文后可能不再披露，也需要验证摘要后的可用性。收益是减少工具 Schema 占用，不是减少业务工具自身的实现。

来源：[Skills 官方文档](https://docs.langchain.com/oss/python/deepagents/skills)、[0.7.25 SkillsMiddleware 源码](https://github.com/langchain-ai/deepagents/blob/deepagents%3D%3D0.7.25/libs/deepagents/deepagents/middleware/skills.py)。

### 5.3 【重点：局部替代】SDK 大工具输出转存与手工截断

> **实施状态（2026-11-11，[doc 7](7_工具大输出SDK转存接入方案.md)）：** 已以 `inspect_tables` 为试点接入。该工具不再经 `finalize_output` 压缩/硬截断，直接返回完整缩进 JSON；超过转存阈值（20000 token ≈ 80000 字符，SDK 按 4 字符/token 近似）时由 `FilesystemMiddleware` 写入 `/artifacts/large_tool_results/`，模型收到预览+路径并可用既有 `read_file` 按行回读。离线验证覆盖边界、逐字相等、权限回读、Command 账本、写入失败与完整生产图（详见 doc 7 实施记录）。默认头尾各 5 行预览对缩进 JSON 主要是结构/括号信息；真实模型是否主动回读、费用与生产性能未验证；其他工具的 12000 字符硬截断与 JSON 中部截断缺陷保持原状、未全局修复。本节以下为实施前分析，保留原文。

当前 [_envelope.py](../src/modules/db_deepagent/tools/_envelope.py) 的 `finalize_output()` 先缩短 SQL、卡片、列表，仍过长时执行 `text[:max_chars]`。这可能截断 JSON，并且模型无法从被丢弃的正文恢复全部内容。

SDK 文件中间件已经配置了 `tool_token_limit_before_evict=20000`，但业务工具的 `max_tool_output_chars=12000` 通常会先缩减结果，当前设计有意避免 SDK 大结果转存。SDK 具有把过大文本工具结果转存到 backend，并向模型返回文件位置和读取提示的能力。

**如何试用：** 选一个非结构化长文本工具，保留 `FilesystemMiddleware` 的转存功能，调整该工具的字符截断策略及转存 token 阈值，让 SDK 保存长文本，再通过已有 `read_file` 按需读取。字符阈值与 token 阈值不是同一单位，需根据真实输出测量。

> **【重点：可研究替代通用硬截断，不可替代业务结果存储】先验证转存路径落在当前允许读取的 `/artifacts/` 路由内、文件可回读，以及 `Command` 中的账本更新仍被保留。SDK 不会恢复数据库查询时已丢弃的行。**

必须保留 [ResultStore](../src/modules/db_deepagent/services/result_store.py) 的结果类型编码、分页、来源、`result_ref`、`source_truncated` 和证据关联。对于需要机器读取的 JSON envelope，仍需保留关键字段和合法结构，不能让通用转存机制成为业务数据契约的替代品。

来源：[上下文管理官方文档](https://docs.langchain.com/oss/python/deepagents/context-engineering)、[0.7.25 文件中间件源码](https://github.com/langchain-ai/deepagents/blob/deepagents%3D%3D0.7.25/libs/deepagents/deepagents/middleware/filesystem.py)。

### 5.4 Rubric：复用评分与修订循环，先解决提交门控冲突

SDK 的 `RubricMiddleware` 可独立选择评审模型、提供查证工具、限制迭代次数，并通过 `on_evaluation` 收集结果。挂载后，还需要在调用输入中传入 `rubric`，没有评分标准时不会启动评分循环。

```python
from deepagents import RubricMiddleware

rubric_mw = RubricMiddleware(
    model=grader_model,
    tools=grader_readonly_tools,
    max_iterations=2,
    on_evaluation=record_evaluation,
)
# 在隔离的评审方案中装配 middleware=[rubric_mw, ...]。
grading_input = {
    "messages": [{"role": "user", "content": question}],
    "rubric": "核对统计口径、证据引用、截断范围说明及回答是否覆盖问题。",
}
```

项目可用于检查答案有没有说明时间范围、分母、单位及样本覆盖范围。评审必须能访问必要证据；SDK 提供 `prepare_messages_for_grader`、`grader_state_schema` 与 `build_grader_state` 等扩展点，但业务证据的整理和只读工具仍由项目提供。

> **【重点：不能直接追加到现有中间件列表】当前答案一旦登记，`FinalAnswerGateMiddleware.before_model()` 会结束运行，提交工具还拒绝重复提交。Rubric 要求回到模型修订时，会被这套控制流挡住。**

可先采用独立的离线评审，避免改变在线提交协议。如果需要在线自动修订，应区分“候选答案”与“最终接受答案”，允许候选修订，评分通过后再写最终记录。必须保留硬证据校验，额外模型和工具调用也要计入观测及预算；当前 observer 只识别 `agent` / `summarization` 用途，需要扩展评审用途。

来源：[Rubric 官方文档](https://docs.langchain.com/oss/python/deepagents/rubric)、[0.7.25 Rubric 源码](https://github.com/langchain-ai/deepagents/blob/deepagents%3D%3D0.7.25/libs/deepagents/deepagents/middleware/rubric.py)。

### 5.5 Todo 与 Memory：已有开关可以复用

**Todo。** 当前只需在配置中设置 `features.todo_list: true`，装配会追加 `TodoListMiddleware`，`stage_expected_tool_names()` 已考虑 `write_todos`。适合多步分析时记录计划，不必自建计划工具；计划只是模型工作状态，不能替代预算或授权检查。

**Memory。** 先创建 `src/modules/db_deepagent/memory/memory_context.md`，填写稳定的项目背景、术语和已确认业务口径，再设置 `features.memory: true`。当前代码会加载虚拟路径 `/memory/memory_context.md`，并提供只读权限。

这能把稳定背景从不断增长的主提示词中分离出来，但 Memory 内容本身仍会进入模型上下文，不是零 token 成本。当前该路径位于模块目录，对使用它的会话共享，不能直接用来保存每个用户或数据源的私有记忆。自动写回记忆还需要单独的更新流程与隔离设计。

**不能替代的部分：** Memory 文件不等于证据账本或历史恢复；不能用它替代 `_handoff()`、`reuse_evidence`、checkpoint 或查询结果归档。新建和切换线程后的内容也应验证，不能把加载机制理解为任意外部编辑后即时热更新。

来源：[Memory 官方文档](https://docs.langchain.com/oss/python/deepagents/memory)、[预置中间件文档](https://docs.langchain.com/oss/python/langchain/middleware/built-in)。

### 5.6 同步子 Agent：专业分工与上下文隔离

`SubAgent` 可定义自己的模型、工具、提示词和 `response_format`；默认 `mode="isolated"` 只接收委派任务。`mode="fork"` 可继承父上下文，但属于实验能力，且不能单独配置 `skills`。

```python
from deepagents import SubAgent
from pydantic import BaseModel

class ReviewFinding(BaseModel):
    issues: list[str]

reviewer: SubAgent = {
    "name": "sql_reviewer",
    "description": "独立核对 SQL 的表粒度、JOIN 放大和统计口径",
    "model": reviewer_model,
    "system_prompt": "返回待核对问题；不要生成未查证的数据结论。",
    "tools": reviewer_readonly_tools,
    "response_format": ReviewFinding,
}
# create_deep_agent(..., subagents=[reviewer])，由 task 工具委派。
```

可用于独立复核、并行探索候选表或复杂查询的多个子问题。声明式子 Agent 不需要依赖默认通用子 Agent，但要在装配审计中允许新增 `task` 工具。

子 Agent 返回的结构化结果会序列化为给父 Agent 的工具结果；**不能假定子线程的 `db_executions`、授权、`result_ref` 会自动成为父运行可提交的证据**。先试验不执行数据库写状态的独立复核角色；如需业务查询，应明确上下文、共享预算、证据回传与 reducer 契约。`CompiledSubAgent` 可承载现有图，但不会自动消除这些差异。

来源：[Subagents 官方文档](https://docs.langchain.com/oss/python/deepagents/subagents)、[0.7.25 子 Agent 定义](https://github.com/langchain-ai/deepagents/blob/deepagents%3D%3D0.7.25/libs/deepagents/deepagents/middleware/subagents.py)。

### 5.7 人工审批与可恢复澄清：避免未来自建暂停引擎

`interrupt_on` 可在工具调用前暂停，借助 checkpointer 持久化中断，随后使用同一线程恢复：

```python
from langgraph.types import Command

# 装配：create_deep_agent(..., checkpointer=checkpointer,
#     interrupt_on={"execute_sql": {"allowed_decisions": ["approve", "reject"]}})
paused = agent.invoke(input_state, config=config, context=ctx)
# 应用确认存在 __interrupt__ 后，把待审批内容展示给用户。
resumed = agent.invoke(
    Command(resume={"decisions": [{"type": "approve"}]}),
    config=config,
    context=ctx,
)
```

此示例使用现有的批量审批协议。若是询问用户业务口径，可以另设澄清工具，在工具内部使用 LangGraph `interrupt()`，后续用 `Command(resume=用户回答)` 恢复；不要混用审批协议与自由文本澄清协议。

**当前限制：** Runner 使用同步 `invoke`，随后关闭预算并按最终答案收尾。现有 `needs_clarification` 是答案状态，不是保存执行现场等待恢复的暂停协议。引入中断需要新增 paused 状态及恢复入口，并处理业务 run_id、预算重开或续接、会话锁、Web pending 和归档时机。`interrupt_on` 也不能替代 SQL 授权和证据守卫。

来源：[DeepAgents 人工审批](https://docs.langchain.com/oss/python/deepagents/human-in-the-loop)、[LangGraph 中断与恢复](https://docs.langchain.com/oss/python/langgraph/interrupts)。

### 5.8 v3 事件流：复用 SDK 事件，增量展示执行过程

当前 Runner 的 `agent.invoke()` 只在执行后进入结果收尾。SDK 已有类型化事件流，可用于展示模型输出与子 Agent 生命周期：

```python
stream = agent.stream_events(input_state, config=config, context=ctx, version="v3")
for message in stream.messages:
    for text_delta in message.text:
        print(text_delta, end="", flush=True)
```

LangGraph 的 v3 流协议目前标为实验性，接口可能变化；这里的 `stream_events` / `astream_events` 是 LangGraph API，并非 DeepAgents 自有 API。异步调用使用 `await agent.astream_events(input_state, config=config, context=ctx, version="v3")`；子图（包括子 Agent）事件通过 `stream.subgraphs` 获取，可按 `graph_name`、`path`、`trigger_call_id` 和 `status` 过滤。前端仍需由应用映射成自己的事件并通过 SSE 或其他通道发送，SDK 不会自动改变 FastAPI 路由。

这属于潜在的交互增强，当前并没有一套可直接删去的同等自研事件流。保留最终状态读取、证据拼装和收尾；v3 原始工具参数事件升级后的增量语义见 §3.2。

来源：[事件流官方文档](https://docs.langchain.com/oss/python/deepagents/event-streaming)。

### 5.9 QuickJS 解释器、程序化工具调用与动态子 Agent

这是可选集成包 `langchain-quickjs` 的能力，不是安装 DeepAgents 主包后就自动启用。`0.7.25` 源码标签已经包含 QuickJS 集成，所带集成版本为 `0.3.9`；该数字仅是 `0.7.25` 基线示例。`deepagents==0.7.34` 的 `quickjs` extra 要求 `langchain-quickjs>=0.3.11`。当前项目锁文件未安装它；实际接入时需另选并锁定与目标 SDK 兼容的版本，不能直接对现有环境执行无约束升级。

```python
from langchain_quickjs import CodeInterpreterMiddleware

interpreter_mw = CodeInterpreterMiddleware(
    ptc=[stateless_lookup_tool],  # 应用另行提供的无账本更新的只读工具
    subagents=False,     # 初次试验关闭解释器中的子 Agent 调度
)
# create_deep_agent(..., middleware=[interpreter_mw, ...])
```

**入口前提：** 上例启用了 PTC，其 bridge 注册为异步宿主函数，须通过 `await agent.ainvoke(...)` 调用。`0.7.25` 标签所带集成明确说明，带异步 bridge 的 REPL 使用同步 `invoke` 会抛出 `ConcurrentEvalError`；当前 Runner 正使用同步 `invoke`，不能只追加中间件就启用上例。应先在隔离实验中验证异步调用链及预算、审计和持久化适配；若只试验普通 eval，可使用 `ptc=None, subagents=False`，不注册这些异步桥。未来选定 QuickJS 版本后，仍须重新核对同步与异步入口限制，不为本次依赖升级提前改造执行框架。

它提供 QuickJS 的 JavaScript `eval`，可使用循环、条件和工具组合。程序化工具调用允许中间结果留在解释器中，只向模型展示整理后的输出；配置了子 Agent 后，还可从代码中通过 `task()` 做动态委派。

> **【重点：与结果分析代码有关，但不能整体替换】[ResultAnalysisService](../src/modules/db_deepagent/services/result_analysis.py) 已实现聚合、排名、占比、比较、类型处理及 SQL 下推。QuickJS 可探索替代新增的通用组合和编排代码，但不能直接替代 Decimal 精度、NULL/排序、并列排名、PostgreSQL 类型语义、源结果截断和证据登记。**

尤其不能把分页读到的部分行当成总体，也不能把 JavaScript `Number` 的计算视为现有 Decimal 计算的等价物。初期应让解释器组合已有受控工具；若计算结果要进入 findings，仍须通过服务端登记证据。

**基线集成存在已核对的控制流差异：** `0.7.25` 标签所带 QuickJS 的 PTC bridge 直接调用工具的 `arun`，其源码明确提示不经过逐工具 `interrupt_on` / HITL 审批；返回 `Command` 时，转换函数提取消息内容交给 JavaScript，不能按普通 ToolNode 路径推断账本更新会被应用。因此，上例先使用另行提供的无账本更新工具，**不能直接把 `execute_sql`、`analyze_result` 或其他返回业务 `Command` 的工具列入 PTC**。

实际选定 QuickJS 版本后，应重新核对这一行为；尤其 `0.3.11+` 需重新验证 PTC 异步入口及返回 `Command` 时的处理，并为 `eval` 和 PTC 建立预算、审计、授权及状态更新契约。一次外层 `eval` 不等于已经统计了所有内层调用。动态子 Agent 也要遵守 §5.6 的证据回传规则。

来源：[Interpreters 官方文档](https://docs.langchain.com/oss/python/deepagents/interpreters)、[动态子 Agent 文档](https://docs.langchain.com/oss/python/deepagents/dynamic-subagents)、[0.7.25 标签下的 QuickJS 集成](https://github.com/langchain-ai/deepagents/tree/deepagents%3D%3D0.7.25/libs/partners/quickjs)。

## 6. 升级后值得使用的能力：接入示例与项目落点

### 6.1 【重点：直接替代】公开文件权限参数

最低版本为 `0.7.31`。在 `build_filesystem_middleware()` 中保留 backend、只读工具清单和阈值，只替换参数：

```python
FilesystemMiddleware(
    backend=backend,
    tools=list(_FILE_TOOLS),
    tool_token_limit_before_evict=cfg.filesystem.tool_token_limit_before_evict,
    permissions=permissions,
)
```

原 Agent 和中间件都使用同一组权限规则，无需重建权限模型。后续如果选择由 SDK 继承权限，可省略中间件权限参数，但应继续验证装配、隐藏文件拒绝、只读路径与写入拒绝。

### 6.2 【重点：简化摘要策略】独立 summarizer

最低版本为 `0.7.26`。在原摘要中间件中保留上下文依据和阈值，把生成摘要的模型独立配置：

```python
from deepagents.middleware.summarization import SummarizationMiddleware

summary_mw = SummarizationMiddleware(
    model=model,
    backend=backend,
    summarizer=summary_model,
    summary_prompt=build_summary_prompt(prompts),
    trigger=("tokens", cfg.summarization.summary_trigger_tokens),
    keep=("tokens", cfg.summarization.recent_context_tokens),
    trim_tokens_to_summarize=cfg.summarization.trim_tokens_to_summarize,
)
```

`summary_model` 可由独立的 LLM profile 创建，继续设置 `dbda_call_kind="summarization"` metadata，并带上明确的请求超时。若使用 Runnable / callable，应返回摘要文本；此时不使用 `summary_prompt`，要由自定义流程自行管理提示词。

这不会替代线程轮换时 `_handoff()` 的结构化证据携带，也不会自动修复私有摘要状态读取接口。应分别验证摘要质量、回调计数与旧会话恢复。

### 6.3 异步子 Agent 排队、通知与 webhook

异步子 Agent 本身在 `0.7.25` 已有，以下三个配置项最低需要 `0.7.30`：

```python
from deepagents import AsyncSubAgent

background_analyst: AsyncSubAgent = {
    "name": "background_analyst",
    "description": "在后台分析复杂查询的候选方案并返回可核对结论",
    "graph_id": "db_analyst",
    "url": "http://agent-server:8123",
    "update_strategy": "enqueue",
    "notify_parent": False,
    "webhook": "https://app.example.com/agent-events",
}
# create_deep_agent(..., subagents=[background_analyst])
```

URL 只是占位示例，需部署真实 Agent Protocol 服务并准备应用回调。SDK 提供 `start_async_task`、`check_async_task`、`update_async_task`、`cancel_async_task` 工具，模型通过这些入口管理任务。`notify_parent=False` 适合由应用自行交付结果；空闲主 Agent 自动唤醒依赖运行服务端，不能从当前单次本地 invoke 推导出完整后台生命周期。

`enqueue` 默认不是启用状态，默认更新策略仍是中断。排队追加指令在前一运行因人工输入暂停时仍可能开始，并丢弃原输入请求，因此需要专项验证审批场景。当前同步 Runner 使用本地 ASGI 传输时还需改为异步入口；使用可访问 URL 是另一种部署方式。

`update_strategy` 与 `notify_parent` 在 `0.7.34` 固定源码中均标为 **Beta**，排队跟踪和父 Agent 唤醒方式可能变化。接入时应把排队、取消、唤醒、人工输入与恢复行为纳入专项兼容检查，不能当作长期不变的生命周期契约。

> **【重点：后台子任务不能直接复用当前预算生命周期】同部署转发父 context 不等于共享预算；跨进程的 Agent Server 也不会自动共享当前 Python 对象。配置远程 `url` 时，SDK 不转发父 context，子服务需要自行重建数据源、身份和运行上下文。**

当前 [DbAgentContext](../src/modules/db_deepagent/context.py) 中的 `run_id` 与 `deadline_monotonic` 属于一次 invoke；[Runner](../src/modules/db_deepagent/runner.py) 在 invoke 前 `open_run()`，在 finally 中 `close_run()`。[RunBudgetLedger](../src/modules/db_deepagent/services/run_budget.py) 对相同 `run_id` 的重复打开保留原条目，关闭则删除条目；而 `reserve()` 遇到未注册的 `run_id` 会放行。在同一账本上直接让父子运行复用该 ID，或者让子任务继续沿用已经关闭的父 ID，可能使一方关闭影响另一方。不同进程中的独立账本则只是各自计数，也不是共享额度。

因此，子任务入口应分配独立 `child_run_id`，记录父运行、任务与数据源归属，独立管理打开和关闭。可以明确采用每子任务独立预算；如果要求整个父子任务树共享总额度，应另设跨任务的原子预算协调机制，不能仅事后汇总计数就视为已强制限额。远程服务还需从可信配置和获准的任务输入重建上下文，不能把客户端任意传入的关联字段当作授权。

跨主机或跨重启恢复时，不能直接复用序列化的 `time.monotonic()` 绝对数值作为 deadline。应约定可持久化的到期时刻或统一期限，并由各执行端计算本地剩余时限；同机进程可使用一致的单调时钟基准，但这并不意味着进程内账本随之共享。

子任务的 `db_executions` / `db_analyses`、`result_ref` 和截断信息仍须显式转换、校验并登记到允许使用它们的父运行证据中，不能因为传递了 context 就直接用于父运行的 findings。以上都是未来启用后台协作的接入条件；当前单主 Agent 未运行这些异步路径。

后台任务管理不等于现有 Web 会话锁、pending 标记、业务 request_id 幂等和历史归档。即使采用 SDK 子任务管理，也需要保留应用层这些职责。

本节依据：`0.7.34` 的 [AsyncSubAgent 定义与运行参数实现](https://github.com/langchain-ai/deepagents/blob/deepagents%3D%3D0.7.34/libs/deepagents/deepagents/middleware/async_subagents.py)，以及上述项目上下文、Runner 与预算账本实现。

### 6.4 标准拒答与通知元数据

`0.7.33` 默认安装拒答中间件，项目通常无需重复添加。可在异常映射层识别 Core 的公开异常类型：

```python
from langchain_core.exceptions import ModelRefusalError

# 在现有 Runner 异常处理或结果映射中分类，仍执行原有 finally 和收尾。
is_model_refusal = isinstance(exc, ModelRefusalError)
```

是否增加独立业务错误码由项目协议决定，不能绕过现有预算关闭和持久化处理。只针对标准拒绝内容块验证，不把普通文本拒答误归为同一路径。

现有 Runner 对逃出 `invoke()` 的此类异常记录 P1 runtime，状态评估优先按 runtime 判失败，随后才考虑无最终答案；已打开的预算在 finally 中关闭。因此验收重点是标准拒答经过完整主 Agent 图后，异常是否真正传播到 Runner，还是被中间件/图内处理转为正常返回；不能仅验证中间件能抛异常就认定收尾已覆盖。目标 SDK 的子 Agent 路径存在拒答转换逻辑，但当前 `subagents=[]`，不能据子 Agent 行为推断主图会吞掉异常，也不应在未复现前新增异常框架。具体检查见 doc 1 §4.4、§5.4。

在 `langchain-core>=1.7.2` 中，可从 `langchain_core.messages` 导入 `add_notification_metadata` / `get_notification_metadata`，统一标记和识别系统通知；SDK 已用于摘要、固定技能、Rubric 与异步通知。以后消费完整消息流时可借助这些公开辅助函数区分通知和普通用户消息；现有 `dbda_kind` 仍有业务含义，不能未经迁移就删除。当前 Core `1.6.10` 不含该模块，目标 Core `1.7.6` 可用。来源：[Core 1.7.2 发布记录](https://github.com/langchain-ai/langchain/releases/tag/langchain-core%3D%3D1.7.2)。

## 7. 当前 LangChain 已具备的复用机会

本节所列中间件均在项目当前 `langchain==1.4.5` 安装包中存在，属于当前可用而尚未在本 Agent 显式启用的能力，不应算成升级到 `1.4.9` 才新增。

### 7.1 【重点：局部替代】调用次数上限与预算代码

```python
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
)

limiters = [
    ModelCallLimitMiddleware(run_limit=40, exit_behavior="error"),
    ToolCallLimitMiddleware(run_limit=80, exit_behavior="error"),
]
```

它们支持 run / thread 级上限，工具上限还可以按 `tool_name` 限定。若只需要“最多调用 N 次”，可以复用这些中间件，避免自行维护通用计数。

> **【重点：不能用两行配置替换整个 RunBudgetLedger】项目需要在工具并发执行前加锁预占，失败不退额度，并在 SQL 管线内部统一统计 query、探针与分析下推。原生中间件按框架 hook 和图状态计数，语义不完全相同。**

例如，当前预算在 `before_model` 预占，原生模型计数在 `after_model` 更新；摘要内部调用、HTTP 内部重试、工具内的多次数据库访问也不等同于一次框架模型或工具步骤。`ToolCallLimitMiddleware(tool_name="execute_sql")` 不能覆盖探针和下推访问。

建议仅在简化版 Agent 或明确接受通用计数语义的路径使用；若重构主链，应先用实际并发、失败、摘要和超时场景对照计数，保留统一 deadline 与业务 stop_reason 映射。不要让两套计数器同时作为不同的判定来源。

### 7.2 模型重试、工具重试与模型回退

```python
from langchain.agents.middleware import ModelRetryMiddleware, ToolRetryMiddleware

retry_middleware = [
    ModelRetryMiddleware(max_retries=1, on_failure="error"),
    ToolRetryMiddleware(
        max_retries=1,
        tools=["search_schema", "inspect_tables"],
        retry_on=(TimeoutError,),
        on_failure="error",
    ),
]
```

适合可安全重试的瞬时异常。`ModelFallbackMiddleware` 还可在主模型失败时切换备用模型；但必须核对备用模型的工具能力、Harness Profile、上下文容量和摘要阈值。

**当前实现的边界：** 业务工具经常返回 `status="error"` 的 JSON，而不是抛异常，`ToolRetryMiddleware` 不会因为其中写了 `retryable=true` 就自动重试。参数错误、SQL 校验失败需要模型修改参数，不能盲目重放同一次调用。数据库查询重试还涉及额度消耗、重新执行时点和状态写入；重试层级与 `RunBudgetMiddleware` 的先后顺序也会影响每次尝试是否预占额度。

本项主要是补充故障恢复能力，当前未发现需要直接删除的独立 Agent 重试循环。已有模型 SDK 内部重试需一起核对，避免重试次数叠加。

### 7.3 ToolError 与工具选择：可复用，但不替代业务错误协议

`ToolErrorMiddleware` 可统一将选定工具异常转换成模型可见的错误消息，用于减少未来新增工具中的通用异常转换重复代码。当前 `_envelope.py` 承担的 `error_type`、`retryable`、`hint`、输入脱敏和 `Command` 状态更新仍须保留；SDK 不解析这些 JSON 业务字段，也不能自动补齐账本。

`LLMToolSelectorMiddleware` 可先用模型选出相关工具，适合工具数量很大而没有明确技能分组时；它会增加模型调用，并同样与固定工具集合审计冲突。本项目只有默认 15 个工具且已有技能分组，建议先研究 §5.2 的技能按需加载，再考虑额外的模型选择层。

### 7.4 ContextEditing 不应直接叠加到现有摘要链

`ContextEditingMiddleware` / `ClearToolUsesEdit` 可清理历史工具内容，但当前已用 DeepAgents 摘要和结果引用机制。上下文裁剪可能改变技能披露和摘要边界，不能据通用文档就直接启用，更不能把 LangChain 同名 `SummarizationMiddleware` 当作 DeepAgents 中间件的等价替代。优先保留现有摘要，确有上下文压力后再做独立比较。

本节来源：[预置中间件官方文档](https://docs.langchain.com/oss/python/langchain/middleware/built-in)、项目当前 LangChain 安装包源码，以及 §1.1 链接的业务实现。

## 8. 后续版本增量完整索引与建议顺序

### 8.1 0.7.26–0.7.34 发布记录索引

下表补齐前文未单列的修复与性能变化，避免把每个后续版本都理解成增加了一项业务功能。

| 版本 | 主要增量 | 当前适用性 |
| --- | --- | --- |
| [0.7.26](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.26) | 开放 summarizer；异步等待人工状态与父 Trace；保留解析后的工具结果路径 | 摘要器优先评估，其他按功能接入 |
| [0.7.27](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.27) | 主 Agent 获得异步状态更新；修复子 Agent 名称传递 | 未来异步协作 |
| [0.7.28](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.28) | ContextHubBackend.glob 正确使用 path | 当前未使用 ContextHub；可研究技能/上下文版本管理，但不替代业务证据持久化 |
| [0.7.29](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.29) | CompositeBackend 接受无尾斜杠路由；拒绝文本方式编辑二进制；内存 grep 跳过二进制 | 当前已有 CompositeBackend，属于健壮性完善 |
| [0.7.30](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.30) | 异步排队、notify_parent、webhook；取消状态与检查重试；摘要清理关联历史的 reasoning；LocalShell 输出按 UTF-8 字节限额 | 异步需接入；reasoning 修复与模型格式有关；当前不使用 LocalShell |
| [0.7.31](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.31) | 文件中间件权限覆盖修复与公开参数；执行输出保留元数据；默认不在回复中暴露异步任务 ID | 权限参数可直接迁移，输出元数据按使用场景生效 |
| [0.7.32](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.32) | 导入 SDK 时不再立即导入 Anthropic / OpenAI 集成 | 启动导入优化，不等于模型推理加速，实际收益需测量 |
| [0.7.33](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.33) | 默认标准拒答处理；通知元数据；子 Agent task 调用关联；grep 范围与长行窗口；压缩失败返回错误状态；Rubric 退出 Beta | 拒答和错误状态需回归，Rubric 是已有能力成熟化 |
| [0.7.34](https://github.com/langchain-ai/deepagents/releases/tag/deepagents%3D%3D0.7.34) | 用消息 ID 锚定摘要边界 | 条件性可靠性修复，不能替代线程交接策略 |

### 8.2 建议按收益和改造范围推进

1. **范围最小：** 升级时迁移公开权限参数、验证拒答收尾；当前版本即可试用 Todo 和关键技能固定加载。
2. **优先能力验证：** 独立摘要模型；对非结构化长文本比较 SDK 转存与当前硬截断；准备 Memory 的稳定背景内容。
3. **最值得做替代实验：** 技能工具按需加载，以及原生结构化输出承担答案格式生成。两者都先改审计/校验契约，再验证是否减少代码和 token。
4. **面向复杂任务扩展：** 独立 Rubric 评审、同步专业子 Agent、事件流；确有后台需求后再接入异步服务与 webhook。
5. **单独验证语义：** 调用上限替代实验、QuickJS 编排、人工暂停恢复。它们与预算、证据或运行生命周期耦合较深，不能作为简单依赖升级顺带启用；QuickJS PTC 另需先满足 §5.9 的异步入口条件。

### 8.3 本文证据与验证边界

- 版本存在性与增量：官方 PyPI 元数据、`0.7.26`–`0.7.34` 发布记录；最新文档与 Context7 用于发现能力，是否属于 `0.7.25` 已有以该版本源码为准。
- API 与项目适用性：当前 `.venv` 中 `deepagents==0.7.25`、`langchain==1.4.5`；先前评估下载的 `0.7.34` / `1.4.9` / Core `1.7.6` 发布包；`AGENTS.md` 指定的版本源码与项目调用链。
- 示例检查：Python 片段做语法检查；在当前环境与临时新版包优先加载的环境中，使用 Fake 模型离线核对了结构化输出策略、技能、Rubric、调用上限及重试中间件的构造参数；新版环境另核对公开权限、summarizer、逐调用审批和拒答异常导入。该检查未调用真实模型，也不是完整集成验证。
- 文中“可替代”“建议”是结合代码职责得出的工程判断，不是已完成改造或性能结论。QuickJS 属于额外依赖，当前未安装验证。
- 本次只更新调研文档。示例为能力接入片段；没有据此声明真实 Qwen、数据库、暂停恢复、子 Agent 协作或整套替代链路已经验收。
- 扩大依赖范围后的安装、1152 通过/10 个基线同样失败/4 跳过的对照回归，以及内存 checkpoint 跨版本探针，统一以 doc 1 的新证据为准。前述最小组合的构造检查和审核不能自动证明扩大组合的全部行为。
- 两环境原始 pytest XML/日志、探针脚本与安装记录已归档至 [evidence](upgrade/candidate_2026-11-09/evidence/README.md)，其中另列本次 GLM 审核后新增的复核输出；未归档的历史构造检查过程不据此追认已留存一手记录。

### 8.4 Kimi 独立审核后的采纳说明

通过 Orca 复用当前 Kimi 会话审核；Kimi 在该会话中实际调用 Context7 的 `resolve-library-id` / `query-docs`，选定官方库 `/langchain-ai/deepagents`，并对照固定版本源码和当前项目代码。审核反馈由主协调者再次核实后处理。

以下记录来自此前以 DeepAgents 能力为主的两轮审核。扩大依赖目标及新增 §9 是后续修订，不能将前两轮“通过”直接泛化到新范围。

| 第一轮反馈 | 主协调者核实与处理 |
| --- | --- |
| R1-01：异步 context 与预算生命周期的条件风险 | 已从当前 `open_run` / `reserve` / `close_run` 和目标版本转发逻辑确认，补入 §6.3。离线验证确认条目关闭后同 ID 的 reserve 会放行；没有运行真实异步服务。对“monotonic 不可跨进程”的泛化建议作收窄，准确写为跨主机或跨重启不可直接复用。 |
| R1-02：外层审计不能代表最终模型工具绑定 | 已核对中间件顺序、Skills 的披露处理和 LangChain 的 ToolStrategy 最终绑定，修订 §1.1、§5.1、§5.2。区分装配授权、基础请求和最终绑定/执行授权，不把仅修改 expected set 当作完整解决方案。 |
| R1-03：异步排队与通知成熟度标记 | 目标版本源码明确标注 Beta，采纳并补入 §2.2、§6.3。 |

**本次扩大范围审核（与上表历史轮次分开计数，最多三轮）：** 通过 Orca 新启动 Kimi Code，同时审核 doc 1、doc 2 与候选证据，并实际查询 Context7 的 DeepAgents / LangGraph 文档。第一轮确认版本、依赖约束、回归结果与主要替代边界，发现 E1-01：QuickJS PTC 示例遗漏异步入口条件。主协调者已对照固定标签 README、同步/异步 eval 实现和当前 Runner 确认并采纳，补入 §5.9、§8.2；不为可选实验提前改造主链。第二轮复核确认 E1-01 关闭，未发现新增问题，文档审核通过。本次共两轮，无遗留争议，无需第三轮；仍须完成 doc 1 所列正式实施验收。精确版本以固定源码为准，Context7 主分支索引不能单独证明版本归属。

## 9. 扩大升级范围后新增的 LangGraph 与持久化能力

本节依据 LangGraph `1.2.16`–`1.2.20`、checkpoint `4.3.1`、prebuilt `1.1.1`、SDK `0.4.14` 和 checkpoint-postgres `3.2.0` 的发布记录与已安装 wheel 源码。候选可以安装及所选回归没有新增失败，不代表下列能力已经启用或真实持久化场景已验收。

### 9.1 恢复执行修复与严格恢复

| 版本或组件 | 增量 | 对当前项目或未来接入的意义 |
| --- | --- | --- |
| LangGraph 1.2.16 | 修复恢复时 Command 更新重复应用、子图重复运行已完成任务、恢复退出时 pending writes 重复保存；改进无效 resume 的任务处理 | 当前有父子图与 checkpoint，恢复回归值得覆盖；不能据此声称项目已经复现并修复了这些故障 |
| prebuilt 1.1.1 | ToolNode 不再吞掉无效 resume 值 | 接入 §5.7 的审批恢复时能更明确暴露错误；仍需正确处理异常与暂停状态 |
| LangGraph 1.2.20 | 同一任务内每次 `interrupt()` 有自己的 ID；严格恢复可拒绝已经消费或不存在的中断答案 | 适合防止迟到审批误用于后续步骤，应用仍要验证用户、会话和业务归属 |
| SDK 0.4.14 | `runs.create` / `stream` / `wait` 支持 `strict_resume`；StreamMode 增加 tools / lifecycle | 远程 Agent Server 场景可采用严格恢复及相应事件流；不是本地 Runner 自动获得完整恢复协议 |

例如，未来已经具备 Agent Server 和审批接口时，可通过同步 SDK 的公开参数发送严格恢复请求：

```python
# client 来自 langgraph_sdk.get_sync_client()，各 ID 已由应用验证归属。
# 先确认 Agent Server 声明支持 strict_resume，不能只升级客户端。
result = client.runs.wait(
    thread_id,
    assistant_id,
    command={"resume": {interrupt_id: {"type": "approve"}}},
    strict_resume=True,
)
```

该示例采用逐调用审批的 Decision 格式；批量审批仍使用其自身协议，不是任意 resume 值都能照搬。`strict_resume` 也不等于 Web request_id 幂等或授权校验，不能删除当前会话锁、pending 和业务归属检查。当前本地 `agent.invoke()` 路径不能照抄远程 `runs.wait` 的参数；本节不引入对 LangGraph 私有配置键的直接依赖。

来源：[1.2.16](https://github.com/langchain-ai/langgraph/releases/tag/1.2.16)、[1.2.20](https://github.com/langchain-ai/langgraph/releases/tag/1.2.20)、[prebuilt 1.1.1](https://github.com/langchain-ai/langgraph/releases/tag/prebuilt%3D%3D1.1.1)、[SDK 0.4.14](https://github.com/langchain-ai/langgraph/releases/tag/sdk%3D%3D0.4.14)。

### 9.2 【重点：局部复用】PostgresSaver.prune 与现有维护服务

`langgraph-checkpoint-postgres==3.2.0` 新增同步/异步 `prune()`，提供批量线程清理入口。当前 [MaintenanceService](../src/modules/db_deepagent/persistence/maintenance.py) 已实现候选线程发现、保留期、归属检查、会话锁、归档/结果引用维护，底层 `_delete_thread()` 已调用 SDK 的 `delete_thread()`，所以不能把“SDK 可以删除线程”当作本次首次出现的能力。

如果以后需要批量删除已确认退役的线程，可评估复用新接口：

```python
# retired_thread_ids 必须先通过现有归属、保留期与锁检查。
# saver 是应用管理的 PostgresSaver；连接与事务前提需另行验证。
saver.prune(retired_thread_ids, strategy="delete")
```

> **【重点：不可替代整个维护模块】`prune(strategy="keep_latest")` 明确不支持使用 DeltaChannel 的线程，会拒绝执行。当前 DeepAgents messages 正是 DeltaChannel，不能以“只留最新 checkpoint”代替现有活跃线程存储或清理策略。**

`strategy="delete"` 只适合完整删除指定线程，不能替代业务保留期、来源检查、会话锁、归档与结果文件引用清理。批量操作的逐线程事务和错误处理也要与当前连接/事务模型兼容；不要为了使用新接口就重构已有正确的单线程删除路径。

来源：[PostgreSQL checkpoint 3.2.0 发布记录](https://github.com/langchain-ai/langgraph/releases/tag/checkpointpostgres%3D%3D3.2.0)，以及该版 `PostgresSaver.prune` / `AsyncPostgresSaver.prune` 源码。本次没有对真实数据库执行 prune。

### 9.3 DeltaChannel 默认快照变化：本项目 messages 保持显式 50

LangGraph `1.2.18` 把一般 DeltaChannel 默认 `snapshot_frequency` 从 1000 改为 100，通常意味着更频繁的完整快照、更短的重放链和更多存储。自行创建通道时可以显式指定频率，以控制读写折中。

**本项目的实际边界：** DeepAgents `0.7.25` 与 `0.7.34` 的 `DeepAgentState.messages` 均显式传入 `snapshot_frequency=50`，项目 `DbDeepAgentState` 继承它，没有覆盖此值。因此不能把一般默认值变化描述为本项目消息快照从 1000 变 100，更不能据此预测存储节省或推理加速。

`DeltaChannel` 的持久化表示仍标为 Beta。升级、通道类型改变和回退需做旧会话冷读验证；本次内存样本探针通过，不等于真实 PostgreSQL 长会话已全部验证。

来源：[LangGraph 1.2.18](https://github.com/langchain-ai/langgraph/releases/tag/1.2.18)、两版 DeepAgents `graph.py` 与目标 `langgraph/channels/delta.py`。

### 9.4 其他编排与 checkpoint 完善

- `1.2.17` 修复显式 `recursion_limit` 等于默认值时未按预期生效的情况，以及 RemoteGraph 子图流的 namespace。项目显式设置 `recursion_limit=200`，仍需按实际图和候选版本验证，不把一般修复等同于项目当前必现故障。
- `1.2.18` 改进 error_handler 恢复和失败任务流，补齐被 interrupt_after 或递归限制停止时的最后 checkpoint 事件。
- `1.2.19` 改进流事件 namespace、嵌套图消息与暂停工具的状态，增加 ToolNode 工具名的节点元数据。元数据可帮助观测，但不等于现有授权工具清单和业务审计，不应未经验证就替换 `verify_assembly()` 的职责。
- checkpoint `4.3.1` 拒绝 namespace 标签中的 NUL 字节。当前自定义会话 ID 有严格格式检查，但仍应覆盖旧数据和非标准调用路径。

来源：[1.2.17](https://github.com/langchain-ai/langgraph/releases/tag/1.2.17)、[1.2.18](https://github.com/langchain-ai/langgraph/releases/tag/1.2.18)、[1.2.19](https://github.com/langchain-ai/langgraph/releases/tag/1.2.19)、[checkpoint 4.3.1](https://github.com/langchain-ai/langgraph/releases/tag/checkpoint%3D%3D4.3.1)。

### 9.5 依赖升级与功能接入分开验收

doc 1 已将模型 SDK、数据库驱动、SQL 解析、API、Web 和开发工具纳入最新兼容候选；这不意味着本文件已研究每个间接依赖的全部新特性。优先关注能对应当前代码职责的增量，其他更新按维护、API 行为和兼容性验证处理。

**实施时必须同步处理 SQLGlot doctor：** 当前 `cli.py` 硬编码 `sqlglot==30.22.0`，扩大候选为 `30.30.0`，单改依赖会产生 doctor 失败。需要更新该精确版本检查，并继续验证 SQL AST、证据守卫和授权文本一致性；这与 SDK 新能力替代业务代码是不同的工作。

新的 FastAPI/Starlette、DashScope 2.x、Pandas 3.x 等组合已经可安装，并完成 doc 1 所述有限回归；真实服务、UI 和旧 PostgreSQL 会话仍待验收。不能把文档中的新接口示例当作生产入口已经启用，也不能把两轮旧范围审核的通过结论直接用于扩大组合。

上述安装与回归描述的是历史候选。正式实施须保留当前三项新增直接声明并显式决定目标版本，特别是 cryptography `46.0.5 → 50.0.2` 的 Fernet 旧登记密文兼容性。PgVector 验收需覆盖池内不同物理连接及扩池新连接的适配行为；默认 pytest 的 `src/tests` 范围也不能替代 `validation_summary.json` 的完整显式路径列表。细化要求统一见 doc 1 §2.6、§5.2、§5.4、§6；这些仍是待验收项。
