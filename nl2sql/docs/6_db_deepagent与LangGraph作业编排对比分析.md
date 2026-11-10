# db_deepagent 与 LangGraph 作业编排对比分析

## 1. 文档目的与分析基线

本文用于回答以下问题：

1. `db_deepagent` 从接收问题到生成最终答案的完整流程是什么，其 tools、skills、middleware 和 runner 如何协作？
2. 旧 LangGraph 父子图如何编排简单问题、复杂问题、SQL 生成、校验、执行与总结？
3. `db_deepagent` 能否完美、无损地替代旧 LangGraph 作业？
4. 如果不能，当前还存在哪些功能、可靠性、兼容性和性能方面的缺口或设计差异，后续应优先处理什么？

本文以 **2026-11-10 的工作区代码**为分析基线，依赖版本锚点为 `deepagents==0.7.34`，见 `pyproject.toml:36` 和 `uv.lock:639-640`。后续代码或依赖变化后，应重新核对本文结论。

`docs/upgrade/*.html` 和 `docs/v4/*` 作为相对可信的设计与历史验收资料，用于辅助理解；其他可能脱节的文档不作为判断当前行为的最终依据。文档与代码冲突时，以代码为准。

## 2. 结论摘要

**`db_deepagent` 已基本接管当前 v2 Web 主链，但它不是旧 LangGraph 作业的完美、无损、drop-in 替代。**

它在以下方面明显强于旧链：

- 通过工具完成可信的 SQL 校验和执行授权；
- 提供只读事务、语句超时、结果行数上限和明确的源头截断标记；
- 支持结果落盘、分页读取、结果分析和证据追溯；
- 支持多轮对话中的证据复用；
- 提供多源会话绑定、运行预算和更严格的持久化与归档处理。

但它仍存在七类主要替代缺口或差异：

1. 缺少与旧 Summarizer 同级的项目层最终答案兜底，SQL 成功后仍可能无法生成答案；
2. 缺少旧 Complex Path 那种针对**已识别、已规划子任务**的代码级终态门控；
3. 工具输出超长时可能硬截断为非法 JSON；
4. 时间表达解析覆盖范围比旧 LLM 解析窄，这是已知设计差异，是否属于必须补齐的功能缺口取决于产品承诺；
5. 固定检索管线变成模型按需调用，完整性主要依赖提示词和 Skill；
6. 返回协议、旧 CLI 和独立 SQL 子图调用语义不能由新结果类型直接兼容；
7. 旧链可配置的批次内 SQL **执行**并发没有被确定性承接，新 Web 每轮重复构造服务和 Agent，性能与成本缺少对照验证。

因此，更准确的定位是：**DeepAgents 替代了手写父子图的业务编排拓扑，但底层仍使用 LangGraph 运行时与 checkpoint；旧 CLI、独立 SQL 子图和确定性对照流程仍有保留价值。** 当前不默认新增旧 v1 API 兼容层，是否需要恢复、冻结或退役未挂载的旧 API 模块，应单独裁定。

## 3. 两套实现的定位

### 3.1 旧 LangGraph 作业

旧实现采用显式父子图：

- 父图负责问题分流、复杂问题规划、参数注入、子查询调度、SQL 执行、完成度判断和最终总结；
- SQL 子图负责单个查询的问题解析、Schema 检索、SQL 生成和校验；
- 业务步骤、状态字段、循环条件和执行并发边界都由项目代码显式定义。

核心入口：

- `src/modules/nl2sql_father/graph.py:355`
- `src/modules/sql_generation/subgraph/create_subgraph.py:90`

### 3.2 新 db_deepagent 作业

新实现将业务步骤交给 Agent 自主决策：

- Agent 根据用户问题、上下文、工具结果和 Skill 指导决定下一步；
- 工具负责 Schema 检索、时间与维度证据、SQL 校验执行、结果读取分析；
- Runner 负责会话与线程解析、Agent 调用、状态与账本读取、结果评估、归档和本地诊断；
- 最终答案通过 `submit_final_answer` 提交。

