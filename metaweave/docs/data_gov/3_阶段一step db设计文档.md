# 阶段一 `--step db` 设计文档

> 状态：设计稿，尚未实施。本文是 [doc 2：添加数据源与数据库概览发布模块设计](2_添加数据源与数据库概览发布模块设计.md) 的第一项可独立验收的后台交付，不表示 Web、作业和发布已经具备。
>
> 源库用户名使用明文 `username` 字段，仅密码使用加密的 `password_ciphertext` 字段，与 doc 2 §3.1 一致。`db_source_info` 的源库数据库名列统一为 `db_name`（不使用 `database_name`），并在首版表中包含可空的 `created_by_user_id`；doc 2 §3.1 与本文 §2 使用相同字段模型。平台表由管理员按各阶段设计中的 SQL 手工创建或修改，不引入 Alembic；本次只创建 `db_source_info`，其余平台表在对应功能开发时再建。数据源身份、目录和统计口径仍沿用 doc 2。

## 1. 本次开发范围与顺序

目标：先在平台 PostgreSQL 库 `metaweave` 中手工登记一条 PostgreSQL 数据源，再运行 `metaweave metadata --step db --source-id <UUID>`，得到 `output/<source_id>/db/database.json`。这条路径要为后续 Web API 和 Worker 留出同一个共享采集函数，但本次只交付独立 CLI 入口。

本次以 **PostgreSQL 18.x 源库**作为开发和验收样本；数据源登记与 `--step db` 的版本准入规则统一为 **PostgreSQL 15.0 及以上**（`server_version_num >= 150000`）。14.x 及更早版本拒绝；达到门槛的版本均按相同逻辑登记、采集，实际 SQL 失败按普通失败处理，不另设“已验证版本”准入分支。

本次完成：

1. 平台库连接配置、`public.db_source_info` 首版表结构、Fernet 凭据工具，以及开发期登记入口 `scripts/register_db_source.py`；手工插入/读取一条数据源。平台库库名可配置，默认 `metaweave`；平台库连接环境变量与现有源库 `DB_*` 分开。
2. `db` 采集函数、严格的选中 schema 校验、数据库概览统计与版本为 `1` 的 JSON 产物。
3. CLI 接线：`--step db`、仅此步骤可用且必填的 `--source-id`、`--clean`，以及只清理当前数据源 `db/` 目录的路径保护。
4. 自动化测试和一条真实数据源的手工验收。手工插入只用于开发阶段，不构成未来用户登记数据源的正式入口。
5. 创建与现有 `metaweave/` 平级的最小 `backend/` Python 包，仅包含本次所需的平台库、数据源读取/登记、密码加解密和 `db` 应用服务；同步增加 §5.1 所列包初始化文件、`pyproject.toml` 的 `backend*` 包发现及 `platform` extra。本次不创建 `frontend/`，也不引入平台库表结构升级工具。

本次**不做** FastAPI、Vue 页面、`job_info` / `job_step_info`、Worker、`.job_id` 的创建、`publish_info` / `db_profile`、发布、Neo4j/Milvus 加载，也不把 `db` 加入 `pipeline`。现有 `ddl/md/json/rel/cql` 的目录与行为保持不变。独立 CLI 不写 Web 作业状态；没有 `job_id` 也能生成并检查 JSON。

推荐实施顺序：先建平台库和唯一一张数据源表 → 完成加解密工具并手工登记 → 实现可接收已解析数据源的采集函数与 JSON → 接入 CLI → 测试成功、失败及 `--clean`。本次不以现有 `.env` 的 `DB_HOST/DB_USER/DB_PASSWORD` 作为 `db` 的临时回退来源，避免正式接入平台时维护两套数据源身份与目录规则。

## 2. `public.db_source_info` 表结构

`source_id` 是数据源的稳定 UUID，也是工作目录名；显示名或密码变更不改变它。第一阶段只支持 `db_type = 'postgresql'`。首版字段固定如下：

| 字段 | PostgreSQL 类型与约束 | 含义 |
| --- | --- | --- |
| `source_id` | `uuid PRIMARY KEY` | 服务端生成的数据源 ID。 |
| `display_name` | `text NOT NULL` | 管理员可读名称，非空白。 |
| `db_type` | `text NOT NULL CHECK (db_type = 'postgresql')` | 首版源库类型。 |
| `host` | `text NOT NULL` | 源库主机；不含凭据。 |
| `port` | `integer NOT NULL CHECK (port BETWEEN 1 AND 65535)` | 源库端口。 |
| `db_name` | `text NOT NULL` | 源库数据库名。 |
| `username` | `text NOT NULL` | 源库用户名，明文保存；仅向管理员展示。 |
| `password_ciphertext` | `text NOT NULL` | Fernet 加密的源库密码。 |
| `sslmode` | `text NULL`，无默认值 | 管理员填写或选择的可选源库连接选项；有值时作为单独的 `sslmode` 参数原样传给 psycopg，未填写时不传。 |
| `selected_schemas` | `text[] NOT NULL`，非空数组且无 `NULL` 元素 | 本数据源选定的 schema 名称；拒绝空串、纯空白名称及重复名称，但保留合法名称中的首尾空格。 |
| `is_active` | `boolean NOT NULL DEFAULT true` | 数据源启用状态，默认启用；启停及禁用拦截规则见 [doc 11](11_数据源启用与禁用设计.md)。 |
| `created_by_user_id` | `uuid NULL` | 创建该数据源的平台登录用户 ID；本次手工登记时留空。 |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | 登记时间。 |
| `updated_at` | `timestamptz NOT NULL DEFAULT now()` | 最后修改时间；本阶段仅新增，后续加入修改入口时由应用显式维护。 |

`created_by_user_id` 专指创建人，而非最后修改人。当前尚无平台用户表和登录体系，手工登记时写 `NULL`，暂不加外键；将来登录功能上线后，后端从已验证的登录身份填入，不接受前端自行指定。若需要记录最后修改人，应另设计字段，不能覆盖创建人。表级约束至少覆盖 `cardinality(selected_schemas) > 0` 且数组中没有 SQL `NULL` 元素；名称校验以 `name.strip() == ""` 判定空串或纯空白，但**不得将 `strip()` 的结果写回**；重复校验、存在性/权限查询与后续统计均按原始名称进行，`"sales "` 与 `"sales"` 是不同名称。登记工具与采集入口均须校验，不能只相信人工插入的数据。可选的 `sslmode` 未填写时保存 SQL `NULL`，表和后端都不设置默认值；有值时规范化（去首尾空白、转小写）后用于连接（doc 18.1 修订：按 libpq 六模式值域校验，非法值建连前拒绝，替代原"应用不设模式取值白名单"的决定）。`source_id` 由登记工具生成 UUID 并插入，不要求数据库扩展提供 UUID 函数。本阶段只有登记工具的新增路径，正常新增时 `created_at` 与 `updated_at` 由同一次插入的默认值生成；首个修改入口（如后续 Web API 的 PATCH）加入时，须由应用显式更新 `updated_at`。本表暂不增加 `status`、`current_publish_id` 或作业字段；将来 Web 页面登记时沿用同一模型，不另建第二张来源表。

