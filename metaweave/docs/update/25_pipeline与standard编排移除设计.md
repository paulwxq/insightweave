# 25. `pipeline` 与 `standard` 编排移除设计（定案稿）

> 状态：已实施（2026-09-19，含 §6 全部验收）；**边界修订**（2026-09-20）：
> 本稿"CLI 不再存在任何自动多步骤编排"的结论由 docs/update/26 修订——
> 配置驱动的 `pipeline` 命令（新参数与语义，读 configs/pipeline.yaml）
> 重新提供了编排能力。旧 `pipeline generate/load`、`metadata --step standard`
> 及独立 `json_llm` 的移除结论**仍然有效**
> 日期：2026-09-19
> 前置：e2bb425（rag_load 重构，doc 24）

## 1. 背景与定案

**现状**：存在两套自动多步编排——`pipeline generate`（9 步 LLM 流水线）与
`metadata --step standard`（5 步串行，`--step` 的**默认值**）。经 doc 23/24
两轮 CLI 收敛，各步骤均已有独立入口，编排层成为冗余壳。

**定案（用户决策）**：删除两套编排，只保留明确的单步命令与独立生成模式。
CLI 边界：**metadata 负责单步/独立生成，rag_load 负责加载，不再存在任何
自动多步骤编排**。

## 2. 移除范围

### 2.1 删除 `metaweave pipeline`

| 对象 | 处置 |
|---|---|
| `metaweave/cli/pipeline_cli.py` | 整体删除（generate/load、`_PipelineContext`、`_step_*`、`GENERATE_STEPS`/`LOAD_STEPS`） |
| `metaweave/cli/main.py` | 删 import 与 `add_command`；顶层仅剩 `metadata` / `rag_load` |
| `tests/unit/metaweave/test_pipeline_domain_config.py` | 删除 pipeline 专属用例；`resolve_domains_yaml_path` 通用用例如有则移植 |

### 2.2 删除 `metadata --step standard`

- Choice 移除 `standard`：`["ddl", "json", "cql", "md", "rel"]`
- 删除 5 步循环（metadata_cli.py `step_lower == "standard"` 分支整段）及
  其中**不可达的 `json_llm` 分支**（:890 起的两段式代码，为统一改造前
  遗留冗余——`MetadataGenerator` 已内建调用 `JsonLlmEnhancer`，
  generator.py:870）
- `--step` 默认值由 `"standard"` 改为 `None`（不设 required）；
  `set_current_step` 的 `(step or "standard")` 兜底同步清理
- **裸调用契约**：未传任何 `--generate-*` 独立模式且 `--step` 为空 →
  `click.UsageError`（"必须指定 step 或 generate 模式"）；
  `--generate-domains` / `--generate-dim-config` / `--generate-sql-rag`
  仍无需传 `--step`
- `tests/unit/test_step_all_orchestrator.py` 删除（standard 编排专属）
- 作用域告警矩阵**无需删项**：`_MODE_SCOPED_PARAMS` 本无独立 standard
  条目（仅 4 键：三个 generate 模式 + 通用 `step`）。保留
  `_MODE_SCOPED_PARAMS["step"]` 的单步参数集合（继续服务
  ddl/json/md/rel/cql）；仅删除其定义处注释中
  「step（--step 流程，含默认 standard）」的表述

### 2.3 不保留 json_llm CLI

- Choice 不新增 `json_llm`；无死代码；**不保留 json_llm 功能的正向测试与
  正向验收；保留 Choice 拒绝回归**（见 §5-4 / §6-2——`--step json_llm`
  被拒绝本身是防回潮的删除验收）
- 唯一 JSON 入口 = `metadata --step json`
- LLM 注释/分类能力由 `json_generation` 配置驱动
  （`comments.llm_enabled` / `table_classification.llm_enabled`，
  见 configs/metadata_config.yaml:307）

## 3. 迁移说明（手动单步**不是**旧编排的严格等价物）

以下差异为**有意行为变更**，写入迁移说明而非等价映射：

