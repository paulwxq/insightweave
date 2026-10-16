# 17. `--step md` 依赖 JSON 产物改造方案

状态：设计稿，尚未实施。修订日期：2026-10-01。

> **后续变更**：`metaweave pipeline` 编排命令及 `configs/pipeline*.yaml` 已由 [doc 20.1](20.1_表结构发布选项统一与空描述字段处理改造计划.md) §10.8 整体移除；下文涉及 pipeline 执行顺序、`pipeline_config.py`、`pipeline.yaml` 的内容仅作历史记录，md 依赖 json 的单步规则不变。

本文承接 [15. `--step md` 依赖 DDL 文件改造方案](../15_md步骤依赖DDL文件改造方案.md)，**反转其数据源**：md 不再读 `output/ddl/*.sql`，改为读 `output/json/*.json`。DDL 默认关闭 LLM 的改动已纳入 [doc 16](16_DDL采集与当前态表定义发布设计.md)；本文实施时检查该前置条件，若尚未落实则先关闭，再由 `--step json` 补齐缺失注释并写出处标识。本文同时**修订 [doc 26](../update/26_配置驱动pipeline编排设计.md) §4.1 的 metadata 执行顺序定义**：把 `json` 提到 `md` 之前（新的唯一标准顺序见 §3.1），冲突处以本文为准。

## 1. 问题与目标

### 1.1 现状（本次改造前）

| 步骤 | 结构与注释来源 | 数据库 | LLM |
| --- | --- | --- | --- |
| `ddl` | 数据库 | 需要 | 改造前 `ddl_generation.comments.llm_enabled` 默认 `true`；doc 16 将默认值改为 `false` |
| `json` | **DDL 文件**（结构/注释）+ 数据库（COUNT/采样/画像/索引） | 需要 | `json_generation.comments.llm_enabled`（默认 `true`）→ 补缺失注释，并写 `comment_source` |
| `md` | **DDL 文件**（含 `SAMPLED_RECORDS` 样例块） | 不需要 | 无（file-only，`generator.py:937` 方法注释明确"零数据库访问"） |

由此产生三个问题：

1. **注释来源不可区分**：`ddl` 开着 LLM 时，LLM 注释与数据库原始注释一起写成 `COMMENT ON ... IS '...'` 落进 `output/ddl/*.sql`；DDL 产物**不记录来源**（`formatter.py` 中 `comment_source` 出现 0 次），`ddl_loader.py:735-747` 解析时只要文件里有注释就一律标 `"ddl"`。→ 下游无法回答"哪些表/字段本来就没有注释"。
2. **md 语料拿不到补齐后的注释**：json 步骤补的注释只落在 JSON 里；md 从 DDL 渲染，因此 `rag_load --type table_schema` 的表/列描述、`--generate-domains` 的 md 摘要永远是"未补齐"版本。
3. doc 16 关闭 ddl 的默认 LLM 后，问题 2 会被放大：md 将只剩原始注释。

### 1.2 改造目标

- **G1**：`--step md` 只读 `output/json/*.json` 生成，不再读 DDL 文件；执行顺序固定为 `ddl → json → md`。
- **G2（前置检查）**：检查 `configs/metadata_config.yaml` 的 `ddl_generation.comments.llm_enabled`、`GenerationConfig.from_config()` 的 `_boolean` 回退值与 `DdlCommentsConfig.llm_enabled` 字段默认值三处均已关闭；若有遗漏，在实施本文其余改动前按 doc 16 补齐。默认运行的 `--step ddl` 产物中的注释才可按数据库原始注释解释；显式开启 DDL LLM 时不适用该口径。
- **G3**：`--step json` 对每条表/列注释写出来源标识，作为注释治理的唯一口径：
  - `comment_source = "ddl"` → 注释来自 DDL 文件（在默认关闭 DDL LLM 且重跑 DDL 后 = **数据库原始注释**）
  - `comment_source = "llm"` → 由 json 步骤 LLM 补齐
  - `comment_source = ""` → 最终仍无注释（治理缺口）
- **G4**：md 的文件格式契约不变——`MDParser`（`metaweave/core/table_schema/md_parser.py`）可解析、章节结构与现在一致，`rag_load --type table_schema`、`--generate-domains`、`--generate-sql-rag` **零改动**。

**非目标**：不改 `rel`/`cql` 的输入契约；不改平台库表结构；不给 md 增加 LLM 能力；不在 md 正文里可视化"LLM 生成"标记（见 §8 D3）；不追溯历史产物（§6.3）。

### 1.3 注释治理口径（G3 的用法）

重跑 `ddl`（LLM 关闭）→ `json` 之后，用 JSON 产物即可统计：

- 原始注释覆盖率 = `comment_source == "ddl"` 的表/列数 ÷ 总数
- LLM 补齐数 = `comment_source == "llm"`
- 缺口清单 = `comment_source == ""`（该表/列最终仍无注释）

表级见 `object_info.comment_source`，列级见 `column_profiles.<列>.comment_source`。

## 2. 现状依据（代码位置）

