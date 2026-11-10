# DeepAgents 实现简化与旧链保留价值分析

- 分析日期：2026-11-10。
- 状态：当前代码与已安装 SDK 的分析结论，不是已经批准或完成的重构方案。
- 代码基线：`1c910fd`，旧 Streamlit 清理已经提交；本轮不修改业务代码、不删除旧链、不升级依赖。
- 参考：[doc 2：DeepAgents 新特性与项目收益分析](2_DeepAgents_0.7.34新特性与项目收益分析.md)。本文补充其研究时点之后的实际状态，历史描述不能直接当作待办清单。
- 路径约定：正文路径相对于仓库根目录；本地 SDK 源码路径相对于 `nl2sql/`。

## 1. 结论

当前 DeepAgents 已经承担主 Agent 循环、摘要、技能加载、文件后端和持久化接入。可以简化若干装配与通用处理，但项目的大部分自研代码承担数据库业务约束，不能用 SDK 中间件整体替换。

建议优先处理工具输出结构完整性，再做独立摘要器评估；工具按需加载和结构化最终答案应分别做隔离验证，只有验证表明净收益成立才迁入主链。显式传入默认工具消息修补中间件可作为小范围装配精简。重试、Rubric、子 Agent、事件流等主要属于能力增补，不能算作已有自研代码的直接删除机会。

旧父图和 SQL 子图不再是当前 Web 查询的必要执行链，仍有旧 CLI、历史对照和确定性流程研究价值。建议短期冻结保留；不让它们阻碍新链优化，也不在本轮直接删除。后续若决定停止维护旧 CLI、结束对照用途，再单独做退役清单。

## 2. 分析依据与当前状态

### 2.1 已安装版本

直接通过当前模块 `.venv` 的包元数据核对：

| 组件 | 当前安装版本 |
| --- | --- |
| DeepAgents | 0.7.34 |
| LangChain | 1.4.9 |
| LangChain Core | 1.7.6 |
| LangGraph | 1.2.20 |
| LangGraph checkpoint-postgres | 3.2.0 |

本次判断以这些版本为准，不声称它们是分析日最新发布版本。Context7 官方仓库资料用于核对概念；具体参数和默认装配以当前安装源码为准，不把 `main` 分支能力直接套用到固定版本。

### 2.2 doc 2 中需要更新理解的内容

| doc 2 内容 | 当前核对结果 |
| --- | --- |
| 公开 `permissions=` 可以替代 `_permissions` | **已经完成**。`agent.py` 的文件中间件和 `create_deep_agent()` 均传同一组公开权限参数，不再作为新任务 |
| 0.7.25 → 0.7.34 的升级候选与旧版本表 | 升级已经落地，应使用上表作为当前基线；旧表保留研究时点含义 |
| pandas 仍直接声明在 web extra 等旧说明 | 旧 UI 清理后 web extra 已删除，pandas 作为其他组件的传递依赖保留，不据旧描述恢复依赖 |
| 独立摘要器 | 已接入（2026-11-10，[doc 5](5_独立摘要器接入修改方案.md)）并独立配置摘要模型（同日，[doc 5.1](5.1_摘要器独立LLM配置与Qwen3.8-Flash接入方案.md)）：`model=主模型(qwen3.8-max), summarizer=build_summarizer_model 产物(qwen3.8-flash)`，离线回归、完整图分类与低阈值真实调用验证通过 |
| 工具按需加载、结构化输出 | 仍未在当前主 Agent 按这些方案启用，需继续评估 |

依据：[当前依赖声明](../pyproject.toml)、[Agent 装配](../src/modules/db_deepagent/agent.py)、[旧 UI 清理实施记录](3_NL2SQL旧前端清理设计.md)。

### 2.3 当前新链的关键职责

