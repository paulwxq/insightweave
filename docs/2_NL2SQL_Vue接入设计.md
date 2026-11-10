# 2. NL2SQL Vue 接入设计

起草日期：2026-10-17；本轮审核更新：2026-10-18。状态：**D0–D3、D6、D8 已获用户确认；D4/D7 按本轮要求作出设计决定并定稿：首版同步返回、知识版本变化要求新建会话，不再作为待选项。doc 4 的多源后端基础能力已实现并经代码复核，Vue 与 Web API 适配尚未实施。设计定稿不表示代码已经实现**。

本文依据当前工作区代码及 [doc 4：多数据源知识隔离与 source_id 链路改造](4_多数据源知识隔离与source_id链路改造.md) 的交付结果同步，区分已实现能力、Web 待实施内容和部署验收。此前代码复核已运行两模块单元测试，并对真实 PostgreSQL 的完整 SQL 校验管线和部分知识库信息做只读验证；这些记录不等于 Vue 联调、真实 Neo4j 双源发布或迁移验收完成。本次同步仅修改本文，不执行代码开发、数据库迁移或发布。总体方向见 [0 号文档](0_项目整合讨论与共识.md)，启动方式见 [1 号文档](1_服务启动指南.md)。

## 1. 目标与决策清单

在统一 Vue 应用中提供数据库问答入口，复用现有界面风格，覆盖问题提交、续聊、当前查询的答案/SQL/结果表格及历史会话。**查看历史记录时，每轮仅返回问题和答案，不返回 SQL 或查询结果表格。** 保留 MetaWeave 现有功能，两个 FastAPI 继续独立运行。

本轮按用户最终澄清收敛为：**Vue 只接新的 db_deepagent；旧父图只通过脚本进行对比测试，不做 Vue 接入。** 已确认需求与本稿设计取舍如下；设计选定不冒充用户此前的单独确认，也不表示已经实施代码。

| 编号 | 问题 | 选定方案或当前建议 | 说明 | 状态 |
| --- | --- | --- | --- | --- |
| D0 | Vue 使用哪条执行链？ | 仅 db_deepagent | 不在 Vue/Web API 加旧父图切换；旧父图只作脚本对比 | 已确认（2026-10-17） |
| D1 | 数据源范围？ | 支持选择平台 `db_source_info` 登记的数据源 | 复用现有列表 API，后端贯通按源连接和检索 | 已确认（2026-10-17） |
| D2 | 登录与身份？ | 不做统一登录，默认 guest | 不提供手输 user_id；所有 guest 使用同一身份，不承诺多用户隔离 | 已确认（2026-10-17） |
| D3 | 历史展示与多轮上下文？ | 历史轮次只返回问题和答案，不返回 SQL、表格或结果引用 | 这是当前确定的范围，不预留历史详情回放入口；多轮上下文由 checkpoint 恢复，以后如需变更另行设计 | 已确认并再次澄清（2026-10-17） |
| D4 | 首版是否要求流式答案或实时步骤？ | 首版同步提交、等待后一次返回，不实现流式或实时步骤 | 冻结本文 WebQueryResult 与执行生命周期；以后增加流式另行设计事件及断连协议 | 已定稿（2026-10-18，本轮设计决定） |
| D5 | 是否接入旧 Streamlit 历史？ | 不作为本次 Vue 接入范围 | 旧链仅脚本对比，原数据保留，不自动迁移 | 按 D0 收敛 |
| D6 | NL2SQL 如何读取源连接及密码？ | 只读访问同一平台库，用同一后端密钥解密 | 配置、登记读取、解密及按源配置已交付；显式只读连接/事务保证待补 | 已确认（2026-10-17），部分交付 |
| D7 | 知识发布更新后是否继续原会话？ | 保存知识版本指纹，变化即拒绝续聊并要求新建会话 | 不做多版本知识存储/读取；首版知识发布与 NL2SQL 查询按 §5.5.1 错开执行，旧文本和已持有的有效结果引用仍可读 | 已定稿（2026-10-18，本轮设计决定） |
| D8 | 普通发布时是否暂停查询？ | 发布知识期间先不使用 NL2SQL，发布完成后再用；先等待在途查询完成，再发布 | 首版本地单用户按 §5.5.1 停接单、排空、发布、重启及就绪核验流程执行；不新增在线发布互锁或版本化读取 | 已确认（2026-10-18） |

### 1.1 新旧切换的工作量与配置边界

按最终澄清，Web 入口无需配置新旧引擎开关，固定调用 db_deepagent。虽然增加一个 YAML 字段本身很小，但 Web 真正支持双引擎还需适配会话主键、响应结构、历史存储及按源执行；这不应包装成仅改一个开关即可完成。

旧父图已有 `scripts/nl2sql_father_cli.py`，新链已有 `python -m src.modules.db_deepagent.cli`，分别执行即可对比。若以后需要统一对比脚本，可在该脚本专用配置中设置 `comparison.engine: db_deepagent | legacy`，默认新链；这是可选的脚本便利项，不写入 Web 路由，也不要求两个引擎同时具备多源能力。本轮不新增该配置。

## 2. 代码核查结果

### 2.1 两条执行链当前分离

现有 Web 路径为：

```text
Streamlit → /api/v1/query → run_nl2sql_query → 旧 LangGraph 父图
                                               ├─ Fast Path
                                               └─ Complex Path
```

依据：[API 路由](../nl2sql/src/api/routers/query.py)、[父图](../nl2sql/src/modules/nl2sql_father/graph.py)。父图注册旧 planner、sql_gen、sql_gen_batch、sql_exec、summarizer 等节点；该调用链没有引用 db_deepagent。

v4 已存在独立的 [CLI 装配](../nl2sql/src/modules/db_deepagent/cli.py)、[Agent](../nl2sql/src/modules/db_deepagent/agent.py)、[Runner](../nl2sql/src/modules/db_deepagent/runner.py)、业务服务、结果存储和持久化代码。Runner 按 P0 准备、P1 invoke、P2 读取、P3 归档、P4 本地记录执行；这不是只有设计文件的占位模块。

doc 4 已交付源登记解析与解密、按源运行配置、Milvus 六入口过滤及独立 alias、Neo4j 实体键发布与查询、会话来源绑定和一次性迁移脚本。本文 §5 以这些现有能力为 Web 接入基线，不重复安排实现。代码落地、图库迁移、重新发布 rel 和真实双源验收分别记录；部署前仍须按 doc 4 完成相应维护与验收步骤。

但是，当前 [FastAPI 应用](../nl2sql/src/api/main.py)只挂载旧 query/history 路由，未提供 db_deepagent 的 Web API。实施时应将该 Web 入口调整为新链路由与生命周期，不能只加页面后继续调用旧 query；旧父图脚本保留。v4 设计文档中的阶段标签可能早于代码实现，不能以文档标题代替实际代码或验收证据。**本稿确认模块代码已存在，不确认其全部运行验收已通过。**

### 2.2 前端与通信现状

- [统一应用外壳](../frontend/src/App.vue)包含 AppSidebar 和 RouterView；[路由](../frontend/src/router/index.ts)当前只有 MetaWeave 页面。
- [侧栏](../frontend/src/components/AppSidebar.vue)有禁用的“新建查询”，可作为接入入口；现有品牌显示 MetaWeave，平台品牌调整属于单独的界面决策，不顺带全面改名。
- MetaWeave 客户端 [http.ts](../frontend/src/api/http.ts)使用 `VITE_API_BASE_URL` 直连后端，按 HTTP 状态及 MetaWeave 错误结构处理；不能原样用于 NL2SQL 的数字业务 code 封装。
- [Vite 配置](../frontend/vite.config.ts)没有代理；当前地址校验只接受完整 origin，不允许 `/api` 或尾斜杠。
- 当前未安装 Pinia、Markdown 渲染或表格组件库。先复用 Vue 与现有 CSS；确需新依赖时在实施中说明用途，不预设新增组件框架。

### 2.3 Streamlit 功能及保留范围

依据（2026-11-10 旧前端清理前）：`nl2sql/frontend/app.py`（Streamlit 页面，已删除）、`nl2sql/frontend/api_client.py`（Python API 客户端，已删除）。下表保留迁移期功能对照的历史语义，清理设计与实施记录见 [nl2sql/docs/3](../nl2sql/docs/3_NL2SQL旧前端清理设计.md)。另注：历史展示此后已随实现演进——turns 现返回问答文本及可回放的 SQL/结果引用 display，不再限于仅 question/answer，以当前代码为准。CSV 结果导出亦已实现（`exportResultCsv` 与 `/results/{result_ref}/export` 路由），下文"首版默认不新增……结果下载……"仅具历史范围含义。

| 当前功能 | 已有行为 | Vue 首版建议 |
| --- | --- | --- |
| 用户身份 | 可手工输入 user_id，默认 guest；切换后清空当前对话 | 固定 guest，移除用户切换交互 |
| 新建对话 | 清空消息与 thread_id，首次查询由旧后端生成标识 | 先选 source_id，由 Web 层创建稳定 session_id |
| 提问、续聊 | 同步 POST，传当前 thread_id | Vue 传 session_id 和 source_id，后端核对绑定，Runner 解析当前 checkpoint 线程 |
| 历史列表 | 按 user_id 获取，标题取首问并截断 | 展示 guest 会话及来源，保留空态、加载态、失败重试 |
| 恢复历史 | 仅恢复 question/answer | 只返回和展示问答文本，不返回 SQL、结果引用或表格，不提供历史详情展开入口 |
| 答案 | Markdown 展示 summary | 首版纯文本展示 answer，保留换行；不使用 v-html，不新增 Markdown 依赖 |
| 单条 SQL 与表格 | 顶层 sql + 首个执行结果 | 使用新链 evidence 与 result_ref，不复用旧响应结构 |
| 多子查询 | sub_query_id 关联 SQL 和执行结果 | 按 evidence_id 展示多条证据与关联结果，不伪造 sub_queries |
| 错误与等待 | spinner、异常消息、detail.error | 区分通信失败、业务失败、无数据、部分完成 |

首版默认不新增 SQL 手工编辑执行、结果下载、图表生成、对话删除/重命名、跨会话搜索或实时步骤。这些能力不是当前 Streamlit 的替换前提，如用户需要应明确增补范围。

## 3. 旧 API 核查记录：仅作差异参考

本节记录当前旧代码，帮助解释新旧差异；**不是 Vue 实施协议，也不作为双引擎接入分支**。新 Web 入口不挂载旧 query/history 路由，包括 `/api/v1/query`、旧 sessions/turns；旧源码暂留，父图对比直接执行脚本。不得仅移除旧 lifespan 预编译却留下可以懒编译旧父图的 HTTP 入口。

| 方法 | 路径 | 请求 | 响应内容 |
| --- | --- | --- | --- |
| GET | `/api/v1/health` | 无 | `{status: "ok"}`，不走业务封装 |
| POST | `/api/v1/query` | `query`，`user_id` 默认 guest，`thread_id` 可空 | `BaseResponse<QueryResponseData>` |
| GET | `/api/v1/sessions` | `user_id` 可选，默认 guest；`limit` 可选，1–50，省略则读配置，当前为 5 | `BaseResponse<SessionInfo[]>` |
| GET | `/api/v1/sessions/turns` | 必填 `thread_id`；`limit` 默认 50，1–200 | `BaseResponse<TurnInfo[]>` |

依据：[查询模型](../nl2sql/src/api/schemas/query.py)、[历史模型](../nl2sql/src/api/schemas/history.py)、[历史路由](../nl2sql/src/api/routers/history.py)、[封装模型](../nl2sql/src/api/schemas/common.py)。

- query 当前只有最大长度 1000 的约束，没有最小长度；Vue 应拒绝全空白。长度校验实现应与后端字符计数一致，不能将 JavaScript UTF-16 length 直接认定为相同语义。
- 成功与捕获异常均可能返回 HTTP 200，必须检查响应体 `code === 200`；FastAPI 参数校验错误仍可能是 HTTP 422。
- 外层 code 成功后仍要检查 `data.error` 及各项 `execution_results[].success`，不能把传输成功当成查询全部成功。
- 响应含 `summary`、`sql`、`sub_queries`、`execution_results`、`query_id`、`thread_id`、可空错误及 `metadata`；统计信息位于 metadata，没有独立的 stats 字段。单子查询时顶层 sql 取 validated_sql，即使执行失败也可能非空，不能据此判断成功；多子查询应按 sub_query_id 关联结果。
- 行数据为位置对应的 `columns: string[]` 与 `rows: unknown[][]`；`rows: []` 表示成功执行但零行，执行失败时 rows 为 null，须结合 success 判断。数值精度与日期格式应以实际 JSON 序列化为准，不擅自转为 JavaScript number 后重新计算。

### 3.1 旧历史的实际限制

[历史读取服务](../nl2sql/src/services/langgraph_persistence/chat_history_reader.py)只交付问题与答案文本；[父图历史写入](../nl2sql/src/modules/nl2sql_father/graph.py)只在有成功执行结果时追加问答，元数据不包含完整 SQL 和行数据。因此：

- 当前页面中有详情，不意味着刷新后能从历史接口恢复详情。
- 查询失败、仅澄清、没有成功 SQL 执行的轮次可能不会进入旧历史。
- 当前读取会截断问题/答案文本，获取最近轮次不等于无限历史或全文检索。
- 列表和轮次读取在超时或异常时可能直接返回空数组，旧前端无法区分“没有历史”和“读取失败”。新 Web API 不沿用该行为：读取失败返回明确错误，页面保留已有消息并允许重试。
- `thread_id` 同时承担旧 Web 对话标识和 checkpoint 标识，不能直接沿用为 v4 的永久会话主键。

## 4. db_deepagent Web 适配

### 4.1 会话和结果模型

v4 [Runner.run](../nl2sql/src/modules/db_deepagent/runner.py)接收 question、session_id、可选 thread_id/request_id、force_rotate 和 source_id。Runner 已核对请求来源、持久化会话来源与服务绑定来源；现有 Python 参数允许省略 request source_id，但本文 Web 创建和提问接口均要求显式传入。[DbAgentResult](../nl2sql/src/modules/db_deepagent/schemas.py)返回 session_id、thread_id、request_id、run_id、status、answer、findings、evidence、口径、限制、待澄清问题、errors、warnings、archived 和 stats。

需要区分：

| 标识 | v4 含义 | 前端使用方式 |
| --- | --- | --- |
| 登录 session | 平台身份与登录状态 | 交给未来身份模块，不能与问答 session_id 混用 |
| session_id | 一段持续业务对话，跨线程轮换保持不变 | 服务端生成 `s_` + UUID4 hex；完整匹配 `[A-Za-z0-9_-]{1,64}`，下同 |
| source_id | 平台登记的数据源短 ID（`db_src_N`，doc 5.1 §1）；会话固定绑定 | 创建和提问均携带，后端与持久化绑定核对；模型不可填写或修改 |
| thread_id | 当前 checkpoint 线程，可能轮换 | 返回后只作诊断；Web 首版不让用户手填或强制指定 |
| request_id | 一轮用户请求 | 服务端生成 `req_` + 完整 UUID4 hex，禁止客户端指定；同时作为诊断文件名和会话内文本日志键，不具备重试幂等语义 |
| creation_id | 一次 Web 会话创建操作 | 服务端生成 `create_` + 完整 UUID4 hex，完整匹配 `create_[0-9a-f]{32}`；仅用于创建保护及定位，不传给 Runner，不写入轮次日志，不作为幂等键 |
| run_id | 一次实际运行 | 诊断及结果归属 |
| result_ref | 服务端保存结果的引用 | 通过受控 API 读取，不能当作磁盘路径 |

v4 的 session_id 不是旧历史的 thread_id；没有 user_id 的 CLI 会话不能直接视为有用户归属的 Web 会话。

### 4.2 Web API 契约（全部尚未实现）

