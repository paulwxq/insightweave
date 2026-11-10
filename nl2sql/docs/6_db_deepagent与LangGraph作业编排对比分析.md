# db_deepagent 与 LangGraph 作业编排对比分析

## 1. 文档目的与分析基线

本文用于回答以下问题：

1. `db_deepagent` 从接收问题到生成最终答案的完整流程是什么，其 tools、skills、middleware 和 runner 如何协作？
2. 旧 LangGraph 父子图如何编排简单问题、复杂问题、SQL 生成、校验、执行与总结？
3. `db_deepagent` 能否完美、无损地替代旧 LangGraph 作业？
4. 如果不能，当前还存在哪些功能、可靠性、兼容性和性能方面的缺口或设计差异，后续应优先处理什么？

本文主要代码阅读与行为分析以 **HEAD `6dbfbf9` 之上、2026-11-10 23:00:41（UTC+8）的工作区状态**为基线，依赖版本锚点为 `deepagents==0.7.34`，见 `pyproject.toml:36` 和 `uv.lock:639-640`。该基线包含未提交工作区改动，仅凭 commit 不能完整重放。此后 23:04–23:06 发生的摘要器独立构造等变更未纳入原有行为对比；本次未重新执行行为对比，仅校正引用锚点，并补充设计细节与边界说明。后续代码或依赖变化后，应重新核对本文结论。

`docs/upgrade/*.html` 和 `docs/v4/*` 作为相对可信的设计与历史验收资料，用于辅助理解；其他可能脱节的文档不作为判断当前行为的最终依据。文档与代码冲突时，以代码为准。

## 2. 结论摘要

**`db_deepagent` 已基本接管当前 v2 Web 主链，但它不是旧 LangGraph 作业的完美、无损、drop-in 替代。**

它在以下方面明显强于旧链：

- 通过工具完成可信的 SQL 校验和执行授权；
- 在旧链已有语句超时的基础上，补齐只读事务、实际生效的结果行数上限和明确的源头截断标记；
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

- `src/modules/db_deepagent/agent.py` 的 `build_db_agent()`
- `src/modules/db_deepagent/runner.py`

需要注意：这并不等于“去掉了 LangGraph”。DeepAgents 的 Agent 执行仍建立在 LangGraph 运行时之上，只是不再由项目手写旧父子图的业务拓扑。

## 4. db_deepagent 完整流程

### 4.1 Agent 装配

默认配置下，`src/modules/db_deepagent/agent.py` 的 `build_db_agent()` 装配了：

- 10 个业务工具；
- 1 个单独追加的最终答案提交工具 `submit_final_answer`；
- 4 个只读文件工具；
- 合计 15 个工具。

`features.todo_list=true` 时还会通过 `TodoListMiddleware` 增加 `write_todos`，因此“15 个”是默认配置口径，不是所有配置下恒定不变的数量。业务工具清单见 `src/modules/db_deepagent/tools/__init__.py:32-49`，提交工具在 `src/modules/db_deepagent/agent.py` 的 `build_db_agent()` 中通过 `tools.append(make_submit_final_answer_tool())` 单独追加。

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

Skill 本质上是模型可加载的操作指导，不是由 Runner 强制执行的固定状态机。模型是否加载、何时遵循以及是否完整执行，仍取决于 Agent 的推理过程。Skill 文档本身不能直接写 LangGraph 状态；如需把规划结果变成可校验账本，必须另配工具或中间件。

#### Middleware 栈与职责

`src/modules/db_deepagent/agent.py` 的 `build_db_agent()` 中，`extra_mw` 与 `create_deep_agent(middleware=...)` 显式传入的 middleware 栈及职责如下：

1. `FilesystemMiddleware`：接入会话后端和权限控制，在当前配置下提供 `ls/read_file/glob/grep` 四个只读文件工具；
2. `SummarizationMiddleware`：在上下文达到阈值时摘要历史消息并保留近期上下文，服务的是 Agent 长上下文管理，不是旧链最终答案 Summarizer；
3. `PatchToolCallsMiddleware`：修补消息历史中的悬空工具调用，避免缺少对应工具结果破坏后续模型调用；
4. `FinalAnswerGateMiddleware`：检测最终答案是否已提交，提交后结束运行，未提交而自然结束时按配置提醒；
5. `RunBudgetMiddleware`：在模型和工具调用边界预占 `model_call/tool_call` 预算，超限或超时后停止或拒绝后续调用；数据库执行预算不在这里预占，而是在 `SqlPipeline.execute` 内通过 `db_execute` 预占，见 `src/modules/db_deepagent/services/sql_pipeline.py:515-525`；
6. `RequestAuditMiddleware`：核对每次模型请求的实际工具集合，记录模型请求、工具名、消息数、近似 Token 和用量等审计信息；工具集合异常时停止运行，但不记录可用于 Schema 门控的工具调用参数账本；
7. 可选 `TodoListMiddleware`：仅在 `features.todo_list=true` 时加入，提供通用 `write_todos`，默认不启用。

