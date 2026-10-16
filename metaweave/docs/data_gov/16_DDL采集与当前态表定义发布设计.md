# 16. DDL 采集与当前态表定义发布设计

状态：设计稿，尚未实施。日期：2026-10-01。

## 1. 决策与范围

本设计将 `--step ddl` 纳入数据源采集作业，并把成功发布的 DDL 写入平台库 `public.table_definition`。同时将已有 `public.db_profile` 从“每个 `publish_id` 保存一版”改为“每个 `source_id` 只保存最后一次**成功**发布的内容”。采集、发布仍是两个动作：采集写作业目录和 `job_info`，发布读取指定作业的产物并写画像表与 `publish_info`。

| 决策 | 本文规则 |
| --- | --- |
| 画像内容 | `db_profile`、`table_definition` 只存当前内容；重复发布覆盖同一数据源相同步骤的旧内容，不保留内容历史。 |
| 发布记录 | `publish_info` 继续按 `(publish_id, step_name)` 保存每次尝试的历史。`publish_id` 是一次发布动作的 ID，也是内容来源字段，**不是**画像内容表的主键。`job_info` 保持 `(job_id, step_name)` 主键，不含 `publish_id`。 |
| 独立步骤 | 每个步骤有自己的当前成功发布。db、DDL，以及以后 MD/JSON 的当前内容可以来自不同 `publish_id` 和 `job_id`；页面不得声称整套画像属于一个统一发布版本。一次请求仍可用同一个 `publish_id` 记录多个步骤结果。 |
| 失败 | 失败尝试只追加 `publish_info(status='failed', is_current_publish=false)`；不替换已有画像，不取消该步骤此前成功记录的 current。页面可同时有“当前可用画像”和“最近一次尝试失败”。 |
| DDL 与 MD | 继续作为独立采集步骤、独立目录、独立发布状态。前端以后可以把它们作为一组展示；本文只实施 DDL。MD 的采集、发布、表写入与页面展示由后续文档规定。 |
| DDL 注释来源 | 本阶段将 DDL LLM 默认值改为关闭，保留显式开启能力；后续 [doc 17](17_md步骤依赖json产物改造方案.md) 实施前会检查该默认值，JSON 步骤负责补齐缺失注释。 |
| 样例数据 | DDL 原文中的少量 `SAMPLED_RECORDS` 一并保存，不在发布时剥离；本阶段不提供 DDL 原文读取或展示接口，展示由后续文档规定。它们是源库的真实采样值，不写入日志或发布错误消息。 |

本文修订 [doc 9](9_db采集作业目录与数据库画像表设计.md) 的 `db_profile` 主键和版本保留规则、[doc 10](10_平台画像加载命令meta_load设计.md) 的独立 CLI 写入规则、[doc 12](12_发布信息表与发布动作设计.md) §6 中“`meta_load` 画像行视为未发布”以及失败发布翻旧 current、`db_profile` 逐次插入和按 `publish_id` 读取画像的规则、[doc 13](13_数据库画像页与安装平台字段设计.md) §8.2 中“`meta_load --type db` 只插画像、不写 current、页面仍是 `not_published`”及其写入路径对照表，以及 [doc 7](7_数据源查看与删除设计.md) §3 中“删源只删登记行、历史画像不清理”“不检测采集是否仍在运行”“不检测独立 CLI 作业、也不加锁或检测”的规则；这些冲突处以本文为准，未受管 CLI 的例外见 §6。`db` 产物 `database.json` 的格式、字段校验以及 `publish_info` 的历史记录主键不变。

本次不实施 MD、JSON 表/字段画像、RAG 加载、DDL 页面渲染或历史画像回看接口。旧发布作业列表仍可显示历史动作，但旧 `publish_id` 不再保证能查到当时的画像内容；“重新执行”仍按原作业配置重新发布当前产物，不能被解释为恢复旧快照。

当前 API 没有鉴权或按用户授权的中间件，CORS 不能充当访问控制。本阶段不新增返回 `table_definition.define_text` 的 API；受管作业目录和平台库中的 DDL 原文须由部署侧限制文件、数据库账号与网络访问。**后续设计修订：**[doc 19.2](19.2_Web表画像数据字典与数据画像展示设计.md) §8.4 已决定其本版新增的 DDL SQL 与样例读取接口不增加鉴权或按用户过滤，具体返回范围及页面行为以 doc 19.2 为准；本段原先设想的“先实现鉴权与授权”不再作为 doc 19.2 的实施前置条件。

## 2. 现状与复用点

