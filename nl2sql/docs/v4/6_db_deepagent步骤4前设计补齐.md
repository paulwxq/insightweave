# db_deepagent 步骤 4 前设计补齐（待评审）

> 状态：**评审通过（2026-10-15，第四轮收口；v4 含三轮评审意见与收口调整）**。本文补齐 doc 5（`5_db_deepagent详细设计.md`）§21 要求在步骤 4 前完成的三项设计：
> ① 历史结果作为最终答案引用（§21 表第 1 行，标注"尚未完成设计"）；
> ② 完整装配验收安排（§21 补齐门禁第 2 项）；
> ③ CLI JSON 输出隔离（§21 补齐门禁第 3 项）。
> 评审通过后按文末"并入计划"折回 doc 5，再开始步骤 4 实施。
>
> v2 修订（第一轮 8 项）：装配清单计入文件工具并改为代码常量；原始来源改由 ResultMeta 补齐、区分"原结果生成时间"；沿用登记与真实执行的统计/归档/提示词解耦；重复沿用幂等（reducer 层去重）；显式复用保留期检查（新增 `result_expired`，登记不续期）；JSON 隔离覆盖直接 print 并定义错误协议；摘要容量按实际请求分别测量；示例区分"引用"与"重新分析"。
>
> v3 修订（第二轮 5 项）：保留期配置路径更正为 `thread.result_retention_days` 并明确存储时钟／UTC／边界；成功归档条件精确到"被接受的最终答案＋有效 E/A 引用"，qa 状态覆盖 failed；链首解析不回查原线程（以当前副本＋元数据完成）；幂等 reducer 区分登记调用字段与事实字段、同 ID 异事实报一致性错误；JSON 参数错误先轻量识别模式再完整解析（自定义 argparse 错误出口）。
>
> v4 修订（第三轮 4 项）：执行／分析记录分别定义去重比较字段；qa 直接保存 Runner 真实运行状态（不再另设枚举，不改写状态）；JSON 预扫描尊重 `--` 分隔符、帮助退出不进错误出口；并入计划补 §18 测试清单更新。
>
> 基线：doc 5 三轮评审通过版（2258 行）；代码基线 commit be9288c（§19 步骤 3 完成）。

## 1. 历史结果作为最终答案引用（执行证据沿用）

### 1.1 目标与边界

多轮对话中，Agent 应能把**历史执行／分析结果作为证据引用**（如"引用刚才的结果说明上一问的结论、口径与截断情况"），不要求重新查询。边界：

- 本文只补"证据可引用"（引用型）。**重新计算不在沿用能力内**："刚才那个结果再按 X 分组"需要 `analyze_result`（步骤 5）或新查询——沿用登记本身不产生新数据。
- 允许引用截断结果 ≠ 可以据已保存样本推断完整总体：沿用记录照带 `source_truncated`，涉及总体结论时模型仍须按 §12.1 第 7 点走下推或如实说明。
- 沿用是**受控登记**：来源只有两类服务端持有物（本线程账本、`carried_evidence`），不扩大跨会话访问（与 §7.12 时间／维度证据同界）。
- 探针与下推执行记录仅作轨迹，不可沿用（与 §7.11 引用白名单一致）。

### 1.2 `reuse_evidence` 扩展（§7.12 增补）

**参数**：`evidence_ids` 接受的 ID 类型从 2 类扩为 4 类：`T-`（时间）、`D-`（维度）、`E-`（执行记录，仅 `purpose="query"`）、`A-`（分析记录）。同批输入先按 ID 去重再处理。

**可沿用来源**：

1. 本线程账本 `db_executions`／`db_analyses` 中 `run_id` 不等于当前运行的记录；执行记录仅接受 `purpose="query"`（探针／下推返回 `invalid_argument`，提示其不可作证据）。**沿用登记本身（`reused_from` 非空的记录）也可被再次沿用，解析到链首原始证据处理**（见"链首解析"）。
2. `runtime.context.carried_evidence`：`CarriedEvidence.kind` 从 `Literal["time", "dim"]` 扩为 `Literal["time", "dim", "execution", "analysis"]`，`record` 相应接受原 `ExecutionRecord`／`AnalysisRecord`（§13.4 轮换清单由 Runner 组装，见 1.5）。