- `agent.py`：装配模型、文件后端、权限、SDK 与项目中间件，核验工具和状态通道。
- `tools/`：十个业务工具加最终提交工具；默认再加四个只读文件工具，共十五个模型可见工具。
- `runner.py`：一次运行的准备、invoke、状态读取、证据投影、归档和收尾，线程轮换与摘要交接。
- `services/`：检索、时间/维度证据、SQL 授权与只读执行、结果存储和分析、运行预算。
- `src/api/v2/`：Web 会话、数据源就绪、运行生命周期、结果快照与接口协议。

这里既有框架装配，也有业务语义。是否可以减少代码，要逐项判断其语义是否已被 SDK 完整覆盖。

## 3. 可直接评估的小范围精简

### 3.1 默认工具消息修补中间件的显式覆盖

**已实施（2026-11-11，[doc 8](8_默认工具调用修补中间件装配精简方案.md)）：** `build_db_agent()` 已删除显式导入与实例，修补职责由 SDK 默认实例承担（同名原位替换机制下显式实例本就占据默认栈槽位，删除前后合并栈一致）。行为保留测试（正常/缺失/部分/畸形历史 + 同检查点二次 invoke + 钩子计数 + 预算零消耗）在删除前后均通过；离线回归 823 passed/4 skipped/0 failed。下文为实施前分析，保留原文。

当前 `agent.py` 导入并显式传入无配置的 `PatchToolCallsMiddleware()`。安装版 `deepagents/graph.py` 在默认栈中已经创建同类实例，且 `_apply_custom_middleware()` 按名称原位替换。

因此当前不是执行了两遍修补，而是用一个等配置实例替换默认实例。省去这个显式导入和传参，理论上仍由 SDK 默认栈完成相同工作。

本轮局部探针核对了两种合并方式：均只含一个修补中间件，对同一条悬空工具调用产生相同修补结果。该证据支持将其列为低风险精简候选，但不等于已经完成整套 Agent 删除前后回归。

收益很小，主要是减少不必要装配代码；无需为此建立新的适配层。后续实施只需检查正常工具调用、悬空/中断调用恢复和现有装配测试。

不能顺带删除自定义文件中间件或摘要中间件：前者限定只读工具，后者有项目阈值与观测标记，确有自定义职责。

## 4. 有价值但需要验证的替代方案

### 4.1 工具输出：优先保证结构完整，再评估 SDK 转存

**试点已实施（2026-11-11，[doc 7](7_工具大输出SDK转存接入方案.md)）：** `inspect_tables` 改为直接返回完整缩进 JSON，不再走 `finalize_output`；超过 SDK 转存阈值（≈80000 字符）时自动写入 `/artifacts/large_tool_results/` 并可按行回读。下述建议 2、3（转存位置/权限回读/`Command` 账本保留/模型实际可见内容）已由 doc 7 的离线测试逐一验证（含完整生产图：动态假模型从替换消息提取路径并回读到末列标记）。建议 1（其余工具返回包的结构完整性）**仍未实施**：`finalize_output` 的 JSON 中部截断缺陷对 read_result/analyze_result/search_schema/plan_joins/guard/SQL 等工具保持原状；真实大输出样本与真实模型主动回读行为未验证。以下为实施前分析，保留原文。

当前 [tools/_envelope.py](../src/modules/db_deepagent/tools/_envelope.py) 的 `finalize_output()` 会先压缩部分字段，仍超长时执行 `text[:max_chars]`。这能限制长度，却不能保证 JSON 仍可解析。

本轮使用与结果工具相似的 `rows` 负载和当前配置上限 12,000 字符执行局部探针：返回长度为 12,000，`json.loads()` 失败。现有 `test_envelope.py` 的硬截断测试只检查长度与警告，没有检查该分支的 JSON 完整性。

建议：

1. 先让压缩后的返回包始终保留合法结构、错误/截断标记及必要标识，避免在 JSON 文本中间截断。
2. 再选非关键长文本或大预览，验证能否交给 SDK 的工具输出转存，减少通用硬截断逻辑。
3. 必须验证转存位置、现有权限下的可回读性、`Command` 账本更新保留，以及最终模型实际看到的内容。

