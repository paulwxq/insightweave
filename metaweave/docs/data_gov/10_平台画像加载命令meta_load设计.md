# 平台画像加载命令 `meta_load` 设计

> 状态：设计稿，尚未实施。本文承接 [doc 9](9_db采集作业目录与数据库画像表设计.md) 的作业目录和 `db_profile` 表。本次只实现 `--type db`；`ddl`、`json` 是后续扩展方向，不得在本阶段伪装为已支持类型。

## 1. 目标与边界

新增独立命令 `metaweave meta_load`，将采集产物加载到 MetaWeave 平台 PostgreSQL，而不是 Milvus 或 Neo4j。它与 `metadata` 的“生成文件”、`rag_load` 的“加载 RAG 数据”分别承担不同职责。本阶段只读取指定工作区中的数据库画像，并向 `public.db_profile` 插入一行：

```text
output/<source_id>/<job_id>/db/database.json  →  public.db_profile
```

实际文件名是现有 `--step db` 生成的 **`database.json`**，不是 `db.json`；不查找旧路径 `output/<source_id>/db/`，也不扫描其他 `job_id`。`source_id` 和 `job_id` 都按 UUID 规范化为小写、带连字符的目录名。

本次只做画像入库，不实现 Web API/页面、`publish_info`、当前版本切换、表/字段画像表、`ddl/json` 加载，也不改造 `metadata`、`rag_load` 或 pipeline。`db_profile.publish_id` 是每次成功加载生成的版本 ID；同一 `job_id` 可再次加载并产生另一行，不覆盖旧行。**入库不等于“设为页面当前版本”**：在后续 `publish_info` 与当前版本选择机制实施前，页面不得靠 `collected_at` 最大值或本次命令的最后一行自行推断当前版本。此处对 doc 9 中“发布时写入 `db_profile`”的阶段安排作细化：`meta_load --type db` 先完成画像入库，当前版本激活留待后续设计。

实施前须由管理员按 doc 9 §2–§3 建好 `public.job_info` 和 `public.db_profile`；命令不自动建表或改表，也不连接被采集的源库。

## 2. CLI 契约

```bash
metaweave meta_load --type db \
  --source-id 9b06f9f1-bbba-468d-8c09-fa5c6f737a96 \
  --job-id 550e8400-e29b-41d4-a716-446655440000

metaweave meta_load --type db \
  --source-id 9b06f9f1-bbba-468d-8c09-fa5c6f737a96 \
  --job-id 550e8400-e29b-41d4-a716-446655440000 --clean
```

| 参数 | 本阶段规则 |
| --- | --- |
| `--type` | 必填且仅允许一次；唯一合法值为小写 `db`。`ddl`、`json`、`all` 暂不支持，直接报用法错误，不静默跳过。 |
| `--source-id` | 必填且仅允许一次；指定已登记数据源的 UUID。CLI 选项使用连字符，不另设 `--source_id` 别名。 |
| `--job-id` | 必填且仅允许一次；指定已有工作区 UUID，不能省略后自动选“最近一次”或新建作业。 |
| `--clean` | 可选，默认 `false`；**画像成功提交平台库后**，清理本次 `db/` 目录顶层文件。详见 §4。 |

根命令现有 `--debug` 继续有效。本命令不增加 `--config`、`--continue-on-error` 或 `--type all`；平台库连接参数沿用项目根 `.env` 的 `PLATFORM_DB_*`，入口显式执行已有 `load_project_env()` 后再读取平台设置。`SOURCE_CREDENTIAL_KEY` 与源库密码不是本命令的必需条件。

退出码：参数缺失、重复或非法、数据源或工作区不存在、工作区归属不符等执行前提错误为 `2`；产物缺失或非法、步骤未成功、平台库读写失败、清理失败等任务失败为 `1`；入库及所请求的清理都完成才为 `0`。失败消息不得包含平台库密码或完整连接串。

## 3. `db` 加载流程与字段映射

按以下顺序执行，不修改 `job_info` 的步骤状态：