核心入口：

- `src/modules/db_deepagent/agent.py:458`
- `src/modules/db_deepagent/runner.py`

需要注意：这并不等于“去掉了 LangGraph”。DeepAgents 的 Agent 执行仍建立在 LangGraph 运行时之上，只是不再由项目手写旧父子图的业务拓扑。

## 4. db_deepagent 完整流程

### 4.1 Agent 装配

默认配置下，`src/modules/db_deepagent/agent.py:458` 装配了：

- 10 个业务工具；
- 1 个单独追加的最终答案提交工具 `submit_final_answer`；
- 4 个只读文件工具；
- 合计 15 个工具。

`features.todo_list=true` 时还会通过 `TodoListMiddleware` 增加 `write_todos`，因此“15 个”是默认配置口径，不是所有配置下恒定不变的数量。业务工具清单见 `src/modules/db_deepagent/tools/__init__.py:32-49`，提交工具的单独追加见 `src/modules/db_deepagent/agent.py:547`。

10 个业务工具覆盖四类能力：

1. Schema 检索、表结构检查与 Join 规划；
2. 时间范围、维度成员和历史证据处理；
3. SQL 校验与受控执行；
4. 已保存结果的读取和分析。

另有 1 个 `submit_final_answer` 工具负责最终答案及证据提交，它不属于上述 10 个业务工具。

同时提供三个 Skill：

- `query-planning`：指导问题拆解、执行顺序和完成度思考；
- `schema-retrieval`：指导 Schema 发现、表结构确认和字段选择；
- `result-analysis`：指导结果读取、分析和最终回答。

Skill 本质上是模型可加载的操作指导，不是由 Runner 强制执行的固定状态机。模型是否加载、何时遵循以及是否完整执行，仍取决于 Agent 的推理过程。

### 4.2 装配与 Runner 的职责边界

在 Web 路径中，services、连接池、persistence、session 目录和 Agent 在 Runner 之前装配，见 `src/api/v2/runs.py:854-902`；请求结束时关闭 persistence 和 services，见 `src/api/v2/runs.py:921-929`。不能把这些前置装配工作归入 Runner P0。

`src/modules/db_deepagent/runner.py` 将一次运行组织为以下阶段：

#### P0：会话、来源与线程准备

- 校验会话和请求标识，并检查本地请求冲突；
- 在会话锁上下文中读取会话记录，核对请求、服务与会话的来源绑定；
- 解析或轮换线程，读取已有 checkpoint、摘要与携带证据。

代码依据：`src/modules/db_deepagent/runner.py:265-354`。此阶段不负责构建 services、session 目录或 persistence。

#### P1：打开运行预算并调用 Agent

- 构造本次运行上下文和审计观察器；
- 通过 `open_run` 打开模型、工具、数据库执行和时限预算；
- 调用 Agent，由 Agent 自主决定检索、SQL 生成、校验、执行、读取结果和分析的顺序；
- 工具返回结构化结果，作为后续推理和最终答案的证据来源。

预算账本在 P1 打开，见 `src/modules/db_deepagent/runner.py:370-420`，不是在 P0 建立。

#### P2：读取状态与运行账本

- 通过 `get_state` 读取 checkpoint 中的消息和各业务账本；
- 后续从账本组装本次运行的 SQL、执行结果、分析和证据；
- Runner 不只依赖模型最后一段自然语言，而是根据实际账本和最终提交记录评估运行。

代码依据：`src/modules/db_deepagent/runner.py:422-456`。

#### P3：结果评估与归档

- 结合运行失败、预算状态、最终提交和证据判定 `status`；
- 生成 `DbAgentResult` 及 `RunStats`；
- 保存运行结果、证据和相关归档信息。

代码依据：`src/modules/db_deepagent/runner.py:557-707`。

#### P4：本地诊断

- 写入本地运行记录；
- 对失败、异常或未完成状态保留诊断信息，辅助定位 Agent、工具或持久化阶段的问题。

### 4.3 一次典型查询的逻辑顺序

一次成功查询通常表现为：