| 关注点 | 现状 | 位置 |
| --- | --- | --- |
| md 的对象枚举 | 扫 DDL 目录，按 `{database}.{schema}.{table}.sql` 三段式过滤 | `generator.py:324` `_get_objects_from_ddl_dir`、`:388-392` `_get_tables_to_process`、`:190` `_infer_schemas_from_ddl_dir` |
| md 的处理路径 | `_process_table_from_ddl_for_md()`，file-only | `generator.py:566-570` 路由、`:937` 方法体 |
| md 的 DDL 读取 | `_get_ddl_loader().load_table()` | `generator.py:1144-1148`、`ddl_loader.py:98-101` |
| md 的前置检查 | DDL 目录存在 + 当前库三段式文件 + `database.database` | `execution.py:738-775`（失败文案"请先执行 --step ddl"） |
| md 的渲染 | `formatter.generate_markdown(metadata, sample_data)`，章节：`# schema.table [type]（表注释）` / `## 字段列表：` / `## 字段补充说明：` | `formatter.py:373`、`:397`、`:490` |
| 类型串渲染 | `format_data_type()` 是列类型显示的统一入口；仅当类型恰为裸 `character varying` / `numeric` 等且给出独立修饰符时追加长度或精度，已带 `(…)` 的类型串经大小写规范化后仍保持原修饰符 | `metaweave/utils/data_utils.py:89-129`、`formatter.py:401-406` |
| md 的 clean 目标 | `output.markdown_directory`（默认 `output/md`），删顶层文件、保留子目录 | `clean_targets.py:80`、`metadata_cli.py:539-540` |
| md 的下游 | `rag_load --type table_schema`（与同名 json 配对）、`--generate-domains`（md 摘要）、`--generate-sql-rag`（`md_context_dir`） | `table_schema_loader.py:244-249`；`md_parser.py:8-12` 的两个正则 |
| json 的产物字段 | 顶层 `metadata_version(3.0)`、`object_info`、`column_profiles`、`table_profile`、`sample_records`、`profiling` | 实测 `output/json/*.json` |
| JSON 样例来源 | 优先从 DDL 的 `SAMPLED_RECORDS` 块提取并把数字字符串转为数字；DDL 无有效样例块时，可回退本次数据库采样 | `formatter.py:529-597`、`:613-623` |
| json 的注释来源标识 | **已具备**：`comment_source` 三态 | `json_llm_enhancer.py:344-360`、`:375`、`:391` |
| json 读 DDL | 逐表 `_get_ddl_loader().load_table()`（对象列表来自数据库） | `generator.py:786`、`:391` |
| ddl 的 LLM 开关 | 改造前默认 true；doc 16 负责关闭 YAML、`from_config()` 的 `_boolean` 回退值及 dataclass 字段默认值，本文检查实际配置 | `metadata_config.yaml:297-301`、`generation_config.py:41-42,96-101,121-130`、`generator.py:160-181` |
| 编排顺序 | `ddl → md → json → rel → cql` | `configs/pipeline.yaml` |

## 3. 详细改造

### 3.1 执行顺序与编排（G1 第一部分）

- **执行顺序的唯一权威是代码常量**：`metaweave/cli/pipeline_config.py` 的 `METADATA_STEP_ORDER`（注释：metadata 唯一标准执行顺序，doc 26 §4.1）。它必须改为：

  ```python
  METADATA_STEP_ORDER = (
      "ddl", "json", "md", "generate-domains",
      "generate-dim-config", "rel", "cql", "generate-sql-rag",
  )
  ```

  即把 `json` 提到 `md` 之前，其余保持：`generate-dim-config` 仍排在 `json` 之后（它依赖 json 产物）、`generate-domains` 与 `generate-sql-rag` 仍排在 `md` 之后（它们依赖 md 产物）。只改这一处顺序时，`build_execution_plan()` 构造的计划顺序随之变化；若不改这个常量，`metaweave pipeline` 仍会先跑 md，而改造后的 md 只认 JSON：`output/json` 为空或缺失时该步骤失败，更糟的是目录里若有上一轮的 JSON 残留，md 会按 §3.4"目录有什么就渲染什么"**静默渲染过期数据**。
- `configs/pipeline.yaml` 的 metadata 章节键序同步调成 `ddl → json → md → rel → cql`。**这只是可读性对齐，不承担顺序语义**：doc 26 §4.1 明确"YAML 中步骤键的书写顺序不表达执行顺序"，执行计划始终由上述常量生成；实测把 YAML 键写成任意顺序，计划顺序仍与常量一致。两处一致是为了避免后人误以为改 YAML 就能改顺序。
- `CLAUDE.md` / `AGENTS.md` 同步更新四处：① pipeline 步骤列表的编号顺序；② 各步骤依赖说明（`md` 依赖 **json**，不再是 ddl）；③ **"全流程手动序列"那一行**（现为 `--step ddl` → `--step md` → `--generate-domains` → `--step json` → …，改为 `--step ddl` → `--step json` → `--step md` → `--generate-domains` → …）；④ **Output Structure 目录树里 `md/` 的说明**（两份文件的 `:211` 现写 `Human-readable Markdown documentation (generated from DDL files)`，改为 from JSON）。
- **文档同步范围（按 §1 的修订声明）**：① `docs/15_md步骤依赖DDL文件改造方案.md`（本目录外）文首标注"数据源已被本文取代"；② `docs/update/26_配置驱动pipeline编排设计.md` 三处——§4.1 的顺序文本、§4.2 的"例如 `md` 需要 DDL"（`:362`）、路径审计段的"依赖输入：MD 读取 DDL"（`:751`）；③ `README.md` 的"按需逐个执行：ddl → md → json → rel → cql"及紧随的示例命令顺序（`:143` 一带）；④ `docs/100_执行命令完整参考.md` **共 8 处**：两处顺序（`:74` 的"执行轨道（手动按序执行）"图、`:529` 的"固定顺序"段，现均列 `ddl → md → generate-domains → json → …`）＋六处"md 读取 DDL"说明（`:96` 输入输出表、`:111` 依赖图、`:127` 关键约束"`md`、`json` 均依赖 `output/ddl/` 非空"须拆开——json 仍依赖 DDL 非空、md 改为依赖 JSON 非空、`:291` md 小节"依赖"行、`:294` "说明"行、`:647` 汇总表）；只改两处顺序会让操作说明自相矛盾；⑤ `configs/pipeline_init.yaml` 的 metadata 键序（现为旧排列，与 `pipeline.yaml` 同理，仅可读性）。

