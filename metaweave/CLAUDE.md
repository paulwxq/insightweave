# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Communication Language

**始终使用简体中文与用户交流。** Always communicate with the user in Simplified Chinese.

## Project Overview

MetaWeave is a database metadata generation and enhancement platform that extracts metadata from PostgreSQL databases, generates data profiles, discovers table relationships, and exports to graph databases (Neo4j) and vector databases (Milvus). The project supports both traditional rule-based analysis and LLM-enhanced semantic understanding.

## Common Commands

### Virtual Environment Setup

项目使用 `uv` 管理依赖，虚拟环境统一为根目录下的 `.venv`（已被 git 忽略）：

```bash
# 创建 .venv 并安装全部依赖（含 dev 工具：pytest/black/ruff 等）
uv sync --all-extras

# 仅安装运行依赖
uv sync
```

- `uv sync` 会自动创建 `.venv`，并以可编辑模式安装项目本身（含 `metaweave` CLI 入口），无需再执行 `uv pip install -e .`
- 激活环境：`source .venv/bin/activate`（WSL/Linux）或 `.venv\Scripts\Activate.ps1`（Windows PowerShell）
- 也可以不激活，直接使用 `uv run metaweave ...` / `uv run pytest ...`

### Running MetaWeave

```bash
# Show help
uv run metaweave --help

# Generate a specific step (ddl, json, md, cql, rel)——不传 step 且无 --generate-* 模式会报错；md 依赖 json 产物
uv run metaweave metadata --config configs/metadata_config.yaml --step ddl

# Clean the step's output directory before generating
uv run metaweave metadata --config configs/metadata_config.yaml --step cql --clean

# Filter by schema or tables
uv run metaweave metadata -c configs/metadata_config.yaml --schemas public
uv run metaweave metadata -c configs/metadata_config.yaml --tables users,orders

# Adjust concurrency
uv run metaweave metadata -c configs/metadata_config.yaml --max-workers 8

# Enable debug mode
uv run metaweave --debug metadata -c configs/metadata_config.yaml
```

### Testing

运行测试前需先激活虚拟环境（`source .venv/bin/activate`），或直接使用 `uv run pytest`。

```bash
# 激活环境后运行全部测试
pytest tests/

# 运行指定目录
pytest tests/metaweave_relationships/

# 带覆盖率
pytest --cov=metaweave tests/

# 运行单个文件
pytest tests/test_composite_matching.py
```

### Development Tools

```bash
# Format code with black
black metaweave/

# Lint with ruff
ruff check metaweave/
```

## Architecture

### Processing Pipeline

单步命令逐个执行（上游产物缺失时由各单步自检报错）。CLI 不再提供跨步骤编排：
`metaweave pipeline`、旧 `--step standard` 与 `rag_load --type all` 均已移除（见 docs/data_gov/20.1 §10.7、§10.8）。

**metadata 步骤**（`--step` 可选 ddl/json/md/cql/rel，必填或使用 --generate-* 模式）:
1. `ddl` - Extract table structures from DB → `output/ddl/*.sql`
2. `json` - Generate data profiles from DDL + DB sampling → `output/json/*.json`
3. `md` - Generate Markdown documentation from JSON 产物（file-only，不连数据库，不回退读 DDL；见 docs/data_gov/17）→ `output/md/*.md`
4. `rel` - Discover relationships: rules + DB sampling；`relationships.llm_candidates.enabled`（默认 true）可叠加 LLM 候选，LLM 结果同样经数据库采样验证 → `output/rel/*.json`
5. `cql` - Generate Neo4j CQL scripts (file-only, no DB access) → `output/cql/*.cypher`

**LLM 增强流程**（各步骤的 LLM 能力由配置驱动）:
- 注释与分类增强内建于 `--step json`，由 `json_generation.comments.llm_enabled` / `json_generation.table_classification.llm_enabled` 配置驱动（原地覆写 `output/json/*.json`）
- `rel` 的 LLM 推断能力由 `relationships.llm_candidates.enabled` 控制
- Web 采集 `sql` 依赖同一 `job_id` 的 MD、Domain、Rel 成功产物，写入 `output/<source_id>/<job_id>/sql-rag/`；准备阶段非受控故障返回 `sql_preparation_error`（503），详见 docs/data_gov/25.1。
- 全流程手动序列：`--step ddl` → `--step json` → `--step md` → `--generate-domains` → `--generate-dim-config` → `--step rel` → `--step cql` → `--generate-sql-rag`（md 依赖 json 产物，见 docs/data_gov/17）；加载按类型单独执行 `metaweave rag_load --type cql`（`dim_value` 已随 doc 22.2、`sql` 已随 doc 25.2 移除，均由 Web 发布替代）；`--type table_schema` 仅供调试，必须传 `--source_id`/`--job_id`，不访问平台库（`--step`/`--type` 均仅允许传入一次，见 docs/update/27、docs/data_gov/20.1）

