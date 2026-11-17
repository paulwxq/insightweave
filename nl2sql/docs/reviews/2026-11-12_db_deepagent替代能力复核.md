# db_deepagent 替代能力复核与现有 API 实现路径

复核日期：2026-11-12。对象：`docs/6_db_deepagent与LangGraph作业编排对比分析.md`。

## 1. 判断

**当前代码不能称为旧 LangGraph 父子图的完整、无损替代；现有 DeepAgents API 则足以承载本文核实出的主要业务缺口，无需为了补齐这些能力恢复旧的整个父子图。**

需要区分三件事：

- **当前业务实现**：答案兜底、任务终态与覆盖校验、确定性的执行批次策略仍未补齐。
- **SDK 能力**：自定义状态、可信工具写状态、中间件拦截、工具并行、checkpoint 均已可用。上述缺口主要是项目尚未实现，并非框架不支持。
- **替代 LangGraph 本身**：做不到，也不是此次迁移的实际含义。当前 DeepAgents 返回的仍是 LangGraph 的 `CompiledStateGraph`，checkpoint 仍依赖 LangGraph。能替代的是项目手写的业务编排拓扑。

原文的大方向正确，但 **§7.3“超长输出硬截断为非法 JSON”已经过时**；§7.7 并发应明确为“未承接旧调度策略”，而非“不支持并行”。另外，旧时间解析采用 LLM 只证明输入形式更开放，不能证明那些表达在旧链上一定正确。

## 2. 基线与证据边界

本次读取的是 HEAD `71ee3e1` 之上的实际工作区，包括未提交改动；没有修改业务实现，也没有覆盖正在编辑的原分析文档。

通过项目 `.venv/bin/python` 实际确认：

| 包 | 已安装版本 |
| --- | --- |
| deepagents | 0.7.34 |
| langchain | 1.4.9 |
| langgraph | 1.2.20 |

API 判断以 `.venv/lib/python3.12/site-packages/` 的上述精确版本源码为准，并通过 Context7 查阅官方资料交叉核对。在线 `main` 文档可能前移，不能反向覆盖已安装版本的行为。官方入口：[DeepAgents graph 源码](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/graph.py)、[文件中间件源码](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/filesystem.py)。

本次运行了现有离线单测与专用探针；没有调用真实模型或业务数据库，没有重新验证真实题集准确率、跨进程恢复、端到端延迟及成本。测试通过不能替代这些验证。

专用探针：[脚本](2026-11-12_deepagent_capability_probe.py)、[输出及关键源码 SHA256](2026-11-12_deepagent_capability_probe.jsonl)。脚本验证的是当前行为，含已知缺口，不能作为未来补齐后的通过标准。

## 3. 原文差异逐项核实

| 原文差异 | 当前判定 | 代码依据与实际含义 |
| --- | --- | --- |
| §7.1 SQL 成功后仍可能无最终答案 | **存在** | `runner.py:660–707` 依据运行失败优先判定，未提交时 answer 取空字符串；现有故障注入测试明确断言该行为。 |
| §7.2 缺少已规划子任务终态门控 | **存在** | `state.py:119` 起无任务通道；`schemas.py:186` 的 finding 只有 text/evidence_ids；`final_answer.py:90–121` 验证证据存在和 findings 非空，不核对计划集合。 |
| §7.3 工具返回包硬截断损坏 JSON | **当前不再存在该路径** | `_envelope.py:42` 起完整序列化；`schema_tools.py:333` 起保留全部表结构；当前没有 `finalize_output` 实现及其字符上限配置。大输出由 SDK 转存。 |
| §7.4 时间表达覆盖收窄 | **新链限制真实，旧链正确性未证明** | `time_resolver.py:247–305` 明确拒绝若干表达；旧 `question_parsing.py:120–203` 将日期解析交给 LLM。不能据此断言旧链全部正确支持。 |
| §7.5 固定检索变按需检索 | **存在** | 旧子图 `create_subgraph.py:115–158` 定义固定边；新 `inspect_tables` 只有字符串返回，无检查账本；但新链的 SQL 校验和执行授权仍是代码强制约束。 |
| §7.6 返回协议及旧入口不兼容 | **存在，属于入口与契约差异** | 新结果是 `DbAgentResult`；`api/main.py:128–131` 只挂 v2；旧 CLI/SQL 子图入口仍保留。没有证据表明必须恢复旧 v1 API。 |
| §7.7 旧执行批次并发未等价承接 | **存在策略差异，SDK 已支持并行** | 旧 `sql_execution.py:152–195` 收集未执行 SQL 并按配置并行；SDK `tool_node.py:801–866` 具备线程池/gather；新 Runner 没有等价的任务就绪批次调度。 |
| §7.7 新链可能重复执行同一 SQL | **存在** | `sql_pipeline.py:515–585` 每次成功调用都预占执行预算、查库、保存结果；没有按任务已完成跳过的机制。证据 reducer 去重不会消除已经发生的查询。 |
| §7.7 每轮装配服务/Agent 的开销 | **装配事实存在，性能影响未定量证明** | `api/v2/runs.py:854–929` 构建并回收本轮资源；readiness 已有部分缓存。不能直接据此判定新链比旧链慢。 |
| §6 SQL 失败修正循环不再由旧图控制 | **存在** | 旧子图校验后按次数回到生成节点；新链依赖 Agent 响应工具反馈，只有全局预算，未建立每任务的 SQL 修正次数和耗尽转移。 |