这些 middleware 在 Agent 调用期间执行控制、审计和上下文管理；Runner 则负责调用前打开预算、调用后关闭预算，以及读取状态、评估终态和归档。两者不能互相替代。

### 4.2 装配与 Runner 的职责边界

在 Web 路径中，services、连接池、persistence、session 目录和 Agent 在 Runner 之前装配，见 `src/api/v2/runs.py:854-902`；请求结束时关闭 persistence 和 services，见 `src/api/v2/runs.py:921-929`。不能把这些前置装配工作归入 Runner P0。

`src/modules/db_deepagent/runner.py` 将一次运行组织为以下阶段：

#### P0：会话、来源与线程准备

- 校验会话和请求标识，并检查本地请求冲突；
- `run()` 获取或沿用会话锁，随后 `_run_locked()` 在锁内读取会话记录，核对请求、服务与会话的来源绑定；
- 解析或轮换线程，读取已有 checkpoint 与携带证据；只有轮换时的 `_handoff()` 才读取旧线程摘要，普通续聊不执行该读取。

代码依据：锁获取与释放见 `src/modules/db_deepagent/runner.py:167-235`，锁内 P0 见 `runner.py:265-354`，轮换摘要读取见 `runner.py:533-553`。此阶段不负责构建 services、session 目录或 persistence。

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

代码依据：`src/modules/db_deepagent/runner.py:897-924` 的 `_write_local_record`；本地记录排他创建，写入冲突或文件系统错误只记录日志，不覆盖既有记录。

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
sql_gen（调用 SQL 子图）
  ├─ 生成失败：summarizer
  └─ 生成成功：sql_exec → summarizer
```

简单问题经过路由后，由简单规划器形成执行所需状态，再由 `sql_gen` 调用 SQL 子图。生成失败时条件边跳过 `sql_exec` 并直接进入 Summarizer；生成成功且执行完成后，同样进入 Summarizer。Fast Path **不经过 `check_completion`**，见 `src/modules/nl2sql_father/graph.py:279-291、317-330`。

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
  ├─ parsing_failed：直接退出子图
  └─ 成功：schema_retrieval
                  ↓
             sql_generation
                  ↓
              validation
              ├─ 可重试失败：回到 sql_generation
              ├─ 不可恢复或达到上限：失败退出
              └─ 成功：退出子图
```

其特点是：

- 正常路径固定经过问题解析、Schema 检索、SQL 生成和校验；问题解析失败时直接从条件边退出，不继续检索；
- `max_iterations: 3` 包含首次生成，因此最多进行 **3 次生成，即首次 + 最多 2 次重新生成**；
- `iteration_count` 在每次生成后加 1，`should_retry` 仅在小于上限时回到生成节点；
- SQL 生成节点自身另有默认 `max_attempts=3` 的 LLM 调用重试；
- 子图有明确的输入输出协议，可独立于父图承担 SQL 生成和验证任务。

代码依据：

- `src/modules/sql_generation/config/sql_generation_subgraph.yaml:235-238`
- `src/modules/sql_generation/subgraph/nodes/sql_generation.py:68-71、566-570`
- `src/modules/sql_generation/subgraph/create_subgraph.py:70-87、118-129`

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
- 落地时可选用当前已安装的 `langchain.agents.middleware.model_retry.ModelRetryMiddleware`，它支持按异常类型重试、退避和失败策略；`max_retries` 表示初次调用之后的最大重试次数。它只重试模型调用，不会根据 SQL 执行账本生成降级答案，因此不能替代下一项；
- 引入中间件重试时，应结合底层 OpenAI 客户端重试和运行时限统一计算最大尝试次数与延迟，避免多层重试叠加；
- 明确重试尝试是否计入 `model_call` 预算及 RunStats 口径：`RunBudgetMiddleware.before_model` 当前每个模型步骤只预占一次，而 `ModelCallObserver` 按回调尝试计数，因此内部重试可能使 `model_calls` 与 `total_model_calls` 不同。推荐保留前者为“预算模型步骤数”、后者为“实际观察到的模型尝试数”，成本对照使用后者及 Token；若预算目标是限制实际尝试，则每次中间件重试也必须单独预占预算，不能混用两套数字；
- 在已有成功 SQL 证据但最终提交失败时，根据执行账本生成降级答案；
- 推荐采用 `partial + 降级答案 + errors/warnings + evidence`，明确表达“有结果但最终生成未完成”，不能伪装成 `completed`；
- 如果产品要求任何此类异常都保持 `failed`，则至少保留并展示证据及状态说明。`partial` 还是 `failed` 是返回语义的产品决策，但两种方案都应避免实时响应空答案。