供管理员在新建的平台库中手工执行的首版建表 SQL 如下；它只创建平台表 `public.db_source_info`，不是 `--step ddl` 生成的源业务库 DDL：

```sql
CREATE TABLE public.db_source_info (
    source_id uuid PRIMARY KEY,
    display_name text NOT NULL,
    db_type text NOT NULL CHECK (db_type = 'postgresql'),
    host text NOT NULL,
    port integer NOT NULL CHECK (port BETWEEN 1 AND 65535),
    db_name text NOT NULL,
    username text NOT NULL,
    password_ciphertext text NOT NULL,
    sslmode text NULL,
    selected_schemas text[] NOT NULL
        CHECK (cardinality(selected_schemas) > 0
               AND array_position(selected_schemas, NULL) IS NULL),
    is_active boolean NOT NULL DEFAULT true,
    created_by_user_id uuid NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
```

本节 SQL 是本阶段平台表结构的依据；先确认目标为平台 `metaweave` 库且该表尚不存在，再手工执行，不要对已有表重复执行 `CREATE TABLE`。在其他环境部署时同样由管理员检查现有表结构后执行适用 SQL，不自动建表或升级；未来修改这张表时应另写明确的变更 SQL，不通过重新执行本节建表语句覆盖旧表。本次不创建 doc 2 的其余四张表，也不要求平台 schema 版本表或迁移工具。

