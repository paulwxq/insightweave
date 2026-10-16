# 26. 配置驱动的 `pipeline` 编排设计

> 状态：已实施（2026-09-20，两提交：路径统一 98d78ee + pipeline 实现见后续提交）
>
> 日期：2026-09-19
>
> 关联历史：doc 25 定义旧版 `pipeline generate/load` 与 `standard` 的移除边界；doc 27 定义 `rag_load` 的显式 `--type all` 与失败语义。本设计重新引入一个**配置驱动、仅负责编排既有单步命令**的 `pipeline`，不恢复旧命令的参数和隐式流程。

## 1. 目标与边界

新增顶层编排命令及两个章节子命令：

```bash
metaweave pipeline
metaweave pipeline metadata
metaweave pipeline rag_load
```

它们均读取独立的 `configs/pipeline.yaml`，再由该文件的 `metadata_config` 指向实际使用的
主配置。裸 `pipeline` 依次执行其中配置的 `metadata` 与 `rag_load` 步骤；`pipeline metadata`
仅执行 `metadata` 章节，`pipeline rag_load` 仅执行 `rag_load` 章节。

本命令的目标是为常用的多步骤任务提供可复现的声明式编排；它不新增元数据生成、加载或
路径覆盖能力。为保证生成、clean 预警与实际清理指向同一目标，本设计会在共享执行层统一
既有相对文件路径的解析基准，具体见 §6.2。

边界如下：

- 所有实际工作继续复用 `metadata` 和 `rag_load` 的既有执行逻辑；
- 依赖目录、输入输出目录、数据库、LLM、加载器等配置，继续读取
  `metadata_config.yaml` 的既有章节；
- v1 对所有 YAML 步骤仅允许覆盖 `clean`；`generate-domains` 步骤额外允许配置
  `description`、`md_context_mode`、`md_limit`，不提供 schemas、tables、workers、
  domain 等任意 CLI 参数透传；
- pipeline 不提供 `md_context_dir`、`domains_config`、`dim_config_path` 或其他路径
  覆盖。所有输入、输出和依赖路径继续由统一主配置的既有章节解析；其中相对文件路径统一
  以项目根目录为基准，绝对路径保持不变；
- 裸 `pipeline` 固定先执行 `metadata` 章节，再执行 `rag_load` 章节；两个子命令只执行
  各自对应章节；
- 本设计不恢复 `metadata --step standard`，也不恢复 `json_llm`、`rel_llm` 等独立步骤。

## 2. CLI 设计

```text
metaweave pipeline [--config/-c PATH] [--continue-on-error]
metaweave pipeline metadata [--config/-c PATH] [--continue-on-error]
metaweave pipeline rag_load [--config/-c PATH] [--continue-on-error]
```

选项必须写在最终实际执行的命令之后。合法形式仅为：

```bash
metaweave pipeline -c configs/custom_pipeline.yaml
metaweave pipeline metadata -c configs/custom_pipeline.yaml
metaweave pipeline rag_load -c configs/custom_pipeline.yaml --continue-on-error
```

当存在子命令时，禁止把 pipeline 选项写在子命令之前；以下写法必须以参数错误
（exit 2）退出，不能静默采用父级或子命令的默认值：

```bash
metaweave pipeline -c configs/custom_pipeline.yaml metadata
metaweave pipeline --continue-on-error rag_load
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--config`, `-c` | `configs/pipeline.yaml` | 三个入口均可使用。pipeline 配置文件路径先按项目根目录解析，必须是存在且可读的普通文件；否则以 CLI 参数错误退出（exit 2）。文件中的 `metadata_config` 指定被编排的主配置。 |
| `--continue-on-error` | `false` | 三个入口均可使用。某一任务失败后，记录失败并继续所选执行范围内的后续步骤。主要用于测试、排查和补跑准备。 |

为支持参数自然写在子命令之后，三个入口都应声明同一组 `--config/-c`、
`--continue-on-error` 选项（可复用同一 Click decorator），而不是依赖父级选项继承。
父级选项只服务于不带子命令的裸 `pipeline`，不得向子命令透传。
除根命令已有的全局参数（例如 `--debug`）外，pipeline 不增加其他参数。调试模式必须写在
MetaWeave 根命令之后、`pipeline` 之前，例如 `metaweave --debug pipeline rag_load`；
`pipeline` 及其子命令不另行声明 `--debug`。pipeline 中各 YAML 步骤的运行选项来自对应步骤
配置及其默认值。v1 的终端 traceback 保证仅覆盖 rag_load 任务；metadata 的边界见 §6.2。

裸 `pipeline` 要求两个章节合计至少存在一个可执行任务；`pipeline metadata` 则要求
`metadata` 章节至少存在一个可执行任务，`pipeline rag_load` 同理要求 `rag_load` 章节
至少存在一个可执行任务。子命令不会因另一章节有任务而执行或借用该章节的计划。

## 3. 配置文件结构

pipeline 使用独立的 `configs/pipeline.yaml`；数据库、输出路径、LLM、加载器等模块配置
仍保留在 `configs/metadata_config.yaml`。`pipeline.yaml` 的 `metadata_config` 可省略，
省略时默认为 `configs/metadata_config.yaml`；需要对不同环境或数据库编排时可显式指定
另一份主配置。`--config` 指定的 pipeline 配置文件路径，以及其中 `metadata_config` 的
相对路径，均相对项目根目录解析，而非当前工作目录；二者也都允许使用绝对路径。
`pipeline.yaml` 本身不执行 `${ENV}` 环境变量替换，其中的 `${...}` 只作为普通字符串处理；
被其引用的主配置仍通过既有 `ConfigLoader` 加载，并保留主配置原有的环境变量处理行为。

根级只允许以下三个键：`metadata` 与 `rag_load` 是两个步骤编排章节；
`metadata_config` 不是步骤类型，而是可选的主配置文件路径。任何其他根级键（例如误写的
`ragload`）均为配置错误，不能静默忽略。

以下内容是实施时必须交付的日常默认 `configs/pipeline.yaml`。它只执行可重复生成的
metadata 主流程，不自动覆盖人工维护的 domain/dim 配置，不默认加载外部数据库，也不默认
启用任何 `clean`：

```yaml
# configs/pipeline.yaml（日常默认）
# 被编排的 metadata / rag_load 使用这份主配置
metadata_config: configs/metadata_config.yaml

metadata:
  ddl:
  md:
  json:
  rel:
  cql:

rag_load: {}
```

因此，默认执行 `metaweave pipeline` 只运行 metadata 主流程；默认执行
`metaweave pipeline rag_load` 会因所选章节没有任务而以 exit 2 报错。错误消息必须明确说明
默认 `configs/pipeline.yaml` 不包含加载步骤，并提示用户通过 `-c` 指定自己的 pipeline
配置；维度值两阶段流程可使用专用的 `configs/pipeline_dim.yaml`，但该文件不能被描述为
通用 rag_load 模板。用户需要加载其他外部数据时，应复制或新建自己的 pipeline 配置，
显式选择所需 rag_load 类型及 `clean`。

首次初始化 domain/dim 配置使用单独的 `configs/pipeline_init.yaml`，不得复制为日常默认：

```yaml
# configs/pipeline_init.yaml（一次性初始化；会覆盖人工维护内容）
metadata_config: configs/metadata_config.yaml

metadata:
  ddl:
  md:
  generate-domains:
    # 可选：description: "请填写当前数据库的业务范围说明"
    md_context_mode: full
    md_limit: 100
  json:
  generate-dim-config:
```

运行该初始化配置会重写 `db_domains.yaml` 与 `dim_tables.yaml`，并将后者的
`embedding_col` 初始化为空。它只适合首次生成或用户明确决定重新初始化的场景；执行前
必须确认并备份已有人工内容。仓库交付的模板不得启用任何特定业务场景的 `description`；
直接运行模板时必须以 `description=None` 执行，用户只有主动取消注释并替换占位说明后，
该描述才可传入 LLM。

维度值加载仍需人工断点。`generate-dim-config` 与 `rag_load.dim_value` 可以写在同一份
专用配置中，但必须通过两个章节子命令分两次运行。

维度值的完整操作分为两次 pipeline 运行，示例如下：

```yaml
# configs/pipeline_dim.yaml（维度值两阶段专用）
metadata_config: configs/metadata_config.yaml
metadata:
  generate-dim-config:
rag_load:
  dim_value:
```

运行第一次配置后，用户必须人工填写生成的 `dim_tables.yaml` 中各表的 `embedding_col`；
然后才可执行第二次命令：

```bash
# 第一次：只生成 dim_tables.yaml
metaweave pipeline metadata -c configs/pipeline_dim.yaml

# 人工填写 output/dim_config/dim_tables.yaml 中的 embedding_col 后
# 第二次：只加载维度值
metaweave pipeline rag_load -c configs/pipeline_dim.yaml
```

### 3.1 配置项规则

- `metadata` 与 `rag_load` 下的**步骤键**同时表达“选择执行”与“该步骤选项”：步骤键存在即加入执行计划，未出现则不执行；
- 步骤键区分大小写，必须与 §3.3 / §3.4 表中的小写形式完全一致。`DDL`、`CQL`、
  `SQL`、`Generate-Domains` 等大小写变体均视为未知步骤键；pipeline 不沿用 CLI
  `Choice(case_sensitive=False)` 的大小写宽容行为；
- 步骤值为 `null`（例如 `md:`）或空对象（`md: {}`）时完全等价：执行该步骤，且使用默认选项；
- 步骤值为对象时读取其中的允许选项；步骤值为字符串、列表、数字或布尔值时为配置错误；
- `clean` 是所有步骤都允许的可选键，省略时使用该 YAML 步骤的默认值 `false`，等价于调用
  底层 metadata/rag_load 能力时不传 `--clean`。若显式配置，仅接受**未加引号的小写** YAML
  字面量 `true` / `false`；为避免 YAML 1.1
  布尔胁迫，`yes`、`no`、`on`、`off`、`True`、`FALSE` 等变体以及字符串
  `"true"` / `"false"` 均为非法；空值（`clean:`）、显式 `null`、整数、浮点数和其他
  类型也均为非法。这里必须区分“步骤值为空”和“选项值为空”：`md:` 表示选中 `md`
  并使用默认选项，而 `md: {clean: null}` 是配置错误；
- `generate-domains` 还允许以下可选键：
  - `description`：字符串或 `null`；省略、为 `null`、为空字符串或仅包含空白时均规范化
    为 `None`，非空字符串去除首尾空白后传入共享执行函数；
  - `md_context_mode`：区分大小写，仅接受 `name` / `name_comment` / `full`；省略时为
    `name_comment`；显式 `null` 也按该默认值处理；
  - `md_limit`：正整数（布尔值不视为整数）或 `null`；省略或为 `null` 时沿用
    `domain_generation.md_context_limit`，再回退到内建默认值；显式配置 `0` 或负数属于
    配置错误；独立 metadata CLI 的显式 `--md-limit` 也遵守同一规则，两个入口均不得沿用
    生成器将其静默钳制为 `1` 的宽松行为。这里的正整数约束针对两个入口的显式覆盖值；
    省略后读取的主配置 `domain_generation.md_context_limit` 暂时保留现有生成器校验/钳制语义；
- 上述三个键写入其他任务属于配置错误；
- 不支持任何路径覆盖键，包括 `md_context_dir`、`domains_config`、
  `dim_config_path`。MD 输出、domain/dim 配置、SQL-RAG 输出以及其他产物目录均由
  统一主配置的既有路径解析逻辑决定；主配置中的相对文件路径统一基于项目根目录解析，
  绝对路径保持不变；默认配置下使用各自的 `output/...` 路径；
- 两个章节允许任一个为空或缺失；但整个 pipeline 配置文件至少必须配置一个可执行 YAML 步骤；

`description` 去除首尾空白、空白值转为 `None`，以及显式 `md_limit` 必须为正整数，属于
`generate-domains` 的共享业务参数语义，不是 pipeline YAML 的专有行为。独立 metadata CLI
与 pipeline 必须复用 §6.2 的同一个规范化函数。YAML 节点类型、`null` 表达方式等仍由
pipeline 输入层检查。`md_context_mode` 的大小写是有意保留的输入语法差异：pipeline YAML
只接受表中所列的小写值；独立 CLI 继续由现有大小写不敏感的 Click `Choice` 接受输入，并在
进入共享执行层前转换成表中所列的规范小写值。两条入口进入 `MetadataExecutionRequest` 后
不得再存在取值语义差异。

步骤选项对象只能包含下表列出的键；出现其他键属于配置错误，而不是静默忽略：

| 步骤键 | 允许选项键 |
| --- | --- |
| 普通 metadata 步骤 | `clean` |
| `generate-domains` | `clean`、`description`、`md_context_mode`、`md_limit` |
| rag_load 步骤 | `clean` |

不使用 `commands[]`、`name` 或 `enabled` 字段：步骤键是否存在是唯一的选择机制。

#### 校验范围

无论使用裸 `pipeline`、`pipeline metadata` 还是 `pipeline rag_load`，都必须先校验整份
pipeline YAML，而不是只校验本次将执行的章节。整文件校验包括：根级与章节结构、重复键、
未知或非小写步骤键、步骤值类型、步骤选项白名单与类型，以及 `clean` 的严格字面量。
因此，即使运行 `pipeline rag_load`，未执行的 `metadata` 章节中存在 `DDL`、非法
`clean`、未知选项或重复键，也必须在任何任务开始前报错。

只有依赖本次执行范围的规则按入口判断：所选章节必须有任务；裸 `pipeline` 才检查
`generate-dim-config` 与 `dim_value` 不得同次执行；完整性告警、clean 清理计划和实际任务
也只针对本次选中的执行计划。`clean` 的键名、原始字面量和类型属于整文件结构校验；其
文件目录、Milvus collection 或 Neo4j database 等实际目标是否能够解析，则只校验本次
执行计划中的 `clean: true` 任务。配置文件的结构合法性不随入口改变，资源目标校验随入口
选择的任务集合变化。

#### 重复 YAML 键

`yaml.safe_load()` 会静默采用重复键的最后一个值，因此不能在普通配置加载后才检查。
原始 Node 预检必须覆盖 pipeline 配置的三层 mapping，重复键一律属于配置错误：