### 7.2 高优先级：缺少针对已识别子任务的代码级终态门控

`submit_final_answer` 当前验证：

- 必须单独调用，同一条模型消息不能同时调用其他工具；
- 同一运行不得重复提交；
- findings 引用的 `evidence_id` 必须属于本运行 `purpose="query"` 的执行记录或分析记录，包括经 `reuse_evidence` 沿用登记的执行（E-）／分析（A-）证据；
- 探针（probe）和下推（pushdown）执行记录不能作为 findings 引用；时间（T-）／维度（D-）证据虽然可以沿用并用于 SQL 守卫，但也不在最终 findings 的合法引用集合中；
- 仅当 `status=completed` 时强制 `findings` 非空。

代码依据：`src/modules/db_deepagent/tools/final_answer.py:67-121` 和 `src/modules/db_deepagent/tools/guard_tools.py:426-474`。Runner 还会把“模型报 `completed` 但本次无任何成功证据”的情况改判为 `failed`，见 `src/modules/db_deepagent/runner.py:699-706`。

这些检查保证提交结构和证据有效，但不检查所有已识别子任务是否都已处理并回答。新链没有等价于旧 Complex Path `check_completion` 的门控。

需要准确理解旧链的保证范围：

- 旧链只保证**已规划子查询**进入 `completed/failed`，不保证 Planner 没有漏拆用户意图；
- 该门控只在 Complex Path 生效，Fast Path 没有；
- 因而新旧链都不能仅凭这一层机制保证“用户所有独立子问题都被识别”。新链缺少的是旧 Complex Path 已有的“已规划任务终态”这一级保证。

例如，Agent 已识别三个独立子任务，但只回答其中两个，只要提交结构和证据合法，仍可能得到 `completed`。

推荐增加**显式子任务账本**，但这是需要新增工具、状态和校验规则的机制，不是现有 Skill 自动具备的能力：