### Database Access Requirements

Web `sql` 在 `sql_rag.validation.enabled=false` 时只读同作业文件并调用 LLM；开启校验时才连接源库执行 EXPLAIN。

**CLI metadata 步骤**：除 `cql` 和 `md` 外通常需要访问源库：
- `ddl` - Queries table structures, constraints, indexes
- `json` - Executes COUNT queries and samples data for statistics; LLM 注释/分类增强（配置开启时）同步完成
- `rel` - Samples data for relationship scoring (inclusion rate, Jaccard index); LLM candidates (if enabled) are also DB-validated
- `md` - File-only: parses table structure and sample data from `output/json/*.json`（json 产物缺失时前置检查报错，不回退 DDL）

### Core Modules

**`metaweave/core/metadata/`** - Metadata extraction and generation
- `generator.py` - Main orchestrator for metadata generation
- `extractor.py` - Extracts metadata from PostgreSQL information_schema
- `connector.py` - Database connection and query execution
- `comment_generator.py` - LLM-based comment generation with caching
- `logical_key_detector.py` - Identifies candidate logical primary keys
- `formatter.py` - Exports to DDL, JSON, Markdown formats
- `json_llm_enhancer.py` - LLM-enhanced JSON generation (replaces deprecated llm_json_generator.py)
- `sample_block.py` - Shared encoding/decoding for `SAMPLED_RECORDS`/`OBJECT_METADATA` JSON comment blocks (`*` escaping, current-format records, legacy-marker rejection; doc 19.2 §8.2)
- `models.py` - Data structures (TableMetadata, ColumnInfo, etc.)

**`metaweave/core/relationships/`** - Table relationship discovery（doc 23.1 共享层修复：`DomainResolver.resolve_table_pairs` 增 `db_name` 规范化参数修复三段式/两段式表名失配、`MetadataRepository.load_all_tables` 按名排除 `manifest.json`）
- `pipeline.py` - Orchestrates relationship discovery process
- `candidate_generator.py` - Generates candidate relationships based on naming patterns
- `scorer.py` - Scores candidates using DB sampling (inclusion rate, Jaccard, etc.)
- `llm_relationship_discovery.py` - LLM-based semantic relationship inference
- `models.py` - Relation data structures

**`metaweave/core/cql_generator/`** - Neo4j CQL generation
- `generator.py` - Generates Cypher scripts from JSON + relationship data
- `reader.py` - Reads JSON metadata files
- `writer.py` - Writes CQL output files

**Web backend object profiles (doc 19.2)** - JSON profile publish & object read APIs
- `backend/profile_loading/json_loader.py` - Managed JSON artifact reading/validation + row mapping to `table_profile`/`table_column_profile`
- `backend/publishing/publish_service.py` - Publish steps `db|ddl|json`; json step replaces the source's profile rows in one locked transaction
- `backend/assets/` - Read-only services: `object_catalog.py` (list + cascade options), `object_detail.py` (detail + DDL summary + DDL text split), `ddl_display.py` (SQL/sample split, pure functions), `read_tx.py` (REPEATABLE READ + READ ONLY connection setup; call before first query)
- `backend/api/routers/asset_objects.py` - `GET /api/data-assets/{source_id}/objects|object-options|object-detail|object-ddl`