1. `db_profile.publish_id` 目前是主键，同一来源每次成功发布插入新行；`backend/assets/catalog.py` 先取 `publish_info` 的 db current，再按 `publish_id` 找画像。失败发布会把旧 current 翻为 false，即使旧画像行仍在，资产页也显示失败。`backend/profile_loading/db_loader.py:load_db_profile()` 可由 `meta_load --type db` 直接插画像，却不写 `publish_info`。
2. `--step ddl` 已由 `metaweave/core/metadata/execution.py:execute_ddl_json_md()` 和 `MetadataGenerator.generate(step='ddl')` 执行，`OutputFormatter.generate_ddl()` 生成普通表、视图和物化视图的 SQL，包含注释、约束/索引及按配置限制的样例记录。当前 `configs/metadata_config.yaml` 将其写到全局 `output/ddl`，`ddl_generation.comments.llm_enabled` 默认是 `true`；CLI 明确拒绝 DDL 携带 `--source-id`、`--job-id`，Web 采集与发布也只允许 `db`。

上述“Web 采集与发布也只允许 `db`”是本文编写时的现状描述。本文实施后两侧已支持 `db/ddl`；[doc 18](18_Web受管JSON画像采集设计.md) 实施后，Web 采集侧还将支持 `json`，发布侧仍只支持 `db/ddl`。
3. `metaweave/core/metadata/ddl_loader.py:DDLLoader` 已能解析生成的 DDL，提取对象类型、schema、对象名和样例记录，可复用其解析逻辑。它的 `load_all()` 只负责扫描与解析，不负责工作区归属、符号链接、文件名身份或本次发布的完整性校验；这些仍由平台加载入口负责。
4. `backend/jobs/repository.py` 已提供 `assert_workspace_owner()`、`start_step()`、`finish_step()` 和步骤状态查询；`backend/data_sources/db_step_service.py` 已有数据源存在/启用、连接信息解密及作业状态的准备链。DDL 执行复用这些能力，不另建一套作业状态表，也不复制 DDL 生成器。
5. DDL 生成结果不能只看 `result.success`：当前逐对象处理失败时，DDL 分支可能只增加 `failed_tables`；`OutputFormatter._save_ddl()` 写文件失败时还可能返回 `None`。受管采集必须检查失败数与实际输出文件数，防止把不完整目录标成成功并发布。

## 3. 当前态表与版本含义

### 3.1 `db_profile` 改为每源一行

`source_id uuid PRIMARY KEY` 代替现有 `publish_id` 主键；`publish_id uuid NOT NULL` 保留为“这一行最后一次成功写入来自哪次发布”的普通字段。`job_id`、采集时间、数据库概览字段及原有字段校验继续保留，`db_name` 是采集时的库名快照，不能作为主键。不同数据源可能同名，且数据源登记信息可能修改。

现有数据按 §6 先去重并修复 current 标记，确认 `source_id` 无重复后，再把主键由 `publish_id` 改为 `source_id`；`publish_id` 不设唯一约束。应用不能在旧代码仍写入时直接执行下面的结构变更。

管理员执行迁移前先用 `\d public.db_profile` 或查 `pg_constraint` 核对实际主键约束名；下方 SQL 适用于约束名确为 `db_profile_pkey` 的环境，若不同须按实际名称调整，不能直接套用。

```sql
ALTER TABLE public.db_profile DROP CONSTRAINT db_profile_pkey;
ALTER TABLE public.db_profile ADD CONSTRAINT db_profile_pkey PRIMARY KEY (source_id);
```

一次 db 发布成功时，在同一平台库事务中锁定数据源行、翻旧 db 成功 current、按 `source_id` 替换 `db_profile` 一行、插入新的 `publish_info` 成功且 current 行，然后提交。可以用事务内 `DELETE + INSERT`；统一的是**旧内容与新内容不能在提交边界外混合**，不是要求所有表必须使用相同 SQL。未来若给 `db_profile` 建入站外键，可改用只更新当前行的 upsert，而不改变外部语义。

失败记录仍保留发布历史，但 `is_current_publish=true` 只表示该 `(source_id, step_name)` **最后一次成功且仍在使用的内容**。失败尝试为 false，旧成功 current 和旧画像保持不变；不能按 `created_at` 最大的尝试行直接决定资产页有无画像。现有 `publish_info_current_idx` 的每源每步骤至多一条 current 约束继续有效。

### 3.2 `public.table_definition`

一行保存一个数据源中一个数据库对象的一种**当前**定义文本。DDL 和未来的 MD 分别占一行，避免分别发布时互相覆盖；本阶段只写 `define_type='ddl'`。