1. **先登记、后执行，并定义账本字段**：新增 `record_sub_tasks` 工具和 `db_sub_tasks` 状态通道。每条记录至少包含 `sub_task_id`、`content`、`dependencies`、`revision`、`status`、`reason` 和支撑状态判断的 `evidence_ids`；状态集合为 `pending/in_progress/completed/failed/needs_clarification/voided`，其中 failed、needs_clarification、voided 必须填写 reason，completed 必须有该任务名下的有效执行或分析证据。`validate_sql` 是首个强制点：在调用 pipeline 前发现本运行尚未登记任务，就返回可重试错误，不允许先校验再补登记；`execute_sql` 必须依赖有效且任务匹配的授权记录，不能绕过该校验；`analyze_result` 和 `reuse_evidence` 在各自入口检查目标任务存在。简单单任务运行同样先登记一条；
2. **由同一工具维护状态、版本和 reducer 语义**：`record_sub_tasks` 支持显式 `add/revise/status/void` 操作，不另增 update_sub_task 工具。`db_sub_tasks` 以 `(run_id, sub_task_id, revision)` 为键保存各版本记录，reducer 只合并完全相同键的当前状态，不负责选择最新版本；status／void 必须携带当前 revision 并只更新该键，过期 revision 拒绝。LedgerView 按 run_id 和 sub_task_id 分组、选择最大 revision 且未作废的记录，生成门控所需的“最新有效任务”视图。add 同 ID 同内容幂等，同 ID 异内容必须走 revise；status 更新 reason 和 evidence_ids，并在 completed 时核验证据归属，不能只凭模型自报：只接受本运行的 query／pushdown 执行或分析证据（含 reuse_evidence 登记到该任务的 E-/A-），证据的 run_id、sub_task_id 和 sub_task_revision 必须与任务当前 revision 一致，probe、时间／维度证据及其他任务或其他 revision 的证据均不接受。revise 新增的 revision+1 固定为 `pending`、`evidence_ids=[]`、`reason=None`，不继承旧版本证据或 completed 状态；旧版本仅保留历史并标记为 voided／被取代，新版本必须重新执行／分析／沿用证据，并由 findings 以新 revision 重新关联。voided 不可再 revise 或通过 status 复活；需要恢复时必须 add 一个新 sub_task_id，旧任务保持历史。任务不得静默删除或重编号；voided 属于终态并从必需覆盖集合排除；
3. **明确任务归属与授权透传**：
   - `validate_sql` 和 `analyze_result` 接收已登记任务的 `sub_task_id + sub_task_revision`，并核对它是该任务的最新有效版本。`ValidationRecord`、`ExecutionRecord` 同时增加 `sub_task_id: str | None` 和 `sub_task_revision: int | None`：query／pushdown 时两者必填，probe 时两者固定为 None；`AnalysisRecord.sub_task_id: str` 与 `AnalysisRecord.sub_task_revision: int` 均必填，因为本地分析和下推分析均由 analyze_result 发起，跨任务 compare 使用已登记的聚合任务。当前这些记录结构都没有任务或 revision 字段；
   - 输入与原任务版本使用明确字段承载：`AnalysisRecord.input_task_refs: list[{result_ref, sub_task_id, revision}]` 与 inputs 一一对应，记录每个输入结果及其任务版本；`EvidenceOrigin` 增加 `sub_task_id: str | None`、`sub_task_revision: int | None`，沿用 E-/A- 时两者必填，时间／维度来源为 None。这些是账本与审计字段，不应机械加入 TOOL_VISIBLE_FIELDS；只有实际新增的工具可见参数才更新该常量，同时补齐协议测试和审计视图；
   - `SqlPipeline.validate()` 增加 `sub_task_id/sub_task_revision` 参数并将两者写入 ValidationRecord。公共 `validate_sql` 固定使用 `purpose="query"`，见 `src/modules/db_deepagent/tools/sql_tools.py` 的 `validate_sql()`；
   - **授权记录必须任务化，不能只按 SQL 继承归属**：当前 `sql_pipeline.py` 的 `auth_key()` 不含任务 ID，而 `db_validations` 的 `merge_by_key` 会用右值覆盖同键记录；同一 SQL 若在任务 A、B 分别校验，后一次会覆盖前一次。因此不能声称 SQL-only 授权记录能唯一决定任务归属；
   - 采用任务版本与用途共同作用域授权：`auth_key()` 必须同时纳入 `purpose`、`sub_task_id` 和 `sub_task_revision`，query／pushdown／probe 的键都包含这三个维度，probe 的任务 ID 和 revision 使用固定 None 标记，避免跨任务、跨版本、跨用途碰撞。`validate_sql` 和 `execute_sql` 都接收目标 `sub_task_id + sub_task_revision`；`SqlPipeline.execute()` 计算相同的版本化授权键，核对记录有效且任务版本一致，再写入 ExecutionRecord。未找到对应版本授权或版本不一致时返回 `not_validated`，因此旧 revision 的校验不能放行新 revision，执行“A 校验、B 执行”也会被拒绝；同一 SQL 在不同任务或 revision 下分别拥有独立授权，不再互相覆盖。同步更新 `ValidationRecord.key` 的键构成说明、`auth_key()` 的测试和相关断言，至少覆盖同 SQL 不同任务／revision 独立授权、跨任务／跨版本执行拒绝、不同 purpose 不碰撞；
   - `SqlPipeline.validate_and_execute()` 增加 `sub_task_id: str | None` 与 `sub_task_revision: int | None` 透传。`analyze_result` 的 pushdown 必须传入非空任务 ID 和 revision，使内部 ValidationRecord、ExecutionRecord 与最终 AnalysisRecord 保持同一任务版本归属；probe 则固定传 `purpose="probe" + sub_task_id=None + sub_task_revision=None`。probe 授权键由原有 `thread/request/run/datasource/validation_fp/normalized_sql` 加上 `purpose="probe"` 和固定 None 任务／revision 标记组成，None 必须采用明确的规范编码，不能与空字符串混用。probe 不走公共 `execute_sql`，而由 `validate_and_execute()` 内部依次调用 `validate()` 和 `execute()`，并以同一 `trusted_record` 完成内部执行；probe 不作为最终答案证据；
   - **同步扩展只读账本接口**：当前 `LedgerView` 只有 `validations` 和 `evidence_of_run()`，不能直接支撑下列查询。需增加 `sub_tasks`、`executions`、`analyses`、`table_inspections` 只读属性，或提供按 `run_id/sub_task_id` 查询任务、按 `run_id/result_ref` 查询执行与分析记录、按 `run_id/table_id` 查询表检查记录的方法；新增状态通道与该视图必须一起扩展。validate_sql、analyze_result、reuse_evidence 和 pipeline 均通过 LedgerView 检查任务及关联记录，不绕过抽象直接读取 runtime.state。当前 reuse_evidence 仍直接读取 runtime.state 中的时间、维度、执行和分析记录，实施时必须把这些读取迁移到扩展后的 LedgerView，而不是只让新增调用方使用该视图；
   - `analyze_result` 对所有操作统一校验输入来源：读取 inputs 的 `result_ref` 后、本地分析或 pushdown 前，通过上述 LedgerView 找到执行／分析记录；来源为 `purpose="probe"` 的结果一律拒绝，不限于 compare，因为 probe 没有任务归属且不作为最终答案证据。跨任务 `compare` 等分析还需先登记一个比较／聚合任务，在依赖字段中列出全部来源子任务，并使用该聚合任务的 `sub_task_id + sub_task_revision`；逐一核对输入记录的 `sub_task_id/sub_task_revision`，要求任务属于声明依赖且输入 revision 是该依赖任务当时的最新有效 revision。AnalysisRecord 将每个输入的 result_ref、sub_task_id 和 revision 写入 `input_task_refs`；依赖在最终提交前被 revise 或 void 后的失效检测与重算路径见第 5 点。查不到归属、任务或 revision 不匹配、输入缺少声明依赖时均拒绝。输出证据归聚合任务的当前 revision；
   - `reuse_evidence` 在沿用执行／分析证据时必须提供目标 `sub_task_id + 当前 sub_task_revision`；新登记到本运行的 E-/A- 记录写入该目标任务和 revision，并在 reused_from 中保留原证据及原任务版本供审计。时间和维度证据可作为共享守卫证据处理；未知任务、未归属执行或事后补挂历史执行均拒绝或标记为无效，不计入门控。`read_result` 不新增 `sub_task_id/sub_task_revision`，其归属通过 `result_ref` 对应的执行或分析记录取得，避免重复维护另一套映射；该 `result_ref` 必须来自本会话可读取的执行／分析记录（含沿用登记），ResultStore 会校验会话归属并拒绝跨会话访问；