**Web 受管 Domain 步骤（doc 21.1 采集、doc 21.2 发布、doc 24.1 发布页编辑）** - 采集作业在 md 之后可勾选 `domain`（`--generate-domains --description`）；发布页 b 组可勾选 `domain`，把 `db_domains.yaml` 写为平台库 `table_domain` 表-域映射（一张表可属多个 domain）。发布页可编辑该 YAML 的 domain 列表（名称、描述、归属表），不改库名、库描述、`job_info.db_desc` 与数据源描述
- `backend/data_sources/domain_step_service.py` - 受管 Domain 采集步骤：依赖同作业 md 产物（行 success + 目录 ≥1 个 `.md`），产物写 `output/<source_id>/<job_id>/domain/db_domains.yaml`；执行时把生效描述快照写入 `job_info.db_desc`，勾选同步时同事务更新 `db_source_info.description`；依赖或配置类失败抛 `DbStepConfigError`、平台库读取异常返回失败结果、本地段非受控故障抛 `DomainPreparationError`（503 `domain_preparation_error`）
- `backend/profile_loading/domain_loader.py` - 受管 Domain 产物读取与解析（doc 21.2）：`O_NOFOLLOW` 加固读取、三段式/登记库名与 `selected_schemas` 校验、MD 清单交叉校验；任何结构非法整次抛 `DomainPublishInputError`，锁下复查与失败文案由发布侧决定
- `backend/publishing/publish_service.py`、`backend/persistence/models.py` - domain 发布步骤（doc 21.2）：预检 `check_domain_output_ready`、锁下五项复查（源存在/启用、登记 `updated_at`、工作区、步骤标识与作业准入）、按 `source_id` 整份替换 `table_domain` 并与成功 current 发布行同一事务提交；平台库 `public.table_domain` 由建表执行

**Web dim_value 发布（doc 22.2）** - 发布页 d 组 `dim_value` 解冻（dim_config 产物存在即解冻）+ 配置编辑弹窗（写回 `dim_tables.yaml`）+ 发布加载到 Milvus `dim_value_embeddings`（显式主键 `col_id` 四段拼接、按源整份替换、注入拦截时同一份配置）；点发布时三态拦截（无效/零表/未配置）整请求拒绝
- `backend/profile_loading/dim_value_config_api.py` - 配置编辑 API（GET/PUT/objects/object-columns 四端点、三态 classify 共享读取、`yaml.dump` 原子写回）
- `backend/profile_loading/dim_value_loader.py` - `inspect_dim_value_context` / `check_dim_value_output_ready` / `build_dim_value_loader`（登记连接六键注入 + alias 唯一化）
- `backend/jobs/collection_run.py` - 白名单/拓扑排序偏序（ddl→json→md→domain、json→dim_config、json+domain→rel、md+domain+rel→sql）；透传 `domain_description` 与 `sync_domain_description`。同请求阻断关系（上游, 下游）：`(ddl, json)`、`(json, md)`、`(ddl, md)`、`(md, domain)`、`(json, dim_config)`、`(json, rel)`、`(domain, rel)`、`(md, sql)`、`(domain, sql)`、`(rel, sql)`；仅两端同选且上游在本次结果中失败时阻断，MD 同选 JSON 时只看 JSON，否则看同选的 DDL。
- `backend/api/routers/data_sources.py`、`backend/jobs/listing.py` - 作业创建请求可带 `domain_description`/`sync_domain_description`（`model_fields_set` 判定携带）；作业列表每项返回本作业 `db_desc`（`domain_description`）供重跑弹窗预填

**Web 受管 Dim Config 步骤（doc 22.1）** - 采集作业可勾选 `dim_config`（`--generate-dim-config`）：file-only，依赖同作业 JSON 产物（json 行 success + 受管 json 目录 ≥1 个除 `manifest.json` 外的普通画像 `.json`），产物写 `output/<source_id>/<job_id>/dim_config/dim_tables.yaml`（`embedding_col` 恒 null 待人工填写）；无 LLM、无描述弹窗、无平台库迁移；前端新建作业须同选 ddl+json 才释放勾选，重跑作业含 json 步骤即可
- `backend/data_sources/dim_config_step_service.py` - 受管 Dim Config 采集步骤：准备链三段失败分类（`DbStepConfigError` / 平台库失败结果 / `DimConfigPreparationError` 503 `dim_config_preparation_error`）；生成落 `mkstemp` 唯一临时文件，核验后 `os.replace` 到最终路径（防悬空链接越界写与 FIFO 阻塞）；`DimTableConfigGenerator` 经可选 `json_files` 参数只读准备链筛出的文件列表（CLI 不传、行为不变）