原文 §8 的主要增强也成立：只读事务、实际行数上限、源头截断标记、结果分页与分析、证据关联、来源绑定及运行预算。旧链已有 `statement_timeout`，不能算新链独有；旧 `max_rows_per_query` 配置并未在生产执行节点应用。

### 3.1 答案兜底：归档已改善，但答案仍缺失

当前已经修复过“SQL 成功、模型失败后执行账本丢失”的问题：Runner 会保留证据并按真实执行条件归档。`test_runner_persistence.py:271` 的测试同时断言：

- status 为 failed；
- answer 为空；
- evidence 和 QA 归档保留。

因此，不能说当前连结果都丢了；缺口是**没有根据可信结果生成可展示的降级答案**。

独立 `build_summarizer_model()` 也不是这个兜底。它服务于上下文摘要，压缩历史消息；不是旧 Summarizer 的最终答案生成器。`FinalAnswerGateMiddleware` 的一次提醒同样不能处理模型已经不可调用的情况。

### 3.2 完成度：证据有效不等于任务全部完成

当前提交工具能防止伪造 evidence_id，但它不知道本轮究竟登记了几个任务、哪些任务尚未完成、哪些答案段落覆盖哪些任务。

探针构造两条执行记录，仅引用其中一条，仍可提交 completed，且没有任何任务账本。这个探针证明的是门控缺失，**不意味着所有执行证据都必须被引用**：诊断查询、中间结果和探索查询本来就可能不应出现在最终发现中。要核对的是必要任务的覆盖，而不是执行记录数量。

旧链的保证也有限：仅 Complex Path 核对已规划子查询的终态，不能保证 Planner 没有漏拆用户意图；达到上限或依赖无法推进时，还会把剩余任务转为 failed 后总结。新设计应承接这种确定性退出，而不是强迫所有任务成功。

### 3.3 大输出：应更新原文，停止追修旧截断逻辑

当前序列化入口使用 `json.dumps(..., ensure_ascii=False, indent=2)`，保留完整载荷。探针对 300 列嵌套结构生成 **95,713 字符** JSON，完整反序列化成功，列全部保留。

生产文件中间件阈值为 20,000 个近似 token；本版本按约 4 字符/token 判断，边界测试覆盖 80,000/80,001 字符。实际阈值应以本版本实现及配置为准，不是模型 tokenizer 的精确 token 数。

现有 `test_tool_output_offload.py` 的 14 项测试覆盖：转存、逐字保真、权限内回读、Command 的账本更新保留、同步/异步 wrapper、写入失败以及假模型驱动的生产图闭环。本次均通过。

转存后模型看到的是说明、路径和预览，**不再保证该 ToolMessage 正文是一整个 JSON 包**；完整 JSON 在文件里。应分别验证“工具原始输出合法 JSON”和“中间件转存协议可回读”，不能对转存提示直接 `json.loads()`，再误报原问题复发。

转存写入失败时 SDK 保留原始内容，因此仍可能增大上下文；这与旧的 JSON 硬截断缺陷不同。原文的旧探针调用已删除的 `finalize_output`，应标记历史证据，不应继续作为现行验证命令。

## 4. 在当前 API 中如何补齐

### 4.1 先补 Runner 的确定性答案降级

