# DeepAgents 0.7.34 升级评估与实施建议

- 评估日期：2026-11-09
- 适用范围：`nl2sql` Python 项目，重点为 `src/modules/db_deepagent`
- 当前版本：`deepagents==0.7.25`
- 目标版本：`deepagents==0.7.34`
- 文档状态：升级评估与实施建议；尚未修改项目实际依赖及业务源码

## 1. 评估结论

可以推进升级，建议采用最小联动升级：更新 DeepAgents 及满足其要求的依赖，并随升级将文件权限参数迁移到公开接口。旧权限参数在目标版本仍兼容，迁移属于建议一并完成的清理。

发布前建议对标准拒绝内容块补充回归测试，验证现有异常收尾。摘要交接的消息 ID 兼容属于未来改写历史消息时的条件风险，不列为本次升级必改项；新增模型拒绝专用错误分类也不是升级前置要求。

本次已核对官方依赖声明，对比 SDK 源码，在临时目录完成依赖解析与新版包加载测试。新版组合下，现有相关测试共 **1011 项通过、4 项跳过**，没有发现需要重构业务工具的兼容问题。

这些结果证明依赖解析和相关单元测试层面的兼容性。真实 Qwen 调用、数据库访问、长对话摘要与历史会话恢复仍需在正式升级后验收，不能仅凭本次结果认定已具备发布条件。

## 2. 升级收益与适用性

相对 `0.7.25`，本次升级对现有查询链最相关的变化包括：

| 变化 | 对本项目的意义 |
| --- | --- |
| 摘要边界增加 `cutoff_message_id` | 摘要边界之前的消息被删除或列表位置发生变化时，只要保留的边界消息 ID 仍可命中，就能定位保留上下文，避免单纯按旧位置计算导致偏移；不能据此保证任意历史内容编辑都与已有摘要一致 |
| 文件中间件支持公开的 `permissions` 参数 | 可以移除当前对 `_permissions` 私有构造参数的依赖 |
| 覆盖文件中间件时保留并检查 Agent 权限 | 自定义文件中间件和 Agent 的权限配置具有更明确的一致性约束 |
| 模型返回标准拒绝内容时默认抛出错误 | 拒绝结果进入明确的异常处理流程，需要验证项目的错误分类和用户侧输出 |
| 摘要中间件开放 `summarizer` 参数 | 后续可独立指定摘要模型或摘要函数，本次可继续保留现有摘要模型策略 |

近期新增的异步子 Agent 排队、状态通知与 webhook 等能力，当前不会直接改变查询链行为。现有实现通过 `subagents=[]` 并禁用默认通用子 Agent，运行单主 Agent。

## 3. 依赖联动范围

### 3.1 需要升级的包

保留现有锁文件，在临时项目中修改必要约束后，`uv lock` 解析成功，共更新以下 8 个包：

| 包 | 当前版本 | 本次验证版本 | 升级原因 |
| --- | --- | --- | --- |
| `deepagents` | `0.7.25` | `0.7.34` | 升级目标 |
| `langchain` | `1.4.5` | `1.4.9` | 新版 DeepAgents 要求 `>=1.4.9,<2.0.0` |
| `langchain-core` | `1.6.10` | `1.7.6` | 新版 DeepAgents 要求 `>=1.7.6,<2.0.0` |
| `langsmith` | `0.14.8` | `0.14.17` | 新版 DeepAgents 要求 `>=0.14.17` |
| `langchain-anthropic` | `1.7.6` | `1.7.10` | 新版 DeepAgents 要求 `>=1.7.10,<2.0.0` |
| `langchain-google-genai` | `4.4.2` | `4.4.3` | 新版 DeepAgents 要求 `>=4.4.3,<5.0.0` |
| `langchain-protocol` | `0.0.19` | `0.0.21` | 新版 Core 要求 `>=0.0.20`；本次锁文件解析选择 `0.0.21` |
| `certifi` | `2026.2.25` | `2026.10.23` | 新版 Core 要求 `>=2026.10.19`；本次锁文件解析选择 `2026.10.23` |

表中“本次验证版本”是已实际解析、加载并测试的组合，不表示每个包都必须使用这一精确版本才能满足上游要求。

即使业务模型使用 Qwen，Anthropic 和 Google 两个集成包仍需升级，因为它们是目标版本 DeepAgents 声明的必需依赖。