```text
用户问题与对话上下文
        ↓
Web/CLI 装配 services、persistence 与 Agent
        ↓
Runner P0：校验会话、绑定来源、解析线程与历史状态
        ↓
Agent 理解问题，必要时加载 query-planning
        ↓
按需检索 Schema，检查候选表和字段
        ↓
按需查询时间、维度等证据
        ↓
生成 SQL
        ↓
调用校验/执行工具，获得可信执行结果
        ↓
保存并读取结果，必要时加载 result-analysis
        ↓
组织答案并关联 evidence_id、findings
        ↓
单独调用 submit_final_answer
        ↓
Runner 读取状态和账本、评估、归档并返回 DbAgentResult
```

这条路径是工具与提示词共同引导的典型路径，不是旧父子图那种每个节点都必然执行的固定流水线。

## 5. 旧 LangGraph 作业流程

### 5.1 父图编排

旧父图位于 `src/modules/nl2sql_father/graph.py:355`。

#### 简单问题（Fast Path）

```text
router
  ↓
simple_planner
  ↓
SQL 子图
  ↓
sql_exec（生成失败时跳过）
  ↓
summarizer
```

简单问题经过路由后，由简单规划器形成执行所需状态，再进入 SQL 子图。SQL 执行完成后，由 Summarizer 生成最终答案。Fast Path 在执行后直接进入 Summarizer，**不经过 `check_completion`**，见 `src/modules/nl2sql_father/graph.py:317-330`。

#### 复杂问题（Complex Path）

```text
planner（仅入口执行一次）
  ↓
inject_params
  ↓
sql_gen_batch
  ↓
sql_exec
  ↓
check_completion
  ├─ 未完成：回到 inject_params，注入依赖/参数后生成并执行下一批 SQL
  └─ 已完成：summarizer
```

实际回边是 `check_completion → inject_params`，见 `src/modules/nl2sql_father/graph.py:294-314、447-455`。循环不会重新运行 Planner，因此不会在每轮重新拆解原始问题。

复杂问题由 Planner 一次性拆解子查询和依赖关系。系统按批次生成并执行 SQL，然后检查已规划子查询的终态。只有这些子查询全部进入 `completed` 或 `failed` 后，才进入最终总结。

这一设计提供了明确的代码级门控：即使部分子查询失败，也不会在仍有**已规划**子查询未处理时直接结束。但它不保证 Planner 最初已经识别用户的全部意图；如果规划阶段漏拆，门控同样可能通过。

### 5.2 SQL 子图

旧 SQL 子图位于 `src/modules/sql_generation/subgraph/create_subgraph.py:90`：

```text
question_parsing
  ↓
schema_retrieval
  ↓
sql_generation
  ↓
validation
  ├─ 可重试失败：回到 sql_generation
  ├─ 不可恢复或达到上限：失败退出
  └─ 成功：退出子图
```

其特点是：

- 每个子查询固定经过问题解析、Schema 检索、SQL 生成和校验；
- `max_iterations: 3` 包含首次生成，因此最多进行 **3 次生成，即首次 + 最多 2 次重新生成**；
- `iteration_count` 在每次生成后加 1，`should_retry` 仅在小于上限时回到生成节点；
- SQL 生成节点自身另有默认 `max_attempts=3` 的 LLM 调用重试；
- 子图有明确的输入输出协议，可独立于父图承担 SQL 生成和验证任务。

代码依据：

- `src/modules/sql_generation/config/sql_generation_subgraph.yaml:235-238`
- `src/modules/sql_generation/subgraph/nodes/sql_generation.py:68-71、566-570`
- `src/modules/sql_generation/subgraph/create_subgraph.py:70-87`

### 5.3 调度与总结

旧链的两个重要确定性特征是：

