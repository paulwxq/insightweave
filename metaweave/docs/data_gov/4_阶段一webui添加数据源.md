# 阶段一 Web UI 添加数据源设计

> 状态：已实施“登记数据源”闭环。本文是 [doc 2：添加数据源与数据库概览发布模块设计](2_添加数据源与数据库概览发布模块设计.md) 的分阶段实施稿，沿用 [doc 3：`--step db` 设计](<3_阶段一step db设计文档.md>) 的数据源身份、平台库表和凭据规则。**本次 Web 开发范围以本文为准**：只交付“登记数据源”的闭环；doc 2 原定同阶段交付的 Web 采集、发布和画像展示延后，不属于本次验收。
>
> 前后端地址配置已按 [doc 5：前后端独立部署与 API 地址配置设计](5_前后端独立部署与API地址配置设计.md) 改造：浏览器通过 `VITE_API_BASE_URL` 直接访问 FastAPI，不再使用 Vite 开发代理；下文的文件职责已同步当前实现。
>
> 页面视觉与布局依据：[首页原型](ui/1.首页.html)、[数据资产管理首页](ui/2.数据资产管理首页.html)、[创建数据源原型](ui/3.创建数据源.html)；`ui/MetaWeaveUI.pen` 和同名 PNG 为设计参考。原型中的示例数字、数据库类型和对象列表不是本阶段的真实数据或交付承诺。

## 1. 开发范围

本次必须走通以下路径：

```text
首页 → 点击“数据资产” → 数据资产页 → 点击“创建数据源”
     → 填写 PostgreSQL 连接信息 → 测试连接并获取 Schema
     → 勾选至少一个 Schema → 保存到 public.db_source_info
     → 返回数据资产页，看到新登记的数据源
```

本阶段的“创建成功”只表示**登记成功**，不表示已经运行 `metadata --step db`、生成 `database.json`、发布画像或完成 RAG 加载。现有独立 CLI `metaweave metadata --step db --source-id ...` 保持可手工使用；Web 页面不调用它。本次不创建 `job_info`、`job_step_info`、`publish_info`、`db_profile`，不开发 Worker、采集/发布按钮、作业状态、发布历史、表/字段详情、画像统计、`PATCH` 修改数据源或其他数据库类型的连接能力。doc 2 §3.2 中的 `PATCH /api/data-sources/{source_id}` 属于后续阶段，不是本次漏实现的接口；上述其他功能也在下一阶段另行设计和实施。

页面仍要参考原型保留整体区块、留白、导航及视觉层次。未开发的区块可以显示“待接入”或空状态，但**不得填入看似真实的统计数字、表名、连接状态或成功提示**；尚不能执行的按钮应禁用并标明“即将开放”，不能点击后无反应或触发假流程。

## 2. 技术栈和仓库目录

前端固定使用 **Vue 3 + TypeScript + Vite**，通过 HTTP 调用 FastAPI；不从浏览器直连 PostgreSQL，不在前端保存源库密码。后端沿用 Python 3.12+、FastAPI、SQLAlchemy 2 和 psycopg 3，复用现有 `backend/` 平台库与凭据代码。前端路由使用 Vue Router；页面状态以组件状态和 API 请求为主，本阶段不引入全局状态库作为前提。

前端目录使用 **`./frontend/`，不是 `./front/`**。这是 doc 2 §2.1 已确定的、与 `metaweave/` 和 `backend/` 平级的目录名；doc 3 未创建它，因为 doc 3 只交付独立 CLI。本次的目录概览如下，逐文件职责见 §6：

```text
frontend/
├── package.json
├── index.html
└── src/
    ├── api/                 # 数据源 API 调用与类型
    ├── components/          # 侧栏、连接表单、Schema 选择器等
    ├── router/              # 首页、数据资产、创建页路由
    └── views/               # Home、DataAssets、CreateDataSource、DataSourceDetail
backend/
├── api/main.py              # FastAPI 应用与开发期本机绑定说明
├── api/routers/data_sources.py
├── data_sources/            # 复用并扩展登记、连接检查、Schema 发现
├── persistence/             # 复用平台库连接及 db_source_info 模型
└── security/                # 复用 Fernet 密码加密函数
```

前端依赖由 `frontend/package.json` 管理；FastAPI/ASGI 运行依赖加入现有 `pyproject.toml` 的 `platform` extra，不影响仅运行传统 CLI 的安装。首版依赖声明统一为 `fastapi>=0.115.9`、`uvicorn[standard]>=0.30.6`、`pydantic>=2.0`，实际安装版本由 `uv.lock` 锁定；使用 `uvicorn[standard]` 而非不带 extra 的 `uvicorn`，避免开发服务器的可选运行能力因安装写法不同而变化。前端页面按 `VITE_API_BASE_URL` 直接访问 FastAPI，后端按 `PLATFORM_CORS_ORIGINS` 放行前端 origin；生产部署的静态资源交付方式留待部署阶段。

创建 `frontend/` 前先更新仓库根目录 `.gitignore`：至少忽略 `frontend/node_modules/`、`frontend/dist/`、`frontend/.vite/` 及前端本地环境文件（不忽略用于说明的示例环境文件）；依赖锁文件应纳入版本控制。`docs/data_gov/ui/` 中的 `*:Zone.Identifier` 是 Windows 下载附带的非原型文件，本次应在 `.gitignore` 忽略该模式，提交原型时确认这三个文件未入库；不删除或改动原型 HTML、PNG、`.pen`。

