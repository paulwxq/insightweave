# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

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
- 全流程手动序列：`--step ddl` → `--step json` → `--step md` → `--generate-domains` → `--generate-dim-config` → `--step rel` → `--step cql` → `--generate-sql-rag`（md 依赖 json 产物，见 docs/data_gov/17）；加载按类型单独执行 `metaweave rag_load --type sql|dim_value|cql`；`--type table_schema` 仅供调试，必须传 `--source_id`/`--job_id`，不访问平台库（`--step`/`--type` 均仅允许传入一次，见 docs/update/27、docs/data_gov/20.1）

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
- `models.py` - Data structures (TableMetadata, ColumnInfo, etc.)

**`metaweave/core/relationships/`** - Table relationship discovery
- `pipeline.py` - Orchestrates relationship discovery process
- `candidate_generator.py` - Generates candidate relationships based on naming patterns
- `scorer.py` - Scores candidates using DB sampling (inclusion rate, Jaccard, etc.)
- `llm_relationship_discovery.py` - LLM-based semantic relationship inference
- `models.py` - Relation data structures

**`metaweave/core/cql_generator/`** - Neo4j CQL generation
- `generator.py` - Generates Cypher scripts from JSON + relationship data
- `reader.py` - Reads JSON metadata files
- `writer.py` - Writes CQL output files

**`metaweave/core/loaders/`** - Data loaders for Neo4j/Milvus
- `factory.py` - Factory pattern for loader creation
- `cql_loader.py` - Loads CQL into Neo4j
- `table_schema_loader.py` - Loads table schemas into vector DB
- `dim_value_loader.py` - Loads dimension values

**`metaweave/services/`** - Shared services
- `llm_service.py` - LLM integration (qwen-plus, deepseek) via LangChain
- `embedding_service.py` - Text embedding for vector search
- `vector_db/` - Vector database adapters (Milvus, pgvector)

**`metaweave/cli/`** - Command-line interface
- `main.py` - Main CLI entry point with subcommands
- `metadata_cli.py` - Metadata generation commands (incl. `--generate-domains` / `--generate-dim-config`)
- `loader_cli.py` - `rag_load` 加载命令（`--type` 必选且仅一个具体类型；table_schema 为 UUID 定位的调试加载）
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
├── cql/              # Neo4j Cypher import scripts
├── domains/          # Business domain config (db_domains.yaml, generated by --generate-domains; git-tracked)
├── dim_config/       # Dimension table config (dim_tables.yaml, generated by --generate-dim-config; git-tracked, embedding_col 需人工填写)
└── sql-rag/          # Question-SQL examples (generated + validated by `metadata --generate-sql-rag`)
```

Web 采集的 SQL 样例位于 `output/<source_id>/<job_id>/sql-rag/`；`rag_load --type sql` 的 Web 发布路径改造见后续设计。

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