不能替换 [ResultStore](../src/modules/db_deepagent/services/result_store.py)：它还负责数据库结果类型、分页、来源、`result_ref`、源结果截断与历史结果读取；SDK 通用文件转存不会恢复数据库阶段已丢弃的数据。

这项具有实际正确性价值，但本轮只发现和复现了辅助函数问题，没有修改实现或验证所有真实工具输出。

### 4.2 摘要：使用主模型上下文参数，独立选择摘要生成器

**已实施并独立配置摘要模型（2026-11-10，[doc 5](5_独立摘要器接入修改方案.md) → [doc 5.1](5.1_摘要器独立LLM配置与Qwen3.8-Flash接入方案.md)）**：doc 5 先以 `model=主模型, summarizer=主模型副本` 显式构造 `SummarizationMiddleware`；doc 5.1 随后将摘要来源改为独立构造器 `build_summarizer_model(cfg)`——按 `summarization.llm_profile=qwen3_8_flash`（qwen3.8-flash，dashscope_openai 兼容路径，temperature=0、max_tokens=8192、enable_thinking=false、独立超时 120 秒、独立 overrides 不继承主模型参数）经 `get_llm()` 创建，并带 `dbda_call_kind=summarization` 用途标记。主模型（qwen3.8-max）继续提供上下文依据与 token 计数。阈值 96000/24000、`trim_tokens_to_summarize=None`、backend、prompt、观测与线程交接全部保留；离线回归（808 passed/4 skipped/0 failed）、完整图分类与低阈值真实调用（主=openai:qwen3.8-max、摘要=openai:qwen3.8-flash 逐调用断言）通过，详见 doc 5.1 实施记录。

原建议“将‘上下文依据’和‘实际摘要生成器’分开”已完全落地：两者现为不同 profile 的独立模型。摘要质量/成本/延迟与生产阈值容量的对照仍未验收，不据此宣称收益。

必须保留摘要用途元数据、回调观测、现有阈值与 `trim_tokens_to_summarize=None`，并验证线程交接。`model_copy()` 路径已按 doc 5.1 移除，用途标记职责由 `build_summarizer_model()` 承担。

这主要改善配置与职责划分，代码行数不一定减少；没有实际测量前不能宣称更快、更便宜或更准确。

### 4.3 Skills：先评估关键技能固定加载，再评估工具延迟暴露

当前三个技能已经由 SDK 加载，但全部十个业务工具仍一次性注册并暴露；并未通过 `SkillsMiddleware(tools=...)` 分阶段披露。

可以分两步评估：

- 小范围：使用 SDK 已有的 `pinned_skills` 状态能力，验证关键规划说明更稳定可见是否有收益；保持工具集合和强制业务规则不变。
- 后续：仅当工具数量或请求开销确有压力时，评估按技能延迟披露工具。

工具延迟披露会改变当前十五工具的静态约定。`verify_assembly()`、`FULL_TOOLS` 和 `RequestAuditMiddleware` 不能直接沿用“每次模型请求必须等于固定集合”的全部假设。

特别是 Skills 位于自定义中间件之后，当前 RequestAudit 在调用内层 handler 前读取 `request.tools`，不能据此证明已审计后续披露到模型的工具。需要区分注册池检查、实际出站工具检查和执行授权；仅修改 expected set 不充分。

这属于调用协议变化，当前不宜仅为减少几行工具装配代码而接入。

### 4.4 最终答案：SDK 可替代格式处理，业务证据接受逻辑保留

当前 [final_answer.py](../src/modules/db_deepagent/tools/final_answer.py) 不只是生成答案 Schema，还检查独立提交、重复提交、证据归属、禁止引用探针和状态一致性，并写入 run 级 `FinalAnswerRecord`。`FinalAnswerGateMiddleware` 控制接受后结束与漏提交提醒；Runner 从 `db_final_answers[run_id]` 读取正式结果。