在现有 NL2SQL FastAPI 内交付 `/api/v2` 新链路由，Vue 仅调用这些路由；不挂载旧 query/history。以下路径、分页及错误封装作为本稿实施契约，不再留待开发者自行选择。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v2/health` | 新链 Web 存活检查，不能以旧父图 health 代替新链就绪验证 |
| GET | `/api/v2/sources/{source_id}/readiness` | 选源后、创建会话前读取；返回 §5.5 的配置兼容性、按源知识及依赖检查，不调用问答模型 |
| POST | `/api/v2/sessions` | 请求 `{source_id}`；验证登记及就绪，生成 session_id，保存 Runner 来源绑定和 Web guest 登记，返回 SessionInfo |
| GET | `/api/v2/sessions` | 仅列 guest 的 Web 会话，可按 source_id 过滤；limit 默认 30、最大 100，offset 默认 0 |
| GET | `/api/v2/sessions/{session_id}` | 返回 SessionInfo，直接打开 URL 时读取，不依赖当前列表页是否包含该会话 |
| GET | `/api/v2/sessions/{session_id}/turns` | limit 默认 50、最大 200，offset 默认 0；按时间正序返回问答，每项仅 question、answer |
| POST | `/api/v2/sessions/{session_id}/query` | 请求仅 `{question, source_id}`；服务端生成 request_id，核对来源及就绪后同步调用 Runner，返回 WebQueryResult |
| GET | `/api/v2/sessions/{session_id}/results/{result_ref}` | offset 默认 0，limit 默认 100、最大 500；读取已保存表格，历史页面不调用；旧引用边界见 Q2 |

SessionInfo 固定为 `{session_id, source_id, title, created_at, last_active_at, turn_count, state, continuable, invalid_reason, invalidated_at}`，时间为带时区 ISO 8601；created_at 为 Web 原始创建时间。创建时 title 为“新对话”、turn_count 为 0、last_active_at 等于 created_at，空会话也显示；创建 Web 登记时不写 metadata_reconciled_at；首次列表/详情读取按该字段缺失安排一次受限全量校正，校正执行时按保留日志判断：无文本则写空会话基线，已有文本则据日志重算缓存；成功后写入当前带时区时间。第一条已保存问答的问题先 `strip()`，再取前 40 个 Unicode 码点作为标题；若所得仍为空，则用“新对话”。此后不自动改名，历史中的 question 始终保留原文。turn_count 只计已持久化 Web 文本轮次，包括失败/澄清摘要，不计忙碌或参数拒绝。

**展示字段完整性先于 SessionInfo 构造**：created_at、last_active_at 必须是合法的带时区时间，且按时间值比较满足 last_active_at ≥ created_at；title 必须是非空白字符串，turn_count 必须是非负整数（不接受 bool）；与会话 ID、来源及状态字段一并校验。缺失/非法展示字段不能仅靠下述失效状态投影凑成完整 DTO。列表对这类可确认损坏的单条登记跳过并在后端日志/maintenance 报告记录 session_id 和问题字段，不使整页序列化失败；先筛出可构造 DTO 的记录再按约定排序/分页，has_more/next_offset 也只计有效条目。直接详情及新查询均返回 **409/session_metadata_invalid**，新查询在装配前拒绝；前端提示“会话展示信息不完整，可刷新；若无法恢复请新建会话”，不引导重新绑定或选择数据源。Store 读取故障仍返回 503/history_unavailable，不当作损坏条目静默跳过。仅展示字段损坏不删除历史，能独立核对 Web 归属时 turns/已知结果仍按原契约读取。

不伪造原始 created_at：不能用当前时间、Runner 每轮重写的 created_at 或 last_active_at 代替。title、turn_count、活动时间等可按 §4.4 从可信文本日志校正；原始 created_at 缺失/非法且无可信值时仅报告，本期不提供人工恢复入口或自动补值，该会话保持只读，续聊须新建会话。上述展示字段齐全后，才适用下面针对状态字段不完整的固定安全投影；该投影不豁免 title/created_at 等必返字段检查。

**历史读取的 API 与 UI 边界**：服务端先独立核对 Web 会话归属，确认只是展示字段损坏后才返回 session_metadata_invalid。直接访问已知会话 URL 时，Vue 收到该码仍按 §7.1 调用 turns，展示只读问答，不因 SessionInfo 构造失败一并阻断文本历史。若 created_at 无法恢复，列表继续跳过该条；本期该会话始终只读且须新建会话才能继续提问，已知 URL、API 及维护记录仍可用于访问/定位历史，不新增“损坏会话”DTO、列表或搜索入口。原始记录与文本不因列表跳过被删除。

**P1 读取实现约束**：turns/results 使用独立的 Web 归属读取校验，不能复用 SessionInfo 构造器、展示字段校验或新查询的“可续聊”门槛。GET turns 除通用 ID/分页参数校验外，只核对 Web 登记的 guest/owner/origin、路径 session_id 与登记 session_id，以及登记中 source_id 是合法短 ID（`db_src_N`，doc 5.1 §1），再读取本会话文本；不要求 title、created_at、last_active_at、turn_count 完整。GET results 同样先校验上述 Web 归属及登记中的固定 source_id，再核对 ResultStore 的 meta.session_id、引用归属、完整性和读取编码，不要求 SessionInfo 完整。两个历史读取入口都不调用 source_registry/db_source_info，也不要求登记源目前存在、启用、连接快照或知识版本仍相同；源删除/停用后，归属及保留文件仍有效的 turns/已知结果引用照常可读。可以复用核心归属检查，不能让其间接实例化包含展示字段的完整登记模型；字段损坏不成为这两个读取接口的 session_metadata_invalid。核心归属无法确认、跨会话引用、文件损坏和依赖故障仍按各自原有契约拒绝，不能以只读模式绕过。

state 为 `active | invalidated`；invalid_reason 为 `knowledge_changed | dependency_unavailable | source_changed | session_binding_missing | null`，invalidated_at 为带时区时间或 null。新建时 active，两个失效字段为 null。失效是持久化且不可自动逆转的状态：即使外部发布记录回退，也不恢复旧会话；列表和详情均返回这些字段。continuable 由服务端根据登记字段完整、**满足下述状态不变量**、state=active 且 §4.4 的 pending_run_id **显式存在并为 null** 得出，不另存第二个布尔真值；它不是锁空闲、源当前启用或 readiness 的保证，新提交仍须重新核验。active 但 pending 非空时 continuable=false、失效字段仍为 null；统一提示“上一操作尚未确认，请刷新查看”，涵盖创建和查询收尾，不声称已有查询正在执行，不新增 pending_kind。invalidated 时 continuable=false，显示原因并引导新建，历史读取不受影响。

**Web 登记状态不变量**：active 必须同时满足 invalid_reason=null、invalidated_at=null；invalidated 必须有上述枚举中的非空原因及合法的带时区 invalidated_at。pending_run_id 必须显式存在，为 null 或本稿规定格式的 creation_id/request_id；invalidated 允许保留非空 pending，不能为美化记录而清除收尾保护。字段缺失、类型/枚举/时间非法或状态组合矛盾时，统一按下述安全投影返回 invalidated/session_binding_missing、continuable=false、invalidated_at=null，并拒绝新查询。这个 null 仅用于表达无法信任原记录的读取投影，不是新写入失效登记可以省略时间的规则。

旧 Web 登记若缺少 state、invalid_reason、invalidated_at 或 pending_run_id 中任一字段，列表/详情统一安全投影为 `{state:"invalidated",continuable:false,invalid_reason:"session_binding_missing",invalidated_at:null}`，不默认 active、不伪造失效发生时间。仅在记录本身仍能核对 guest/owner/origin、session_id/source_id 的前提下列出，保留可读历史；一般 CLI/测试会话依旧不纳入。GET 不为补字段写 Store，也不借机重建通用来源绑定；新查询拒绝为 409/session_binding_missing。invalidated_at=null 在这里表示旧记录无可信时间，不与普通运行中失效必须记录首次时间的契约冲突。

列表、详情和新查询（包括取得会话锁后的再次检查）使用同一 Web 登记校验规则，先检查原始记录再计算 continuable；不能只由前端判断，也不能在模型默认值补齐后才检查缺字段。读取安全投影不自动修改 Store，元数据校正也不把矛盾状态修成 active。

本期有意区分“字段缺失”和“显式 null”：pending_run_id 是收尾安全保护，缺失不能证明上次操作已正常确认，不按可空字段的模型默认值自动补 null。Web 登记本次才引入，暂无需保留兼容的已投产旧格式；测试/预发中缺字段的记录保留历史并新建会话即可。本期不增加 schema/version 字段或自动升级机制；未来若需兼容存量格式，须按已知旧格式单独验证和迁移，不能把缺保护字段直接解释为 active。

创建会话不提供幂等键：Vue 禁止重复点击，**不自动重试 POST /sessions**；超时提示“创建结果未确认”，先刷新列表，用户再决定是否新建。首版接受用户手动重试可能产生另一个空会话，不增加客户端幂等记录或自动删除规则。

请求模型使用 extra="forbid"，多余的 request_id、user_id、连接字段等返回 422，不静默忽略；source_id 校验为合法短 ID（`db_src_N`）。问题原文长度须为 1–1000 个 Unicode 码点，去除首尾空白后不得为空；实际提交文本保留用于本轮和历史展示，不在模型输入中拼接未受控连接配置。

会话和轮次分页的 data 统一为 `{items, limit, offset, has_more, next_offset}`；limit 为正整数、offset 为非负整数，超上限返回 422，不静默截断。服务端多读取一条确定 has_more；有后页时 next_offset 为 offset + 本页条数，否则为 null。会话按 last_active_at 降序、session_id 升序打破同刻并列，轮次按锁内保存时间升序、request_id 升序排列；Store 默认顺序不能代替该排序。首版使用 offset，会话列表和 turns 均不承诺跨请求快照；新增、迟到收尾及 maintenance 删除可能改变分页位置。会话列表变化后从第一页刷新并按 session_id 合并；轮次“加载更多”按正序追加，本页发起新查询或发现历史变化时重新加载，不能按 question/answer 文本去重（相同问答可合法重复），不做后台全量预取。

**会话列表 offset 是过滤并排序后的有效 SessionInfo 条目偏移，不是原始 Store 键偏移**；其中包含可完整构造 DTO 的失效会话，不等同于只计 continuable=true。服务端须遍历所需 Store 分页、完成归属/source_id 过滤及展示字段检查和排序，再从有效集合取 offset 后最多 limit+1 条；原始记录扫描量可能超过 limit+1，不得直接把 API offset 传给 Store 后只过滤那一页。next_offset 仍为有效 offset 加本页有效条数，不加扫描/跳过的原始记录数；损坏记录修复后进入有效集合造成的位置变化，沿用上述非快照刷新规则。

列表扫描设置单请求上限（首版最多检查 10000 条原始记录、最长 5 秒）；Store 分页读取也须受剩余时间限制，不能只在外层计时后放任后台读取继续占用线程。到达任一上限而尚未完成过滤、排序及 has_more 判断时，整次返回 503/history_unavailable，不返回部分列表或错误的空页/next_offset；页面保留已显示列表并提示稍后重试。本期不为此新增索引或缓存服务；如果实际 Web 会话量触及上限，再调整分页实现。

GET turns 的 offset、排序和切页以本次完整校验通过的保留日志为准；扫描时一旦发现最小有效记录判定失败，立即整页返回 500/history_record_invalid。只有尚未发现无效记录、但达到资源上限且仍未完成扫描时，才返回 503/history_unavailable；错误选择不因扫描耗时变化而漂移。SessionInfo.turn_count 仍按 §4.4 的缓存/校正规则处理；记录损坏时保留已确认缓存并报告，不用本次扫描结果改写计数。单次 GET turns 的全量扫描最多读取 10000 条原始轮次、最长 5 秒；Store 分页读取受剩余时间限制。达到任一上限仍未完成校验/排序时整次返回 503/history_unavailable，不返回部分历史或错误分页信息；读取线程槽须等底层调用实际结束后才释放，不能只超时返回后让后台读取无限占用资源。本期用该上限保护异常长会话，需要更大历史范围时由后续分页实现另行设计。

结果分页 data 固定为 `{columns, rows, offset, limit, next_offset, has_more, saved_row_count, source_truncated, max_display_rows}`。`max_display_rows` 是前端表格展示上限（来自 `config.yaml` 的 `api.max_display_rows`，默认 200），不是单页数据上限；服务端 `limit` 仍为默认 100、最大 500。columns 为按列序排列的 `{name:string, pg_type:string}[]`，沿用现有 ColumnMeta；rows 为同序的 `CellV2[][]`，单元格契约见 §4.5。空结果 rows=[]，仍返回列定义；无后页 next_offset=null，has_more=false。**查询响应不内嵌 preview/rows/columns**：UI 收到 evidence 后，按 result_ref 调用结果 API 获取第一页及后页，不保留“可选第一页预览”分支。

**WebQueryResult 字段表（均必返，nullable 仅以下明确标注的字段）**：

| 字段 | 类型与语义 |
| --- | --- |
| session_id | string，会话标识 |
| thread_id | string 或 null，Runner 未确定线程时为空 |
| request_id / run_id | string，各自的权威请求及运行标识 |
| status | completed / partial / needs_clarification / no_data / failed / budget_exhausted / persistence_error |
| answer | string，可为空；历史写入时另按 §4.4 补安全摘要；一致性未确认终态必须改为下述安全摘要 |
| findings | `{text:string, evidence_ids:string[]}[]` |
| evidence | 当前 EvidenceItem 的结构化安全投影，保留 evidence_id、kind、tool_call_id、source、sql、sql_template、result_ref、input_result_refs、operation、row_count、source_truncated、warnings、reused_from、result_generated_at；字段类型/nullable 沿用 schemas.py 中的定义，**不输出 preview_truncated**，也不新增行/列字段 |
| metric_definitions / limitations / open_questions / warnings | string[]，无项时 [] |
| errors | 安全化的 RunFailureRecord[]：phase、kind、message、primary，结构化忙碌/故障类型按本节扩展，message 不含异常堆栈/凭据 |
| archived | boolean，仅诊断，不表示问答已保存 |
| stats | RunStats：elapsed_ms、model_calls、summary_model_calls、total_model_calls、tool_calls 为整数；input_tokens/output_tokens 为整数或 null；token_usage_complete 为 boolean，未知用量不填零 |
| source_id | 数据源短 ID 字符串（`db_src_N`），取已核对的会话绑定 |
| history_saved | boolean 或 null，按 §4.4 判定 |
| error_code | 本节闭集中的字符串或 null，按下述终态/拒绝映射 |

嵌套结构复用当前 [schemas.py](../nl2sql/src/modules/db_deepagent/schemas.py) 的 FindingItem、EvidenceItem、RunStats、RunFailureRecord 及 EvidenceOrigin 定义，并在 Web DTO 中显式声明上述投影；不能用任意 dict/默认序列化悄悄增减字段。operation 等内部分析描述也须安全化；数组空值用 []，不改为 null。P1 生成前端对应类型并以完整样例做契约测试；这不允许将整份 checkpoint/工具状态作为 evidence 透出。

**一致性未确认终态的强制投影**：执行中复核发现变化时返回 HTTP 200、status=failed、error_code=knowledge_changed；无法完成终态复核时返回 HTTP 200、status=persistence_error、error_code=dependency_unavailable。两者均由后端将 answer 改为“本轮知识一致性无法确认，请新建会话”等安全摘要，并固定 findings=[]、evidence=[]、metric_definitions=[]、limitations=[]、open_questions=[]；warnings/errors 仅保留安全诊断，不夹带原答案、SQL、结果引用或模型内容。ID、stats、archived 与实际 history_saved 保留。Web 文本日志也只写同一安全摘要，不能先保存原答案再由 Vue 隐藏；无须另增 result_usable。普通 P3 归档失败但知识复核通过的 persistence_error 仍可交付已验证结果，不能仅见该 status 就清空所有有效证据。

**来源契约沿用 doc 4 §5.1–§5.2 与 doc 5.1 §1 短 ID**：创建会话与每次提问均必填合法短 ID（`db_src_N`）source_id。服务端先读取会话绑定，再比对请求 source_id；一致时使用该绑定读取登记、构造服务并传给 `Runner.run(..., source_id=...)`，不以请求值覆盖既有绑定。绑定不一致、已有会话缺少绑定时，按下表返回稳定错误，不匹配中文消息。查询接口不隐式创建会话；不存在时拒绝执行，不能仅凭请求 source_id 将其视为首轮。source_id 由服务端装配注入检索和图服务，不加入模型工具参数。恢复会话使用单会话详情的绑定，不能因当前列表页未包含它就采用页面选源。

**Q2（已确认选择方案 1）：保留已获得旧 result_ref 的读取能力。** D3 约束历史 API/页面只返回和展示问答、不返回 result_ref、不提供 SQL/表格回放入口，不限制结果读取接口只能服务最近一轮。现有 ResultStore 按 session_id 校验归属，没有“当前轮”限制；已取得的旧引用在结果仍存在、完整性检查和归属校验通过时仍可读，刷新页面不会自动使引用失效。

沿用该能力，不新增“最近一次 run”授权状态：Web 校验 guest 的 Web 会话登记及其既有 source_id 绑定，再由结果服务核对 meta.session_id 与文件完整性；**不实时要求 db_source_info 中的源仍存在或启用，也不比较当前连接指纹**。源停用、删除或连接变化后，新查询受限，但既有会话文本及有效结果引用仍可读取。随机引用不是授权凭据；实际可用性取决于文件是否仍被保留，30 天不是强制 URL 到期时间（见 §4.5）。刷新不失效是 API 能力：历史 DTO 不返回 result_ref，刷新后的页面不会自动恢复 SQL/表格，只有事先持有引用的调用方能再次读取。以后增加历史入口可复用结果服务，不需要重新执行 SQL。

结果分页当前只是响应切页：每次 read_rows 都读取完整 rows.jsonl、计算 SHA256、解析全部行后再切片，后端成本随结果总大小增长；缩小 limit 不会消除整文件读取。普通业务查询的默认保存上限为 10000 行，由 SqlPipeline 的有效行数预算控制，可随配置或调用参数变化；ResultStore.save 本身不强制该上限，不能将其当作所有结果文件的硬限制，宽行也可能使成本较高。Web 应按需翻页，不预取全部页；API 仅依赖统一结果服务的分页、归属和完整性契约，与 [doc 3 §4.3](3_SQL查询结果文件存储问题与改造思路.md#43-尽量保留上层服务契约) 保持兼容，不绑定文件路径，也不在本次顺带实施底层存储重构。

**session_id/request_id 校验必须先于副作用**：两者均完整匹配 `[A-Za-z0-9_-]{1,64}`。Web 分别生成 `s_` 和 `req_` 加完整 UUID4 hex；请求体不接受 request_id，也不新增 client_request_id，页面使用本地请求序号协调迟到响应。API 之外，`build_db_agent`、`build_session_dir` 和 `Runner.run` 的入口也必须先调用同一校验；Runner 未传 request_id 时先生成，再校验。检查须早于创建目录、取锁、写本地记录、Store 或 checkpoint，非法值直接拒绝，不用非法 ID 写错误记录。当前底层只有无长度上限的 session 正则，且校验晚于部分副作用，属于待修复项。CLI 显式传入的 request_id 同样受限；同会话复用已有 request_id 在锁内拒绝为 request_id_conflict，不覆盖诊断/归档/文本日志，不将重复请求当作幂等重放。校验和路径边界测试须覆盖绝对路径、`../`、分隔符及超长值。旧链含冒号的 ID 不可沿用；内部 checkpoint 的 `dbda:<session_id>:<seq>` 不属于这两个字段。

忙碌等取锁失败路径也不能覆盖原 request_id 的本地诊断；P4 文件采用排他创建，已有文件则报告冲突/跳过该次落盘，不能用 write_text 覆盖。锁内检查已有诊断、归档和 Web 文本键后再开始实际运行；本期不支持客户端用同一 ID 自动重试。

**响应及错误映射采用统一封装** `{code, message, data, trace_id}`，code 为 HTTP 状态的数字值。正常业务响应为 HTTP 200/code=200；WebQueryResult 保留 Runner 的 status、answer、证据及诊断字段，增加已核对绑定的 source_id、history_saved 和可空 error_code，不能直接复用旧 QueryResponseData。Runner 已产出终态时，partial、failed、budget_exhausted、persistence_error 均返回 HTTP 200，按上述可信性投影保留可交付结果；失败查询的 error_code 为 query_failed，其他终态依下述闭集映射，前端继续按 status 展示。请求拒绝或无法形成运行结果时，使用下表 HTTP 状态，data 至少包含 `{error_code, request_id, run_id}`，尚未生成的 ID 为 null；若已有运行信息则另放 data.result，同样先作安全投影。前端只提交 source_id 和问题，不提交连接、密码、user_id。

上述 HTTP 200 指已开始执行的业务轮次；Runner 即使返回了对象，若原因是 P0 来源/绑定拒绝、忙碌等前置失败，仍按稳定错误类型映射为下表的非 200 响应，不把“有 DbAgentResult”本身当作执行已开始。completed 的 error_code 为 null，其余业务终态通常取 query_failed 或对应 status；执行期间的结果编码失败以 `status=partial/failed`、`error_code=result_encoding_error` 返回 HTTP 200，保留其他有效证据/答案，不因同名错误码改为传输层 500。发布版本意外变化的执行中失败同理，按 §5.5.1 返回 failed/knowledge_changed；HTTP 成功不等于业务全部成功。

trace_id 是 HTTP 入口服务端生成的独立 UUID hex，每个请求均为非空字符串，与业务 request_id 分开；422、未知路由、health 等尚无业务请求标识的响应也有 trace_id，记录日志时关联两者。GET health 固定返回 HTTP 200、`{code:200,message:"success",data:{status:"ok"},trace_id:"<hex>"}`，不连接数据库或执行 readiness。

统一封装覆盖 RequestValidationError、Starlette/FastAPI HTTPException（包括 405 和未知路由 404）、未捕获异常，以及本机 Origin/Host/媒体类型拒绝；不能只包装已定义业务路由。未知路径为 404/route_not_found，不支持方法为 405/method_not_allowed，保留 Allow、Retry-After 和允许来源所需的 CORS 头。未捕获异常为 500/internal_error，不回显原始异常；处理器自身不得因为 data 不可编码再返回默认 detail。反向代理自身的 HTML/断连不受应用封装保证，前端仍须处理非 JSON。

| HTTP / code | 稳定 error_code | 语义 |
| --- | --- | --- |
| 422 | invalid_request | 字段、ID、分页或问题非法，包括 FastAPI RequestValidationError；统一封装，不另用默认 detail，不回显含凭据的原始输入 |
| 404 | session_not_found / source_not_found | 对应 Web 会话或登记源不存在；CLI/测试会话不当作 Web 会话公开 |
| 404 | result_not_found | 结果不存在、已清理、跨会话或不属于可访问 Web 会话统一返回；不区分 403，不泄露引用存在性 |
| 404 / 405 | route_not_found / method_not_allowed | 未知路由 / 不支持的方法，仍统一封装；405 保留 Allow |
| 409 | session_busy / request_id_conflict | 会话锁明确占用 / 同会话运行标识冲突；不执行 Agent |
| 409 | session_metadata_invalid | Web 会话展示字段缺失/非法；详情与新查询均拒绝，提示刷新或新建会话，不当作来源绑定缺失。列表跳过该条并安排可行的受限校正 |
| 409 | source_inactive / source_changed / source_mismatch / session_binding_missing | 停用、连接/范围改变、请求来源不匹配或缺少可信绑定；保留输入，提示选择或新建会话 |
| 409 | knowledge_changed / session_invalidated | 执行前发现知识版本变化/无可信版本，或 Web 登记已失效/有上轮未确认收尾；拒绝恢复 checkpoint。已失效登记仍以 invalid_reason 说明最初原因，不因版本回退重新放行 |
| 409 | source_not_ready | 配置兼容但必需知识缺失或发布状态不匹配；附安全的 readiness 检查结果 |
| 422 | source_unsupported / source_config_invalid | 不支持的类型/键空间，或空 selected_schemas 等登记配置错误 |
| 403 / 400 / 415 | origin_not_allowed / host_not_allowed / unsupported_media_type | 来源或 Host 不在白名单 / 写接口不是 JSON；在执行业务前拒绝 |
| 429 | query_capacity_exceeded / rate_limited | 全局执行名额或本地提交频率上限；返回 Retry-After，不自动重发 |
| 503 | lock_backend_error / source_registry_unavailable / dependency_unavailable / history_unavailable | 锁服务、登记库、依赖或历史读取不可用；不伪装成忙碌、空历史或源无知识 |
| 500 | history_record_invalid | Web 文本轮次记录损坏，无法安全排序/构造历史；整次 turns 请求失败并报告，不跳过损坏记录或伪装空历史 |
| 500 | result_integrity_error / result_encoding_error / internal_error | 结果读取 API 的损坏/无法安全编码，或未形成可交付业务终态的内部故障；执行期编码失败按上文返回 HTTP 200。若已有安全可交付运行结果则保留在 data.result，不强行为 GET 构造 Runner 结果 |

例如锁冲突返回 HTTP 409、`{code:409,message:"会话忙碌",data:{error_code:"session_busy",request_id:"req_<hex>",run_id:null},trace_id:"..."}`；已产出的部分结果返回 HTTP 200、`{code:200,message:"success",data:{status:"partial",error_code:"partial",history_saved:true,...},trace_id:"..."}`。result_expired 不单列：文件不存在时通常无法区分到期清理与从未存在，统一 result_not_found。

**错误字符串为闭集，不能由异常类名/中文消息临时生成**：非 200 外层 data.error_code 的全集即上表；HTTP 200 的 WebQueryResult.error_code 仅为 `null | partial | needs_clarification | no_data | query_failed | budget_exhausted | persistence_error | result_encoding_error | knowledge_changed | dependency_unavailable`。普通 completed 为 null，其他普通终态与同名码对应（failed 用 query_failed）；执行期编码失败覆盖为 result_encoding_error，终态一致性异常优先覆盖为 knowledge_changed/dependency_unavailable。前置拒绝若附 data.result，其 error_code 使用同一拒绝码；故 WebQueryResult 字段类型为上述两集合的并集加 null，不把所有 409 都压成 query_failed。

RunFailureRecord.kind 保留现有 `persistence | runtime | tool_set_mismatch | budget_exhausted | no_final_answer`，仅新增 `session_busy | lock_backend_error | knowledge_changed | dependency_unavailable | result_encoding_error`，phase 沿用现有枚举；Web 锁内终态复核归 P3。新增 kind 分别映射同名 error_code，runtime/no_final_answer/tool_set_mismatch 通常映射 query_failed、persistence 映射 persistence_error；最终码仍由明确的前置拒绝类型或终态原因选择，不能从错误数组最后一项猜测。其余参数/来源拒绝使用带稳定 code 的 Web 异常，不为每个 HTTP 码新增 Runner kind。primary 按已选终态原因标记；Web DTO 和 TypeScript 同步生成闭集，新增值须同步修改契约与测试。

**忙碌必须有机器可读信号，不能匹配中文 message。** 当前 Runner 只返回 `status=failed`、`errors.kind=runtime` 与中文说明；最小改造新增 `kind=session_busy`，同步调整终态判定及主错误选取，保持 Runner `status=failed`。Web 将其作为请求未执行的特例，按上表返回 409/data.error_code=session_busy，已有 Runner 结果放入 data.result，保留定位 ID，不另增顶层 busy 状态。

锁获取接口须区分“明确被占用”和“获取过程异常”：只有 PostgreSQL 明确返回未取得锁才标为 session_busy。当前 `SessionAdvisoryLock.try_acquire` 和 Runner 都会将连接/查询异常归为未取得锁，实现时须一并修正，使此类异常可被 Runner 识别并映射为持久化或服务不可用错误（HTTP 503），不能误报忙碌。忙碌请求不调用 Agent、不推进 checkpoint，也不改写正在运行会话的线程指针或问答记录；允许保留独立请求诊断信息。

同批修复锁连接的异常回收：当前连接建立后、赋给 self._conn 前若 execute 抛错，异常路径只检查 self._conn，会遗漏局部连接。须保证未取得锁或查询异常时关闭已创建的连接，取得锁后才移交生命周期管理。现有 hashtext 锁键只有 32 位，理论上不同 session_id 可碰撞并误报占用；这不允许跨会话访问，但可能影响可用性。本次不为此新增锁服务，若改用 64 位键，获取与释放须统一算法，并在没有旧在途持锁请求时切换。

旧接口 query 的 1000 字符上限不自动成为 v4 Runner 限制；本稿 Web 采用上述 1–1000 个 Unicode 码点及空白校验，前后端一致实现。

### 4.3 不能只包一层 CLI

- Web 层直接调用公共服务与 Runner，不通过 subprocess 执行 CLI，不解析终端输出。
- 新 Web 应用不导入/挂载旧 query/history，不初始化旧父图或其历史读写线程池；移除对应旧 lifespan 逻辑，新 lifespan 只管理新链资源。旧模块不得因路由导入间接启动；若发现过渡初始化，关闭时仍须释放，不能只删 shutdown。`src.api.main:app` 入口可以保留，启动指南同步更新；旧脚本独立管理自己的生命周期。
- CLI 的 `_Session` 展示了服务、平台池、持久化、Agent 和 Runner 的装配及关闭，但属于 CLI 内部代码；实施应抽出可复用运行装配，避免 API 依赖私有 CLI 类。
- [Agent 后端](../nl2sql/src/modules/db_deepagent/agent.py)按 session_id 绑定文件目录。不能把会话 A 的 handle/runner 当成全局单例给所有会话使用。
- 按 doc 4 §4.7，Web 首版每轮提问独立构造 `DbAgentServices`、本会话的 Agent handle 和 Runner；读取并核对会话来源后装配，执行及归档结束后在 finally 释放本轮资源，不做按源服务缓存。预算和可变上下文不跨请求共享；连续性由 PostgreSQL Store/checkpoint 保持，memory 模式不能替代 Web 历史验收。会话列表、文本历史及结果读取只构造所需的读取依赖，不装配整套 Agent、Embedding 和知识检索服务。
- Runner 有 PostgreSQL 会话 advisory lock，按 §4.2 增加结构化 session_busy 类型及 Web 映射，同时区分锁竞争与锁后端异常。不得跨会话共享可变运行上下文。
- query 路由采用 async 协调，阻塞的装配、Runner 和终态保存放入专用有界 ThreadPoolExecutor；首版本机单进程、单 worker，`NL2SQL_MAX_CONCURRENT_RUNS` 默认 2、正整数，执行线程数与该值一致。提交前非等待式取得同等数量的全局名额，满额立即返回 429/query_capacity_exceeded，不向 executor 积压无限队列、不占用 FastAPI 通用线程池等待名额。名额涵盖装配至清理全程，不在浏览器断开时提前归还；会话 advisory lock 继续控制同会话冲突。
- health、列表、历史、结果和 readiness 路由也采用 async 协调；其中 psycopg/Store、文件解析和知识库 SDK 等阻塞读取须放到独立的受限读取线程中，不能直接堵塞事件循环，也不共用长查询 executor。list/detail 触发的元数据校正使用这个独立的有界读取执行器，以 session_id 合并重复任务并限制排队；HTTP 请求不等待全量校正完成。health 只做轻量存活响应。首版不做通用任务队列或跨进程调度，增加 worker 前必须重新核定总并发预算。
- 构造部分失败须回收资源；HTTP 超时/断开后，后台仍执行则继续持有名额和依赖，直到 Runner、文本日志和清理真正结束才释放。服务关闭先停止接单，再等待或明确处理在途运行，不能提前关闭其连接和驱动。全局一致性检查的复用按 §5.5，不因每轮新建服务而每题再付探针模型调用成本。

本稿 D4 采用首版同步返回，资源生命周期覆盖后台执行与文本收尾全过程。以后若增加流式，另行设计事件 DTO/断连恢复，不在本期预留第二套响应格式；doc 4 的流式清理约束不表示本期必须开发流式。

### 4.4 身份归属与历史补齐

[QaArchive.session_record](../nl2sql/src/modules/db_deepagent/persistence/archive.py)已记录 source_id、活动线程、时间和携带证据；Runner 在 `upsert_session` 整体覆盖时保留来源绑定。Web 创建会话须使用这一契约先持久化 source_id，不能等首次查询才绑定，也不能只在另一个 Web 命名空间存来源而让 Runner 读取的会话记录缺少绑定。

当前 `_write_thread_tracks` 每轮以当前时间重写 created_at，并固定写 active=False；因此现有 created_at 不能作为原始创建时间，active 也不能用于判断是否正在执行。**本稿采用独立 Store 命名空间**，不在 Runner 整体覆盖的记录里夹带 Web 元数据，不新建 SQL 会话表：

- Web 登记：namespace `("db_deepagent", "web_sessions", "guest")`，key 为服务端 session_id。value 保存 owner="guest"、origin="web"、session_id/source_id、原始 created_at、title、登记字段指纹及 §5.5.1 的 knowledge_snapshot；**持久化 state、invalid_reason、invalidated_at 与 pending_run_id（string 或 null）**。创建写入时前三项为 active/null/null，pending_run_id 为 creation_id；成功完成创建后才清为 null。运行时该字段放 request_id，两种前缀可区分；另保存可修复的 turn_count/last_active_at、metadata_reconciled_at 缓存及可空 metadata_reconcile_retry_after 失败退避时间。Web 列表仅枚举该命名空间并核对 owner/origin，不从通用 sessions 列表推断；CLI/测试会话不自动出现在 Web 中。
- 创建严格遵循下述固定写序；不覆盖碰撞记录。Web 已存在但通用绑定缺失/不一致时仍能读取已保存文本，新查询按 session_binding_missing/source_mismatch 拒绝，不自动重建绑定。
- 文本日志：namespace `("db_deepagent", "web_turns", session_id)`，key 为权威 request_id。内部 value 保存原文 question、非空白 answer、与 key 一致的 request_id、run_id、终态及带时区的 saved_at；历史 DTO 严格投影 question/answer。title/last_active_at 的更新发生于终态文本确认后；文本日志是 turn_count 的真值来源，不将非原子缓存当作保存凭据。分页枚举须遍历该会话全部保留记录后按 §4.2 校验、排序，不能仅排序默认第一页。

**web_turn 共用的历史有效性谓词**仅检查四项：question 与 answer 都是 `strip()` 后非空的字符串；request_id 是非空字符串，满足 §4.1 的 ID 契约并与 Store key 完全一致；saved_at 是合法的带时区时间字符串。四项缺失、类型错误或取值非法时，该记录无效。GET turns、turn_count 全量校正、maintenance retention 判断及 history_saved 的本轮记录确认都必须使用同一谓词。run_id 与终态是供排查使用的审计字段，不参与历史问答投影或有效性判定；它们缺失、类型错误或无法识别，不单独阻断 GET turns、计数或文本保留清理。写入新记录仍应按当前格式提供这两个字段。

写入、历史读取和 maintenance 共用上述谓词，不以缺省值补齐损坏字段。发现无效记录时 GET turns 返回 500/history_record_invalid、记录 session_id/key 与安全的错误类别，不回显问题/答案原文。损坏的 saved_at 不能作为“已过期”的依据；maintenance 在该会话锁内报告并跳过该会话本轮 web_turns 删除和元数据校正，不按部分有效日志重算 turn_count、title 或 last_active_at，也不因该问题删除其他历史/结果。维护报告将这类会话标为“需人工核验/处理”；本期不自动修复，也不提供自动解禁或坏记录隔离入口。

创建、运行收尾及元数据更新均须保持 §4.2 的状态不变量；失效转换在锁内用同一次 Web 登记写入同时保存 state、invalid_reason、invalidated_at，并保留 pending 等其余字段，不能分三次写出互相矛盾的中间状态。写入结果不明仍按现有回读/拒绝规则处理，不假设所有命名空间已原子提交。

**创建写序唯一采用“通用绑定在先，Web 登记在后”**：完成登记/知识快照检查后，生成 session_id 和 creation_id；取得同一会话 advisory lock 并确认无碰撞/遗留 checkpoint → 确认写入通用 sessions 来源绑定 → 写入 pending_run_id=creation_id 的 Web 登记 → 按 §5.5.2 写后复核 → 确认清除创建 pending → HTTP 200 返回 SessionInfo。创建全程持锁，不允许其他请求在写后复核前将创建 pending 判为遗留。失败按下表处理，不假装两次 Store 写入具有原子性：

| 失败位置 | 响应与残留 |
| --- | --- |
| 取得创建会话锁时，后端连接/查询异常 | 503/lock_backend_error；不进入任何通用/Web 登记写入，不生成 checkpoint/问答记录。错误 data 带已分配的 session_id/creation_id，request_id/run_id 为 null；回收未移交的锁连接 |
| 取得创建会话锁时，明确被占用 | 409/session_busy；本次不写通用/Web 登记，不调用 Runner；保留定位 ID，客户端不自动重试。已有持锁会话/记录保持不动，不能把“本次未写入”说成该 ID 下原本没有记录 |
| 通用绑定写入失败/结果不明 | 503/dependency_unavailable，停止后续写入，不产生 Web 登记；通用绑定可能已写入，作为不可见孤立残留留档 |
| Web 登记写入失败/结果不明 | 503/dependency_unavailable；记录可能不存在，也可能已带创建 pending 落下，不声称“一定保留 pending”。存在则按正常列表规则列出、continuable=false，不存在则只有通用残留；不自动重试整个创建或补写可用登记 |
| 写后复核发现来源/知识变化 | 409/source_changed 或 knowledge_changed，Web 登记尽力标为相应 invalidated；不能确认失效写入时仍不得主动清除 pending，不返回成功 |
| 写后复核不可用，或清除 pending 失败/结果不明 | 503/dependency_unavailable；如仍可回读，在同一锁内核对两条来源绑定、原快照和 active/null pending 状态，并再次轻量复核，只有全部确认通过才可改为 HTTP 200。否则报告结果未确认；若清除实际已提交，列表可能出现可用空会话，不能把“返回 503”承诺成“会话必定不存在/不可用” |

已分配 ID 的创建失败响应在标准错误 data 中增加 `session_id`、`creation_id`，业务 request_id/run_id 仍为 null；失败发生在分配前时这两个字段也为 null。不泄露连接或内部 Store 内容，客户端不据 ID 自动重试/恢复创建。列表/详情以实际保存记录为准；读取故障返回错误，不伪装为空。创建 pending 遗留在下一次取得锁的新查询中按现有收尾保护拒绝，不自动转成成功会话。maintenance 须识别并报告创建 pending，通用孤立绑定仅按下述条件报告为“疑似 Web 创建残留”；本期沿用不自动删除空会话的规则，**不新增自动删除或解禁创建残留的机制**，人工后续处理须先核验无执行/历史数据。

**疑似创建残留的报告边界**：通用 session_record 没有 origin/creation_id，不能把“无 Web 登记”等同于“Web 创建失败”。maintenance 在现有会话锁内、该会话清理动作之前核对：session_id 完整匹配 `s_[0-9a-f]{32}`，存在通用 sessions 记录但没有 Web 登记，且无 checkpoint、threads 轨迹、web_turns、qa/run_errors、携带证据、结果或本地运行诊断等实际执行/历史痕迹，才列为“疑似 Web 创建残留”。需枚举全部相关分页；纯粹的预分配 thread_id 指针不算已有 checkpoint。读取失败/范围无法核实时报告核验未完成，不把它视为“没有数据”，也不在本轮先删除历史后再据空记录判为创建残留。短 ID 的普通 CLI 会话及有执行痕迹的记录不进入此分类；CLI/测试也能显式使用长 ID，因此格式只用于缩小候选，不能证明 Web 来源。两种 cleanup 模式均只报告该分类，不自动删除绑定、补 Web 登记或解禁，不改通用 CLI 会话契约。

列表/详情使用 Web 登记中的元数据缓存，不为每个条目扫描全部 web_turns。锁内文本确认后只增量更新 turn_count、title、last_active_at，不更新 metadata_reconciled_at；该时间只由全量校正成功时写入当前服务器的带时区时间。失败不否认日志已保存，也不把计数缓存当 history_saved 判据。沿用现有 maintenance 做校正，并在读取发现缺字段或 metadata_reconciled_at 超过 300 秒时，向 §4.3 的独立有界读取执行器安排按会话锁校正（正在运行则跳过）；同一会话合并重复任务且队列有界，HTTP 列表/详情请求不等待全量扫描。metadata_reconciled_at 使用独立于 retry_after 的时间解析规则：缺失、非时间字符串、无时区时间或未来时间偏差严格大于 60 秒的值均视为缓存未命中/已过期并安排校正，不使 GET sessions/detail 返回 session_metadata_invalid 或 500。最多 60 秒的未来偏差仅作时钟偏差容忍，校正新鲜度仍按 300 秒计算；校正成功写入当前服务器的带时区时间。metadata_reconcile_retry_after 缺失、非法或已到期时，list/detail 才触发按需校正；非法值按“无退避限制”处理并记录安全原因，失败后由校正尝试写回合法值。合法退避时间范围为当前时刻至 1 小时后；更远的未来时间按非法值处理，不无限阻止校正或 retention。若任务因会话锁明确忙碌而跳过，不设退避，后续请求可在当前查询结束后再次安排。若队列已满、执行器拒绝任务或锁后端不可用，则不从请求线程另行抢锁写标记，而在本进程抑制该 session_id 的请求触发校正，避免重复入队/扫描；抑制项记录加入时的单调时钟值及当时观察到的两个持久化时间值。每次 list/detail 重新读取登记时，分别使用 metadata_reconciled_at 与 retry_after 各自的时间解析规则比较持久化值；任一值变化即解除抑制并按新值重新判断，无效值按缺失比较。若均未变化，抑制最多持续 300 秒，到期自动解除；300 秒以进程单调时钟计算，不受系统墙钟回拨影响。该进程内机制不依赖独立 maintenance 进程清除内存状态。按需校正与 GET turns、maintenance 共用同一 web_turn 扫描 helper 和 10000 条/5 秒上限；达到上限、发现坏记录或读取失败时不改缓存及成功时间 metadata_reconciled_at，报告安全失败原因。Web 登记增加可空 metadata_reconcile_retry_after（带时区时间）：校正失败后在持有会话锁时设为当前时间后 300 秒，成功校正后更新 metadata_reconciled_at 并清除此标记；maintenance 只在该标记缺失、非法或已到期时重试，失败时同样重置为失败时刻后固定 300 秒，不做倍增或累计延长。标记未来有效时 maintenance 报告退避中并跳过该会话本轮清理/校正，不提前改写或缩短时间。该标记只用于服务端调度，不进入 SessionInfo、不影响 state/continuable，也不是旧 Web 登记完整性条件。若退避标记也无法写入，本进程对该 session_id 使用上述有限期抑制并记录字段快照；maintenance 改动持久化时间后，或 300 秒到期后，Web 可自行解除抑制。**列表本次跳过损坏条目也须先安排可行的受限校正，不能只校正已经进入返回页的会话**；读取到的归属及原始 created_at 可核对时才安排，校正成功且展示字段通过检查后，后续请求可重新列出。原始 created_at 缺失/非法只报告，不自动生成，不安排无法完成的修复。首版允许更新失败至校正之间的计数/排序短时陈旧，不宣称列表强一致；不新建计数服务。

校正固定使用以下回退，以合法原始 created_at 为前提，遍历并校验全部保留 Web 文本日志后进行一次锁内元数据更新，不在 GET 内伪造临时值：turn_count 为符合 §4.4 共用历史有效性谓词的保留日志条数，**无保留文本时为 0**；已有合法标题保留，缺失/非法标题按 §4.2 从第一条保留问题 `strip()` 后取前 40 个 Unicode 码点，无文本时为“新对话”。last_active_at 取合法既有活动时间、保留日志中的活动时间及 created_at 的最大值；**既有活动时间缺失/非法且无保留文本时回退到 created_at**，早于 created_at 的既有值无效。已有合法活动时间不因文本全部过期而倒退，不能将“无日志”一律解释为重置活动时间。读取日志不完整/失败或任一记录不满足该谓词时，不按零条或部分条目覆盖缓存，报告失败并保留原记录及原 metadata_reconciled_at；若已安排按需校正，则按前述规则设置重试时间。正常空会话校正后不再因同一缺字段反复触发。校正完成前展示字段仍不满足 §4.2 时，列表跳过，详情/新查询为 409/session_metadata_invalid；不改写原始 created_at，不恢复已失效状态或清除 pending。

**不可续聊标记必须经得起 Store 暂时不可用**：在同一会话锁内、检查 active 且复核通过后，先确认写入 pending_run_id=本轮 request_id，再恢复 checkpoint/执行；写入不能确认则不执行。收尾复核通过后才在锁内清除此标记；复核变化或失败则置 state=invalidated，写明首次 invalid_reason/invalidated_at，禁止自动恢复。若失效状态写入失败，保留预先落下的 pending 标记作为拒绝续聊的依据；不得在 finally 无条件清除。下一请求取得锁后发现遗留 pending（此时不可能仍由另一请求持有同一锁），保守置 invalidated/dependency_unavailable 并返回 409/session_invalidated，不自动重放或认定上一轮成功；Store 不可读时返回 503，绝不恢复 checkpoint。进程重启、外部版本回退也不能绕过此检查。这只是登记中的一条收尾保护字段，不实现任务恢复平台。列表/详情读取到 pending 时返回 continuable=false，历史照常可读；缺失失效/保护字段的旧 Web 登记按 §4.2 固定值投影，新查询为 session_binding_missing，不因 GET 自动补成 active。元数据更新和 maintenance 校正须锁内读改写并保留 state/invalid_reason/invalidated_at/pending_run_id，以及本次未修改的 metadata_reconciled_at 和 metadata_reconcile_retry_after。创建 pending 清除、文本收尾和普通状态写入不得改写 metadata_reconciled_at 或清空退避字段；只有全量校正成功才更新 metadata_reconciled_at，且仅该成功路径可清除 retry_after；校正失败/超限按前述规则设置 retry_after。不能用缓存或旧快照覆盖失效登记。

原始创建时间由 Web 登记读取，多轮及线程轮换不重置。空会话保留并显示，忙碌仍以锁契约为准；不将 active=False 用作可提交保证。新文本日志沿用 archive_retention_days（2026-10-19 起 365 天，原 180 天；下限口径，以配置为准），在现有 maintenance 流程中按会话锁清理，不能依赖 QA 归档清理自动覆盖新命名空间；清理后 turn_count 表示保留轮数，页面注明保留范围。本期不增加自动删除空会话或新的保留期配置。

**P1 必须扩展 maintenance 的发现、清理及报告，而非仅约定入口**：会话发现取现有 sessions/结果/qa/run_errors/checkpoint 与 web_sessions 键、web_turns 命名空间中 session_id 的并集，枚举全部所需 Store 页。仅有 web_turns、没有通用 sessions、QA 或 Web 登记的残留也纳入；不得为了清理而自动补来源绑定。沿用同一会话 advisory lock，取不到则跳过该会话全部删除/校正；锁内重查并用 §4.4 共用历史有效性谓词校验该会话全部 web_turns，均有效时才按保存时间删除超过 archive_retention_days 的文本并校正存在的 web_sessions 元数据。该会话扫描最多读取 10000 条原始轮次、最长 5 秒；每次 Store 读取受剩余预算限制，不留脱离维护任务的后台读取。达到任一上限仍未完成、遇到单条坏记录或读取失败，都属于本轮校正失败：不删除文本、不校正缓存、不更新 metadata_reconciled_at 为成功时间；仍持有会话锁且登记可写时，将 metadata_reconcile_retry_after 设为失败时刻后 300 秒。退避标记写入失败时在 maintenance 报告中明确记录，当前执行只对该会话尝试一次并释放锁；独立 maintenance 进程不能设置 Web 进程内存状态，Web 进程仍依 §4.4 的持久字段变化检查和 300 秒单调时钟抑制恢复，下一次 maintenance 只按自身调度重试，不在同次执行内循环。超限/损坏/读取失败均报告“需人工核验/处理”，不得用坏 saved_at 猜测过期。若 metadata_reconcile_retry_after 是合法未来时间且不超过当前时刻后 1 小时，maintenance 报告退避中并跳过该会话本轮清理/校正；非法、缺失、已到期或超出一小时的时间按无退避限制处理。校正成功后清除退避标记；失败时不更新 metadata_reconciled_at 为成功时间，将 retry_after 固定设置为失败时刻后 300 秒，不做倍增或累计延长。正常 Web 登记不因文本过期自动删除，空会话政策不变；dry-run 只报告候选及预计缓存修正、不写入，apply 报告实际删除/失败/跳过。读取或归属不可判定时保留并报告，不作猜测删除，现有结果引用保护不变。

maintenance 报告单独汇总“展示字段无法自动恢复、已从列表隐藏”的会话，至少记录 session_id、问题字段及安全原因，便于通过已知 ID 定位；原始 created_at 缺失/非法归入此类。可修复缓存问题及读取失败分别报告，不把依赖故障定性为不可恢复。dry-run/apply 均保留这个报告分类，不因分类删除登记/历史、解禁会话或改 API/SessionInfo DTO。

已有 sessions 记录缺少 source_id 时，Runner 已拒绝恢复执行；但记录缺失时，当前实现会按首轮绑定来源，仍可能从默认或指定线程恢复已有 checkpoint。须在 Runner 的 P0 补齐：sessions 记录不存在而将要恢复的 checkpoint 已有状态时，拒绝续聊并提示新建会话，不推断来源、不复用旧上下文。Web 查询入口同时要求会话已创建且绑定完整；创建接口不得覆盖碰撞的已有会话或将遗留 checkpoint 纳入新会话。切换来源创建新会话，原会话绑定保持不变。

所有会话、轮次和结果读取先核对 Web 会话登记、固定 guest 及既有来源绑定，不实时依赖源登记仍存在；界面历史不读取 checkpoint。新查询另须核对通用 sessions 与当前源登记。所有 guest 共享身份，不承诺浏览器间私有历史；结果必须匹配指定 session_id，guest 不允许跨源复用结果。

v4 的 QA 归档有写入条件，不能充当完整 Web 文本日志；本稿明确采用上述 web_turns，不在读取时临时拼接 QA/run_errors/checkpoint。已实际进入运行并形成终态的每轮均保存文本：answer 是非空白字符串时使用安全化后的答案；needs_clarification 且无可用 answer 时将非空白的 open_questions 转为可读的澄清文本；其余无可用答案轮次保存“本轮未生成答案（状态：failed）”等对应终态的安全摘要。最终写入前再次确认摘要非空白，不拼接 SQL、工具参数或异常原文。DTO 不新增 status，仍只返回 question/answer。忙碌、名额耗尽、参数/来源前置拒绝等未执行请求不生成问答轮次。

文本保存由公共装配接入 Runner 的**锁内终态收尾钩子**，在 P3 归档尝试后、释放会话锁前执行一次；不等 HTTP 响应发送后才写，也不在 Runner 已释放锁后无保护地更新列表元数据。回调失败不能抹去既有运行结果或伪称写入已回滚：按 request_id 回读确认，得到下述 history_saved，安全记录失败；不新增整轮跨 Store/checkpoint 的分布式事务。请求断连但运行完成时仍执行保存，进程崩溃尚未收尾的请求不承诺一定留下历史，也不自动重放查询。

**archived 不是问答可恢复标志。** `_archive_run` 先写 qa/run_errors，再写 sessions/threads 轨迹，独立写入不具有整轮原子性：QA 已写成功而轨迹失败时 archived 仍可能为 false；仅写 run_errors 的轮次也可能为 true。因此 true/false 均不能推出历史问答是否存在，也不能作为前端“已保存/未保存”的判据。

Web 当前查询 DTO 新增 `history_saved: true | false | null`，只表达收尾时该轮 Web 文本记录是否已确认保存并能按本轮 key 回读：日志写入确认成功且本轮记录符合 §4.4 共用历史有效性谓词，或回读找到并校验对应轮次，为 true；明确未写入且回读确认没有该轮，为 false；写入结果及回读均无法确认时为 null，不把异常自动判为未保存。该字段不从 archived 推导，保留 archived 仅供诊断，不另增 archive_kind。元数据更新失败但本轮日志可读仍为 true；true 不保证其他保留轮次均完整，若其他记录损坏，GET turns 仍按 history_record_invalid 失败。false 提示“本轮问答未保存”，null 提示“本轮问答保存状态未确认”；true 也不保证永久保存，超过文本保留期后可被 maintenance 清理，页面提示当前保留范围。前置拒绝请求不生成问答或保存告警，data.result 如存在其 history_saved 为 null。

终态复核失败也必须尝试保存 §4.2 的安全摘要；Store 故障不改写上述三值语义。明确未发出日志写入且回读确认不存在才为 false；写入超时/异常且回读也失败为 null；回读确认该轮安全摘要存在为 true。失效登记写入、文本日志写入及 history_saved 各自判定，任一写入成功不代表另一项成功；Store 全面不可用通常只能报告 null，并保留 pending 收尾保护。不得为了返回 true/false 将待核实原答案写入日志。

按 D3，列表展示会话信息，进入会话后只展示问答文本。**历史轮次 DTO 仅包含 `question` 与 `answer`，不返回 `sql`、`sql_template`、`evidence`、`result_ref`、列信息或行数据，也不返回 `details_available` 等引导历史详情回放的字段。** 分页游标等属于响应外层，会话标识及来源属于会话信息，不混入每轮的问答内容。

历史读取只从本次 Web 文本日志提取 question/answer，不直接序列化 DbAgentResult、QA evidence 或 checkpoint；既有 CLI/旧历史不自动导入。答案按纯文本展示，不额外拼接 SQL/结果证据；历史消息没有 SQL 或表格展开按钮，不调用 ResultStore 结果读取接口。

当前页面新发起查询仍按当前查询响应展示 SQL 与表格；刷新或重新打开会话后，已保存轮次通过历史接口恢复为问题和答案。继续提问时，新一轮可再次展示其 SQL 与表格；历史可恢复提示仅按上述 history_saved 契约，不承诺未确认轮次刷新可恢复。历史文本展示不依赖查询结果文件仍然存在，history_saved 也不保证 checkpoint 续聊或结果文件可用。

### 4.4.1 checkpoint 与界面历史的区别

用户理解的多轮上下文方向正确：db_deepagent 通过 checkpoint 保存 Agent 状态，包含消息和 SQL 校验/执行/证据等账本；继续同一会话时 Runner 解析当前 thread_id 并读取状态，达到轮换条件时按既有机制携带证据。Vue 不需要把上一轮 SQL、完整消息或账本重新塞入下一次 query。

历史回放和多轮上下文是两个职责：checkpoint 是内部执行状态，SQL、执行账本及 result_ref 可以在其中；行数据另存于 ResultStore。**这些内部数据存在，不意味着本次历史 API 要返回它们。**

本次历史 API 确定只返回问题和答案，不实现 SQL/表格回放。界面文本历史通过 Web 历史 DTO 提供；多轮模型上下文继续按现有 Runner 逻辑恢复：先从 Store 会话记录确定当前 thread_id 及携带证据，再从 checkpoint 读取执行状态。Vue 不重传上一轮 SQL，不读取内部 checkpoint 来展示历史。

达到线程轮换条件后，旧消息不一定完整留在当前 checkpoint；Agent 状态还可能包含工具消息和摘要。Web 历史固定使用 §4.4 的文本日志，不依赖当前线程 messages，也不增加合并旧归档的另一条回放路径。

### 4.5 当前查询的结果证据与表格

本节仅适用于当前页面中新执行的查询，不适用于查看历史记录。历史接口和页面遵守 §4.4 的仅问题/答案约束；以后如需历史 SQL 或表格回放，另行修改设计，不在本次隐含实现。

[EvidenceItem](../nl2sql/src/modules/db_deepagent/schemas.py)提供实际 SQL、result_ref、row_count 和截断信息，**不包含 rows/columns**；不能单凭 DbAgentResult 画表格。

Web 适配层使用 [ResultStore.read_rows](../nl2sql/src/modules/db_deepagent/services/result_store.py)读取 §4.2 的分页 DTO：列固定为 `{name, pg_type}[]`，行值使用下述 v2 cell 编码，并验证 session_id。即时查询响应只给 evidence/result_ref，第一页及后续页均通过结果 API 获取，不另设 preview 字段。

- 以 evidence_id 关联 finding 与证据卡片，区分 SQL 查询和本地结果分析。
- 展示实际执行 SQL；sql_template 如存在作为附加详情，不拿模板冒充实际 SQL。
- 显示总保存行数、当前页范围、has_more、source_truncated、生成时间及 warnings，不能用前 20 行冒充全部数据。Runner 目前将 EvidenceItem.preview_truncated 固定为 false，不能据此判断表格已展示全部；首版不展示该字段，工具信封内部的预览截断语义不直接搬到页面。
- 当前查询分页时若文件缺失，显示“结果已不可用”，保留当前响应中的有效答案和 SQL；不得自动重新执行 SQL。查看历史记录不读取这些文件，也不展示历史表格过期提示。
- 结果保留期配置为 30 天、归档保留期为 365 天（2026-10-19 调整，原 180 天；两者均为下限口径，具体以配置参数为准）。生产清理统一走 [persistence/maintenance.py](../nl2sql/src/modules/db_deepagent/persistence/maintenance.py) 的 maintenance 清理流程。2026-10-19 修订：结果目录改为**超过保留期即删除、不再受 checkpoint／携带证据／归档引用的保护制约**——历史展示改由结果快照表承接（见 docs/3.1 §7），原"由引用保护、并非超过 30 天必删"的条款作废。`ResultStore.purge_expired` 本身只按时间删除，没有引用保护；Web 或运维脚本不得绕过 maintenance 直接调用该方法。历史文本展示不依赖结果文件保留（快照兜底），无需为历史展示调整结果保留机制。
- 结果类型编码必须同时覆盖保存与读取：仅在 HTTP 响应前转换 Decimal，不能修复保存时 numeric[]/time/interval/UUID/bytea[] 的 TypeError。当前 _encode_value 仅转换顶层，_decode_value 还会把普通 JSON 的 `$dec` 键误当类型标签；NaN/Infinity 会写成非标准 JSON。这些属于 P1 必需修正，不将其推给 doc 3 的存储介质改造。

**采用一个可版本化的 cell 编码器，文件与 Web 共用编码规则**：新文件 meta 增加 format_version=2，行内每个 cell 为 `{kind, value}`；类型标签只解释这一层信封，不递归猜测业务 JSON 对象的键。rows 仍按列序为二维数组，内部 read_rows 保留按需解码到 Python 类型的能力；Web 使用原始编码读取模式，不经过 Any/默认 JSON 编码再把 Decimal 转为 float。具体 kind 如下：

| kind | value / 展示规则 |
| --- | --- |
| null / boolean / text | null / JSON 布尔 / 字符串；按文本展示 |
| integer / decimal | 十进制字符串；包括安全范围内整数也不强制转 JS number，保留大整数和小数精度 |
| float | 有限值为 JSON number；非有限值为字符串 `NaN`、`Infinity`、`-Infinity`，由 kind 区分并按文本展示 |
| date / datetime / time / uuid | 对应 ISO 文本或标准 UUID 文本；datetime 保留时区，不悄悄转成本地日期 |
| interval | timedelta 的精确总微秒十进制字符串，不通过 float 计算；UI 格式化为时长文本 |
| array | value 为同规则的 cell 数组，递归保留元素类型及 null，支持多维数组 |
| json | value 为 JSON 文本字符串，前端以文本显示而非默认 JSON.parse 后重算数值；`$dec/$bin/kind/value` 等业务键均不解释为标签 |
| binary | value 为 `{length:N, content_saved:false}`；只显示“二进制内容未保存（长度 N）”，无 Base64/下载。沿用 len(value) 长度语义，不承诺所有 memoryview 都按字节计数 |

编码参考列 pg_type 与实际值，JSON/JSONB 必须先识别为业务 JSON；不以 str(任意对象) 静默掩盖不支持类型。最终文件及 HTTP JSON 均严格禁止裸 NaN/Infinity，JSON 文本中的既有数值只承诺保存驱动已提供的精度。执行期未知类型显式形成 result_encoding_error/安全限制说明：有其他有效产出时为 partial，否则 failed，HTTP 200 保留其他有效证据和答案；结果 GET 无法读取/转码时才按 §4.2 返回 HTTP 500，不把文件不存在混同为编码失败。

仅接受 meta 缺少 format_version 的旧文件（按 v1）和 format_version 为整数 2 的新文件；显式的其他版本、布尔值或非法类型均返回 500/result_encoding_error，不猜测格式。v2 的未知 kind、信封缺少/多出字段及 kind/value 类型不符同样返回 result_encoding_error；文件或 SHA256 损坏仍按 result_integrity_error。旧文件无 format_version 时按 v1 兼容读取：先校验原始文件 SHA256，再按列 pg_type 解释已知 numeric 的 `$dec` 或二进制占位，JSON 列的同名键保持普通 JSON，不调用无条件标签解码；结果输出统一转成上述 cell。旧文件无需重写、重跑 SQL，无法恢复当初未成功保存或已丢失的精度。旧版文件中非有限浮点也在输出时转为带 kind 的文本；损坏值返回明确错误。共用读取实现继续服务列值提取和本地分析，不另造一套会话授权/文件访问路径。

新增测试覆盖 numeric[]、time、interval、UUID、bytea[]、含 `$dec/$bin/kind/value` 的 JSON、NaN/Infinity、超过 2^53 的整数、旧 v1 文件和同一值的保存→读取→HTTP→前端显示；不改变 ResultStore 的文件落地介质或提前实施 doc 3。

## 5. 数据源选择与按源执行

### 5.1 已有列表与详情 API：可以复用

代码链路为：[数据源路由](../metaweave/backend/api/routers/data_sources.py) → [登记服务](../metaweave/backend/data_sources/registration_service.py) → 平台 PostgreSQL 的 [DbSourceInfo 模型](../metaweave/backend/persistence/models.py)，表名为 **`public.db_source_info`**。

| 已有接口 | 返回结构 | 是否包含要求字段 |
| --- | --- | --- |
| `GET http://127.0.0.1:8001/api/data-sources` | `{items: DataSourceCatalogItem[]}` | 包含 source_id、display_name、db_type |
| `GET http://127.0.0.1:8001/api/data-sources/{source_id}` | `DataSourceListItem` | 包含 source_id、display_name、db_type |