本次后端使用同步 psycopg 与 SQLAlchemy Session。`connection-check`、创建和列表路由均定义为普通 `def`，由 FastAPI 在线程池执行；本次不在 `async def` 路由中直接调用同步 psycopg 或平台库访问，以免阻塞事件循环。后续若改用 `async def`，须把整个同步业务调用显式放入线程池并重新验收并发行为。

建议前端路由固定为 `/`（首页）、`/data-assets`（数据资产）和 `/data-assets/new`（创建数据源）。创建成功跳转 `/data-assets`；取消或返回也回到该页，不留下一个看似已经创建的假条目。

## 3. 页面设计与状态

| 页面 | 本次可用内容 | 原型中暂不接入的内容 |
| --- | --- | --- |
| 首页 | 保留原型的侧栏和主体布局；“数据资产”导航能进入数据资产页。 | 查询、分析项目、图表、系统设置等只保留视觉位置或明确标为未开放，不展示虚构的近期项目。 |
| 数据资产 | 展示从 `db_source_info` 读取的已登记数据源列表、登记数量、空状态和“创建数据源”入口；新建成功返回后重新请求列表。列表至少显示 `display_name`、`db_type`、`db_name`、选中的 Schema 与登记时间。 | 表/视图列表、行数、数据库大小、已发布数量及其他画像指标尚无平台画像来源；对应区块保留布局但显示“尚未接入采集与发布”，不使用原型示例值。MySQL、SQLite、CSV、Excel 等示例源不作为真实列表。 |
| 创建数据源 | 按原型分为数据库类型、连接信息、Schema 选择、可选说明四块；可返回数据资产页。 | MySQL、SQLite、SQL Server、Oracle 等类型保留卡片位置但不可选择，提示目前仅支持 PostgreSQL。 |

创建页字段为 `display_name`、`host`、`port`、`db_name`（页面可标为 Database）、`username`（页面可标为 User）、`password`、可选 `sslmode`、`selected_schemas`、可选 `description`。数据库类型本次固定提交 `postgresql`。`description` 是纯文本，前后端均限制为首尾空白去除后最多 1000 个字符；省略、`null`、空串或纯空白一律规范为 `NULL`，不得保存看似有说明的空白内容，超过上限则由后端拒绝。页面展示时按纯文本渲染，不作为 HTML 插入。密码输入默认遮蔽，可临时切换可见；不在页面、路由参数、浏览器持久化存储或接口响应中回显密码。`source_id` 由服务端生成，用户不填。`created_by_user_id` 在没有登录系统时为 `NULL`，不由前端指定。

原型的 SSL Mode 下拉框可列“未指定”以及 `disable`、`allow`、`prefer`、`require`、`verify-ca`、`verify-full`；**“未指定”是初始选项，对应 `null`，不在后端补 `disable` 或其他默认值**。用户选择的值原样作为单独 `sslmode` 参数提交、保存并用于连接；后端不新增模式白名单，不把字段文本拼进 conninfo。`verify-*` 是否能连接取决于源库和部署环境，失败时显示脱敏后的连接错误，不在本阶段开发证书管理。

连接信息初始为空，原型中的地址、账号、密码和“连接成功”仅是示意，不作真实默认值。测试连接前，Schema 区域保留位置并提示先测试连接；测试中禁用重复点击，成功后显示实际数据库版本及可访问的 Schema 名称，失败则显示脱敏后的错误，不保存记录。Schema 可多选，至少选一项才能创建。原型中 Schema 旁的“几张表/视图”若本次连接检查接口不返回可靠计数，就先显示 `—` 或隐藏数字，不能写死示例统计。

任何会影响连接结果的字段（host、port、db_name、username、password、sslmode）变化时，前端使原连接检查结果失效并清空已选 Schema，要求重新测试。保存时后端仍重新连接并校验所选 Schema，不能仅信任浏览器先前的“连接成功”。提交中禁用重复保存；保存失败保留表单与选项，成功后跳转数据资产页并显示新记录。数据资产页对列表加载中、加载失败、空列表分别提供可辨状态。

## 4. 数据模型差异：可选说明

创建页原型有“数据源说明（可选）”，而 doc 3 已创建的 `public.db_source_info` 及现有 `backend.persistence.models.DbSourceInfo` **没有**该字段。为了让页面输入真正保存，本次在现有表上增加可空 `description text`，并同步 ORM、创建请求和列表响应。该字段只保存管理员填写的文字，不参与连接、Schema 校验或 `--step db`；旧记录保持 `NULL`，页面显示为空。数据库列不加长度 `CHECK`；去除首尾空白后最多 1000 字符的规则由本阶段 API 写入入口统一执行，并在请求模型测试中覆盖，不能误称数据库本身保证了此上限。将来新增 `PATCH` 等写入入口时须复用同一规则。

平台表继续由管理员确认目标是平台库 `metaweave` 后**手工执行**变更，不引入 Alembic，也不由 API 启动时自动改表：

```sql
ALTER TABLE public.db_source_info
    ADD COLUMN IF NOT EXISTS description text;
```

执行前需先检查当前表结构；`IF NOT EXISTS` 仅防止重复添加同名列，不代表现有同名列类型一定正确。此处是 doc 2/doc 3 首版表结构之后的**明确增量**，不需要重建数据源表；doc 2 的字段总表已同步列出该列，本次实施以本文增量为准。