4. **最终覆盖由带版本的 findings 反查**：当前 `FindingItem` 只有 `text/evidence_ids`，见 `src/modules/db_deepagent/schemas.py` 的 `FindingItem`；需要新增提交内唯一的 `finding_id` 和 `task_refs: list[{sub_task_id, revision}]`，其中 task_refs `min_length=1`，不允许空列表；全局说明放入 answer／limitations，不伪装成无任务 finding。每个 task_ref 只能指向最新、未作废且 status=completed 的任务版本；引用 voided、failed、needs_clarification、pending/in_progress 或旧 revision 一律拒绝，修订后必须用新 revision 重新关联 finding。每条 task_ref 至少要有该 finding 的一条主证据，其记录的 `sub_task_id/sub_task_revision` 与 task_ref 完全一致，其他任务版本的证据不能用于覆盖。`covered_finding_ids` 不在 db_sub_tasks 中重复维护，而由 submit_final_answer 按 `(sub_task_id, revision, finding_id)` 计算覆盖映射，Runner 使用同一输入复核；submit_final_answer 不自行改写任务状态；
5. **提交和 Runner 按固定规则检查并映射整体状态**：先取得 LedgerView 的最新有效任务视图，再做依赖失效检查：对最终 findings 或 completed 任务 evidence_ids 实际引用的 AnalysisRecord，逐项比较 `input_task_refs` 与对应依赖的最新有效 revision；revision 不一致或依赖已 voided 都判定为 `stale_task_inputs`。submit_final_answer 和 Runner 都在覆盖判定前执行该检查，提交被拒绝时不写最终答案记录，也不自动级联改写任务。Agent 随后通过 record_sub_tasks 将聚合任务置为 pending、reason="dependency_stale"，基于最新依赖重新分析并登记新证据后再提交；若依赖被 voided，应先 revise 聚合任务以调整依赖，或将其标记为 failed／needs_clarification。若运行带着 stale 证据自然结束，整体为 failed，而不是 completed／partial。随后再应用以下规则；completed 任务必须有覆盖 finding，failed／needs_clarification 必须有 reason，不能用其他任务的成功证据替代：

   | 最新有效任务状态 | 提交处理与整体 status |
   | --- | --- |
   | 空集合，包括全部 voided／被取代 | 禁止 completed，整体为 failed，并记录“无有效任务”原因 |
   | 含 pending／in_progress | 拒绝最终提交，要求继续执行或显式转为失败／澄清；若 Agent 自然结束而未提交，Runner 按现有无最终答案规则判 failed |
   | 含 needs_clarification | 整体为 needs_clarification；已有结果仍保留在 evidence 中，不与 partial 混用 |
   | 全部 failed | failed |
   | 同时有 completed 和 failed | partial |
   | 全部 completed，但至少一个未被 finding 覆盖 | 拒绝提交，要求补齐覆盖，不能按 completed 接受 |
   | 至少一个有效任务，且全部 completed 并被覆盖 | completed；若全部结果明确无数据，可使用现有 no_data，但仍须通过相同覆盖门控 |

   以上规则由 submit_final_answer 和 Runner 双重执行；不存在“所有有效任务为空时全部完成”的真空通过；