### 3.2 检查 ddl 已默认关闭 LLM（G2）

- 先检查 `configs/metadata_config.yaml` 中 `ddl_generation.comments.llm_enabled: false`，以及 `generation_config.py` 中 `GenerationConfig.from_config()` 的 `_boolean(ddl_comments, "llm_enabled", False, ...)` 回退值和 `DdlCommentsConfig.llm_enabled` 默认 `False`。`from_config()` 总是显式传值构造 dataclass，字段默认值不能代替 `_boolean` 的回退值。doc 16 负责落实这三处；若实施本文时有遗漏，先按 doc 16 补齐并验证再继续。
- 检查显式 `true` 仍可开启 DDL LLM，且 `overwrite: true` + `llm_enabled: false` 的原有互斥校验仍有效（`generation_config.py:121-130`）。
- 验证默认运行 `--step ddl` 的摘要里“LLM 请求/注释成功失败”均为 0；DDL 的 `SAMPLED_RECORDS` 仍按 `output.ddl_options.sample_records.enabled: true` 生成。`generator.py:607-613` 的 `needs_sample_data` 条件实际是 `comment_enabled` 与样例块开关的逻辑或；样例块开关为 `true` 时，即使 DDL LLM 关闭，也会独立触发采样。这属于前置验收；即使 doc 16 实施时已经验证，本文实施时也应复核。

### 3.3 json 的注释来源标识（G3）

现状已实现，本次只需**固化与验证**：

- 保持 `json_generation.comments.llm_enabled: true`（补齐缺失注释）与 `overwrite: false`（不覆盖从 DDL 读到的注释）。
- 三态语义写进文档与测试：`"ddl"` / `"llm"` / `""`；临时键 `comment_original`、`comment_source_original` 落盘前剥离（`json_llm_enhancer.py:344-360`）。
- 新增测试：造一份"部分表/列有注释、部分没有"的 DDL 输入，跑 json 后断言三态齐全（表级 + 列级各至少一条），且 `overwrite: false` 时已有注释不被改写。
- 汇总块不做（见 §8 D5）：不在 JSON 里加 `profiling.comment_stats`；平台若要覆盖率，先按 §1.3 口径扫文件统计。

### 3.4 md 的输入切换（G1 第二部分）

**新增** `metaweave/core/metadata/json_loader.py`（建议名）：

- `list_objects() -> list[DatabaseObjectRef]`：扫 `output.json_directory`（默认 `output/json`），按 `{database}.{schema}.{table}.json` 三段式过滤（与 DDL 侧命名规则一致）；对每个文件**先复用 `MetadataDocument.from_dict()` 做 v3 校验**（见下一条），再读 `object_info.object_type` 做对象类型筛选、核对文件名与 JSON 内对象身份。**枚举阶段的失败语义与现有 DDL 枚举保持一致**：`_get_objects_from_ddl_dir()` 遇到文件名或对象类型异常是直接 `raise DDLLoaderError`（不跳过），json 侧同样——三段式不符、v3 校验失败、身份不一致都会**使整个 MD 步骤失败并报告文件名**；`generate()`（`generator.py:289` 调用 `_get_tables_to_process()`）在枚举处没有逐表容错，异常由外层捕获，`failed_tables` 不会按表增加。用于替代 `_get_objects_from_ddl_dir()`。
- `infer_schemas() -> list[str]`：替代 `_infer_schemas_from_ddl_dir()`。
- `load_object(schema, name) -> TableMetadata`：把已通过枚举校验的 JSON 按 3.5 的映射还原成 `TableMetadata` / `ColumnInfo`；映射中发现的错误（3.5 表要用的字段缺失、类型不符）抛专用异常，**计入该表失败、不中断其余对象**（沿用 `DDLLoaderError` 的同类语义与现有逐表失败统计）。枚举通过的文档保留为本次运行的内存快照，`load_object()` 复用快照、不重读磁盘——枚举之后文件被删除或被外部修改**不影响本次运行**（快照语义）；仅当快照未命中（未经枚举直接调用 `load_object`）且文件缺失时按逐表失败处理。**校验入口复用**：v3 结构校验统一走 `MetadataDocument.from_dict()`（`metaweave/core/metadata/metadata_document.py:28`；校验 `metadata_version == "3.0"`、`object_info`/`column_profiles`/`table_profile` 必需字段、节点形状、分类契约与索引，docstring 已点名"后续…加载器迁移时可复用"），在 `list_objects()` 阶段执行；**不得另写第二套 schema 校验**。
- JSON 目录中的当前数据库三段式文件是 md 的对象清单：目录有什么就渲染什么，不回头查询数据库或补读 DDL；再沿用现有 `--schemas` / `--tables`、对象类型及排除规则过滤。JSON 目录有旧文件时，也会按这份清单渲染，运行前需按现有清理流程确保输入正确。
- **旧产物残留的口径（本阶段不改 clean 默认值）**：默认 `configs/pipeline.yaml` 的步骤值为空，`_validate_step_options()` 对空值返回 `{"clean": False}`（`pipeline_config.py:171`），即日常 pipeline 运行**不清理任何输出目录**。源库删除对象后，旧 `output/json/*.json` 仍在，md 会按上一条继续渲染它并留下对应旧 MD，`--generate-domains` 也会照读这些旧 MD。这与改造前"旧 DDL 残留 → 旧 MD"是同一性质，不是本次引入；本文不改变 clean 默认（doc 26 把"不启用 clean"作为安全默认值的说明不修订），需要精确产物时按**先 JSON、后 MD** 的顺序显式清理：`--step json --clean` → `--step md --clean`（或在 pipeline 对应步骤值写 `clean: true`）——**只清 MD 而不清 JSON 时，旧 JSON 会让旧 MD 重新生成**，达不到"当前对象集合"；向量库若也要求全量一致，再按 §3.6 的覆盖范围条件处理加载。
- **继续模式下 JSON 失败不做依赖跳过（已定，不改 doc 26 规则）**：`--continue-on-error` 时 JSON 步骤失败（含逐表失败、已写出部分文件）后，MD 仍会被执行，并读取目录中现有的 JSON（可能是旧文件或本轮的部分文件）。本阶段**不为 JSON→MD 增加专用失败跳过**，保持 doc 26"继续模式不进行通用依赖推断或产物可用性求解"的规则（`docs/update/26` §5.2）；**继续模式只用于排查，其下游 MD 产物不得视为本轮结果**。需要可靠产物时使用默认的失败即停模式，并从 JSON 步骤重跑。另需注意**跨章节写入**：同一计划若还选了 `rag_load.table_schema`，继续模式下调度仍会执行它（`pipeline_cli.py` 的继续逻辑不做依赖跳过），并把当前（旧或部分）MD/JSON **真实写进 Milvus**——"仅用于排查"指的是产物不可信，**不等于没有下游副作用**。
- 本文只改未受管 CLI/pipeline 对配置中 `output.json_directory` 的读取；不接入 doc 16 的 `output/<source_id>/<job_id>/ddl/` 受管工作区，也不宣称 MD 已能作为 Web 采集或发布步骤。受管 JSON/MD 的工作区输入和发布规则另行设计。