建议在 `_compose_result()` 的评估与归档流程中增加 `build_fallback_answer()`，而非只加一个 `after_model` 提醒。模型调用直接抛异常时，后续模型 hook 不一定能运行；Runner 才是现有的异常收敛边界。

推荐流程：

1. P2 确认读取到本轮可信 query 执行/分析证据；排除 probe、失效/不可读记录。
2. 最终答案缺失或最终生成失败时，仅根据账本与 ResultStore 的有限结果预览生成模板：已执行哪些查询、保存多少行、可展示的结果值、哪些部分未完成。
3. 明确披露 `source_truncated`；只知道保存行数时，不把它当作总体数量。模型未完成的业务推断不由模板补猜。
4. 降级结果、证据、错误和状态先形成统一有效载荷，再传入归档、Web finalize、CLI 与本地记录，防止实时有答案但历史仍为空。

建议普通最终生成失败且有可用结果时使用 `partial + 降级答案`。但当前状态优先级是显式契约，需要有范围地修改：

- persistence_error、来源隔离失败、工具集合异常等保留原状态，不因有旧证据而改成 partial；
- budget_exhausted 可保留状态并展示已有结果，不能抹掉停止原因；
- 普通模型生成失败可按明确分类转换为 partial，保留原始错误；
- 没有可信结果时生成明确失败说明，不伪造 finding 或成功记录。

当前错误主要被合并为 runtime，建议同时增加可判定的错误分类。尤其 `_archive_run()` 从 `answer_record` 提取 payload，而非直接取 `DbAgentResult.answer`；只在返回前填一个 answer 字符串会造成归档不一致。服务端降级应标记来源，不能伪装为模型调用过 `submit_final_answer`。

模型短暂故障另可接入已安装的 `ModelRetryMiddleware`：

```python
ModelRetryMiddleware(
    max_retries=2,       # 初次调用之外再尝试两次
    retry_on=is_transient_model_error,
    on_failure="error", # 耗尽后交由 Runner 统一收敛
)
```

这只负责模型调用重试，不修复 SQL，也不生成账本降级答案。还需处理：底层 HTTP 重试叠加、剩余时限、`before_model` 每步骤计数与实际请求尝试计数的区别。若要预算限制每次尝试，应把预占放到 retry 内层 wrapper，避免同时保留原预占造成双计数。摘要模型的内部调用也不能假定自动受主 Agent 的重试 wrapper 保护。

### 4.2 用状态账本和提交工具承接任务完成度

现有组合已经具备必要扩展点：

```text
create_deep_agent(state_schema=DbDeepAgentState, middleware=[...], tools=[...])
工具经 ToolRuntime 读取本轮上下文与账本
工具返回 Command(update={业务账本, messages: [ToolMessage(...)]})
submit_final_answer 检查任务与证据
Runner 再用同一校验函数复核、生成对外状态
```

不需要改 SDK 私有节点，也不需要让 Skill 直接写状态。

推荐新增 `record_sub_tasks` 和私有 `db_sub_tasks` 通道；关键字段：任务 ID、所属 run、revision、必要性、内容、依赖、状态、原因、证据。简单问题登记一条即可。将注册、修订、作废集中到同一工具，完成状态必须通过实际证据校验。

实施链条应完整覆盖：

1. **先登记后校验**：`validate_sql`、`analyze_result`、E/A 沿用检查目标任务存在且 revision 当前有效。
2. **授权按任务和用途隔离**：当前 `auth_key(ctx, sql)` 没有任务和 purpose；增加 `purpose + sub_task_id + revision`，校验与执行均透传，防止同 SQL 跨任务覆盖或借用授权。同步修改内部 probe/pushdown 路径。
3. **证据有归属**：ValidationRecord、ExecutionRecord、AnalysisRecord、EvidenceOrigin 记录任务版本。下推执行属于分析任务；probe 不参与答案覆盖。结果依赖和沿用关系保留源记录身份。
4. **提交核对覆盖**：findings 加入任务引用；成功任务须有对应版本的执行/分析主证据，失败或澄清须有原因。所有必要任务终态后才允许整体成功；有成功有失败为 partial；自然异常退出时由 Runner 收敛。
5. **执行失效检查**：计划修订后旧版本证据不能继续证明新版本完成；分析输入版本变更后必须重新分析或明确失败。
6. **共享规则**：提取 `evaluate_task_completion(view, submission)`，提交工具和 Runner 共用，避免两套判定逐渐分叉。