## 5. FastAPI 接口与后端复用

本次只实现三类接口；路径延续 doc 2 §3.2，`PATCH` 修改功能后置。

| 接口 | 请求与响应要点 |
| --- | --- |
| `POST /api/data-sources/connection-check` | 接收 `db_type`、`host`、`port`、`db_name`、`username`、`password`、可空 `sslmode`；只支持 PostgreSQL，其他 `db_type` 在建连前返回 `422 invalid_request`。使用提交的 `host`、`port` 连接源库，读取 `server_version_num` 并校验 PostgreSQL 15.0 最低版本；通过后才返回 `server_version`（如 `"15.6"`）和可访问的业务 Schema 名称，不创建数据源、不持久化密码。 |
| `POST /api/data-sources` | 接收上述连接字段、`display_name`、非空 `selected_schemas`、可空 `description`；其他 `db_type` 同样在建连前返回 `422 invalid_request`。服务端使用提交的 `host`、`port` 重新连接，再次校验版本、Schema 存在性与 `USAGE` 权限，通过后才加密密码并插入 `db_source_info`。成功响应返回服务端生成的 `source_id`，字段集合与下方列表项完全相同，前端复用同一类型。 |
| `GET /api/data-sources` | 返回已登记的数据源列表，按 `created_at` 降序、`source_id` 稳定补序；每项仅包含 `source_id`、`display_name`、`db_type`、`host`、`port`、`db_name`、`username`、`sslmode`、`selected_schemas`、`description`、`db_version`、`created_at`，不包含密码、密文、平台凭据或密钥。`sslmode` 与 `db_version` 由 [doc 6](6_数据源版本字段与修改连接设计.md) 加入列表项；`db_version` 在该列添加前的旧行上为 `null`。`created_at` 统一转换为 UTC、以 `Z` 结尾的 RFC 3339 字符串，固定六位小数（如 `2026-09-22T09:30:00.000000Z`），与全平台时间契约一致（doc 19.3 §3）；创建成功响应沿用同一格式。`host`、`port`、`username` 属于管理员可见信息，本接口和对应页面仅供管理员使用；本阶段无认证时只允许本机访问。首版数据量小，可直接返回列表；未来需要分页时再扩展契约。 |

连接检查和创建须复用 doc 3 的 `build_connect_kwargs(...)`（doc 18.1 更名）、连接/语句超时、共享的 `read_and_validate_server_version(conn)`、Schema 发现与校验函数、`encrypt_password(...)` 及平台库访问代码，不在 API 内复制第二套连接串拼装、版本门槛或加密算法。`host`、`port` 原样作为独立连接参数传给 psycopg；域名由 libpq 解析，本阶段不增加服务端 DNS 预解析、目标 IP 禁止清单或地址固定逻辑。连接检查执行 `SHOW server_version_num` 后，先按共享门槛 `MIN_POSTGRESQL_VERSION_NUM = 150000` 校验；版本无效或低于 PostgreSQL 15.0 时立即返回版本不支持错误，不返回“连接成功”或 Schema 选项。`server_version` 复用本次从 `db_step.py` 抽出的 `format_server_version(server_version_num)`，与 `database.json` 的 `database.server_version` 使用同一格式，不另写转换逻辑或再查一次版本；连接检查接口本身不写库；创建和修改保存时写入 `db_version`。正式画像版本仍由未来 `--step db` 采集。

Schema 发现与校验统一放在 `metaweave/core/metadata/db_step.py`，与现有 `validate_schema_names()`、`validate_selected_schemas(conn, names)` 同处一模块；本次新增 `discover_selectable_schemas(conn) -> list[str]`。三者共用一份可选范围规则：固定排除集合 `SYSTEM_SCHEMA_NAMES = {"pg_catalog", "information_schema", "pg_toast"}` 和临时 Schema 前缀 `TEMP_SCHEMA_PREFIXES = ("pg_temp_", "pg_toast_temp_")`；系统/临时 Schema 不得出现在连接检查结果，也不得通过直接提交的创建请求。发现和校验均从 `pg_catalog.pg_namespace` 查询，按 OID 使用 `has_schema_privilege(oid, 'USAGE')`，共用同一内部查询/判定函数；发现只返回存在、有 `USAGE` 权限且可选的名称，按名称升序。`validate_selected_schemas()` 仍先用 `validate_schema_names()` 保留原始名称并拒绝空值、重复项，再逐项区分“不存在”“无 `USAGE` 权限”“系统/临时 Schema 不可选”；合法但没有目标对象的 Schema 仍可选。不能从发现结果是否包含名称来推断“不存在”，否则无权限会被误报为不存在。创建接口、手工登记工具和 `--step db` 复用修订后的校验函数；已登记但选中了系统/临时 Schema 的旧记录在后续采集时须给出明确错误，由管理员修正登记范围。

按 doc 2 的边界，连接检查与创建只供企业管理员使用；无登录系统的本地开发阶段，FastAPI **仅绑定本机**。本阶段信任管理员提供的 PostgreSQL 连接地址，不在应用层筛选可连接的 IP、预解析域名或实现多地址故障转移。连接失败按源库连接失败处理，不能把失败伪装成已登记成功；超时和凭据保护仍按 doc 3 执行。以后若管理 API 面向更广的调用者，再单独评估网络访问控制，不在这次 Web 登记功能里预设复杂地址策略。

