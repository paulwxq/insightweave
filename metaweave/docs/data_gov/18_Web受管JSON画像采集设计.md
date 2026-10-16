# 18. Web 受管 JSON 画像采集设计

状态：设计稿，待实施。日期：2026-10-02。

## 1. 目标与边界

在现有数据源采集页面启用 `json` 步骤，让用户通过 `POST /api/data-sources/{source_id}/collection-jobs` 对已登记的 PostgreSQL 数据源执行逐表画像采集，并把 v3 JSON 写到 **`output/<source_id>/<job_id>/json/`**。沿用现有 `MetadataGenerator.generate(step="json")` 的 DDL 读取、数据库计数/采样、规则画像、可选 LLM 增强和 `MetadataDocument` 校验；采集结果仍写 `job_info(job_id, 'json')`，与 `db`、`ddl` 共用作业列表及执行结果响应。

**本篇只做采集和文件落盘。** 不新增平台库表/迁移 SQL，不把逐表 JSON 写入平台库，不新增 `json` 发布预检或发布入口，不修改 `publish_info`，不使发布页的 `json` 步骤可选，也不新增 JSON 原文读取 API。表画像、字段画像的表结构与加载事务另写后续文档。JSON 文件中的样例值可能来自源库真实记录，不在 API 响应和日志中输出原文。

本篇承接 [doc 16](16_DDL采集与当前态表定义发布设计.md) 的受管工作区和 `ddl` 步骤、[doc 17](17_md步骤依赖json产物改造方案.md) 的 `ddl → json → md` 顺序。[doc 18.1](18.1_业务库与平台库连接边界及统一访问设计.md) 的源库/平台库连接边界与统一参数模块已于 2026-10-02 完成代码改造：连接器已接入 `sslmode`，受管 DDL 快照也已补齐该字段。本篇只复用并验证这条连接链，开发范围是 Web JSON 采集。doc 17 只改未受管 CLI/pipeline 的 JSON→MD 文件链，本篇才把 `json` 接入 Web 采集；**本篇不接入 Web `md` 步骤**。现有独立 `metaweave metadata --step json` 继续使用配置中的全局 DDL/JSON 目录，CLI 参数与未受管行为不改；本篇不新增 `--step json --source-id` 模式。

## 2. 已有能力与验证边界

| 现状 | 代码位置与结论 |
| --- | --- |
| 脚本入口 | `metaweave/cli/metadata_cli.py` 把独立 `--step json` 交给 `metaweave/core/metadata/execution.py:execute_ddl_json_md()`；共享层预检配置，再调用 `MetadataGenerator.generate(step="json")`。不需要另造 JSON 生成器。 |
| 输入与输出 | `generator.py:_process_table_from_ddl()` 从 `output.ddl_directory` 读取同名 DDL，同时查询源库的对象、行数、样例和索引；`_finalize_json_documents()` 经可选 LLM 增强后通过 `OutputFormatter.save_json_document()` 校验 v3 并原子写到 `output.json_directory`。`MetadataDocument.from_dict()` 是现有 v3 校验入口。 |
| Web 入口 | `backend/jobs/collection_run.py` 与 `backend/data_sources/ddl_step_service.py` 已有数据源、`job_id`、运行状态、受管目录和同步结果的范式；当前只允许 `db/ddl`。`frontend/src/views/CreateCollectionJobView.vue` 已显示但禁用 `json`。 |
| 受控实跑 | 2026-10-02 使用现有 `output/ddl`、源库 `public.categories`，以默认采样设置、关闭两项 JSON LLM、临时 JSON 输出目录实跑生成器：`success=True`、`processed_tables=1`、`failed_tables=0`，产出 1 份通过 `MetadataDocument.from_dict()` 的 v3 JSON。既有相关单测 30 个通过。这只证明**DDL 解析 → 画像计算 → v3 JSON 落盘**的核心生成链在该配置下可工作，不代表 Web 受管输入口径；受管 JSON 必须读取同一 `source_id/job_id` 工作区的 DDL，不能照搬本次全局 `output/ddl` 的实跑路径。没有验证默认开启 LLM 的完整 Web 请求或大数据源的耗时。本次验证没有读取或改写全局 `output/json`。 |