[响应模型](../metaweave/backend/api/schemas.py)中的基础字段完整列为：source_id、display_name、db_type、host、port、db_name、username、sslmode、selected_schemas、description、db_version、created_at、is_active。列表额外包含 last_collected_at、last_published_at。**没有 password 或 password_ciphertext**。

上表 DataSourceCatalogItem 是后端响应模型名；当前 Vue [types.ts](../frontend/src/api/types.ts)使用 DataSourceListItem 并以可选 last_* 字段兼容列表，另有 AssetCatalogItem，尚无同名的 DataSourceCatalogItem 前端类型。首版可复用现有列表类型；若新增 NL2SQL 展示类型，须区分列表附加字段与详情基础字段，不将数据资产类型当作数据源类型。

last_collected_at 实际取该源作业 updated_at 最大值，包含运行中和失败作业；last_published_at 取发布记录 created_at 最大值，包含失败尝试。UI 如展示，使用“最近采集活动”“最近发布尝试”，不得写成“最近成功采集/发布”，也不据此判定就绪。db_source_info 有 source_id 主键及字段检查约束，但连接组合没有唯一约束，重复连接由应用层查重且 confirm_duplicate 可放行；同一连接可能对应多个 source_id，仍须分别处理。

现有 Vue [dataSources.ts](../frontend/src/api/dataSources.ts)已经提供 `listDataSources()` 和 `getDataSource(sourceId)`，查询页直接复用，不需要为取这三个字段新增一个重复的列表 API。当前列表服务不自动排除停用源；UI 与 NL2SQL 后端都需要检查 is_active。