1. 规范化两个 UUID，加载项目根 `.env` 和平台库连接设置；用 doc 9 的 `resolve_db_output_dir(source_id, job_id)` 解析唯一输入目录，不允许从调用时工作目录拼路径。共享解析器须拒绝 `output/`、`<source_id>/`、`<job_id>/`、`db/` 任一已存在层级的符号链接，并核对真实路径与规范 UUID 组成的预期路径完全一致；仅确认“仍在项目根内”不够。
2. 查询 `db_source_info`，确认数据源存在；查询 `job_info` 中该 `job_id` 的步骤行，确认全部归属本次 `source_id`，且 `(job_id, 'db')` 的 `last_status='success'`。找不到步骤、归属不符或状态不是成功时停止，**不读取其他作业的文件，不清理目录**。
3. 要求 `<db目录>/database.json` 存在且为普通文件；缺失、目录冒充文件或文件是符号链接均失败。本阶段不从源库重新采集，也不以 `job_info` 成功状态代替文件存在性检查。
4. 读取并校验 JSON：顶层为对象、`schema_version` 严格等于整数 `1`、文件中的 `source_id` 与 CLI 指定值一致；`collected_at` 是带时区的时间；`database`、`selected_schemas`、`schemas`、`totals` 符合 doc 9 §3 的结构；必要字段缺失或类型错误、计数为负、端口越界均失败。`server_version` 可缺失或为 `null`，对应 SQL `NULL`；doc 13 增补的 `server_version_full`、`os_platform` 同样可缺失或为 `null`，但出现即须为非空字符串。不得用当前数据源登记值补写历史画像字段。
5. 生成新的 `publish_id = uuid.uuid4()`，按下表映射数据，在一次平台库事务中插入一条 `db_profile` 并提交。任何校验或插入失败都不应留下半行画像；主键冲突直接失败，不自动重试生成 UUID。
6. 若传了 `--clean`，只在上述事务**提交成功之后**清理本次目录顶层文件；否则保留所有输入文件。输出成功消息包含 `source_id`、`job_id`、`publish_id`、加载的文件路径和是否完成清理。

| `database.json` 来源 | `db_profile` 字段 |
| --- | --- |
| 本次生成的 UUID、CLI 参数 | `publish_id`、`job_id` |
| `schema_version`、`source_id`、`collected_at` | 同名字段 |
| `database.type/host/port/name/server_version` | `db_type/host/port/db_name/server_version` |
| `database.server_version_full`、`database.os_platform` | `server_version_full`、`os_platform`（doc 13 增补：可缺失或为 `null`，对应 SQL `NULL`；一旦出现必须是非空字符串，类型错误或空串即失败；不校验取值） |
| `database.size_bytes/size_scope/record_count` | 同名字段；JSON `null` 对应 SQL `NULL` |
| `selected_schemas`、`schemas` | 同名字段；`schemas` 保留 JSON 数组写入 JSONB，不改写计数口径 |
| `totals.ordinary_table_count/partitioned_table_count/partition_child_count/view_count/materialized_view_count/object_count` | 同名六列 |

不把 `database.json` 整体另存一列，也不加载用户名、密码、密文或连接级 TLS 选项。`db_profile` 保留文件采集时的 host、port、库名等画像值；数据源登记记录日后修改，不回写历史画像。`job_info` 的 `last_status` 仅表示采集步骤状态，不因为加载成功或失败而改成另一种状态。

## 4. `--clean` 的精确语义

`meta_load` 消费的文件就在用户指定要清理的目录中，因此**不能照搬 `metadata --clean` 的“执行前清理”时序**。本命令的 `--clean` 是“成功入库后的输入文件清理”，顺序固定为：验证并读入 `database.json` → 提交 `db_profile` → 调用现有 `clean_top_level_files(db_dir)`。如果文件不存在，绝不因为带了 `--clean` 就创建空目录或清理其他位置。

清理范围严格限定 `output/<source_id>/<job_id>/db/` 的顶层文件，包含 `database.json` 和该层其他普通/隐藏文件；不递归、不删除子目录或其中的文件，也不触碰同一数据源下其他 `job_id` 或其他步骤目录。清理前再次调用共享的 `resolve_db_output_dir()`，拒绝路径层级中的符号链接及真实路径错位；不得由 loader 自己维护第二套路径规则。清理前须在日志中输出一行，明确目标目录及“删除顶层所有文件、保留子目录及其内容”的范围；不做交互确认，命令在脚本或 CI 中可直接运行。`--clean` 成功后再次对同一工作区执行 `meta_load --type db` 会因缺少 `database.json` 而失败，除非重新执行 `--step db` 生成该文件。