实跑也发现一个须在实施时处理的既有边界：当 `sampling.enabled=false`、`logical_key_detection.enabled=true` 且对象没有物理主键时，`generator.py:_process_table_from_ddl()`（JSON 路径，现约第 778—779 行）会把 `sample_data=None` 传给 `LogicalKeyDetector.detect()`，后者访问 `.empty` 抛 `AttributeError`。此时对象失败，但 `GenerationResult.success` 仍可能是 `True`。`_process_table_from_db()`（现约第 582—583 行）有同类代码，但现行 CLI/pipeline/Web 的 DDL 路径处于 `ddl_only_mode`，不会进入该块；本篇对它做防御性修复，由直调单测覆盖。两处均只在有采样数据时调用逻辑主键检测，未采样时保持无候选的语义；CLI/受管 JSON 回归覆盖实际可达的 `_process_table_from_ddl()` 路径。不因此改变正常采样或默认 LLM 配置。受管成功判定绝不能只看 `result.success`。

与行数查询相关的口径（2026-10-03 定调）：JSON 逐表链路的 `COUNT(*)` 查询失败计为**该对象失败**——查询失败不等于空表，不得把 `row_count=0` 当作事实落盘；该失败经 `failed_tables` 进入受管成功判定，与源库对象增删共用"重跑同一作业 DDL"的排障提示。`COUNT` 语句以 `psycopg.sql.Identifier` 引用 schema 与表名（对齐 `sample_data` 的既有写法），需引用的标识符不再触发语法错误路径。**采样查询失败维持现状**（返回空样本，与未采样同貌）：本篇不收紧采样降级——它产生的是缺失而非错误事实。该口径作用于共享 json 路径，未受管 CLI 同步适用。

## 3. Web 作业与 DDL 前置条件

**连接信息来源分两条链。** 独立脚本 `metaweave metadata --step json` 从 `configs/metadata_config.yaml` 的 `database` 配置取源库连接参数，其中 `${DB_HOST}`、`${DB_USER}` 等占位符可由 `.env` 提供。Web 受管采集则先用 `.env` 中的 `PLATFORM_DB_*` 连接**平台库**，以请求中的 `source_id` 查询 `db_source_info`；源库的 host、port、库名、用户名、`sslmode` 和已选 schema 来自该登记行，源库密码从该行的密文用环境变量 `SOURCE_CREDENTIAL_KEY` 解密。服务端把登记字段覆盖到仅供本次任务使用的主配置内存快照，再交给生成器；其中 DDL 快照传入登记源 `sslmode`、连接器使用该参数已由 doc 18.1 落实。因此 Web 采集连接的源库由 `source_id` 决定，**不是**由 `.env` 中的 `DB_HOST/DB_NAME/DB_USER/DB_PASSWORD` 决定。主配置中的采样、画像、LLM 等非连接设置仍继续生效。

沿用现有 `POST /api/data-sources/{source_id}/collection-jobs` 的请求与响应形状，步骤白名单增加小写 `json`，并继续去重。保留已有 `db/ddl` 行为；只有同时选中 `ddl` 和 `json` 时，服务端保证 `ddl` 先于 `json` 执行，不能信任前端传入顺序。页面把步骤展示顺序调为 `ddl → json → md → generate-domains → dim-config`，只把 `json` 从禁用改成可选，其余步骤仍按现状禁用。

JSON **只消费同一个 `source_id/job_id` 工作区的 DDL**：