上述字段表与建表 SQL 已同步 doc 11 的 `is_active` 增量，供新环境使用；已有表的加列操作按 [doc 11 §2](11_数据源启用与禁用设计.md#2-表结构与迁移) 执行，已存在该列的环境无需重复加列。

平台库连接使用独立的 `PLATFORM_DB_HOST`、`PLATFORM_DB_PORT`、`PLATFORM_DB_NAME`、`PLATFORM_DB_USER`、`PLATFORM_DB_PASSWORD`，另可选填 `PLATFORM_DB_SSLMODE`；与现有 metadata 主配置中的源库 `database.*`、`.env` 的 `DB_*` 严格区分。其中 **HOST、USER、PASSWORD 三项必填**，PORT 未配置时为 `5432`，NAME 未配置或为空时为 `metaweave`；PORT 非法时仍报配置错误。这些配置从服务端本机 `.env`/进程环境读取；其中 **`PLATFORM_DB_PASSWORD` 直接以明文字符串配置，不做 Fernet 加密**。它是应用登录平台库的密码，不是 `db_source_info` 中的源库密码。手工登记和 CLI 读取使用同一平台库连接配置；平台库连接失败必须报错，不回退到源库连接，也不得回退使用 `DB_PASSWORD`。

平台库连接独立于源库连接：本阶段的登记工具与 `db` CLI 均通过 `backend/persistence/platform_db.py` 建连；`PLATFORM_DB_SSLMODE` 有值时传入该值，未设置或为空时不传 `sslmode`，后端不补默认。连接仍显式使用 `connect_timeout=10` 秒，未来 API/Worker 复用相同规则。建连超时不限制后续查询或整次任务。新增 `db` 源库连接则按 `db_source_info.sslmode` 遵循同样的“有值才传”规则；既有 `ddl/json/rel` 的连接方式保持原样。未传 `sslmode` 时由 libpq 自行决定，常见支持 SSL 的构建默认 `prefer`，不是 `disable`。

当前项目并非进程启动时自动加载 `.env`：`ConfigLoader.load()` 在读取主配置时才调用 `load_dotenv(项目根目录/.env)`，进程中已设置的环境变量保持优先。因此 `--step db` 的 CLI 准备流程必须**先**调用一次 `load_main_config_snapshot(config_path)`，完成主配置加载及现有预检，**再**读取 `PLATFORM_DB_*` / `SOURCE_CREDENTIAL_KEY` 和查询平台库；不能反过来先读平台库环境变量。此顺序也适用于将来的 Worker，且均须发生在源库连接和目录清理之前。不需要为本阶段的 CLI 再单独加载一次 `.env`，也不需要为了这个顺序提前拆出 `validate_main_config(config)`；以后若改为传入已加载主配置，则须保证同等校验没有被绕过。

本次实施时在已有的 `.env.example` 中追加上述变量和 `SOURCE_CREDENTIAL_KEY` 的占位项，不放真实密码或密钥；实际值只写入已被 Git 忽略的本机 `.env`（或由进程环境注入）。以下仅列出本阶段 `--step db` 所需的变量；Web 阶段另需 `PLATFORM_CORS_ORIGINS`，见 [doc 5](5_前后端独立部署与API地址配置设计.md)。例如变量名称如下，占位值不能直接作为生产凭据：

```dotenv
PLATFORM_DB_HOST=localhost
PLATFORM_DB_PORT=5432
PLATFORM_DB_NAME=metaweave
PLATFORM_DB_USER=YOUR_PLATFORM_USER
PLATFORM_DB_PASSWORD=YOUR_PLATFORM_PASSWORD
# 可选：需要指定平台库 sslmode 时再填写 PLATFORM_DB_SSLMODE
SOURCE_CREDENTIAL_KEY=YOUR_FERNET_KEY
```

`SOURCE_CREDENTIAL_KEY` 是源库密码的加解密密钥，同样属于敏感配置，但它**不用于**加密或登录平台库。运行时不得在日志、异常或 JSON 中输出 `PLATFORM_DB_PASSWORD` 和密钥。

## 3. 密码加密与手工登记

源库用户名明文写入 `username`；仅源库密码使用应用层 `cryptography.Fernet` 加密，写入 `password_ciphertext`。单独的 `SOURCE_CREDENTIAL_KEY` 环境变量保存 Fernet 密钥；密钥不入平台库、不入 Git，也不写入 `database.json`。开发者只生成一次密钥并妥善保存；更换密钥前必须安排重加密或密钥轮换，否则旧密码不可解密。用户名、`host`、端口、数据库名、选中 schema 便于后续管理员查看；它们仍属于管理员连接信息，不向未来的只读目录用户开放。

手工登记工具不经过 metadata 主配置加载，故它必须先显式加载**项目根目录** `.env`（或由调用进程提前注入环境变量），再读取平台库连接与 `SOURCE_CREDENTIAL_KEY`；不能假定 CLI 曾在同一进程运行过。之后交互取得 `display_name`、`host`、`port`、`db_name`、`username`、密码、`selected_schemas` 和可选的源库 `sslmode`（密码不回显）；`display_name` 是必填的管理员可读名称，空串或纯空白须在连接和插入前拒绝，不自动生成名称 → 校验其余登记字段 → 按 `host`、`port`、`db_name`、`username` 四元组查询已登记数据源 → 调用与 `db` 采集相同的 `build_connect_kwargs(...)`（doc 18.1 更名），以这些**尚未入库**的凭据建立一次临时 psycopg 连接（`connect_timeout=10` 秒，仅当填写 `sslmode` 时传入）→ 调用共享的 `read_and_validate_server_version(conn)` 确认 PostgreSQL 15.0 及以上 → 将该连接传给 `validate_selected_schemas(conn, names)`，按原始名称检查所选 schema 在 `pg_namespace` 中存在且有 `USAGE` 权限（每条校验 SQL 使用 60 秒语句超时）→ 关闭测试连接 → 只加密密码 → 用参数化 SQL 或 ORM 把 `display_name`、明文用户名、密码密文及可选 `sslmode` 等登记字段插入一行。若四元组命中已有记录，先列出各记录的 `source_id`、`display_name` 和 `selected_schemas`，提示可能重复并要求管理员明确确认后才继续；取消时不插入，不自动复用旧 `source_id`。这只是防误操作提示：同一连接可因采集范围或用途不同而登记为不同数据源，本阶段不加四元组唯一约束。连接、版本或 schema 检查失败时拒绝登记，不插入数据源；这次检查不执行完整画像统计，也不能代替后续采集时的再次校验。未提供 `sslmode` 时存 `NULL`，不补 `disable`。不把明文密码写进 SQL 文件、shell 命令参数、Git 跟踪的配置或日志。后续 Web API 复用相同的 `encrypt_password` / `decrypt_password` 函数，不在 API 中另造一套算法。

登记工具的 schema 输入以逗号分隔，普通名称写作 `public,sales`；分隔符后的空格不会自动去除。若任一名称含首尾空格，工具先以 `repr` 形式显示解析结果并要求确认；未确认时重新输入 schema，确认后才按原始名称查询。合法的带空格名称仍可登记。schema 不存在或无 `USAGE` 权限的错误也以 `repr` 显示名称边界，使 `"sales "` 与 `"sales"` 的错误可区分。

`db` 执行时按 `source_id` 从平台库读取该行，先校验类型、用户名和选中 schema，再在内存中解密密码，并与明文用户名一起构造源库连接参数。缺密钥、密钥错误、密文损坏、未找到数据源均须在源库连接及目录清理前失败，错误不得回显密码、密文或完整连接串。解密结果仅供连接使用，不进入长久保存的配置快照。`db_source_info.sslmode` 有值时规范化（去首尾空白、转小写）后作为**单独参数值传入**，为 `NULL` 时不传、后端不补默认（doc 18.1 修订：公共参数模块按 libpq 六模式值域校验，非法值在建连前拒绝，替代原"不做白名单或建连前预检、由连接尝试报错"的决定）。管理员对自己填写或选择的连接选项负责，应用负责正确还原并传递参数，不得让 `sslmode` 的内容被解析成其他 libpq 参数。此规则不修改既有步骤的连接行为。

若 `db` 采集复用现有 `DatabaseConnector` 的连接池，`database.pool_min_size` / `database.pool_max_size` 从 `--config` 指定的主配置快照读取，缺省沿用连接器的 `1/5`；它们只控制源库连接池，不覆盖 `db_source_info` 中的源库身份或 `selected_schemas`。若 `db` 实现不使用连接池，则无需为本步骤虚设这两个参数。手工登记工具仅用一次临时 psycopg 连接完成验证并关闭，不创建连接池，也不读取主配置中的连接池参数。

现有 `DatabaseConnector` 使用字符串拼接连接参数；本次在 `metaweave/core/metadata/connector.py` 增加模块级纯函数 `build_source_connect_kwargs(*, host, port, db_name, username, password, sslmode, connect_timeout_seconds, statement_timeout_ms) -> dict`，由 `db` 采集与手工登记工具共同调用（该函数已按 doc 18.1 迁移至 `metaweave/db/postgres_options.py:build_connect_kwargs`，原函数删除，两个超时参数改为可选——DDL/JSON/REL 既有路径不传超时）。它把登记的 `host`、`port`、`dbname`、`user`、`password`、`connect_timeout` 和固定生成的 `options`（设置 `statement_timeout`）分别放入 psycopg kwargs，仅当 `sslmode` 有值（经规范化与值域校验）时才包含该键；函数不读配置、不建连接，不给 `sslmode` 设默认值（doc 18.1 修订：新增 libpq 六模式值域白名单，由该公共函数唯一维护）。超时常量留在 `db_step.py`，由两个调用方传入；`connector.py` 不反向导入 `db_step.py`。kwargs 可直接传给 `psycopg.connect()`；如连接池需要连接串，就用 `psycopg.conninfo.make_conninfo(**kwargs)` 构造，**不得用 f-string 拼接字段值**。各入口必须复用同一组参数构造规则，不能各写一套。这样用户名、密码中的空格和特殊字符可正确传递，`sslmode` 即使包含空格或 `host=...` 等文本，也仍只是 `sslmode` 的一个值，不能改变其他连接参数；值不被 libpq 接受时正常报连接错误。用户填写的域名或 IP 原样作为 `host` 交给 libpq，本阶段不预解析 DNS、不维护 IP 禁止清单、不增加 `hostaddr` 固定逻辑。返回的 kwargs 含密码，不得整体记录日志。平台库连接同样不得手工拼接含密码的连接 URL/conninfo。连接日志**允许**输出用户名，但不得输出源库或平台库密码、密码密文、密钥、完整连接串，也不得输出密码长度；连接异常若可能带出连接串，须先脱敏再记录。

doc 4 的 Web 登记阶段继续复用上述连接参数构造函数：连接检查用当次提交的 `host`、`port`，创建时重新建连并校验版本与 Schema；以后的 `run_db_step()` 用登记表中的 `host`、`port`。三类入口不引入额外的目标地址解析层，连接失败由 libpq 报告。

## 4. `database.json` 契约及采集指标

文件固定为 UTF-8 JSON，`schema_version` 为整数 `1`。`database.json` 是独立于现有逐表画像 JSON 的产物族：逐表画像使用字符串 `metadata_version: "3.0"` 并按其自身契约严格校验，本文件使用整数 `schema_version: 1`；后续平台同时导入两类产物时，必须按产物类型分别校验版本字段，不得混用版本号或校验器。本阶段 `database.json` 不记录 `sslmode` 等连接级选项或本次连接实际是否使用 TLS，不作为连接传输审计记录；不能从登记表当前的 `sslmode` 反推这次采集的实际连接状态。顶层字段和结构如下，字段名在本阶段定稿；`collected_at` 使用带 `Z` 的 UTC RFC 3339 格式，固定六位小数（如 `2026-09-22T09:30:00.000000Z`）；所有计数为非负整数。示例值仅说明结构：

```json
{
  "schema_version": 1,
  "source_id": "b9a4943d-44d6-4f35-b553-d25207f9d8c1",
  "collected_at": "2026-09-22T09:30:00.000000Z",
  "database": {
    "type": "postgresql",
    "host": "127.0.0.1",
    "port": 5432,
    "name": "orders",
    "server_version": "18.6",
    "size_bytes": 104857600,
    "size_scope": "whole_database",
    "record_count": null
  },
  "selected_schemas": ["public", "sales"],
  "schemas": [
    {
      "name": "public",
      "ordinary_table_count": 8,
      "partitioned_table_count": 1,
      "partition_child_count": 12,
      "view_count": 2,
      "materialized_view_count": 1,
      "object_count": 12
    },
    {
      "name": "sales",
      "ordinary_table_count": 3,
      "partitioned_table_count": 0,
      "partition_child_count": 0,
      "view_count": 1,
      "materialized_view_count": 0,
      "object_count": 4
    }
  ],
  "totals": {
    "ordinary_table_count": 11,
    "partitioned_table_count": 1,
    "partition_child_count": 12,
    "view_count": 3,
    "materialized_view_count": 1,
    "object_count": 16
  }
}
```

JSON 顶层 `source_id` 必须使用规范 UUID 字符串 `str(uuid.UUID(raw_source_id))`，即小写、带连字符；不得直接写入 CLI 原始输入。数据库中的 UUID、工作目录名与 JSON 值均代表同一个规范化后的数据源身份。

`database.server_version` 是源库版本的展示字符串（如 `"18.6"`），由本次连接已读取并通过门槛校验的 `server_version_num` 转换，**不增加第二次版本查询**。当前采集代码直接内联格式化；doc 4 的 Web 接入阶段将其抽为 `db_step.py` 中的 `format_server_version(server_version_num) -> str`，供画像和连接检查共用（如 `150006` → `"15.6"`），不得在 API 再写一份转换。新生成的文件必须包含该字段；顶层 `schema_version` 仍为 `1`，它表示 JSON 契约版本，不是数据库版本。旧版 `schema_version: 1` 文件可能缺少 `server_version`，后续读取或发布时按“版本未知”兼容处理，不把缺失值伪造成当前数据库版本；需要完整画像可重新执行 `--step db`。

`selected_schemas` 来自 `db_source_info`，不得被 `metadata_config.yaml` 的 `database.schemas` 或 CLI 的 `--schemas` 覆盖；`schemas` 为每个选中 schema 恰好一条统计，`totals` 是这些行逐字段求和。输出前按 schema 名称在应用层升序排列 `selected_schemas` 和 `schemas`，两者顺序一致；不依赖登记数组的原始顺序或 SQL 的隐含返回顺序。`collected_at` 记录实际采集时间，即使固定到微秒，跨次采集仍可能不同；比较画像内容或检查无意义 diff 时应忽略该字段，而不是把微秒精度当成完全稳定的输出。schema 没有四类对象时计数为 `0`；schema 不存在、无权限或查询失败绝不能伪装成空 schema。对象统计只覆盖选中 schema，而 `database.size_bytes` 是当前**整库**大小，二者口径不同。`database.record_count` 本阶段始终是 JSON `null`，表示未采集；不执行全库 `COUNT(*)`，不写 `0` 冒充真实零行。

连接源库后、执行任何对象统计 SQL 或 `--clean` 前，先用 `SHOW server_version_num` 读取并解析服务器版本号。`metaweave/core/metadata/db_step.py` 定义唯一门槛常量 `MIN_POSTGRESQL_VERSION_NUM = 150000`，并提供共享的 `validate_server_version_num(value)` 与 `read_and_validate_server_version(conn) -> int`：后者读取一次版本号、解析为整数并调用前者。`--step db`、手工登记工具及后续 Web 连接检查/创建均复用这套校验，不得各自写版本条件。值无效或 `< 150000` 时明确报“要求源库 PostgreSQL 15.0 及以上”，本次采集任务失败（CLI exit 1），不清理旧产物，也不写 `database.json`；登记入口则在插入 `db_source_info` 前拒绝。值 `>= 150000` 时按相同指标 SQL 采集，查询失败按普通任务失败处理。采集继续使用本次已读取的整数生成 `database.server_version`，不增加第二次查询。

指标计算口径固定为：

| 指标 | PostgreSQL 来源/判定 | 计入 `object_count` |
| --- | --- | --- |
| 普通表 | `pg_class.relkind = 'r' AND NOT relispartition` | 是 |
| 分区表 | `relkind = 'p' AND NOT relispartition`；仅顶层分区父表 | 是 |
| 分区子表 | `relispartition AND relkind IN ('r', 'p')`；包括中间层及叶子分区 | 否，单独显示 |
| 视图 | `relkind = 'v'` | 是 |
| 物化视图 | `relkind = 'm'` | 是 |
| 数据库大小 | `pg_database_size(current_database())`，单位字节；含索引、TOAST、系统目录等 | 不适用 |
| 数据库记录数 | 不查询；固定 `null` | 不适用 |

每个 `object_count` 必须等于同一行的普通表、分区表、视图、物化视图之和；`totals.object_count` 也满足该等式。序列、外部表、复合类型及分区子表不计入合计。分区子表可以与父表位于不同 schema，因此某个选中 schema 的 `partition_child_count > 0`、`partitioned_table_count = 0` 是合法统计结果；不得要求父表也位于该 schema 或同在 `selected_schemas` 中。统计**不应用**主配置的 `database.include_object_types` / `exclude_tables`，因为这是源库对象概览，不是已画像对象数。不能直接复用当前把 `r/p` 合并为 `table` 的旧对象分类 SQL；需查 `pg_class` 的 `relkind`、`relispartition` 并按 `pg_namespace` 分组。

采集前对 `selected_schemas` 每项以**原始名称**查询 `pg_namespace` 确认存在，再用 `has_schema_privilege(oid, 'USAGE')` 确认权限；统计查询也使用同一原始名称，不得去除首尾空格。不存在与无权限分别报错。对象查询和大小查询使用不吞异常的入口，连接失败/权限失败均导致本次步骤失败。不要用当前 `information_schema.schemata` 的返回列表推断不存在，也不要用当前会将查询异常变成 `[]` 的 `get_schemas()` / `get_database_objects()` 判定零对象。可访问但没有目标对象的 schema 才记录零值。JSON 不得含用户名、密码、密文、密钥、连接串或任意 `.env` 值。接入 doc 4 的 Web 登记时，本模块新增 `discover_selectable_schemas(conn)`，并让它和 `validate_selected_schemas()` 共用系统/临时 Schema 排除常量与 `pg_namespace`/`USAGE` 查询；这会将显式选择、手工登记和采集的范围一并收紧，既有系统/临时 Schema 登记须修正后再采集。

`db` 步骤连接**源库**时固定使用 `connect_timeout=10`（秒）；其 schema 校验、对象统计和整库大小等每条源库 SQL 固定使用 `statement_timeout=60000`（毫秒，即 60 秒）。两者在 `metaweave/core/metadata/db_step.py` 定义为 `SOURCE_CONNECT_TIMEOUT_SECONDS = 10`、`SOURCE_STATEMENT_TIMEOUT_MS = 60000`，`db` 采集与手工登记工具导入同一份常量；不从 `db_source_info` 或主配置读取，也不改变现有 `ddl/json/rel` 等步骤的默认行为。平台库的 10 秒建连超时由 §2 的独立连接策略规定，不套用源库的 `statement_timeout`。连接超时只约束建立连接；语句超时逐条约束 SQL，**不是整次 `db` 作业的总时限**。任一超时须使本步骤失败并指出超时环节，不得把未完成的统计写为 `0`、输出不完整的 `database.json` 或报告成功。后续 Web 连接测试与 schema 发现复用同样的源库超时值；本次不实现 Web 入口。

## 5. CLI、共享执行函数与输出目录

命令形式：

```bash
uv run metaweave metadata --config configs/metadata_config.yaml --step db --source-id b9a4943d-44d6-4f35-b553-d25207f9d8c1 --clean
```

`--source-id` 对 `db` 必填，只允许一次，必须能解析为合法 UUID 且能查到数据源；对其他 metadata 步骤禁止使用。入口将文本统一解析为 `uuid.UUID` 对象，后续查询、路径解析和 JSON 生成只传递这个对象或其 `str()` 规范形式，不再使用原始输入字符串。大写、无连字符、带花括号等可解析形式视为同一个 UUID；解析失败在副作用前以用法错误（exit 2）退出。`db` 不接受显式 `--schemas`、`--tables`、`--max-workers`、`--domain` 等其他步骤选项，也不能与 `--generate-*` 同时选择。其他参数与模式错误同样以 exit 2 退出；连接、权限或统计失败以任务失败（exit 1）退出。未传 `--clean` 时沿用现有 CLI 默认 `false`。第一阶段 `pipeline.metadata.db` 仍为未知步骤，不将 `db` 偷加进 doc 26 的 pipeline 顺序。

参照 `ddl` 的“Click 解析参数 → 构造共享执行请求 → 返回结构化结果”模式接线，但**不**复用 `ddl` 的表级采集器或 `output/ddl` 目录。`db` 的准备顺序固定为：校验 CLI 参数 → `load_main_config_snapshot(config_path)`（此时加载项目根 `.env` 并校验主配置）→ 从环境读取平台库连接与解密密钥 → 查询 `source_id` 对应的数据源并在内存中解密源库密码 → 构造已校验的执行请求 → 调用 `execute_db`。CLI 与将来的 Worker 应复用这条准备链和同一个 `execute_db`（或等价名称）业务函数；核心采集函数接收已核验的数据源身份，并在请求中携带已校验的主配置快照，返回现有共享层同形的 `{success, execution_time, message}`，不抛 Click `Abort`，也不反向导入平台库适配器。每次任务只加载、校验一次主配置；本阶段 `execute_db()` 不读取该快照，源库身份、schema、输出路径只取本文件规定的来源，不被主配置覆盖。将来的 Worker 不直接复制 CLI 命令体，也不能跳过该准备链。

产物路径**唯一**为项目根目录下 `output/<source_id>/db/database.json`；其中 `<source_id>` 固定为 `str(source_uuid)`（小写、带连字符），不得采用 CLI 原始文本。`db` 是固定步骤目录名，不替换成真实库名，不加入 `job_id`。路径与 `--clean` 目标必须由同一解析函数从规范化后的 UUID 派生，不接收用户提供的输出目录，也不能使用主配置的 `output.*` 重新指向别的数据源。清理前校验目标确实为当前规范 `source_id` 的 `db/` 目录，并限定在项目根内；`--clean` 仅删除该目录顶层普通文件，保留子目录，不碰其他数据源及旧 `output/ddl` 等目录。后续 [doc 7](7_数据源查看与删除设计.md) 的 `DELETE /api/data-sources/{source_id}?delete_output=true` 是唯一会删除整个 `output/<source_id>/` 工作区的入口，不改变这里的 `--clean` 范围。删除范围包括旧 `.job_id` 隐藏文件。即使不传 `--clean`，独立 CLI 在覆写 `database.json` 之前也要使旧 `.job_id` 失效，且自己不写新的 Web 作业标记。

主配置加载与预检、连接平台库、查询数据源、解密凭据、校验目标路径，以及连接源库后的版本前置检查，都须发生在任何目录清理或删除 `.job_id` 之前，顺序遵循上段及 §4 的规定。带 `--clean` 时，旧 `.job_id` 随目录顶层文件一起清理；不带 `--clean` 时，先完成统计和 JSON 字段校验，仅在即将覆写 `database.json` 前删除旧 `.job_id`：若采集或校验失败，旧 JSON 和它的标记均不动；若删除标记后写入失败，允许旧 JSON 留存但不得再保留可能误指新产物的旧标记。独立 CLI 在成功或失败时都不写新的 `.job_id`。统计成功并通过 JSON 字段校验后，使用项目已有的 `atomic_write_json()` 或等价临时文件加原子替换方式写入 `database.json`，避免正常覆盖时留下半截 JSON。`--clean` 后若采集失败，旧工作区文件可能已删除；本次没有发布机制，不能把工作目录当作历史版本库。失败消息应指出源库连接、版本、schema、权限或统计哪个环节失败；可包含用户名，但不能包含密码、密文、密钥或完整连接串。

本阶段手工 CLI 不提供同一 `source_id` 的并发互斥：两个终端同时运行（尤其一个带 `--clean`）可能相互清理或覆盖文件；原子替换只防止写出半截 JSON，不能消除这类竞争。操作上须串行执行同一数据源的手工采集。未来 Web 作业层的数据库互斥只约束经其提交的作业；如果届时仍允许手工 CLI 与 Worker 或发布并行操作同一数据源，还须让这些入口共用互斥机制，不能认为作业表会自动保护独立 CLI。

实施接线至少检查：`metadata_cli.py` 的 Click Choice、单次参数保护及作用域校验；`core/metadata/execution.py` 的 operation/request、日志 step 映射与共享执行入口；`core/clean_targets.py` 的专用 `source_id` 清理目标解析；源库连接器的安全连接参数**及构造日志**；打包配置与平台可选依赖。当前 `DatabaseConnector.__init__` 的 `User` INFO 日志可以保留，`Password` 日志即使只输出按真实密码长度生成的星号也必须删除，不能泄露密码长度；保留 host、port、dbname 和用户名即可，错误日志也不能回显含密码的连接串。平台库连接日志遵循同一边界。平台库读取适配器在确认选择 `db` 后延迟导入，未安装平台依赖时现有 CLI 步骤和 `--help` 不受影响；真正执行 `db` 缺依赖时明确提示安装 `platform` extra。不要仅把 `db` 塞进现有 `ddl/json/md` 分支或从 `output.db_directory` 推断目录。

### 5.1 本次代码文件与主要函数

为后续 Worker 复用，第一版把平台库读写和凭据处理放在 `backend/`，把不依赖平台库的采集、统计与文件生成放在 `metaweave/core/`。下表是本次实施的**最小文件规划**；函数名是建议的稳定职责边界，允许实现时在同一文件内调整辅助函数，不应新增一套只供 Worker 使用的采集代码。

| 文件 | 本次新增/修改 | 主要函数与职责 |
| --- | --- | --- |
| `backend/persistence/platform_db.py` | 新增 | `load_project_env()` 供独立登记工具显式加载项目根 `.env`；`load_platform_settings_from_env()` 读取并校验 `PLATFORM_DB_*`（含可选 `PLATFORM_DB_SSLMODE`），不回退到 `DB_*`；`platform_session()` 提供短生命周期 SQLAlchemy Session，只有配置了 `PLATFORM_DB_SSLMODE` 才传入 `sslmode`，并显式传入 `connect_timeout=10`，不读取源库主配置。CLI 的 `db` 准备链在主配置加载后只调用后两者，不重复加载 `.env`。 |
| `backend/persistence/models.py` | 新增 | 定义 `DbSourceInfo` SQLAlchemy 模型，与 §2 的 DDL 一致；本次不预建作业/发布模型。 |
| `backend/security/source_password.py` | 新增 | `encrypt_password(plaintext)` / `decrypt_password(ciphertext)` 使用 `SOURCE_CREDENTIAL_KEY` 和 Fernet；只处理**源库**密码，不处理平台库密码。错误消息不含敏感值。 |
| `backend/data_sources/repository.py` | 新增 | `get_db_source(session, source_id)` 按 UUID 精确查询；`find_db_sources_by_connection(session, host, port, db_name, username)` 供登记前提示可能重复的记录，不负责拒绝登记；`insert_db_source(session, fields)` 供手工登记工具写一行，`fields` 至少包含 `source_id`、非空白 `display_name`、`db_type`、`host`、`port`、`db_name`、`username`、`password_ciphertext`、`selected_schemas`，可选 `sslmode`，本阶段 `created_by_user_id` 为 `NULL`。均使用 ORM/参数化 SQL，不在查询函数中采集源库。 |
| `backend/data_sources/db_step_service.py` | 新增 | `run_db_step(config_path, source_id, clean) -> dict`：**CLI 与未来 Worker 共用的应用入口**。函数入口先设置日志 step 为 `db`，再加载主配置快照和 `.env`、读取平台库配置、查数据源、解密密码、构造内存请求，最后调用核心 `execute_db()`；不导入 Click、FastAPI，不写 `job_info`。定义不依赖 Click 的 `DbStepConfigError`，供各入口分别处理配置错误。 |
| `metaweave/core/metadata/db_step.py` | 新增 | 定义源库超时常量、`MIN_POSTGRESQL_VERSION_NUM = 150000`、共享的 `validate_server_version_num()` / `read_and_validate_server_version()` 与不含平台模型的 `DbExecutionRequest`；`validate_selected_schemas(conn, names)` 接收一个可执行查询的 psycopg 连接，供登记工具与采集共用严格存在性/权限校验：登记工具传入一次性连接，`db` 采集若使用连接池则从池中取得连接后传入；不要求把现有 `DatabaseConnector` 对象传给该函数。`execute_db(request) -> dict` 在清理前连接源库并复用版本校验，再做可选清理、采集及原子写入；`collect_database_profile(connector, request)` 从连接器取得 psycopg 连接并调用同一 schema 校验函数，再查询对象数量及整库大小并组装 §4 的 JSON 数据。本模块不查询平台库、不读取 `.env`、不导入 `backend`/Click；`execute_db()` 返回 `{success, execution_time, message}`。请求中的源库密码仅存内存，密码和主配置快照字段均声明为 `dataclasses.field(repr=False)`。doc 4 的 Web 接入阶段须为版本及 Schema 校验增加 `DbProfileValidationError` 子类（具体类型见 doc 4 §6.1），供 API 按类型映射安全错误码；独立 CLI 继续捕获基类并给出原有中文提示，不靠解析提示文字判断原因。 |
| `metaweave/core/metadata/db_step.py`（doc 4 扩展） | 后续 Web 接入阶段，非本阶段已完成项 | 抽取 `format_server_version(server_version_num)`；新增 `discover_selectable_schemas()`、`SYSTEM_SCHEMA_NAMES`、`TEMP_SCHEMA_PREFIXES`，让发现与校验共用系统/临时 Schema 排除规则；新增 `DbProfileValidationError` 的四个子类 `UnsupportedServerVersionError`、`SchemaMissingError`、`SchemaNoUsageError`、`SchemaNotSelectableError`，供 Web 按类型映射错误码。独立 CLI 仍捕获基类，保留原有中文提示；完整契约见 doc 4 §5–§6。 |
| `metaweave/utils/source_id.py` | 新增 | `normalize_source_id(value) -> uuid.UUID` 接收字符串或 UUID 对象，返回规范 UUID 对象；供应用服务与清理目标解析器共用，不依赖平台库或清理逻辑。 |
| `scripts/register_db_source.py` | 新增 | 开发期手工登记入口 `main()`：先调用 `load_project_env()`，交互取得非空白的 `display_name` 和其余登记字段，通过 `getpass` 取得源库密码，可选填写源库 `sslmode`；查询相同连接四元组并展示已有记录，命中时须明确确认才继续；导入 `db_step.py` 的源库超时常量，调用公共参数模块的 `build_connect_kwargs(...)`（doc 18.1 更名）建立一次性 psycopg 连接，再调用共享版本校验与 `validate_selected_schemas(conn, names)` 验证源库及所选 schema，失败则不插入，结束时关闭连接，不创建连接池；通过后调用加密与 repository 插入，输出 `source_id`，不输出密码。后续 Web 登记替换这个入口，复用下层模块。 |

Web 登记实施后，手工工具仍作为开发期入口保留：查重完成即关闭平台库会话，源库校验完成后另开短会话插入，不在源库连接期间占用平台库会话。工具不采集数据源说明，插入时明确写入 `description = NULL`；Web 入口可单独填写该字段。

上述是 **8 个新增业务 Python 文件**；本次还须创建 `backend/__init__.py`、`backend/persistence/__init__.py`、`backend/security/__init__.py`、`backend/data_sources/__init__.py`，使这些目录成为明确的包，且初始化文件不得提前导入 SQLAlchemy、Fernet 或 FastAPI。平台表由管理员按 §2 的 SQL 手工创建，本次不创建 `alembic.ini` 或 `migrations/`。`pyproject.toml` 的包发现本次纳入 `backend*`；`[project.optional-dependencies].platform` 首版直接声明 SQLAlchemy 2 和 `cryptography`，供平台表访问和 Fernet 使用，不加入 Alembic。`psycopg`、`python-dotenv` 已是基础依赖，不重复加入；FastAPI 与 ASGI 服务运行依赖在后续 Web API 阶段追加到同一个 `platform` extra，本次脚本阶段不安装也不导入它们。`.env.example` 只增加占位变量。

`DbExecutionRequest` 至少携带已校验的主配置快照、`source_id`、`db_type`、登记表中的 `host`、`port`、`db_name`、`username`、解密后的 `password`、可空的 `sslmode`、`selected_schemas` 和 `clean`；不携带 SQLAlchemy 模型或平台 Session。密码和主配置快照字段均使用 `dataclasses.field(repr=False)`，因为快照中也可能含凭据；但这只保护 `repr`，**不能**阻止 `dataclasses.asdict()`、`vars()` 或 `__dict__` 导出敏感值。禁止把请求整对象、这些导出结果或完整连接参数写入日志/异常。需要记录运行参数时只显式挑选非敏感字段。`collect_database_profile()` 返回纯数据字典，不写盘；`execute_db()` 是唯一负责本步骤输出路径、`--clean` 和 JSON 写入的函数。

现有文件的改动范围也固定：

| 文件 | 需要改动的接线 |
| --- | --- |
| `metaweave/cli/metadata_cli.py` | 增加 `db` Choice 和 `--source-id`，完成参数模式校验；仅在 `db` 分支延迟导入并调用 `run_db_step()`。配置错误转 Click 用法错误（exit 2），采集结果 `success=False` 转任务失败（exit 1）。不在 CLI 中直接写 SQL、JSON 或作业表。 |
| `metaweave/core/metadata/execution.py` | 将 `db` 加入 operation 标识，固定 `OPERATION_STEP_MAP['db'] = 'db'`，供共享应用入口设置日志 step；采集实现留在 `db_step.py`，不继续膨胀现有 `ddl/json/md` 分支。`pipeline_config.py` 的步骤集合仍不含 `db`。 |
| `metaweave/core/clean_targets.py` | 只增加按规范 UUID 对象推导的唯一 `db/` 输出/清理目标解析，复用 `metaweave.utils.source_id.normalize_source_id()`；目录名仅用 `str(source_uuid)`，不得从原始输入或通用 `output.db_directory` 获取目标。 |
| `metaweave/core/metadata/connector.py` | 新增模块级 `build_source_connect_kwargs(...)`（doc 18.1 已迁至 `metaweave/db/postgres_options.py:build_connect_kwargs`），按 §3 从独立参数安全构造 psycopg kwargs；`db` 采集、登记工具及 doc 4 的 Web 入口均调用它，并分别传入 `db_step.py` 的同一组超时常量。`db` 路径始终传入 `connect_timeout=10`，仅当数据源登记的 `sslmode` 有值时才将规范化后的值作为该参数传入（doc 18.1 修订），并通过受控的连接启动选项设置每个池连接的 `statement_timeout=60000` 毫秒（例如固定的 `options=-c statement_timeout=60000`）。连接池需要连接串时使用 `psycopg.conninfo.make_conninfo(**kwargs)`；不得手工拼接字段值。不得在连接器中补全局 `sslmode` 缺省（doc 18.1 修订：模式白名单由公共参数模块统一维护，连接器经它构造参数、不自行拼装）；现有 `ddl/json/rel` 的连接行为保持不变。当前连接器使用 autocommit，不能仅靠事务外的 `SET LOCAL` 设置超时。其他既有步骤不因本次改造获得新的全局超时默认值。删去打印密码长度的日志；`db` 统计使用不吞异常的查询入口，不直接复用会把异常变成空列表的旧方法。 |
| `configs/logging.yaml` | **必须**新增 `db` 日志分流：`filters` 增加 `db_filter`（`allowed_steps: ['db']`），`handlers` 增加 `db_file`（`filename: logs/db.log`，其他设置参照 `ddl_file`），并把 `db_file` 加入 `root.handlers`。只定义 handler 而不挂到 root 不会生效；step 名须与 `OPERATION_STEP_MAP['db'] = 'db'` 一致。保留现有步骤的日志行为。 |

### 5.2 两个入口与未来 Worker 的调用边界

本次实际运行链为 `metadata --step db` → CLI 参数校验 → `backend.data_sources.db_step_service.run_db_step()` → `metaweave.core.metadata.db_step.execute_db()` → `database.json`。`run_db_step()` 入口先调用 `set_operation_step('db')`（或等价的 `set_current_step('db')`），早于主配置读取、平台库连接和其他可能产生日志的业务操作；不能只依赖 CLI 中已有的 step 设置，因为未来 Worker 会直接调用此共享函数，不经过 metadata CLI 或通用 `execute_metadata_operation()`。这样向 root 传播的 `backend.*` 日志也按 `db` 过滤器写入 `logs/db.log`。随后 `run_db_step()` 在任何平台查询或路径操作前调用 `normalize_source_id()`，之后只传 UUID 对象或其规范字符串；它在调用 `execute_db()` 前**只执行一次** `load_main_config_snapshot(config_path)`，该函数当前通过 `ConfigLoader.load()` 加载项目根 `.env`，随后才允许调用 `load_platform_settings_from_env()`、查询 `db_source_info` 和解密源库密码。这样独立 CLI 无需依赖进程启动时预先导出环境变量，也不会绕过现有主配置预检。传给核心 `execute_db()` 的请求携带已校验快照，但本阶段采集不读取它；采集使用已解析的数据源，不再次读主配置或平台表。

未来 Web Worker 的入口增加作业领取和 `job_info` / `job_step_info` 状态写入，但执行 `db` 时**调用同一个 `run_db_step(config_path, source_id, clean)`**；它不启动 `metaweave metadata ...` 子进程，不导入或模拟 Click，也不再实现一套 schema 查询、JSON 组装或文件清理。CLI 不写作业表，Worker 才负责作业状态；这是入口职责的差异，不是两套 `db` 采集逻辑。本次仅为此保留稳定的 Python 函数边界，不创建 Worker 文件或作业表。

`run_db_step()` 的配置错误（非法/不存在的 `source_id`、平台连接变量或密钥缺失、主配置预检失败等）以 `DbStepConfigError` 或等价的无 Click 异常表示，且发生在清理前；CLI 将其转换为 exit 2，将来 Worker 则将对应作业标记为失败。**平台库连接变量齐全，但建连超时、不可达或认证失败**属于运行失败，由 `run_db_step()` 返回 `success=False`，CLI 转 exit 1；它也必须发生在清理与连接源库之前。连接源库、源库连接/语句超时、权限、统计和写文件等运行失败由 `execute_db()` 返回 `success=False`，CLI 同样转 exit 1，Worker 将来据此写失败状态；超时不能被吞掉并转成零对象。配置错误与运行失败的文本均不得含密码、密文、密钥或完整连接串。手工登记工具是单独的开发入口：它复用平台连接、模型、加密、repository、源库连接参数构造及 `validate_selected_schemas()`，但不调用 `run_db_step()` 或完整画像采集。

后续 `job_id` 工作区改造的共享返回契约以 [doc 9 §4](9_db采集作业目录与数据库画像表设计.md) 为准：本次 `job_id` 确定后，`run_db_step()` 的成功与任务失败结果均在原有 `{success, execution_time, message}` 基础上增加规范化字符串 `job_id`；`execute_db()` 的三个字段可保持不变，由应用服务补字段。前置配置错误仍抛异常，不保证返回字典或 `job_id`。

## 6. 验收

1. 本机 `.env` 中的 `PLATFORM_DB_*` 可连接平台库，`PLATFORM_DB_PASSWORD` 为未加密的原始平台库密码且不会回退到 `DB_PASSWORD`；手工登记工具与 `db` CLI 的平台库连接仅在配置可选的 `PLATFORM_DB_SSLMODE` 时传入对应 `sslmode`，未配置时不传，均显式使用 `connect_timeout=10`，不读取源库主配置。`PLATFORM_DB_*` 必填项缺失时 CLI exit 2；变量齐全但平台库连接超时、不可达或认证失败时 CLI exit 1，且不连接源库、不清理工作区。手工建表并登记一条带明文用户名、加密源库密码和至少一个 schema 的数据源，`created_by_user_id` 留空；再次登记相同 `host/port/db_name/username` 时展示已有 `source_id`、名称和所选 schema，取消则不插入、明确确认则允许新建独立 `source_id`，表上不设四元组唯一约束。登记工具导入与 `db` 采集相同的 10 秒建连、60 秒语句超时常量，以单次临时 psycopg 连接调用 `validate_selected_schemas(conn, names)` 检查所选 schema 的存在性和 `USAGE` 权限，检查后关闭连接，不创建连接池；`db` 采集从连接池取连接时也传给同一函数。错误凭据、不可达、schema 不存在或无权限时不插入数据源。`selected_schemas` 为 `ARRAY['public', NULL]` 时表约束拒绝，空串、纯空白或重复名称由登记工具/采集入口拒绝；名称 `"sales "` 不被修剪，与 `"sales"` 区分，按原样查询与输出。正常登记后 `created_at` 与 `updated_at` 相同。用户名可直接读取、源库密码可正确解密，密钥错误时采集前失败；平台库或数据源缺失时不回退到 `DB_*`。
2. `--step db --source-id <UUID>` 生成上述 `schema_version=1` 的 JSON，字段齐全；其版本校验与逐表画像的 `metadata_version="3.0"` 分开，互不复用版本字段或校验器。JSON 不含用户名、密码或密文，日志允许出现用户名，但不得出现源库/平台库密码、密文、密钥、完整连接串或密码长度（包括按长度打印的星号）。用特征明显的测试密码断言 `repr(DbExecutionRequest)` 与连接成功/失败日志均不含该值；不得通过记录请求整对象或 `dataclasses.asdict()` 等导出结果泄露密码。特殊字符凭据可连接。大小为整库字节数，记录数是 `null`。
3. 选中 schema 为空、重复、不存在、无 `USAGE` 权限、查询失败分别给出明确错误；存在且无目标对象的 schema 统计为零。`selected_schemas` 和 `schemas` 按名称升序且顺序一致，`collected_at` 为 UTC `Z` 结尾、六位小数（微秒）。分区父表及 12 个叶子分区计为 1 个分区表、12 个分区子表，对象合计只增加 1；父表与子表位于不同 schema 时，子表所在 schema 可以只有分区子表计数而没有分区父表计数。
4. 同一 `source_id` 重跑输出路径稳定；大写、无连字符及带花括号的同一 UUID 输入，都命中同一个小写带连字符的 `output/<source_id>/db/`，JSON 的 `source_id` 也相同；`--clean` 只清这个规范目录的顶层文件，不产生大小写等别名目录，另一 `source_id` 与旧 `output/ddl`、`output/md` 保持不变；独立 CLI 重写 JSON 时旧 `.job_id` 不会遗留。前置校验失败时旧 `.job_id` 不动；不带 `--clean` 且采集失败时旧 JSON 与标记都不动；删除标记后写入失败时不留下旧标记；独立 CLI 从不写新标记。
5. `--source-id` 缺失、重复、无效或用在其他步骤；`db` 与不适用参数/`--generate-*` 混用，均在副作用前拒绝。原有 `ddl/md/json/rel/cql` 与 pipeline 行为不回归。
6. 仅在项目根 `.env` 中设置 `PLATFORM_DB_*` 和 `SOURCE_CREDENTIAL_KEY`、不预先导出到 shell 时，独立 `--step db` 仍能读取；主配置预检失败时不读取数据源、不连接源库、不清理目录。独立手工登记工具在未运行 metadata CLI 的新进程中也能加载项目根 `.env`。将来接入 Worker 时复用相同的“先主配置、后平台库”顺序。
7. 单测分别覆盖 repository/密码工具、严格 schema 与对象统计、清理目标和 CLI 错误映射；使用模拟平台库和连接器验证 `run_db_step()` 只加载一次主配置，且核心 `execute_db()` 不访问平台库。`metaweave --help` 和现有 metadata 步骤在不安装 `platform` extra 时仍能使用；本次无需启动 Worker 即可完整运行 `--step db`。
8. `db` 源库连接参数在登记 `sslmode` 有值时原样作为单独参数传入，未登记时不含 `sslmode`；无论是否登记，均含 10 秒 `connect_timeout`，每个池连接的统计 SQL 具有 60 秒 `statement_timeout`。平台库的可选 `PLATFORM_DB_SSLMODE` 同样测试有值原样传入、无值不传；~~两者均不设应用白名单，libpq 不接受的值在连接时失败~~（doc 18.1 修订：六模式值域校验，非法值在建连前拒绝，业务库与平台库共用同一校验函数）。测试密码包含空格或特殊字符仍按原值连接；`sslmode` 包含空格或 `host=...` 等文本时不能变成额外连接参数。若复用现有源库连接池，`pool_min_size/pool_max_size` 取主配置值或连接器缺省 `1/5`。模拟源库及平台库连接超时和源库语句超时均返回失败、不写零值或不完整 JSON。独立 `ddl/json/rel` 等步骤的连接行为不因本次变更而改变。未来 Web 连接测试接入时复用同一组源库超时值。
9. PostgreSQL 18.x 源库按 §4 正常采集；模拟 `server_version_num = 140000`、`140012` 或版本号不可解析时，在任何对象统计 SQL 与 `--clean` 之前给出明确不支持错误，CLI exit 1，原有 `database.json` 保持不变。模拟 `150000`、15.x、16.x、17.x 和 18.x 时均通过同一个版本前置校验，进入相同采集流程；后续 SQL 若失败则按普通任务失败处理。手工登记对低于 15.0 的版本也在插入前拒绝；两入口使用相同门槛常量与校验函数。
   新生成的 JSON 必须分别写入对应的 `database.server_version`（例如 `150000` → `"15.0"`、`150006` → `"15.6"`、`160000` → `"16.0"`、`180006` → `"18.6"`），不额外查询版本，且不改变顶层 `schema_version: 1`。
10. `configs/logging.yaml` 的 `db_filter`、`db_file` 和 `root.handlers` 均已接线；运行 `metadata --step db` 后，`logs/db.log` 不仅存在，还包含本次 `db` 采集的实际日志行。单测直接调用 `run_db_step()`（模拟未来 Worker）时，即使不经过 CLI，也在平台库读取等早期日志前设置 `step=db`，这些日志可落入 `logs/db.log`；不能只凭终端有输出或日志文件被创建就判定分流成功。
11. 手工登记时必须输入并保存非空白 `display_name`；空串或纯空白在连接源库、加密密码和插入平台表前被拒绝，不自动使用 `host:port/db_name` 等值补齐。成功登记后可按 `source_id` 读回原始名称。
12. 用模拟连接验证手工登记和 `db` 采集都调用 `build_connect_kwargs(...)`（doc 18.1 修订：原 `build_source_connect_kwargs(...)` 已迁移至 `metaweave/db/postgres_options.py` 并更名），并传入相同的源库超时常量；对相同的源库字段，两条入口得到相同的连接参数（含固定 `options`），仅连接 API 形式可不同。分别覆盖 `sslmode` 为 `NULL` 时不传、有值时经规范化（去首尾空白、转小写）后作为单独参数传入（doc 18.1 修订：替代原"原样传入"），以及密码包含空格或特殊字符时不被拆成其他连接参数；返回的 kwargs 不写日志。
13. doc 4 接入后，连接检查与创建分别用当次请求的 `host`、`port` 建连，`run_db_step()` 用登记表中的 `host`、`port` 建连；三个入口均复用 `build_connect_kwargs(...)`（doc 18.1 修订：随第 12 条更名同步），不增加服务端 DNS 预解析、IP 禁止清单或 `hostaddr` 参数。模拟域名和 IP 输入时，断言连接函数收到的 `host`、`port` 与提交或登记的值一致。

本次验收结束后，才按 doc 2 后续阶段增加 Web 登记、Worker 作业、`db_profile` 和发布；届时用本次的同一张数据源表与同一个 `db` 共享采集函数，不重写一条平行实现。