**链首解析（不回查原线程）**：沿用源记录可能本身是沿用登记。"解析链首"只是**身份解析**——沿 `reused_from` 链取最初的原始 `evidence_id` 与 `EvidenceOrigin`；**不要求原线程 checkpoint 或原账本仍存在**。登记使用**当前服务端持有的完整记录副本**（本线程账本中的记录，或 `carried_evidence` 中的副本）＋该副本已有的 `reused_from` ＋原结果元数据（`ResultMeta`）完成。`EvidenceOrigin.evidence_id` 来自链首解析结果（原记录 ID 或既有 `reused_from.evidence_id`——`ResultMeta` 没有该字段，不从元数据取）；其余来源字段（thread_id／request_id／run_id／datasource_id）以 `ResultMeta` 为准。

**检查顺序**（任一失败整批拒绝、不登记任何一条，沿用 §7.12 的批语义）：

1. 逐 ID 查来源：找不到 → `evidence_not_found`（列出 ID）。
2. `prior_result` 维度证据、探针／下推执行记录 → `invalid_argument`（维持 §7.12 与 §7.11 的既有口径）。
3. **可读性检查**（执行／分析证据特有）。`ResultStore.load_meta` 维持现状（只查文件与会话，不做保留期判断——保留期判断属于沿用服务，见第 4 步）：
   - `result_ref` 目录不存在 → `result_not_found`（已被维护任务删除或从未存在）；
   - 会话不符 → `result_access_denied`（保持现有语义，不把跨会话归成"不存在"）；
   - `read_rows` 触发 `rows_sha256` 校验失败 → `result_integrity_error`，hint：结果文件损坏，重新查询。
   - 分析记录只核验 `output_result_ref`；其 `input_result_refs` 允许已不可读——分析在原运行已经发生，输出结果完整即可作证据。
4. **复用保留期检查（显式，不依赖 load_meta）**：
   - 配置路径为**`thread.result_retention_days`**（现有配置；`maintenance` 段只有两个超时字段，无保留期）。
   - 判断用**存储生命周期的当前时间**（`datetime.now()` 基准的注入时钟），**不得使用** `ctx.now`／`--fixed-now` 业务分析时钟——固定业务日期会让超龄结果恒通过检查。
   - 比较双方为带时区时间，统一转 UTC 后计算 `age = now_utc - created_at_utc`；**`age >= timedelta(days=result_retention_days)` 即过期**（闭边界）。
   - 过期 → 新增错误码 `result_expired`（入 §7.1 错误表，`retryable=true`，hint：结果已过复用保留期，重新查询）。**沿用登记不续期**：新记录引用原 `result_ref`，后续沿用仍按同一原结果生成时间判定。
   - 测试通过**注入存储时钟**验证边界（恰好到期／过期一天／未到期），不改变业务分析时钟。
5. **数据源检查（以元数据为准）**：`ResultMeta.datasource_id` 必须等于 `runtime.context.datasource_id`，否则 `evidence_datasource_mismatch`（列出 ID 与原数据源）。执行／分析证据**不允许跨数据源沿用**——数据事实来自原库，与维度证据同规则；只有时间证据保留 §7.12 的跨数据源例外。执行记录的账本副本带 `datasource_id`，与元数据不一致时按本错误拒绝；**`AnalysisRecord` 没有 `datasource_id` 字段，分析记录一律以 `output_result_ref` 的元数据为准**，不做记录级比对。

**登记形态**：沿用登记为本运行的新记录，**事实字段全保留、不改写**：