**generator 改动**：

- `_process_table()`（`generator.py:566-570`）：md 分支改为调用新的 `_process_table_from_json_for_md()`。
- `_process_table_from_json_for_md()`：从 JSON 还原 `TableMetadata` → 用 `sample_records.records` 构造 `sample_data`（`pandas.DataFrame`，仅用于渲染示例值）→ `formatter.format_and_save(metadata, sample_data, formats_override=["markdown"])`。保持现有章节和筛选语义；`metadata.full_name`、`object_type` 不变。JSON 样例中的数值已类型化，`_get_sample_value()` 会转成字符串，因此小数尾零可能不同于 DDL 样例原文；本次不额外按 scale 补零。
- md 路径切到 JSON 后，删除只服务于 DDL→md 路径的 `_get_objects_from_ddl_dir()`、`_infer_schemas_from_ddl_dir()`、`_md_parsed_ddl` 与 `_process_table_from_ddl_for_md()`；保留 JSON 步骤仍使用的 DDL 读取逻辑，不把两者混删。
- 保持 md 步骤**不初始化数据库连接**（`generator.py:` `if self.active_step != "md"` 的连接检查分支不变），`finally` 的 `connector is None` 防空检查不变。

**CLI / 前置检查改动**：

- `execution.py:738-775` 的前置检查改为：`output.json_directory` 存在 + 存在当前 `database.database` 的三段式 JSON 文件；失败文案改为"`--step md` 依赖 JSON 产物，请先执行 `--step json`"；`database.database` 仍是必需配置（用于文件名匹配）。另验证**旧版本或结构非法的 JSON 被明确拒绝且不静默跳过**：`metadata_version: "2.0"`、缺 `object_info`、根节点非对象等输入在**枚举阶段**即失败——整个 `--step md` 以非零结束并**报告具体文件名**（与现有 DDL 枚举"文件名/对象类型异常直接抛错"同口径）；不得按空值渲染。只有进入处理阶段后的映射错误才计入逐表失败。
- `--clean` 语义与目标不变（`markdown_directory`）。
- `--schemas` / `--tables` 过滤语义不变（从 JSON 枚举后过滤）。
- **不提供 DDL 回退**：`output/json` 缺失或该库无三段式 JSON 文件时直接前置失败，不做"退回去读 DDL"的兼容分支——保持单一数据源，避免同一份 md 因环境不同而内容不同（延续 `docs/15_md步骤依赖DDL文件改造方案.md` 的硬依赖取向）。

### 3.5 JSON → TableMetadata 字段映射