1. 新建作业未带 `job_id` 时，允许单独选择 `json`、选择 `db+json`，也允许同时选择 `ddl+json`；**不以本次是否勾选 DDL 作为请求准入条件**。同时选中 DDL 时先执行 DDL，JSON 复用本次 `job_id`。JSON 执行前只检查同一 `source_id/job_id` 工作区是否已有成功且完整的 DDL 产物；若没有，在 JSON 准备阶段按现有受控错误出口报告“同一作业的 DDL 产物未就绪”，不执行 JSON 生成或清理。新作业只选 JSON 时，`run_json_step(job_id=None)` 应直接报告这一受控错误，不能先生成空工作区再报“指定的 job_id 不存在”；要单独补跑 JSON，可指定已有 DDL 成功的 `job_id`。不得自动读取全局 `output/ddl`、其他作业或其他数据源的 DDL，也不暗中自动采集 DDL。
2. 已有作业带 `job_id` 时允许只补跑/重跑 JSON；按现有 `assert_workspace_owner()` 校验归属，并要求该作业 `job_info(job_id,'ddl').last_status='success'`、DDL 受管目录与 `manifest.json` 就绪。`check_ddl_output_ready()` 可供轻量检查；正式执行前复用 `backend/profile_loading/ddl_loader.py:read_ddl_definitions()` 的清单、文件名、头部与身份校验，并用该函数返回行的 `db_name`（已与 DDL 头部、文件名核对）额外确认其与**登记源当前的库名**一致，不能仅凭目录存在或旧的 DDL `success` 状态放行。这一额外检查只用于 JSON 前置：JSON 随后要连接当前登记源执行 COUNT/采样，不能把旧库 DDL 与当前库数据混合；doc 16 的 DDL 发布仍按头部与文件名互相一致的规则校验，不在本篇改变其发布条件。不要求 DDL 已发布到 `table_definition`。
3. 同一请求中 DDL 失败时不执行 JSON 生成，也不清理旧 JSON。**后续修订（doc 19.1）**：这项阻断应由 `collection_run.py` 根据本次请求内 DDL 的执行结果负责，包含准备失败、生成失败与平台库不可用；不能只靠 JSON 准备链查看 `job_info` 最近状态，因为 DDL 准备失败可能保留上一轮的 `success`。已有前序结果和 `job_id` 时，JSON 在本次响应中记为失败并提示“本次请求的 DDL 未成功，JSON 未启动；如需使用已有 DDL 产物，请单独重跑 JSON”；首步失败且没有可返回的 `job_id` 时沿用整份请求的受控错误出口。若本次**未选择 DDL**而单独运行 JSON，其准备链仍按同作业 DDL 状态与清单检查，未就绪时提示“同一作业的 DDL 产物未就绪”。**JSON 在 `start_step()` 成功登记 `running` 之前发生准备失败时，不调用 `finish_step()`，不新建或改写 `job_info(job_id, 'json')`：原本无行则仍无行，已有行则保持上次状态；响应中的失败结果只描述本次请求。**~~当前已实现代码尚未覆盖“同请求 DDL 准备失败但旧 DDL 行仍为 success”时的阻断，实施 doc 19.1 时须补上，不能把本段修订当作已交付能力。~~（doc 19.1 已于 2026-10-03 实施：`collection_run.py` 按本次 `results` 列表阻断后继步骤，本段修订已交付。）

这意味着同一份 DDL 会解析两次：`read_ddl_definitions()` 在准备阶段完整解析，以便在清理 JSON 和启动作业前拒绝无效输入；生成器随后在 JSON 步骤中由 `DDLLoader.load_table()` 再解析以生成画像。这是本篇有意保留的取舍：优先保证前置拒绝，通常这部分解析成本低于后续源库采样和可选 LLM 调用；本次不增加跨平台加载层与生成器的解析结果缓存。

登记数据源的连接字段、已选择的 schema、密码解密及启用状态复用 DDL 准备链；配置快照只在内存中构造，不把密码或临时路径写回 `configs/metadata_config.yaml`。沿用配置中的 `include_object_types`、`exclude_tables`、采样/画像与 JSON LLM 规则，不因 Web 入口悄悄关闭 LLM 或缩小对象范围。本篇 Web 请求不暴露 `--schemas`、`--tables`、`--clean` 等临时范围覆盖：每次针对登记源的完整选中范围生成，避免把局部集合误认为完整产物。`max_workers` 沿用现有生成器默认值 4；前端本篇不新增并发度输入。

JSON 虽然最终写文件，但生成器在 `generator.py:_process_table_from_ddl()` 中仍连接源库执行 `COUNT(*)` 和采样，因此必须使用登记数据源的连接参数。本篇复用 doc 18.1 改造后的 `build_managed_ddl_config()` 和 `DatabaseConnector`，不另写连接参数构造或 `sslmode` 传递逻辑；即使主配置中的源库字段指向另一数据库，也只能连接 `source_id` 对应的登记源。登记源未指定 `sslmode` 时，`backend/data_sources/ddl_step_service.py:build_managed_ddl_config()` 已显式用 `None` 覆盖主配置中的同名字段；本篇验收只需确认这一现有规则在 JSON 链路生效。