- 根 mapping 中重复的 `metadata_config`、`metadata` 或 `rag_load`；
- `metadata` / `rag_load` 下重复的步骤键，例如两个 `ddl`；
- 已选步骤的选项对象内重复的选项键，例如两个 `clean` 或两个 `md_limit`。

上述每层 mapping 的键还必须是原始 YAML **字符串 scalar**，即 Node 类型为 scalar 且 tag
为字符串；布尔、整数、浮点、`null`、序列、对象和 YAML 1.1 隐式布尔键均在普通加载前
报错。例如未加引号的 `on:` / `yes:` 会被 YAML 1.1 标记为布尔键，必须按原始文本报告
`metadata 包含非字符串步骤键: on`，不能等 `safe_load()` 后变成
`未知步骤键: True`。加引号的 `"on":` 是字符串键，但随后仍会因未知步骤键而报错。

错误信息应包含完整路径和原始键文本，例如 `根节点包含重复键: metadata`、
`metadata 包含重复步骤键: ddl`、`metadata.generate-domains 包含重复选项键: md_limit`。
确认键为字符串后，以其字符串值在同层查重，因此 `metadata:` 与 `"metadata":` 也视为
重复。预检只处理独立 pipeline 配置文件，不改变 `metadata_config.yaml` 主配置加载器的
既有行为。

#### YAML 空值、缺失与步骤值

pipeline 配置文件根节点必须为对象；`metadata` 与 `rag_load` 两个章节则可单独为空
或缺失。为避免 YAML 的 `null` 被宽松归一化后吞掉配置错误，按下表处理：

| YAML 形态 | 处理 |
| --- | --- |
| pipeline 配置文件根节点为 `null` 或不是对象 | 配置错误，执行前退出。 |
| 根级出现 `metadata_config`、`metadata`、`rag_load` 之外的键 | 配置错误，执行前退出。 |
| `metadata_config` 存在但不是非空字符串，或解析后不是存在、可读的普通文件 | 配置错误，执行前退出。绝对路径直接使用；相对路径按项目根目录解析。 |
| `metadata` / `rag_load` 章节缺失、章节为 `null` 或空对象 | 视为该章节没有任务；裸 `pipeline` 只要另一章节有合法步骤即可执行；对应章节子命令则因所选章节没有任务而报错。 |
| `metadata` / `rag_load` 章节为非对象值，例如字符串、列表、数字或布尔值 | 配置错误，执行前退出。 |
| 已知步骤键的值为 `null` 或空对象 | 执行该步骤，使用默认选项。 |
| 已知步骤键的值为字符串、列表、数字或布尔值 | 配置错误，执行前退出。 |

根节点为空对象、两个章节均缺失或为 `null`、或两个章节均为空对象，均属于“没有可执行
任务”，执行前报错。章节层面的 `null` 等价于缺失；步骤值的 `null` 则等价于空对象，
表示“选择执行且使用默认选项”。

### 3.2 `clean` 的清理范围与风险

`clean` 是所有 pipeline YAML 步骤共同支持的唯一选项，对应底层命令的 `--clean`；
`generate-domains` 另有 §3.1 定义的三个专属步骤选项。`clean` 省略时为 `false`；配置为
`true` 时，清理发生在对应任务写入或加载之前。各任务的清理目标不同，不能将其理解为统一
的“删除旧文件”操作：

| 任务 | `clean: true` 行为 | 风险说明 |
| --- | --- | --- |
| `ddl` / `json` / `md` / `rel` / `cql` | 删除各自输出目录顶层全部文件，保留子目录 | 步骤失败时，目录可能只剩部分新产物或为空。 |
| `generate-domains` | 删除 `db_domains.yaml` 所在目录顶层全部文件，保留子目录 | 默认目录当前包含 Git 跟踪的 `output/domains/db_domains.yaml`；该步骤每次执行都会重写该文件，`clean` 只会使生成失败时文件已丢失的风险更高。 |
| `generate-dim-config` | 删除 `dim_tables.yaml` 所在目录顶层全部文件，保留子目录 | 默认目录当前包含 Git 跟踪的 `output/dim_config/dim_tables.yaml`；该步骤每次执行都会重写文件并将 `embedding_col` 设为 `None`，会丢失人工填写内容；`clean` 只额外扩大生成失败时文件已丢失的风险。 |
| `generate-sql-rag` | 删除 `sql_rag.generation.output_dir` 顶层全部文件，保留子目录 | 会删除旧 Question-SQL 产物；步骤失败时可能没有可供 `rag_load.sql` 使用的完整样例。 |
| `rag_load.sql` | 清空对应 Milvus collection 后重新加载 | 加载失败时 collection 可能为空或只完成部分写入。 |
| `rag_load.table_schema` | 清空对应 Milvus collection 后重新加载 | 加载失败时 collection 可能为空或只完成部分写入。 |
| `rag_load.dim_value` | 清空对应 Milvus collection 后重新加载 | 加载失败时 collection 可能为空或只完成部分写入。 |
| `rag_load.cql` | 删除目标 Neo4j 数据库中的全部节点和关系后重新加载 | 风险最高：清理范围是目标 Neo4j 数据库，不限于本次生成的图数据。 |

表中的 Git 跟踪路径是当前仓库默认配置的事实；若用户修改了对应输出路径，`clean`
仍会删除目标目录的顶层文件。因此，对人工维护或需保留的产物，执行前应备份，或不
配置 `clean: true`。

### 3.3 `metadata` 可用步骤键

| 步骤键 | 实际调用的 metadata 模式 |
| --- | --- |
| `ddl` | `metadata --step ddl` |
| `md` | `metadata --step md` |
| `generate-domains` | `metadata --generate-domains` |
| `json` | `metadata --step json` |
| `generate-dim-config` | `metadata --generate-dim-config` |
| `rel` | `metadata --step rel` |
| `cql` | `metadata --step cql` |
| `generate-sql-rag` | `metadata --generate-sql-rag` |

`json` 的 LLM 注释和表分类能力继续由 `json_generation` 配置控制；它不是新的 `json_llm` 子命令。

### 3.4 `rag_load` 可用步骤键

| 步骤键 | 实际调用的加载类型 |
| --- | --- |
| `sql` | `rag_load --type sql` |
| `table_schema` | `rag_load --type table_schema` |
| `dim_value` | `rag_load --type dim_value` |
| `cql` | `rag_load --type cql` |

独立执行 `rag_load` 时，`--type` 是语义必选参数，且仅允许指定一次。只有
`--type all` 才会显式触发四类全量加载。pipeline 配置中的每个步骤键对应一次
明确类型的加载，不支持 `all` 步骤键。

两个章节中的 `cql` 含义不同：`metadata.cql` 生成 Cypher 文件；
`rag_load.cql` 将已有 Cypher 文件加载到 Neo4j。仅当使用裸 `pipeline` 且本次执行计划
同时包含两者时，才固定先完成 metadata 章节，因而生成发生在加载之前；执行
`pipeline rag_load` 时不会运行 `metadata.cql`，此时要求 CQL 输入文件已事先存在。

## 4. 排序与完整性策略

### 4.1 metadata 自动排序

`metadata` 的唯一标准执行顺序为：

```text
ddl → json → md → generate-domains → generate-dim-config
    → rel → cql → generate-sql-rag
```

（doc 17 将 `json` 提到 `md` 之前：md 的数据源改为 JSON 产物，依赖 `json` 先行。）

用户可配置任意子集。pipeline 从已出现的步骤键生成任务集合，再按上述标准顺序构造
实际执行计划。YAML 中步骤键的书写顺序不表达执行顺序，也不产生顺序告警；用户不能
借由配置键排列定义另一条执行流程。

### 4.2 可跳过步骤与完整性告警

下列步骤可任意跳过，跳过本身不构成错误：

- `generate-domains`
- `generate-dim-config`
- `generate-sql-rag`

`ddl`、`md`、`json`、`rel`、`cql` 也允许被跳过，以支持补跑和局部执行；但只要其中任意步骤未选择，pipeline 应记录完整性 warning，例如：

```text
⚠️ metadata pipeline 未选择完整主流程，缺少：md、rel。
   将继续执行已选择步骤；各步骤的输入前置条件仍由其自身校验。
```

pipeline 不把“流程不完整”升级为错误，也不人为补齐缺失步骤。若被选步骤需要上游产物，例如 `md` 需要 JSON 产物（doc 17）、`cql` 需要 JSON 与关系文件，仍由该步骤的既有前置校验决定是否成功。

上述完整性 warning **仅**统计 metadata 主流程的 `ddl`、`md`、`json`、`rel`、`cql`。
rag_load 的四种类型不参与完整性告警，任意子集均是合法执行计划；其产物文件、配置和外部
服务的前置条件由对应 loader 自行校验。`generate-domains`、`generate-dim-config`、
`generate-sql-rag` 缺失时不告警，也只表示它们不参与完整性统计，**不表示**后续已选
任务不再依赖它们此前生成或人工维护的产物。

pipeline 不做跨任务依赖补齐或全局依赖预判，避免将补跑、复用既有产物的合法子集误判为
错误。特别是，当主配置的 `relationships.domain` 为非空值时，任何 `metadata --step`
类型任务在解析有效 domain 时都需要已有的 `db_domains.yaml`；该文件可以由此前运行
`generate-domains` 生成或人工维护，并不要求本次 pipeline 必须选择该步骤。文件不存在时，
由被选任务按既有行为失败。**rel 任务的 domains 依赖校验（文件存在性、可解析性、
业务域列表非空）发生在该任务的任何 clean 副作用之前**——校验失败时旧关系产物
保持不变；`DomainResolver` 对空文件/损坏 YAML 会静默退化为空配置，因此共享
执行层必须显式校验 `get_all_domains()` 非空，避免"成功执行但未生成任何关系"。默认主配置的 `relationships.domain: null` 不触发这一前置。

### 4.3 rag_load 顺序

metadata 与 rag_load 的顺序策略有意不同。metadata 任务之间存在确定的产物依赖
拓扑，因此 pipeline 对其自动排序；四类 rag_load 仅依赖各自已存在的上游产物，彼此
没有加载依赖，也写入不同目标。pipeline 不把 YAML 步骤键的书写顺序作为接口；为保证
执行日志和失败即停时的行为稳定，使用固定内部顺序。

rag_load 已选择步骤的固定内部顺序为：

```text
sql → table_schema → dim_value → cql
```

该顺序仅用于确定性执行，不表示四类加载之间存在依赖，也不对 YAML 书写顺序校验或告警。

裸 `pipeline` 的同一次执行计划不得同时包含 `metadata.generate-dim-config` 与
`rag_load.dim_value`。前者会重写 `dim_tables.yaml`，并将所有 `embedding_col` 初始化为空；
后者会跳过空 `embedding_col` 的表且仍可能返回成功，无法在一次无人值守执行中替代人工
填写。裸命令同时选中两者属于配置错误，必须在执行任何任务前退出。

两个章节子命令不构成同一次执行计划：同一份 pipeline 配置可以同时声明这两个步骤，
先运行 `pipeline metadata`，人工填写 `embedding_col`，再运行 `pipeline rag_load` 即可。

独立执行 `rag_load` 时必须显式指定一次 `--type`；裸 `rag_load` 会以
UsageError（exit 2）拒绝执行。只有 `--type all` 才会展开为全量加载，展开顺序为：

```text
sql → table_schema → dim_value → cql
```

pipeline 不调用 `all`，而是按本节的固定内部顺序逐项调用已选择的明确类型。

### 4.4 执行前必须报错的配置问题

下列情况属于参数、配置或执行计划错误。pipeline 必须在执行任何任务前通过
`click.UsageError`（或等价的 Click 用法错误路径）退出，统一使用 **exit 2**；不得转换为
`click.Abort` / exit 1，`--continue-on-error` 也不适用：

- pipeline 配置文件根节点为 `null` 或不是对象；
- 根级出现 `metadata_config`、`metadata`、`rag_load` 之外的未知键；
- `metadata_config` 存在但不是非空字符串，或解析后不是存在、可读的普通文件（包括路径
  不存在、指向目录或无法读取）；
- 本次执行计划中的任一 `clean: true` 任务无法从已加载的主配置解析出唯一、明确且合法的
  清理目标，包括文件目录越出项目根目录，或应用既有运行时默认值后 Milvus
  endpoint/database/collection、Neo4j URI/database 仍无法确定；未进入本次执行计划的
  `clean: true` 步骤不解析实际目标；
- `metadata` / `rag_load` 章节不是对象；
- 已知步骤键的值不是对象或 `null`，或 `clean` 不是未加引号的小写 `true` / `false`；
- 步骤选项对象包含该步骤不支持的键，或 `generate-domains` 的 `description`、
  `md_context_mode`、`md_limit` 类型或取值非法；其中 `md_limit` 显式值必须为正整数；
- 出现未知步骤键；
- 根 mapping、步骤章节或步骤选项对象中出现非字符串 YAML 键；
- 裸 `pipeline` 的执行计划同时选择 `metadata.generate-dim-config` 与 `rag_load.dim_value`；
- pipeline 配置文件根 mapping 中出现重复键，或 `metadata` / `rag_load` 子树中出现重复
  步骤键、重复选项键；
- 裸 `pipeline` 的两个章节均没有可执行步骤（包括根节点为空对象、章节缺失/为 `null` 或两个章节均为空对象），或某个章节子命令所选章节没有可执行步骤。

章节子命令所选章节为空时，`UsageError` 必须包含实际 pipeline 配置路径和章节名，不能只写
“没有任务”。一般形式为：`<配置路径> 的 <metadata|rag_load> 章节没有可执行步骤`。当
`pipeline rag_load` 实际读取日常默认 `configs/pipeline.yaml` 时，还必须追加可操作提示：

```text
默认 configs/pipeline.yaml 不包含加载步骤；请使用
metaweave pipeline rag_load -c <包含 rag_load 步骤的配置文件>。
维度值两阶段流程可使用 configs/pipeline_dim.yaml。
```

最后一句只说明维度值专用入口，不得暗示 `pipeline_dim.yaml` 是 sql、table_schema 或 cql 的
通用加载模板。该错误属于执行计划/用法错误，统一 exit 2，且不能因
`--continue-on-error` 降级为 warning。

metadata 与 rag_load 的 YAML 步骤键书写顺序不参与配置校验。