- 执行证据：`db_executions` 追加一条新 `ExecutionRecord`——新 ID 与 `tool_call_id`／`request_id`／`run_id` 为当前运行，`purpose` 保持 `"query"`，`sql_template`／`executed_sql`／`executed_sql_sha256`／`result_ref`／`columns`／`saved_row_count`／`source_truncated`／`elapsed_ms`／`warnings` 照抄**当前持有的完整副本**。
- 分析证据：`db_analyses` 同构（新 `A-` ID、当前运行字段、原 `operation`／`input_result_refs`／`output_result_ref`／`executed_sql` 等照抄副本）。
- **Schema 增量**（`ExecutionRecord`／`AnalysisRecord` 各加两个 `NotRequired` 字段）：
  - `reused_from: NotRequired[EvidenceOrigin]`——沿用时必填；为链首解析所得 `EvidenceOrigin`（连续沿用保留链首，不改为上一运行的登记）；
  - `result_generated_at: NotRequired[str]`（ISO 时间，原结果生成时间）：沿用时取原 `ResultMeta.created_at`；**本运行正常执行的记录在步骤 4 落账时同样填写**（值即本次 `ResultMeta.created_at`；它是结果生成时间，不是 SQL 开始执行时间，统计与展示按此语义使用）。
- `source_truncated=true` 的历史结果允许沿用：它本就是合法的答案依据；截断标记随记录带入 `EvidenceItem`，模型须在 `limitations` 说明（提示语随工具返回给出）。

**幂等规则**（重复提交与并行调用都不得产生重复或冲突登记）：

- 新 ID 确定性：`"E-" + sha256(run_id + ":" + 链首原始 evidence_id)[:8]`（分析为 `"A-"` 前缀同构）——同一运行内重复沿用同一原始证据，返回同一个登记 ID。
- **同一登记 ID 的记录并非所有字段都相同**：每次调用的 `tool_call_id` 不同。去重时保留**首次登记**的 `tool_call_id`；后续重复调用的轨迹由既有工具调用审计（工具调用轨迹／RunStats）单独记录，不覆盖、不丢弃。
- **账本合并层去重**：`db_executions`／`db_analyses` 的状态注解从 `operator.add` 改为自定义 reducer（与 `merge_by_key` 同处定义），按 `evidence_id` 比较，**两类记录分别定义事实比较字段**（分析记录没有 `result_ref`／`executed_sql_sha256`／`columns`，混用清单会 KeyError 或误判重复）：
  - 执行记录（`db_executions`）：`reused_from`（原始来源）、`result_ref`、`executed_sql_sha256`、`saved_row_count`、`source_truncated`、`columns`、`result_generated_at`；
  - 分析记录（`db_analyses`）：`reused_from`、`output_result_ref`、`operation`（操作与参数）、`input_result_refs`、`executed_sql`（全文比对）、`saved_row_count`、`source_truncated`、`result_generated_at`；
  - 两类都**排除** `tool_call_id` 等登记调用字段（允许不同）；其余字段（`run_id`／`request_id` 等）在同 ID 下本就相同。
  - 同 ID、同事实 → 去重（保留首条）；
  - **同 ID、不同事实 → reducer 抛出一致性错误**（LangGraph reducer 异常，运行按 runtime 失败处理），不得静默保留第一条——既支持并行幂等，也不掩盖异常冲突。

**返回**：新旧 ID 对照（`kind` 增加 `execution`／`analysis`）、每条的 `result_ref`／行数／截断标记／原结果生成时间；提示"沿用历史结果须在回答中写明原结果生成时间与数据源"。

### 1.3 `submit_final_answer` 引用检查

§7.11 检查 3 规则不变，白名单措辞同步："本运行 `db_executions`（`purpose="query"`，含沿用登记）或 `db_analyses`（含沿用登记）中的 `evidence_id`"。沿用记录天然在本运行账本内，新 `evidence_id` 通过校验。

### 1.4 Runner evidence 组装与统计口径

`EvidenceItem` 增加两个字段：

- `reused_from: EvidenceOrigin | None = None`——本运行执行的记录为 `None`，沿用登记照抄记录的 `reused_from`（链首）；
- `result_generated_at: str`——**所有记录都填**（协议中原执行时间由此进入 CLI），沿用记录为原结果生成时间。

`sql`／`row_count`／`result_ref`／两种截断标记等事实字段照抄记录（与 §14.3 现规则一致）。CLI 证据表对 `reused_from` 非空的行标注"沿用（原 run_id、原结果生成时间）"。

**统计与归档解耦**（沿用登记不冒充本轮实际执行）：