### 3.2 可以保留的依赖

以下版本在本次解析及相关测试中保持不变：

| 包或组件 | 保留版本 |
| --- | --- |
| `langgraph` | `1.2.15` |
| `langgraph-checkpoint` | `4.3.0` |
| `langgraph-prebuilt` | `1.1.0` |
| `langgraph-sdk` | `0.4.7` |
| `langgraph-checkpoint-postgres` | `3.1.3` |
| `langchain-openai` | `1.7.1` |
| `openai` | `3.29.0` |
| `langchain-community` | `0.4.2` |
| `langchain-classic` | `1.0.8` |
| `langchain-text-splitters` | `1.1.3` |

数据库驱动、Milvus、Neo4j、SQLGlot、FastAPI 等依赖也无需因本次升级而调整。目标版本支持 Python `>=3.11,<4.0`，与项目要求的 Python `>=3.12` 兼容；本次测试解释器为 Python `3.12.13`。

### 3.3 项目依赖文件的修改建议

在 `pyproject.toml` 中修改以下 4 处版本约束，并更新相关注释：

```toml
[project]
dependencies = [
    # 仅列出需要调整的条目，其余依赖保留。
    "langchain==1.4.9",
    "deepagents==0.7.34",
]

[project.optional-dependencies]
monitoring = [
    "langsmith==0.14.17",
    # 其余条目保留。
]

[tool.uv]
constraint-dependencies = [
    "langchain-core==1.7.6",
    # 其余约束保留。
]
```

以上只是需要修改条目的示意，不能整体替换现有配置段。

`langsmith` 虽然显式锁定在 `monitoring` 可选依赖中，但 DeepAgents 本身也依赖它，旧的 `0.14.8` 约束必须同步更新，否则会与新版要求冲突。

其余联动包通过重新生成 `uv.lock` 固定。保留原锁文件并进行定向更新，检查最终差异，避免无关依赖随之升级。

## 4. 代码影响与修改建议

### 4.1 文件系统中间件：迁移公开权限参数

位置：`src/modules/db_deepagent/agent.py` 的 `build_filesystem_middleware()`。

当前使用：

```python
FilesystemMiddleware(
    backend=backend,
    tools=list(_FILE_TOOLS),
    tool_token_limit_before_evict=cfg.filesystem.tool_token_limit_before_evict,
    _permissions=permissions,
)
```

建议将 `_permissions=permissions` 改为 `permissions=permissions`。

旧参数在 `0.7.34` 中仍可使用，但从 `0.7.31` 起已弃用，计划在 `0.9.0` 移除。新旧参数不能同时传入。

新版在自定义 `FilesystemMiddleware` 覆盖默认实例时，会继承 Agent 权限，并拒绝冲突的非空规则。项目当前向 Agent 和文件中间件传递同一份权限规则，本次装配和权限测试通过。

建议本次一并完成参数迁移，验证只读路径、隐藏文件拒绝、写入拒绝，以及默认 15 个工具的集合保持符合项目约定。

### 4.2 摘要交接：条件风险与可选兼容

位置：`src/modules/db_deepagent/agent.py` 的 `read_summary_for_handoff()`。

当前函数读取 `_summarization_event`，并要求 `cutoff_index` 不超过当前消息数。新版摘要事件增加可选字段 `cutoff_message_id`，SDK 优先按消息 ID 定位边界，找不到 ID 时回退到旧索引。

当前调用路径与现有读取方式兼容，无需为升级强制修改交接函数。`runner.py` 只在线程轮换时通过 `_handoff()` 调用该函数，交接函数只读取摘要文本，并不负责为模型重建上下文。

项目使用 DeepAgents 的 `SummarizationMiddleware`：摘要事件记录在私有状态中，原始 `messages` 日志保留；它与会使用 `RemoveMessage` 重写历史的 LangChain 同名中间件不同。当前项目代码中未发现 `RemoveMessage`、`REMOVE_ALL_MESSAGES`、`trim_messages` 或主动缩短原线程消息列表的调用；维护服务按线程清理 checkpoint，也不是删除保留线程内的前序消息。因此，本次未发现常规运行能触发旧摘要索引超过消息数的路径。

本次仅用人工构造的摘要事件和缩短后的消息列表验证了以下条件性差异，并非复现真实业务链路产生的故障：