## 4. 受管目录、执行与成功条件

在 `metaweave/core/clean_targets.py` 增加 `resolve_json_output_dir(source_id, job_id)`，使用 `resolve_db_output_dir()` / `resolve_ddl_output_dir()` 同样的 UUID 规范化、项目根边界、逐级符号链接拒绝与预期路径核对。目录固定为 `output/<source_id>/<job_id>/json/`；清理、配置快照、输出核验均使用这个 resolver。`OutputFormatter` 初始化会创建 DDL、MD、JSON 三类目录，快照中的 `output_dir`、`ddl_directory`、`markdown_directory`、`json_directory` 都必须指向**同一作业工作区**；其中 DDL 输入必须指向该作业原有 `ddl/`，不能回退全局目录。可复用 `build_managed_ddl_config()` 构造连接与目录快照，再由新 resolver 严格核对 JSON 目标；不要再复制一套解密和配置拼装逻辑。

新增 `backend/data_sources/json_step_service.py:run_json_step(config_path, source_id, job_id=None) -> dict`，对齐 `run_ddl_step()` 的错误映射与 `job_info` 生命周期。保留可选 `job_id` 以接收 Web 新作业的 JSON 单选；若为 `None`，在创建工作区或查询作业归属前抛 `DbStepConfigError("同一作业的 DDL 产物未就绪；请先执行 DDL 或指定已有 job_id")`，不伪造一个注定没有 DDL 的新 `job_id`。有 `job_id` 时执行：参数及主配置校验 → 查登记源、验证 DDL 状态/目录/清单、解密 → 验证 JSON 生成配置 → 锁下复查 DDL 并由 `start_step(..., 'json')` 标为 `running` → 只清理**本次** `json/` 顶层文件（包括旧 `manifest.json`）→ 注入配置快照调用既有 `MetadataGenerator.generate(step="json")` → 核验结果 → 锁下再次复查 DDL，写新清单并将 JSON 标为 `success`。准备阶段的配置预检须覆盖 `MetadataGenerationConfig.from_config()`、生成器的 `sampling.sample_method=limit` 校验，以及 `OutputFormatter` 构造期的 `output.json_options`、`output.ddl_options.sample_records.count`（非负整数）、`output.markdown_options.sample_value_count`（可转换为整数）校验。后三项目前要等生成器构造/运行时才会触发，晚于受管作业登记与目录清理；实施时将这些检查提取为可直接检查配置快照的无副作用校验，供准备链与原生成链共用；其中 `output.json_options` 的同一校验还须供 `JsonLlmEnhancer.__init__()` 调用，替换它目前重复的判断，不另留一份规则。提取时须保留现有输入语义：`json_options` 先经 `or {}` 规范化，空值视为未配置；`sample_value_count` 沿用 `max(1, int(...))` 的转换，不额外收紧接受范围。不要为了预检而实例化会创建输出目录的 `MetadataGenerator` 或 `OutputFormatter`。上述可静态判定的配置错误在 `start_step()` 和清理旧 JSON 前按 `DbStepConfigError` 受控拒绝。**仅在 `start_step()` 已成功登记 JSON 步骤行后**，后续执行或核验失败才调用 `finish_step(..., 'json', failed)`；准备阶段失败按 §3 处理，不对不存在的行调用 `finish_step()`。清理不触碰本次 DDL、其他作业或全局 `output/json`。采集失败时保留已生成的部分文件供排障，但不生成有效清单、不标成功；重跑从清理本作业 JSON 目录开始。

登记源准备阶段须比照现有 `run_db_step()`（`db_step_service.py`）和 `run_ddl_step()`（`ddl_step_service.py`）中的同款非空校验，自行检查 `source.username`、`source.host`、`source.db_name`；缺失时在 `start_step()` 前抛出受控的 `DbStepConfigError("数据源登记缺少必要的连接字段")`，不新增 JSON 步骤行。这两处现有检查都是各自函数内的条件语句，没有可直接调用的公共校验函数。不能仅依赖 `build_connect_kwargs()` 的必填形参：显式传入空值并不会由该函数自动校验。

