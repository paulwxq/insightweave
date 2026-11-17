# db_deepagent SQL 生成子 Agent 隔离方案

> 状态：设计稿待审。日期：2026-11-14。编号沿用现行文档序号（本文为 15）。
> 依据：2026-11-13/14 会话裁定与两次真实查询日志（req_72fc5d…、req_9322…）、doc 13 上下文日志、
> 安装版 DeepAgents 0.7.34 / LangChain 1.4.9 / LangGraph 1.2.20 源码核对。
> 关联：doc 6.3（任务账本，本方案**冻结不动**）；doc 13（上下文日志，验收依赖它）。

## 1. 目标与范围

把 **SQL 生成**从主 Agent 中隔离为一个**密封车间式子 Agent**：主 Agent 只做业务级拆解与编排，
子 Agent 在隔离环境内完成检索螺旋、SQL 生成、草稿验证与自我修复，把**验证过的 SQL 文本**（或
结构化失败/阻塞原因）作为唯一交付物返回。主 Agent 保留账本、授权、执行、汇总与提交的全部职责。

本方案不改任务账本（6.3 冻结）、不改 guard/预算/兜底（6.2）、不改对外 API 与前端。

**不做的**：不拆"执行/汇总"为子 Agent；不在子 Agent 与主 Agent 之间共享任何图状态；不引入
子 Agent 记忆（每次 `task()` 调用独立）；不修改 validate→execute 授权握手语义。

## 2. 架构总览

```
主 Agent（编排层；账本、授权、执行、提交）
  澄清判断 → record_sub_tasks（账本不动）
       │
       ├─ 任务可沿用 → reuse_evidence（不派车间）
       │
       └─ task("为 T1 生成 SQL：<任务书>")          ← 框架注入的 task 工具
                │ 只有文本进入
                ▼
        ┌─ 密封车间（声明式 SubAgent：sql_gen）────────────┐
        │ 工具全部为闭包工厂，不写任何图状态、不依赖 ctx：    │
        │  检索螺旋（search_schema/inspect/plan_joins/      │
        │   resolve_time_range/probe，证据存本地字典）       │
        │  → 生成 SQL → check_sql_draft（草稿验证）          │
        │  → 失败自修（≤3 次，第 4 次调用被确定性拒绝）       │
        │ 出口：一条最终消息 = SQLGEN_RESULT 结构化文本        │
        └──────────────┬───────────────────────────────────┘
                       │ 只有文本返回
       收到 SQL → 证据官方登记（resolve_time_range / search_schema(dim_values)
                  / 必要时 probe——确定性重放，写入正式状态）
              → validate_sql（正式授权，一次过）→ execute_sql
              → analyze_result → record completed → submit_final_answer
```

两条铁律：

1. **隔离**：子 Agent 不写图状态、不读主 Agent 账本、不需要 context 传播即可完成本职；
   进出车间的只有任务书文本与结果文本。
2. **单一公章**：草稿验证（车间内）是质量反馈回路，随车间销毁；正式授权（主 Agent
   validate_sql）是唯一写状态的验证，永远只有一个写者。

## 3. 职责切分

| 维度 | 主 Agent | 子 Agent（sql_gen） |
| --- | --- | --- |
| 拆解 | 业务级：一个问题拆几次执行、任务间依赖（业务结果层面） | 不拆，收到的是一个任务的描述 |
| 分析 | 不识别指标/维度/时间口径 | 指标、时间、维度、粒度、比较基准的识别与证据获取 |
| 检索 | 不做结构探索（仅证据官方登记，见 §6） | 完整检索螺旋（八件工具，§5） |
| SQL | 不撰写；核对车间交付物 | 生成、草稿验证、三次内自修 |
| 账本/授权/执行/提交 | 全部保留，行为不变 | 一律不接触 |
| 澄清 | 收到阻塞原因后决定：问用户或 needs_clarification | 只能把阻塞原因写进返回物 |

依据：现行系统提示词"任务 = 需要交付的业务问题或必要中间结果，不是工具步骤"——本方案把
"分析细节"进一步明确为车间职责，主 Agent 上下文从此不出现 schema/证据细节。

## 4. 交互契约

### 4.1 任务书（主 → 子，`task()` 的 description 参数）