```sql
CREATE TABLE public.table_definition (
    source_id uuid NOT NULL,
    schema_name text NOT NULL,
    object_name text NOT NULL,
    define_type text NOT NULL,
    db_name text NOT NULL,
    object_type text NOT NULL,
    define_text text NOT NULL,
    job_id uuid NOT NULL,
    publish_id uuid NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT table_definition_pkey
        PRIMARY KEY (source_id, schema_name, object_name, define_type),
    CONSTRAINT table_definition_type_ck
        CHECK (define_type IN ('ddl', 'md')),
    CONSTRAINT table_definition_object_type_ck
        CHECK (object_type IN ('table', 'view', 'materialized_view')),
    CONSTRAINT table_definition_names_ck
        CHECK (length(btrim(schema_name)) > 0 AND length(btrim(object_name)) > 0
               AND length(btrim(db_name)) > 0 AND length(btrim(define_text)) > 0)
);
```

| 字段 | 含义 |
| --- | --- |
| `source_id`、`schema_name`、`object_name` | 数据源内对象身份；schema 和对象名按产物原样保存，不以 `db_name` 或 `publish_id` 区分当前行。 |
| `define_type` | 文本类型。本阶段仅产生 `ddl`；允许值 `md` 只是为后续独立步骤预留表结构，不代表 MD 已可发布。查询 DDL 必须带 `define_type='ddl'`。 |
| `db_name`、`object_type` | 本次采集产物的库名与对象类型；DDL 当前支持 table、view、materialized_view。 |
| `define_text` | 整份 UTF-8 DDL 原文，包含原有注释和可配置的少量样例记录；不执行其中 SQL，不另存第二份样例数据。 |
| `job_id`、`publish_id` | 最后一次成功发布这**一行**的采集作业和发布动作；二者都不是内容历史主键。以后同一对象的 MD 行可以带不同的两个 ID。 |
| `loaded_at` | 这一行当前内容写入平台库的时间；再次发布并整批替换时重新生成。发布尝试的审计时间以 `publish_info` 为准。 |

`table_definition` 是定义文本表，不承担 JSON 表级指标或字段画像；以后这些内容应使用各自的当前态表，不让 `define_type='json'` 混入 `define_text`。DDL 中已有的 `COMMENT ON TABLE` 是定义文本的一部分；未来要单独展示可检索的表注释时，需再定义字段来源与更新步骤，本文不从 SQL 字符串中派生第二个注释列。

## 4. DDL 采集：受管作业目录与成功条件

本阶段把三处 DDL LLM 默认值同步改为关闭：`configs/metadata_config.yaml` 中的 `ddl_generation.comments.llm_enabled: false`、`metaweave/core/metadata/generation_config.py` 中 `GenerationConfig.from_config()` 调用 `_boolean(ddl_comments, "llm_enabled", False, ...)` 的回退值，以及 `DdlCommentsConfig.llm_enabled = False`。`from_config()` 会显式构造 dataclass，只改字段默认值不能改变省略 YAML 字段时的行为。保留显式 `true` 的开关与现有 `overwrite: true` 配合 `llm_enabled: false` 的互斥校验。默认生成的 DDL 只承载数据库原始表/列注释；缺失注释由后续 JSON 步骤补齐并标记来源。DDL 文本不区分注释来自数据库还是 LLM 补齐；`DDLLoader` 解析非空注释时统一标记 `comment_source="ddl"`。因此显式开启 DDL LLM 后，该值不能再解释为“数据库原始注释”。本次默认值变更同时作用于未受管 CLI 和受管 DDL，不额外增加受管专用配置。

受管 DDL 采集使用 `output/<source_id>/<job_id>/ddl/`，文件仍沿用 `database.schema.object.sql`。文件头注释字段固定为 `-- Database: <库名>` / `-- Object Name: <schema>.<object>` / `-- Object Type: <table|view|materialized_view>` 三行（另有条件写出的 `-- Comment:` 与 `-- Generated:`）；不按对象类型使用专名标签。解析侧不读这三行（身份取自 `CREATE` 语句或 `OBJECT_METADATA` 块），**发布侧只读 `-- Database:`**（唯一、且与文件名前缀一致）。增加与 `resolve_db_output_dir()` 同级的 DDL 目录解析函数：规范化两个 UUID，拒绝工作区路径中的符号链接与越界，清理和写入共用该函数。**现有未受管 CLI、pipeline 和文件型后续步骤继续使用配置中的 DDL 目录（默认 `output/ddl/`）；这些文件不属于任何 `job_id`，不能供 Web 发布读取。** 旧全局文件不自动迁移。未改造的 MD/JSON 步骤不会自动读取受管 DDL 工作区；它们的作业路径与依赖关系由后续文档处理，不能因为受管 DDL 已成功就声称它们消费了同一批文件。

本阶段受管 DDL 只保证当前文件命名与解析链能一致还原的对象；PostgreSQL 标识符是否需要加引号，本身不是发布拒绝条件。库名、schema 名或对象名含点号时，文件名将不符合严格三段式；对象名含空白时，解析身份将与文件名不一致。若这类对象采集成功，发布校验将拒绝整批，并明确返回具体文件或对象及原因。保留大小写的名称可能通过身份校验；非 ASCII、无空白的名称也可能通过，但现有 `DDLLoader` 的 `COMMENT ON` 正则无法完整解析非 ASCII 等特殊名称的注释。`define_text` 仍保存 DDL 原文，后续 JSON/MD 步骤不能假定这类注释已被正确解析。本阶段不扩展生成器的文件名转义和 DDL 解析能力。