**实现注记（读取失败统一 exit 2）**：pipeline YAML 的读取与解析异常
（`OSError` 权限/IO、`UnicodeError` 非 UTF-8、`yaml.YAMLError`）在
`load_and_validate_pipeline_yaml` 内统一转为 `PipelineConfigError`；
主配置经 `ConfigLoader` 加载的异常（`OSError`、`UnicodeError`、
`yaml.YAMLError`、`ValueError`（`${VAR}` 引用缺失无默认值））在计划阶段
统一转为配置错误。两者均在首个任务前 exit 2，不受 `--continue-on-error`
影响。

上述 YAML 结构、步骤键和选项结构错误始终针对整份 pipeline YAML 检查，包括本次入口不会
执行的章节；“所选章节为空”、`generate-dim-config` / `dim_value` 同次冲突，以及
`clean: true` 的实际目标解析则按本次入口及执行计划判断。未执行章节中的 `clean: yes`、
`clean: null`、未知选项或重复键仍须报错；结构合法的 `clean: true` 不会仅因未执行章节的
外部数据库配置不完整而阻止当前入口。

## 5. 失败处理

### 5.1 默认：失败即停

默认未传 `--continue-on-error` 时，任一实际任务返回 `success=False` 即停止后续执行，并以
exit 1 退出。这是生产执行的安全默认值，避免在上游失败后继续产生无意义或被旧产物污染的下游结果。
失败任务之后仍在执行计划中的任务必须保留在最终汇总中，并标记为 `⏭️ 未执行`，原因写为
“前序任务失败”；不得把它们从汇总中静默删除，也不得把未执行任务计为新的失败。

### 5.2 `--continue-on-error`：记录后继续

传入 `--continue-on-error` 后：

1. 失败步骤记录名称、错误摘要与耗时；
2. pipeline 继续尝试执行后续已配置任务；唯一由编排器主动跳过的特例是本节定义的
   `generate-sql-rag` 失败后阻断同次计划中的 `rag_load.sql`；
3. 全部计划步骤完成后输出成功/失败/未执行三态汇总；
4. 只要存在任一失败，整体仍以 exit 1 退出。

该模式主要用于测试、故障定位和获得尽可能多的执行结果；它不改变各子命令本身的成功判定。
它也不会回滚已执行任务的副作用：某任务在失败前已完成 `clean` 时，其目标文件、
Milvus collection 或 Neo4j 数据库已经被清理，后续任务是否继续执行不影响该结果。

pipeline 的任务级失败策略与 `rag_load` 使用同名开关并保持同向默认：均默认失败即停，
开启 `--continue-on-error` 才继续。pipeline 在自身执行计划层实现该策略；不将该
参数透传给单次 `rag_load --type <类型>` 调用，因为后者仅在 `--type all` 模式下生效。

继续模式不进行通用依赖推断或产物可用性求解。除以下 SQL 安全阻断外，后续任务仍按计划
尝试执行；缺少输入文件、配置或其他前置条件时，由该任务自身返回失败：裸 `pipeline` 的
同一次执行计划若同时包含 `metadata.generate-sql-rag` 与 `rag_load.sql`，且启用了
`--continue-on-error`、前者已经实际执行并返回 `success=False`，则后者必须标记为
`⏭️ 未执行`，原因写为“SQL-RAG 产物校验失败，为避免污染 Milvus”；其后的其他任务继续执行。

默认失败即停模式不使用上述 SQL 专用原因。任何任务失败后都立即终止任务循环，尚未执行的
全部剩余任务（包括另一章节中的任务）统一标记为“前序任务失败”。因此，默认模式下无论
`ddl` 失败还是 `generate-sql-rag` 失败，都不会进入 rag_load 执行；`rag_load.sql` 也只会
作为普通剩余任务被标记为未执行。

该保护只依据本次运行的实际结果：如果本次计划没有选择 `generate-sql-rag`，pipeline
不得阻止 `rag_load.sql`，因为用户可能有意加载此前生成并人工确认过的文件；如果
`generate-sql-rag` 返回成功（包括启用修复后仅产生 warning），`rag_load.sql` 也可正常执行。
这是一条运行期安全阻断规则，不是执行前依赖检查，也不改变任一共享执行函数返回的成功判定。

### 5.3 既有任务成功判定原样透传

pipeline 只根据共享执行函数返回的 `success` 调度后续任务，不得为任一子命令另加质量
门禁或改变其成功判定。尤其是：

- `generate-sql-rag`：生成阶段会先写出 Question-SQL pair 文件。当
  `sql_rag.validation.enable_sql_repair=true` 时，
  校验发现无效 SQL 后会尝试修复；修复失败的条目会从产物文件删除，任务仍返回成功并记录
  warning。仅当 `enable_sql_repair=false` 且存在无效 SQL 时，该任务才返回失败，此时包含
  无效 SQL 的 pair 文件不会被修正；`SQLExampleLoader` 自身不校验 SQL 有效性，因此必须
  应用 §5.2 的运行期安全阻断，不能让同次计划中的 `rag_load.sql` 将其加载到 Milvus；
  这里存在两层默认值：仓库发行的 `configs/metadata_config.yaml` 显式配置为 `true`，但代码
  使用 `validation_config.get("enable_sql_repair", False)`，因此自定义主配置省略该键时按
  `false` 处理，不启用修复，发现无效 SQL 后任务失败。pipeline 必须原样继承该代码缺省，
  不得因发行配置通常为 `true` 而自行补成 `true`；
- `rag_load.dim_value`：若全部表的 `embedding_col` 均为空，加载器会将它们计入
  `tables_skipped`，并以 `success=True`、`records_loaded=0` 返回。此情况不触发
  `--continue-on-error`，也不应由 pipeline 改判为失败。pipeline 应在最终汇总中输出
  明确 warning，列出 `tables_total`、`tables_processed`、`tables_skipped` 与
  `records_loaded`，避免“成功但零加载”被忽略。

因此，验证 `--continue-on-error` 的测试必须构造实际返回 `success=False` 的任务失败，
不能用“维度值零加载”作为失败替身。

| 场景 | 默认模式 | `--continue-on-error` |
| --- | --- | --- |
| 主流程缺少步骤 | 告警，继续执行子集 | 告警，继续执行子集 |
| 普通任务失败 | 立即停止，后续任务标记未执行，exit 1 | 记录失败，继续其他任务，最终 exit 1 |
| clean 当前目标无法解析或与预警目标不一致 | 当前任务失败且不清理任何目标，后续任务标记未执行，exit 1 | 当前任务失败且不清理任何目标，继续其他任务，最终 exit 1 |
| `generate-sql-rag` 失败且同次计划包含 `rag_load.sql` | 后续任务因失败即停而未执行，exit 1 | `rag_load.sql` 因安全阻断标记未执行，其他任务继续，最终 exit 1 |
| 配置结构、步骤键、选项值错误 | 执行前报错，exit 2 | 执行前报错，exit 2；继续开关不生效 |

### 5.4 退出码契约

| 结果 | 退出码 |
| --- | ---: |
| 所有已执行任务成功；包括仅有 warning、`dim_value` 零加载等既有成功结果 | 0 |
| 任一已开始任务返回 `success=False`；包括任务阶段配置重载失败、clean 当前目标无法解析或与预警目标不一致；默认停止或继续模式最终有失败 | 1 |
| Click 参数、pipeline YAML、整文件结构或执行计划错误 | 2 |

exit 2 的范围包括但不限于：pipeline 配置文件不存在/不可读/非普通文件、YAML 解析失败、
根节点或章节结构错误、未知键、重复键、大小写错误、非法步骤值或选项、非法 `clean`、
`metadata_config` 类型或路径错误、空计划、章节子命令所选章节没有任务、裸 pipeline 的
dim 同次冲突、本次执行计划中 `clean: true` 的实际清理目标无法解析，以及父子命令选项
位置错误。所有这些错误均发生在首个任务之前，不能受 `--continue-on-error` 影响。

## 6. 实现设计

### 6.1 CLI 注册

新增 `metaweave/cli/pipeline_cli.py`，在 `metaweave/cli/main.py` 注册：

```python
cli.add_command(pipeline_command)
```

`pipeline` 是可不带子命令执行的 Click group：裸 `metaweave pipeline` 执行两个章节；
`metadata`、`rag_load` 是仅执行对应章节的子命令。它们不恢复旧版的
`pipeline generate/load` 子命令或参数语义。

group 必须使用 `invoke_without_command=True`，且回调严格遵守以下分派规则：

```text
ctx.invoked_subcommand is None
  → 这是裸 pipeline；解析并校验 group 的 --config / --continue-on-error
  → 加载配置并执行 metadata + rag_load

ctx.invoked_subcommand is not None
  → 检查 group 层两个选项的 ParameterSource
  → 任一选项来自 COMMANDLINE：抛 click.UsageError（exit 2），提示选项须写在子命令之后
  → 两个选项均为默认来源：立即 return，由子命令独立解析、校验和执行
```

有子命令时，group 回调不得检查默认 `configs/pipeline.yaml` 是否存在，不得加载任何
pipeline/主配置，不得创建执行计划或执行任务。这样
`metaweave pipeline metadata -c configs/custom_pipeline.yaml` 即使默认配置文件不存在，
也只会读取用户指定的文件；同时避免 group 与子命令各执行一次。

group 的 `--help` 必须明确说明：不带子命令时，裸 `pipeline` 会先执行 `metadata`，再执行
`rag_load`；两个子命令只执行各自章节。

根 `cli` 必须通过 `click.Context.obj` 保存全局调试状态；可采用以下合同：

```python
# metaweave 根 cli
ctx.ensure_object(dict)
ctx.obj["debug"] = debug

# pipeline group / 子命令
effective_debug = bool(ctx.find_root().obj.get("debug", False))
```

有子命令时 group 回调虽然不加载配置或执行任务，但仍须保证子命令能从根 context 读取
全局状态；不得把 group 自己的默认选项字典误当成根 context。

### 6.2 复用执行逻辑，而非嵌套 Click

pipeline 的行为是调用 metadata / rag_load 的既有能力，但实现上不得：

- 从 pipeline 内部再次调用 Click 命令对象；
- 使用 `CliRunner` 模拟 CLI；
- 启动子进程执行 `metaweave metadata` 或 `metaweave rag_load`。

应将两类 CLI 的可执行部分抽取为可复用的 Python 执行函数：

- metadata：接收配置路径、操作名称、`clean` 和可选的 `expected_clean_target`，执行一个 metadata 模式；仅当
  操作是 `generate-domains` 时，再接收 `description`、`md_context_mode`、
  `md_limit`；
- rag_load：复用现有 `_run_single_load()` 的单类型加载能力，提供一致的成功/失败结果；
  每次调用都将根 context 的全局 `debug` 传入其 `debug` 参数，并允许 pipeline 传入可选的
  `expected_clean_target`；
- CLI 包装层负责解析命令行参数；pipeline 负责解析 YAML、生成计划、调用执行函数和汇总结果。

一个 `PipelineTask` 必须只执行一个确定的 metadata operation 或一个确定的 rag_load
类型。pipeline 不得将多个 YAML 步骤拼成一次 `metadata_command()` 调用：现有
`--generate-*` 模式彼此互斥，`--step` 也只接受一次；每个任务都必须独立调用共享执行层。

metadata 共享执行层须将既有的 `click.Abort`、任务运行期 `UsageError` 等失败路径收敛为
与 `_run_single_load()` 一致的结果字典，至少包含：

```python
{"success": bool, "execution_time": float, "message": str}
```

它不向 pipeline 抛出 `click.Abort`。pipeline 据此实现默认停止或
`--continue-on-error` 的后续调度；YAML 结构、步骤键和选项值等计划级配置错误仍必须在
任何任务开始前直接报错。

metadata CLI 也必须通过该共享入口执行单个已确定 operation：Click 包装层只负责解析、
归一化和校验 CLI 参数，构造 `MetadataExecutionRequest` 后调用共享函数；若结果
`success=False`，再由 CLI 包装层输出既有摘要并转换为 `click.Abort`。不得保留一条仅供
CLI 使用、与 pipeline 共享入口分叉的业务执行路径。

这样可避免 Click context、`click.Abort`、退出码处理在嵌套调用中失真，同时保证单独执行与 pipeline 执行走同一业务逻辑。

metadata 的执行请求可采用以下边界（字段名仅为签名草案）：

```python
MetadataExecutionRequest(
    config_path: Path,
    operation: MetadataOperation,
    clean: bool = False,
    expected_clean_target: ResolvedCleanTarget | None = None,
    domains_config_override: Path | None = None,
    md_context_dir_override: Path | None = None,
    dim_config_path_override: Path | None = None,
    description: str | None = None,       # 仅 generate-domains
    md_context_mode: str = "name_comment",  # 仅 generate-domains
    md_limit: int | None = None,          # 仅 generate-domains
)
```

三个 `*_override` 字段表示用户是否**显式**覆盖路径，而不是解析后的最终路径，作用域如下：

| 请求字段 | 允许使用的 operation |
| --- | --- |
| `domains_config_override` | `generate-domains`、`generate-sql-rag`，以及需要 DomainResolver 的普通 metadata 步骤 |
| `md_context_dir_override` | `generate-domains`、`generate-sql-rag` |
| `dim_config_path_override` | `generate-dim-config` |

pipeline v1 不提供路径覆盖能力，因此构造任何 `MetadataExecutionRequest` 时，这三个字段始终为
`None`。独立 metadata CLI 的包装层负责读取 Click `ParameterSource`：只有参数来源为
`COMMANDLINE` 时才把对应路径写入请求，否则传 `None`。特别是 `--md-context-dir` 的 Click
默认值虽然是 `output/md`，用户未显式传入时也必须转换为 `md_context_dir_override=None`，
不能覆盖主配置中的 `output.markdown_directory` 及其回退链。`domains_config_override` 与
`dim_config_path_override` 同样只表达显式 CLI 覆盖。请求进入共享执行层后，覆盖值和主配置
回退值都必须通过 §6.2 的共享项目路径 resolver 解析。

`generate-domains` 的请求参数必须通过共享的纯函数规范化（函数名仅为草案）：

```python
normalize_generate_domains_options(
    description: str | None,
    md_context_mode: str | None,
    md_limit: int | None,
) -> NormalizedGenerateDomainsOptions
```