**Web 受管 Rel 步骤（doc 23.1）** - 采集作业在 json+domain 之后可勾选 `rel`（`--step rel`）：连源库采样评分（非 file-only），`relationships.domain`/`cross_domain` 按 yaml 生效（null=全表两两、all=仅域内，不做 null→all 改写；共享层已修复 db_domains.yaml 三段式表名与管线两段式失配）；产物写 `output/<source_id>/<job_id>/rel/{db}.relationships_global.{json,md}`；重跑前旧产物备份至 `rel/.backup/`（doc 23.2）；发布由 doc 23.2 的 rel 发布步骤承接（见下）
- `backend/data_sources/rel_step_service.py` - 受管 Rel 采集步骤（doc 23.1）：依赖同作业 json+domain 两行 success 与产物核验，连源库采样（凭据注入 `build_managed_ddl_config`）；`relationships.domain`/`cross_domain` 按 yaml 生效（null=全表两两、all=仅域内，不做 null→all 改写）；构造预检（`_precheck_pipeline_config` 两族节点 + `batch_size` 镜像 CLI 语义）与对象-范围核对（`check_objects_in_scope(step_label)`）在清理前；构造先于清理（急切建池=连接探测前移，构造期失败旧产物保留）、clean 抛错时服务侧显式 `connector.close()`；上游 marker 复查（会话 2/3）；零关系/零推断告警互斥双分支（success+告警）；产物写 `output/<source_id>/<job_id>/rel/{db}.relationships_global.{json,md}`；本地段非受控故障抛 `RelPreparationError`（503 `rel_preparation_error`）

**Web 受管 Rel 发布（doc 23.2）** - 发布页 c 组原位放 `rel`（cql 行已从采集/发布页移除，WebUI 全链路不出现 cql，D1）：发布过程在锁内生成 cql（`output/<source_id>/<job_id>/cql/`，由发布创建，无采集步骤行）并加载 Neo4j（doc 4 B 多源共存：按源单一数据事务替换——删除本源旧子图→写入→按源核验，Table/Column 按 entity_id=source_id:id MERGE、全部节点与边带 source_id/publish_run_id，仅生成 entity_id 唯一约束；失败整体回滚旧图保留）

- `backend/profile_loading/rel_config_api.py` - rel 关系编辑 API 三端点：PUT 持源行锁 + 指纹乐观并发（409 `rel_regenerated`）+ D7 新增/D8 七项重标/实质未变原值覆盖 + statistics 基础计数重算（遥测保留）+ 两阶段写回（渲染/写盘分离、先 md 后 json，任一失败指纹不失效可重试）；错误码 400 `rel_config_invalid`/404 `rel_config_output_missing`/500 `rel_write_failed`
- `backend/profile_loading/rel_loader.py` - `check_rel_output_ready`（宽松产物就绪，忽略 `.tmp*`/`.backup`）、`inspect_rel_context`、`assert_rel_publish_strict`（三行存在性+success 就地复查、新鲜度严格晚于 json/domain、画像/domain/主配置）、`capture_rel_precheck_snapshot`（登记快照+两行 marker+rel json 指纹+主配置快照，经 `registrations` 旁路携带）
- `publish_service._publish_rel` - 锁内执行链：登记/三 marker/指纹复核 → 清空 cql 目录 → 受管快照生成（provenance=(source_id,publish_run_id) 必填，Table/Column/边统一注入）→ 产物核验 → `CQLLoader(config, source_id)` → `validate()` → `load()`（按源替换）→ 发布行；`CQLScriptValidationError` 在通用 except 前单列（图写入前拒绝、不撤销 current）；数据事务确认回滚的失败（`_rolled_back`：回调内异常/核验不符/约束失败；所有无 source_id 及本源节点（任意 label）在事务内按节点归属清理）图保持原状、不撤销 current；COMMIT 阶段失败（IncompleteCommit 等，提交状态不可知）不标记回滚、撤销 current；按源预期/实际计数入日志（关联 source_id/publish_run_id）；零关系附告警（D10）
- `../frontend/src/components/RelEditDialog.vue`（平台根 `frontend/`，2026-10-17 自本目录 `frontend/` 迁出；原型 23.rel-1~5）- 关系编辑弹窗：列名多选 chips 按序配对（复合键）、基数 {N:1, 1:1, M:N}、关联方向落库归一 from/to、统计标签按 `discovery_method` 派生、低分告警 <0.90、保存成功卡片（diff 三类计数 + rel json 格式化预览）
- 共享层：`render_markdown_report`（writer 模块级纯渲染）+ `relation_from_json_entry`（models）支撑 md 整份重渲染（A14 逐字节一致）；cql_loader 加固（解析前置/必需章节核验/数量核验置败/按节点归属清理所有无 source_id 及本源节点（任意 label）/提交状态细分回滚口径/幂等 `close()`）；CLI `rag_load --type cql` 不背书（调试路径按脚本头 source_id 整源替换，旧格式脚本拒绝），Web 发布为正式链路
- **doc 4 多源改造（2026-10-17）**：`cql_generator/keyspace.py`（db 前缀统一判定式 + 标识符正则 + `build_entity_id`，reader 读后整源校验）；`scripts/migrate_neo4j_source_identity.py` 一次性迁移（默认只读预检、`--apply --backup-archive` 执行：Column/边归属回填单事务 → 删旧四约束建 entity_id 约束 → 实际定义核验，报告落 `output/maintenance/neo4j_source_identity/`；普通发布对无 source_id 或本源节点（任意 label）直接在事务内清理，不再要求先迁移）；NL2SQL 侧对应改造见 nl2sql `db_deepagent`（keyspace/会话绑定/Milvus 六入口过滤/join_graph 实体键）