连接检查与创建是两个独立请求：创建时重新连接并校验版本与所选 Schema，不能只信任先前的“连接成功”结果。后续 `run_db_step()` 使用登记表中的 `host`、`port` 独立连接和采集；不能因为数据源曾成功登记就跳过源库前置检查。源库建连与查询沿用 doc 3 的超时。错误响应不得包含密码、密文、完整连接参数或未脱敏的驱动异常。前端只显示后端返回的安全错误摘要。

本阶段 API 仅绑定本机供开发使用，不实现应用层并发限流，也不以限流为由要求固定 Uvicorn worker 数。连接检查与创建各自打开源库连接，用完即关闭；建连和查询仍遵守 doc 3 的超时。以后扩大使用范围时，再依据实际并发量和源库容量决定是否需要限流或队列。

创建接口的顺序固定为：先校验请求字段的格式，再按 `(host, port, db_name, username)` 查询平台库中的可能重复记录；若存在且 `confirm_duplicate` 未设为 `true`，直接返回 `409 duplicate_source`，并在 `details` 数组中列出所有匹配记录的 `source_id` / `display_name`，**不先连接源库**。用户确认后重新提交完整请求，后端重新查重，再连接源库、校验版本及所选 Schema，最后加密密码并插入 `db_source_info`。这与 doc 3 手工登记工具的“先查重、再连接”一致；确认重复仅允许继续验证和登记，不能跳过源库校验。首版不在数据库上增加该四元组唯一约束，因为同一库/账号可以有不同登记用途；查重是提示而非并发唯一性保证。重复提交按钮应在请求期间禁用，避免一次点击产生两条记录。

连接检查和创建的密码只放在 POST 请求体中，不能放入 URL、查询参数、访问日志或异常文本。Uvicorn 默认访问日志不记录请求体，但本项目不得启用记录请求体的中间件、调试日志或异常处理器；记录失败时只写脱敏后的错误类型、请求路径和必要的非敏感定位信息，不记录原始请求模型、完整连接串、明文密码或密文。若将来接入统一日志/追踪组件，同样遵守这一边界。

接口最小状态契约：创建成功返回 `201`；连接检查成功返回 `server_version` 与 `schemas: ["public", "sales"]`（按名称升序的字符串数组，不包装成 `{name}` 对象），不返回未经采集的表/视图计数；低于 PostgreSQL 15.0 或版本号不可解析时返回 `422`、错误码 `unsupported_postgresql_version` 和“仅支持 PostgreSQL 15.0 及以上”的安全提示，能解析时可附实际版本展示字符串。前端显示该错误，并保持 Schema 选择和创建按钮不可用；后端创建接口也重新校验，低版本绝不入库。表单或 Schema 校验失败返回可安全展示的 `4xx` 错误。创建请求可带 `confirm_duplicate: false`（缺省）；发现上述四元组重复时返回 `409`、错误码 `duplicate_source`，附带 §6.1 约定的 `details` 数组，用户确认后以 `confirm_duplicate: true` 重试，服务端仍重新校验连接、版本及 Schema。连接、平台库等异常须归类为失败响应，不返回明文驱动异常或假装登记成功。`422` 同时用于请求字段不合法（`invalid_request`）和版本不支持等业务校验失败；前端必须依据响应 `code` 区分具体错误，不能只按 HTTP 状态码显示同一提示或决定是否允许创建。

## 6. 实施文件与模块设计

本节列的是**本次应新增或修改的文件**，不要求照原型 HTML 的文件结构搬运。依赖方向固定为 `backend.api`（HTTP、请求/响应、状态码）→ `backend.data_sources`（业务编排）→ `metaweave.core.metadata` / `backend.persistence` / `backend.security`（共享能力）。API 路由不直接导入 psycopg、SQLAlchemy 或执行 SQL；业务服务不导入 `backend.api`、不抛 HTTP 异常或决定状态码；底层校验、连接与仓储层不导入 API。请求模型在路由边界转换为不依赖 Pydantic 的服务入参，不能把 API 模型传入共享层。现有 `backend/persistence/platform_db.py` 的 `load_project_env()` 保留供入口调用，但服务函数和共享校验函数不自行读取 `.env` 文件：Web 应用创建时显式加载项目根 `.env` 并读取平台库设置，手工登记与独立 CLI 保持各自的入口加载方式。

唯一的轻量例外是 API 请求模型直接复用 `metaweave.core.metadata.db_step.validate_schema_names()` 这一纯校验函数，用于在请求入口尽早拒绝空值和重复 Schema；服务层仍独立再校验一次。该例外不涉及数据库访问，也不改变上述业务编排的依赖方向。

### 6.1 后端新增文件