模型面向文本，由 query-planning skill 规范写法；必含：业务问题（含用户原话）、必要背景、
可用前序结果（`result_ref` + 列名，供 `{{result:...}}` 占位符）、已澄清的口径（若有）。
不含：schema 细节（车间自查）、账本信息、run_id（车间不需要）。

### 4.2 返回物（子 → 主，最终一条 AI 消息）

```
SQLGEN_RESULT
status: ok | failed | blocked
sql: <多行 SQL 文本>                       # status=ok
verified_values: 商品05@products.product_name …   # 供主 Agent 证据登记参考
used_time_range: >=2026-10-01 AND <2026-11-01     # 若有时间过滤
failures: 3                                 # status=failed
block_reason: dim_value_unmatched | time_ambiguous | schema_conflict | …
notes: …
```

返回物是**建议性文本**（主 Agent 仍会走正式验证），其格式由 sql-generation skill 与车间提示词
约束，验收用脚本模型断言。`blocked` 不算失败次数熔断，是车间判断"无法继续"（如维度值两路
验证均未命中且口径不明），主 Agent 决定向用户澄清或登记 needs_clarification。

### 4.3 三次熔断（用户裁定：同一问题三次验证不过即返回失败）

- **计数**：`check_sql_draft` 从**本车间自己的 messages** 中数本工具的失败 ToolMessage——
  无状态计算，每次 `task()` 调用天然归零，无跨调用泄漏；
- **执行**：第 4 次调用被工具**确定性拒绝**（返回"已三次失败，立即按 failed 格式返回"的信封），
  不依赖模型自觉；提示词同步告知熔断规则；
- **返回**：`status: failed, failures: 3` + 最后一次错误摘要；主 Agent 按现行规则处置
  （登记 failed/agent_abandoned 或换路径），不新增账本机制。

## 5. 子 Agent 工具集（八件，全部闭包工厂、ctx 无关）

| 工具 | 后端 | 与现行的差异 |
| --- | --- | --- |
| search_schema（tables/columns/dim_values/sql_examples 四 kind） | Milvus | 命中写**本地证据字典**，不写 db_dim_evidence 通道 |
| inspect_tables | 表目录 | 原样复用服务 |
| plan_joins | Neo4j 外键图 | 原样复用服务 |
| resolve_time_range | 规则解析器 | 证据写本地字典 |
| probe_dimension_values | 业务库只读探针 | 轻量变体：不走管线准入/会话租约；本地计数上限（默认 5 次/调用）防失控 |
| **check_sql_draft**（新增） | 复用 DbSqlValidator + guard | **新增工具**：normalize→安全检查→解析→EXPLAIN→输出列检查→guard（用本地证据字典）；**不含**会话租约、任务准入、血缘核验、db_execute 预占、授权记录写入、状态 Command |

不提供给车间：execute_sql、record_sub_tasks、submit_final_answer、reuse_evidence、
read_result/analyze_result（执行与账本是主 Agent 领地；结果读取由主 Agent 在任务书里灌入）。

### 5.1 主 Agent 工具集变化

| 保留 | 移除（移交车间） | 新增 |
| --- | --- | --- |
| record_sub_tasks、validate_sql、execute_sql、read_result、analyze_result、reuse_evidence、submit_final_answer | inspect_tables、plan_joins（纯结构探索，主 Agent 不再需要） | task（框架随子 Agent 注入） |
| search_schema（**仅用于证据官方登记**，见 §6）、resolve_time_range（同左）、probe_dimension_values（登记兜底） | | |

`stage_expected_tool_names` 与 `verify_assembly` 同步更新，否则 RequestAudit 将以
tool_set_mismatch 停机。

## 6. 证据官方登记（隔离带来的必要配套）

**问题**：guard 要求 SQL 中的时间/维度字面值在**正式状态**里有证据（`evidence_of_run`）。
车间在本地字典里验证过，但本地证据随车间销毁——主 Agent 直接 validate_sql 会被 guard 拒绝。

**方案（首版）**：主 Agent 收到 `status=ok` 的 SQL 后，先做**证据官方登记**——对返回物中
`verified_values` 与 `used_time_range` 重放确定性工具（search_schema(dim_values) /
resolve_time_range，必要时 probe），把正式证据写入状态，再 validate_sql。