`response_format` / `ToolStrategy` 可以接管结构生成、Schema 错误反馈和 `structured_response`，不能自动完成上述业务接受流程。

建议先提取可复用的业务证据校验函数，再做隔离对照：候选结构形成、业务校验、拒绝后修订、正式提交记录、Runner 读取与错误映射。还要处理固定工具审计与框架内部追加输出工具的关系。

若迁移后反而要维护更多桥接状态和修订机制，应继续保留当前提交协议。现阶段不建议直接删除提交工具和门控中间件。

## 5. 暂不作为“减码”项目的能力

| 能力 | 当前判断 | 原因 |
| --- | --- | --- |
| ModelCallLimit / ToolCallLimit | 不整体替换 RunBudgetLedger | 当前有线程锁预占、失败不退额度、数据库执行/探针/分析下推统一计数、deadline 与稳定 stop_reason；原生计数语义不同 |
| 模型/工具重试、模型回退 | 按实际故障补能力 | 主 Agent 未实现一套等价自研重试中间件，接入主要是新增可靠性能力；需避免叠加模型客户端重试，校对预算与审计 |
| Rubric | 先考虑离线质量评估 | 当前最终提交接受后即结束，在线评分修订需要新控制流，还会增加模型调用，不能替代证据守卫 |
| 同步/异步子 Agent | 有明确任务分工需求再引入 | 当前单主 Agent、通用子 Agent 被禁用；异步任务还涉及运行归属、预算、锁与生命周期，不属于本轮减码 |
| v3 事件流 | 后续前端交互增强再评估 | 当前同步 API 没有同等自研流式系统可删除；会涉及 API 和 UI，本轮不扩入 |
| Todo / Memory | 保留现有开关，按产品需求启用 | 属于新增运行能力，不能把开启配置当成简化了查询实现 |
| LangGraph checkpoint / store | 继续使用 | 已经是 SDK 运行底座与现有持久化的一部分，不是待删除的旧编排代码 |
| SDK 私有适配与装配核验 | 继续集中维护，不先删除 | Harness 匹配核验、摘要交接读取、工具 Schema 与状态通道核验仍有职责；公开权限参数已迁移不代表全部私有依赖都能去除 |

依据：[运行预算](../src/modules/db_deepagent/services/run_budget.py)、[SQL 管线](../src/modules/db_deepagent/services/sql_pipeline.py)、[Runner](../src/modules/db_deepagent/runner.py)、[配置](../src/modules/db_deepagent/config/db_deepagent.yaml)。

## 6. 旧链还有什么价值

### 6.1 当前产品执行关系

- Web 入口只挂载 v2；`src/api/v2/runs.py` 装配 `build_db_agent()`。
- `scripts/nl2sql_father_cli.py` 直接调用旧 `run_nl2sql_query()`。
- `scripts/nl2sql_subgraph_cli.py` 直接调用旧 `run_sql_generation_subgraph()`。
- 对 `src/modules/db_deepagent/` 和 `src/api/v2/` 的 Python AST 直接导入扫描，没有发现对旧父图、旧 SQL 子图或旧 `src/services/langgraph_persistence` 包的导入。

这证明新入口没有直接依赖旧编排包；不是针对所有动态导入、配置与历史脚本的完整删除证明。

### 6.2 有限但真实的保留价值

| 价值 | 当前证据 | 保留建议 |
| --- | --- | --- |
| 独立 CLI 与诊断 | 两个旧 CLI 仍直接调用旧链，scripts README 仍有用法 | 停止支持旧 CLI 之前保留入口及必要实现 |
| 基线对照 | 旧链可以用于比较固定流程与新 Agent 的行为 | 保留代码和固定题集，不代表本轮已重新实测旧链或证明对照环境可运行 |
| 确定性流程研究 | 父图显式区分简单/复杂路径，有规划、参数注入、批量 SQL 生成、并行执行与完成检查；子图有解析/检索/生成/校验回路 | 可作为流程设计参考；不能未经实测就宣称新 Agent 具有完全相同的执行顺序和结果 |
| 历史会话与问题追溯 | 旧持久化、旧路由和旧测试仍对应历史协议 | 代码退役与数据清理分开；本次不删历史数据 |

