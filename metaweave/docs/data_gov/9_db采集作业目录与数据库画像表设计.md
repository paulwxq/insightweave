# `db` 采集作业目录与数据库画像表设计

> 状态：设计稿，尚未实施。本文件只设计独立 CLI 的 `metadata --step db`、平台表 `job_info` / `db_profile` 及其交付顺序；不改造其他 metadata 步骤、pipeline、Web 采集或发布接口。
>
> 本设计修订 [doc 2](2_添加数据源与数据库概览发布模块设计.md) 中“独立 CLI 不写 `job_info`”、作业/步骤分表与状态字段、`.job_id` 标记、`db_profile.job_id` 可空以及 `output/<source_id>/db/` 的约定，并修订 [doc 3](3_阶段一step db设计文档.md) 中 `db` 产物不带 `job_id` 的路径约定。在这些冲突点上以本文为准。`db` 采集和画像发布仍是两个独立动作。

## 1. 目标与边界

执行 `metaweave metadata --step db --source-id <UUID> [--job-id <UUID>] [--clean]`。未指定 `--job-id` 时生成新 UUID；指定时使用已登记、属于该数据源的工作区。`job_id` 是可以跨天继续使用的工作区身份；本阶段执行的 `db` 步骤把产物写到：

```text
output/<source_id>/<job_id>/db/database.json
```

两级 UUID 均使用 `str(uuid.UUID(...))` 的小写、带连字符形式。`db` 是固定步骤目录名，不是实际数据库名。例如，已有数据源 `9b06f9f1-bbba-468d-8c09-fa5c6f737a96` 的一次新作业可能写入：

```text
output/9b06f9f1-bbba-468d-8c09-fa5c6f737a96/550e8400-e29b-41d4-a716-446655440000/db/database.json
```

同一个 `job_id` 下可逐步补充步骤，也可再次执行某个步骤；本阶段先让 `db` 支持新建或复用工作区。后续改造 `ddl/json/...` 时再让它们使用已有 `job_id`，从对应工作区读取上游产物。`--job-id` 省略时不会自动选取“最近一次”；用户显式指定才会复用。本次不把 `db` 加入 pipeline，也不改变其他步骤的输出路径。

本阶段只创建 `job_info` 和空的 `db_profile` 表。`database.json` 保持现有 `schema_version: 1` 和字段格式；`--step db` **不向 `db_profile` 写入任何数据**。将来的发布动作选择一个成功的 `job_id`，读取其 `db/database.json`，验证后才写入 `db_profile`。本设计不创建 `publish_info`，不实现发布接口、Worker 或 Web 作业状态页。

## 2. `public.job_info`

`job_info` 每行记录**一个 `job_id` 下一个步骤的最近一次执行状态**。同一 `job_id` 可以对应多行，且所有行必须属于同一 `source_id`；这个归属由 Python 校验，不建外键。`job_id` 在新建工作区时用 `uuid.uuid4()` 生成，不依赖 PostgreSQL UUID 扩展。当前没有单独的作业主表，也不创建 `job_step_info`。

| 字段 | 含义 |
| --- | --- |
| `job_id uuid` | 工作区 ID，与 `step_name` 组成主键，同时是第二级目录名。 |
| `source_id uuid` | 所属数据源 ID；新建及复用时由 Python 校验。 |
| `step_name text` | 步骤名，本阶段仅写 `db`；与 `job_id` 组成主键。 |
| `last_status text` | 这个步骤最近一次执行的状态，只允许 `running`、`success`、`failed`。补跑时更新同一行。 |
| `run_by_user_id uuid NULL` | 最近一次执行该步骤的平台用户；当前无登录身份，独立 CLI 写 `NULL`。 |
| `created_at timestamptz` | 该步骤第一次登记的时间；查询一个工作区的创建时间时取其步骤行的最早值。 |
| `updated_at timestamptz` | 该步骤最近一次状态写入时间。开始执行改为 `running`，以及写回 `success` 或 `failed` 时，由应用触发、值通过数据库 `now()`（代码为 `func.now()`）生成，因此只会前进。采集作业列表的「执行时间」取同一 `job_id` 全部步骤行的 `max(updated_at)`，列表按该时间倒序。 |