该函数是两个入口唯一的语义事实源：`description` 为 `None`、空字符串或纯空白时返回
`None`，否则返回去除首尾空白后的字符串；显式 `md_limit` 必须是非布尔的正整数，`0`、
负数及其他类型均拒绝，`None` 则保留为“未覆盖”，随后由任务配置快照读取
`domain_generation.md_context_limit` 并回退到内建默认值；`md_context_mode=None` 规范化为
`name_comment`，其他值必须已经是规范小写值 `name`、`name_comment`、`full` 之一，否则
拒绝。该函数本身不得执行大小写转换：pipeline YAML 的 `FULL` 已在输入层拒绝；独立 CLI
的 `FULL` 必须先由大小写不敏感的 Click `Choice` 转换为 `full`，再调用本函数。直接 Python
调用传入 `FULL` 时也必须由本函数拒绝。

pipeline 在构造执行计划时调用该函数，非法值作为计划配置错误在任何任务开始前 exit 2；
metadata Click 包装层在构造 `MetadataExecutionRequest` 前调用同一函数，非法的显式 CLI
参数转为 `click.UsageError`、exit 2。共享 metadata 执行函数入口还必须使用同一函数做
防御性规范化，避免未来 Python 调用者绕过两个输入层；若绕过输入层传入非法请求，共享函数
在任何 clean、文件写入或外部调用前返回 `success=False`。不得在 YAML 解析器、Click 回调和
共享执行层各维护一份不同规则。`DomainGenerator` 现有 `max(1, md_context_limit)` 可暂时保留
为主配置回退值和直接 Python 兼容调用的底层防御，但不能再把 pipeline YAML 或独立 CLI 的
显式 `0`、负数静默钳制为 `1` 当作正常行为。测试 46 锁定的是这两个显式覆盖入口；本设计
不同时收紧 `domain_generation.md_context_limit` 主配置键的既有钳制语义。

rag_load helper 的内部签名相应扩展为（字段名仅为草案）：

```python
_run_single_load(
    load_type: str,
    config_path: Path,
    clean: bool,
    debug: bool,
    expected_clean_target: ResolvedCleanTarget | None = None,
    emit_clean_warning: bool = True,
) -> dict
```

`expected_clean_target` 不是 CLI 或 YAML 参数，只是 pipeline 将预警阶段目标传入共享执行层的
内部契约。独立 metadata / rag_load CLI 调用时传 `None`；pipeline 对 `clean: true` 任务传入
计划中保存的期望目标，对 `clean: false` 任务传 `None`。

pipeline 对其他 metadata 可选参数一律不传，也不暴露任何路径字段。路径的语义是
“未覆盖”，由既有主配置解析：例如 `--step md` 的输出继续取
`output.markdown_directory`（默认 `output/md`）；`generate-domains` 和
`generate-sql-rag` 读取 MD、查找 domain 配置时继续走各自现有的默认路径解析链。
路径不存在时，由执行的步骤按现有行为报错。

#### 相对路径基准的有意统一

共享执行层抽取前，必须先将主配置中**所有语义为文件或目录的相对路径字段**统一为相对
项目根目录解析，不能继续把进程 cwd 作为隐式配置。这里既包括显式的 `*_directory`、
`input_file`、`config_file` 等字段，也包括 `output.output_dir` 及其派生子目录等回退路径；
不能只修正当前暴露问题的几个输出字段。绝对路径保持不变；涉及 `clean` 的文件目标仍受
“必须位于项目根目录内”的安全限制。生成输出、依赖输入、loader 输入、clean 目标解析和
§6.4 的清理预警必须复用同一个项目路径 resolver，禁止各模块自行使用 `os.getcwd()`、
`Path.cwd()`、直接拼接 `get_project_root()` 或另一套等价逻辑。

这是对独立 metadata CLI 的有意行为修正，不是“重构但行为零变化”。当前代码存在混合
基准：`OutputFormatter.output_dir` 及 ddl/json/md 显式目录、`RelationshipWriter` 的 rel
相对路径跟随 cwd，`_clean_step_output_dir()` 也使用 cwd；而关系发现读取 JSON、CQL、
domain、dim 和 SQL-RAG 等路径主要基于 `get_project_root()`。因此既可能发生
`RelationshipDiscoveryPipeline` 从项目根读取 JSON、`RelationshipWriter` 却向 cwd 写 rel，
也可能发生 CQL 生成到项目根目录、clean 却清理 cwd 下同名目录。只修改三个
`*_directory` 字段还会漏掉 `OutputFormatter.output_dir/<step>` 的回退路径。

实施时必须至少审计并迁移以下路径链路，而不是把类名清单当成封闭范围：

- 生成输出：`OutputFormatter.output_dir` 及 ddl/json/md 回退目录、关系 rel、CQL、domain、
  dim config、SQL-RAG `output_dir`；
- 依赖输入：MD 读取 JSON（doc 17）、关系发现读取 JSON、CQL 读取 JSON/rel、domain 与 SQL-RAG 读取
  MD/rel/domain、dim config 读取 JSON；
- loader 输入：`input_file`、`config_file`、`metadata_config_file`，包括显式配置值与自动
  推导值；已使用 `get_project_root()` 的 loader 也要迁移到共享 resolver；
- clean 与预警：`_clean_step_output_dir()`、三个 generate 操作的清理目标，以及 §6.4 的
  真实目标展示和运行期目标复核。

代码审计至少执行下面的全仓检索，并逐项记录为“已迁移”“确认不是文件路径”或“绝对路径
无需转换”；不得只处理检索时已知的三个类：

```bash
rg -n 'Path\.cwd|os\.getcwd|get_project_root|ensure_dir|output_dir|[a-z_]+_directory|input_file|config_file|metadata_config_file' metaweave tests
```

因此，当 CLI 从项目子目录运行时，相对路径不再落到该子目录，而仍指向项目根目录下的
目标；从项目根目录运行时，仓库默认配置的行为不变。该变化必须写入迁移说明，并同时作用于
独立 `metaweave metadata` 与 pipeline，避免两个入口产生不同产物。不得为了“冻结旧行为”
保留 cwd 语义；若现有测试依赖 cwd 下的临时目录（例如
`tests/unit/test_step_clean_semantics.py`），应改用绝对测试路径或显式项目根 fixture。

#### clean 期望目标的运行期复核

本设计只采用一种一致性方案：**计划阶段保存期望目标，执行层使用同一 resolver 从该任务
实际加载的配置快照重新解析当前目标，再比较二者**。不得改成“预警使用计划目标、loader
自行再次读取配置并直接清理”的双轨实现，也不得把计划目标作为配置覆盖值强行注入 loader。

每个 `clean: true` 任务的共享执行层必须按以下顺序处理：

1. 任务开始后加载一次主配置，得到该任务唯一的配置快照；
2. 使用与计划阶段相同的 resolver，从该快照解析 `current_clean_target`；
3. 在创建任何清理副作用之前，将其与 pipeline 传入的 `expected_clean_target` 比较；
4. 相等时，后续 loader / generator、clean 和业务执行全部使用这同一份配置快照，不得再次
   从磁盘读取主配置；
5. 不相等，或任务开始后配置已无法读取、当前目标已无法解析时，立即返回
   `success=False`：既不清理期望目标，也不清理当前目标。

比较使用 `ResolvedCleanTarget` 的规范化、非敏感身份字段，例如文件绝对目录，或外部数据库
的脱敏 endpoint + database + collection；显示文本不参与相等判断，密码、Token 和用户名
不得进入比较错误消息。构造身份字段时必须先执行一致的类型和值规范化：文件目录使用共享
resolver 得到的规范绝对路径；host 转为去除首尾空白的小写字符串；port 拒绝布尔值并校验为
合法端口后转为无前导零的十进制字符串，因此 YAML 整数 `19530`、字符串 `"19530"` 和环境
变量展开得到的同值字符串必须判定为同一端口；database 与 collection 转为去除首尾空白的
字符串但保留大小写。相等判断只比较这些规范化身份字段，不比较原始 YAML 类型、原始 URI
文本或预警展示文本。该检查发生在该任务 clean 之前，而不是整个 pipeline 执行前。

此类不一致表示“预警完成后运行环境发生变化”，属于已经开始任务的运行期失败：任务状态为
`failed`，整体 exit 1，不是 `skipped` 或 exit 2。默认模式据此停止后续任务并把剩余任务标记
为“前序任务失败”；`--continue-on-error` 模式记录该失败后按 §5.2 继续。若失败任务是
`generate-sql-rag`，继续模式仍按 §5.2 阻断同次计划中的 `rag_load.sql`。

独立 metadata / rag_load CLI 没有预警阶段，`expected_clean_target=None`；共享执行层仍从
唯一配置快照解析一次当前目标并用于 clean，但不执行期望值比较。
**实现注记（用户审核第三轮第 3 条）**：`verify_clean_target()` 与
`_run_single_load()` 的复核分支在 `clean=true` 时**始终**解析一次当前目标
（`clean=false` 才跳过）——独立 CLI 因此与 pipeline 共用同一套 clean 安全
边界（配置节点类型校验、Milvus host/port 校验、Neo4j URI 严格解析、唯一
目标解析）；`expected` 为 None 时解析成功即通过。解析 current 时须透传
`domains_config_override`/`dim_config_path_override`，保证与独立 CLI 实际
clean 执行的目标完全一致。计划阶段首次解析不出目标
仍属于 §4.4 的执行前配置错误、exit 2；只有计划已成功、任务阶段重新加载或比较失败时才是
本节定义的 exit 1。

共享 metadata 执行层必须使用与用户裸执行 metadata、且未传可选参数时一致的语义
默认值：`schemas`、`tables`、`domain`、`cross_domain` 均为“未覆盖”；
`max_workers=4`；`domains_config`、`dim_config_path` 及其他
路径类参数均为“未覆盖”，继续走统一主配置的既有解析链。pipeline 不得为这些字段
自行定义不同默认值。对 `generate-domains`，未配置时 `description=None`、
`md_context_mode="name_comment"`，`md_limit` 按 §3.1 的主配置回退链处理；显式覆盖值必须先
经过上述共享规范化，不能把独立 CLI 的原始字符串或非正整数直接交给生成器。

共享 metadata 执行层不得直接把 YAML operation 名称传给 `set_current_step()`，也不得沿用
当前 `set_current_step((step or "").lower())` 的宽泛逻辑；必须使用以下固定映射：

| metadata operation / 阶段 | `set_current_step(...)` 值 |
| --- | --- |
| `ddl` | `ddl` |
| `md` | `md` |
| `generate-domains` | `generate-domains` |
| `json` | `json` |
| `generate-dim-config` | `dim_config` |
| `rel` | `rel` |
| `cql` | `cql` |
| `generate-sql-rag` 生成阶段 | `sql-rag-generate` |
| `generate-sql-rag` 校验阶段 | `sql-rag-validate` |

`generate-sql-rag` 允许在同一个任务内部按阶段二次切换日志 step；若配置跳过校验阶段，
则只设置 `sql-rag-generate`。映射由共享执行层统一负责，独立 metadata CLI 与 pipeline
不得各自复制一套。

这是对独立 metadata CLI 现有行为的有意修正：当前 `generate-domains` 和
`generate-dim-config` 因没有 `--step` 值而设置成空字符串，无法命中 `logging.yaml` 的
`generate-domains` / `dim_config` filter。实施本设计后，独立 CLI 也必须通过共享执行层
获得正确分流，不保留旧的空 step 行为。

每个任务还必须按其 `config_path` 独立且仅调用一次既有的 `load_config()` / `ConfigLoader`
链路；这次调用产生的唯一配置快照，同时用于解析 `current_clean_target`、构造 loader /
generator、执行 clean 和后续业务逻辑。pipeline 的预校验加载不能替代任务执行时的配置
加载，但同一任务内部不得再次读取配置；跨任务不共享配置快照，因而重复输出既有的 `.env`
加载日志属于预期副作用。

#### 单任务配置快照注入合同

上述“唯一配置快照”必须通过下游组件的配置注入真正落实，不能只在共享执行函数中增加
`expected_clean_target` 后，仍把 `config_path` 交给下游重新读盘。当前
`MetadataGenerator`、`CQLGenerator`、`RelationshipDiscoveryPipeline` 会在构造时从路径
重新加载配置；`DimValueLoader`、`TableSchemaLoader`、`SQLExampleLoader` 也会在
`validate()` 或后续懒加载路径中重新读取 `metadata_config_file`。若保持现状，会形成
“计划预检快照 → 任务比较快照 → 下游实际执行快照”三套配置，使 clean 目标一致性检查失效。

pipeline 实现提交必须同时完成以下改造：

- `MetadataGenerator`、`CQLGenerator`、`RelationshipDiscoveryPipeline` 支持注入已完成
  ConfigLoader 展开和模块配置校验的完整主配置字典；共享 metadata 执行层必须使用注入入口，
  不得只传 `config_path`；
- 三个 `generate-*` operation 的共享实现必须直接接收同一主配置快照，或只接收从该快照解析
  出的明确路径和模块配置，禁止再次调用 `load_config()` / `ConfigLoader`。现有
  `generate-domains`、`generate-dim-config` CLI 分支中的配置加载必须上移到共享 metadata
  任务入口：前者结合 `domains_config_override`、`md_context_dir_override` 解析路径后将快照
  传给 `DomainGenerator`；后者结合 `dim_config_path_override` 从快照解析 JSON 输入与
  `dim_tables.yaml` 输出路径后再构造 `DimTableConfigGenerator`。三者都必须抽成独立于 Click
  的 operation 执行函数，由独立 metadata CLI 和 pipeline 共用，并遵守本节的结构化结果、
  不抛 `click.Abort` 合同；不得在两个入口各复制一份 generate 分支；
- `_run_generate_sql_rag()` 必须从“接收 `config_path` 并自行加载”改为接收已加载的
  `main_config` 快照，以及请求中的 `domains_config_override`、
  `md_context_dir_override`；`config_path` 若保留，只能用于日志、诊断或来源标识，不能触发
  读盘。LLM 模块路径预检、domain/MD/rel 路径解析、clean、生成、SQL 校验与修复必须全部
  使用该快照，确保其失败结果和同次 `rag_load.sql` 安全阻断也基于同一配置；