`--step ddl --source-id <uuid> [--job-id <uuid>]` 进入受管模式：省略 `job_id` 时新建；指定时要求工作区已存在且属于该数据源。Web 采集接口复用同一服务入口，允许单独选 DDL，或在一次请求中选 `db`、`ddl` 并沿同一 `job_id` 顺序执行；各步骤分别记录结果，不把一次采集请求视作单个数据库事务。多步请求中，本次请求内还没有任何步骤执行并计入结果时，准备链配置错误（含指定的工作区不存在）仍整体抛出——重跑请求自带的 `job_id` 不代表已有步骤执行，不能据此返回部分结果；已有步骤在本请求内执行并计入结果后，后续步骤报准备链配置错误（含数据源被并发禁用）不再整体抛出，而是把该步骤按 failed 计入结果、连同 `job_id` 与已完成步骤的结果一并返回，避免隐藏第一步的成功产物。后续步骤的平台库故障（结果未登记 `job_id`，步骤状态以平台库为准）同样按 failed 计入结果返回部分结果；仅首步的平台库故障仍整体返回 503。旧 `metaweave metadata --step ddl` 未带 `--source-id` 时继续按现有配置输出，不写 `job_info`，也不能直接发布。受管模式本阶段不接受显式 `--schemas`、`--tables` 或 `--clean`：前两者会缩小采集范围，后者与受管模式每次自动清理本次 DDL 目录的行为重复。CLI 应在创建工作区或启动作业前对这些组合抛出带提示的 `click.UsageError`（退出码 2），不能静默忽略。现有 `_MODE_SCOPED_PARAMS` 将它们列为 step 模式有效参数，须为受管 DDL 组合新增硬校验。受管 DDL 每次针对数据源登记的全部选中 schema，按现有对象类型及排除规则生成完整的纳入范围 DDL 集合，便于发布时准确替换该数据源的旧 DDL 行；未受管 CLI 的筛选和 `--clean` 语义不变。

受管入口读取登记数据源的连接参数和选中 schema，复用现有解密、配置校验和 `job_info` 状态函数。给共享 `execute_ddl_json_md()` / `MetadataGenerator` 传入**本次内存配置快照**，覆盖连接参数及全部输出目录键：`output.output_dir`、`output.ddl_directory`、`output.markdown_directory`、`output.json_directory`，不得修改 `configs/metadata_config.yaml`。`OutputFormatter` 当前初始化时会创建 DDL、MD、JSON 三类目录；快照里的所有输出目录都应落在同一 `job_id` 工作区，不能因为执行 DDL 顺带创建/使用全局 MD、JSON 目录。本次只生成 DDL 文件，不执行 MD 或 JSON。

运行顺序：完成参数、来源和目录校验 → `start_step(..., 'ddl')` 标记 running → 在**本次作业的 DDL 目录**清理上次受管 DDL 文件 → 调用既有 DDL 生成器 → 核对结果 → 采集成功时把本次完整文件清单写入目录内的 `manifest.json`（非 `.sql` 后缀，不进发布 glob；随目录每次清理后重写）→ `finish_step(..., 'ddl', success/failed)`。清单写入失败时该次采集判 failed：没有清单凭据的产物不可发布。受管 DDL 每次重跑都生成完整文件集合，不能因为用户未传 `--clean` 就保留上次不再存在的对象文件；目录清理仅限已验证的当前 `source_id/job_id/ddl/`。失败不改平台库的当前已发布定义；该作业的 DDL 状态为 failed，不能以残留或部分生成的文件再次发布。旧 CLI 模式保留原 `--clean` 语义。

受管 DDL 成功条件同时满足：`result.success` 为真、`failed_tables=0`、本次实际处理对象数大于零、实际 `.sql` 输出数等于成功处理对象数，且每个输出都在本次受管 DDL 目录内。选中范围内没有对象时**有意判为 failed 且不可发布**，避免一次空集发布删除已有当前 DDL；应向用户说明“选中范围内没有可采集对象”。写文件返回 `None`、部分对象失败、空输出或状态写回失败都不能标为 success。对象枚举与字段/约束/视图定义等结构提取查询失败时必须让步骤失败，**不得以空列表或空集合表示异常**——空集合只表示查询成功且确实无对象；否则多 schema 采集时现存 schema 的产物会掩盖缺失整个 schema 的不完整结果，发布再整批替换当前表定义。该收紧作用于共享查询入口（连接器与提取器），未受管 CLI 的 ddl/json 生成同步适用；仍吞查询异常返回空集的旧枚举接口（如仅服务未改造步骤的普通表枚举）不在本阶段调整范围。日志只写 `source_id/job_id`、对象名、错误类别和受控路径，不写 SQL 原文、样例值、连接密码；元数据提取、采样与格式化链路的异常日志同样只记对象名与错误类别，不记异常原文或解析到的数据值。显式开启 DDL LLM 时该约束覆盖注释链路：prompt 与 LLM 响应含样例数据，注释生成器与 LLM 服务的异常日志只记对象名与错误类别，共享 LLM 服务的其他模块（JSON 增强、关系推断等）同步适用。平台依赖缺失与参数错误继续使用明确的 CLI 错误出口。