| md 渲染所需 | JSON 来源 | 备注 |
| --- | --- | --- |
| 数据库名（用于核对） | `object_info.database` | 还原为 `TableMetadata.database`，并与配置的 `database.database`、JSON 文件名前缀**三方核对**。注意 **MD 文件名不由它决定**：`formatter._get_filename()`（`formatter.py:110-120`）拼的是 formatter 自己配置的 `database_name`，输出仍为 `{database}.{schema}.{object}.md` |
| 表名 `schema.table`、对象类型 | `object_info.schema_name` / `object_name` / `object_type` | `object_type ∈ {table, view, materialized_view}`，与 md 现有支持范围一致 |
| 表注释（标题括号） | `object_info.comment` | 空则标题不带括号（`formatter.py:394`） |
| 列名、顺序、类型 | `column_profiles` 的键、`ordinal_position`、`<列>.data_type` | 按 `ordinal_position` 还原列顺序。JSON 没有独立的 `character_maximum_length`、`numeric_precision`、`numeric_scale`，类型串已包含修饰符（如 `character varying(100)`、`numeric(10,2)`）；`format_data_type()` 只给裸类型追加独立修饰符，已带修饰符的类型经大小写规范化后显示不变 |
| 列注释 | `<列>.comment` | 空渲染为"无注释"（`formatter.py:412`） |
| 列示例值 | `sample_records.records`（顶层，`{col: value}` 列表） | JSON 数值已类型化，转回字符串后可能丢失 DDL 原有小数尾零（`18.00` → `18.0`）；DDL 无样例块时 JSON 可能回退数据库采样，原 md 的 `null` 会变成真实示例值 |
| 主键 | `table_profile.physical_constraints.primary_key`（`{constraint_name, columns[]}`） | 渲染进 `## 字段补充说明：` |
| 外键 / 唯一约束 | `table_profile.physical_constraints.foreign_keys` / `unique_constraints` | 同上 |
| 独立索引 | `table_profile.indexes[]`（含 `key_expressions`、`included_columns` 及既有 `index_name/index_type/columns/is_unique/is_primary/is_constraint_backed/condition/definition`） | 渲染规则复用 `formatter.py:449-471`（过滤 `is_primary` / `is_constraint_backed`） |
| （渲染边界）多行注释 | 表注释、列注释在**渲染时**把内部换行折叠为空格 | `normalize_comment()`（`comment_utils.py:9`）只去首尾空白、不经手内部换行，而 `generate_markdown()` 直接把注释插进标题行与字段行；注释含内部换行时 md 会被拆成多行，`MDParser.COLUMN_PATTERN`（`md_parser.py:25`，逐物理行匹配）只取到第一行。实测复现：列注释 `第一行\n第二行` 的解析结果为 `{'c1': '第一行'}`，**第二行从"列对象"的 `object_desc` 中丢失**（`table_schema_loader.py:283` 用 `get_column_descriptions()`；表对象取的是整份 MD 文本（`:266` 的 `get_table_description()`），不受该截断影响，但标题行会被撑破）。json 步骤的 LLM 补齐很容易产出多行文本，风险比改造前更高。处理：只在 MD 渲染边界折叠，**不改 JSON 产物中的原文**；补多行注释的表/列用例。属有意修正——这类对象的 md 文本会与改造前不同 |
| 数值精度说明 | 从 `<列>.data_type` 解析 `numeric(p,s)` / `decimal(p,s)` | 普通表 `orders.amount` 的 JSON 类型是 `numeric(10,2)`，须还原为基础类型 `numeric` 与独立的 `numeric_precision=10`、`numeric_scale=2`，使现有 formatter 同时复现字段类型和“精确到小数点后2位”行；不能将已带 `(10,2)` 的类型再追加一次修饰符。视图的裸 `numeric` 原本就没有该说明行 |

### 3.6 与下游契约的关系

- `MDParser.COLUMN_PATTERN` / `TABLE_NAME_PATTERN`（`md_parser.py:8-12`）与渲染格式不变，故 `rag_load --type table_schema` 无需改动。
- ⚠️ 但 `MDParser.get_table_description()` 返回**整份 md 文本**，它会作为表的 `object_desc` 写进 Milvus `table_schema_embeddings`。因此要区分两种情形：**内容更新**（注释补齐）重跑 `rag_load --type table_schema` 即可同步；**对象删除**（源库删表、MD 已不再产出该对象）**普通重跑不会同步**——加载器默认按 `object_id` upsert（`table_schema_loader.py:344-347`：只有 `clean=True` 走 `insert_batch`），已消失对象在 collection 里的旧行仍在。要求向量库与当前 MD 全量一致时，须另行 `rag_load --type table_schema --clean` 重建，并**先确认本次输入覆盖该 collection 的全部内容**，否则会把其他来源的数据一并清掉。
- `--generate-domains` 的 md 摘要在注释补齐后内容会变（属预期，验收时确认域划分无异常变化）。

## 4. 影响面