在已有 `public.db_source_info` 的平台库中执行：

```sql
CREATE TABLE public.job_info (
    job_id uuid NOT NULL,
    source_id uuid NOT NULL,
    step_name text NOT NULL CHECK (length(btrim(step_name)) > 0),
    last_status text NOT NULL CHECK (
        last_status IN ('running', 'success', 'failed')
    ),
    run_by_user_id uuid NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, step_name)
);

CREATE INDEX job_info_source_created_idx
    ON public.job_info (source_id, created_at DESC);
```

新建工作区时先确认 `source_id` 存在，只生成一次 UUID，并确认该 `job_id` 尚无任何步骤行；若发生 ID 冲突，本次任务直接失败（exit 1），不重新生成 UUID、不连接源库或创建产物目录。复用工作区时，查不到任何该 `job_id` 的步骤行、或已有行的 `source_id` 与本次指定值不一致，都在执行前报错（exit 2）。主键保证同一工作区的同一步骤最多一行；补跑该步骤只更新 `last_status`、`updated_at` 和最近执行人，不改 `created_at`。采集作业列表不把最早的 `created_at` 当作执行时间。步骤标签仍按各行 `created_at` 从早到晚拼接；列表上的执行者仍取最早一行的 `run_by_user_id`。索引 `job_info_source_created_idx` 仍服务按登记时间查询，列表排序在读出该数据源的步骤行之后完成。错误详情只写日志，日志须包含 `job_id`、`source_id`、`step_name` 以便检索；表中不存错误文本、配置快照、心跳、租约或文件数量。

## 3. `public.db_profile`

`db_profile` 存放**已发布**的数据库画像，而非当前工作目录中的最新文件。第一版从现有 `database.json` 映射下列指标：

| JSON 路径 | 表字段 | 说明 |
| --- | --- | --- |
| `schema_version`、`source_id` | `schema_version`、`source_id` | 发布前核对 JSON 与选中的数据源一致。 |
| 选中的作业目录 / `job_info` | `job_id` | 发布时从已选择的 `job_id` 获取，并核对 `(job_id, 'db')` 行属于同一 `source_id` 且 `last_status = 'success'`；不从文件修改时间猜测。 |
| `collected_at` | `collected_at` | 按 UTC 时间解析。 |
| `database.type`、`host`、`port`、`name`、`server_version` | `db_type`、`host`、`port`、`db_name`、`server_version` | `server_version` 允许为空，以兼容旧版 JSON。连接信息仅供管理员展示。 |
| `database.server_version_full`、`database.os_platform` | `server_version_full`、`os_platform` | doc 13 增补：前者是该次采集 `SELECT version()` 的原串，后者是解析出的安装平台展示串（族名 + 架构，如 `Windows x86_64`；只解析出族名时为 `Linux`）。两者均可为空——旧版 JSON 无此键，或该次采集读取/解析失败。加载端只校验类型与非空，不校验取值。 |
| `database.size_bytes`、`size_scope`、`record_count` | 同名字段 | `size_scope` 当前为 `whole_database`；未采集记录数保持 SQL `NULL`，不可写成零。 |
| `selected_schemas`、`schemas` | `selected_schemas text[]`、`schemas jsonb` | `schemas` 保留每个 schema 的对象分类计数，便于详情展示。 |
| `totals.*` | 六个 `*_count` 列 | 保存普通表、分区父表、分区子表、视图、物化视图和对象合计；`object_count` 沿用 JSON 的既定口径，不把分区子表重复计入合计。 |

`publish_id` 在将来的发布动作中生成，并作为 `db_profile` 的主键，保证一次发布最多对应一条数据库画像。`source_id` 和 `job_id` 不建外键；未来发布代码检查数据源存在，以及 `job_info` 中 `(job_id, 'db')` 行属于同一数据源且最近一次执行成功。当前 `db_profile` 保持空表。