## 5. DDL 发布与加载策略

预检和执行复用现有 `GET .../publish-check`、`POST .../publications`，新增 `ddl` 为可发布步骤；前端只在预检返回 `publishable=true` 时允许选择。执行时重新校验数据源启用、工作区归属、`job_info(job_id,'ddl').last_status='success'` 和受管目录可读。预检可只判断文件就绪，真正发布必须完整读取并验证，不信任预检结果。

发布读取 `output/<source_id>/<job_id>/ddl/` 中的全部本次 `.sql` 文件，不读取全局 `output/ddl/`。目录现状必须与采集清单 `manifest.json` 完全一致：缺文件、多出清单之外的 `.sql`、清单缺失或不可读都整批拒绝——目录只反映"现存"文件，缺件若无清单凭据会被当作完整集合发布，无提示缩小当前表定义。逐文件要求普通、非符号链接、UTF-8、非空、严格的当前数据库三段式文件名。复用 `DDLLoader` 解析 SQL 中的 schema、对象名、对象类型及样例记录；平台加载入口自行核对文件名与 `-- Database: ...` 头部行，并将解析身份与文件名比对，拒绝重复对象、混入其他数据库、解析失败或无法识别的文件。`DDLLoader` 不读取头部库名，`database_name` 是构造时传入的参数；`load_all()` 也不提供上述平台边界校验，不能直接把其返回值视为可发布输入。文件读取完成后先在内存中形成整批行，任一文件失败都不触碰当前 DDL 行。DDL 原文只作为数据写入 `define_text`，绝不向平台库执行这些 SQL。

成功事务按同一 `(source_id, 'ddl')` 串行化，并完成以下操作后一次提交：

1. 取得数据源行锁，重新确认它仍属于本次发布且可用；翻旧 DDL 成功 current 为 false。
2. `DELETE FROM public.table_definition WHERE source_id=:source_id AND define_type='ddl'`。这一步**不删除 MD 行**，也不改 `db_profile` 或未来 JSON 画像表。
3. 插入本次全部 DDL 行，各行的 `publish_id`、`job_id` 相同；插入 `publish_info` 的 DDL 成功且 current 行。
4. 提交后新 DDL 集合整体可见。任何写入或 current 切换失败都回滚，旧 DDL 与旧成功 current 保持不变。

发布输入缺失、损坏或数据库写入失败时，按现有发布接口的受控结果写一条 `failed` 历史行，但 **`is_current_publish=false`，不翻旧 current，不删除画像**。源行锁下重查发现数据源登记已不存在时，Web 返回 `404 not_found`；发现登记已禁用时，Web 返回 `409 source_disabled`。CLI（`meta_load --type db`）遇到这两种情况分别报告“数据源不存在”或“数据源已禁用”，并以非零状态退出。两种情况均不新增 `failed` 历史行，也不改画像或 current；与前置检查的对外行为一致。无法记录失败时返回请求级错误，不能假称已有失败记录。`publish_id` 冲突仍按 `publish_info` 主键识别；`db_profile_pkey` 从 publish UUID 冲突变成 source 主键语义，相关冲突分类和测试须同步调整。成功读取当前 DDL 时按 `source_id + define_type='ddl'` 查询，并可与 DDL 的 current 成功发布行核对 `publish_id`；不同步骤当前 ID 不同是正常状态。

发布分预检与执行两个阶段核对步骤新鲜度：预检时快照各成功步骤的运行标识（`job_info` 步骤行的 `updated_at`，`start_step`/`finish_step` 每次写入都会刷新），提交事务在源行锁下复核状态仍为 success 且标识未变。预检不通过时整体拒绝（400 step_not_ready），未提交任何内容。执行阶段读取输入之后步骤被重跑（标识已变，哪怕重跑已完整走完）时按**部分成功**处理：已提交步骤保持成功且 current 有效，被拒步骤在响应中记为 failed 并写一条非 current 的 `failed` 历史行（同一 `publish_id` 可查，不留无记录的发布 ID），响应返回 `publish_id` 与各步骤结果，不再整体抛错。CLI `meta_load --type db` 无发布 ID 语义，读取后被重跑时直接报告“已被重跑”并建议重跑结束后重新加载，不写历史行。