- `DimValueLoader`、`TableSchemaLoader`、`SQLExampleLoader` 支持注入同一份完整主配置快照，
  不能只收到 `loaders` 子树后再通过 `metadata_config_file` 读取 database、embedding、
  vector_database 或 sql_rag；`LoaderFactory.create()` 或等价的运行时上下文必须能够同时传入
  loader 配置和完整主配置快照。`CQLLoader` 不需要重新读取完整主配置，但它收到的
  `cql_loader` 配置也必须是从该快照派生的副本，不得另行读盘；
- 上述类可以保留 `config_path` 作为兼容的直接类调用回退入口，以及日志、诊断和配置来源
  标识；但当调用方已经注入配置快照时，路径参数不得触发 `ConfigLoader`、`load_config()`、
  `_load_metadata_config()` 或任何文件重读；
- 构造 API 必须消除歧义：实现可采用“`config` 优先、缺失时才读取 `config_path`”，或要求
  两者恰有一个作为数据来源。无论采用哪种签名，共享执行层都必须走注入分支；
- `_run_single_load()` 从完整快照派生 loader 配置时必须复制所需子树，再补充自动推导的
  `input_file` 等运行时值；不得像现有实现一样直接修改 `full_config["loaders"]` 引用，污染
  任务快照。派生副本不构成新的磁盘快照，其所有值仍必须来自本任务唯一快照；
- clean 目标复核成功后，文件清理、Milvus/Neo4j 目标、生成器输入输出目录、数据库连接、
  embedding 与 LLM 配置均只能来自该快照或其派生副本。任务期间即使磁盘上的主配置发生
  变化，也不得影响已经开始的任务。

独立 metadata / rag_load CLI 实施后也通过同一共享执行层加载一次并注入快照，不保留“独立
CLI 允许下游重读、pipeline 禁止重读”的分叉。

**实现注记（快照合同落地，含一处行为收敛）**：CLI 包装层不再提前加载业务配置——
`load_config` 及其后的 LLM 模块路径预检、废弃关系配置节点校验统一移入共享执行层的
`load_main_config_snapshot()`（每个任务调用恰好一次；`generate-sql-rag` 分支也改为经
共享入口调用，不再由 CLI 直连内部函数）。由此：

- 独立 CLI 单步任务的配置读盘由最多 3 次降为恰好 1 次（实测 md/cql 均 1 次），
  快照贯穿前置检查、clean 复核、下游构造与执行；
- 原先在 CLI 层执行的非 LLM 配置校验（非法 llm 路径、废弃节点）失败时，
  独立 CLI 的退出码由 exit 2 收敛为 exit 1（共享层统一返回失败结果，CLI 以
  `click.Abort` 结束）——与 §5.4“任务阶段配置失败”语义一致；
- **参数防御先于 clean（用户审核第三轮第 2 条）**：共享执行层的参数级校验
  （如 `max_workers` 非正整数）前移至配置快照加载之后、一切依赖检查与
  `clean` 副作用之前——共享层支持直接 Python 调用（CLI 的 IntRange 拦不到），
  校验失败时不得删除旧产物；六个执行函数的 clean 调用点均已审计，其前
  不存在可失败的校验；
- `_run_generate_sql_rag()` 改为**结构化返回** `Optional[str]`（None=成功；
  字符串=失败原因）——原先的四处 `raise click.Abort()`（domains 缺失、md 目录
  缺失、生成失败、质量门禁终止）全部改为返回原因；`execute_generate_sql_rag`
  消费该返回值构造结果字典，并在通用 except 中对空消息兜底
  （`str(e) or type(e).__name__`），修复 `str(click.Abort()) == ""` 导致
  pipeline 汇总"只有失败标记无原因"的问题；
- `_resolve_md_context_dir` 的 Click 版本删除，仅保留共享纯函数
  `resolve_md_context_dir(config, override)`；`--md-context-dir` 的显式传参判断
  仍只由 CLI 包装层完成并写入请求的 `md_context_dir_override`；
  `_warn_out_of_scope_params` 等 CLI 层职责保持不变。直接在 Python 中实例化上述类且只提供路径的
兼容用法可以继续自行加载一次；该兼容入口不属于一个已由共享执行层开始的任务。此合同覆盖
全部八个 metadata operation：ddl、md、json、generate-domains、generate-dim-config、rel、
cql、generate-sql-rag，不能只覆盖使用三个下游类的普通步骤。

为支持 `rag_load.dim_value` 的零加载汇总 warning，`DimValueLoader` 的成功结果应额外提供
`tables_total`；`_run_single_load()` 应将该加载器已有的 `tables_processed`、
`tables_skipped`、`records_loaded` 与 `tables_total` 作为可选 `metrics` 字段透传给
pipeline。该字段只增强可观测性，不改变既有 `success`、退出码或
`--continue-on-error` 语义。

`_run_single_load()` 的 `debug` 参数不能固定为 `False`：裸 pipeline 和
`pipeline rag_load` 均须传入根命令的 `effective_debug`。该值除控制日志级别外，还决定
异常时是否通过 `exc_info=True` 记录堆栈并向终端输出 `traceback.format_exc()`；因此
`metaweave --debug pipeline rag_load` 的失败诊断能力必须与直接启用 rag_load debug 时一致。

v1 不把这一终端 traceback 合同扩展到 metadata 任务：`MetadataExecutionRequest` 和共享
metadata 执行函数不新增 `debug` 参数，metadata 失败仍返回结构化结果，由 pipeline 按失败
策略汇总；pipeline 不得自行捕获该结果后合成或打印 metadata traceback。根级 `--debug`
仍按现有根 CLI 行为提高全局日志级别，因此 metadata 内部日志可能包含其既有诊断信息，但
本设计不保证终端显示完整堆栈。未来若要统一两类任务，必须单独扩展 metadata 执行合同和
对应测试，不能假定 rag_load 的 `debug` 参数已自动覆盖 metadata。

Click 包装层与共享执行层必须明确分离：

- Click 包装层保留 `--step multiple=True` 的重复传入检查与 tuple 归一化、
  `Choice`/缺参等参数校验、多模式互斥检查，以及
  `_warn_out_of_scope_params()` 的命令行作用域告警；
- `_warn_out_of_scope_params()` 和当前 `_resolve_md_context_dir()` 中依赖
  `click.Context.get_parameter_source()` 的“是否显式传参”判断，不能由 pipeline
  调用，也不得读取 pipeline 命令的 Click context；该判断只能由独立 metadata CLI 包装层
  执行，并转换为请求中的 `domains_config_override`、`md_context_dir_override`、
  `dim_config_path_override`。抽取后的路径解析函数只接收这些 Optional 覆盖值，pipeline 始终
  传 `None`。因此 pipeline 的 MD 目录只按主配置的
  `output.markdown_directory` 及其既有回退链解析，而不采用 metadata CLI 的
  `--md-context-dir` 参数默认值作为覆盖；
- 共享执行层只负责一次已确定 operation 的业务执行、清理、依赖检查与结构化结果，
  不读取或构造 Click 参数来源。

pipeline CLI 在加载 `metadata_config` 前，必须先读取原始 `pipeline.yaml` 并检查
根 mapping、`metadata` / `rag_load` 章节 mapping、各步骤选项 mapping 的键类型与重复键，
以及 `clean` 的原始 scalar 字面量。可使用 `yaml.compose()` 保留原始 Node 树后递归检查；
每个目标 mapping 的 key Node 必须是字符串 scalar，随后按原始字符串值做同层查重。
不得依赖 `yaml.safe_load()` 产生的 dict，因为重复键届时已经被静默覆盖，未加引号的
`yes` / `on` 等 YAML 1.1 键名和布尔值也已无法与其原始文本区分。完成原始 Node 预检后，
再正常加载 pipeline 配置，并按 `metadata_config` 加载既有主配置，以保留环境变量替换和
其他主配置章节的现有行为。该预检不扩展到独立的主配置文件。

为避免多个 `rag_load` 任务的 `clean: true` 重复打印“即将清空目标数据库”的通用
警告，`_run_single_load()` 可增加内部参数 `emit_clean_warning: bool = True`：单独执行
`rag_load` 时保持 `True`，pipeline 调用时传 `False`。该参数不属于对外 CLI 或 YAML
配置。

本设计实施并实际扩展该 helper 签名时，再同步修订 doc 27 的状态头和 §3：说明新增
`emit_clean_warning` 仅用于内部输出控制，新增 `expected_clean_target` 仅用于 pipeline 在
clean 前执行运行期目标一致性复核；新增可选 `metrics` 仅用于结果可观测性，核心的
`success`、`execution_time`、`message` 契约、`Abort` 归属和统一耗时计量保持不变。doc 26
尚未实施前，不提前改写 doc 27 的已实施历史记录。

### 6.3 执行计划和结果模型

建议在 `pipeline_cli.py` 中定义轻量的内部模型：

```text
PipelineTask(section, operation, clean, generate_domains_options, expected_clean_target)
PipelineTaskResult(section, operation, status, elapsed, message, metrics, skip_reason)
```

其中 `generate_domains_options` 仅可包含 `description`、`md_context_mode`、
`md_limit`；其他 metadata 任务与所有 rag_load 任务均为空。`status` 只允许
`success`、`failed`、`skipped` 三种值；共享执行函数的 `success=True/False` 分别转换为
前两者，因失败即停或安全阻断而未调用共享执行函数的任务使用 `skipped`，并填写
`skip_reason`、令 `elapsed=None`。`skipped` 不等同于失败，也不单独改变退出码。
`expected_clean_target` 仅在 `clean=true` 时存在，保存 §6.4 定义的计划阶段已解析目标；它必须
在首个任务开始前完成解析和校验，不能在打印清理计划时临时拼接泛化描述。

执行顺序：

```text
按项目根目录解析 --config，并确认 pipeline.yaml 是存在且可读的普通文件（否则 exit 2）
  → 加载 pipeline.yaml
  → 不区分入口，校验整份 pipeline 文件的结构、metadata_config、两个章节、步骤键与步骤选项
  → 解析 metadata_config（允许绝对路径；相对路径基于项目根目录），并确认其为存在、可读的普通文件
  → 通过既有 ConfigLoader 加载 metadata_config.yaml
  → 根据入口构造执行计划：裸命令构造 metadata + rag_load；子命令仅构造对应章节
  → 校验入口相关计划：所选章节非空；裸命令检查 generate-dim-config 与 dim_value 不得同次执行
  → 仅为本次执行计划中的 clean: true 任务解析并校验实际清理目标；任一目标不明确则 exit 2
  → metadata 计划存在时输出主流程完整性告警
  → 汇总并输出包含真实目录/数据库/collection 的清理计划（仅存在 clean: true 任务时）
  → 组成单一有序任务列表：metadata 任务在前，rag_load 任务在后，各章节内部按固定顺序
  → 依次遍历完整任务列表：
      → 默认模式：任务 failed 后立即停止遍历，将全部剩余任务标记为“前序任务失败”
      → 继续模式：任务 failed 后记录结果并继续遍历
      → 每个 clean: true 任务在 clean 前按 §6.2 重新解析 current_clean_target 并与
        expected_clean_target 比较；不一致则该任务 failed，且不得清理任一目标
      → 继续模式准备执行 rag_load.sql 时：仅当本次 generate-sql-rag 已实际执行且 failed，
        才将 rag_load.sql 标记为“SQL-RAG 产物校验失败，为避免污染 Milvus”并跳过该任务
      → 除上述 SQL 特例外，继续模式不做依赖推断，其他任务均调用其共享执行函数
  → 输出三态汇总：全部已执行任务成功 exit 0；任一任务 failed 则 exit 1
```

所有配置与计划校验必须在第一个有副作用的任务开始前完成；任一校验失败直接 exit 2，
不进入任务循环，也不输出伪造的任务失败结果。SQL 安全阻断状态只能由继续模式的任务循环
根据本次 `generate-sql-rag` 的实际失败结果产生，不能在执行前预判，也不能应用于默认失败
即停模式或未实际执行的 `generate-sql-rag`。

### 6.4 输出与 clean 预警

pipeline 在执行第一个任务前，汇总一次本次执行计划中全部 `clean: true` 的任务并输出
清理计划；
预警必须显示通过主配置和既有回退链解析后的**真实清理目标**，不能只写“DDL 输出目录”、
“Milvus collection”或“Neo4j 数据库”等泛化名称。路径必须规范化为绝对路径；外部数据库
必须显示足以唯一识别目标的 endpoint、database 与 collection。任何密码、Token、Neo4j
用户名或 Milvus 用户名均不得输出；URI 若包含 userinfo 或敏感查询参数，必须脱敏后再显示。

各任务的目标解析来源如下：