```sql
CREATE TABLE public.db_profile (
    publish_id uuid PRIMARY KEY,
    source_id uuid NOT NULL,
    job_id uuid NOT NULL,
    schema_version integer NOT NULL,
    collected_at timestamptz NOT NULL,
    db_type text NOT NULL,
    host text NOT NULL,
    port integer NOT NULL CHECK (port BETWEEN 1 AND 65535),
    db_name text NOT NULL,
    server_version text NULL,
    server_version_full text NULL,
    os_platform text NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    size_scope text NOT NULL,
    record_count bigint NULL,
    selected_schemas text[] NOT NULL,
    schemas jsonb NOT NULL,
    ordinary_table_count bigint NOT NULL,
    partitioned_table_count bigint NOT NULL,
    partition_child_count bigint NOT NULL,
    view_count bigint NOT NULL,
    materialized_view_count bigint NOT NULL,
    object_count bigint NOT NULL
);

CREATE INDEX db_profile_source_collected_idx
    ON public.db_profile (source_id, collected_at DESC);
```

发布时校验 `schema_version = 1`、`schemas` 是预期结构、计数非负、JSON 的 `source_id` 与所选工作区一致，且 `job_info` 的 `(job_id, 'db')` 行 `last_status = 'success'`，再在发布事务中插入画像。画像不保存密码、`sslmode` 或实际 TLS 状态。将来页面读取哪个版本，仍由 doc 2 约定的 `publish_info.is_current_publish` 决定；**不能**按本表 `collected_at` 最大值直接展示。未发布的 `database.json` 可以有很多份，但不会出现在平台数据库画像页面。

## 4. `--step db` 的执行与目录修改

不传 `--job-id` 创建工作区；传入时在已有工作区内执行或补跑 `db`：

```bash
metaweave metadata --step db --source-id 9b06f9f1-bbba-468d-8c09-fa5c6f737a96 --clean
metaweave metadata --step db --source-id 9b06f9f1-bbba-468d-8c09-fa5c6f737a96 --job-id 550e8400-e29b-41d4-a716-446655440000 --clean
```

执行顺序：

1. 沿用现有 CLI 参数、主配置、平台配置和数据源校验。新增 `--job-id` 仅适用于 `--step db`，最多传一次，按 `uuid.UUID` 规范化；非法 UUID、指定的 ID 在 `job_info` 中不存在，或已有步骤行所属 `source_id` 与本次指定值不一致，都在源库连接和目录清理前以 exit 2 拒绝。不传时仅生成一次 `job_id = uuid.uuid4()`；生成的 ID 若已被占用，本次以 exit 1 失败，不重生、不创建目录。
2. 在平台库短事务中为 `(job_id, 'db')` 插入一行 `last_status='running'`；补跑已有行时，原状态为 `success` 或 `failed` 才更新为 `running` 并继续采集。若原状态仍为 `running`，本次直接以 exit 1 退出，提示“当前步骤可能已有作业正在执行”，不连接源库、不清理或写入文件。新建/更新状态提交失败也以 exit 1 结束。提交后关闭平台库会话，采集期间不持有平台库事务。
3. 将 `source_id` 和确定的 `job_id` 传入共享 `DbExecutionRequest`。`resolve_db_output_dir(source_id, job_id)` 是 `database.json` 写入路径和 `--clean` 目标的唯一解析函数，规范化两个 UUID；在解析真实路径前拒绝 `output/`、`<source_id>/`、`<job_id>/`、`db/` 任一已存在层级的符号链接，并确认真实路径等于项目根下的规范 `output/<source_id>/<job_id>/db/`。只检查“仍在项目根内”不足以防止跨工作区写入或清理。
4. `execute_db()` 继续负责源库连接、版本检查、统计和原子写入。返回结果或发生可捕获异常时，在新的短事务中将 `(job_id, 'db')` 的 `last_status` 更新为 `success` 或 `failed`，并更新 `updated_at`。错误详情写入带 `job_id` 的步骤日志，不写数据库；CLI 只输出简短结果和 `job_id`，成功/任务失败沿用现有退出码。若最终状态写入失败或无法确认，CLI 以 exit 1 结束，提示“采集产物可能已生成，但作业状态更新失败”，给出 `job_id` 和本次产物路径，并引导查看日志；不报告成功，也不自动删除已生成文件。后续发布须确认 `(job_id, 'db')` 的 `last_status='success'`，不能仅凭文件存在发布。
5. 进程异常终止后，步骤行可能留在 `running`。本阶段只给出上述提示，不设计自动纠正或 Web 按钮；管理员可在后台确认实际执行情况后处理该状态，例如使用 SQL。此后才可用同一 `--job-id` 补跑。