| 文件 | 函数/职责与边界 |
| --- | --- |
| `backend/api/__init__.py`、`backend/api/routers/__init__.py` | 空包初始化；不在包导入时提前加载 FastAPI、平台库或凭据。现有 `backend/__init__.py`、`backend/data_sources/__init__.py` 不重建。 |
| `backend/api/main.py` | `create_app(settings: PlatformSettings | None = None, cors_origins: Sequence[str] | None = None) -> FastAPI`：正常 Web 入口加载项目 `.env`，从中读取平台库设置及 `PLATFORM_CORS_ORIGINS`；注入 `settings` 可隔离平台库配置，注入 `cors_origins=[]` 可隔离本机 CORS 配置，`cors_origins=None` 则读取环境变量。调用共享校验并注册 FastAPI `CORSMiddleware`，同时注册数据源路由和安全异常处理器；启动时不连接平台库做只读可达性/表存在性自检，也不自动创建或修改平台表；平台库故障由实际请求返回安全错误，联调前按 §7 确认表已就绪。开发启动示例：`uv run --extra platform uvicorn backend.api.logged_app:create_logged_app --factory --no-access-log --host 127.0.0.1 --port 8001`（doc 15 部署专用工厂，写 `logs/api.log` 与 `logs/backend/*.log`；继续使用旧入口 `backend.api.main:create_app` 不会自动得到这些项目文件日志）。 |
| `backend/api/cors.py` | `validate_cors_origin()`、`parse_cors_origins()`：校验规范形式的前端 origin，解析逗号分隔的 `PLATFORM_CORS_ORIGINS`；只做纯解析与校验，不读取 `.env`，不创建 FastAPI 应用。 |
| `backend/api/schemas.py` | Pydantic v2 请求/响应模型：`ConnectionFields` 集中声明 `db_type`、`host`、`port`、`db_name`、`username`、`password`、`sslmode` 七个连接字段及共用校验；`ConnectionCheckRequest(ConnectionFields)` 与 `DataSourceCreateRequest(ConnectionFields)` 分别用于两个入口，后者补 `display_name`、`selected_schemas`、`description`、`confirm_duplicate=false`，不重复声明连接字段。`ConnectionCheckResponse` 使用 `server_version`、`schemas: list[str]`；另有 `DataSourceListItem` / `DataSourceListResponse`、`ErrorResponse(code, message, details?)`。`details` 只用于 `duplicate_source`，形状固定为 `[{source_id: UUID 字符串, display_name: 字符串}]`，其他错误响应省略该字段。创建成功响应直接使用 `DataSourceListItem`，不另定义一套登记字段。连接类请求设置 `extra='forbid'`，拒绝约定外字段；密码字段在模型表示中隐藏，说明字段按 §3 规范化和限长；所有响应模型只列安全字段。 |
| `backend/api/errors.py` | API 层唯一的业务异常→安全响应映射；可用 `ApiError(code, message, status)` 封装结果，但服务层只抛自身的领域异常。覆盖 Pydantic 请求校验、重复登记、低版本、Schema 不存在/无权限/不可选、源库连接失败、平台库不可用和未知异常；处理 `RequestValidationError` 时只输出预定义摘要，不序列化其中可能含密码的输入值，也不回显原始 psycopg 异常或连接参数。 |
| `backend/api/routers/data_sources.py` | 在本模块定义 `get_platform_settings(request)`，从应用状态取得已加载的 `PlatformSettings`，通过 `Depends` 供路由使用；不在每个请求里重读 `.env`，测试可替换此依赖。三个同步 `def` 路由：`connection-check`、创建、列表。接收已校验请求模型和平台库设置，转换为服务入参，调用服务并返回响应；不直接建源库连接、不调用仓储 SQL、不自行管理信号量。只有其他路由也需要该依赖时，才考虑抽出独立模块。 |
| `backend/data_sources/connection_check.py` | 不依赖 HTTP 的 `SourceConnectionInput`（密码字段不参与 `repr`）、`SourceConnectionFailedError`、`open_source_connection(fields)` 上下文管理器及 `check_connection(fields)`。上下文执行：共享 `build_connect_kwargs(host=提交的地址, port=提交的端口, ...)`（doc 18.1 更名）→ psycopg 建连 → 共享版本校验 → 向调用方提供连接与版本；离开时关闭连接。连接/驱动错误只转成不含凭据的领域异常。`check_connection()` 在同一上下文内调用 `discover_selectable_schemas()`。 |
| `backend/data_sources/registration_service.py` | 不依赖 HTTP 的 `CreateSourceInput`、`DuplicateSourceError`、`PlatformUnavailableError`、`create_data_source(settings, fields)` 和 `list_registered_sources(settings)`。`CreateSourceInput` 至少携带连接字段、`display_name`、`selected_schemas`、`description` 和布尔值 `confirm_duplicate`（缺省 `false`，不持久化）；路由把请求模型中的确认值原样传给服务，**查重与是否返回重复错误均由服务层决定**。创建先短暂打开平台库会话查重，未确认即抛 `DuplicateSourceError`，**关闭该会话后**才调用上述同一个 `open_source_connection()`；在连接仍打开时校验所选 Schema，再关闭源库连接、加密密码，另开短会话插入并提交。列表只查平台库，不连接源库。服务层不读 `.env`，不返回密码或 ORM 整对象。 |

连接检查与创建复用 `connection_check.py` 的 `open_source_connection()`，确保连接参数、超时、版本校验和关闭行为一致。业务服务抛 `DuplicateSourceError` 等领域异常，由 `backend/api/errors.py` 转为 HTTP 状态。平台库查重与源库连接不跨同一个长事务；当前四元组查重是提示，不提供数据库级唯一性保证。

`open_source_connection()` 对 `db_type` 的 PostgreSQL 限定保留为服务层防御：当前 HTTP 请求已由 Pydantic `Literal["postgresql"]` 提前拦截，但未来非 HTTP 调用者不能绕过这一限制。