| 任务 | 必须解析并显示的真实目标 |
| --- | --- |
| `metadata.ddl/json/md/rel/cql` | 按 `output.<step>_directory` 及既有 `output.output_dir/<step>` 回退链解析出的绝对目录，并注明“只删除顶层文件、保留子目录”。其中 `md` 使用 `output.markdown_directory`，`rel` 使用 `output.rel_directory`。 |
| `metadata.generate-domains` | 通过既有 `resolve_domains_yaml_path()` 得到的 `db_domains.yaml` 实际路径之父目录，并注明只删除该目录顶层文件。 |
| `metadata.generate-dim-config` | 通过既有 `resolve_dim_tables_yaml_path()` 得到的 `dim_tables.yaml` 实际路径之父目录，并注明只删除该目录顶层文件。 |
| `metadata.generate-sql-rag` | `sql_rag.generation.output_dir`（缺省 `output/sql-rag`）经项目根目录规则解析后的绝对目录，并注明只删除顶层文件。缺少该配置键时必须应用此运行时默认值，不能误判为目标无法解析。 |
| `rag_load.sql` | `vector_database.providers.milvus` 经 ConfigLoader 展开后的 `host:port/database`，以及 `loaders.sql_loader.collection_name`。**必须与 loader 实际传给 `MilvusClient` 的配置完全一致**：三个 loader 均传入 `providers.milvus` 子配置，根级 `vector_database.database` 无任何消费者、不得参与解析；host 须非空，port 须在 1..65535，`database` 缺省表示不切换（Milvus 默认库 `default`）。**loader 侧统一规范化（用户审核第六轮第 1、3 条）**：`MilvusClient`（host strip+lower、database strip）与 `CQLLoader`/`Neo4jConnectionManager`（database strip）使用与预警同一套规范化——否则 `database: " neo4j "` 等配置下预警 identity 与实际连接参数不同、运行期比较无法发现。**active 与 provider 约束（用户审核第八轮第 1 条）**：`rag_load` 三类 Milvus 目标解析与 SQL 加载器统一要求 `vector_database.active == "milvus"` 且 `providers.milvus` 明确存在且非空（对齐 table_schema 加载器的既有约束）——`active: pgvector` 或 provider 缺失/空配置此前会落到客户端默认连接（清理非预期 Collection 风险）。**database 统一规则**：只有**键缺失**才使用默认库（Neo4j `neo4j` / Milvus 不切换）；键存在时必须为非空字符串，`null`/数字/列表/布尔/空白一律在连接与写入前报配置错误（此前非 clean 加载会静默落到默认库、可能向默认库写入数据）。 |
| `rag_load.table_schema` | 同一 Milvus endpoint/database（同上，provider 子配置解析），以及 `loaders.table_schema_loader.collection_name`。 |
| `rag_load.dim_value` | 同一 Milvus endpoint/database（同上），以及 `loaders.dim_loader.collection_name`。 |
| `rag_load.cql` | `loaders.cql_loader.neo4j.uri` **经严格解析后**的值与 `database`，并注明“删除该数据库中的全部节点和关系”。解析规则（用户审核第 2 条）：协议白名单（bolt/neo4j 及其 +s/+ssc 变体）、host 非空、显式 port 须在 1..65535、拒绝格式错误；**uri 为必填项**（与 CQLLoader `neo4j_section["uri"]` 契约一致，第三轮第 4 条——resolver 不得虚构默认值，缺失/为空即配置错误，避免计划阶段"合法"、任务阶段才失败）；clean 计划与身份字段只使用规范形式（scheme://host[:port]，小写化）；**v1 边界（用户审核第四轮第 1 条）：URI 中的 userinfo、查询参数、path、fragment 一律拒绝**——userinfo 被 Neo4j 驱动直接拒绝（凭据只经 `user`/`password` 配置提供），neo4j:// 的 query 会被驱动解析为 routing context（`driver.py` `parse_routing_context`）具业务语义，剥离会使不同 routing context 产生相同 identity、破坏"预警目标=实际清理目标"保证；如需 routing 支持，须规范化 query 后纳入 identity；**校验错误消息不回显原始 URI**（原文可能含凭据）；连接日志同样只输出规范化脱敏 URI 或不输出（`cql_loader` 经 `normalize_neo4j_uri` 脱敏、`Neo4jConnectionManager` 不输出 uri）。**边界一致性（用户审核第五轮第 1 条）**：`CQLLoader._get_neo4j_config()`（构造期）强制调用 `normalize_neo4j_uri`——严格边界在 clean/non-clean、pipeline/独立 CLI、直接 Python 调用下判定一致，不再"仅 --clean 时拒绝"；连接失败的异常文本经 `redact_uris_in_text` 脱敏并抹除 password 值（驱动异常可能内嵌完整 URI 与凭据）。 |

**键缺失 vs 键存在但值非法（用户审核第三轮第 1 条 + 第四轮第 2 条）**：
配置节点与可选字符串值必须区分"键缺失"（使用缺省，合法）与"键存在但值
非法"（`null`/`[]`/`""`/`false`/`0` 等一律拒绝）——**`null` 亦不豁免**：
`ddl_directory: null` 曾回退并清理默认目录；`neo4j.database: null` 曾使
预警显示 `database=neo4j`、而 CQLLoader/连接管理器的 `.get(..., None)`
语义使驱动实际使用服务端默认库（预警目标≠实际目标）。Milvus host 只接受
非空字符串，不做 `str()` 强转（`[]`/`false`/`0`/字典曾被规范化成荒谬
endpoint）。**禁止使用 `xxx.get(key) or {}`
形式的假值回退**——那会把非法配置静默转成缺省（如 `output: []` 被当作未
配置，clean 目标解析到默认目录后可能被误清理）。实现由共享工具
`require_mapping()`/`get_optional_str()`（file_utils）承担，clean 解析链与
两个路径 resolver 统一复用。

**配置节点类型校验（用户审核第 3 条）**：clean 解析链上的每个配置节点
（`output`/`loaders`/`vector_database` 及其 `providers.milvus`/`loaders.cql_loader.neo4j`/
`sql_rag.generation` 等）在访问前显式校验为 mapping；类型错误抛带完整配置
路径的 `ValueError`（如 `vector_database 必须是对象（mapping），实际为 str`），
由计划阶段统一转为 exit 2。不得依赖 `.get()` 自然抛出的 AttributeError 逃逸
（那会以 exit 1 + traceback 结束，且可能掩盖真正的代码错误）。

清理目标解析必须复用实际清理路径使用的同一组 resolver / 配置回退函数，不得在 pipeline
中复制一套仅用于展示的配置键拼接逻辑。计划阶段的解析结果保存为
`expected_clean_target` 并用于清理预警；任务开始后，共享执行层按 §6.2 从该任务唯一的
配置快照重新解析 `current_clean_target`，在任何 clean 副作用前比较二者。两者一致后，
实际清理及业务执行继续使用同一配置快照；不得让 loader 再次从磁盘读取配置并形成第三套
目标。

目标不一致、任务阶段配置无法重新读取或当前目标无法解析时，不得清理期望目标或当前目标；
共享执行函数返回 `success=False`，pipeline 将该任务记为 `failed`。这是任务开始后的运行期
失败，统一 exit 1：默认模式停止后续任务，继续模式按 §5.2 调度。它不是 `skipped`，也不能
回退为 §4.4 的 exit 2。错误输出可同时显示期望和当前目标的脱敏身份，但不得泄露认证信息。

文件目录仍须执行既有的 clean 安全校验：目标必须是**项目根目录内的严格
子目录**——既不能越出项目根，**也不能是项目根目录本身**（``ddl_directory:
"."``、``output_dir: ""``/``"."`` 等解析后等于项目根的取值一律拒绝），
否则 clean 会删除根目录顶层文件（pyproject.toml、README、.env 等）。外部目标解析必须应用实际客户端
和 loader 使用的既有默认值（例如 Neo4j database 的 `neo4j` 默认值），不能因为原始 YAML
省略了有明确运行时默认值的键而误报错误。本次执行计划中的任一 `clean: true` 任务若在
应用这些默认值后仍无法解析出唯一目标，例如目录越界、collection 名为空，或
endpoint/database 最终仍不明确，均属于执行计划配置错误：必须在输出清理计划和执行任何
任务前以 exit 2 退出。解析清理目标只读取配置，不得为了预览而连接、创建或修改外部数据库。

本节的“任一 `clean: true` 任务”均仅指本次执行计划中的任务。未执行章节只做 §3.1 定义的
结构校验，不解析其目录、collection、URI 或 database。例如，`pipeline metadata` 不会因
未执行的 `rag_load.cql: {clean: true}` 缺少 Neo4j 配置而失败；切换为
`pipeline rag_load` 或裸 `pipeline` 后，该任务进入执行计划，才必须在任何副作用前完成
Neo4j 目标解析并在失败时 exit 2。

输出示例：

```text
⚠️ pipeline 将执行以下清理操作：

  - metadata.ddl
    目录：/opt/projects/metaweave/output/ddl
    范围：删除目录顶层全部文件，保留子目录

  - rag_load.sql
    Milvus：localhost:19530/nl2sql
    Collection：sql_example_embeddings
    范围：删除并重建该 Collection

  - rag_load.cql
    Neo4j：bolt://localhost:7687
    Database：neo4j
    范围：删除该数据库中的全部节点和关系
```

任务汇总还应保留可选 metrics；例如 `rag_load.dim_value` 返回成功但
`records_loaded=0` 时，若 `tables_skipped>0`，输出：

```text
⚠️ rag_load.dim_value 已完成，但未加载任何维度值：
   表总数 12，已处理 0，因 embedding_col 为空跳过 12，加载记录 0。
```

这只是成功任务的结果提示，不改变任务状态、整体退出码或后续任务调度。

执行期间保留每项任务既有的 banner、进度、结果和错误详情；pipeline 不捕获或重排
这些任务级输出。所有任务结束或提前停止后，pipeline 额外输出跨任务的三态汇总。
对于 rag_load，pipeline 通过 `emit_clean_warning=False` 抑制重复的通用 clean 警告，
由上述一次性清理计划承担安全提示职责。

跨任务汇总必须按照原执行计划列出每个任务，使用统一三态标识：`✅` 成功、`❌` 失败、
`⏭️` 未执行。默认失败即停后的剩余任务显示“前序任务失败，未执行”；
`generate-sql-rag` 失败触发的 SQL 加载保护显示“SQL-RAG 产物校验失败，为避免污染
Milvus，未执行”。在继续模式下，被该保护跳过的 `rag_load.sql` 之后的其他任务仍正常执行。

若 `clean: true` 的任务失败或未执行，汇总行必须附带可复制执行的**单任务补跑命令**；
`clean: false` 的任务不显示该提示。补跑命令使用 pipeline 已解析出的主配置路径，并保留
该任务的 `--clean`：

| pipeline 任务 | 补跑命令形式 |
| --- | --- |
| `metadata.ddl/md/json/rel/cql` | `metaweave metadata -c <metadata_config> --step <operation> --clean` |
| `metadata.generate-domains` | `metaweave metadata -c <metadata_config> --generate-domains --clean`，并追加本任务显式配置的 `--description`、`--md-context-mode`、`--md-limit` |
| `metadata.generate-dim-config` | `metaweave metadata -c <metadata_config> --generate-dim-config --clean` |
| `metadata.generate-sql-rag` | `metaweave metadata -c <metadata_config> --generate-sql-rag --clean` |
| `rag_load.<type>` | `metaweave rag_load -c <metadata_config> --type <type> --clean` |

路径和 `description` 等动态值必须按当前 shell 安全引用，避免空格或特殊字符使提示命令失真。
对本次执行计划中因失败即停或 SQL 安全阻断而未执行、且配置了 `clean: true` 的任务，仍
给出补跑命令：它虽然尚未清理目标，但目标保留旧数据，同样偏离了用户声明的“清理后重建”
意图。未被当前入口选入计划的另一章节任务不出现在汇总中，也不生成补跑提示。补跑提示只是
操作建议，不自动执行，也不改变三态结果或退出码。

## 7. 文档与测试更新

### 7.1 文档

以下活文档需要从“pipeline 已移除”改为本设计定义的当前行为：

- `README.md`
- `AGENTS.md`
- `CLAUDE.md`
- `docs/100_执行命令完整参考.md`
- `docs/迁移后运行指南.md`

迁移指南必须明确记录本设计对独立 CLI 的以下有意行为修正，不能只把它们写成 pipeline YAML
规则：

- **相对路径基准**：实施前部分 metadata 模块会把主配置中的相对产物路径解释为相对 cwd；
  实施后独立 metadata/rag_load CLI 与 pipeline 均统一相对项目根目录。从项目根目录运行的
  默认用法不变；依赖“从子目录运行并把产物写入该子目录”的脚本必须改用明确的绝对路径或
  调整配置；
- **`--md-limit` 边界**：独立 `metaweave metadata --generate-domains` 显式传入的
  `--md-limit` 必须为正整数。实施前 `0` 或负数会由 `DomainGenerator` 静默钳制为 `1`；
  实施后在任何 clean、文件写入或 LLM 调用前以 `click.UsageError`、exit 2 拒绝。依赖旧钳制
  行为的脚本和手工命令必须改为显式传入正整数；
- **空白 `--description`**：空字符串或纯空白值在共享执行入口规范化为 `None`，等同于未传。
  当前生成器原本已通过 `user_description.strip()` 避免把纯空白加入 LLM 提示词，因此正常
  生成语义不变；变化只影响观察原始参数值的扩展、Mock 或测试，这些调用方应改为预期
  `None`。非空描述仍去除首尾空白后使用。

实施时必须同时交付以下 pipeline 配置文件，不能只注册 CLI 而依赖用户自行创建默认文件：

- `configs/pipeline.yaml`：日常安全默认，只包含 metadata 主流程；不包含
  `generate-domains`、`generate-dim-config`、`rag_load.dim_value`、任何 rag_load 任务或
  `clean: true`；
- `configs/pipeline_init.yaml`：一次性 domain/dim 初始化模板，文件头必须醒目标注会覆盖
  `db_domains.yaml`、`dim_tables.yaml` 及人工 `embedding_col`；不得启用任何业务特定
  `description`，只能提供注释形式的待填写示例；
- `configs/pipeline_dim.yaml`：维度值两阶段模板，供 `pipeline metadata`、人工填写、
  `pipeline rag_load` 三段流程复用；它不是通用 rag_load 模板。

若后续提供通用 rag_load 模板，应使用独立文件（例如 `configs/pipeline_load.yaml`），
不得把加载外部数据库或 `clean: true` 放入日常默认 `configs/pipeline.yaml`。

doc 25 保留为旧版 pipeline 与 `standard` 移除的历史决策记录。其中 §1 关于“CLI
不再存在任何自动多步骤编排”的边界结论，由本文档修订；旧 `pipeline generate/load`、
`metadata --step standard` 及独立 `json_llm` 的移除结论仍然有效。旧版
`docs/update/6_...` 是历史编排说明，不作为当前 CLI 手册回改。

### 7.2 实施拆分

本设计必须分成两个可独立审查、测试和回滚的提交，不能把路径语义迁移夹在 pipeline 主体
实现中一次完成：

1. **路径统一提交**：引入共享项目路径 resolver，完成 §6.2 所列全仓路径审计和迁移，修复
   独立 metadata / rag_load CLI 的相对路径语义，并同步调整全部受影响测试；该提交不得新增
   pipeline 调度逻辑，也不要求在此阶段改造各生成器、发现管线和 loader 的配置注入接口；