共享入口 `run_db_step()` 在确定本次 `job_id` 后，返回结果字典统一包含 `success`、`execution_time`、`message` 和 `job_id`；`job_id` 为规范化的小写带连字符 UUID 字符串。采集成功、采集失败，以及状态登记或最终状态写回失败等任务失败结果都带本次 `job_id`，供 CLI 输出和后续入口定位。核心 `execute_db()` 可继续只返回原有三个结果字段，由 `run_db_step()` 补上 `job_id`。参数非法、主配置校验失败、指定的 `job_id` 不存在或与 `source_id` 不匹配等前置配置错误仍抛异常（exit 2），不承诺返回结果字典或 `job_id`。

`--clean` 的清理目标严格限定为本次 `output/<source_id>/<job_id>/db/`，只删除该目录顶层文件；解析和清理函数均不得越过本次 `job_id` 目录。指定已有 `--job-id` 补跑 `db` 时，也只清理该工作区的 `db/`。旧路径 `output/<source_id>/db/database.json` 保留在原处，不自动移动、删除或当作本次结果；发布功能将来只从明确指定的 `job_id` 路径读取。由于目录已经给出作业身份，本阶段不创建 `.job_id` 调试标记，现有删除旧标记的逻辑应从这条新路径的执行流程中移除。

## 5. 实施文件与关联改动

| 文件 | 需要改动 |
| --- | --- |
| `backend/persistence/models.py` | 增加 `JobInfo`、`DbProfile` ORM 模型，分别以 `(job_id, step_name)`、`publish_id` 为主键，不定义外键，与 §2–§3 的 SQL 一致。应用启动不自动建表。 |
| `backend/jobs/__init__.py`（新增） | 建立与现有 `backend` 子包一致的包目录；保持空文件，不在导入包时建立平台库连接。 |
| `backend/jobs/repository.py`（新增） | 查询 `job_id` 的全部步骤并校验其 `source_id` 一致；新建工作区或插入新步骤行、补跑时更新 `last_status`、执行结束时按联合主键写回结果。插入冲突直接失败，不重新生成 UUID。 |
| `backend/data_sources/db_step_service.py` | 在现有准备链完成数据源校验后生成新 `job_id` 或验证传入的 `job_id`，登记/更新 `db` 步骤，调用核心执行并写回最近状态；在已确定 `job_id` 的成功/任务失败返回字典中补上规范字符串 `job_id`。错误详情写日志，不入库。保持平台库会话与源库统计过程分离。 |
| `metaweave/core/metadata/db_step.py` | `DbExecutionRequest` 增加 `job_id: UUID`；`execute_db()` 使用新的双 UUID 路径解析器，不在这里写 `job_info`。步骤日志记录 `job_id`、`source_id`、阶段、异常类型及经脱敏的错误原因，供失败排障；不得记录密码或完整连接参数。JSON 的字段及采集算法不变。 |
| `metaweave/core/clean_targets.py` | 把 `resolve_db_output_dir(source_id)` 扩为 `resolve_db_output_dir(source_id, job_id)`；写入和 clean 共用它。`resolve_source_workspace_dir(source_id)` 仍用于删除整个数据源工作区。 |
| `metaweave/cli/metadata_cli.py` | `--source-id` 仍必填；新增仅 `db` 可用、最多传一次的可选 `--job-id`，完成 UUID/作用域检查，并打印执行结果中的 `job_id`。其他步骤、`rag_load` 和 pipeline 不改。 |
| `tests/unit/test_db_step.py` | 更新 `resolve_db_output_dir()` 的路径断言和调用：现有 5 处 `monkeypatch.setattr(db_step, "resolve_db_output_dir", lambda _: ...)` 及 1 处直接赋值都要接收两个 UUID；`resolve_source_workspace_dir(source_id)` 应等于新 `db/` 路径的上两级，而非直接父目录。同步 `DbExecutionRequest`、`run_db_step()` 的替身签名和 CLI 派发断言；原来针对旧 `.job_id` 文件的用例按本设计调整。补测新建/复用工作区、找不到或错配的 `--job-id`、重复传参、非 `db` 步骤传参、同一步骤补跑、旧状态仍为 `running` 时以 exit 1 提示并停止、成功及已确定 ID 后的任务失败结果均带规范化 `job_id`、前置配置异常不要求结果字典、`last_status` 变化、日志定位信息及 `--clean` 不越过本次 `job_id` 目录；采集后 `db_profile` 仍为空。 |
| 后续发布模块 | 发布时通过 Python 检查 `(job_id, 'db')` 属于指定数据源且 `last_status='success'`，再插入 `db_profile`；`publish_id` 唯一性由主键保证。本次只记录此合同，不实施发布。 |