1. 摘要事件保留旧的 `cutoff_index=5`，并记录一个有效的 `cutoff_message_id`。
2. 人工模拟删除摘要边界之前的历史，当前消息列表只剩 1 条，消息 ID 仍可命中。
3. 新 SDK 能正确恢复“摘要 + 保留消息”。
4. 项目交接函数抛出 `AssemblyVerificationError`，原因是旧索引超过当前消息数。

只有未来引入历史消息裁剪或外部改写 checkpoint 消息列表等能力时，才需要重新评估此差异。届时可做最小兼容：只有 `cutoff_message_id` 在当前消息中实际命中时，才按命中的位置检查边界；ID 缺失或未命中时仍回退旧索引并保留结构校验，不能仅凭字段存在就跳过校验。

本次保留现有摘要与交接回归测试；若未来实施上述兼容，再补充命中 ID 和回退索引的针对性用例。读取摘要文本的职责保持不变，不新增边界模型，也不把 SDK 私有状态直接复制到新线程。真实摘要生成和持久化恢复仍属于第 5.3 节待验收范围，与本节人工构造验证不同。

### 4.3 模型拒绝：验证错误分类与返回结果

位置：`src/modules/db_deepagent/runner.py`，以及 Web API v2 的结果映射。

新版默认添加 `ModelRefusalMiddleware`，对标准拒绝内容块抛出 `ModelRefusalError`。它不会仅凭普通文本推断模型拒绝。

当前 Runner 已捕获执行异常并归类为运行失败，可以进入收尾流程。建议作为升级行为回归，人工构造标准 `refusal` 内容块，验证新增中间件抛出异常后：

- 不会被误判为查询成功或普通的“未提交最终答案”。
- 错误信息、模型调用观测与预算收尾保持一致。
- Web 返回和日志能清楚表达失败原因。

该回归测试无需调用真实模型。真实模型验收时，仅当模型返回标准拒绝内容块，才验证这一异常路径；不要求 Qwen 必须产生该块，也不把普通文本拒答认定为同一行为。是否单独增加模型拒绝错误分类，可根据产品错误协议决定，不是依赖升级的前置要求。

### 4.4 已核对且未发现必须修改的部分

对比 `0.7.25` 与 `0.7.34` 的 SDK 源码后：

- `_models.py` 和 Harness Profile 解析文件未变化，项目使用的模型标识与 Profile 私有接口仍兼容。
- `PatchToolCallsMiddleware` 源码未变化。
- `CompositeBackend` 构造签名保持兼容。
- `DeepAgentState` 继承、消息 `DeltaChannel`、工具 Schema 和默认工具集合通过现有测试。
- 现有业务工具无需因本次升级重构。

私有接口仍需在后续升级中继续核对；本次兼容不代表上游承诺长期稳定。

## 5. 已完成的验证与边界

### 5.1 验证方式

1. 从 PyPI 获取目标版本元数据和发布 wheel，核对实际依赖与 API。
2. 将项目 `pyproject.toml`、`uv.lock` 复制到临时目录，修改必要约束后执行 `uv lock`。
3. 将上述 8 个新版包安装到临时目录，通过 `PYTHONPATH` 优先加载；其余依赖沿用原虚拟环境。
4. 确认实际加载的 DeepAgents 为 `0.7.34`，LangChain 为 `1.4.9`，Core 为 `1.7.6`。
5. 分别运行当前版本基线与新版组合的相关测试。

这不是一次从空环境执行的完整 `uv sync` 安装验证。项目原虚拟环境中的包未被替换。

### 5.2 测试结果

| 环境 | 测试范围 | 结果 |
| --- | --- | --- |
| 当前版本基线 | `src/tests/db_deepagent/unit/` | 602 通过，4 跳过 |
| 新版本组合 | `src/tests/db_deepagent/unit/` 与 `src/tests/unit/api/v2/` | 779 通过，4 跳过 |
| 新版本组合 | 旧父图、SQL 子图与 LLM 工厂单元测试 | 232 通过 |

新版组合合计 **1011 通过、4 跳过**；当前版本基线不重复计入新版总数。

首次基线执行遇到本机 SOCKS 代理配置与缺失 `socksio` 的环境问题。在测试子进程中移除代理环境变量后，基线通过；新版对照使用同样处理。这一问题并非升级引入，也没有因此修改项目依赖。

### 5.3 尚未覆盖的验证