## 6. `db_profile` 现有加载链的必要改动

`backend/profile_loading/db_loader.py` 保留 `database.json` 的路径与字段校验函数，改造“插入一行”的内部函数为“在调用方事务中按 `source_id` 替换当前行”。`backend/publishing/publish_service.py` 在同一事务中写画像与成功 `publish_info`；失败事务只插入非 current 的失败历史行。`backend/assets/catalog.py` 由按 `publish_id` 查画像改为按 `source_id` 查，并核对画像行 `publish_id` 与 db 当前成功记录一致；最新一次失败可在发布历史展示，不把仍有效的旧画像隐藏为 `failed`。发布首页和列表仍按 `publish_info` 展示尝试历史，不推断旧画像内容仍存在。

`metaweave meta_load --type db` 不能继续只覆盖 `db_profile` 而不记录发布：否则手工调试会悄悄改变 Web 当前画像，却无对应当前发布行。CLI 与 Web 复用同一个 db 当前态提交函数；CLI 成功也生成 `publish_id`，在同一事务写成功 `publish_info` 并替换画像。失败不覆盖旧内容；能够记录失败尝试时写非 current 历史行；源行锁下发现登记已删除或已禁用时按 §5 的例外处理。`--clean` 仍只在成功提交后清理本次 db 输入目录；清理失败须明确报告“已入库、清理失败”，不能回滚已提交画像。

**数据源删除规则同时修订 doc 7**：`delete_output` 仍只决定是否删除 `output/<source_id>/`，不决定平台画像表的处置。删除流程顺序固定为：

1. 查询并确认数据源登记仍存在；不存在立即返回 `404 not_found`，不碰工作区。
2. 开启平台库事务并取得该数据源行锁；锁下用 `list_source_step_rows(session, source_id)` 查询该源的**全部作业、全部步骤**，任一行的 `last_status='running'` 即返回 `409 source_busy`，回滚并保持登记、画像及工作区不变，不能只检查某个 `job_id`。
3. 仍持有源行锁时，若 `delete_output=true`，按 doc 7 的路径约束删除 `output/<source_id>/`；文件删除失败则回滚，不删除登记或画像。
4. 在同一平台库事务中删除该源的 `db_profile` 当前行和 `table_definition` 所有定义类型行，将 `publish_info` 的 current 标记全部置为 false，最后删除 `db_source_info` 并提交。`publish_info` 历史尝试行和 `job_info` 历史步骤行保留作审计，不新增数据库外键级联。

数据库事务失败则画像清理与登记删除一并回滚；若用户勾选删除工作区，文件先删而数据库后提交失败的既有非原子边界仍按 doc 7 报告。选择在持有源行锁和未提交事务时删除工作区，接受大工作区会延长事务、阻塞该源采集/发布/删除，以及可能触发数据库事务空闲超时的代价；超时后目录可能已删除而数据库回滚，沿用上述非原子失败处理。残留的 `running` 状态须先由管理员核实和修复，不提供强制删除。受管 `start_step()` 也必须在取得同一源行锁并确认登记仍存在后才写 `running`，避免与删除竞争；发布成功与失败记录写入同样须在源行锁下重新确认登记仍在。Web 采集与带 `--source-id` 的受管 CLI 作业写 `job_info`，可由上述检查发现；不带 `--source-id` 的未受管 CLI 不写 `job_info`，仍不在检测范围内，其与删源的并发安排须由运维手工协调。删除后历史行虽保留 `source_id`，但不再有 current，资产目录和画像页不展示该源；不得再由历史行恢复旧画像。

迁移已有数据时先备份 `db_profile` 与 `publish_info`。先找出 `db_source_info` 已不存在的孤立源：删除其所有 `db_profile` 行、清除对应 `publish_info` current，保留历史行，并记录处理数量。对仍登记的每个 `source_id`，从**有匹配画像行**的 db 成功发布历史中按 `publish_info.created_at DESC, publish_id DESC` 选一行作为保留内容；`publish_id` 只在 `created_at` 相同时用于确定性裁决，不代表更晚的真实发布时间，不能单凭 UUID 大小选“最新”。若只有旧 CLI 写入而无 `publish_info` 对应行，列为人工确认项，不静默猜测。去重后将 `db_profile` 主键改为 `source_id`，原 `publish_id` 降为非主键且保留所选来源；相应 db 成功历史中仅所选行标为 current，失败历史均为 false。迁移与应用切换期间停止旧写入者，不允许旧代码按旧主键继续插入；迁移前后核对每源最多一行画像、每源每步骤最多一个成功 current，以及该 current 与画像的 `publish_id` 一致。切换到新代码后做一次重复发布冒烟，确认 `db_profile` 每源一行、`publish_id` 指向最新成功发布；新代码的替换提交观测到同源多行会记录警告日志，出现即说明迁移顺序失误。