| 文件 | 改动 |
| --- | --- |
| `configs/metadata_config.yaml` | 检查 `ddl_generation.comments.llm_enabled: false`；若 doc 16 尚未落实则补齐；`json_generation.comments` 保持不变 |
| `metaweave/cli/pipeline_config.py` | **执行顺序的唯一改动点**：`METADATA_STEP_ORDER` 改为 `(ddl, json, md, generate-domains, generate-dim-config, rel, cql, generate-sql-rag)`；`METADATA_STEP_KEYS`（白名单）不变 |
| `configs/pipeline.yaml` | metadata 章节键序同步调成 ddl → json → md → rel → cql（仅可读性对齐；执行顺序由 `pipeline_config.py` 常量决定） |
| `metaweave/core/metadata/generation_config.py` | 检查 `from_config()` 中 `_boolean` 回退值和 `DdlCommentsConfig.llm_enabled` 均为 `False`；若 doc 16 尚未落实则与 YAML 同步补齐 |
| `metaweave/core/metadata/json_loader.py`（新增） | JSON → `TableMetadata` 适配 + 枚举/推断 schema；**读取即复用 `MetadataDocument.from_dict()` 做 v3 校验**，不另写 schema 校验 |
| `metaweave/core/metadata/generator.py` | md 路由、`_process_table_from_json_for_md()`、对象和 schema 改从 JSON 枚举；删除只供旧 md 路径使用的 `_get_objects_from_ddl_dir()`、`_infer_schemas_from_ddl_dir()`、`_md_parsed_ddl` 与 `_process_table_from_ddl_for_md()`，保留 json 步骤使用的 DDL 读取逻辑 |
| `metaweave/core/metadata/formatter.py` | **需改一处**：`generate_markdown()`（`formatter.py:373` 起；表注释渲染在 `:393`、列注释在 `:412`）在渲染表注释与列注释时把内部换行折叠为空格（§3.5 的多行注释规则），不改 JSON 原文，并新增多行注释的表/列用例。其余渲染逻辑复用现有实现；`_extract_sample_records_from_ddl()` 仍是 JSON 生成链的依赖，本次不得顺手删除或改动。核对带修饰符类型、精度说明和示例值差异的测试 |
| `metaweave/core/metadata/execution.py` | md 前置检查与文案（`:738-775`） |
| `metaweave/cli/metadata_cli.py` | 现有提示文案核对后无需改动：全仓的"请先执行 --step ddl"只出现在 `execution.py`（本次改写为指向 `--step json`）与待删除的 `generator.py:279` 分支；`metadata_cli.py:127` 的提示是 generate-domains 的"请先执行 metaweave metadata --step md"，改造后依然成立。**但需按验收 7 补一句 CLI 帮助**：在 `--step json` 的说明或 `metadata` 命令 docstring 中注明"旧 JSON（ddl 阶段 LLM 补齐过注释）的 `comment_source` 不构成'原始注释'口径，需重跑 ddl+json 后才有治理含义" |
| `CLAUDE.md` / `AGENTS.md` | 四处：pipeline 步骤编号顺序、各步骤依赖说明（md 依赖 json）、**"全流程手动序列"一行**（改为 ddl → json → md → generate-domains → …）、**Output Structure 目录树里 `md/` 的说明**（`:211` 的 "generated from DDL files" → from JSON） |
| `docs/15_md步骤依赖DDL文件改造方案.md` | 标注数据源已被本文取代 |
| `docs/update/26_配置驱动pipeline编排设计.md` | §4.1 的 metadata 执行顺序定义按本文 §1 的修订声明标注（json 提到 md 之前）；**§4.2 的"例如 `md` 需要 DDL"（`:362`）与路径审计段的"依赖输入：MD 读取 DDL"（`:751`）一并标注**；§5.2 继续模式规则不变（见 §3.4）；其余条款不变 |
| `README.md`、`docs/100_执行命令完整参考.md`、`configs/pipeline_init.yaml` | 顺序与依赖说明同步，**完整清单见 §3.1 ④**：README 的"按需逐个执行"序列与示例命令（`:143` 一带）、命令参考的**两处顺序＋六处"md 读取 DDL"说明**（`:74`/`:529`、`:96`/`:111`/`:127`/`:291`/`:294`/`:647`）、init 模板的 metadata 键序（仅可读性） |
| 测试 | `tests/unit/metaweave/metadata/test_md_view_support.py`、`tests/unit/metaweave/metadata/test_formatter_output_paths.py`、`tests/unit/metaweave/metadata/test_ddl_summary.py`、`tests/unit/metaweave/metadata/test_formatter_sample_records_block.py`、`tests/unit/test_pipeline_config.py`、`tests/unit/test_pipeline_cli.py`、`tests/unit/test_single_config_snapshot.py`、`tests/unit/test_metadata_generate_domains_cli.py`、`tests/unit/test_metadata_generate_sql_rag_cli.py`、`tests/unit/test_db_step.py`、`tests/unit/test_project_path_unification.py` 等需逐项核对；其中三处**不是核对而是重写**：① `test_formatter_sample_records_block.py::test_md_step_uses_normalized_ddl_sample_records` 直接调用待删除的 `_process_table_from_ddl_for_md()`（**测试中唯一直接调用点**；该方法的另一处调用是生产路由 `generator.py:569`，随本节 generator 行一并改掉），须重写为 JSON 已类型化样例值的渲染测试；② `test_md_view_support.py` 用 DDL 夹具驱动 `generate(step="md")`（`:137/:190/:214/:229` 共 4 处，对应 4 个测试函数），须把夹具由写 `*.sql` 改写为写 JSON 文件，并覆盖 table / view / materialized_view 三类对象；③ `test_project_path_unification.py::test_md_step_clean_target_project_root`（`:229`）现在只写 DDL 夹具（`output/ddl/db.public.t.sql`）来通过 md 前置检查，切换为 JSON 前置后会失败——夹具改为当前库的同名 JSON 文件，并保留"从非项目 cwd 执行时 `--clean` 目标仍基于项目根"的断言。其余列出的文件按"逐项核对"处理；**但 `test_pipeline_cli.py` 不止核对**——按验收 9 还要新增（或扩展）一个含 `ddl`/`json`/`md` 的 CLI 实跑顺序用例，勿按"核对"低估工作量。复核 doc 16 的 DDL 默认关闭断言；新增 md 读 JSON 的用例：正向（正常渲染）+ 负向（旧版本/缺字段/根节点非对象的 JSON 在**枚举阶段**被 `MetadataDocument.from_dict()` 拒绝，整步失败并报文件名）+ 多行注释折叠 |

## 5. 验收标准