当前 API 和表约束只支持 `db_type = postgresql`。保留类型展示和检查，不将“可以选择多个数据源”表述成已经支持 MySQL 等其他数据库。

新建流程为：加载列表 → 选源并读取 NL2SQL readiness → 配置兼容且必需项就绪后创建会话 → 提问。下拉显示 display_name/db_type，可辅以 db_name；value 固定为短 ID source_id（`db_src_N`）。停用、类型不支持或 db_name/selected_schemas 不符合知识键空间的源标记原因并禁用创建；列表信息可作初步提示，服务端 readiness/创建仍按可信登记重查。无源、全部不可用、读取失败分开显示，不能等到模型运行才暴露键空间不兼容；不要求修改 MetaWeave 的通用登记规则。

### 5.2 前端列表与后端连接是两条路径

```text
Vue ── MetaWeave 列表 API ── 平台 db_source_info（展示与选择）
Vue ── {source_id} ── NL2SQL Web ── 数据源解析器 ── db_deepagent（执行）
```

不能将列表 API 返回值直接作为可信执行配置。NL2SQL 必须按收到的 source_id 从可信后端来源重新读取登记记录，检查存在、启用状态、db_type 和 selected_schemas，并取得密码。

**D6 已确认**：NL2SQL 后端以独立只读连接访问 MetaWeave 平台库的 db_source_info，用同一个 `SOURCE_CREDENTIAL_KEY` 解密 password_ciphertext。前端只接收公开字段，密钥仅存在后端。这样两个业务后端不互调 API，也不必让列表接口返回密码。MetaWeave 平台库和 LangGraph 持久化库是不同职责，不能拿 `LANGGRAPH_DB_*` 默认替代源登记库配置。