**Web 受管 SQL 步骤（doc 25.1）** - `sql` 依赖同作业 MD、Domain、Rel 成功行及文件；新作业须同选 DDL、JSON、MD、Domain、Rel 才能勾选，重跑通过 `sql-readiness` 检查未同选上游。`backend/data_sources/sql_step_service.py` 清理受管 `sql-rag/` 顶层文件后生成 SQL，可选 EXPLAIN 校验与修复；结束时在源行锁下复查上游 marker 和全部输入指纹，再写 `last_status`。`validation.enabled=false` 不连接源库；Domain PUT 从指纹读取至文件替换持同一源行锁。`SqlPreparationError` 映射 503 `sql_preparation_error`。

**Web 受管 SQL 发布（doc 25.2）** - 发布页 d 组解冻 `sql`（就绪条件：受管 `sql-rag/` 顶层存在非忽略 `*.json`）；发布读取该目录全部 `*.json` 合并，question 向量化后写入 Milvus `sql_example_embeddings`（新增 `source_id`/`publish_run_id`，`example_id` 前缀改为 `source_id`，按源整份替换）。`backend/profile_loading/sql_loader.py` - inspector/就绪检查/受管加载器构造（共享枚举 helper `collect_sql_example_input_files` 在 `metaweave/core/sql_rag/loader.py`）；`publish_service._publish_sql` 走 `_PUBLISHERS` 5 参通用分支；`rag_load --type sql` CLI 入口已移除。

**`metaweave/core/loaders/`** - Data loaders for Neo4j/Milvus
- `factory.py` - Factory pattern for loader creation
- `cql_loader.py` - Loads CQL into Neo4j
- `table_schema_loader.py` - Loads table schemas into vector DB
- `dim_value_loader.py` - Loads dimension values into Milvus（doc 22.2：仅服务 Web 发布链路，CLI 入口已移除；集合主键 col_id、按源替换、注入配置）

**`metaweave/services/`** - Shared services
- `llm_service.py` - LLM integration (qwen-plus, deepseek) via LangChain
- `embedding_service.py` - Text embedding for vector search
- `vector_db/` - Vector database adapters (Milvus, pgvector)

**`metaweave/cli/`** - Command-line interface
- `main.py` - Main CLI entry point with subcommands
- `metadata_cli.py` - Metadata generation commands (incl. `--generate-domains` / `--generate-dim-config`)
- `loader_cli.py` - `rag_load` 加载命令（`--type` 必选且仅一个具体类型 table_schema/cql；table_schema 为 UUID 定位的调试加载；dim_value 已随 doc 22.2、sql 已随 doc 25.2 移除（Web 发布替代）；cql 已随 doc 4 改为按源替换（--clean 无整库清空；删除范围取脚本头 source_id），Web rel 发布为正式链路（doc 23.2/doc 4））
- `../core/metadata/execution.py` - metadata 共享执行层（`metadata` 命令使用）

### Configuration

Primary config file: `configs/metadata_config.yaml`

Key sections:
- `database` - PostgreSQL connection, schemas, exclusions
- `llm` - Provider selection (qwen-plus/deepseek), API keys, model parameters
- `comment_generation` - Enable/disable LLM comments, language
- `logical_key_detection` - Candidate key discovery settings
- `sampling` - Sample size for data profiling
- `output` - Output directories and formats; `domains_directory` (default `output/domains`) locates `db_domains.yaml` — CLI `--domains-config` overrides the full file path