- 这些工具全部确定性（规则解析器 + 向量索引），重放结果与车间一致，无口径漂移；
- 登记与验证分两轮（同批工具看不到新写入的证据，现行合并语义不变）；
- 成本：带过滤条件的 SQL 约 +1~2 个主 Agent 轮次（登记轮 + 验证轮），换取隔离成立。

**后续优化（不在本版）**：正式 validate_sql 服务端自证——对无本地证据的字面值在管线内直接
向 Milvus/规则解析器复核，省去登记轮。改变 guard"证据须先经工具写入"的契约，另立文档。

## 7. skill 与提示词切分

| skill | 去向 | 内容 |
| --- | --- | --- |
| query-planning | 主 Agent（瘦身） | 业务级拆解、依赖声明、**车间任务书规范**、澄清判断时机 |
| result-analysis | 主 Agent（不动） | 读取/分析/组织答案 |
| schema-retrieval | **整体搬入车间** | 找表找列、维度值验证、JOIN——车间操作手册，基本原文 |
| **sql-generation**（新增） | 车间专用 | 指标/时间/维度/粒度识别 → 检索螺旋 → SQL 生成规则（两段式表名、别名、占位符、时间区间写法）→ check_sql_draft 修复纪律 → 三次熔断 → 返回物格式 |

命名裁定（用户）：用 **generate** 语义（"write"与只读系统冲突）；技能名取名词形式
`sql-generation` 与现有三技能（planning/analysis/retrieval）一致。

提示词（prompts.yaml）同步切分：现主提示词的"## 表与字段 / ## 时间 / ## 维度值 / ## SQL 流程"
移入车间 system_prompt；"## 业务任务登记 / ## 结果 / ## 结束"留在主 Agent；主提示词新增
"收到 SQLGEN_RESULT 后的处置流程（§6 登记 + 验证 + 执行）"。

SDK 事实（graph.py:451-455）：声明式 SubAgent 可覆盖 `system_prompt/tools/model/middleware/
interrupt_on/skills/permissions/response_format`——**skills 与 middleware 均可独立指定**，
主/子技能不同是声明参数，无需框架改造。共享同一 FilesystemBackend（/skills/ 挂载），"不同"
通过披露列表实现；车间 permissions 仅放开 `/skills/**` 只读。

## 8. SDK 事实与实施前验证项

已核对（graph.py / subagents.py / tool_node.py）：

- `task()` 调用同步执行子图，父 callbacks/tags/configurable 自动传播（subagents.py:885-897）
  → observer 能看到车间模型调用的 token；
- 子 Agent 终态里非私有变更会回流父状态（subagents.py:784-816）——本设计车间**不产生**
  状态变更（只有 messages），回流自然为空，隔离成立；
- 子 Agent 不继承父 middleware，须在 spec 里显式传；
- 声明式 spec 无 state_schema/context_schema——本设计不需要（不写状态、工具 ctx 无关），
  这正是选择声明式而非 CompiledSubAgent 的原因。

**验证项 V1（写代码前先跑探针，约 30 分钟）**：车间内 `runtime.context` 是否继承主 Agent 的
DbAgentContext（决定预算中间件可用性）。

- 若传播成立（预期）：spec.middleware 传 RunBudgetMiddleware(services.budget, cfg.budget)，
  车间模型/工具调用按 run_id 正常预占记账；
- 若不成立：首版接受车间模型调用不占 model_call 预算（observer 仍统计 token，预算兜底由
  全局 run_timeout + 主 Agent 轮次约束），probe 用 §5 本地上限约束；后续如需严格记账再升级
  CompiledSubAgent（仅 context_schema 一处差异，其余设计不变）。

**验证项 V2**：车间模型调用的 `dbda_call_kind` metadata 注入点——核对主 Agent 现有注入机制
（观测器按它区分 agent/summarization；缺失会记 anomaly、token_usage_complete=False），在
车间 spec 复用同一机制（middleware 或模型 config）。

## 9. 修改文件清单（首版）