目前没有发现当前 Web 必须通过旧图才能获得的业务工具能力。新链已有检索、时间/维度证据、SQL 校验执行、多步结果引用及结果分析；旧图的确定性调度方式仍是不同实现，不能仅凭工具名称相近宣布所有复杂场景等价。

### 6.3 不宜整目录删除共享代码

新链仍使用仓库公共能力，例如 `src.services.config_loader`、`src.services.llm_factory`、`src.services.db.neo4j_connection` 和 `src.tools.schema_retrieval.table_names`。这些文件可能来自旧实现时期，但当前仍被新链引用。

因此旧图退役时，应按实际引用区分旧编排专用代码与公共服务，不能连同 `src/services/`、`src/tools/` 一并清除。

### 6.4 保留结论与退出条件

建议现在冻结旧链的功能扩展，将日常改进集中到 `db_deepagent`。暂时保留旧 CLI、图与对照资料，不把旧链作为当前 Web 的第二套在线执行路径。

后续满足以下条件时，可另行退役旧编排：

1. 明确停止支持两个旧 CLI，或已用新 CLI 承接所需用途。
2. 不再需要执行旧链做对照，必要的基线代码与证据可以通过版本历史定位。
3. 对简单查询、复杂结果依赖、独立子问题、多轮、失败和预算等场景完成能力核对，明确接受实现差异。
4. 完成代码、配置、脚本、测试和文档的引用清单；共享服务与历史数据另行保留。

无需等到旧链删除后才改进 DeepAgents，两者可以独立推进。

## 7. 建议的下一步顺序

1. **先处理工具返回结构问题**：明确保留字段与超长结果策略，再验证 SDK 转存能否减少通用截断处理。
2. **做摘要器隔离对照**：保留主模型的上下文依据，单独配置实际摘要生成器；核对质量、观测与交接，不急于更换模型。
3. **顺带清理等价装配覆盖**：已完成（2026-11-11，[doc 8](8_默认工具调用修补中间件装配精简方案.md)）——显式覆盖已删除，行为保留测试与回归通过。
4. **再决定工具披露与答案协议是否值得迁移**：分别验证净收益，不捆绑实施。
5. **旧链先冻结保留**：只有明确停止其 CLI/对照用途后，再制定删除清单。

这是分析后的优先级建议，不是本轮开始实施这些改动的声明。

## 8. 验证边界与来源

本轮完成：源码与依赖声明阅读、安装版本核对、指定新链目录的直接导入扫描、默认修补中间件的局部行为对照、超长工具返回的 JSON 解析探针。

未完成：删除中间件后的完整装配回归、独立摘要器真实对照、Skills 延迟工具/结构化答案迁移、真实数据库/模型业务验收、新旧链准确率/成本/延迟比较。未调用真实模型或数据库，未修改业务代码与依赖。

版本精确源码位置：

- `.venv/lib/python3.12/site-packages/deepagents/graph.py`：默认中间件栈、按名称替换、自定义装配与 LangGraph 返回类型。
- `.venv/lib/python3.12/site-packages/deepagents/middleware/summarization.py`：`model` 与 `summarizer` 的职责。
- `.venv/lib/python3.12/site-packages/deepagents/middleware/skills.py`：延迟工具与技能状态能力。
- `.venv/lib/python3.12/site-packages/langchain/agents/middleware/`：框架调用限制、重试等能力。

官方概念参考：[DeepAgents 概览](https://docs.langchain.com/oss/python/deepagents/overview)、[DeepAgents 源码仓库](https://github.com/langchain-ai/deepagents)、[LangChain 结构化输出](https://docs.langchain.com/oss/python/langchain/structured-output)。这些页面可能随版本变化，本文结论以本地安装版本的核对结果为准。