## 7. 计划改动与验收

| 文件或模块 | 改动 |
| --- | --- |
| `backend/persistence/models.py`、平台库建表/迁移 SQL | 调整 `DbProfile` 主键；新增 `TableDefinition` 模型和 §3.2 的表。平台表由管理员执行迁移，应用启动不自动建表。 |
| `configs/metadata_config.yaml`、`metaweave/core/metadata/generation_config.py` | 将 DDL LLM 的 YAML、`from_config()` 中 `_boolean` 回退值及 dataclass 字段默认值三处同步改为关闭；保留显式开启与原有互斥校验。 |
| `metaweave/core/clean_targets.py` | 增加受管 DDL 作业目录解析/清理边界，复用现有项目根路径规则。 |
| `metaweave/cli/metadata_cli.py`、`metaweave/core/metadata/execution.py` | `--step ddl` 接受受管 `source_id/job_id`；显式 `--schemas`/`--tables`/`--clean` 报 `UsageError` 而不忽略；给现有生成入口传每次运行的配置快照，核实成功条件；保留未受管 CLI 行为。实施核对：快照经 `MetadataGenerator(config_path, config=snapshot)` 注入即满足，`execution.py` 无需改动。 |
| `backend/data_sources/ddl_step_service.py`（新增）、`backend/jobs/collection_run.py`、`backend/jobs/repository.py` | 复用数据源、工作区和步骤状态逻辑，运行受管 DDL；`start_step()` 与删源共用源行锁并确认登记存在；允许 Web 选择 DDL。 |
| `backend/data_sources/registration_service.py`、`backend/data_sources/repository.py`、`backend/api/errors.py`、`frontend/src/api/http.ts` | 删源时同一事务删除当前画像、清除全部 current 并删除登记；保留作业/发布历史，运行中步骤返回 `409 source_busy` 和明确文案，前端显示该错误。原有可选工作区删除语义不变。 |
| `backend/profile_loading/ddl_loader.py`（新增） | 读取和验证指定作业的 DDL 文件，复用 `metaweave.core.metadata.ddl_loader.DDLLoader` 的解析能力；向调用方提供整批画像行，不自行提交。 |
| `backend/profile_loading/db_loader.py`、`metaweave/cli/meta_loader_cli.py` | db 当前态替换函数及 CLI 与发布历史的一致性。 |
| `backend/publishing/publish_service.py`、`backend/publishing/repository.py` | db 及 DDL 的当前态事务、失败不切断旧 current、并发冲突处理和来源校验；成功与失败写入均在源行锁下确认登记仍存在。 |
| `backend/assets/catalog.py`、`backend/api/schemas.py`、`frontend/src/views/PublishView.vue`、`frontend/src/views/CreateCollectionJobView.vue` | 按 `source_id` 读取当前 db 内容，增加 DDL 可选/可发布状态；保持现有端点地址和响应基础形态。此阶段不增加 DDL 详情页。 |
| doc 7、doc 9、doc 10、doc 12、doc 13 | 按 §1 的修订清单标注本文实施后的新规则；旧文档的“当前实现”描述在代码切换前仍按原状理解。 |
| 相关既有测试与新的 DDL 采集/加载测试 | 更新旧多版本、失败隐藏画像、CLI 直接插入、仅 db 可选及 DDL LLM 默认值等断言，覆盖下列验收场景。 |

验收至少覆盖：

1. YAML、`GenerationConfig.from_config()` 的 `_boolean` 回退值及 `DdlCommentsConfig` 字段默认值三处均关闭；省略 YAML 对应字段时仍关闭。默认运行 `--step ddl` 的 LLM 请求/注释成功失败数均为 0；抽查有注释和无注释的表/列，DDL 注释与数据库原始注释一致，`SAMPLED_RECORDS` 仍按采样配置生成。显式开启 LLM 的旧能力与互斥校验仍有效。
2. 同源 db 成功发布两次后 `db_profile` 仍恰好一行，字段和 `publish_id` 为第二次成功内容；`publish_info` 保留两次历史。第三次失败后内容和成功 current 不变，失败历史新增且不为 current；资产页仍可读旧成功画像。
3. `meta_load --type db` 的成功提交也同时产生成功发布记录，Web 读取到同一内容；提交失败不改旧画像，`--clean` 只在成功提交后执行。
4. 受管 DDL 的新建/复用工作区、源归属和禁用检查、running/failed/success 写回及仅在本次 DDL 目录清理；`output.output_dir`、DDL/MD/JSON 目录均落在该工作区，全局 `output/ddl`、`output/md`、`output/json` 不被受管流程读取、覆盖或创建（无数据库测试环境下由传给生成器的配置快照断言代理覆盖；生成链路改造时需补真实运行后的全局目录断言）。未受管 DDL CLI 仍可运行。
   受管 CLI 显式带 `--schemas`、`--tables` 或 `--clean` 时退出码为 2，提示参数不适用，且不创建工作区、不写 `job_info`；未受管 CLI 的筛选与 `--clean` 仍生效。