6. **不混淆语义完整性**：将“规划是否漏拆”作为单独的语义质量问题评估。先登记约束能防止事后补记，但仍不能证明模型最初识别了全部用户意图。

该设计还牵动装配契约。本文推荐把业务专用的 `record_sub_tasks` 归入 `build_business_tools()`，因此默认口径由当前 10 个业务工具、总计 15 件变为 **11 个业务工具 + 1 个提交工具 + 4 个文件工具 = 16 件**；§4.1 的 10／15 仍是当前代码口径。如果实施时选择像 `submit_final_answer` 一样单独追加，则应写成“10 个业务工具 + 2 个单独工具 + 4 个文件工具 = 16 件”，总数不变但分类不同。两种选择都需同步更新 `src/modules/db_deepagent/agent.py` 中的 `STEP4_TOOLS/FULL_TOOLS` 常量、`stage_expected_tool_names()`、`TOOL_VISIBLE_FIELDS` 和 `verify_assembly()`。新增或变更的 `sub_task_id/sub_task_revision/task_refs/finding_id` 可见字段、RequestAuditMiddleware 的期望工具集合及相关装配／协议测试也必须同步，不能只修改工具函数。

不建议把可选 `TodoListMiddleware/write_todos` 直接当作唯一门控依据：它默认关闭，仍是模型自主维护的通用待办，缺少本业务所需的证据关联和终态约束。

轻量替代设计是扩展 `submit_final_answer` 的 `sub_tasks` 字段，提交每个子任务的状态、findings 或失败原因，再由 Runner 校验覆盖关系和证据引用。但如果该列表只在最终提交时生成，模型仍可漏报或事后改写计划，Runner 只能校验“自报列表内部是否完整”，不能证明它等于规划阶段的任务集合。因此它只适合作为过渡方案；要降低事后自报风险，仍应在 plan 阶段先通过 `record_sub_tasks` 写一次账本，最终提交只引用既有子任务 ID。

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

可重放证据已归档：

- 探针脚本：`docs/upgrade/implementation_2026-11-09_run01/tool_output_envelope_probe.py`；
- 真实输出日志：`docs/upgrade/implementation_2026-11-09_run01/tool-output-envelope-probe.log`；
- 300 上限案例构造单个 5000 字符 `blob`；12000 上限案例构造单表 300 列，每列包含名称和 80 字符注释，以覆盖未被顶层列表缩减规则处理的 `tables[].columns`。

从模块根目录可用以下命令重放：

```bash
PYTHONPATH=. .venv/bin/python docs/upgrade/implementation_2026-11-09_run01/tool_output_envelope_probe.py
```

该脚本当前在 `tool_output_envelope_probe.py:32` 断言 `json_valid is False`，属于**缺陷复现探针**，不是修复后的回归测试。修复 `_envelope.py` 后它预期会失败；届时应改为断言合法 JSON、截断标记和长度预算，或将脚本与原始日志明确移作历史证据并停止作为现行检查执行。

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

一种合法的硬降级目标结构如下（这是修复建议，不是当前返回）：