1. **SQL 生成串行，SQL 执行可并发**：`sql_gen_batch_wrapper` 串行调用 SQL 子图，见 `src/modules/nl2sql_father/graph.py:174-179、203-204`；执行节点仅在待执行 SQL 多于一条且 `enable_parallel=true` 时使用 `ThreadPoolExecutor`，见 `src/modules/nl2sql_father/nodes/sql_execution.py:184-195`。默认配置开启执行并发，最大并发数为 3，见 `src/modules/nl2sql_father/config/nl2sql_father_graph.yaml:73-83`。
2. **Complex Path 在已规划子查询到达终态后总结**：该保证由父图状态提供，而不是完全依赖模型自行宣布完成；Fast Path 没有这一完成度节点。

旧执行节点还会跳过已经有 `execution_result` 的子查询，避免 Complex Path 后续批次重复执行，见 `src/modules/nl2sql_father/nodes/sql_execution.py:152-160`。

## 6. 核心逻辑差异

| 对比维度 | 旧 LangGraph 父子图 | db_deepagent |
| --- | --- | --- |
| 编排方式 | 项目代码显式定义节点和边 | Agent 根据上下文自主决定工具调用 |
| 底层运行时 | LangGraph | DeepAgents，底层仍使用 LangGraph |
| 问题拆解 | Complex Path 的 Planner 一次生成明确子查询及依赖 | 模型在 Skill 指导下隐式或显式规划 |
| 检索流程 | 每个子查询固定经过问题解析和 Schema 检索 | 模型按需检索，可能复用已有证据 |
| SQL 重试 | 子图有明确回边和次数限制，生成节点另有 LLM 重试 | 主要由 Agent 根据工具反馈决定是否重新生成或校验 |
| 完成度判断 | Complex Path 检查已规划子查询终态；Fast Path 无此节点 | 主要由 Agent 判断并提交，无同级的已识别子任务终态门控 |
| 并发 | SQL 生成串行；执行并发可配置，默认开启 | 取决于 Agent 和工具调用方式，无同等的确定性执行批次调度 |
| SQL 执行安全 | 有语句超时；无只读事务和实际生效的结果行数上限 | 只读事务、语句超时、结果上限、源头截断标记 |
| 结果处理 | 执行结果进入父图状态并总结 | 结果落盘，可分页读取、分析和追溯 |
| 最终输出 | 旧 summary、子查询和执行结果协议 | `DbAgentResult`、证据和 findings |
| 可预测性 | 流程确定，便于逐步验证 | 灵活性更高，但行为更依赖模型 |
| 入口与兼容 | 保留旧父图 CLI 和独立 SQL 子图 CLI；旧 API 模块当前未挂载 | 面向当前 v2 Web、新 CLI 和 Agent 结果协议 |

## 7. 替代缺口、缺陷与设计差异

### 7.1 高优先级：模型失败后的项目层最终答案兜底不足

旧链的两类保障需要区分：

- SQL 生成节点有默认 `max_attempts=3` 的 LLM 调用重试，但没有同级的 SQL 模板答案兜底；
- Summarizer 有项目层 LLM 重试，并在失败时使用执行结果生成模板摘要，见 `src/modules/nl2sql_father/nodes/summarizer.py:113-119、181-262`。

新链并非“完全没有重试或提醒”：

- 走 ChatOpenAI 兼容模式且未覆盖配置时，底层 OpenAI 客户端默认 `max_retries=2`，主要提供传输层重试；
- `FinalAnswerGateMiddleware` 在模型自然结束但未提交时，按默认 `final_answer_reminders: 1` 提醒一次，见 `src/modules/db_deepagent/middleware/final_answer_gate.py:43-88`。

这些机制都不等价于旧 Summarizer 的项目层模板兜底，也不会根据执行账本生成降级答案。

特别是：**SQL 已经成功执行，并不代表最终一定能返回答案。** 如果后续模型调用失败、最终答案未提交或提交过程异常，新 Runner 可能返回 `failed` 和空 `answer`，见 `src/modules/db_deepagent/runner.py:581-707`。

不同入口的呈现需要区分：

- v2 Web 实时响应使用 `answer=result.answer or ""`，可能为空，见 `src/api/v2/dto.py:283-299`；
- Web 历史轮次会写入“本轮未生成答案（状态：……）”等通用状态文案，见 `src/api/v2/runs.py:450-456`；
- 新 CLI 会显示“（无答案）”，见 `src/modules/db_deepagent/cli.py:489-493`。