2. **pipeline 实现提交**：在第一阶段测试全部通过后，再新增 pipeline CLI、配置解析、执行
   计划、clean 预警和结果汇总；pipeline 只能复用第一阶段已经稳定的路径 resolver。该提交
   必须同时完成 §6.2 的单任务配置快照注入改造，包括 metadata 三个下游类、三个
   `generate-*` operation 的共享函数、`_run_generate_sql_rag(main_config, ...)` 改造、三个会
   重读主配置的 loader、CQL loader 的快照派生配置、`LoaderFactory` /
   `_run_single_load()` 传参和快照派生副本；不能只新增 `expected_clean_target` 字段而保留
   任一普通步骤、generate 分支或 loader 的任务内二次读盘。

第一阶段必须单独证明从项目根目录和项目内子目录启动时得到相同的生成、输入与 clean 目标，
并覆盖 `output.output_dir/<step>` 回退路径；不得只把现有 cwd 断言机械改成新的项目根断言。
第一阶段的合入门槛是 §7.3 测试 42a，不能依赖尚不存在的 pipeline 入口。
第二阶段若发现新的路径字段遗漏，应先补回第一阶段的 resolver 覆盖与独立 CLI 回归测试，再
由 pipeline 复用，不能在 `pipeline_cli.py` 内增加专用路径修正。第二阶段完成前必须用读盘
次数和“比较后修改磁盘配置”的测试证明任务只有一个实际执行快照；否则 pipeline 提交不得
视为完成。第二阶段必须保持 42a 持续通过，并新增通过测试 42b；42b 不能替代或复制一套
pipeline 专用的期望路径。

### 7.3 测试

`tests/unit/test_cli_no_orchestration.py` 中“顶层拒绝 pipeline”的测试应删除或改写。新增测试至少覆盖：

1. `metaweave pipeline`、`metaweave pipeline metadata`、`metaweave pipeline rag_load` 的
   `--help` 均可见 `--config/-c` 和 `--continue-on-error`；group 帮助只列出
   `metadata`、`rag_load` 两个子命令，不出现旧 `generate` / `load`；实际调用
   `metaweave pipeline generate` 或 `metaweave pipeline load` 均以未知子命令、exit 2 退出，
   且不加载配置或执行任务；
2. 三个入口不传 `--config` 时均使用仓库实际交付的 `configs/pipeline.yaml`；该文件存在性与
   安全内容由测试 29 单独断言。传入 `-c` 时按项目根目录解析指定的 pipeline 配置，并按其
   `metadata_config` 加载主配置；
3. metadata 步骤键以任意 YAML 书写顺序出现时，均按标准内部顺序执行，且不输出顺序告警；
4. 合法子集可执行，缺少主流程步骤仅 warning；
5. `generate-domains` / `generate-dim-config` / `generate-sql-rag` 缺失不产生完整性 warning；
6. 未知步骤键、空计划、错误结构均在执行前以 exit 2 失败；其中 pipeline 配置文件根节点为
   `null` 必须失败，裸命令下单个章节缺失/为 `null`/为空对象且另一章节有合法步骤时必须正常执行；
   对应章节子命令在所选章节没有任务时必须失败；步骤值为 `null` 或 `{}` 时必须执行该步骤并使用默认选项；
7. 默认模式下首个返回 `success=False` 的任务立即终止统一任务循环；失败任务之后的全部
   metadata 与 rag_load 计划任务都不调用共享执行函数，而以 `skipped` / `⏭️` 汇总并注明
   “前序任务失败”，最终 exit 1。至少覆盖裸 pipeline 的 `metadata.ddl` 失败，验证不会进入
   后续 metadata 或 rag_load；
8. `--continue-on-error` 下普通任务失败后继续调用后续任务，最终 exit 1；除测试 40 的 SQL
   安全阻断外，编排器不得根据推测的产物依赖主动跳过其他任务，前置不足由任务自身失败；
9. metadata 与 rag_load 任务均将各自的 `clean` 值传入共享执行函数；
10. 裸 `pipeline` 始终先完成 metadata 计划，再执行 rag_load 计划；`pipeline metadata` /
    `pipeline rag_load` 仅执行对应章节，绝不执行另一章节任务。
11. `generate-domains` 配置的 `description`、`md_context_mode`、`md_limit`
    能通过任务请求传入共享执行函数；省略时使用本文定义的默认语义；
12. `md_context_dir`、`domains_config`、`dim_config_path` 及其他不受支持的路径
    覆盖键均在执行前报错；无路径覆盖的 pipeline 任务使用主配置既有的路径解析结果。
13. 多个 `clean: true` 任务只输出一次 pipeline 清理计划；pipeline 调用 rag_load
    helper 时抑制重复通用 clean 警告，但保留各任务的结果与错误输出。
14. 参数化覆盖步骤选项对象的不支持键、三个 generate-domains 字段写入非该步骤、非法
    `md_context_mode`、以及 `md_limit` 的字符串/浮点/布尔值、`0`、负数和非字符串
    `description`；`md_context_mode` / `md_limit` 显式 `null` 时验证默认回退行为；
    `description` 省略、显式 `null`、空字符串或仅含空白时均规范化为 `None`，非空值去除
    首尾空白后传递。
15. `metadata` / `rag_load` 章节或已知步骤键为字符串、列表、数字或布尔值时，均在
    执行前以配置错误退出。
16. 根级 `metadata_config` / `metadata` / `rag_load`、`metadata.ddl`、`rag_load.cql` 及
    `metadata.generate-domains.md_limit` 等重复 YAML 键均在任何任务开始前失败，且错误
    信息包含对应完整配置路径和原始键文本；引号形式不同但字符串值相同的键也视为重复。
17. `DDL`、`CQL`、`SQL`、`Generate-Domains` 等大小写步骤键变体均作为未知步骤键，
    在执行前失败。
18. `clean: true` / `clean: false` 通过；`clean:`、`clean: null`、`clean: 1`、
    `clean: yes`、`clean: on`、`clean: True`、`clean: FALSE` 以及 `clean: "true"` /
    `clean: "false"` 均在执行前失败；同时验证步骤值 `md:` 仍合法，不能与选项空值混淆。
19. `metadata_config` 省略时使用 `configs/metadata_config.yaml`；绝对路径和相对项目根目录的
    路径均可使用；配置为非字符串、空字符串、不存在路径、目录或不可读文件时，均在任何
    任务开始前以 exit 2 失败。
20. 根级的 `ragload`、`metdata` 等未知键均在任何任务开始前失败，不能被静默忽略。
21. `--config` 指向不存在、非普通文件或不可读的 pipeline 配置文件时，以 CLI 参数错误
    退出（exit 2），且不会尝试加载主配置或执行任务。
22. 维度值两阶段配置拆成三个可自动验证的断言：裸 `pipeline` 同时选择
    `metadata.generate-dim-config` 与 `rag_load.dim_value` 时，在执行任何任务前以配置错误
    退出，两个共享执行函数都不得调用；`pipeline metadata` 只调用一次
    `generate-dim-config`，不得调用 dim loader；`pipeline rag_load` 只调用一次
    `dim_value` loader，不得调用配置生成器。人工填写 `embedding_col` 只保留为操作文档中的
    阶段边界，不作为 CI 自动化测试步骤。
23. 每个 YAML 步骤均独立调用一次共享执行函数，不会合并为一次 metadata Click 命令调用；
    metadata 任务的运行期失败返回 `success`、`execution_time`、`message`，并由 pipeline
    按失败策略调度后续任务，不传播 `click.Abort`。
24. 多个 metadata 任务分别按 §6.2 固定映射设置当前日志步骤并独立加载主配置；pipeline
    执行 `generate-domains` 时不读取 pipeline Click context，MD 路径按主配置
    `output.markdown_directory` 的既有回退链解析。
25. `generate-sql-rag` 在 `enable_sql_repair=true` 且修复失败条目被删除时仍作为成功任务；
    `enable_sql_repair=false` 或该键缺失且存在无效 SQL 时作为失败任务。发行配置显式为
    `true`，自定义配置缺键则验证代码缺省为 `false`，pipeline 不覆盖该差异。
    `rag_load.dim_value` 全部表因空 `embedding_col` 被跳过、`records_loaded=0` 时仍作为成功
    任务；两类成功结果均不触发失败处理或 `--continue-on-error` 逻辑。
26. `--continue-on-error` 测试使用返回 `success=False` 的真实任务失败；不得将
    `rag_load.dim_value` 的零加载或 `generate-sql-rag` 已启用修复后的 warning 视为失败。
27. metadata 主流程缺项才产生完整性 warning；rag_load 任意子集及三个 `generate-*`
    步骤缺失均不产生该 warning。`relationships.domain` 为非空且既有 `db_domains.yaml`
    不存在时，所选 metadata `--step` 任务按其既有前置校验失败；该文件已存在时，不要求
    本次 pipeline 额外选择 `generate-domains`。
28. `rag_load.dim_value` 返回成功但 `records_loaded=0`、`tables_skipped>0` 时，
    `_run_single_load()` 向 pipeline 透传 `tables_total`、`tables_processed`、
    `tables_skipped`、`records_loaded` metrics，pipeline 在最终汇总输出零加载 warning，
    但保持成功状态、退出码和后续调度不变。
29. 仓库必须交付可被默认 `--config` 直接读取的 `configs/pipeline.yaml`，且其中不含
    `generate-domains`、`generate-dim-config`、任何 rag_load 任务或 `clean: true`；裸命令
    使用该文件时只执行 metadata 主流程，不修改人工 domain/dim 配置或外部加载目标。
30. `configs/pipeline_init.yaml` 与 `configs/pipeline_dim.yaml` 均能通过整文件结构校验；前者
    醒目标注覆盖风险，且不得包含已启用的业务特定 `description`，直接运行时共享请求中的
    `description` 必须为 `None`；后者的两个章节子命令分别构造单一章节计划，裸命令运行后者
    则因 `generate-dim-config` / `dim_value` 同次冲突而在任务开始前失败。该测试使用共享执行
    函数 spy 验证调用边界，不模拟人工填写过程。
31. `pipeline metadata -c custom.yaml` 与 `pipeline rag_load -c custom.yaml` 只读取指定文件；
    即使默认 `configs/pipeline.yaml` 不存在，group 回调也不得提前失败或执行任务。
    `pipeline -c custom.yaml metadata`、`pipeline --continue-on-error rag_load` 及父子两层同时
    传参均以参数错误（exit 2）退出，不能静默丢弃任一层参数；每次调用的执行计划只能构造
    和执行一次。有子命令时还须用 spy 断言 group 的裸 pipeline 执行入口调用次数为 0、
    对应子命令执行入口调用次数为 1，防止 metadata 或 rag_load 被父子回调重复执行。
32. 三个入口都校验整份 pipeline YAML：运行 `pipeline rag_load` 时，未执行的 metadata
    章节若包含重复键、`DDL` 等未知/大小写错误步骤、非法 `clean`、错误步骤值或未知选项，
    仍须在任何任务开始前失败；反向运行 `pipeline metadata` 时对非法 rag_load 章节同理。
    所选章节为空与 dim 同次冲突则分别按入口和实际执行计划判断。
33. 参数化验证退出码矩阵：全部任务成功及仅有 warning 时 exit 0；任一任务
    `success=False` 时，无论默认停止还是 `--continue-on-error` 最终均 exit 1；§4.4 的每类
    参数、YAML、结构与计划错误均 exit 2，且不会启动任务，继续开关不能改变 exit 2。
34. 参数化验证八种 metadata operation 的日志映射，独立 metadata CLI 与 pipeline 必须
    得到相同结果；`generate-domains` / `generate-dim-config` 不得再设置空 step，
    `generate-sql-rag` 在启用校验时依次设置 `sql-rag-generate`、`sql-rag-validate`，跳过
    校验时只设置前者。
35. 未传根级 `--debug` 时，裸 pipeline 与 `pipeline rag_load` 调用
    `_run_single_load()` 均传 `debug=False`；使用 `metaweave --debug pipeline ...` 时均传
    `debug=True`。模拟 helper 内部异常，debug 模式必须输出 traceback，普通模式不得输出；
    pipeline 子命令不新增或接受自身的 `--debug`。metadata 任务仍能继承根级日志级别，但
    v1 的执行请求不新增 `debug` 字段，pipeline 不为 metadata 结构化失败合成终端 traceback；
    本测试不得把 rag_load 的 traceback 保证泛化到 metadata。
36. 根 mapping、两个步骤章节和步骤选项对象中的键必须是原始字符串 scalar；参数化覆盖
    未加引号的 `on:`、`yes:`、`true:`、数字键、`null` 键及复合键，均在 `safe_load()` 前
    exit 2，并用原始文本和完整路径报错，不能出现 `未知步骤键: True` 等胁迫后的错误信息。
37. pipeline 配置文件中的 `${ENV}` 不进行替换并按普通字符串处理；例如
    `metadata_config: ${CONFIG_PATH}` 不读取同名环境变量，而是按项目根目录解析该字面路径，
    文件不存在时在任何任务开始前 exit 2。被引用主配置仍保留既有 ConfigLoader 的环境变量
    处理行为。
38. 最终汇总按原计划完整输出 `success` / `failed` / `skipped` 三种状态；默认失败即停时，
    后续任务没有调用共享执行函数、耗时为空且不计为新增失败。继续模式下普通失败不会把
    无关后续任务标为跳过。
39. `clean: true` 的失败任务和未执行任务均显示正确的单任务补跑命令，包含解析后的
    `metadata_config`、对应 `--step` / `--generate-*` / `--type` 与 `--clean`；
    `generate-domains` 还保留显式配置的三个专用选项并正确引用动态值。`clean: false` 时不
    显示补跑提示，提示命令也绝不自动执行。
40. 裸 pipeline 同时选择 `metadata.generate-sql-rag` 与 `rag_load.sql` 时，模拟前者因
    `enable_sql_repair=false` 且存在无效 SQL 返回失败：默认模式的剩余任务均因失败即停而
    跳过，`rag_load.sql` 的原因也必须是“前序任务失败”，不得使用 SQL 专用阻断原因；继续
    模式仅将 `rag_load.sql` 以“避免污染 Milvus”为由安全阻断，其他后续任务仍执行。若本次
    未选择 `generate-sql-rag`、该任务没有实际执行，或该任务成功但仅有修复 warning，则
    不得阻断 `rag_load.sql`。