- **本轮有效回答证据**：允许包含受控沿用（`submit_final_answer` 引用、`EvidenceItem`、状态判定中的"有成功证据"均认沿用记录）。
- **本轮实际执行次数**：只统计真实执行（`reused_from` 为空）——预算已天然只计真实预占（沿用不调 `run_readonly`，不占 `db_execute`）；任何执行耗时汇总**不得计入**沿用记录的 `elapsed_ms`（它是历史事实的转录）。
- **成功问答归档（§13.3 增补）**——"登记成功"不等于"回答成功"，`reuse_evidence` 也能登记 T／D，而它们不能单独支撑业务数值结论；E／A 登记后模型也可能未提交或提交被拒。因此：
  - **qa 写入条件**（满足其一）：
    a. 存在成功真实执行（`purpose` 为 `query` 或 `pushdown`、`reused_from` 为空）；或
    b. **最终答案已被接受**（`db_final_answers` 有本 `run_id` 记录）**且**评估状态为 `completed`／`partial` **且**其 `findings` 引用了有效登记的 E／A 沿用证据。
  - **纯沿用请求只有满足 b 才进 qa**：仅沿用时间／维度证据、没有最终答案、或答案引用无效（引用不存在/被移除后无剩余有效引用），都不构成成功问答，按 run_errors 处理。
  - **qa.status 直接保存 Runner 判定的真实运行状态**（与 `DbAgentResult.status` 同一七值枚举：`completed`／`partial`／`needs_clarification`／`no_data`／`failed`／`budget_exhausted`／`persistence_error`），**不另维护 qa 专用枚举，也不为凑枚举改写状态**——SQL 成功后预算耗尽（满足条件 a、状态 `budget_exhausted`）、查询成功但结果为空（`no_data`）、需进一步澄清（`needs_clarification`）都按真实状态入 qa。`persistence_error` 仍按 §14.3 P3 原规则处理（归档本身失败，不写 qa）。
  - **run_errors 写入条件**：不满足 a 且不满足 b（含纯 T／D 沿用、无答案、初始化失败等；预算耗尽但已有成功真实执行的情形满足 a，入 qa）。
- **系统提示（§12.1 第 1 点措辞增补）**："所有数据结论必须来自本次实际执行的查询或结果计算，**或经 `reuse_evidence` 受控沿用并标注来源的历史结果**"。

### 1.5 轮换携带（§13.4 增补）

Runner 组装 `carried_evidence` 时，除时间／维度证据外，把**最近 N 条**（配置 `thread.carry_recent_executions`，默认 3，0 关闭）满足以下条件的执行／分析记录一并携带：`purpose="query"`／分析记录、输出结果元数据 `datasource_id` 等于当前数据源、结果仍可读且未过复用保留期（`load_meta` 成功且 `rows_sha256` 校验通过，保留期按 1.2 第 4 步的存储时钟判定；不满足的不携带，也不阻塞轮换）。

- **按链首原始证据去重**：候选中出现同一原始结果的多次登记（或多次轮换的沿用副本）只携带一份（以当前持有的最完整副本为准，链首身份由其 `reused_from` 解析），避免"最近三条全是同一结果的沿用副本"。
- 携带清单只含记录与 `EvidenceOrigin`；沿用前仍按 1.2 重新全量检查（轮换后首跑数据源可能已变化）。
- **原线程 checkpoint 已删除不影响携带与沿用**：链首解析不回查原线程（1.2），测试须覆盖"账本中无原记录、仅携带副本在场，且原结果文件仍有效"的沿用登记成功路径。

### 1.6 不做的事

- 不为沿用建立新的 ID 前缀或新账本段——复用 `db_executions`／`db_analyses`，靠 `reused_from` 区分。
- 不允许沿用 `probe`／`pushdown` 执行记录；不允许跨会话；不允许执行／分析证据跨数据源。
- 不在沿用时重算任何内容、不为沿用结果续期保留（与 §7.12"不重新计算"原则一致）。
- 沿用不是分析：不产生新数据、新行数、新口径；需要新计算时走 `analyze_result` 或新查询。