`TodoListMiddleware/write_todos` 可作为展示或模型记事辅助，但没有这些证据约束，不能直接成为完成度依据。`response_format` 也只约束输出结构，不能代替数据库授权和任务覆盖校验。

为防无限修正，可在任务账本记录 SQL 尝试次数与错误，耗尽后由代码转入 failed/needs_clarification。全局运行预算继续作为最后保护。这样承接旧子图的次数限制，又保留 Agent 根据错误选择补查 Schema、改 SQL 或澄清的自由。

### 4.3 原文任务方案还需澄清的实现边界

原文 §7.2 已经提出较完整的任务版本设计，方向可用，但有几处不能直接照文字编码：

- **最新版本与作废顺序**：先按任务 ID 找最大 revision，再判断该版本是否有效。不能先过滤 voided 再取最大值，否则可能在最新版本作废后把旧版本重新选为当前。
- **跨轮任务身份**：账本键含 run_id，单个 `sub_task_id` 不是全局身份。历史任务/证据来源必须能唯一定位所属 run（或使用全局任务 ID），并在 checkpoint 轮换归档后仍可读取。不能只用当前 run 的 LedgerView 判断历史任务的“最新版本”。
- **跨轮沿用规则**：推荐每轮登记本轮任务，沿用记录明确保存历史源任务完整身份、源版本和结果时效；不要要求历史任务 ID 等于本轮新任务 ID 才能复用。严格禁止复用上一版本与允许使用历史证据，需要分清适用范围。
- **并发原子性**：同一轮多个工具看到的是同一份状态快照。两个规划修订各自检查无环，不代表合并后无环；`merge_by_key` 也不能提供 compare-and-swap。应要求规划修订工具单独成批调用，整批检查后写入；不同任务的只读执行再并行。仅在 `wrap_tool_call` 周围加锁，但仍读旧快照，不能解决问题。
- **与兜底协调**：原文严格拒绝含 pending 的最终提交，而故障可能恰好发生在 pending 阶段。普通提交继续拒绝；异常收敛时由服务器记录本次中止原因并生成 partial/failed 降级，不让模型补报完成，也不能因为严格门控再次丢弃已有结果。
- **no_data 也需要证据**：0 行查询可以是正常完成；SQL 执行成功不应强制要求行数大于零。多任务整体 no_data 还要验证所有必要任务确实无数据，不能只看 findings 文案。

这些属于拟议设计的补充约束，不是声称当前已实现了一半的故障。

### 4.4 用表检查账本承接检索前置条件

建议新增 `db_table_inspections`，把 `inspect_tables` 改为接收 runtime 并返回 Command。每个成功表检查写入：数据源、run、规范表名、结构来源、检查时间、实际列结构摘要 hash。多表调用只为成功项写记录。

在 `SqlPipeline.validate()` 中，完成结果占位符展开后，从 PostgreSQL AST 提取实际物理表，再检查本轮/可沿用的结构记录：

- 复用现有 sqlglot 与 `_visible_cte_names()` 的作用域处理，避免把 CTE、别名误认为物理表。
- 裸表名根据实际 search_path 和目录核对，不能自行补 `public`。
- 不直接把 `_check_schema_scope()` 返回成功当作“已提取全部表”：该函数在未配置范围或解析失败时允许跳过；新的检查必须区分解析失败、目录不可用和真正没有物理表。
- `include_live_columns=False` 只代表目录证据；不能自动视为实时结构检查。
- 对 probe 和内部 pushdown 明确策略，不能统一要求所有服务内部 SQL 先由模型 inspect，形成循环依赖。

可先以结构化 warning 观察，再对核心查询启用硬门控。必须区分“SQL 可以执行”和“业务口径有足够依据”；EXPLAIN 成功不能证明 JOIN、金额字段、时间粒度或分母语义正确。

`LedgerView` 需同步扩展，提供任务、执行、分析及表检查查询；不能仅增加状态字段却让 pipeline 无法读取。

### 4.5 扩展时间解析，不必更换框架

可保留 `resolve_time_range` 的工具入口，在 `TimeResolver` 扩展规则及参数枚举：

- “前天”可直接确定为本地日期 `[today-2d, today-1d)`。
- “最近三周”需定义滚动 21 天还是最近三个完整自然周。
- “最近三个月/一年”需定义滚动日历区间还是完整自然周期；不能把月固定当 30 天、年固定当 365 天。
- 对产品已确定的默认语义，由代码计算边界并在答案披露；无法确定时继续澄清。