```json
{
  "status": "error",
  "data": {
    "truncated": true,
    "original_chars": 25340,
    "summary": "明细过大，未返回；请缩小范围或分页读取"
  },
  "warnings": [
    "输出超过 12000 字符上限，已降级为摘要"
  ],
  "error": {
    "error_type": "tool_output_truncated",
    "message": "无法在当前输出预算内返回完整结构",
    "retryable": true,
    "hint": "减少字段、缩小分页或缩小查询范围后重试"
  }
}
```

实现应保留原有 warnings 并追加截断原因；`data` 只保留摘要和必要元数据。降级 envelope 自身序列化后也必须满足字符预算，不能再次通过硬截断使其变小。测试应同时断言 `json.loads` 成功、`data.truncated=true`、警告保留和长度不超限。

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

**现有账本不能直接支撑 Schema 前置门控。** `DbDeepAgentState` 继承 DeepAgentState；编译后的图状态还包含 `messages`，启用 TodoListMiddleware 时还包含由该中间件加入的 `todos`，后者不是 `DbDeepAgentState` 类自身声明的字段。这里讨论的是当前**自定义业务账本通道**：校验、执行、分析、时间、维度、工具错误、运行状态和最终答案等，见 `src/modules/db_deepagent/state.py` 的 `DbDeepAgentState` 声明，其中没有 Schema 或表检查通道。并且：

- `search_schema` 只把维度证据写入 `db_dim_evidence`，见 `src/modules/db_deepagent/tools/schema_tools.py:276-289`；
- `inspect_tables` 当前无状态写入，直接返回字符串，见 `schema_tools.py:300-364`；
- `reuse_evidence` 只支持时间、维度、执行和分析记录，不支持 Schema 沿用；
- `RequestAuditMiddleware` 记录工具名集合和模型请求信息，不记录 `inspect_tables` 的调用参数，见 `src/modules/db_deepagent/middleware/request_audit.py:71-135`。

推荐新增 `db_table_inspections` 状态通道，并形成以下完整机制：

1. 改造 `inspect_tables` 的完整协议：接收 runtime，在单表检查成功后写入 `run_id`、规范 `table_id`、`datasource_id`、`include_live_columns`（或等价的结构来源）和检查时间。当前 `TableCatalog.describe()` 没有稳定的结构版本字段；若上游未提供版本，则以 `include_live_columns + 检查时间 + 规范化列结构摘要 hash` 作为等价证据，摘要由本次实际返回的列名、类型、顺序等结构内容计算，不假设存在上游版本号。`include_live_columns=false` 只表示目录结构证据，不能被门控当作已覆盖实时列结构。在 `DbDeepAgentState` 新增 `db_table_inspections` 通道及配套 reducer，处理按运行和表去重及事实冲突；工具返回值从纯字符串改为通过 `tool_command` 构造的 `Command`，同时携带 ToolMessage 和状态更新。`search_schema` 的候选表命中不能替代实际表结构检查；
2. 表引用识别应嵌入 `SqlPipeline.validate` 的现有流程，在结果占位符展开之后进行，复用 `expand_placeholders` 的错误处理，见 `src/modules/db_deepagent/services/sql_pipeline.py` 的 `validate()`。表检查记录通过 §7.2 所述扩展后的 `LedgerView.table_inspections` 或按 `run_id/table_id` 查询方法读取；当前 LedgerView 没有该访问器，不能只新增状态通道而遗漏视图扩展。物理表识别分为两层复用 `src/modules/db_deepagent/services/sql_validation.py` 的现有能力：**AST 提取**使用 `parse`、`normalize_identifiers`、`find_all(exp.Table)` 和 `_visible_cte_names()` 的 CTE 作用域判断，递归覆盖子查询并保留 schema 前缀，不另写正则扫描；**目录核对**使用 `_locate_tables()` 处理非限定表名的 search_path 实际解析，它查询 information_schema，不属于 AST 能力，目录查询失败必须归为“目录不可用”，不能当作“无表引用”。**不能直接继承 `_check_schema_scope()` 的返回语义**：该方法在 `allowed_schemas` 缺失或 sqlglot 解析失败时都会返回 `ValidationOutcome(True, "syntax")`，属于 fail-open，不能把这一结果解释成“无表引用”或“Schema 门控已通过”。新机制应返回能区分 `成功／解析失败／范围或目录不可用` 的表引用结果；占位符展开失败沿用现有校验错误，AST 解析失败或必要范围信息缺失时，硬门控必须 fail-closed 拒绝校验，软约束则返回明确 warning。可重试性按原因区分：缺少表检查记录可引导补查后重试，配置或目录不可用通常不是同参数重试可解决的问题，SQL 解析失败则要求修正 SQL；
3. 如果需要跨轮沿用表结构，另增加显式沿用规则并校验数据源一致性和结构新鲜度，不能复用现有 E-/A-/T-/D- 证据规则代替；
4. 为多表、多指标和历史上下文复用增加针对性测试。