## 2. 完整装配验收安排

采用 §21 给出的第二选项：**把完整工具集合验收移到步骤 5**。ToolNode 实际集合包含文件工具（§10：`ls`、`read_file`、`glob`、`grep`；`features.todo_list=true` 时另有 `write_todos`），验收按**实际可分派总数**核对：

| 阶段 | 业务工具＋最终提交 | 文件工具 | 实际总数 |
| --- | ---: | ---: | ---: |
| 步骤 4 | 9（检索 3＋证据 3＋SQL 2＋`submit_final_answer`） | 4 | **13** |
| 步骤 5 | 11（＋`read_result`、`analyze_result`） | 4 | **15** |

（任一阶段启用 `write_todos` 时各 +1。）

- **阶段清单在代码中定义**（装配模块常量，如 `STEP4_TOOLS`／`FULL_TOOLS`）：不让配置任意指定"期望工具名"，否则错误装配可能被配置成验收通过。`features.todo_list` 只作为清单内条件项。
- **装配检查与运行期审计使用同一清单常量**：`doctor --stage all` 的工具集合检查、`test_assembly`、Runner 运行期的工具集合核对（`tool_set_mismatch` 判定）都引用它。
- 步骤 4 的提示词与 Skill **不得要求调用尚未提供的结果工具**（`read_result`／`analyze_result`）：§12.1 第 7 点中"总体统计用 `analyze_result` 下推"等指引在步骤 5 交付后随清单切换一并写入；步骤 4 提示词以"源结果截断时如实说明、不冒充总体"替代。
- `test_assembly` 拆分：步骤 4 断言 13 件 + 结构检查（`DeltaChannel`、无 `files` 通道、`submit_final_answer` 设置、无未清单工具）；步骤 5 改为 15 件断言。

## 3. CLI JSON 输出隔离（§15.2 增补）

原则：**`--json` 模式下 stdout 只出现一个完整 JSON 文档**，此前后后不混入任何其他文本。

1. **启动顺序**：
   1. **轻量预扫描** `argv` 识别输出模式：只判断 `--json` 是否出现（含位于子命令之后的情况，如 `cli query --json --unknown`），**尊重 `--` 分隔符**——`--` 之后的 token 是位置参数（问题文本），其中的 `--json` 不识别为输出选项（如 `cli query -- "--json"` 不是 JSON 模式）；预扫描不执行完整参数解析、不产生任何 stdout 输出。`--help`／`--version` 的 `exit(0)` 属正常帮助退出，按标准行为输出与退出，**不进入参数错误出口**；
   2. `--json` 时，初始化与运行全程用 `contextlib.redirect_stdout(sys.stderr)` 包住：覆盖复用组件的直接 stdout 输出（旧 `ConfigLoader` 在 `.env` 加载时 `print` 的 `[OK]／[WARN]`，及任何第三方库的 stray print），同时配置全部 logger handler（含 RichHandler）到 stderr；
   3. **最终序列化在重定向范围之外**执行：一次性 `print` 单个 JSON 文档到 stdout。