| 旧用法 | 新做法 | 有意差异 |
|---|---|---|
| `pipeline generate` | 按序手动：`--step ddl` → `--step md` → `--generate-domains` → `--step json` → `--generate-dim-config` → `--step rel` → `--step cql` → `--generate-sql-rag` | 无自动串联；上游产物缺失由各单步自检报错 |
| `pipeline generate --clean` | **已移除，无等价命令**。各命令 `--clean` 统一语义：**所有 step（ddl / json / md / rel / cql）及 `--generate-domains` / `--generate-dim-config` / `--generate-sql-rag` 均删除目标目录顶层所有文件（不限扩展名），不递归，保留子目录**（`_clean_step_output_dir` 已统一；generate-domains 为本轮新增，generate-sql-rag 由"仅删旧样例文件"改为统一目录清理）。如需清理旧产物，逐个命令带 `--clean` 执行或自行处理输出目录 | 不提供一次性全产物清理语义；如未来需要，另设计显式清理命令/脚本 |
| `--step standard` | 按需逐个执行 5 个单步命令 | 无自动串联；上游步骤产物缺失时由各单步自检报错 |

**加载迁移**：`pipeline load` 已移除；数据加载统一使用 `metaweave rag_load`，
默认加载 sql、table_schema、dim_value 与 cql，也可通过 `--type` 指定单类加载。

## 4. 文档更新（兑现 doc 24 §4.1/§6 债务 + 本轮新增变更）

- `docs/100_执行命令完整参考.md`：命令树去 pipeline；`--step` 取值表去
  standard；load 章节改写为 rag_load；新增迁移说明段（§3 表）
- `docs/迁移后运行指南.md`：旧 `metaweave load` 三行 → rag_load 等价命令
- `CLAUDE.md` / `AGENTS.md`：删 pipeline generate 9 步与 `--step standard`
  描述；Processing Pipeline 段改为单步命令说明；模块清单删
  `pipeline_cli.py`；`metadata --step` 帮助文本引用同步
- `README.md`：pipeline / standard 引用同步

**活文档范围界定（评审修订）**：本轮维护的活文档**仅包括**以下五份：
`README.md`、`AGENTS.md`、`CLAUDE.md`、`docs/100_执行命令完整参考.md`、
`docs/迁移后运行指南.md`。`docs/` 下其余编号设计/审计/方案文档与
`docs/update/` 全部历史记录均为**归档决策记录**（现有 35 份含 pipeline
引用），不作为当前 CLI 使用说明，本轮不回改。

## 5. 测试设计

新增 `tests/unit/test_cli_no_orchestration.py`（CliRunner + mock）：

1. 裸 `metaweave metadata`（无 step、无 generate 模式）→ UsageError
   "必须指定 step 或 generate 模式"，exit code 2
2. `--generate-domains` / `--generate-dim-config` / `--generate-sql-rag`
   不传 `--step` 仍正常执行（各自 mock）
3. `--step standard` → Choice 拒绝（exit 2）
4. `--step json_llm` → Choice 拒绝（exit 2）
5. `metaweave pipeline generate ...` 经**顶层命令组**（`main.cli`）→
   exit 2 + `No such command 'pipeline'`
6. `metadata --step json` 配置驱动 LLM 路径保留回归测试
   （`json_generation.*.llm_enabled` 开/关时 generator 对
   `JsonLlmEnhancer` 的调用行为，mock LLM）

删除 `test_pipeline_domain_config.py`（pipeline 专属部分）、
`test_step_all_orchestrator.py` 后，全量回归通过
（已知 2 个预存在问题除外）。

## 6. 验收标准

1. `metaweave pipeline`（任意子命令）→ `No such command 'pipeline'`（exit 2）
2. `metaweave metadata --step standard` / `--step json_llm` → Choice 拒绝
3. 裸 `metaweave metadata` → "必须指定 step 或 generate 模式"
4. 三个 `--generate-*` 独立模式不传 `--step` 正常工作
5. `metaweave metadata --step json -c configs/metadata_config.yaml` 真实
   冒烟通过（LLM 增强由配置驱动，产物含 LLM 注释/分类）
6. `metaweave --help` 顶层仅剩 `metadata` / `rag_load`
7. 新增单测全绿；全量回归通过（已知 2 个预存在问题除外）
8. 活文档中不得把 `metaweave pipeline` 或 `metadata --step standard` 作为
   **可执行、受支持的当前命令**展示；迁移说明中为说明"已移除"而保留的
   历史名称允许存在。配套检查：
   - **代码**：`rg -n 'pipeline_cli|pipeline_command|@click.group\(name="pipeline"\)' metaweave --glob '*.py'`
     —— 预期无结果
   - **活文档**：`rg -n -i 'metaweave pipeline|pipeline generate|pipeline load|--step standard' README.md AGENTS.md CLAUDE.md docs/100_执行命令完整参考.md docs/迁移后运行指南.md`
     —— 允许命中迁移段"已移除"类文字（人工复核），但不得存在仍指导
     用户执行的代码块或示例