Environment variables (`.env`):
- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`
- `DASHSCOPE_API_KEY`, `DASHSCOPE_MODEL` (for qwen-plus)
- `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`, `DEEPSEEK_API_BASE` (for deepseek)

#### Module-level LLM Override

Modules can override the global `llm` config via `xxx.llm` (deep merge semantics). The resolver is `metaweave/services/llm_config_resolver.py`.

Currently supported module-level LLM paths:
- `domain_generation.llm`
- `sql_rag.llm`
- `relationships.llm`
- `json_llm.llm`
- `comment_generation.llm`

All five modules have been integrated. Declaring unsupported paths in YAML will raise `ValueError` at startup.

### Data Models

**TableMetadata** - Complete table information including columns, constraints, indexes, sample data

**ColumnInfo** - Column details with optional statistics and LLM-generated comments

**Relation** - Table relationships (single or composite columns) with:
- `relationship_type`: "foreign_key" or "inferred"
- `cardinality`: "1:1", "1:N", "N:1", "M:N"
- `composite_score`: 0-1 confidence. 推断关系来自五维加权评分（inclusion_rate 0.40 / name_similarity 0.10 / comment_similarity 0.10 / type_compatibility 0.20 / jaccard_index 0.20，见 `scorer.py` DEFAULT_WEIGHTS）；物理外键不评分，固定 1.0
- `score_details`: 上述五个维度的引擎内部评分明细（外键关系不设置；rel JSON 产物也不写此字段）
- rel JSON 产物只含标量 `composite_score` 与 `confidence_level`（≥0.90 → high，≥0.80 → medium；低于 accept_threshold 0.8 的关系在写盘前已被过滤，产物中不会出现 low）

### LLM Integration

LLM calls are used for:
1. **Comment Generation** (`comment_generator.py`) - Generates missing table/column descriptions
2. **Semantic Classification** (`--step json`，由 `json_generation.table_classification.llm_enabled` 配置驱动) - Infers table categories，原地覆写 `output/json/*.json`
3. **Relationship Inference** (`rel` 步骤，由 `relationships.llm_candidates.enabled` 控制) - LLM 推断隐含关系候选，再经数据库采样验证

LLM outputs are written into the generated artifacts (DDL/JSON). There is no local comment cache file.

### Output Structure

```
output/
├── ddl/              # PostgreSQL DDL with comments and sample data annotations
├── md/               # Human-readable Markdown documentation (generated from JSON artifacts)
├── json/             # Table/column profiles (json_llm enhances these files in place)
├── rel/              # Discovered relationships (relationships_global.json)
├── cql/              # Neo4j Cypher import scripts（受管路径 output/<sid>/<jid>/cql/ 由 rel 发布创建，doc 23.2）
├── domains/          # Business domain config (db_domains.yaml, generated by --generate-domains; git-tracked)
├── dim_config/       # Dimension table config (dim_tables.yaml, generated by --generate-dim-config; git-tracked, embedding_col 需人工填写)
└── sql-rag/          # Question-SQL examples (generated + validated by `metadata --generate-sql-rag`)
```

Web 采集的 SQL 样例位于 `output/<source_id>/<job_id>/sql-rag/`；发布经 Web 发布页 `sql` 步骤加载到 Milvus（doc 25.2），CLI 入口已移除。

## Development Notes

### Project Origin
This is a standalone project migrated from `nl2sql_v3/src/metaweave/` (copy only, no modifications to original).

### Virtual Environment
项目使用 `uv` 管理依赖，虚拟环境统一为根目录下被 git 忽略的 `.venv`（创建与同步方式见上方 Virtual Environment Setup）。

### Concurrent Processing
The metadata generator supports concurrent table processing via `--max-workers` flag. Default is 4 workers.

### Logging
- CLI log files: `logs/script/*.log` (per-step + loader, see docs/data_gov/15)
- API log files: `logs/api.log` (request summaries) and `logs/backend/*.log` (per-module business logs); requires the `backend.api.logged_app:create_logged_app` entry
- Logging config: `configs/logging.yaml` (one YAML: top level = script profile, `x_profiles.api` + `x_modules` = API profile; loader: `services/logging_manager.py`)
- Use `--debug` flag for verbose output

### Testing Structure
- `tests/unit/` - Unit tests for individual components
- `tests/metaweave_relationships/` - Relationship discovery integration tests
- `test_composite_matching.py` - Composite key matching tests
- `test_two_stage_matching.py` - Two-stage matching algorithm tests

### Code Organization
- The package includes both `metaweave/` and `services/` modules
- Use `python -m metaweave.cli.main` or `uv run metaweave` to invoke CLI
- Scripts in `scripts/` provide alternative entry points for specific operations