错误映射的最低契约：重复登记 `409 duplicate_source`；低版本 `422 unsupported_postgresql_version`；Schema 不存在/无权限/不可选分别为 `422 schema_missing` / `schema_no_usage` / `schema_not_selectable`；字段不合法为 `422 invalid_request`；源库连接失败为 `422 connection_failed`；平台库不可用为 `503 platform_unavailable`；未预期异常及源库凭据加密密钥缺失/无效等服务端配置问题为 `500 internal_error`，不得误报为平台库故障。这里的 `422` 统指“请求可解析，但字段/业务校验或本次指定的源库连接未通过”，并非只表示 Pydantic 请求校验失败；错误密码、源库不可达等归 `connection_failed`，平台库不可用仍单列为 `503`。普通错误响应仅含 `{code, message}`；只有 `duplicate_source` 另带非空 `details: [{source_id, display_name}]`，列出所有同四元组匹配记录，不返回 host、用户名、Schema、密码或 ORM 整对象。`source_id` 为规范 UUID 字符串；其他 `code` 不带 `details`（不返回 `null` 或空数组）。响应只写预定义的安全文案；请求模型校验错误也转换为此结构，前端不解析原始驱动错误文本，必须按 `code` 而非仅按状态码分支。

版本及 Schema 校验统一新增 `DbProfileValidationError` 子类：`UnsupportedServerVersionError`（版本过低或不可解析）、`SchemaMissingError`（不存在）、`SchemaNoUsageError`（无 `USAGE` 权限）、`SchemaNotSelectableError`（系统/临时 Schema 不可选）。原有中文提示保持不变，CLI 继续捕获基类；API 按子类映射上述安全错误码，**不得解析中文异常字符串**。只有源库连接/驱动错误归为 `connection_failed`，不得把这些校验异常包装成连接失败。单测分别断言异常类型及 API 错误码。

### 6.2 后端既有文件与配置改动

| 文件 | 本次改动 |
| --- | --- |
| `metaweave/core/metadata/db_step.py` | 将现有 `_check_server_version()` 的 `> 16.0` 门槛改为 `MIN_POSTGRESQL_VERSION_NUM = 150000`，抽出 `validate_server_version_num()`、`read_and_validate_server_version()` 供 Web、CLI、登记工具共用；同步将旧错误文案改为“要求源库 PostgreSQL 15.0 及以上”。把现有画像内联的版本字符串转换抽为 `format_server_version(server_version_num) -> str`，由画像与连接检查共用。增加 `discover_selectable_schemas()`、`SYSTEM_SCHEMA_NAMES`、`TEMP_SCHEMA_PREFIXES`，并使 `validate_selected_schemas()` 拒绝系统/临时 Schema、与发现共享查询规则。新生成的 `database.json` 继续包含 `database.server_version`；旧 `schema_version: 1` 文件若缺该字段，后续读取方按“版本未知”处理，不伪造当前版本（沿用 doc 3 §4）。 |
| `metaweave/core/metadata/connector.py` | 复用 doc 3 的公共参数构造 `build_connect_kwargs(...)`（doc 18.1：由 `build_source_connect_kwargs(...)` 更名，宿主迁至 `metaweave/db/postgres_options.py`，参数名改为 dbname/user、两个超时可选）：把传入的 `host`、`port` 等字段作为独立连接参数返回；Web 入口直接复用，不添加额外地址解析参数。 |
| `scripts/register_db_source.py` | 改用共享版本函数和修订后的 Schema 校验；保留先查重、后连接的流程，仍用 doc 3 的共享连接参数构造函数。 |
| `backend/data_sources/db_step_service.py`、`metaweave/core/metadata/db_step.py` | `run_db_step()` 和核心 `execute_db()` 沿用登记表中的 `host`、`port`；本阶段不为采集请求增加网络目标解析字段或改造现有建连流程。 |
| `backend/persistence/models.py`、`backend/data_sources/repository.py` | ORM 增加可空 `description`；`insert_db_source()` 支持该字段；新增 `list_db_sources(session)`，按 `created_at DESC, source_id` 稳定排序。仍复用既有 `find_db_sources_by_connection()`；列表响应在服务层白名单投影，不暴露 `password_ciphertext`。 |
| `pyproject.toml`、`uv.lock` | `platform` extra 按 §2 增加 `fastapi>=0.115.9`、`uvicorn[standard]>=0.30.6`、`pydantic>=2.0`，`dev` extra 增加 `TestClient` 所需的 `httpx`，并更新锁文件；沿用现有 SQLAlchemy、cryptography，不把 Web 依赖放进基础 CLI 依赖。 |
| `.gitignore` | 增加 `frontend/node_modules/`、`frontend/dist/`、`frontend/.vite/`、前端本地环境文件和 `*:Zone.Identifier`；不忽略前端依赖锁文件或示例环境文件。 |

平台表的 `description` 增量 SQL 按 §4 由管理员在确认目标平台库后手工执行；以上文件改动不能悄悄在 API 启动时迁移表。连接参数构造继续复用 doc 3 的既有函数，不扩展其签名。

手工登记工具是开发期入口：查重后关闭平台库会话，源库校验通过后另开会话插入；不在连接源库时长期占用平台库会话。工具没有说明字段输入，登记结果的 `description` 固定为 `NULL`，不影响 Web 创建页填写说明。

### 6.3 前端文件

本次以 npm 管理 `frontend/`，提交 `package.json` 与 `package-lock.json`；`vite.config.ts` 将前端开发端口固定为 `5173`（占用时直接报错），不再配置 `/api` 代理。前端须在开发启动与生产构建前提供 `VITE_API_BASE_URL`；后端端口改变时修改该地址并重启或重新构建。最小文件清单如下：