若平台库提交失败，**不执行清理**。若提交成功但清理失败，`db_profile` 已持久化，命令返回 exit `1` 并明确提示“画像已入库、清理失败”，打印 `publish_id` 与目标目录；不谎称事务已回滚，也不自动删除已入库画像。命令不清空 `db_profile`，`--clean` 只作用于本次输入目录。

## 5. 实施文件与职责

| 文件 | 改动 |
| --- | --- |
| `metaweave/cli/main.py` | 注册 `meta_load` 子命令。 |
| `metaweave/cli/meta_loader_cli.py`（新增） | 定义 Click 参数、重复/缺失检查、退出码与简短结果输出；不在命令体直接写 SQL 或删除文件。与 `rag_load` 同为加载入口，但目标是平台库。 |
| `backend/profile_loading/__init__.py`（新增） | 建立平台画像加载包，不在导入时连接数据库。 |
| `backend/profile_loading/db_loader.py`（新增） | 实现可由 CLI 及后续 API 复用的 `load_db_profile(source_id, job_id, clean=False) -> dict`：执行 §3 的校验、映射、入库及提交后的可选清理；不依赖 Click 或 FastAPI。结果至少含 `success`、`execution_time`、`message`，已生成时包含 `publish_id`，并回显规范化 `source_id`、`job_id`。 |
| `backend/persistence/models.py` | 复用现有 `DbProfile` 映射；其 `schemas` 为 JSON 数组，若 ORM 类型注解仍写作 `dict`，须修正为与实际产物一致的列表类型。不改表结构。 |
| `backend/jobs/repository.py`、`backend/data_sources/repository.py` | 复用既有工作区归属/步骤行查询与数据源查询；仅在确有缺口时增加只读帮助函数，不复制两套校验。 |
| `metaweave/core/clean_targets.py`、`metaweave/utils/file_utils.py` | 收紧共享 `resolve_db_output_dir()`：在 `resolve()` 前检查 `output/source/job/db` 各层符号链接，并核对真实路径等于规范预期路径；采集写入、采集 `--clean`、本命令读取和清理共用该检查。复用 `clean_top_level_files()`，不新增第二套路径拼接或递归清理函数。 |
| `tests/unit/test_meta_loader_db.py`（新增） | 覆盖 CLI、字段映射、版本及归属校验、事务失败、`--clean` 时序与目录边界；用假平台会话和临时目录，不依赖真实源库。 |

本次不为 `ddl/json` 创建空的加载器、数据库表或 Click 分支。将来设计这两类产物时再确定文件结构、表结构和入库规则；不能把 `ddl/json` 当作本命令已实施能力。

## 6. 验收

1. `metaweave meta_load --help` 只展示本阶段有效的 `db` 类型及三个必填选项；`--type ddl/json/all`、缺失或重复的 `--type/--source-id/--job-id` 均在写库、清理前以 exit `2` 拒绝。
2. 使用指定 `source_id/job_id` 下的合法 `database.json` 加载后，`db_profile` 新增一行，字段与 JSON 一致，`publish_id` 为新 UUID，`job_info` 未被改动；同一工作区再次加载产生另一行而不覆盖旧行。
3. 指定目录没有 `database.json`、JSON 损坏、版本不支持、文件 `source_id` 不匹配、工作区不属于数据源或 `db` 步骤未成功，均不插入画像、不清理文件；只从指定工作区读取。
4. 未带 `--clean` 时文件原样保留。带 `--clean` 时先确认画像已提交，再删除该 `db/` 顶层文件；同层子目录及其文件、别的 `job_id` 目录保持不变。`output/`、数据源、作业或 `db/` 任一层是指向项目内其他工作区的符号链接时，采集与加载都须拒绝，链接目标文件保持不变。
5. 模拟平台库提交失败：输入文件仍在。模拟提交成功后清理失败：返回 exit `1`，消息包含 `publish_id` 并明确画像已入库，不误称为“加载已回滚”。
6. 不要求源库在线，不读取源库凭据；日志和命令输出不得包含平台库密码或完整连接串。本次命令不创建 `publish_info`、不设置当前版本、不触发 Web 页面变化。