已按用户授权，将 `metaweave/.env` 中的 `PLATFORM_DB_HOST`、`PLATFORM_DB_PORT`、`PLATFORM_DB_NAME`、`PLATFORM_DB_USER`、`PLATFORM_DB_PASSWORD`、`SOURCE_CREDENTIAL_KEY` 复制到 `nl2sql/.env`；原 MetaWeave 文件及 NL2SQL 已有配置保持不变。源文件没有定义 `PLATFORM_DB_SSLMODE`，本次未自行补值。已在内存解析核验复制值一致，不在文档、对话或日志中记录实际值。

doc 4 已实现独立的 [source_registry.py](../nl2sql/src/modules/db_deepagent/source_registry.py)：`resolve_source_registration(source_id)` 使用 psycopg 从平台库读取登记，检查短 ID 格式（`db_src_N`）、存在性、启用状态和 PostgreSQL 类型，并用 `SOURCE_CREDENTIAL_KEY` 解密密码；`apply_registration_to_config` 将登记值注入独立运行配置。query/chat CLI 已复用这条链路，Web 应复用相同公共能力。源登记库读取 `PLATFORM_DB_*`；现有 db_deepagent 的 `platform_db` 继续指 LangGraph 持久化库，两类连接保持职责独立。

登记解析还须补充非空范围检查：当前 `tuple(selected_schemas or ())` 会接受空范围，下游 allowed_schemas 为空时按“不限制”处理。平台表已有 selected_schemas 非空约束，但 NL2SQL 不能把该约束当作唯一防线；解析时遇到 null 或空列表应将该源判为不可用，返回稳定的源配置错误，不装配业务服务、不退回全库范围。非空范围继续遵守已有键空间规则，不另造配置字段。

此前配置复制沿用已有平台库账号，没有创建新角色或更改数据库权限。现有解析器使用独立连接且仅执行登记 SELECT，但尚未显式设置只读连接/事务；不能将“仅执行读语句”宣称为数据库权限或事务级只读已经完成。Web 接入仍须落实 D6 的只读访问保证，保留独立连接和参数化查询，不调用 MetaWeave 写入服务；如果需要数据库权限层面的最小授权，可另行配置专用只读账号。

现有解析器已按 MetaWeave [Fernet 密码处理](../metaweave/backend/security/source_password.py)的格式独立实现解密，不跨两个虚拟环境导入另一服务的顶层 `backend` 包。Web 沿用这一登记与密文契约，不新增跨服务 Python 包依赖。

现有公开详情接口继续不返回密码或密文。Web 的后续工作是调用已实现的解析器、补充登记变化检测和错误映射，以及落实上述只读访问保证；不再安排重新开发基础平台连接和解密模块。

### 5.3 运行配置必须来自选定数据源

当前 [business_db YAML](../nl2sql/src/modules/db_deepagent/config/db_deepagent.yaml)保留单源调试默认值，但 [配置模块](../nl2sql/src/modules/db_deepagent/config.py)与源解析器已提供按源覆盖能力：`apply_source_runtime_config` 校验登记库名前缀并同步 database、expected_database、table_key_prefix，`apply_registration_to_config` 同时注入连接、sslmode 和 allowed_schemas。Runner 接收已装配的 services，Web 必须先完成按源配置，再调用 `build_services(cfg, source_id=...)`。

应在创建服务前由“全局模型/预算/知识后端配置 + 登记数据源快照”生成并验证运行配置，不能先要求 `.env` 的 DB_NAME 指向某个固定库再覆盖。当前 CLI 先 `load_db_agent_config()` 后注入登记，而加载函数已执行 database/expected_database/table_key_prefix 三键一致性检查；YAML 后两项默认 orders，DB_NAME 为其他库时会在覆盖前失败。Web 公共装配不得照搬这一顺序：先读取源无关的基础配置内容，再应用登记连接、范围和三键覆盖，最后完成配置模型构造及完整校验，再创建服务。可复用现有底层配置读取与映射逻辑，不通过修改全局环境或取消最终一致性检查解决。

Web 装配顺序为：读取 Web 登记状态及会话绑定 → 拒绝已失效登记、比对请求 source_id → 重新读取并校验登记及知识版本 → 按 §5.5 轻量复核/完整检查缓存判定 → 读取基础配置、应用同一登记快照并完成最终校验 → `build_services` → 构造持久化、Agent 和 Runner → `Runner.run(..., source_id=...)` 在锁内再次核对状态/版本并确认 pending 收尾保护后才恢复 checkpoint → 终态复核、归档及文本保存 → 释放本轮资源。公共配置加载与按源覆盖的接线由 Web 装配完成，现有 `apply_registration_to_config` 的映射规则继续复用；YAML 默认连接不能成为选源依据，失败时不回退到默认源。

| 登记字段 | 运行时用途 |
| --- | --- |
| source_id | 会话源绑定，知识检索与结果作用域的登记主键 |
| host、port、db_name、username、解密密码 | 源库连接；expected_database 使用登记 db_name 并与真实连接核对 |
| sslmode | 源连接安全选项，显式传入，不能遗漏或继承其他源值 |
| selected_schemas | 映射为非空 BusinessDbConfig.allowed_schemas，限定 Web 完整查询管线的目录、表键、SQL 范围及 search_path；空值拒绝，登记变更后的知识有效性另按 §5.5 核验 |
| db_name | 已同步 database、expected_database、table_key_prefix，并与发布侧知识键契约一致，不固定 orders |
| 连接及范围字段指纹 | Web 待补：只用 host、port、db_name、username、规范化 sslmode、selected_schemas（保留顺序）判定目标/范围变化；updated_at 若读取仅供诊断，不作失效依据 |

当前 BusinessDbConfig 已有 sslmode 和 allowed_schemas；登记字段仍叫 selected_schemas，两者由解析器映射，不另新增一个重复配置字段。完整 SqlPipeline 先经 SQL validator 的范围检查，再经过守卫和执行；目录服务、表键归一与连接 search_path 配合使用该范围。SQL 分析 AST 按 PostgreSQL 规则统一归一未引用标识符，保留双引号名称；原始 SQL 继续用于授权和执行。

创建 Web 会话时保存上述字段的规范化快照指纹，每轮查询比较同一规则。selected_schemas 顺序参与比较，因为它影响 search_path；不任意排序消除差异。display_name、description、updated_at 不参与指纹；is_active 单独校验，密码轮换不强制重建上下文。现有 SourceRegistration 不含 updated_at 不妨碍按已有字段计算指纹，不能为此把更新时间当目标变更信号。

上述保证依赖完整管线，不能解释为所有底层方法均独立实施范围授权：table_exists、column_type、locate_table_schemas 自身没有完整 allowed_schemas 检查，sql_guard 对显式 schema 的范围拒绝依赖前置 validator。Web 查询须走完整 SqlPipeline，不能直接以这些方法或单独守卫替代范围校验；其他业务若新增直接调用，须先明确受信上下文或使用已有校验入口。locate_table_schemas 为识别裸表名的实际解析需要查询 pg_catalog，不应机械地给每个底层方法套同一过滤。Web 复用现有解析与校验，不新增第二套规则。

键空间沿用 doc 4 §4.2：db_name 是不透明的知识前缀，允许连字符、数字开头和内部普通空格，拒绝空串、首尾空白、点号及控制字符；schema/表/列当前支持 `[A-Za-z_][A-Za-z0-9_]*`，知识键保留实际大小写。模型/工具输入的逻辑表键使用 `normalize_table_id(...).key`；持久化知识身份用 §5.5 的严格入口校验，不直接套用模型容错函数。**这两个入口不是同一个函数；实现时在各自注释注明用途，仅共用字符判定/拼接，不为“复用”将 readiness 改回宽松 normalize_table_id。** 向 SQL 提供 schema.table 或范围内可解析的裸表名，source_id、entity_id 和知识 db 前缀不作为 SQL 表引用的限定段。

**业务源只读契约以实际执行事务为准，不要求将源库全局配置为只读。** 当前 [BusinessDbService](../nl2sql/src/modules/db_deepagent/services/business_db.py) 有两层设置：连接 options 显式指定 `default_transaction_read_only=on`；`run_readonly` 和 `explain` 在事务内首先执行 `SET TRANSACTION READ ONLY`，再设置 `SET LOCAL statement_timeout`。按源装配须保留这两层行为及 SQL 校验，不修改源库的服务器级、数据库级或角色级默认配置；此契约仅针对查询业务源，不使需要写入的 checkpoint/归档库变为只读。