软约束与硬门控是产品和风险取舍，不是唯一实现：

- **软约束**：缺少 Schema 证据时允许校验通过，但返回结构化 warning 和可重试提示，并把缺失情况保留到运行记录；对简单单表查询干扰较小，但不能宣称已实现确定性强制检查；
- **硬门控**：缺少记录就拒绝校验并引导补查，语义正确性更强，但可能因账本规则过严或证据识别误报而阻塞本来可正确执行的简单查询。

推荐先以软约束观察缺失率和误报，再至少对多表／Join 或语义风险较高的 SQL 开启硬门控；是否对所有单表 SQL 强制，应由产品对确定性与可用性的优先级决定。

不建议扫描 `messages` 中的历史 `inspect_tables` 调用参数作为主要方案：摘要、裁剪和线程轮换会影响可见性，且消息文本不是可靠的结构化账本。

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

旧执行节点会跳过已有 `execution_result` 的子查询；新 `execute_sql` 每次成功调用都会实际执行并新增结果和执行记录。`execute` 方法体见 `src/modules/db_deepagent/services/sql_pipeline.py:462-595`，其中执行记录追加见 `sql_pipeline.py:568-585`。模型重复调用同一 SQL 时，没有旧节点那样的“已有结果则跳过”机制，因此重复执行率应纳入成本和正确性观测。是否增加幂等复用可作为后续低优先级优化，不能在未定义结果时效性前盲目缓存。

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
- 语句超时（旧链同样具备，不属于新链新增差异）；
- `fetchmany(max_rows + 1)` 实现的结果行数上限；
- 明确的 `source_truncated` 标记。

代码依据：`src/modules/db_deepagent/services/business_db.py:209-247`。

旧 `PGClient.execute_query` 有 `SET statement_timeout`，但随后直接调用 `fetchall()`，见 `src/services/db/pg_client.py:493-505`。它缺少同等的只读事务保护。`max_rows_per_query: 10000` 声明于 `src/modules/nl2sql_father/config/nl2sql_father_graph.yaml:83`，但全 `src` 搜索的另一处命中仅为测试 mock 配置 `src/tests/unit/nl2sql_father/test_sql_execution.py:19`，旧生产执行代码没有读取或应用它。准确的差异是：**旧链有语句超时，新链进一步补齐只读事务和实际生效的结果行数上限。**

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

本文涉及的直接行为验证为工具返回包截断探针：

- 受控 `300` 字符上限案例和生产默认 `12000` 字符上限的单表 300 列案例均复现非法 JSON；
- 可重放脚本为 `docs/upgrade/implementation_2026-11-09_run01/tool_output_envelope_probe.py`，真实输出为 `docs/upgrade/implementation_2026-11-09_run01/tool-output-envelope-probe.log`；
- 探针只调用 `finalize_output`，不访问真实模型或数据库，不能据此推断其他业务链路已通过。

其余结论来自代码路径、配置和状态契约分析。性能、准确率及完整业务回归仍需要专项测试，不应从静态分析直接推导为已通过。

## 10. 建议的处理顺序与产品决策

### 第一阶段：修复确定性的正确性和可靠性问题

1. 修复 `_envelope.py` 的非法 JSON 截断，并补齐嵌套字段、`rows` 和生产上限测试；
2. 增加关键模型调用的有限项目层重试，区分底层传输重试；
3. 为“SQL 成功但最终答案生成失败”提供基于账本的降级答案，并保留错误和证据。

返回语义建议优先评估 `partial + 降级答案`；若产品选择 `failed + evidence`，也必须明确前端如何展示证据。这一选择不影响“需要账本兜底”的结论。

### 第二阶段：补齐已识别任务的完成度与检索门控

1. 新增 `record_sub_tasks` 工具和 `db_sub_tasks` 状态通道，让 Agent 按 `query-planning` Skill 规划后通过工具写入；关联后续执行与 findings，并在提交前检查终态和答案覆盖；
2. 新增 `db_table_inspections` 状态通道，由 `inspect_tables` 成功时写入，再由 `validate_sql` 按选定的软约束／硬门控策略检查涉及的表；先观测误报，再确定强制范围；
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