`--job-id` 的作用域由 `metadata_cli.py` 的 `db` 分支显式校验，像现有 `--source-id` 一样在其他模式执行前拒绝；不要把它简单加入通用 `_MODE_SCOPED_PARAMS["step"]`，该模式同时覆盖 `ddl/md/json/rel/cql`。因此现有 `tests/unit/test_metadata_generate_dim_config_cli.py::test_mode_scoped_params_matrix` 本次不必改；只有实施时决定重构通用作用域矩阵、为 `db` 单列模式，才同步修改该矩阵及测试。`_PARAM_DISPLAY` 也只在实际使用通用告警/显示逻辑时增补，本次显式拒绝不依赖它。

本阶段不因建表而修改现有数据源删除接口，也没有数据库外键导致“先删目录再删记录失败”的问题。历史作业与数据源的删除保留策略留到采集 API/发布模块设计时确定。本阶段不新增从 Web 触发采集的按钮或接口。

## 6. 实施顺序与验收

1. 在已存在 `db_source_info` 的平台库中由管理员按 §2、§3 的 SQL 依次手工建表。SQL 不作用于被采集的业务库；本设计阶段不执行建表。
2. 接入 ORM、`backend/jobs/` 包、作业仓储、`db` 服务、目录解析与 CLI，同步 §5 列出的测试。实施时凡与 doc 2、doc 3 的旧约定冲突，**必须以本文为准，不得沿用旧约定**；旧文档保留当时的阶段决策，**不得将逐条回改旧文档列为本阶段交付任务**。其他 metadata 步骤留待下一阶段。
3. 不传 `--job-id` 连续运行两次，得到两个不同工作区；传入第一次返回的 `job_id` 再运行，只更新原有 `(job_id, 'db')` 行，并写入同一工作区。带 `--clean` 时只调用指定 `job_id` 的 `db/` 清理函数，不解析或清理其他目录。
4. `--job-id` 非法、重复传入、用于非 `db` 步骤、指定的 ID 不存在或与 `--source-id` 不匹配时，均在连接源库、改动状态或清理目录前 exit 2。已有 `(job_id, 'db')` 仍为 `running` 时，以 exit 1 提示“当前步骤可能已有作业正在执行”并停止，不改动该行或文件，其他步骤行不受影响；本阶段不提供自动纠正或人工改状态的 Web 按钮。
5. 验收成功、源库连接失败、统计失败分别使 `(job_id, 'db')` 的 `last_status` 成为 `success` / `failed`，`updated_at` 随状态更新，`created_at` 在补跑时不变。模拟产物写入成功而最终状态更新失败：CLI 返回 exit 1，消息包含 `job_id`、产物路径与查看日志的提示，不误报成功，不删除已生成文件。错误详情只在日志中，`job_info` 无错误文本列；作业记录和日志不得包含源库密码。
6. 验收 `db_profile` 建好后仍为空；运行 `--step db` 不向它插入行。不存在的 `source_id` 由 Python 拒绝；模拟新生成的 `job_id` 被占用时，CLI 直接以 exit 1 失败，不重生、不连接源库、不创建产物目录。`publish_id` 唯一性由数据库主键保证，发布时若冲突也直接失败。

后续发布设计需要补建 `publish_info`，并要求发布请求明确选定一个 `db` 步骤最近一次成功的 `job_id`，在 Python 中检查三个 ID 的关联。这一步再实现 JSON 校验、画像入库和当前版本切换；不属于本次 `db` 路径改造。