当前 [doctor 业务库检查](../nl2sql/src/modules/db_deepagent/cli.py)读取的是其连接会话的 `SHOW default_transaction_read_only`，并非直接检查服务器全局值。由于连接 options 已设置为 on，不能推断“普通源服务器默认 off 必然使 doctor 失败”，也不能将 on 描述成仅适用于 orders 的环境约定。PostgreSQL 的 [默认值与事务当前值说明](https://www.postgresql.org/docs/current/runtime-config-client.html)及 [连接 options 说明](https://www.postgresql.org/docs/current/libpq-connect.html)可用于核对这一区别。

每源 doctor 应使用与运行期相同的只读事务入口，以只读查询核验实际 `transaction_read_only=on`、本事务 statement_timeout 生效、数据库匹配以及结束后连接回到 IDLE。不能只查 default 值就宣称运行期事务安全：会话 default 为 off 但实际事务只读生效时仅告警，不因此判整源失败；无法建立连接、实际事务未只读、超时设置未生效或数据库不匹配则失败。检查不执行写入探针，不自动修改源库设置；只读业务源检查与知识库是否已发布的诊断分别报告。

Web 不修改全局 `.env`、全局 ConfigLoader 缓存或 YAML 来切源。每个会话绑定独立的源上下文；配置、连接池、Agent handle、校验/结果沿用指纹和目录缓存不得跨源污染。Milvus 已按实例分配唯一 alias，close 只断开本实例；Web 沿用 §4.3 的每轮构造、结束释放方式，同源并发也不能共用一个可变执行实例。

### 5.4 知识检索隔离已实现，Web 复用 doc 4 契约

[BusinessDbService](../nl2sql/src/modules/db_deepagent/services/business_db.py)计算的 `pg:<摘要>` datasource_id 是 database、role、search_path、server_addr、server_port 的摘要，不含 source_id。两个登记源指向同一物理连接及角色/范围时可以得到相同 datasource_id；它用于连接兼容性与证据校验，**不是 source 级身份或授权键**。两个标识均保留，Web 必须另以 source_id 会话绑定和 result_ref 的 session_id 归属校验隔离来源，不能仅凭 datasource_id 相同允许跨源复用 checkpoint、证据或结果；本次不要求重定义现有指纹算法。

当前 [Milvus 适配器](../nl2sql/src/modules/db_deepagent/services/milvus_retrieval.py)构造时必须绑定合法 source_id，六个入口 search_tables、search_columns、search_dim_values、search_similar_sqls、fetch_table_cards、fetch_table_categories 均追加同一来源过滤，精确回查也按源隔离；缺失或非法来源拒绝，不回退到全库查询。Web 通过 `build_services` 注入绑定，不把 source_id 暴露为模型工具参数。按源就绪与 Embedding 一致性仍按 §5.5 分别检查。

[JOIN 图服务](../nl2sql/src/modules/db_deepagent/services/join_graph.py)已绑定来源，表存在性和两种寻路入口均按 `entity_id = source_id:逻辑表id` 定位，返回仍保留逻辑表 id。[MetaWeave writer](../metaweave/metaweave/core/cql_generator/writer.py)已对 Table、Column 使用实体键 MERGE，保留 id/full_name，并给节点及 HAS_COLUMN/JOIN_ON 关系写入 source_id、publish_run_id（doc 12.1 统一命名）。[loader](../metaweave/metaweave/core/loaders/cql_loader.py)在单个数据事务内完成来源检查、按源删除、写入及数量核验，事务内失败时回滚保留旧图；发布服务区分明确回滚、图已提交和提交状态未知，不能把未知状态当作旧图仍有效。旧约束替换、旧节点回填和重新发布 rel 的维护流程由 doc 4 §4.8 承接，Web 不新增另一套迁移或发布路径。

[目录服务](../nl2sql/src/modules/db_deepagent/services/table_catalog.py)、图服务及其运行配置由同一个源绑定构造；结果引用继续按 session_id 校验，证据沿用保留现有连接与校验指纹约束。前端选中源 A 时，检索、实际连接及模型可见目录必须全部对应 A。此处是来源隔离，不代表已经实现 publish_run_id 版本快照读取或 doc 3 的结果存储改造。

**doc 4 状态勘误（仅在本文登记）**：其首页“尚未实施”、§5.2“session_record 当前没有 source_id”、§4.8.1“待开发脚本”及末尾“未创建迁移脚本/doctor 参数”描述的是实现前状态，不能用于判断当前代码。当前 session_record/Runner 已有 source_id，迁移脚本和 doctor 参数已存在；本文引用 §4.8 仅复用停机、备份、预检、迁移、重发布和核验顺序，不要求再次开发这些能力。**脚本存在不等于 --apply 已执行，代码交付不等于正式图库维护或双源验收通过**，后者以执行报告为准。本轮不改 doc 4 原文。

### 5.5 Web 会话生命周期与就绪检查：待实施契约

doc 4 已完成基础来源绑定与隔离；以下接线、变更检测和 Web 策略仍待实施，不能随基础能力交付一并标为已实现。

- 每个会话固定 source_id，切换来源创建新会话，不复用原 checkpoint 或证据。
- 每轮执行前重新确认源仍存在、启用且类型受支持；源停用/删除时阻止新查询，已保存文本和有效旧引用仍可读。
- 登记连接目标或 selected_schemas 字段指纹变化返回 source_changed，要求新建会话；显示名、描述或单改 updated_at 不失效。
- 按 §5.5.1 保存并核对知识版本；不允许跨发布继续旧上下文，不实施多版本查询。
- readiness 说明检查时点的可用性，不是未来运行永不失败的保证，也不以 last_published_at 或全库数量代替按源检查。

readiness 固定为 `{source_id, compatible, ready, checked_at, checks}`，checked_at 是完整检查完成时刻（命中缓存不伪装为本次重做）；checks 为 `{name, required, state, error_code, message, counts?}[]`，state 为 pass/warn/fail，正常项 error_code=null。compatible 只表示 PostgreSQL 类型、非空 selected_schemas 和知识键空间兼容性，不把网络失败叫作“不兼容”。其 HTTP 约定为：

| 情况 | HTTP 与 data |
| --- | --- |
| 兼容且完成检查，包括缺知识 | 200，data 直接为 readiness；compatible=true，ready 按检查结果为 true/false |
| 不兼容或登记配置非法 | 422，标准错误 data 内固定附 `readiness`，compatible=false、ready=false，checks 保留已完成项，不尝试后续依赖 |
| 依赖/登记读取无法完成 | 503，标准错误 data 内固定附 `readiness`，ready=false，compatible 为已知 true/false 或尚不可判定的 null；保留已完成项，出错项明确 fail，不按零计数冒充未发布 |
| 源不存在或停用 | 404/source_not_found 或 409/source_inactive，标准错误 data 内固定附 readiness；ready=false，compatible 尚未判定时为 null，checks 说明终止原因 |

业务源检查按原因区分：连接、认证、驱动、网络超时或检查 SQL 未能完成为 **503/dependency_unavailable**，check.error_code 同名；不能将无法测量写成“无知识”。查询成功返回了确定结果，但真实数据库不等于登记 db_name、实际 transaction_read_only 不为 on 或事务超时设置未生效，为 **200/ready=false**，对应检查码 `business_target_mismatch / business_readonly_invalid / business_timeout_invalid`；创建/提问据此返回 409/source_not_ready，不启动 Agent。仅 default_transaction_read_only=off 而实际只读事务有效为 warn/readonly_default_off，不阻断。登记缺失、停用、不兼容仍分别按 404/409/422，优先于依赖检查。

checks.error_code 的闭集为 §4.2 非 200 error_code，加 `business_target_mismatch | business_readonly_invalid | business_timeout_invalid | readonly_default_off | knowledge_missing | knowledge_inconsistent | optional_knowledge_empty` 及 null；缺必需知识、已确认身份/发布不一致、正常可选空数据分别用后三项。503 出错项用具体登记/依赖不可用码，不能生成任意异常字符串。required=false 仅表示该类数据可以正常缺省，不表示检测到存在但不一致的数据也可忽略。

因此 **HTTP 200 时 compatible 只会为 true**。前端成功读取 data，非 200 读取 data.readiness（检查尚未开始的参数 422 可无此项），不要求靠不同猜测解析。readiness 的 source_id 对合法路径短 ID 始终为规范化值，不返回密码或连接串。创建/查询因未就绪被拒绝时采用 409/source_not_ready，并附 data.readiness；不会将 GET readiness 的正常 ready=false 误包装为后端故障。

**首版采用“table_schema 与 rel 成套可用”的产品范围**，而非声称简单 SQL 在技术上必需图服务。当前 JoinGraphService 构造本身不验证图非空；仅发布 table_schema 的源可能支持简单查询，但本稿首版不提供“部分知识能力模式”，统一要求完整准备后接入，以避免增加无图模式、工具开关及恢复分支。后续若要允许无图源，再单独明确 plan_joins 的降级契约。本条是本轮设计取舍，随本文审核，不冒充既有用户决定。

| 检查 | 必需性及判定 |
| --- | --- |
| 登记/业务源 | 必需；可信登记合法启用，业务库目标一致且实际只读事务检查通过 |
| table_schema | 必需；按 source_id 过滤，object_type=table 非零；字段及向量维度合法，不能仅有 Column 就算就绪 |
| Neo4j rel 图 | 按上述首版产品范围必需；本源 Table 非零，节点 entity_id/来源和 Column/HAS_COLUMN 归属有效。JOIN_ON=0 合法，不要求每源都有外键路径 |
| dim_value、sql_example | 数据可为空，给 warn，不阻止使用；非空时必须通过下述实际 publish_run_id 核对，残留/混批为 fail 并阻止使用。当前三集合的结构/向量字段及配置依赖仍需存在且一致，集合缺失/不可用不能伪称“可选数据为空” |
| 发布及结构一致性 | table_schema/rel 均有本源源级最近成功发布记录（`vw_workspace_latest_successful_publish` 视图按工作区/步骤各一行；NL2SQL 经 `DISTINCT ON (step_name)` 按源取每步骤最近一行）；各自实际知识 publish_run_id 与对应记录的 publish_run_id 一致；执行下述集合核对。两个步骤来自不同工作区/发布运行均合法（doc 6.2 §10.2），不要求同作业或同工作区 |

发布状态由 NL2SQL 对平台视图 `public.vw_workspace_latest_successful_publish` 关联 `workspace_info` 做参数化只读查询，按源及步骤读取 `step_name/publish_run_id/published_at/workspace_id`（doc 6.2 §10.2 源级最近成功：`published_at DESC, publish_run_id DESC`，失败不替代之前成功），不调用 MetaWeave HTTP。完整检查先按 source_id 枚举本源实际知识，检查全部 publish_run_id，再核对批次；不能只筛当前版本而隐藏其他批次残留。枚举 Milvus table/column 的 object_id、parent_id 及 Neo4j Table.id/Column.id 与 HAS_COLUMN 端点，**先按以下规则规范化再比较**：表集合必须相等，Milvus 每个列键及其父表必须在图中存在且对应，不混入其他源/批次。不能只比较数量，也不能只读 Store/Milvus 的默认第一页。

**跨存储对象身份的唯一比较键**：使用本次登记快照，经**独立纯函数严格校验入口**得到三段逻辑键 `db.schema.table`，外层作用域是 source_id；列键为 `(规范表键, 实际列名)`。返回的键格式与 TableKey.key 一致，TableKey.display / Neo4j full_name 仅作显示及属性一致性核验，不直接作为跨存储主键。现有 `TableCatalog.normalize_table_id` 面向模型/工具输入，会 strip 并对 schema 做小写匹配兜底，**不能直接充当持久化知识身份校验器**；本期只复用 keyspace 的字符判定与拼接规则，不改变现有模型输入容错行为、不新增可切换的宽松/严格模式。

- 当前受管 [table_schema 输入](../metaweave/metaweave/core/table_schema/managed_input.py)已要求严格三段文件名并核对 JSON 身份，表 object_id/parent_id 为该 stem，列 object_id 为 `stem.column`、parent_id 为 stem；不是任意裸表名。Neo4j 模型允许 database 缺省而形成两段 id，因此仍须明确读入及拒绝规则。
- 严格入口不 strip、不修剪各段、不 lower/casefold、不去除引号。两段表键仅用同一登记 db_name 补前缀；三段键首段必须与之精确一致。db 前缀不折叠大小写，沿用 §5.3 的不透明字符串规则，内部普通空格仍允许，不按普通 SQL 标识符解析。持久化知识的 schema/表/列保留真实大小写，schema 必须**精确命中**登记范围；不把模型输入的未引用名称折叠兜底用于合并两个实际知识对象，不将 Sales 与 sales 合并。首尾空白、schema/表/列段内空白、未知 schema、非法段数或字符均拒绝，不能忽略后继续计数。
- Milvus 列从 object_id 最后一个点拆出实际列名，归一前缀表键并与归一 parent_id 相等，再构造列键。Neo4j Column.id 同样拆分，核对 schema/table/name、full_name 与 HAS_COLUMN 所指的 Table；表 id 也须与节点 schema/name/database 一致。相同规范键对应多个对象、错误父子归属或无法解析均为 knowledge_inconsistent。
- 规范集合相等还须核对**可实际命中的实体键**：Neo4j 的 entity_id 必须精确为 `source_id:三段规范逻辑键`（列另加列名），节点/边 source_id、publish_run_id 也须符合本批。旧两段 id 即使归一后集合相等，只要实际 entity_id 仍是两段形式就 ready=false，要求受管重新发布 rel；不能用比较时补前缀掩盖 JoinGraphService 无法命中的事实。

例如登记 db_name=my-orders、schema=public，`public.orders` 与 `my-orders.public.orders` 的比较键均为 `my-orders.public.orders`；列 `my-orders.public.orders.id` 的键为 `(my-orders.public.orders, id)`，Table.entity_id 应为 `<source_id>:my-orders.public.orders`。对象键归一本身不调用模型、不创建整套服务，也不将知识前缀带入 SQL；可复用归一纯逻辑，P1 不为身份比较而额外建立一套执行连接。readiness 仍可能触发 §5.5.2 已约定的 Embedding 探针。

**列集合不能机械要求完全相等**：现有 table_schema 发布支持 include_columns=false，且 skip_empty_desc 会跳过空描述列，Neo4j 则保存完整列。因此采用“源级最近成功发布各自核对 + 表集合相等 + 已发布 Milvus 列是图列的合法子集”的首版兼容性条件；图中多出的列本身不判失败，报告数量差异和已知跳过规则。此检查证明声明的对象身份兼容，不承诺逐列类型/描述内容或同一发布内人工改文件的语义完全一致；发布内产物应按既有受管发布校验使用，不在本期新增历史产物版本仓库。表集合、父子归属或发布版本不符均 ready=false，不因“各自非空”放行。源范围还须与 §5.3 的登记范围一致；全局 embedding_release.yaml 不扩展 source 维度，也不能替代本源知识检查。

**可选知识也不得把残留当作有效数据**：对 dim_value、sql_example 分别按 source_id 完整枚举实际 publish_run_id 集合，与对应步骤 dim_value/sql 的源级最近成功发布记录核对。无记录且无行，或存在记录但合法结果为空，均 warn/optional_knowledge_empty；有行时要求记录存在、所有行 publish_run_id 非空且仅等于该记录的 publish_run_id。无记录但有旧行、混入另一批次或缺少批次字段，均 fail/knowledge_inconsistent、ready=false，修复或受管重发后再接入；不以“可选”放过会被当前按源检索读到的陈旧数据。可选步骤沿用独立发布范围；实际版本必须与各自源级最近成功记录一致。检查读取失败为 503，不按空集合处理。无需本期增加可选工具开关或改成多版本检索。

### 5.5.1 会话知识版本与发布期间边界

采用最小策略：**创建时保存 knowledge_snapshot，版本变化要求新会话，不把旧 checkpoint 迁入新版本**。snapshot 包含本源 table_schema/rel 的 `(workspace_id,publish_run_id)`、可选 dim_value/sql 发布的 `(workspace_id,publish_run_id)`（未发布为 null；平台步骤名分别为 dim_value、sql），以及实际使用的 Embedding release 内容/模型配置、Milvus 和 Neo4j 目标配置摘要。实际知识批次须经 §5.5 完整核对（或同键有效缓存）与源级最近成功记录一致后，才可据此创建快照；不再存一份可与记录矛盾的“实际版本”真值，计数也不当版本键。由后端规范化构造并保存到 Web 登记，不接收客户端指纹，不公开凭据或配置摘要原料；可选知识从无到有或重新发布也算变化，避免旧轮次证据与新检索混用。table_schema/rel 允许来自不同工作区/发布运行（doc 6.2 §10.2）；重复发布即使内容相同也保守要求新建会话。

每轮先读取 Web 登记，已 invalidated 或取得锁后仍有上轮 pending_run_id 时优先返回 409/session_invalidated，不恢复 checkpoint；其余请求在恢复 checkpoint/沿用证据前，将最新可信登记和源级最近成功发布记录与会话快照比较。不同或既有 Web 会话缺少可信 snapshot 时，锁内持久化 invalidated/knowledge_changed 并返回 409/knowledge_changed，不自动补为当前版本、不仅靠线程轮换继续；写入不能确认也不得执行。源连接/范围指纹改变优先返回 source_changed，并失效为对应原因；源停用/删除按相应错误拒绝，不自动改写其原有历史。历史读取与已取得 result_ref 的读取不进行发布版本或可续聊状态检查，原会话不删除。

**D8 已确认（2026-10-18）：发布知识期间先不使用 NL2SQL，发布完成后再用。** 发布前先停止接收新问题，等待正在进行的查询及收尾完成；发布和核验完成后恢复查询。这段暂时不使用查询功能的时间即本文所说的“维护窗口”，无需另设固定时段，普通重发布也按此约定执行。首版采用以下人工操作流程，尚无技术互锁阻止 MetaWeave 发布页被误点，不把使用约定宣称为运行时自动保证。本期不新增共享维护标志/锁或版本化读取；以后需要发布与查询并行时再单独设计。readiness=true 只表示知识检查通过，不宣称支持在线发布；界面/部署说明须明示“发布期间暂停查询”。

按已确认的 D8，普通发布统一执行以下操作流程（本地单用户首版，停止范围包括所有会读取待发布知识的消费者）：

1. 暂停前端新提交，并停止 db_deepagent CLI/chat、测试脚本、定时任务或其他读取同一知识后端的进程接收新问题，等待其在途查询正常完成；发布期间不得另起这些消费者。只关闭 8008 不能保证它们已停止。**此时不要点 MetaWeave 发布**。记录目标 source_id、待发布步骤、现有源级最近成功发布标识及需排空的进程/任务；无法确认其他消费者已停止时中止发布。
2. 向 NL2SQL/Uvicorn 发送正常关闭信号，由 **Uvicorn 先停止接单并等待在途连接/ASGI 任务，再执行 lifespan shutdown**；不是 lifespan 先于服务器拒绝新请求。lifespan shutdown 还须等待应用登记的专用 executor 运行、断连后仍在执行的 Runner、版本复核、Web 文本保存及清理全部结束，最后关闭执行器和共享读取资源；ASGI 请求结束/被取消不等于工作线程已结束。该顺序已对照本地安装及 [Uvicorn 0.54.0 Server.shutdown 源码](https://github.com/Kludex/uvicorn/blob/0.54.0/uvicorn/server.py#L281-L311)。确认所有相关消费者无在途运行/残留执行进程且 8008 不再接单；不能仅靠浏览器转圈消失或 health 存活判断排空。Uvicorn graceful timeout、第二次关闭信号及服务管理器超时不得用来强制取消/杀死在途执行后继续发布；排空未确认则中止发布并排查，不用固定 sleep 或 kill -9 继续。本期不为此新增应用级维护开关。
3. NL2SQL 保持停止，通过受管 MetaWeave 链路发布本源 table_schema/rel 及计划中的可选知识；按 §5.5 核对源级最近成功发布记录与实际对象版本。不得绕过链路直接修改知识库。首次图库迁移另按 doc 4 的备份/预检/迁移顺序，不将本 runbook 当作免迁移重发。
4. 发布失败/核验未通过时暂停恢复正常查询，修复或按既有发布/恢复流程还原到可验证状态，不把“发布记录看起来没变”视为没有发生外部改写。可启动 NL2SQL 仅用于受控诊断，但不恢复用户提交。
5. 重启 NL2SQL，先检查 v2 health，再检查该源 readiness；只有 ready=true 后恢复前端及脚本提交。旧会话版本已变化则按 D7 新建；验收记录发布前后标识、排空/重启及 readiness 结果。MetaWeave 可以继续运行，首次新问答仍按正常入口校验，不为验收自动执行 SQL。

维护窗口内 NL2SQL 已停服，readiness 请求可能是连接失败，不承诺返回专门的 maintenance HTTP 错误；Vue 显示服务暂不可用、保留输入且不自动重试。若未来引入在线维护标志/锁，再另定其读写及错误契约。本流程靠同一操作者执行，并不等于当前两个服务已形成技术互斥。

开始前的版本检查仍须在取得会话锁后、首次 checkpoint 恢复前重做，避免仅在 HTTP 入口检查后排队留下窗口；公共 Runner 钩子/装配负责接线。终态收尾、写 Web 文本日志前再轻量核对登记/版本：意外变化时形成 HTTP 200 的 failed/knowledge_changed，无法复核时为 persistence_error/dependency_unavailable，均严格使用 §4.2 清空证据的安全投影及 §4.4 的锁内失效/收尾保护。持久化 state=invalidated，invalid_reason 分别为 knowledge_changed/dependency_unavailable，首次 invalidated_at 不被后续请求改写；禁止恢复既有 checkpoint，即使版本回退也不解禁。保留定位信息与旧文本/文件，不向本轮调用方发出不可信 result_ref。

前后检查只是异常防线，**不能证明查询过程中从未发生切换**，尤其失败发布在外部知识库改写后恢复旧版本记录时，光比较发布记录视图无法识别。严格正确性的前提是上述发布/查询互斥维护流程；不能把 TTL、前后指纹相等或 readiness=true 宣称成跨库原子快照。验收中须记录维护约束；若部署不能执行这一约束，不得以本稿方案宣称支持在线发布与查询并发。

### 5.5.2 完整检查、逐轮轻量复核与缓存

避免每题重复扫描知识，采用进程内短缓存，不缓存服务连接：

1. 每次 readiness、创建和新查询都重新读取可信登记及源级最近成功发布标识，计算源字段指纹、knowledge_snapshot 和实际 release/知识后端配置摘要；这类轻量读取不由 TTL 遮蔽，失败按依赖错误拒绝。源删除/停用/字段或发布变化立即生效。
2. 选源 readiness 和创建使用同一完整检查入口；可命中同键未过期的成功结果。完整检查成功缓存 TTL **30 秒**，键包含 source_id、字段指纹、全部源级最近成功发布版本及实际知识后端/release 配置摘要，同键并发合并检查。缓存到期、键变化或未命中时重新完成业务只读核验、三集合结构/计数、图身份/发布及表列集合核对；失败不缓存成功、不沿用过期通过结果。
3. 每轮仅轻量复核、比较会话快照并使用未过期完整检查；需重做时先做完再装配 Agent。build_services 的一致性检查接入同一受验证入口，不能 API 命中缓存后又在服务工厂无条件重复全部扫描。每轮执行连接仍须核对真实业务库目标、只读和来源绑定；实际 SDK/查询异常正常报告，TTL 不保证依赖未来持续在线。缓存到期仅重查上文明确列出的结构/身份，不保证发现所有人工内容修改；脱离受管发布的改写属于不支持的运维路径，不靠缩短 TTL 假装消除。
4. **创建快照一致性**：读取登记/发布快照 S → 从 S 构造配置和指纹、完成/命中同键完整检查 → 取得创建会话锁、写入前重读登记及发布记录，指纹或版本变化即 409/source_changed 或 knowledge_changed，不写会话登记。保存的字段指纹和 knowledge_snapshot 必须来自通过检查的 S，不能把第二次读取的值替换进去。随后**严格按 §4.4 固定写序：先确认通用 sessions 绑定，再写 pending_run_id=creation_id 的 Web 登记**，两条记录确认且写后复核通过才清除 pending 并返回；禁止反向先写 Web。各写入失败、清除结果不明及列表可见性均按 §4.4 的表处理，不声称发生异常必然留下/清掉标记。首轮依旧重新复核，不宣称跨库原子创建。显示名或 updated_at 单独变化不使 S 无效。

**Embedding 模型探针使用单独的成功缓存，TTL 300 秒**：当前 build_services 每次 check_embedding_consistency 都会 embed_query；Web 将这一模型请求拆为可复用检查，缓存键包含 profile 模型/维度/提供方与端点、凭据配置摘要、Milvus 目标/集合/字段及 release 内容摘要。键变化立即重查，同键并发只执行一次，失败不沿用旧通过结果。readiness 和查询共用入口；首次/到期仍可能付一次探针成本，不保证 readiness 零成本。模型探针缓存不同于上面按源 30 秒完整 readiness 缓存，不能相互代替。两者均不共享可变预算或连接，不引入 Redis/主动失效消息；每轮 DbAgentServices 仍独立构造和释放。

现有 doctor --source-id 仅提供按源计数和短 ID 格式校验，不检查完整登记/业务连接，也不执行上述跨存储集合兼容性判定；可复用查询思路，不能把 doctor 全零或全局统计当 readiness。需要按 §5.3 接入每源只读检查；这些必要后端配套不能省略或声称已由 doc 4 全部交付。

## 6. Vue 代码与双后端配置

### 6.1 目录和路由建议

```text
frontend/src/
├── App.vue                         # 现有公共外壳
├── components/AppSidebar.vue       # 接入“新建查询”入口
├── router/index.ts                 # 汇总路由
├── api/                            # 当前 MetaWeave 客户端，首版保留
└── modules/nl2sql/
    ├── api/client.ts               # origin、HTTP/业务错误、超时
    ├── api/types.ts                # db_deepagent Web DTO
    ├── routes.ts
    ├── composables/useChat.ts      # 状态与请求协调
    ├── views/QueryView.vue
    └── components/
        ├── SourceSelector.vue     # 复用平台数据源列表 API
        ├── ConversationList.vue
        ├── MessageList.vue
        ├── QuestionInput.vue
        ├── QueryDetails.vue
        └── ResultTable.vue
```

建议 `/query` 表示选择数据源并新建对话；`/query/:sessionId` 表示已有 v4 会话，进入时由后端解析源绑定。来源选择只对新会话生效，不信任 URL query 中的 source_id 覆盖已有绑定。页面文件按模块维护；现有 MetaWeave 页面目录暂不整体重排，以降低这次接入的回归范围。

AppSidebar 保持平台主导航；查询页内部提供自己的会话列表与内容区，不额外重复整个应用侧栏。细节样式复用现有 styles.css，新增样式限定在查询模块，避免覆盖已有数据资产页面。

当前没有通用 table/th/td 全局样式，现有表格样式主要位于各 view 的 scoped style；ResultTable 首版自行提供模块内样式即可，不要求先抽全局表格系统。当前也没有全局 toast，沿用 ApiError.code 的结构化识别方式和页面内联加载/错误/重试状态块；NL2SQL 客户端仍须按自身数字 code 协议映射错误，不直接套用 MetaWeave 解包逻辑。

### 6.2 两个独立 origin

建议保留现有 `VITE_API_BASE_URL=http://127.0.0.1:8001` 作为 MetaWeave 地址，新增 `VITE_NL2SQL_API_BASE_URL=http://127.0.0.1:8008`。两个变量都只接受 origin；`/api/v1` 或 `/api/v2` 由各自客户端添加。

本阶段沿用直连模式。**固定 guest 的首版限定本机访问**：NL2SQL 绑定 127.0.0.1:8008、单进程单 worker；`NL2SQL_CORS_ORIGINS` 为显式 origin 白名单，默认仅 http://127.0.0.1:5173，使用 localhost 或其他端口时须显式配置，不沿用当前 `*`、不启用凭据 cookie。除 CORS 响应头外，API 对携带不在白名单内 Origin 的请求直接拒绝为 403/origin_not_allowed；写接口仅接受 application/json，其他类型返回 415/unsupported_media_type，避免仅靠 CORS 限制读取而简单跨站请求仍启动执行。无 Origin 的本机脚本可调用，这仍不等于鉴权；不宣称不同本机用户隔离。允许的 Host 限为本机部署所需值，不能仅凭回环绑定允许任意 Host。

首版 query 提交另加进程内简单频率限制 `NL2SQL_QUERY_RATE_LIMIT_PER_MINUTE`，默认 20 次/分钟（共享 guest，包含被拒绝的重复提交），超限 429/rate_limited；与 §4.3 的全局在途上限共同生效，不引入 Redis。频率检查先于昂贵装配或模型调用。需要局域网/多用户访问时，先补 token 或反向代理鉴权及相应限流，再开放监听地址；本稿不顺带开发统一登录。

新增 NL2SQL origin **仅在进入查询模块/首次调用客户端时惰性校验**，缺失时查询页显示未配置提示，禁止回退 MetaWeave 地址。不要复制现有 http.ts 的 import 期校验，也不要在 vite.config.ts 配置期无条件校验新增变量；NL2SQL 未配置时，MetaWeave 单独构建及页面仍可使用。MetaWeave 原有变量规则保持不变。

NL2SQL 客户端处理顺序：HTTP 状态 → JSON/结构校验 → 外层业务 code → 内层查询状态。非 JSON、422、网络失败、超时分别处理；不要把所有错误都称为“后端未启动”。数据源列表继续调用 MetaWeave 客户端，解析 `{items}`，不套用 NL2SQL 的 `{code,data}` 解包。

## 7. 交互、状态和异常

### 7.1 页面状态

```text
未配置 / 就绪 → 加载数据源与会话 → 选源新建或恢复会话 → 输入 → 提交中 → 完成 / 部分完成 / 待澄清 / 无数据 / 失败
```

同步 API 没有逐步进度。提交时显示等待与已耗时，禁用同一会话重复提交；不展示编造的百分比、SQL 阶段或 Token 流。

- 选源先读取 readiness，兼容且 ready 后允许创建；创建成功更新路由，空会话显示“新对话”。提交完成后刷新列表，turn_count 与文本保存结果一致。
- 每次提交携带当前会话信息中的 source_id；切换来源走新建会话流程，不能复用旧 session_id。恢复已有会话以服务端返回的绑定为准，绑定缺失时提示新建，绑定冲突时保留输入并展示明确错误。
- 保留稳定 session_id，当前 thread_id 变化不新建业务会话；Vue 不将旧链 thread_id 传给新 API。
- 新建对话清空当前输入与展示状态，不调用会话删除 API。
- 创建按钮在请求期间禁用，创建请求超时不自动重试，先刷新列表并提示结果未确认；若发现对应会话已 active/continuable=true，可直接选中使用，不要求因先前 503/超时重新创建。有错误响应 session_id 时按该 ID 定位；纯网络超时没有 ID 时由用户选择列表项，不按“新对话”标题自动认定是同一次创建。用户仍手动再建可能产生另一空会话，不声称创建幂等。
- 历史读取成功后才替换页面；失败时保留原消息，给出重试提示。
- 用会话 ID 和请求序号/令牌关联响应，切换会话后旧响应不能插入新对话；组件卸载可放弃展示，但不宣称服务端已停止。
- 直接访问历史 URL 先读取单会话详情；成功时按正常流程读取 turns。详情返回 409/session_metadata_invalid 时仍以路由中合法的 session_id 调用 turns，进入只读历史模式：顶部提示“会话展示信息不完整，可刷新；若无法恢复请新建会话”，禁用本会话提问，只展示 question/answer；不读取结果 API，不展示历史 SQL/表格。不构造假的 SessionInfo，不从页面选源/URL query 猜 source_id，不填造创建时间或计数；没有有效详情时可用“历史对话”作页面占位标题。仅该稳定错误码触发此分支，404、归属/绑定错误、其他 409 或依赖故障不因此绕过检查；turns 自身仍独立验证 Web 归属。
- 已打开会话的 query 若返回 409/session_metadata_invalid，立即禁用该会话输入，保留草稿及同会话已有消息，只重新读取一次详情、不自动重发 query；详情仍为该码时按上述规则读取 turns 并进入只读历史模式，详情恢复 200 时按 state/continuable 决定是否开放输入。详情重读失败时保持禁用并提示手动刷新，不能把旧响应插进已切换的会话。
- 只读模式中 turns 成功后展示文本，失败则保留此前同会话已显示的内容并提示明确错误，不伪装为空历史、不将其他会话旧消息带入。可手动刷新详情等待校正，详情恢复有效且按正常状态契约可续聊后才重新开放输入，不自动提交。原始 created_at 无法恢复时，本期该会话持续只读，提示新建会话才能继续提问，不引导无限刷新或承诺人工恢复。新建走正常选源流程，不复用损坏登记的 session_id。源已停用/删除但会话详情完整时仍可按原约定查看文本，提交按钮说明不可新查询；未知原始创建时间始终不由前端填当前时间。
- 刷新/切换会话后按 SessionInfo.state/continuable 恢复输入状态：invalidated 始终禁用并显示 invalid_reason，版本回退不解禁；active 但 continuable=false 时统一提示“上一操作尚未确认，请刷新查看”，不将创建 pending 描述为查询正在执行；可重新读取详情，不自动提交或轮询重发 query。active/continuable=true 的空会话可以直接提问，仍须接受提交时的锁/源/readiness 拒绝，不能把它当授权或空闲承诺。
- 新 API 提供分页/has_more，读取范围有限时明确提示“最近 N 轮”，不伪装成无限历史。

### 7.2 v4 状态展示

| status | 页面行为 |
| --- | --- |
| completed | 展示答案、发现、证据及口径 |
| partial | 展示可用结果及限制，不标作全部完成 |
| needs_clarification | 展示澄清问题，允许同会话继续输入 |
| no_data | 展示无匹配数据说明，不作为网络错误 |
| failed | 展示安全摘要与已有可用信息，提供定位 ID |
| budget_exhausted | 说明本轮达到运行预算，保留已有证据，不自动重试 |
| persistence_error | 说明持久化异常；问答保存提示按 history_saved，checkpoint 续聊和表格可用性分别判断 |

会话忙碌按 §4.2 的 `data.error_code=session_busy` 优先识别，不能仅凭 `status=failed` 当作普通查询失败，也不能将其 archived=false 展示为答案保存失败。提示“本次未执行，会话正在处理另一项操作”，保留输入，待当前操作结束后由用户手动提交；不自动重试、不轮询重发查询。锁后端异常则展示服务不可用提示，不声称其他标签页正在执行。忙碌是请求冲突的 Web 展示分支，不增加上表的 Runner status 枚举。

运行终态与历史保存分开展示：archived true/false 都不能判断问答可恢复性；按 §4.4 的 history_saved 显示确认保存、明确未保存或状态未确认。QA 成功但线程轨迹失败时仍独立检查 Web 文本日志，不凭 archived 判断；QA 存在也不能代替 web_turns 已保存。history_saved=true 不表示本轮运行成功或 checkpoint 续聊已就绪。

knowledge_changed/session_invalidated 不自动重发：按安全原因显示“知识已更新”或“本轮一致性未确认，请新建会话”，保留原问答和输入，禁止该会话继续提交；不能只清空页面后用原 session_id 再试。执行中才检测到版本异常的 failed、终态无法复核的 persistence_error/dependency_unavailable 也按此处理；后端已按 §4.2 清空本轮 findings/evidence 等不可信内容，Vue 不依赖“收到证据后再藏起来”保证边界。刷新后以 SessionInfo 的失效状态保持禁用。history_saved 仅说明安全摘要保存情况，与失效状态能否落盘分别处理，不表示混合答案已被认可。

错误文案由 Web 适配层安全化；不直接把数据库连接字符串、堆栈或任意异常原文展示给用户。保留 request_id/run_id 供定位，默认不展示内部 prompt、完整模型对话及工具参数。

### 7.3 超时与重试

旧客户端 query 超时为 120 秒；v4 当前运行预算为 300 秒，属于协作式时限，不能简单沿用旧前端的 120 秒作为 v4 总等待时间，也不能宣称 300 秒一定强制中止。

建议 query 超时独立配置；首版 v4 本地联调从 **480 秒**起评估：协作式预算 300 秒 + 预算到期前已放行的最后一次模型 HTTP 请求最多约 120 秒 + 收尾余量 60 秒。预算到期后会拒绝新的预占，并非仍允许随意启动调用；但已执行调用不会因预算时刻到达自动中断，因此 360 秒可能早于后端完成。

480 秒是等待时长的估算起点，不是严格最坏上界；装配、排队、SDK 重试或额外摘要和持久化收尾仍需实测。同步 Web、HTTP 客户端及部署代理的超时应协调，不能保留更短的中间层超时；本次不改变 Agent 的 300 秒预算，也不新增硬取消或任务系统。超时或断连时仍提示“结果尚未确认，后端可能仍在执行”，不自动重发可能执行 SQL 的请求。

request_id 为服务端权威标识，重复标识拒绝仅用于防覆盖，不提供重放原结果的幂等保证。刷新恢复在途请求、可靠重试或取消仍需另行设计任务协议，不能只加 AbortController 就当作完成。不同会话可在全局名额内并发，本稿不提供跨会话的后台任务状态面板。

## 8. 实施阶段与文件范围

| 阶段 | 工作 | 验收出口 |
| --- | --- | --- |
| 已交付基线（doc 4） | 源登记解析与解密、按源配置、Milvus 过滤及独立 alias、Neo4j 实体键发布/查询、会话来源绑定、迁移脚本和按源 doctor 统计 | 已有代码及相关测试可供 Web 复用；迁移和真实双源发布验收单独记录 |
| P0 Web 设计及部署基线 | D4 同步返回、D7 版本变化新建会话已定稿，D8 已确认；完成本文设计审核并核对 §5.5.1 操作流程与独立 Neo4j 验收实例准备 | D4/D7 不再作为未决项；本文审核通过且验收环境准备核对完成后进入 P1，不将人工操作约定当现有技术互锁，也不将设计定稿等同于环境已就绪 |
| P1 后端 Web 适配 | 公共装配及生命周期、ID 校验/防覆盖、Web 会话失效状态/pending 收尾保护及安全投影、锁内文本收尾、孤立 checkpoint 拒绝、history_saved、元数据缓存修复；**扩展 maintenance 发现/锁内清理 web_sessions/web_turns 并留报告**；cell 编码/旧文件兼容、字段/知识快照及锁内前后复核、readiness 30 秒缓存/300 秒探针缓存、规范键跨存储核对及可选知识实际版本检查、登记库只读、锁异常、闭集错误类型及统一封装、CORS/频率/并发限制；停止挂载旧路由 | 完整 DTO/类型、保存读取、保留期清理、快照竞争、发布变更拒绝、资源与负载边界均有测试，不仅 happy path 可用 |
| P2 最小查询流程 | 模块路由、双 origin、源选择、新建/提问、答案、当前轮 SQL 与表格 | 选择两个源分别查询，结果与来源一致 |
| P3 多轮与历史 | checkpoint 续聊、会话列表、仅问题/答案恢复、线程轮换、history_saved 保存状态提示 | 刷新/切换不串源不串会话；历史响应与页面均无 SQL、结果引用或表格 |
| P4 回归和收尾 | MetaWeave 页面只读回归、新查询正常及失败验收、旧父图脚本独立对比，更新文档 | Vue 只调用新链，旧父图脚本可独立执行 |
| P5 清理 Streamlit | 功能确认覆盖后，删除内部 Streamlit 入口、专用依赖和失效说明 | 清理单独提交，后端与 Vue 不受影响。**2026-11-10 已实施**（入口、web 依赖与失效说明均已移除，见 [nl2sql/docs/3](../nl2sql/docs/3_NL2SQL旧前端清理设计.md)实施记录） |

多源已经确认，doc 4 的代码交付作为当前基线；进入 P2 真实查询验收前，还须确认目标知识库完成适用的迁移、重新发布 rel 及双源行为验收。迁移维护和 Web 开发可分别安排，代码单测通过不能代替目标图库的身份、约束和实际发布验证。本次不做登录，源绑定和结果归属校验仍须保留。

建议的实现位置：`frontend/src/modules/nl2sql/`、现有 AppSidebar/router、前端环境模板；Web 路由/DTO 放在 `nl2sql/src/api/`，运行装配放在可复用的 db_deepagent 模块，并调用现有 source_registry、配置和服务工厂。guest/展示元数据、文本轮次日志及登记快照的持久化按 §4.4 明确保留契约；需要数据库结构改动时单列迁移和验证，不在启动时悄悄生成新业务表。实体键、按源过滤和知识发布事务的详细设计继续以 doc 4 为准。

## 9. 验证与验收

### 9.1 有意义的自动化检查

- API 契约：成功/已形成业务终态为 HTTP 200/code=200；422 统一封装、409 忙碌/来源变更、404 跨会话与缺失结果统一响应、503 依赖故障。验证稳定 error_code、已有结果保留、非 JSON、空 data、partial/failed 与超时；客户端遇到 HTTP/code 不一致（含旧式 HTTP 200 + code 500）判为异常，不当成功。
- 完整响应：冻结 WebQueryResult 全字段、嵌套投影和空数组/nullable；不含 preview/rows/columns/preview_truncated。零行及正常表格均调用结果 GET，columns 精确为 name/pg_type，rows 为 CellV2；查询期编码错误为 HTTP 200/partial 或 failed，结果 GET 转码错误为 500。health、未知路由、405、HTTPException、未捕获异常均统一外层，trace_id 始终存在且与业务 request_id 分开，Allow/Retry-After 不丢失。
- 不可信终态投影：知识变化及终态复核不可用均只返回安全 answer，findings/evidence/口径/待澄清等数组为空，诊断中不泄露原答案/SQL/result_ref；文本日志与 HTTP 使用同一摘要。普通归档失败但知识已复核通过时保留有效证据。验证 error_code/kind 闭集及优先级，不靠中文匹配；Store 写入未发出且回读不存在、写入及回读均不明、回读安全摘要成功分别为 history_saved=false/null/true。
- 会话接口与分页：直接访问不在列表首页的 session_id 仍可加载详情；Web 空会话可见、CLI/测试会话不可见；limit/offset 边界、has_more/next_offset 和排序正确，读取全部所需 Store 页而非只排序首页。原始记录夹杂损坏/归属不符项时，offset 仅计算过滤排序后的有效 SessionInfo（含完整的失效会话），不得按 Store 原始偏移或扫描数推进；必要时扫描超过 limit+1 条，修复条目重新进入列表后按非快照规则刷新。模拟原始记录数/读取耗时达到 §4.2 上限时整次返回 503/history_unavailable，不返回部分页或错误 next_offset，底层读取结束后才释放受控线程槽。列表刷新不因迟到响应重复插入。
- 创建与分页竞争：创建超时不自动重发，重复点击被禁用；创建 pending 和 run pending 均提示“上一操作尚未确认”，不暗示创建会话已执行查询。模拟清除实际已提交而响应 503，刷新出现 active/continuable=true 空会话时可直接使用，不强制重建；无 session_id 时不按标题自动关联。记录手动重试可产生独立空会话的范围。turns 在并发新增/迟到收尾/清理导致 offset 变化时按约定重载，不按问答文本去重，重复问答仍可见。
- 创建写序与残留：断言先通用绑定、再 Web 创建 pending、写后复核后才清除，创建全程持同一锁；creation_id 为 create_ 加完整 UUID hex，不进入 Runner/轮次。先覆盖创建取锁异常 503/lock_backend_error、明确占用 409/session_busy，两者都不写通用/Web 记录，不进入 Runner，定位 ID 保留且锁连接回收。再分别注入两次写入和清除的明确失败/超时已提交/超时未提交及回读故障，验证 503/409/确认后 200、定位字段及列表可见性按 §4.4 处理，不自动重试或覆盖；maintenance 只报告创建 pending 及符合边界的疑似创建残留，不自动删除/解禁。
- 状态协调：重复提交、切换会话后的迟到响应、历史失败不清空旧消息。
- 历史边界：每轮只返回 question/answer；即使内部 Store/checkpoint 有 SQL 和 result_ref，历史 DTO 也不透出。历史页面无 SQL/表格展开按钮、不发结果读取请求，结果文件缺失不影响问答文本展示。
- 已知旧引用：同会话内保留的旧引用可读，刷新/后续查询不使其失效；源删除、停用或字段指纹变化不阻止文本/既有结果读取，但禁止不合规的新查询。模拟 registry 已删除或停用该源，并让 source_registry 调用一旦发生就使测试失败，断言 GET turns 和同会话已知 result_ref 的 GET results 仍返回 200。跨会话与不存在结果均 404/result_not_found，损坏返回明确错误；历史页面不获取引用或请求表格。随机引用不能替代归属检查。
- 结果编码：按 §4.5 覆盖 numeric[]、time、interval、UUID、bytea[]、嵌套数组、含保留键 JSON、NaN/Infinity、超过 2^53 整数、二进制占位及 v1 兼容；保存阶段成功、HTTP 无裸特殊数/精度损失、前端按 kind 展示。分别注入未知/非法 format_version、未知 kind、缺字段/多字段或值类型错误的 v2 信封，断言 500/result_encoding_error；原始文件/hash 损坏仍为 result_integrity_error，不猜测旧格式。验证零行、列序和 source_truncated；has_more 来自分页，不依赖 EvidenceItem.preview_truncated；不能把响应切页称为范围读取。
- v4 Web：身份归属、会话忙碌、资源释放、线程轮换不改变会话、result_ref 归属、分页上限。
- 问答保存：QA 已成功但轨迹失败导致 archived=false、仅 run_errors 成功导致 archived=true，均不推断文本保存。锁内 Web 日志成功、明确未写入、结果不明分别映射 history_saved=true/false/null；回读和元数据失败分别注入验证。首问有 40 个以上前导空格/换行再接正文时，标题取去首尾空白后的前 40 码点，历史 question 仍保留原文；answer 仅含空白时生成非空安全摘要。澄清/失败无 answer 时保存非空安全摘要，前置拒绝不入历史；HTTP 断开后仍正常收尾，不自动重放。历史读取异常返回错误，不伪装成空历史。
- 会话元数据：多轮及线程轮换不重置原始创建时间，不丢失 guest/标题/空会话标记/登记快照；列表按 last_active_at 排序，不把 active=False 当作空闲保证。
- 展示字段损坏：分别缺失/损坏 created_at、title、last_active_at、turn_count（包括 bool），以及 last_active_at 早于 created_at，列表跳过该条并记录问题、不拖垮正常页，排序/分页/has_more 按有效记录计算；直接详情及新查询均为 409/session_metadata_invalid，前端不引导重绑来源，历史文本仍可独立读取。校正可恢复标题/计数/活动时间等缓存，活动时间按时间值取不早于 created_at 的最大值，但不拿当前时间或 Runner 时间冒充原始 created_at；字段修复前不输出不完整 SessionInfo。依赖读取失败为 503，不伪装成空列表或跳过。
- 损坏会话的历史页面：已知 URL 的详情返回 session_metadata_invalid 后仍调用 turns 并展示问答，顶部说明展示信息损坏、输入禁用，无 SQL/结果请求，不伪造 SessionInfo/来源/时间。已打开会话的 query 返回该码时立即禁用输入，单次重读详情；仍为 409 则读取 turns 并转只读，恢复 200 则按 state/continuable 决定输入，均不自动重发 query，切换会话时迟到响应不得污染新会话。turns 失败不当作空历史，切换会话不带入旧内容；其他详情错误不触发该分支。created_at 无法恢复且列表不可发现时，本期永久只读、需新建会话，已知 URL/文本/维护定位仍保留；可修复字段校正后刷新可恢复正常加载，但不自动提问。
- 读取校验独立性：在核心 Web 归属完整、文本及结果正常的前提下，分别损坏 title/created_at/last_active_at/turn_count，断言详情为 409/session_metadata_invalid，而直接调用 turns 和已知 result_ref 的 results 均为 200、内容及分页正确；两个读取入口不得依赖 SessionInfo 的成功构造。再注入核心归属缺失、跨会话引用、结果损坏及依赖异常，确认原有拒绝仍生效。结果读取仅为 API 契约测试，历史页面仍不发 results 请求。
- 持久失效与收尾保护：创建/列表/详情字段完整；终态检测变化或无法复核后持久化 invalidated，重启/刷新/版本回退仍不续聊。模拟 Store 在执行前保护写入、终态失效写入和清除保护各处失败，确认未取得写入确认不执行，遗留 pending 在下一次取得锁后拒绝恢复 checkpoint；正在持锁请求仍按 session_busy，不将其误作遗留。元数据校正/maintenance 不覆盖失效状态，正常复核成功才清除 pending；历史/已取得旧引用仍可读。
- 旧 Web 登记投影：逐项删除 state/invalid_reason/invalidated_at/pending_run_id，列表与详情均返回 invalidated、continuable=false、session_binding_missing、invalidated_at=null；特别对比“仅缺 pending_run_id”和“显式 pending_run_id=null 且其余字段完整”，前者拒绝，后者不因该字段失效。GET 不写 Store、不隐藏仍可核对归属的旧会话，历史可读，新查询拒绝。不是 Web 登记或无法核对归属的记录不借此进入 guest 列表。
- 状态不变量：覆盖 active 携带非空失效原因/时间、invalidated 缺原因/时间、未知 state/reason、非法时间/pending 格式，列表/详情均按固定安全投影且查询在恢复 checkpoint 前拒绝；不能仅字段齐全就 continuable=true。正常 active/null pending 可续聊，正常 active/非空 pending 等待确认，正常 invalidated 永不续聊；元数据校正不得解禁，失效三字段在同次登记写入中更新。
- 疑似残留报告：短 CLI ID、长 ID 但有任一 checkpoint/threads/文本/归档/携带证据/结果/诊断痕迹均不标为疑似创建残留；只有符合 §4.4 全部条件的通用孤立绑定进入该分类，长 ID 仍不被宣称为已确认 Web 来源。跨页、读失败、取锁失败分别正确报告/跳过，dry-run/apply 不因该分类删除绑定或补登记，检查发生在清理历史之前。
- 元数据读成本：列表只读缓存元数据，不逐会话扫描全部 web_turns；注入文本成功/缓存更新失败，history_saved 仍 true，校正后计数与保留日志一致。校正有锁且受限，缓存陈旧范围有说明，不因文本过期重写原始标题/创建时间或倒退活动时间。
- 校正回退与重新可见：已跳过但 created_at 合法的损坏条目仍安排受限校正，成功后可重新列出；无保留文本时 turn_count=0，标题缺失时为“新对话”，last_active_at 缺失/非法时为 created_at，合法既有标题/活动时间保留。超长会话按需校正达到 10000 条/5 秒预算或发现坏记录后不更新缓存和 metadata_reconciled_at，设置 300 秒失败退避；连续 list/detail 不重复扫描，maintenance 可重试，成功后清除退避。校正由独立有界读取执行器异步处理，HTTP 请求不等待；任务去重、队列满、取锁失败及日志读取失败均有界报告，不将读失败当零条覆盖、不解禁或清除 pending。原始 created_at 缺失只报告。正常新建空会话首次列表/详情因 metadata_reconciled_at 缺失安排全量校正；成功写入后，后续读取不因同一缺失条件重复调度，失败则按退避规则重试。
- list/detail 触发校正的负载边界：超长会话在独立校正执行器内持会话锁，达到同一 10000 条/5 秒预算即停止，不更新 title/turn_count/last_active_at 或 metadata_reconciled_at，按 §4.4 设置 retry_after 并及时释放锁；随后连续列表/详情请求不再次排队或扫描，maintenance 可在后续周期重试。HTTP 响应不等待扫描，慢 Store 的底层读取结束后才释放受控执行槽；队列拒绝、执行器拒绝、锁后端异常或失败退避标记写入失败时，进程内抑制最多 300 秒，持久化时间值变化可提前解除，使用单调时钟计时，不依赖 maintenance 清理 Web 进程内存。模拟锁明确忙碌则不设置退避，查询结束后可以重新触发校正。
- retry_after 边界：缺失、字符串、无时区时间、未知类型及超过当前时刻 1 小时的未来值均按无退避处理并记录安全原因；其他 SessionInfo 字段有效时 GET sessions/detail 仍为 200。若同时满足缺字段或校正到期条件，仍可立即调度校正，失败后写入合法退避时间。模拟队列持续满、执行器拒绝、锁后端故障时连续请求不会无界尝试入队/扫描；分别覆盖三类原因及退避标记写入失败后的 300 秒单调时钟抑制和到期解除。模拟另一进程写入或清除 retry_after/推进 metadata_reconciled_at，Web 下一次读取发现持久化时间值改变后解除抑制并按新值处理。锁明确忙碌不设退避，查询结束后可重新触发。用可注入 fake clock 固定同一 now 构造所有时间边界；maintenance 在有效标记未来时跳过该会话、不改标记；now+1h 仍是有效退避，now+1h+1s 按无退避处理；到期后失败固定设为失败时刻后 300 秒，连续尝试不提前重试、不累计延长也不缩短有效未来时间。创建、首次校正、陈旧边界/重启及失败重试另按下列独立用例覆盖。
- metadata_reconciled_at 边界：未来 10 年、普通字符串、无时区时间及未知类型均作为缓存未命中/已过期处理，不使 GET sessions/detail 返回 500 或 session_metadata_invalid；按需校正可调度，成功后写当前带时区时间。抑制项比较该字段时复用相同解析规则，非法值按缺失处理。精确边界用例使用可注入 fake clock 固定同一 now 构造时间值，验证 now+60 秒视为新鲜、now+61 秒触发校正；now-300 秒（恰好 300 秒）仍新鲜、now-301 秒触发校正，避免真实时钟漂移和比较符号实现歧义。
- 创建与首次全量校正：创建 Web 登记及 pending 清除后 metadata_reconciled_at 初始不写；首次列表/详情安排一次全量校正，成功后不因同一缺失重复调度，失败按退避规则重试。校正执行时若无文本则写空会话基线；若创建后先完成一轮问答再首次列表/详情，须按校正执行时读取的日志得到 turn_count=1、从该问题派生标题、last_active_at 不早于 created_at，并写入 metadata_reconciled_at，不得跳过或写成 0。另在首次校正成功后完成一轮问答，验证文本收尾只更新 turn_count/title/last_active_at，保留且不刷新 metadata_reconciled_at。创建 pending 清除、状态失效写入和普通状态写入也须保留未修改的 metadata_reconciled_at 与 retry_after；只有全量校正成功才能更新前者并清除后者。
- 300 秒边界与重启：使用可注入 fake clock 固定时刻；恰好 300 秒仍新鲜，301 秒触发校正。校正后 300 秒内重启 Web 或由另一进程读取，不重复调度校正（空会话不会重复做空基线校正）；超过 300 秒后按陈旧规则触发一次全量校正。
- 首次校正失败与重试：模拟首次全量校正写入失败（可分别使用空会话和已有文本会话），重启后按缺失时间戳重试；若存在有效 retry_after，则退避期内不提前重试、到期后重试；确认失败不会使会话永久跳过。
- Web 历史维护：覆盖仅 web_turns、仅 web_sessions、无 QA/run_errors、缺通用 sessions、跨 Store 页发现等情况；同一锁内重查与清理，取锁失败全会话跳过，dry-run 不删除/不改缓存，apply 仅删到期文本并报告。分别注入 question/answer 空白与非字符串、request_id 缺失/类型错误/与 Store key 不符、saved_at 缺失/类型错误/非法时间的单条 web_turn：GET turns 整页 500/history_record_invalid、无部分页，maintenance 报告为需人工核验/处理并跳过该会话本轮文本删除和缓存校正，不能把坏 saved_at 认作过期或用剩余日志覆盖 turn_count。另验证 run_id/终态缺失、类型错误或未知值不影响 GET turns、turn_count 校正、maintenance retention 和 history_saved；这四处对有效性的结论必须一致。扫描超限、坏记录和读取失败在 maintenance 中均视作校正失败：仍持锁且能写登记时 retry_after=失败时刻+300 秒，下一次 maintenance 在到期前不重扫、不清理；写标记失败时写入报告且本次只尝试一次，不循环。读取失败保留、正常空会话登记保留、缓存校正与结果引用保护同时成立。history_saved=true 的旧文本超过保留期后可能不可读，不承诺永久历史。
- 长轮次扫描边界：模拟 GET turns 达到条数/时间上限，整次返回 503/history_unavailable、不返回部分页；另模拟扫描中先发现坏记录、随后触及时间上限，稳定返回 500/history_record_invalid。确认受控读取线程在底层调用真正结束后才释放，不让超时遗留读取占满或绕过并发上限；正常上限内多页历史仍按稳定顺序返回。maintenance 达到同样扫描上限时报告超限、跳过该会话本轮删除/校正并释放锁，其他会话继续处理。
- 隐藏会话维护报告：原始创建时间缺失/非法的登记单列“展示字段无法自动恢复、已从列表隐藏”，带 session_id/问题字段/安全原因；可修复项与依赖读取失败不误归此类。dry-run/apply 报告均可用于定位，不因分类新增删除、解禁或 API 字段。
- 结果清理：生产入口只使用 maintenance；仍被保留 checkpoint、携带证据或归档引用的结果不被按时间误删，引用读取失败时保持保护行为；不从 Web 直接调用 purge_expired。普通查询默认 10000 行预算在管线生效，不假设 ResultStore.save 自动截断。
- 忙碌契约：同会话并发提交时，竞争失败的一方返回结构化 session_busy、HTTP 409 及定位 ID，未调用 Agent、未覆盖活动会话状态；更换中文文案不影响前端判断，页面保留输入且不自动重试。锁数据库连接/查询异常返回服务不可用而非 session_busy，连接建立后 execute 抛错也须关闭局部连接；锁释放后手动提交可以正常运行。
- ID 入口：Web 生成完整 UUID hex 的 session_id/request_id，客户端提交 request_id 返回 422；两类 ID 完整满足 1–64 字符约束。API、直接 build_db_agent/build_session_dir、直接 Runner 调用分别注入非法/超长 ID，确认没有目录、锁或记录副作用；重复 request_id（含忙碌路径）不覆盖已有文件/Store，返回或记录稳定冲突。
- 每源只读检查：源库全局默认读写时，当前服务的连接 options 与运行事务仍可只读并通过检查；会话 default 关闭但实际只读事务有效时只告警，实际 transaction_read_only 关闭则失败。验证本事务超时生效、结束后连接为 IDLE，并确认检查未修改源库全局设置、未影响其他源或 checkpoint/归档写入。写入拒绝行为如需额外验证，仅在隔离测试环境执行。
- 数据源：停用/删除/不支持类型；登记 db_name 含点号或 schema 含空格/连字符时 readiness 提前拒绝并显示原因；selected_schemas 为 null/空时不创建服务。两个同名库不混查询，sslmode 正确；只按字段指纹使旧会话失效，改显示名/描述或单改 updated_at 不失效，schema 顺序变化不被忽略。最近活动字段不标作成功时间。
- 公共装配：环境 DB_NAME 与 YAML 的 orders 默认值不同、登记源另有 db_name 时，先覆盖再完成最终三键校验，能装配正确目标；缺失或非法登记仍拒绝，不修改进程环境、不回退默认源。
- 来源传递与旧会话：创建即保存 source_id；首次提问、续聊、刷新恢复和线程轮换保持绑定；请求省略/伪造/错配来源时不执行 Agent，已有会话缺少绑定时拒绝续聊；sessions 记录缺失但待恢复 checkpoint 有状态时同样拒绝，不以新源接管。不存在的 Web 会话不隐式创建；模型工具参数没有 source_id，不能覆盖服务绑定。
- 连接指纹边界：两个短 ID 登记源即使物理目标、角色和 search_path 相同而 datasource_id 相等，也不因指纹相等允许跨源或跨会话复用 checkpoint、证据和结果。
- 每轮资源：同源和不同源并发均使用独立执行服务及 Milvus alias；关闭 A 实例不影响 B；部分构造失败释放已建资源，执行及归档结束后释放全部本轮资源，HTTP 断开但后台未结束时不会提前关闭；历史/结果读取不装配 Agent 与整套检索服务。
- 键空间与 SQL：复用 doc 4 样例验证含连字符、数字开头和内部空格的 db 前缀、两段/三段逻辑键一致性、实际名称大小写及非法键拒绝；知识 db 前缀/source_id/entity_id 不进入 SQL 表限定段。完整校验管线覆盖非 public schema、裸名解析、未引用列名/别名/CTE 名的大小写折叠、双引号名称保留、CTE 作用域及系统目录同名优先级；Web 不直接绕过 validator 调用底层执行或仅以守卫替代范围检查。
- 双源知识隔离：同名 db/schema/table/column 在 A/B 源并存，六个 Milvus 入口及精确回查只命中本源；仅 B 存在的表在 A 不可见，A 不返回 B 的寻路结果。Neo4j 按源替换、数量核验失败回滚、不同源并发发布与其他源保留按 doc 4 §4.6–§4.7 验收，记录基础链路和 Web 接线各自结果。
- 知识就绪：A 有知识、B 为空时 B 未就绪；table_schema 仅有列记录不算就绪，首版未发布 rel 的源不能创建会话，JOIN_ON=0 合法，维度/示例数据为空给 warn 但集合缺失或依赖异常不伪装为空。同源 table_schema 与 rel 来自不同工作区/发布运行合法（doc 6.2 §10.2），不要求两步骤版本相等；但表集合不符、Milvus 列或父表在图中缺失、实际下游版本与各自源级最近成功记录不一致同样拒绝；空描述列跳过/include_columns=false 不误判为列集合不一致。完整枚举全部分页，核对各自 publish_run_id；200/422/503 等 data 与 compatible 的位置/nullable 明确，不执行问答模型。
- 跨存储键：严格纯函数入口两段补同源 db 前缀、三段校验前缀，列键拆分与 parent_id/HAS_COLUMN 对应一致；覆盖 my-orders、db 前缀合法内部空格、Sales/sales 并存。仅登记 public 时 PUBLIC.orders 拒绝；public . orders、表键首尾空白、列键/parent_id 首尾空白均拒绝，不 strip/lower 修正；模型输入的原有宽松归一行为不变。非法 schema、错误父表/重复规范键拒绝；集合归一后相等但图 entity_id 仍为旧两段形式也不就绪，不以 full_name 绕过真实查询键。
- 可选实际版本：无源级最近成功记录/无行、有记录但零行可 warn；无记录/旧行、不同或多个 publish_run_id、缺批次字段均拒绝，不能先按当前版本过滤再漏掉残留；跨全部分页核验。业务源连接/检查异常为 503，确定目标/只读/事务超时设置不符为 200 ready=false，创建/提问转 409/source_not_ready；不将上述情况误显示为单纯缺知识。
- 版本与维护边界：创建即存知识 snapshot，版本相同时才能恢复 checkpoint；table_schema/rel/可选知识的源级最近成功发布记录或对应 (workspace_id,publish_run_id) 快照变化、必需发布记录缺失或 release 摘要变化，都拒绝旧会话，缺 snapshot 也不自动接管；历史/旧引用仍读得出。模拟入口检查后、取得锁前变化，以及执行中意外变化，分别验证 409 拒绝或 HTTP 200 failed 安全摘要/禁止续聊，不把旧证据混入新会话。模拟创建完整检查与保存之间登记/发布记录改变，确认没有“旧目标检查、新指纹成功登记”；写后复核失败的残留不返回成功。正式发布按排空维护执行，前后指纹检查不作为在线发布原子性证明。
- readiness 成本：同源同版本连续提问在 30 秒 TTL 内只重读登记/发布记录/release 摘要，不重复知识全量计数/表列枚举；到期、指纹/版本/后端配置变化重新完整检查，同键并发合并，失败不复用旧成功。缓存命中后 build_services 不重复相同完整检查，但实际业务连接仍验证目标/只读；轻量复核异常 fail closed。
- Embedding 探针：同配置连续提问或并发 readiness 命中一次成功探针；TTL 到期、release/模型/目标变化重查，失败不复用旧成功。同源/异源仍分别装配执行服务，不缓存服务实例，不在每题无条件发探针请求。
- 负载与本机边界：用慢服务替身占满执行名额，后续 query 立即 429，health/列表不被长查询线程耗尽；名额在真实收尾后归还，关闭/装配失败不泄漏。验证频率限制、非白名单 Origin 被业务前拒绝、JSON 类型要求；不将 CORS 当登录鉴权。
- 超时：覆盖模型调用在 300 秒前放行、360 秒后完成并正常返回的情况，以及超过客户端等待时长仍不自动重发查询；模拟时钟和慢服务即可，不为此强制等待真实数分钟。
- 路由与启动：旧 `/api/v1/query`、旧历史路由不可调用，启动/调用新路由均不导入或懒编译旧父图；旧脚本仍独立可用。新 lifespan 释放自己的资源，没配置 NL2SQL origin 的 MetaWeave 构建/页面仍通过，进入查询页才提示配置缺失。答案/历史用纯文本，不执行 HTML。
- 发布排空：按已确认的 D8，验证 Uvicorn 停接单/等待 ASGI、随后 lifespan 等待专用执行线程并关闭资源的顺序；断连或 ASGI 被取消仍不提前释放执行依赖，不以 8008 停听证明 CLI/脚本已停止。维护记录覆盖所有相关消费者，未确认排空不得发布；发布完成且 readiness 通过后才恢复查询。
- 前端已有 [node:test 用例](../frontend/src/utils/relDisplayFilter.test.ts)，新增纯逻辑测试沿用 node:test，按实际 Node 版本选择可运行 TypeScript 的现有方式，不要求引入测试框架。package.json 尚无 npm test；运行现有类型检查/构建及必要的 node:test。后端运行相关单元/契约测试，本次文档编辑不声称代码检查已执行。

### 9.2 浏览器与实际联调

按 [启动指南](1_服务启动指南.md)分别启动两个后端及统一前端，检查 MetaWeave 页面不退化；确认请求发往正确端口且无新 CORS 问题。

**验收环境是前置条件，在 P0 核对，不能留到 P4 才准备。** 平台库和 Milvus 可用独立测试 database；当前 Neo4j Community 不能在同实例创建隔离业务 database，真实双源写入、按源替换、并发和回滚测试须准备独立 Neo4j 实例（另一进程/主机，或具备条件时的容器），使用独立数据目录及端口。正式实例在这类开发验收中仅做只读核验，不用写入测试源代替隔离；fake 单测可先验证逻辑，但必须标注真实写入场景未验收。此条不取消已授权维护窗口内正式迁移/重新发布 rel 的独立流程。

在隔离环境登记两个不同短 ID 的源，准备同名 db/schema/table/column、可区分查询结果及仅 B 存在的表/关系、可发布作业产物和有效源级最近成功发布记录。回滚测试保留旧子图并安排可控失败注入；同一源重复查询不能代替双源验收。独立实例不可用时列出阻塞场景，不为凑验收通过改写正式图库。

使用这两个经确认的登记源，各验证查询、续聊和问答文本恢复，再验证空数据、失败、慢请求、来源错配拒绝、切换来源/会话和刷新。检查每次提问都携带并校验 source_id，多轮使用 checkpoint 而非前端重传 SQL；当前新查询可展示 SQL/表格，刷新恢复后仅展示问题和答案，不返回或按需加载历史 SQL/表格。此类验收会调用模型、读取业务数据并可能写入 checkpoint/问答历史，应按选定环境执行；文档编写或构建检查不替代实际验收。

若浏览器或外部依赖不可用，分别记录静态检查、构建、接口和浏览器的结果，不把 HTTP 200 或模块存在写成整体验收通过。

### 9.3 Streamlit 删除条件

- 约定范围的功能全部覆盖，当前查询、多轮和历史验收通过。
- 新旧历史分开，旧历史不混入新会话；文本恢复与当前轮详情展示的范围已说明。
- 用户确认 Vue 可以替代迁移期对照入口，再单独清理。
- 删除 `web` extra 前检查 requests 等依赖是否被其他代码使用，不能按依赖组名称直接删除整组。

## 10. 本轮同步与后续讨论

本轮依据 doc 4 已交付代码同步了 source_id 创建/提问契约、每轮独立运行装配、会话来源保留与旧会话拒绝、已有源解析及按源配置、Milvus 六入口过滤、Neo4j 实体键发布/查询、键空间与 SQL 归一、按源 doctor 统计和双源验收准备。上述能力作为现有基线，后续开发复用；doc 4 中编写于实现前的“尚未开发”说明不能代替当前代码状态，维护和验收以各自执行记录为准。

已确认的产品范围保持：D0 仅新链、D1 平台选源、D2 guest、D3 历史仅问答、D6 后端读取登记并解密，以及 Q2 保留有效旧结果引用的读取能力。D8 于 2026-10-18 确认：发布知识期间先不使用 NL2SQL，发布完成后再用；首版按 §5.5.1 操作流程执行。双后端端口、480 秒等待估算和 doc 3 的结果存储边界保持原约定。

前轮已补齐 archived/history_saved、孤立 checkpoint、ID/分页/Store 命名空间、cell 编码、登记指纹、D6 部分交付状态、本机执行边界及 doc 4 状态勘误。本轮进一步明确：首版同步返回、知识版本变化要求新会话、发布与查询错开执行；rel 必需是首版成套知识范围而非构造函数强制。readiness 按源级最近成功发布/对象集合核对，区分 30 秒完整检查缓存与 300 秒模型探针缓存；创建使用同一登记/知识快照并复核竞争。固定 WebQueryResult 和结果列形状，不在查询响应塞预览；执行期编码失败与结果 GET 错误分开映射；所有应用异常和 health 统一封装并使用独立 trace_id。Web 文本 maintenance 扩展、可修复元数据缓存、创建不自动重试及 turns 分页边界已落实到 P1 和验收项。

本轮复审补齐 SessionInfo/登记中的持久失效状态与最小 pending 收尾保护、不可信终态的后端强制清空投影、跨存储规范键和实际可命中实体键检查、可选知识实际 publish_run_id 核对、业务源检查 HTTP 分类与闭集错误类型，并明确 Store 故障下安全摘要和 history_saved 的独立语义。对应 P1/UI/测试已同步，不以修改文档代替代码修复。

本次继续明确创建写序和 create_ 操作标识、写入结果不明的 HTTP/残留可见性及不自动删除政策；readiness 改用严格纯函数校验持久化身份，保留模型输入归一的容错行为；旧 Web 登记固定安全投影且 GET 不写入。维护流程修正为 Uvicorn 停接单后进入 lifespan 的实际顺序，并纳入 CLI/脚本等所有相关消费者，未增加新的维护锁或任务系统。

上述是已写入本文、尚待代码实施的契约，不能当作当前代码已修复。**D4/D7 按本轮要求作出设计决定并定稿，D8 已获用户确认，三项均不再作为待决策项**。D4/D7 的定稿记录不追溯表述为此前已获用户单独确认。本文审核及独立验收环境准备核对完成后，按 §8 进入 P1，实现公共装配、正常关闭/排空、恢复就绪检查和 Web 能力，不要求为 D8 新增在线发布互锁。不新增登录系统、任务平台、SQL 会话表、缓存服务或前端组件框架，doc 3 的存储介质改造仍独立。

最近的边界复审进一步固定：标题从去首尾空白后的问题派生；损坏的 Web 文本轮次使整次历史读取明确失败，并阻止该会话本轮清理/校正；历史读取不依赖登记源当前状态；已打开页面收到 session_metadata_invalid 时转入只读检查；原始创建时间无法恢复的会话本期只读、新提问须新建。展示时间先后、结果编码版本、会话列表及轮次读取上限也已纳入契约和验收。GET turns 的分页依据完整有效日志，SessionInfo.turn_count 仍使用独立缓存校正规则；web_turn 的最小有效字段与审计字段边界已区分。

本轮最后收口 web_turn 的共享有效性谓词及其在历史读取、计数校正、maintenance 和 history_saved 中的一致用法；审计字段 run_id/终态不影响文本有效性。GET turns 扫描发现坏记录时优先报 history_record_invalid，只有未发现坏记录而先达到扫描上限才报 history_unavailable；maintenance 与 list/detail 按需校正采用相同扫描上限，超限/坏记录/读取失败时设置 300 秒退避并跳过该会话本轮处理。按需校正运行于独立有界读取执行器；进程内抑制可由持久化时间变化解除且自身最长 300 秒，使用单调时钟。metadata_reconciled_at 与 retry_after 使用各自带时区解析规则：前者未来偏差容忍至 60 秒（含边界）、严格超过即按缓存未命中，后者未来有效窗口为 1 小时、超出按无退避处理；登记的其他写入保留未修改的 metadata_reconciled_at 与 retry_after，只有全量校正成功更新前者并清除后者，maintenance 在退避到期后重试。创建 Web 登记时不初始化 metadata_reconciled_at；首次列表/详情安排全量校正，校正执行时按保留日志建立空会话基线或重算缓存，成功后不因同一缺失重复调度，失败按退避规则重试；文本收尾及状态写入保留该时间戳，不刷新它。

代码交付、正式知识库迁移/重新发布 rel、隔离环境双源验收与 Vue 浏览器联调分别记录。本文不将其中任一项通过视为其余项目完成。本次只修改本文，未修改 doc 4、业务代码、环境配置、依赖或数据库，未执行迁移、发布或 Git 暂存/提交。