保留原文 substring 核验、固定运行时钟、时区和半开边界；同步修改工具 schema、`TOOL_VISIBLE_FIELDS` 以及闰年、月末、跨年测试。仅让 LLM 先把“前天”改写成日期再传回，当前原文核验会拒绝，也会削弱可信时间证据边界。

### 4.6 并发、依赖与幂等可以在工具/服务内实现

已安装 SDK 的同步 ToolNode 使用线程池，异步使用 gather。离线屏障探针确认同批两个工具调用实际重叠运行。项目 `RunBudgetLedger` 也已为同步并发使用锁。

两种可落地方案：

| 方案 | 能保证什么 | 边界 |
| --- | --- | --- |
| 保留多个 `execute_sql` 调用，显式传同步 `max_concurrency` 并在 DB 服务加并发限制 | 控制模型已经发出的同批调用的并发 | 不会自动发现就绪任务，也不会强迫模型批量调用；gather 的并发限制需另行验证/实现 |
| 新增 `execute_ready_batch` 可信工具 | 代码选择已登记、依赖满足、已授权、未完成的 SQL，受控并行执行并一次合并结果 | 需要任务账本、逐项失败返回、预算与重复执行协调；可承接旧批次执行语义 |

**若目标是等价承接旧调度，推荐第二种。** 不要求把 SQL 生成也并行化，旧链原本就是串行生成。

同批工具读取同一状态快照，所以“同一条模型消息同时 validate 和 execute”不能依赖前者的 Command 立即授权后者。应先完成全部校验步骤，再执行就绪批次；批次内部若统一校验执行，则只能经服务端 `validate_and_execute` 的可信记录路径，不能绕过守卫。

幂等也必须在数据库调用前落实：按 `(run, task, revision, purpose, authorized SQL/参数指纹)` 等建立执行身份，处理并发重复请求的 in-flight 占位，确认结果存在后才复用。只靠 reducer 合并证据或事后查账本，无法防止同批重复查库。进程内锁不解决跨进程恢复；若要求该范围保证，需要持久执行记录。

查询结果有时效，是否跨轮复用应显式声明；“每次重查最新值”和“沿用原结果快照”不能靠一个 SQL 缓存策略混用。多条独立连接执行也不自动共享数据库快照，新旧并发均不能由此宣称是同一时点数据。

### 4.7 SQL-only 与旧协议分别处理

如果需要替换独立 SQL 子图，建议提供服务端 `generate_only` 模式：允许检索、时间/维度核验和校验，禁止查询执行与结果分析；返回已校验 SQL、必要口径和错误。

要由服务端允许列表或执行边界强制禁止执行，不能只提示“不要执行”。现有 `include_result_tools=False` **仍保留 execute_sql**，不等价于 generate_only。探针等辅助工具也可能内部查库；SQL-only 应明确是“禁止目标 SQL 执行”还是“完全不访问数据库”，后者连当前 EXPLAIN/元数据核验都要另设策略。

保留已存在的 `submit_final_answer` 证据要求，不要为了返回 SQL 而放松 completed 必须有查询证据的通用规则；生成模式采用独立结果契约与装配 profile。工具集合变化要同步装配验证与审计。

旧 API 协议适配是另一项需求：新 evidence 无法自然还原旧复杂 Planner 的完整 sub_queries 状态。若确有旧调用方，可在新任务账本上投影有限兼容字段，并明确限制；没有调用方证据时不建议默认重建 v1。

## 5. 当前 API 支撑点与实施清单

| 扩展点 | 本次确认的现有能力 | 应承担的职责 |
| --- | --- | --- |
| `create_deep_agent(state_schema=...)` | 当前已传 DbDeepAgentState，继承 DeepAgentState | 增加私有任务/表检查通道，保留 messages 的 DeltaChannel |
| `ToolRuntime` / `Command(update=...)` | 现有 SQL/分析/提交工具已使用 | 写可信账本并返回与调用匹配的 ToolMessage |
| `AgentMiddleware` / `wrap_tool_call` | 当前预算工具拦截已使用 | 调用边界规则；不能替代跨并行调用的原子任务事务 |
| `before_model` / `after_model` 跳转 | FinalAnswerGate 已使用 | 提醒、阻止继续推理、已提交后结束 |
| `ModelRetryMiddleware` | 已安装 LangChain 中可导入 | 有限模型重试；不承担 SQL 业务修正 |
| FilesystemMiddleware | 当前已装配 | 大输出转存与受权限约束回读 |
| ToolNode 并行 | 已安装运行时具备 | 并行执行已发出的工具调用；任务调度策略仍需项目实现 |
| `checkpointer` / `store` | 当前已传入 | 持久状态与归档；不自动提供执行恰好一次语义 |