因此，问题不是历史或 CLI 完全没有文字，而是**缺少基于已成功执行账本的结果说明**。

建议：

- 为关键模型调用增加明确、有限的项目层重试，并与底层传输重试区分观测；
- 在已有成功 SQL 证据但最终提交失败时，根据执行账本生成降级答案；
- 推荐采用 `partial + 降级答案 + errors/warnings + evidence`，明确表达“有结果但最终生成未完成”，不能伪装成 `completed`；
- 如果产品要求任何此类异常都保持 `failed`，则至少保留并展示证据及状态说明。`partial` 还是 `failed` 是返回语义的产品决策，但两种方案都应避免实时响应空答案。

### 7.2 高优先级：缺少针对已识别子任务的代码级终态门控

`submit_final_answer` 当前验证：

- 必须单独调用，同一条模型消息不能同时调用其他工具；
- 同一运行不得重复提交；
- findings 引用的 `evidence_id` 必须属于本运行的有效执行、分析或沿用证据；
- 仅当 `status=completed` 时强制 `findings` 非空。

代码依据：`src/modules/db_deepagent/tools/final_answer.py:67-121`。Runner 还会把“模型报 `completed` 但本次无任何成功证据”的情况改判为 `failed`，见 `src/modules/db_deepagent/runner.py:699-706`。

这些检查保证提交结构和证据有效，但不检查所有已识别子任务是否都已处理并回答。新链没有等价于旧 Complex Path `check_completion` 的门控。

需要准确理解旧链的保证范围：

- 旧链只保证**已规划子查询**进入 `completed/failed`，不保证 Planner 没有漏拆用户意图；
- 该门控只在 Complex Path 生效，Fast Path 没有；
- 因而新旧链都不能仅凭这一层机制保证“用户所有独立子问题都被识别”。新链缺少的是旧 Complex Path 已有的“已规划任务终态”这一级保证。

例如，Agent 已识别三个独立子任务，但只回答其中两个，只要提交结构和证据合法，仍可能得到 `completed`。

建议增加轻量子任务账本：

- 在 `query-planning` 阶段显式写入已识别子任务，而不是提交时由模型事后自报；
- 记录每个子任务的证据、执行状态和最终答案覆盖位置；
- 提交前检查是否存在已识别但未到达终态、或未被子任务答案覆盖的项目；
- 对无法回答的子任务要求显式标记失败或说明原因；
- 将“规划是否漏拆”作为单独的语义质量问题评估，不把终态门控误当成完整意图识别器。

### 7.3 高优先级：工具超长输出会被截断为非法 JSON

问题位于 `src/modules/db_deepagent/tools/_envelope.py:79-114`。

当前硬截断直接缩短整个序列化字符串。截断后可能丢失字符串结尾、对象括号或数组括号，使工具返回内容不再是合法 JSON。

初次分析使用受控的 `300` 字符上限复现：

```text
{'length': 300, 'warnings': ['输出超过 300 字符上限，整体截断']}
json_valid=False JSONDecodeError
```

本次复核又在生产配置相同的 `12000` 字符上限下，使用单个超宽表的嵌套 `columns` 载荷复现：

```text
{'length': 12000, 'warnings': ['输出超过 12000 字符上限，整体截断']}
{'json_valid': False, 'error': 'JSONDecodeError', 'message': 'Unterminated string ...'}
```

生产默认上限见 `src/modules/db_deepagent/config/db_deepagent.yaml:127-129`。这说明缺陷并非只在人为设置的低上限下可达，超宽表、嵌套长字段或长文本结果仍可能触发，因此保持高优先级。

当前代码已有部分缩减规则：截短 `card_text/sql`，并保留 `tables/columns/dim_values/sql_examples/results` 等顶层列表的前 5 项，见 `_envelope.py:97-107`。但缺口仍包括：