受管 JSON 成功须**同时**满足：`result.success is True`、`failed_tables == 0`、`processed_tables > 0`；声明的 `.json` 输出路径去重后数量等于处理对象数，均为目标目录内真实、普通、非符号链接文件；目录现存 `.json` 集合与声明集合一致。先对目标目录中**全部** `.json` 文件做集合相等核对，再按已验证的 DDL 对象所属 schema 调用 doc 17 的 `metaweave/core/metadata/json_loader.py:JsonLoader.list_objects()`：它在枚举时通过 `_load_document()` 复用 `MetadataDocument.from_dict()` 校验 v3 结构，并核对严格三段式文件名与 `object_info.database/schema_name/object_name/object_type`；本篇只复用公开枚举接口，不复制 JSON 解析或身份校验代码。`JsonLoader` 的 `_database_files()` 会静默跳过其他库名前缀文件，因此**不能单靠 `list_objects()` 判完整性**；任何清单外或非本库的 `.json` 文件都应在目录集合核对时判失败。最终 JSON 对象集合以同一作业**已通过 `read_ddl_definitions()` 完整校验的 DDL `manifest.json`** 为准：该函数已确认清单、目录文件和解析所得行一一对应，可用本次返回的行构造期望文件名 stem 集合；JSON 侧从已核验的 `.json` 文件名取 stem，与期望集合按原样、区分大小写比较（完整 stem 为 `{database}.{schema}.{object}`），不再重新解析一遍 DDL，也不靠将 `.sql` 字符串替换为 `.json` 来比对；源库对象增删、配置范围变化或漏写一份 JSON 都判本次 JSON 失败，提示重跑同一作业 DDL 后再跑 JSON。这个范围漂移校验有意放在生成完成后：准备链只检查既有 DDL，不再连接源库枚举一次；生成器按运行时的源库对象集合处理，发现不一致前可能已执行部分或全部对象的 `COUNT(*)`、采样和可选 LLM，并留下部分 JSON 文件。本篇接受这项失败成本，不增加前置源库枚举。选中范围零对象有意判失败，不把空目录标成功。LLM 增强失败时即使规则 JSON 已落盘也标失败，不把部分增强的文件认作可发布集合。

成功核验后，使用已有 `atomic_write_json()` 写 `json/manifest.json`。清单与受管 DDL 同形状，均为 `{"files": [...]}`；列表只列本次画像 `.json` 文件名，按字典序排序并去重，**不包含 `manifest.json` 自身**。本篇在写清单前核验画像文件集合，写入失败按 `failed` 处理；写后不再重读清单做第二次自检。后续任何读取方沿用“清单与目录实际文件集合一致”规则时，目录侧的 `*.json` 集合必须先排除 `manifest.json`，再与清单中的画像文件名双向比较；其他多出的 `.json` 仍须拒绝。DDL 的清单是 `.json`、产物是 `.sql`，没有这一后缀重叠。DDL 当前的 `_write_ddl_manifest()` 使用 `write_text()`；本篇只统一清单契约，不要求改动 DDL 写入实现或为此抽取共享函数。`manifest.json` 是完整性凭据，不是画像数据。未来 JSON 发布加载器如何使用清单由后续设计确定，本篇不读取或写入平台画像表。日志只包含 `source_id/job_id`、对象名、文件名、错误类别和计数；不记录 JSON 原文、样例值、LLM prompt/响应或凭据，API 结果只返回安全的失败摘要。为使这条约束在默认开启 JSON LLM 时也成立，本篇须改造 `json_llm_enhancer.py:_parse_llm_response()` 的解析失败日志：无论 DEBUG 与否，均不写原始响应或截断片段；同一增强链的异常日志及错误摘要也不得通过异常文本、`exc_info` 或 `outcome.error` 带出 prompt、响应和样例值，只保留错误类别与定位信息。LLM 输入本就包含 `sample_records`，不能假定异常文本没有业务数据。