还有两个容易遗漏的项目约束：

1. 新增工具/参数必须更新 `build_business_tools`、`FULL_TOOLS/STEP4_TOOLS`、`stage_expected_tool_names()`、`TOOL_VISIBLE_FIELDS`、`verify_assembly`、相关测试及 RequestAudit 的预期集合，否则运行会因工具集合不匹配停止。
2. 若选择按阶段动态隐藏工具，当前审计要求完整固定集合，需要同步改成识别允许的阶段集合。更小改动是保留静态工具列表，在工具边界拒绝不满足前置条件的调用。

若未来要求任意自定义 LangGraph 拓扑、强制节点顺序、任意分叉/汇合，不能声称仅靠 DeepAgent 的几个 jump hook 就等价表达一切；可以在可信工具中封装小型确定性服务或组合图。但本文这些主要 NL2SQL 缺口无需先恢复整个旧父图。

## 6. 验证与处理顺序

### 本次验证

- `src/tests/db_deepagent/unit`：**661 passed，4 skipped**（[日志](2026-11-12_deepagent_unit_tests.log)）。4 个跳过项在连接池相关测试中，不计为已验证。
- 旧链的 check_completion、summarizer、sql_execution、graph 四组单测：**67 passed**（[日志](2026-11-12_langgraph_unit_tests.log)）。这只覆盖选定的旧链单元行为，不代表旧链完整集成验收。
- 首次运行因环境 SOCKS 代理缺少 socksio，出现 3 failed、4 errors；仅在重跑子进程移除代理变量后全部非跳过项通过。没有安装包或修改系统/项目代理设置。
- 专用探针：大 JSON 完整、无任务账本仍可 completed、SQL 成功后模型失败为空答案、四个受限时间表达拒绝、同批工具并发均与上述判断一致。
- 使用假模型的生产装配测试仍是离线测试，不等于真实模型业务验收。

复现命令（从 `nl2sql/` 运行）：

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
  .venv/bin/python -m pytest src/tests/db_deepagent/unit -q --disable-warnings
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
  .venv/bin/python -m pytest \
  src/tests/unit/nl2sql_father/test_check_completion.py \
  src/tests/unit/nl2sql_father/test_summarizer.py \
  src/tests/unit/nl2sql_father/test_sql_execution.py \
  src/tests/unit/nl2sql_father/test_graph.py -q --disable-warnings
PYTHONPATH=. .venv/bin/python docs/reviews/2026-11-12_deepagent_capability_probe.py
```

### 建议实施顺序

1. **先改原文过时判断**：移除当前缺陷清单中的非法 JSON 硬截断，保留历史说明；并发和旧时间能力改用准确表述。
2. **补答案兜底与有限模型重试**：优先消除“已有可信结果但无可展示答案”，同时保证实时、归档、历史与 CLI 一致。
3. **补任务账本、授权归属与覆盖门控**：这是能否承接旧 Complex Path 可靠性保证的核心；同时加入每任务 SQL 修正上限和异常收敛。
4. **补表检查门控**：先观察再强制，验证 CTE、裸名、多表及内部 SQL 例外策略。
5. **按产品契约扩时间文法、SQL-only 与旧入口**：分别验收，避免协议问题绑架主链改造。
6. **增加就绪批次执行与幂等，再优化资源复用**：共享连接池/只读客户端按数据源和配置版本管理，运行预算与上下文保持隔离；Agent 的 backend/session_dir 与会话绑定，不宜直接全局共享编译产物。
7. **最后验证等价性**：同模型、同数据、同题集对照简单题、依赖题、独立多问题、多轮、失败恢复；统一统计准确率、已规划任务覆盖、错误状态、模型尝试、token、查库次数、重复执行率和延迟。

完成这些工作后，可以对已定义的产品契约宣称替代旧业务流程；不能据此宣称移除了 LangGraph，或保证所有自然语言问题都被正确识别与完整回答。