- `tables[].columns` 等嵌套大字段；
- `read_result` 返回的 `rows`，见 `src/modules/db_deepagent/tools/result_tools.py:78-90`；
- 缩减后仍超长时的硬截断兜底；
- 硬截断后 `_merge_warnings` 因 JSON 解析失败而直接返回原文，截断 warning 无法并回 envelope，见 `src/modules/db_deepagent/tools/schema_tools.py:69-84`。

建议：

- 扩展现有结构化缩减规则，覆盖嵌套大字段和 `rows`，而不是只增加一个顶层字段白名单；
- 不要直接截断完整 JSON 文本；若结构化缩减后仍超限，返回最小但合法的降级 envelope；
- 保留合法的 `status`、`warnings`、明确的 `truncated` 标记和必要元数据；
- 增加超宽表、长文本行、嵌套字段和生产 `12000` 上限测试，断言输出可解析且不超过预算；对极小上限另行明确契约。

### 7.4 已知设计差异：时间表达覆盖范围收窄，是否补齐待产品裁定

旧链使用 LLM 进行时间解析，能够处理更开放的自然语言表达。新链的确定性解析更安全，但明确不支持或要求澄清部分表达，例如：

- “前天”；
- “最近三周”；
- “最近三个月”；
- “最近一年”。

代码明确写着“最近 N 周/个月/年第一阶段不映射”，并要求确认具体日期范围，见 `src/modules/db_deepagent/services/time_resolver.py:70-79、245-305`。因此这首先是一个**有意的安全设计与语义覆盖差异**，不能直接描述成无意引入的实现 bug。

裁定口径：

- 如果产品承诺兼容旧链的全部常用自然语言时间表达，它构成需要排期的功能缺口；
- 如果产品接受缩小文法并以澄清换取确定性，则应记录为已知差异和澄清行为，不要求无条件补齐。

无论采用哪种口径，都应明确支持的文法、默认时区和日期边界；对仍有歧义的表达继续要求澄清，不应为了兼容而猜测。

### 7.5 中优先级：固定检索管线变成提示词约束

旧 SQL 子图中的 `question_parsing → schema_retrieval → sql_generation → validation` 是代码强制路径。

新链中，`inspect_tables` 等操作是否充分执行，主要依靠系统提示词和 Skill 指导。模型可能：

- 跳过必要的表结构检查；
- 在信息不足时直接生成 SQL；
- 对多表问题只检查部分候选表；
- 过度复用历史证据，忽略当前问题的差异。

按需检索带来了效率和灵活性，但也降低了流程完整性的确定性。

建议：

- 在 `validate_sql` 前或工具内部解析 SQL 涉及的表，检查这些表是否已出现在本运行的 `search_schema/inspect_tables` 证据或合法的沿用证据中；
- 缺少必要证据时返回可重试错误，明确要求先检查表结构，而不是只依赖提示词；
- 为多表、多指标和历史上下文复用增加针对性测试。

### 7.6 中优先级：旧协议和独立子图语义不兼容

新返回类型 `DbAgentResult` 不能直接替代旧调用方期待的：

- `summary`；
- `sub_queries`；
- `execution_results`；
- `dependency_graph`；
- 其他旧父图状态字段。

此外，旧 SQL 子图可承担“只生成并验证 SQL、不执行”的独立任务；新 `db_deepagent` 面向完整查询、执行和分析流程，不是该入口的等价替代。

当前入口现状也需要区分：

- `src/api/main.py:128-131` 只挂载 v2 router；
- `src/api/routers/query.py:22-36` 的旧 v1 查询路由仍调用旧父图，但当前未挂载；
- 仍可用的旧入口主要是 `scripts/nl2sql_father_cli.py` 和 `scripts/nl2sql_subgraph_cli.py`。

因此，Web v2 主链可用不代表旧 CLI、独立 SQL 子图或旧调用方协议已经无损迁移；也不能把未挂载的旧 API 模块描述成当前仍对外服务的接口。

建议：