为避免同一作业的 DDL 在 JSON 生成期间重跑，JSON 准备时记录 `job_info` 中 DDL 步骤的 `updated_at`；调用会自行加源行锁并提交的 `start_step('json')` 前，先在**同一数据库事务、同一源行锁下**核对 DDL 仍为 `success` 且标识未变。实现时，锁下核对与 `start_step(session, ..., 'json')` 必须使用**同一个 `platform_session` 的同一事务**，由 `start_step()` 内部的 `commit()` 提交 `running` 并释放源行锁；不能在核对后先提交，再另开会话调用 `start_step()`。生成与文件核验完成后，再取得源行锁、复查 DDL 标识；只有仍匹配时才在持锁期间写清单并提交 JSON 的 `success` 状态。实现时，锁下复查、写清单和 `finish_step(session, ..., 'json', success)` 必须使用**同一个 `platform_session` 的同一事务**；`finish_step()` 内部的 `commit()` 提交状态并释放源行锁，不能把复查与状态写回拆成两个会话。文件写入与数据库提交不能组成跨资源原子事务：若状态提交失败，立即删除新清单；后续消费者也必须同时检查清单与 `job_info.json=success`。若 DDL 已重跑，本次 JSON 标 `failed` 且不写有效清单。`start_step()` 与 DDL 受管流程已经共用该源行锁，无须在采样/LLM 的整个耗时阶段持有数据库事务。JSON 完成**之后**单独重跑 DDL 不追溯修改已完成的 JSON 状态；这时若要得到与新 DDL 配套的 JSON，须再执行 JSON。人工直接改写工作区文件不受作业行锁保护；本篇沿用 doc 16 的受管目录/清单边界，不宣称文件内容具备不可变快照保证。

## 5. 页面与接口范围

- `backend/jobs/collection_run.py` 把 `json` 接入已有线程执行编排；不支持步骤的后端消息同步为“当前仅支持 db、ddl、json 步骤”（doc 19.1 实施后已更新为“当前仅支持 db、ddl、json、md 步骤”）。步骤结果仍为 `step/status/message/finished_at`，同一请求共用 `job_id`。`backend/api/routers/data_sources.py` 的路由可继续透传，无须另建 JSON 专用采集 API。
- `frontend/src/views/CreateCollectionJobView.vue` 启用 `json`，允许单选；新建作业勾选 JSON 但未勾选 DDL 时（包括 `db+json`）提示：当前作业没有 DDL 产物将无法生成 JSON，可先运行 DDL 或选择已有 DDL 成功的作业补跑。复用作业重跑选择时也允许 JSON 单选，由后端最终校验同作业 DDL 是否成功。运行中显示等待状态，响应返回后显示逐步结果；现有接口是同步等待后一次返回。把执行结果区的“点击执行后，实时显示各步骤的执行进度与结果。”改为“执行期间显示等待状态；执行完成后显示各步骤结果。”，不声称逐步或逐表实时进度。
- `frontend/src/api/http.ts` 的 `step_not_supported` 共用兜底文案改为“所选步骤暂不支持”，不在兜底中列举步骤；采集与发布请求现已设置 `preferServerMessage=true`，正常响应分别展示各自后端消息，无须扩展当前仅用于 `create/edit` 的 `mode`。采集成功的 JSON 仅表示文件和 `job_info` 就绪；页面的“下一步：执行画像发布”不能暗示 JSON 已可发布。**入口显示条件已由 [doc 20.1](20.1_表结构发布选项统一与空描述字段处理改造计划.md) §11 取代**：不再以本次 `db/ddl` 成功为条件，而是按整个作业全部现有步骤行的汇总状态判断，汇总为 `success` 才显示，`running` 或 `failed`（含本次未执行的旧步骤）时不显示。
- `backend/publishing/publish_service.py`、`backend/api/schemas.py` 中的发布步骤白名单仍为 `db/ddl`，发布侧不支持步骤的后端消息仍为“当前仅支持 db、ddl 步骤”；`frontend/src/views/PublishView.vue` 的 JSON 选项仍禁用。采集作业列表、首页可照常显示 `json` 的 `job_info` 状态，但不能把它计入成功发布数。

## 6. 计划改动与验收