1. **顺序链路**：`--step ddl`（LLM 关闭）→ `--step json` → `--step md` 全绿。与改造前比较同一份数据和注释时，标题、字段名与顺序、类型、注释、约束、索引及数值精度说明保持一致（**例外**：表/列注释含内部换行的对象按 §3.5 在渲染边界折叠为单行，属于有意修正，md 文本会与改造前不同且不再被 `MDParser` 截断）；JSON 补齐注释后对应文本按新值输出。示例值不要求逐字节一致，验收中明确接受这三类差异：① 类型化数值丢失小数尾零（`18.00` → `18.0`）；② DDL 无样例块而 JSON 回退数据库采样时，原来的 `null` 变成真实示例值；③ **含 `null` 的整数列经 pandas 变浮点**（`1` → `1.0`：该列 dtype 被提升为 `float64`，`formatter.py:364` 的 `astype(str)` 直接渲染，已实测复现）。
2. **DDL 前置条件**：检查 YAML、`GenerationConfig.from_config()` 的 `_boolean` 回退值及 `DdlCommentsConfig` 字段默认值三处均已关闭；若没有，先按 doc 16 补齐再继续。默认执行 `--step ddl` 的 LLM 请求为 0 次；重跑后 `output/ddl/*.sql` 的注释与数据库 `obj_description` / `col_description` 一致（人工抽查一张有注释、一张无注释的表）。
3. **md 的硬依赖与非法输入**：删掉 `output/json` 或该库的 JSON 文件后执行 `--step md`，前置检查失败且文案指向 `--step json`；`--step md` 全程零数据库连接（用无 DB 环境验证）。非法 JSON（`metadata_version` 非 `"3.0"`、缺 `object_info`、根节点非对象）在**枚举阶段**使整步失败并报告文件名，不静默跳过、不按空值渲染（失败语义见 §3.4）。
4. **来源标识**：json 产物中，表级与列级的 `comment_source` 三态齐全且与输入吻合（原始注释 → `"ddl"`；缺失被补齐 → `"llm"`；LLM 也未给出 → `""`）；`overwrite: false` 下已有注释不被改写；无残留 `comment_original` / `comment_source_original` 键。
5. **下游不变**：`--generate-domains`、`--generate-sql-rag` 正常读取 md；`rag_load --type table_schema` 重跑后**现存对象**的表/列描述与 md 同步；**对象被删除**的情形按 §3.6 另行用 `--clean` 全量重建验证（并确认输入覆盖整个 collection，否则视为验收不通过）；`--clean`（md 目标）语义不变。
6. **视图/物化视图**：md 对 `view` / `materialized_view` 的结构与注释输出按验收项 1 保持一致；示例值允许上述差异（`test_md_view_support.py` 覆盖）。
7. **历史产物**：不追溯；旧 JSON（含 ddl 阶段 LLM 注释）在重跑 ddl+json 之前，其 `comment_source` 口径不成立——文档中明确说明，并**在 CLI 帮助中补一句**（落点见 §4 的 `metadata_cli.py` 行：`--step json` 说明或 `metadata` docstring）。
8. **数值精度说明**：普通表 `orders.amount` 等原先已有精度说明的 `numeric(p,s)` / `decimal(p,s)` 列仍须输出与改造前相同的“精确到小数点后 s 位”行；现有 formatter 对 `scale=0` 等原先不输出的情况保持原行为。裸 `numeric` 且原先无该行的视图/物化视图仍不输出。字段类型不得出现重复修饰符（如 `numeric(10,2)(10,2)`）。
9. **pipeline 执行顺序**：`pipeline_config.py` 的 `METADATA_STEP_ORDER` 已改为 §3.1 的新值。`tests/unit/test_pipeline_config.py` 有完整的计划顺序断言（含 `test_yaml_order_irrelevant`），把期望顺序同步改成 `ddl → json → md → rel → cql` 即可；`tests/unit/test_pipeline_cli.py` 的现有用例**不覆盖本序列**——`test_bare_orders_and_runs_all` 只选了 `ddl`/`cql`，继续模式用例只选了 `ddl`/`md`，**须新增（或扩展）一个含 `ddl`/`json`/`md` 的 CLI 执行顺序用例**，断言实跑调用序列为 `ddl → json → md`，避免只测常量而漏掉实际调度；`metaweave pipeline metadata` 实跑顺序一致。“顺序语义不在 YAML”这一点由既有 `test_pipeline_config.py::test_yaml_order_irrelevant` 覆盖（内联合成 YAML、键序 `cql → ddl → md → json → rel`，断言计划仍按常量顺序）：**保持通过即可，不必另加反证用例**——但该断言里的期望顺序必须随常量同步改成 `md` 在 `json` 之后。
10. **旧产物与继续模式的口径**：默认 `configs/pipeline.yaml`（步骤值为空 → `clean: false`）下重跑，源库已删除对象的旧 JSON 与旧 MD 仍存在且被继续使用——记录为**已知行为**，不视为缺陷；按 §3.4 的顺序（**先 `--step json --clean`、再 `--step md --clean`**）执行后旧文件消失；只清 MD 不清 JSON 时旧 MD 会再次生成，不作为通过条件。`--continue-on-error` 下让 JSON 步骤失败后，MD 仍被执行且其产物来自旧/部分 JSON——按 §3.4 的既定口径，该 MD 不作为本轮结果；若同一计划还选了 `rag_load.table_schema`，它同样会执行并把这份 MD/JSON **真实写进 Milvus**（记录该跨章节写入行为，"仅用于排查"不等于无副作用）。两种情形都不得引入自动依赖跳过。

## 6. 实施顺序与回滚

### 6.1 顺序

1. G2（前置检查）：核对 doc 16 已将 YAML、`from_config()` 中 `_boolean` 的回退值和 dataclass 字段默认值三处关闭；如有遗漏，先补齐并验证，再执行下列步骤。
2. G3（来源标识的测试与文档固化）：同样独立。
3. G1（json_loader + generator/execution 改造 + `pipeline_config.py` 顺序常量与 `pipeline.yaml` 键序）：核心改动，前面两步完成后一次切换。
4. 文档与 CLAUDE.md/AGENTS.md 同步。

### 6.2 回滚

- G1 回滚：恢复 md 的 DDL 读取路径与前置检查、`pipeline_config.py` 的 `METADATA_STEP_ORDER` 与 `pipeline.yaml` 键序；JSON 侧无副作用。
- 回滚本文的 MD 输入切换时，不回滚 doc 16 的 DDL LLM 默认值；如要单独撤销 doc 16 的该决策，必须同步停止把 `comment_source="ddl"` 解释为“数据库原始注释”。