2. **参数解析与 JSON 错误出口**：标准 `argparse.parse_args()` 遇错会直接打印用法并退出（例如未知选项时可能尚未进入 JSON 分支）。处理方式：主解析器**子类化 `ArgumentParser` 覆写 `error()`／`exit()`**——出错时进入统一 JSON 错误出口，不向 stdout 打印普通用法文本（用法/诊断细节走 stderr），以 JSON 错误文档 + 码 2 退出；错误 JSON 同样在 stdout 重定向范围**之外**输出。
3. `query --json`：输出 `DbAgentResult.model_dump_json()`（一个文档、以换行结束）；Rich 对 stdout 的 Console 完全禁用。
4. **错误输出协议**：`--json` 模式下参数错误与初始化失败同样只在 stdout 输出**单个 JSON 错误文档**（`{"status": "error", "error": {"error_type": ..., "message": ..., "retryable": false}}` 结构）并以非零码退出（参数错误 2、运行失败 1）；诊断细节走 stderr。任何异常路径都保证 stdout 可整体 `json.loads`。
5. `doctor --json`／`acceptance` 同规则：单文档 stdout、过程日志 stderr。
6. `--show-trace` 与 `--json` 同给时忽略 `--show-trace`（轨迹属交互渲染），stderr 打一条忽略提示；`--plain` 与 `--json` 同给时 `--json` 优先，stderr 提示忽略 `--plain`。
7. 交互模式（`chat`／无参）不支持 `--json`：同给时按第 4 条 JSON 错误文档退出（码 2），不进入交互。
8. **测试**（断言分组，避免把"未启用 JSON 模式"的路径误纳入单文档断言）：
   - **真正启用 `--json` 的路径**（含首次启动/`.env` 加载、未知参数、缺少必填值、非法选项值、`--json` 位于子命令之后、初始化失败、正常查询）：断言 stdout 可完整解析为**单个 JSON 文档**；
   - **`--` 之后的问题文本含 `--json`**（如 `cli query -- "--json"`）：断言**未启用 JSON 模式**（按普通文本问题处理，输出非 JSON 契约不受单文档约束）；
   - **`--help`**：断言标准帮助输出与退出码 0（不走 JSON 错误出口）。

## 4. 步骤 4 实施前核对清单（非设计，随步骤 4 执行）

- 摘要余量核对（§21"摘要输入范围与模型容量"）：**不按配置值相加推算**（`recent_context_tokens` 是触发时上下文内的保留部分，不是额外输入）。分别以接近触发的真实请求测量：
  - 主模型最坏情况：实际消息历史＋系统提示＋工具定义＋输出余量；
  - 摘要模型单次请求：本次待摘要历史＋摘要提示＋输出余量。
  任一超限则按 §21 调低 `summary_trigger_tokens` 并记录。实测按 §18.3 `test_model_summary_window.py` 在步骤 4 完成。
- 既有已验证机制（状态注解顺序、工具参数防护、摘要观测归属、预算快照标量化）照 doc 5 §5.2/§7.1/§10/§11 实施，不重新推导。

## 5. 并入计划（评审通过后）

| 本文条目 | 折回 doc 5 位置 |
| --- | --- |
| 1.2 reuse_evidence 扩展（四类 ID、检查顺序、保留期、链首解析） | §7.12 增补小节 |
| 1.2 Schema 增量（`reused_from`／`result_generated_at`） | §5.3 `ExecutionRecord`／`AnalysisRecord`／`CarriedEvidence` |
| 1.2 账本去重 reducer（事实字段比较、冲突报错） | §5.2 状态注解（`db_executions`／`db_analyses`） |
| 1.2 `result_expired`（`thread.result_retention_days`、存储时钟、UTC 闭边界） | §7.1 错误类型表 + §6.8 说明 |
| 1.3 引用白名单措辞 | §7.11 检查 3 |
| 1.4 EvidenceItem 两字段 | §14.2 模型 + §15.2 证据表标注 |
| 1.4 统计与归档解耦（qa 条件 a/b、保存 Runner 真实状态） | §13.3（qa／run_errors 判定）+ §12.1 第 1 点措辞 |
| 1.5 轮换携带 | §13.4 + §4.1 `thread` 配置项 |
| 2 装配清单常量与 13／15 件口径 | §10 装配、§15.3 doctor 表、§19 步骤 4/5 行加注 |
| 2 步骤 4 提示词不引用未交付工具 | §12.1、§12.2 |
| 3 JSON 隔离（预扫描含 `--`、启动顺序、argparse 错误出口、子进程测试） | §15.2 增补 + §16（ConfigLoader stdout 说明） |
| 全部新增测试（§18 清单更新） | §18：执行／分析记录分别去重及同 ID 冲突检测；连续沿用、并行沿用与混批失败；原 checkpoint 删除后的携带复用；纯沿用归档与各真实运行状态（含 `budget_exhausted`／`no_data`／`needs_clarification`）归档；保留期边界（存储时钟注入）；CLI 子进程输出（含 `--` 后置与 `--help` 边界） |
| §21 表 | 删除"历史结果作为最终答案引用"行；"实施顺序中的完整装配验收"行标记已按选项二收口 |