5. 单个 DDL 文件写入失败、逐对象失败、零输出、坏文件、文件名或对象身份不符、符号链接、重复对象时，作业或发布失败且旧当前画像保持不变；不能因 `result.success=true` 就误标成功。选中范围无对象时返回明确原因并不可发布；文件名不符合严格三段式（如对象名含点号），或解析身份与文件名不一致（如对象名含空白）时，即使已采集成功，发布校验仍应明确失败并报告具体文件或对象，不得部分覆盖当前 DDL。需要加引号但未触发这两类问题的名称，不要求发布失败。
6. 同源 DDL 连续成功发布两次后 `table_definition` 中只保留最后一批 `define_type='ddl'` 行，已删除对象不会残留；预先存在的 `define_type='md'` 行原样保留。每行来源 `publish_id/job_id` 与本次发布一致，DDL 原文中的少量样例记录仍可读取。
7. db 与 DDL 分别从不同 `job_id/publish_id` 成功发布时，两者各自 current 正确，互不覆盖；任何页面或 API 不把其中一个 ID 解释为整套画像版本。
8. 并发发布触发 `publish_info_current_idx` 冲突时返回 `409 publish_conflict`；`publish_info_pkey`、`db_profile_pkey` 或 `table_definition_pkey` 的意外主键冲突按内部错误处理，不误报成可重试的 current 冲突，响应与日志不泄露 SQL、样例值或凭据。
9. 删除已发布数据源后，`db_profile` 无该源行、`table_definition` 无该源任何类型的行、`publish_info` 无该源 current 行；历史 `publish_info` 与 `job_info` 保留，资产目录和画像页不显示该源。`delete_output` 仅控制工作区；该源任意 `job_id` 的任一步骤仍在 running 时返回 `409 source_busy`，尤其在 `delete_output=true` 时须先完成此检查，工作区和数据库均不变。清理事务失败时登记和画像均回滚。并发删除先取得源行锁后，Web 发布返回 `404 not_found`、`meta_load --type db` 明确报错并以非零状态退出；两者均不追加 `failed` 历史行，也不恢复画像或 current。受管采集与删除并发时，若删除先提交，随后 `start_step()` 查不到登记，采集侧报“数据源不存在”、不写 `running` 或新增 `job_info` 行；若采集先提交 `running`，随后删除返回 `409 source_busy`，不删除工作区或登记。未受管 CLI 不写 `job_info`，不以本项断言它会被拦截。
10. 前置校验后、提交前数据源被并发禁用时，Web 发布返回 `409 source_disabled`，`meta_load --type db` 明确报错并以非零状态退出；不追加 `failed` 历史行，旧画像和 current 不变。
11. 迁移前已备份 `db_profile` 与 `publish_info`，并核对实际主键约束名。迁移孤立源后，`db_profile` 无其画像行、`publish_info` 无其 current 行，历史行保留且处理数量有记录。对多版本源，仅在有匹配画像行的 db 成功发布中按 `created_at DESC, publish_id DESC` 保留首行；构造相同 `created_at` 的样例核对裁决。只有旧 CLI 画像、无匹配 `publish_info` 的源进入人工确认清单，不被静默推断。切换后核对 `db_profile_pkey` 约束于 `source_id`、每源最多一行画像、每源每步骤最多一个成功 current，以及 db current 与画像 `publish_id` 一致。
12. 本阶段新增 API 的响应与错误消息不包含 `table_definition.define_text` 或 `SAMPLED_RECORDS` 样例值；部署说明列明平台库账号、工作区文件和 API 网络访问的限制。后续 doc 19.2 的 DDL SQL 与样例读取接口按其 §8.4 的本版无鉴权决定实施；本项不再对这些新接口附加鉴权前置条件。
13. 发布对受管 DDL 目录与采集清单 `manifest.json` 做双向核对：清单缺失或不可读、清单内文件缺失、目录中多出清单外的 `.sql` 文件时整批拒绝并报告差异，不得部分覆盖当前 DDL 行。
14. 发布分阶段核对步骤新鲜度：预检时步骤未就绪整体返回 400 `step_not_ready`、不提交任何内容；执行阶段读取输入之后步骤被重跑（`job_info` 步骤行 `updated_at` 已变）时按部分成功处理，已提交步骤保持成功且 current 有效，被拒步骤在响应中记为 failed 并写同一 `publish_id` 的非 current `failed` 历史行。