| 文件 | 职责 |
| --- | --- |
| `frontend/package.json`、`package-lock.json`、`tsconfig.json`、`index.html`、`vite.config.ts`、`.env.example` | Vue/TypeScript/Vite 构建与依赖锁定；校验浏览器可访问的 `VITE_API_BASE_URL`，无 Vite `/api` 代理；示例配置不包含源库凭据。 |
| `frontend/src/main.ts`、`frontend/src/App.vue`、`frontend/src/router/index.ts` | 挂载应用、全局布局和 `/`、`/data-assets`、`/data-assets/new`、`/data-assets/:sourceId/edit`、`/data-assets/:sourceId`。 |
| `frontend/src/api/types.ts`、`apiBaseUrl.ts`、`http.ts`、`dataSources.ts` | 请求/响应类型；`types.ts` 文件头注明“字段与 `backend/api/schemas.py` 对应，改动需同步”，本阶段不引入类型代码生成；连接检查响应的 `schemas` 定义为 `string[]`，直接供 Schema 多选组件使用；对 `duplicate_source` 的 `details: Array<{source_id: string; display_name: string}>` 明确建模，其他错误不依赖 `details`。`apiBaseUrl.ts` 校验前端 API 地址，`http.ts` 将 `/api/...` 路径与该地址组合并提供 `apiRequest<T>()` 和安全错误码到中文文案的映射；`dataSources.ts` 提供 `checkConnection()`、`createDataSource()`、`listDataSources()`。密码仅在本次 POST 请求内传输，不写浏览器持久化存储。 |
| `frontend/src/components/AppSidebar.vue`、`DataSourceForm.vue`、`SchemaSelector.vue`、`PlaceholderPanel.vue` | 侧栏、连接表单、Schema 多选，以及未开放模块的明确占位；连接字段变化时清除旧测试结果和 Schema 选择。 |
| `frontend/src/views/HomeView.vue`、`DataAssetsView.vue`、`CreateDataSourceView.vue`、`DataSourceDetailView.vue` | 对照三份原型完成导航、登记列表/空状态、连接检查与创建流程；列表数据只来自平台 API，不从画像文件或原型常量读取。`DataSourceDetailView.vue` 为数据源只读页。提交前执行前端类型检查与生产构建，确认路由和组件均可编译。 |

### 6.4 测试落点与三条调用链

| 测试文件 | 必须覆盖 |
| --- | --- |
| `tests/unit/test_db_step.py`（修改） | PostgreSQL 15.0 门槛矩阵及新错误文案、共享版本函数与 `format_server_version()` 的转换样例、Schema 发现与系统/临时排除、`database.server_version` 保留、失败不清理旧画像；保留 `test_connection_kwargs_keep_values_separate` 与 `test_registration_uses_shared_connection_builder`，确认域名或 IP 的 `host`、`port` 原样传入共享构造函数，密码和 `sslmode` 仍作为独立参数。 |
| `tests/unit/test_web_data_sources.py`（新增） | FastAPI `TestClient` 加假平台库/源库：成功 `200/201`、重复 `409` 且不建连、版本/Schema/未知键拒绝、列表排序与敏感信息隔离、说明字段、错误和日志不泄露密码；用同四元组匹配多条记录验证 `duplicate_source.details` 完整且只含 `source_id` / `display_name`，其他错误响应不带 `details`。断言非 PostgreSQL `db_type` 在建连前返回 `422 invalid_request`，连接检查的 `server_version` 与画像转换结果一致，创建成功响应与列表项字段完全相同，二者 `created_at` 均为 UTC `Z` 结尾、六位小数；断言 `RequestValidationError` 不回显密码值，并用同为 `422` 的 `invalid_request` 与 `unsupported_postgresql_version` 验证前端所需的不同 `code`。测试连接成功及异常时均关闭源库连接；列表响应逐字段断言 §5 白名单，不返回凭据。另以真实 PostgreSQL 完成 §7 的联调，不让 mock 结果代替真实建连。 |

测试替身边界：业务流程用例只替换源库连接、平台库会话、`.env`/设置来源等外部依赖，让真实的 `check_connection()`、`create_data_source()` 与共享校验函数执行；通过替身提供的查询结果触发对应异常，并断言 `schemas` 是按名称升序的字符串数组。单独测试 HTTP 异常映射时，可以让服务替身抛出真实领域异常子类（如 `SchemaNoUsageError`），但不得直接构造预期响应来证明业务流程正确。两类用例分别覆盖，避免“因其他错误得到同一状态码”而误通过。

连接检查：`router → check_connection() → open_source_connection()〔共享参数构造 → 建连 → 版本校验〕→ discover_selectable_schemas() → 200`。

创建：`router → create_data_source() → 平台库查重〔未确认则 409〕→ open_source_connection()〔同一连接链〕→ validate_selected_schemas() → encrypt_password() → insert_db_source() → 201`。

列表：`router → list_registered_sources() → list_db_sources() → 200`（只读平台库，不连接源库）。

## 7. 开发顺序与验收