- 在全新虚拟环境中按新锁文件完成完整安装。
- 真实 Qwen 模型的工具调用、超时和摘要调用；标准拒绝异常路径尚未做专项验证，不能假定普通文本拒答会产生标准内容块。
- PostgreSQL、Milvus、Neo4j 的真实业务链路。
- PostgreSQL 持久化中的旧会话恢复、摘要状态兼容与线程轮换。
- 固定验收题集的端到端结果。

### 5.4 临时证据位置

本次依赖解析副本、下载源码和测试日志位于：

```text
/tmp/insightweave-deepagents-eval-rpqywuqe/
├── pyproject.toml
├── uv.lock
├── site/                        # 8 个新版包及其发布元数据
├── wheels/                      # 下载并解压的 SDK 与依赖源码
├── baseline-tests.log
├── baseline-tests.xml
├── upgrade-tests.log
├── upgrade-tests.xml
├── legacy-upgrade-tests.log
└── legacy-upgrade-tests.xml
```

`/tmp` 中的文件可能被系统清理。正式实施时应重新生成并保存 CI 或升级记录，不将临时文件作为长期验收依据。

临时 `pyproject.toml` 是当时的验证副本，不是实施模板；其中 monitoring 注释仍残留 `langsmith>=0.14.8` 的旧说明，正确要求为 `>=0.14.17`，依赖条目本身已是 `langsmith==0.14.17`。该注释不影响解析结果，保留原副本作为评估证据；正式实施应以第 3.3 节为准修改项目文件并更新注释，不直接覆盖为临时副本。

## 6. 建议实施顺序与验收条件

1. 保存升级前的依赖文件和可恢复的运行状态，明确本次代码变更范围。
2. 修改 4 处版本约束，生成并检查新锁文件，确认包变更范围与本次评估一致。
3. 建议将文件中间件迁移到 `permissions` 参数，并补充人工构造标准拒绝内容块的回归测试；保留现有摘要交接实现与测试，不强制引入第 4.2 节的条件性兼容改造。
4. 在隔离的新虚拟环境中按锁文件安装并运行相关回归测试。
5. 执行真实模型及数据库验收，重点覆盖单表、多表 JOIN、时间和维度证据、结果分析、多轮追问、摘要与线程轮换。
6. 验证升级前已存在的持久化会话能够恢复，再按既有发布流程部署。

单元测试可沿用本次已验证范围；真实集成验收参考：

- `src/tests/db_deepagent/integration/test_model_capability.py`
- `src/tests/db_deepagent/integration/test_model_summary_window.py`
- `src/tests/db_deepagent/integration/test_business_cases.py`
- `src/modules/db_deepagent/config/acceptance_cases.yaml`

建议发布验收条件：依赖安装成功，相关测试通过，权限限制有效，旧摘要和旧会话可恢复，固定题集结果符合既有标准。标准拒绝内容块的异常收尾通过构造用例验证；真实模型若返回该块，再核对实际链路，不将普通文本拒答或新增专用错误分类作为这一项的前提。

如需回退，恢复升级前的依赖文件和配套代码，并重建运行环境。新版本写入的 checkpoint 能否由旧版本读取，尚未在本次评估中验证；正式发布前应使用测试会话演练恢复流程。

同时更新 `AGENTS.md` 中的目标 SDK 版本和源码参考，以及相关设计文档中已不再适用的版本或私有接口说明。

## 7. 官方参考

- [DeepAgents 0.7.34 发布记录](https://github.com/langchain-ai/deepagents/releases/tag/deepagents==0.7.34)
- [目标版本依赖声明](https://github.com/langchain-ai/deepagents/blob/deepagents==0.7.34/libs/deepagents/pyproject.toml)
- [PyPI 0.7.34 元数据](https://pypi.org/pypi/deepagents/0.7.34/json)
- [摘要消息 ID 边界变更：PR #7567](https://github.com/langchain-ai/deepagents/pull/7567)
- [文件中间件权限保留与公开参数：PR #7166](https://github.com/langchain-ai/deepagents/pull/7166)
- [模型拒绝等行为变更：0.7.33 发布记录](https://github.com/langchain-ai/deepagents/releases/tag/deepagents==0.7.33)

API 文档通过 Context7 查询；最终依赖要求与接口兼容性以本次获取的目标版本发布元数据和 wheel 源码为准。