- 当前优先保留旧父图 CLI 和独立 SQL 子图入口，用于明确用途和确定性对照；
- 不默认新增 v1 API 兼容层；若存在外部调用方，再单独评估恢复或适配；
- 对不再支持的旧 API 模块明确冻结或退役计划，避免“代码存在”被误解为“接口可用”；
- 保留独立 SQL 生成验证能力，或提供等价的新工具级入口。

### 7.7 中优先级：执行并发、重复执行、性能与成本尚未完成等价验证

旧复杂问题的 SQL 生成始终串行；只有执行阶段在配置开启且待执行 SQL 多于一条时进行批次内并发。新链由 Agent 决定工具调用顺序，没有确定性承接这一执行并发策略。

旧执行节点会跳过已有 `execution_result` 的子查询；新 `execute_sql` 每次成功调用都会实际执行并新增结果和执行记录，见 `src/modules/db_deepagent/services/sql_pipeline.py:462-585`。模型重复调用同一 SQL 时，没有旧节点那样的“已有结果则跳过”机制，因此重复执行率应纳入成本和正确性观测。是否增加幂等复用可作为后续低优先级优化，不能在未定义结果时效性前盲目缓存。

同时，新 Web 每轮重新构造 services、连接池和 Agent，并在结束时关闭，可能增加初始化成本。当前没有充分的新旧链准确率、延迟、模型调用次数和 Token 成本对照，因此不能认定新链在性能与成本上已经完成等价替代。

新链已有可作为对照基础的观测能力：

- `RunStats` 记录模型调用、工具调用和 Token 用量完整性，见 `src/modules/db_deepagent/schemas.py:258-270`；
- Runner 配置 `audit.jsonl` 模型观察器，见 `src/modules/db_deepagent/runner.py:377-415`。

旧链若要公平比较，需要补齐相应口径的埋点，不能只比较墙钟时间。

建议：

- 评估服务和连接池的复用边界；
- 对可独立执行的工具调用研究受控并发；
- 使用相同模型、数据和题集比较准确率、延迟与成本；
- 对比模型调用、Token、数据库执行次数和重复执行率，并统一统计口径；
- 不只比较最终成功率，还要比较多轮、失败恢复和部分完成场景。

## 8. 新链已经增强的能力

替代缺口不意味着旧链整体更好。代码分析表明，新链在结果可信度、执行安全和运行治理方面有实质提升。

### 8.1 SQL 校验与执行授权

SQL 通过受控工具校验和执行。`execute_sql` 要求 SQL 与有效授权记录匹配，模型不能仅凭生成一段 SQL 文本就绕过工具边界直接操作数据库。

### 8.2 只读事务与资源限制

新链提供：

- 只读事务；
- 语句超时；
- `fetchmany(max_rows + 1)` 实现的结果行数上限；
- 明确的 `source_truncated` 标记。

代码依据：`src/modules/db_deepagent/services/business_db.py:209-247`。

旧 `PGClient.execute_query` 有 `SET statement_timeout`，但随后直接调用 `fetchall()`，见 `src/services/db/pg_client.py:493-505`。它缺少同等的只读事务保护，配置中的 `max_rows_per_query` 也未在旧生产执行路径中真正应用。准确的差异是：**旧链有语句超时，新链进一步补齐只读事务和实际生效的结果行数上限。**

### 8.3 结果持久化、分页与分析

新链可将较大结果落盘，再按需分页读取和分析，避免把所有结果一次性塞入模型上下文，并为后续证据追溯提供基础。

### 8.4 证据结构与多轮复用

最终答案可以关联证据，运行账本保存实际执行信息，多轮对话可复用已有证据。`status=completed` 不仅要求 findings 非空，还要求 findings 引用有效证据；Runner 也会复核本次证据是否为空。这些约束超越了旧链单纯将执行结果放入父图状态再总结的方式。

### 8.5 多源会话绑定

Runner 会核对请求来源、服务来源和会话已绑定来源，拒绝跨源续聊或无来源检索，见 `src/modules/db_deepagent/runner.py:1116-1169`。这是当前多源分支下重要的数据隔离能力。

### 8.6 运行预算与可观测性