### 6.3 迁移

- 已有 `output/ddl/*.sql`（可能含 LLM 注释）必须**重跑 `--step ddl`（LLM 关闭）**才能得到纯原始注释；随后重跑 `--step json`、`--step md`、`rag_load --type table_schema`。
- 不提供自动清洗旧 DDL 的工具：旧文件不追溯，重跑即覆盖（`--clean` 语义不变）。

## 7. 风险

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| JSON 缺少独立精度字段 | 普通表 `numeric(p,s)` 的现有精度说明行会消失 | 按 §3.5 / §8 D2 从带修饰符的 `data_type` 还原基础类型、精度与小数位；验收项 8 必须通过 |
| JSON 样例值与 DDL 原文不同 | 小数尾零丢失（`18.00` → `18.0`）；DDL 无样例块时 JSON 可能回退数据库采样（`null` → 真实值）；含 `null` 的整数列变浮点（`1` → `1.0`） | §5 验收项 1 明确接受这**三类**示例值差异，不要求整份 md 逐字节一致 |
| md 文本变化影响向量库与域划分 | `table_schema_embeddings` 的 `object_desc`、domains 摘要变化 | 验收项 5 要求重跑并人工复核 |
| ddl 关 LLM 后 rel 评分输入变化 | rel 仍读取 JSON，但其中供 `comment_similarity` 使用的注释来源由 ddl 阶段 LLM 补齐转为 json 阶段 LLM 补齐 | 重跑 json→rel→cql，比对关系数量/置信度分布 |
| 旧产物未重跑即统计 | 治理指标口径失真 | 文档 + CLI 文案明确"需重跑"；§1.3 口径写死在文档中 |
| 三处默认值不一致 | YAML 显式配置、省略字段调用 `from_config()`、直接构造 dataclass 的行为不同 | G2 检查 YAML、`_boolean` 回退值及 dataclass 字段默认值均为关闭；有遗漏就先补齐，并测试三种入口 |
| 旧 JSON 残留被继续渲染（默认 pipeline 不清理） | 源库删表后 MD 仍生成该表，`--generate-domains` 也会读到；改造前"旧 DDL 残留"是同一性质 | §3.4 已写明口径：不改 clean 默认，需要精确产物时显式 `--clean`；验收 10 记录该已知行为 |
| 继续模式下 JSON 失败后 MD 仍执行 | MD 可能读旧 JSON 或本轮部分 JSON，产物不可信 | §3.4 已定：不新增依赖跳过，继续模式只用于排查，下游产物不视为本轮结果；验收 10 记录 |

## 8. 已定决策（实施按此执行，无需再确认）

- **D1｜md 不回退读 DDL**：`--step md` 只认 JSON。JSON 缺失即前置失败（§3.4）。理由：单一数据源、失败要响亮；保留双路径会让"md = JSON 的渲染"这个不变量失效。
- **D2｜必须复现普通表的数值精度说明**：JSON v3 的 `column_profiles` 没有独立的 `numeric_precision` / `numeric_scale`，但 `data_type` 已含 `numeric(p,s)` / `decimal(p,s)`。JSON→`ColumnInfo` 映射时解析为基础类型与独立精度、小数位，交给现有 `format_data_type()` 和精度说明逻辑渲染；既要保留 `numeric(10,2)` 类型显示，也要 1:1 复现普通表原有的“精确到小数点后2位”说明行，且不能重复追加 `(10,2)`。视图/物化视图的裸 `numeric` 原本就没有该说明行，保持不输出；解析不到明确 `(p,s)` 时按现有裸类型行为处理。本次无需修改 JSON schema。
- **D3｜md 正文不标注来源**：不在表/列描述里写"（LLM 生成）"。理由：md 文本会经 `MDParser.get_table_description()` 整体进入 Milvus 的 `object_desc`，标注会污染检索语料；治理统计只读 JSON 的 `comment_source`（§1.3）。如产品上需要可视化，另立需求、以独立章节实现。
- **D4｜JSON 注释补齐沿用现有 LLM 路径**：`--step json` 的注释补齐继续走 `json_generation.comments` + `json_llm.llm` 模块级覆盖，不新增模型配置。
- **D5｜本次不加 JSON 汇总块**：不在 JSON 里加 `comment_stats` 之类的汇总字段（属于 JSON schema 变更）。平台若需要覆盖率，先按 §1.3 口径扫文件统计；确有需要再单独立项。

### 实施时需验证（不阻塞，属于落地检查项）

1. JSON 还原出的 `TableMetadata` 渲染 md 后，按 §5 验收项 1 比对结构与注释，并按验收项 8 检查普通表精度说明；覆盖 table / view / materialized_view 三类对象。示例值单独核对允许的尾零和数据库采样回退差异。
2. 复核 doc 16 的默认值变更：YAML、`from_config()` 的 `_boolean` 回退值与 dataclass 字段默认值均关闭 DDL LLM；默认运行 `--step ddl` 的摘要统计（LLM 请求数、注释成功/失败）全为 0，且 DDL 的 `SAMPLED_RECORDS` 块仍在（`output.ddl_options.sample_records.enabled: true` 不受影响）。若前置检查未通过，先修正默认值再继续本文实施。
3. `rel` 仍读取 JSON；其中 `comment_similarity` 使用的注释改由 json 阶段 LLM 补齐后，核对关系数量与置信度分布的变化范围（重跑比对，属预期变化而非缺陷）。