1. **先修正已运行的版本门槛和 Schema 共享层（已完成）**：实施前 `metaweave/core/metadata/db_step.py` 中 `_check_server_version()` 按 `version <= 160000` 拒绝，`tests/unit/test_db_step.py` 也把 `150000` 和 `160000` 当成失败。实施时将门槛改为 `MIN_POSTGRESQL_VERSION_NUM = 150000`，抽出 §5 约定的 `validate_server_version_num()` / `read_and_validate_server_version()`；`execute_db()` 和 `scripts/register_db_source.py` 共用它们，使 PostgreSQL 15.0 可以采集和登记，14.x 在清理或入库前失败，错误文案同步改成“要求源库 PostgreSQL 15.0 及以上”。同时在 `db_step.py` 增加 `discover_selectable_schemas()` 和共用的系统/临时 Schema 排除规则，修订 `validate_selected_schemas()`，让发现、创建、手工登记与采集使用一致的可选范围。旧版本测试已同步，覆盖 `150000`、15.x、16.0、17.x、18.x 成功及 14.x、无效版本失败；保留失败时不清理旧 `database.json` 的断言。该项完成后才接入 Web API。
2. 确认平台库 `db_source_info` 已存在，人工添加 `description` 列；更新 SQLAlchemy 模型和仓储层的列表查询。保留现有数据和 `source_id`。
3. 实现并测试 FastAPI 的连接检查、创建、列表三接口；扩充 `platform` extra 的 Web 依赖。同步 `def` 路由调用现有阻塞式数据访问，连接检查及创建调用第 1 项的同一版本函数，并在各自请求结束时关闭源库连接。先以真实 PostgreSQL 测通“测试连接 → 版本与 Schema 校验 → 加密入库 → 列表读取”，再接页面。未接入认证的开发服务须显式以 `--host 127.0.0.1` 启动，不使用 `0.0.0.0`；本阶段不规定 worker 数。
4. 先按 §2 补齐 `.gitignore` 并检查原型目录不含待提交的 `Zone.Identifier` 文件，再在 `frontend/` 初始化 Vue 3 + TypeScript + Vite，按三份原型实现公共布局、首页导航、数据资产空状态/列表、创建页表单与连接测试。
5. 联调成功、失败与返回路径；在数据资产页区分“已登记”和未来的“已采集/已发布”，不读取 `output/<source_id>/db/database.json` 来伪造登记列表。

最低验收：

- 从首页只通过可用的“数据资产”入口进入资产页；空列表可到创建页；创建后回到资产页并看见真实登记记录，刷新页面后仍存在。
- 测试连接成功返回实际版本和可访问 Schema；错误密码、不可达地址、无可选 Schema 有明确状态，不创建记录；连接字段变化使旧 Schema 选择失效。
- 连接检查与创建在成功、校验失败或连接异常后均关闭源库连接；列表请求不连接源库。同步阻塞操作不得直接运行在 `async def` 路由的事件循环中。本阶段不实现应用层并发限流，也不要求固定 Uvicorn worker 数。
- PostgreSQL 14.x 的连接检查返回版本不支持，页面不允许选 Schema 或创建；直接调用创建接口、手工登记工具也不插入。`server_version_num` 为 `150000`、15.x、`160000`、17.x、18.x 时沿用同一共享校验并可进入 Schema 发现和登记，现有 `--step db` 对这些版本也能完成采集；Web 与 CLI 对同一版本给出一致结论。
- 域名或 IP 输入均作为原始 `host` 连同 `port` 传给共享连接参数构造函数，由 libpq 建连；连接检查、创建与后续采集使用同一构造规则，不在应用层预解析 DNS、筛选目标地址或改写 `host`。创建必须独立建连并重验版本与 Schema，不复用连接检查的结果。
- 不选 Schema、选了不存在或无 `USAGE` 权限的 Schema、非法端口或缺必填字段均不能入库；`sales` 与 `sales ` 保持可区分。
- 模拟 `pg_catalog`、`information_schema`、`pg_toast`、`pg_temp_*`、`pg_toast_temp_*`（含当前会话临时 Schema）：发现结果不包含它们，直接提交也被拒绝；缺权限的业务 Schema 不出现在列表且直接提交时报“无权限”，不存在的 Schema 报“不存在”。连接检查与创建调用同一模块的 Schema 查询和排除规则；合法空 Schema 可以登记。
- `sslmode` 未选择时数据库列为 `NULL` 且连接参数不带该键；选择后连接测试和保存使用同一值。`description` 首尾空白去除后最多 1000 字符，纯空白保存为 `NULL`、超长请求被拒绝；列表接口可读回非空说明，页面以纯文本展示。
- 已存在相同 `(host, port, db_name, username)` 时，未确认的创建请求在源库建连前返回 `409`；确认后仍重新查重并完成连接、版本及 Schema 校验，不能只凭确认直接入库。
- 接口、页面、访问日志、应用日志和异常中都不返回源库明文密码、密文或密钥；测试密码包含独特标记时，连接检查与创建的成功/失败日志、异常响应均不含该标记或完整请求体，库中只存密码密文。未认证的开发服务以 `127.0.0.1` 而非 `0.0.0.0` 监听；启动后用 `ss -ltn` 核对实际监听套接字，不得出现该服务绑定 `0.0.0.0` 或其他非本机地址。环境允许时，再从另一主机向本机局域网地址请求管理 API 验证不可访问；这一补充检查不作为每个开发环境的必需验收项。
- 提交前确认 `.gitignore` 忽略前端依赖/构建产物与 `*:Zone.Identifier`，三个现有 ADS 残留文件不入库，前端依赖锁文件正常入库。
- 数据资产页不展示原型里的假数据源、假表、假行数或假数据库大小；采集、发布、作业及画像展示均不属于本次功能验收。