| 文件或模块 | 本篇改动 |
| --- | --- |
| `metaweave/core/clean_targets.py` | 新增受管 JSON 目录 resolver；不改未受管 `resolve_clean_target()` 的路径口径。 |
| `backend/data_sources/json_step_service.py`（新增） | 复用 DDL 准备链、工作区与生成器；实现前置依赖、状态、清理、完整性校验和清单。 |
| `backend/jobs/collection_run.py` | 接入 `json`，保证同请求 DDL 先于 JSON；步骤选择允许 JSON 单选，无 `job_id` 时由 JSON 准备链报告 DDL 未就绪，带已有 `job_id` 时核对同作业 DDL 产物后可单步执行；其余沿用现有部分结果语义，不支持步骤的消息列出 `db、ddl、json`（doc 19.1 实施后已含 `md`）。 |
| `metaweave/core/metadata/generator.py`、`metaweave/core/metadata/formatter.py` | 修复 `_process_table_from_ddl()` 在采样关闭时把 `None` 传给逻辑主键检测的可达缺陷；同类 `_process_table_from_db()` 调用做防御性修复。将 JSON 采样方式及格式化器构造期的三项输出配置校验提取为准备链也可调用的无副作用函数，原生成链继续调用。 |
| `metaweave/core/metadata/json_llm_enhancer.py` | `__init__()` 复用与准备链、格式化器相同的 `output.json_options` 校验；解析失败及相关异常路径不记录原始 LLM 响应、prompt、样例值或可能包含这些内容的异常文本，保留错误类别和对象定位。 |
| `frontend/src/views/CreateCollectionJobView.vue`、`frontend/src/api/http.ts` | 启用并正确排序 JSON 步骤、说明依赖与未发布状态；修正执行结果区的“实时显示进度”文案，使其符合等待后一次返回的实际行为；将共用 `step_not_supported` 兜底改为不列步骤的中性文案。 |
| 测试 | 扩展 `tests/unit/test_collection_job_api.py`；新增 `tests/unit/test_json_step_service.py`；复跑 `tests/unit/test_ddl_step_service.py` 中 `build_managed_ddl_config()` 的既有快照用例，确认 JSON 复用的配置构造不回归；复核 `tests/unit/metaweave/test_json_llm_enhancer.py`、`tests/unit/metaweave/metadata/test_json_generation_finalization.py`、`test_json_v3_schema.py` 与前端类型检查。 |

验收至少覆盖：