新链对主 Agent 模型步骤、工具调用、数据库执行次数和运行时限进行协作式预算控制，并记录 `RunStats` 与审计轨迹。这些能力为故障治理和新旧对照提供了旧链不具备的统一基础。

## 9. 文档与验证证据的边界

`docs/upgrade/implementation_2026-11-09_run01/README.md:73` 记载：

- 新链固定业务题集进行了 3 轮、每轮 12 例验证；
- 相关静态检查和协议验收通过。

这是历史验收记录，不是本文分析时重新执行的全量测试。固定题集通过可以证明新链在该题集和当时环境中可用，但不能证明其在所有输入、故障和兼容性场景下与旧链等价。

`docs/4_DeepAgents实现简化与旧链保留价值分析.md:192`（§8）明确说明，尚未完成新旧链准确率、成本和延迟的完整对照。

本文涉及的直接行为验证包括：

- 初次分析在受控 `300` 字符上限下复现非法 JSON；
- 本次复核在生产默认 `12000` 字符上限下，用嵌套超宽表载荷复现非法 JSON。

其余结论来自代码路径、配置和状态契约分析。性能、准确率及完整业务回归仍需要专项测试，不应从静态分析直接推导为已通过。

## 10. 建议的处理顺序与产品决策

### 第一阶段：修复确定性的正确性和可靠性问题

1. 修复 `_envelope.py` 的非法 JSON 截断，并补齐嵌套字段、`rows` 和生产上限测试；
2. 增加关键模型调用的有限项目层重试，区分底层传输重试；
3. 为“SQL 成功但最终答案生成失败”提供基于账本的降级答案，并保留错误和证据。

返回语义建议优先评估 `partial + 降级答案`；若产品选择 `failed + evidence`，也必须明确前端如何展示证据。这一选择不影响“需要账本兜底”的结论。

### 第二阶段：补齐已识别任务的完成度与检索门控

1. 在 `query-planning` 阶段写入轻量子任务账本，并在提交前检查终态和答案覆盖；
2. 在 SQL 校验前检查涉及表的 Schema 证据；
3. 将“已识别任务是否完成”和“用户意图是否完整识别”分开测试。

### 第三阶段：裁定设计差异与旧入口策略

1. 明确产品是否承诺兼容旧链的常用时间表达，再决定补齐文法或保留澄清设计；
2. 保留旧父图 CLI 和独立 SQL 子图的明确用途；
3. 单独裁定未挂载的旧 v1 API 模块是恢复、冻结还是退役，不默认建设兼容层。

### 第四阶段：验证并发、重复执行和运行成本

1. 使用同一批简单问题、依赖问题、多个独立子问题、多轮对话和故障场景做新旧对照；
2. 对比准确率、延迟、模型调用次数、Token 成本、数据库执行次数和重复执行率；
3. 根据结果决定是否恢复确定性执行并发、复用每轮创建的服务，或增加有明确时效语义的执行复用。

## 11. 最终判断

`db_deepagent` 已经能够替代旧 LangGraph 父子图的**主要 Web 查询路径**，并在安全执行、结果管理、证据追溯、多源隔离和运行治理方面实现了重要升级。

但从“完美替代”的标准看，当前答案仍然是**否**。主要差距不只是个别工具缺陷，还包括：

- 旧 Complex Path 针对已规划子查询的确定性终态控制尚未完全承接；
- 模型失败后的项目层最终答案兜底不足；
- 工具超长输出存在已复现的非法 JSON 缺陷；
- 时间语义和固定检索行为存在已知差异，其中时间文法是否补齐需要产品裁定；
- 旧协议与独立 SQL 子图调用方式不兼容；
- 执行并发、重复执行、性能和成本缺少充分对照。

在补齐高优先级缺陷并完成同题集、同模型、同数据的新旧对照之前，建议继续保留旧父图 CLI、独立 SQL 子图和确定性对照基线，而不宜直接删除旧链。旧 v1 API 当前未挂载，是否恢复或退役应独立决策，不与新链主流程替代结论捆绑。