| 文件 | 修改 |
| --- | --- |
| services/sql_workshop.py（新增） | 车间闭包工具集：本地证据字典、check_sql_draft（复用 DbSqlValidator+guard）、probe 轻量变体与本地计数、三次熔断计数 |
| agent.py | build_workshop_subagent()（声明式 spec：prompt/tools/middleware/skills/permissions）；主 Agent 工具集增删；装配 subagents=[workshop]；stage_expected_tool_names/verify_assembly 同步 |
| config/prompts.yaml | 车间 system_prompt；主提示词瘦身与 SQLGEN_RESULT 处置流程 |
| skills/sql-generation/SKILL.md（新增） | §7 内容 |
| skills/query-planning/SKILL.md、schema-retrieval/SKILL.md | 瘦身 / 归位说明 |
| middleware/run_budget.py（视 V1） | 车间 middleware 装配 |
| src/tests/db_deepagent/unit/test_sql_workshop.py（新增） | 本地证据、草稿验证、熔断、隔离性（父状态零写入） |
| test_assembly.py、test_request_audit.py 等既有装配测试 | 工具集期望更新 |

不改：sql_pipeline.py 正式路径、task_ledger、runner、API、前端、logging。

## 10. 验收矩阵（离线，脚本模型 + 真实编译图）

| 编号 | 断言 |
| --- | --- |
| B01 隔离 | 车间执行期间父图状态除 messages 外零变更；车间结束后 db_validations/db_dim_evidence 仅由主 Agent 的登记/验证写入 |
| B02 端到端 | 拆解→task→车间返回 ok→登记→validate（一次过）→execute→submit；全程账本行为与现行一致 |
| B03 自修复 | 车间内草稿验证失败→修正→通过；返回的 SQL 正式验证一次过 |
| B04 三次熔断 | 注入三次不可修复错误：第 4 次 check_sql_draft 被拒；返回 status=failed, failures=3；主 Agent 登记失败，无部分 SQL 执行 |
| B05 阻塞回传 | 维度值两路验证均未命中：返回 blocked+原因；主 Agent 走澄清/needs_clarification，不执行任何 SQL |
| B06 证据登记 | 带维度值+时间过滤的 SQL：登记轮证据与车间本地判定一致（确定性）；正式 validate 通过 |
| B07 并行一致 | 同一时间表达在两个并行车间/两次调用解析结果一致（TimeResolver 确定性） |
| B08 预算记账 | 视 V1：车间模型调用计入 model_call 与 observer 统计；或明确记录不记账的边界 |
| B09 技能切分 | 主 Agent 披露 query-planning/result-analysis；车间披露 schema-retrieval/sql-generation；互不泄露 |
| B10 提示词切分 | 主提示词无 SQL 细节章节；车间提示词无账本/提交章节 |
| B11 工具集边界 | 车间无 execute/record/submit/reuse；主 Agent 无 inspect/plan_joins；RequestAudit 零 mismatch |
| B12 上下文日志 | doc 13 的 MODEL_INPUT 对主/车间调用均可完整记录，attempt_id 可区分归属 |

## 11. 成本与边界

- 常规成本：每个生成的 SQL = 主 Agent 派发 1 轮 + 车间内 2~4 次模型调用（检索/生成/验证）+
  登记轮（带过滤时）；对比现行（主循环内检索+生成+验证≈4~6 轮），总轮次相近，**主 Agent
  上下文不再累积检索与修复噪音**（今晚日志中 126 行 RAG 返回块以后只活在车间里）；
- 双验证：check_sql_draft 与正式 validate 各跑一次 EXPLAIN（只读，无 db_execute 预占，与现行
  validate 语义一致）；
- 边界：车间无跨调用记忆、无用户交互；沿用由主 Agent 决定（可沿用则不派车间）；本方案不
  承诺车间模型调用的强预算记账（视 V1 结果如实记录）；不处理车间内部超时（由 run_timeout
  与模型调用预算兜底）。

## 12. 实施顺序

1. 探针验证 V1/V2（不改业务代码）；
2. test_sql_workshop.py：本地证据/草稿验证/熔断/隔离四组红测；
3. 实现 services/sql_workshop.py 转绿；
4. 装配：spec、工具集增删、提示词与 skill 切分、期望工具名同步；
5. test_assembly/test_request_audit 更新 + B01–B12 全绿；
6. 跑 db_deepagent unit + llm_factory + api/v2 回归；真实查询人工验收时用 doc 13 日志核对
   主/车间输入输出。