1. 既有独立 `--step json` 及 pipeline 不回归；受管 `ddl → json` 使用同一 `job_id`，JSON 只读同作业 DDL、只写同作业 `json/`，`metadata_version="3.0"`，不读写全局 DDL/JSON 目录，也不写平台画像表或 `publish_info`。同一请求选择 `db+ddl+json` 且三步均成功时，响应保持原有 `job_id` 与逐步 `results` 形状，包含三个步骤的成功结果；平台库 `job_info` 中同一 `job_id` 的 `db`、`ddl`、`json` 三行均为 `success`，接入 JSON 不改变 `db/ddl` 的既有状态语义。
2. JSON 单选和 `db+json` 均通过步骤白名单，不在步骤准入阶段因未同时勾选 DDL 返回 `step_not_supported` 或拒绝整份请求；若 JSON 是首个执行步骤且准备失败，执行期的受控错误仍表现为整份请求返回 400。新作业只选 JSON 时，`run_json_step(job_id=None)` 明确报告“同一作业的 DDL 产物未就绪”，按首步配置错误返回 400 `invalid_request`，不创建空工作区、不生成或清理文件、不新增 JSON 步骤行；新作业选 `db+json` 时，DB 已执行的结果保留，JSON 准备失败作为该请求的逐步失败结果返回，不改写 JSON 步骤行。原有“`db+json` 因 JSON 不受支持而返回 `step_not_supported`”的断言须改为这两类依赖错误用例，另用真正不支持的步骤覆盖 `step_not_supported`。带正确 `job_id` 的 JSON 单独重跑成功；错源工作区、禁用源、DDL 非 `success`、清单缺失/损坏/不匹配、DDL 库名与登记不符或 DDL 解析失败均在清理 JSON 之前拒绝；这项登记库名核对仅属于 JSON 前置，不改变 doc 16 的 DDL 发布校验。JSON 与 DDL 同请求时即使请求顺序相反也先执行 DDL；DDL 失败后 JSON 不启动。准备失败时不新增 JSON 步骤行，不调用 `finish_step()`；已有 JSON 行的上次状态保持不变，部分结果响应仍可报告本次 JSON 未执行。
3. `result.success=True` 但 `failed_tables>0`、零对象、输出缺件/多出/越界/符号链接、v3 或身份错误、LLM 增强失败、清单写入失败都标 `job_info.json=failed`；生成完成、写清单之前先按目录中全部 `.json` 文件核对集合，再由 `JsonLoader.list_objects()` 校验 v3 与身份，不能漏掉会被其库名前缀过滤跳过的外来文件。本篇不要求写清单后再做一次核对；清单只列画像文件，不把 `manifest.json` 本身当作画像文件。按已验证的 DDL 清单对应 stem 集合与 JSON 文件 stem 集合做区分大小写的精确比对，缺件、多件或身份不一致均失败；两次运行之间修改 `include_object_types` 或 `exclude_tables` 导致 JSON 集合与同作业 DDL 清单不同，也须判失败并提示重跑同作业 DDL；只在完整成功时生成清单并标 `success`。失败后重跑仅清本作业 JSON 顶层文件，其他目录及其他作业保持不变。
4. DDL 在 JSON 执行期间重跑时，本次 JSON 不标成功；`sampling.enabled=false` 且逻辑主键检测开启时，独立 CLI 与受管 JSON 的实际路径 `_process_table_from_ddl()` 不抛 `AttributeError`、不虚构逻辑主键；防御性修改的 `_process_table_from_db()` 由直调单测覆盖，不要求从现行 CLI/Web DDL 入口命中其受 `ddl_only_mode` 保护的代码块。Web JSON 使用登记源的 `sslmode`，登记源未设置时不继承主配置的值；DDL/独立 CLI 的连接回归由 doc 18.1 验收。用含唯一标记的样例值/LLM 响应触发无 JSON 起点、非法 JSON 及 LLM 调用异常，在 INFO 和 DEBUG 日志级别均断言日志、受控错误消息不含标记、JSON 原文、prompt 或响应。
5. Web 页面可勾选 JSON，重跑选择可带入 JSON；只有 JSON 成功的页面不引导用户把它作为已支持的发布步骤。执行期间只显示等待状态，执行结果区不再写“实时显示各步骤的执行进度与结果”，请求完成后才显示逐步结果。采集侧不支持步骤返回“当前仅支持 db、ddl、json 步骤”（doc 19.1 实施后含 `md`），发布侧仍返回“当前仅支持 db、ddl 步骤”；当服务端消息为空而使用 `step_not_supported` 共用兜底时，只显示“所选步骤暂不支持”，不得暗示 JSON 已可发布。现有 `db/ddl` 采集和发布的 API、页面与测试仍通过；前端 `typecheck`、构建通过。
6. 登记源缺少 `username`、`host` 或 `db_name`，或 `sampling.sample_method` 不是 `limit`、`output.json_options` 结构无效、`output.ddl_options.sample_records.count` 非整数/为负数、`output.markdown_options.sample_value_count` 不能转换为整数时，JSON 在 `start_step()` 和清理目录前按 `DbStepConfigError` 受控失败，不新增 JSON 步骤行，旧 JSON 文件与清单保持不变；原生成器入口的同类配置校验仍生效，`JsonLlmEnhancer` 对 `output.json_options` 的接受/拒绝行为与准备链一致。成功生成的 `json/manifest.json` 为 `{"files": [...]}`，文件名排序去重，清单集合与 JSON 目录实际画像文件集合一致；与 DDL 清单格式兼容。

## 7. 实施前提与运行边界

以已完成的 doc 16 平台库迁移、doc 18.1 连接参数改造和可用的受管 DDL 采集为实施前提。虽然本篇不新增平台画像表，但 Web 采集仍依赖 `db_source_info`、`job_info` 和受管 DDL 工作区。默认配置的 JSON 注释与表分类 LLM 开关目前为 `true`；本篇保留默认值，受控单对象实跑仅证明关闭 LLM 时的核心路径，实施验收还应以受控替身覆盖 LLM 成功、失败和保存部分结果的状态语义。Web 入口沿用现有同步请求加 `asyncio.to_thread()`：浏览器在任务完成后收到整批结果；耗时较长时需保证部署侧 HTTP 超时允许本次运行，本篇不引入队列、Worker 或轮询进度。