41. 参数化覆盖进入本次执行计划的所有 `clean: true` 任务的实际目标解析：metadata 文件
    任务显示经项目根目录规则规范化的绝对目录；三类 Milvus 任务显示 ConfigLoader 展开并应用运行时默认值后的
    host、port、database 与各自 collection；CQL 加载显示脱敏 URI 和 Neo4j database。预警
    目标必须与执行清理使用的目标一致，且解析过程不连接或修改外部数据库。目录越界、空
    collection 或应用默认值后仍无法唯一确定外部目标时，在任何任务开始前 exit 2；输出中
    不得出现密码、Token、用户名或 URI userinfo 等敏感信息。
42. 路径测试按实施提交拆成两个独立门槛：
    - **42a（路径统一提交）**：从项目根目录、项目内任意子目录分别运行独立 metadata /
      rag_load CLI；同一主配置中的相对路径必须始终解析到项目根目录下同一绝对目标。参数化
      覆盖 ddl/json/md/rel/cql、三个 generate 模式及其输入目录、rag_load 的 `input_file` /
      `config_file` / `metadata_config_file`，验证生成、读取和实际 clean 目标一致；同时分别
      覆盖显式 `*_directory` 与仅配置 `output.output_dir` 的 `<output_dir>/<step>` 回退路径，
      以及 `sql_rag.generation.output_dir` 缺失时的 `output/sql-rag` 默认值。特别验证关系发现
      的 JSON 读取与 rel 写出不再分属项目根和 cwd，CQL 不再出现生成目录基于项目根、clean
      目录基于 cwd 的分叉，SQL loader 的显式相对 `input_file` 也不再基于 cwd。绝对路径保持
      不变，但 clean 目标越出项目根时仍须拒绝。该组测试不得导入、注册或调用 pipeline 入口。
    - **42b（pipeline 实现提交）**：复用 42a 的同一组主配置 fixture 和绝对路径期望值，分别
      运行裸 `pipeline`、`pipeline metadata`、`pipeline rag_load`；断言三个入口都调用第一
      阶段的共享 resolver，生成、读取、clean 计划、运行期目标复核与实际 clean 目标均与
      42a 一致。`pipeline_cli.py` 不得维护第二套路径拼接、默认值或测试期望；可通过 resolver
      spy 断言 pipeline 只委托共享 resolver。第二阶段必须同时保持 42a 通过，不能用 42b
      替换独立 CLI 的路径回归测试。
43. `enable_sql_repair` 使用三组配置验证：发行配置显式 `true` 时启用修复；自定义配置显式
    `false` 时不修复；自定义配置省略该键时也按代码缺省 `false` 处理。后两种情况下若存在
    无效 SQL，`generate-sql-rag` 返回失败；默认模式按失败即停处理全部剩余任务，继续模式才
    触发同次计划中 `rag_load.sql` 的安全阻断。
44. 使用同一份结构合法的 pipeline YAML，在未执行章节配置 `clean: true`，并让其外部目标
    无法解析：`pipeline metadata` 不因未执行的 `rag_load.cql` 缺少 Neo4j 目标而失败，
    `pipeline rag_load` 或裸 `pipeline` 则在任务开始前 exit 2；反向场景同理。对照验证未执行
    章节中的 `clean: yes`、`clean: null`、未知选项或重复键仍因整文件结构校验 exit 2。
45. 对 metadata 与 rag_load 的 `clean: true` 任务分别模拟“清理计划输出后、任务开始前主配置
    被修改”：共享执行层收到计划保存的 `expected_clean_target`，从任务唯一配置快照解析
    `current_clean_target` 并在 clean 前比较。目标相同则只使用该快照完成清理和业务执行，且
    不再读取配置；目标不同、配置重载失败或当前目标无法解析时，不调用任何文件删除、Milvus
    或 Neo4j 清理方法，任务返回 `success=False` / `failed`、整体 exit 1。默认模式停止后续
    任务，继续模式继续调度；两种模式都不得把它归为 `skipped` 或 exit 2。独立 CLI 传
    `expected_clean_target=None` 时只解析一次当前目标，不执行期望值比较。对外部目标额外
    参数化验证 port 的 YAML 整数、字符串、环境变量字符串和前导零表示，以及 host 的大小写/
    首尾空白差异均规范化为相同身份，不得误判为目标变化；database 或 collection 的大小写
    真正变化仍必须判定为不同目标。
46. 对独立 `metaweave metadata --generate-domains` 与 pipeline 的 `generate-domains` 任务
    参数化验证同一套共享规范化：`description` 省略、空字符串或纯空白均以 `None` 进入
    `MetadataExecutionRequest`，非空值均去除首尾空白；显式 `md_limit=1` 正常传递，`0` 和
    负数均在任何 clean、文件写入或 LLM 调用前 exit 2，不能到达 `DomainGenerator` 后被静默
    钳制为 `1`。pipeline YAML 的非法类型仍由计划校验拒绝；独立 CLI 的 Click 类型错误仍按
    标准参数错误处理。另验证省略 `md_context_mode` 或 pipeline YAML 显式 `null` 时，均经
    共享函数规范化为 `name_comment`；CLI 的 `--md-context-mode FULL` 被 Click 接受后以规范
    值 `full` 进入共享请求，而 pipeline YAML 或直接 Python 调用传入 `FULL` 时仍被拒绝，
    共享规范化函数不得自行执行大小写转换。
47. 使用日常默认 `configs/pipeline.yaml` 执行 `metaweave pipeline rag_load`，断言在调用任何
    loader 前 exit 2；错误信息必须包含实际配置路径、`rag_load` 章节、默认文件不包含加载
    步骤，以及 `pipeline rag_load -c <配置文件>` 的操作提示，并可补充维度值专用的
    `configs/pipeline_dim.yaml`。使用其他自定义配置的空 `metadata` / `rag_load` 章节时，错误
    信息至少包含实际配置路径和所选章节名；不得把 `pipeline_dim.yaml` 描述为通用加载模板，
    `--continue-on-error` 也不得改变退出码。
48. 对 metadata 全部八个 operation（ddl、md、json、generate-domains、generate-dim-config、
    rel、cql、generate-sql-rag）以及 rag_load 的 sql、table_schema、dim_value、cql 分别注入
    `ConfigLoader` / `load_config` spy：计划阶段读盘与任务阶段读盘分开计数，每个已开始任务的
    共享执行入口只允许加载主配置一次；`MetadataGenerator`、`CQLGenerator`、
    `RelationshipDiscoveryPipeline`、三个 generate 共享实现和三个当前会重读主配置的 loader
    收到快照后，不得再次调用任何配置读盘方法；`CQLLoader` 也不得另行读盘。
    `generate-sql-rag` 必须单独断言 `_run_generate_sql_rag()` 收到注入的 `main_config`，不会
    使用 `config_path` 重读；LLM 预检、domain/MD/rel 路径、clean、生成、校验/修复和失败结果
    均使用该快照。`generate-domains` 与 `generate-dim-config` 也必须断言其共享分支不再
    `load_config()`。在 `expected_clean_target` 比较通过后、下游组件构造前修改磁盘配置，
    验证 clean、输入输出路径、Milvus/Neo4j 目标、database、embedding、LLM 与 SQL-RAG
    安全阻断仍使用比较时的快照。另断言 `_run_single_load()` 推导 SQL `input_file`、补充
    loader 运行时字段时只修改派生副本，不改变原始完整快照；独立 metadata/rag_load CLI
    通过共享执行层时也满足任务内只加载一次。
49. 参数化验证 `MetadataExecutionRequest` 的三个路径覆盖字段：独立 metadata CLI 显式传入
    `--domains-config`、`--md-context-dir`、`--dim-config` 时，只在各自允许的 operation 中写入
    对应 `*_override`；未显式传入时全部为 `None`。特别断言 Click 自动提供的
    `--md-context-dir=output/md` 默认值不会被当成命令行覆盖，主配置
    `output.markdown_directory` 仍可生效。裸 pipeline、`pipeline metadata` 构造的所有 metadata
    请求始终将三个字段设为 `None`，pipeline YAML 中出现这些路径键仍按测试 12 拒绝。共享
    generate 函数和普通步骤的 DomainResolver 路径解析在没有 Click context 时，仅依靠请求
    Optional 字段、配置快照和共享 resolver 正常工作；不得调用 `click.get_current_context()`。

## 8. 验收标准

1. 裸 `metaweave pipeline` 能按固定章节顺序执行指定子集；`metaweave pipeline metadata` /
   `metaweave pipeline rag_load` 能仅执行对应章节，并均按 `metadata_config` 加载主配置；
2. metadata 步骤键不论 YAML 书写顺序，均按标准内部顺序稳定执行；
3. 主流程缺步骤只告警，不阻止子集执行；
4. 未知步骤键、无效步骤选项和错误结构在执行前明确报错并 exit 2；
5. 默认遇到任务失败即终止完整任务列表，另一章节也不得继续，剩余任务统一标记“前序任务
   失败”，最终 exit 1；`--continue-on-error` 会继续且最终仍 exit 1，除 SQL-RAG 安全阻断外
   不进行通用依赖跳过；
6. `clean` 未配置时保持各单命令默认行为，配置后仅影响对应任务；
7. 单独运行 metadata/rag_load 与经三个 pipeline 入口执行使用同一业务执行函数；
8. 活文档不再将 `pipeline` 说明为“已移除”，且不再展示旧 `pipeline generate/load` 用法；
   `pipeline --help` 只列出 `metadata`、`rag_load`，旧 `generate` / `load` 调用以 exit 2 拒绝。
9. 仓库实际提供安全的 `configs/pipeline.yaml` 默认文件，以及明确标注覆盖风险的
   `configs/pipeline_init.yaml`、`configs/pipeline_dim.yaml` 模板；初始化模板不启用任何
   业务特定 `description`，默认 rag_load 空章节错误会说明默认文件不加载外部数据库并给出
   自定义配置提示。
10. pipeline 选项位于最终执行命令之后；有子命令时父级显式选项明确报错，父级默认值不会
    触发配置检查或执行，且 `pipeline --help` 明确说明裸命令会执行两个章节。
11. 三个入口对同一份 pipeline YAML 的结构合法性结论一致；所选章节非空、dim 同次冲突、
    `clean: true` 的实际目标解析、完整性告警与执行任务集合等计划规则随入口变化。未执行
    章节的 clean 字面量和结构仍校验，但其外部目标延迟到对应章节真正进入计划时解析。
12. 退出码严格区分：成功为 0，任务运行失败为 1，Click 参数、pipeline 配置与计划错误为 2。
13. metadata 日志严格按 §6.2 映射分流；共享执行层同时修复独立 CLI 的 domain/dim 空 step，
    SQL-RAG 在同一任务内按生成与校验阶段正确切换。
14. 根命令 `--debug` 通过 Click 根 context 传入所有 pipeline rag_load 任务，确保异常时的
    traceback 行为与直接调试 rag_load 一致，pipeline 自身不新增同名选项；v1 对 metadata
    仅保留全局日志级别影响，不承诺终端 traceback，也不由 pipeline 合成堆栈输出。
15. 原始 YAML Node 预检覆盖根、章节和选项三层 mapping；重复键及非字符串键在普通加载前
    exit 2，错误信息保留原始键文本。
16. `clean` 的 YAML 字面量按 §3.1 严格校验；`description` 与显式 `md_limit` 由 pipeline
    和独立 metadata CLI 复用同一规范化函数，空白描述统一为 `None`、非正 limit 均在副作用
    前 exit 2；`md_context_mode=None` 统一为 `name_comment`，共享函数只接受规范小写值且不做
    大小写转换。pipeline 配置本身不替换环境变量，`--config` 与 `metadata_config` 最终都
    必须指向存在、可读的普通文件。
17. 汇总完整呈现成功、失败、未执行三态；配置 `clean: true` 的失败或未执行任务提供准确
    的单步补跑命令；同次运行的 `generate-sql-rag` 失败时，即使继续模式也不会让
    `rag_load.sql` 加载未通过校验的产物，但其他无关任务仍可继续。
18. 对本次执行计划中的 `clean: true` 任务，在执行任何任务前输出包含绝对目录或外部数据库
    endpoint、database、collection 的 clean 计划，并与实际清理目标一致；当前计划目标不明确
    时 exit 2，未执行章节不做目标解析；预览过程不连接外部服务、不泄露任何认证信息。
19. 主配置中所有文件/目录路径字段及其运行时回退路径，在独立 metadata/rag_load CLI 与
    pipeline 中均通过共享 resolver 基于项目根目录解析；从子目录运行不会改变生成、依赖
    输入、loader 输入、clean 预警或实际清理目标。路径统一及受影响测试作为独立提交先行
    通过测试 42a，再实施 pipeline 并通过 42b；第二阶段仍须保持 42a 通过。该独立 CLI 行为
    修正已写入迁移说明。
20. SQL 修复语义区分发行配置与代码缺省：发行文件显式为 `true`，自定义主配置缺键时按
    `false` 执行；pipeline 不另行覆盖该值。
21. clean 预警目标通过 `expected_clean_target` 接入 metadata 与 rag_load 共享执行层；任务
    使用唯一配置快照重新解析并在 clean 前比较，目标变化时不清理任何目标、任务 failed、
    exit 1，并按默认停止或继续模式调度后续任务。比较前统一规范化 host、port、database、
    collection 和文件绝对路径，原始 YAML 类型或等价文本表示不得造成误判。
22. metadata 全部八个 operation 均使用任务的唯一配置快照：三个下游类支持配置注入，三个
    generate 共享实现不得重读，`_run_generate_sql_rag()` 接收 `main_config`；三个当前会重读
    主配置的 rag_load loader 及其工厂支持注入完整主配置，CQL loader 使用从该快照派生的
    配置。共享执行层每个任务只读盘一次，下游不得形成第三套配置。clean 复核后的全部副作用
    使用同一快照或不污染原快照的派生副本，磁盘配置在任务期间变化不会改变本任务行为。
23. 迁移指南同时记录相对路径基准、显式非正 `--md-limit` 从静默钳制改为 exit 2，以及空白
    `--description` 提前规范化为 `None`；不得误写成纯空白描述在旧实现中会进入 LLM 提示词。
24. `MetadataExecutionRequest` 提供三个 Optional 路径覆盖字段；独立 metadata CLI 仅将
    `COMMANDLINE` 来源映射为覆盖值，pipeline 始终传 `None`。共享执行层不读取 Click context，
    尤其不能把 `--md-context-dir` 的 Click 默认值误当成用户覆盖。
