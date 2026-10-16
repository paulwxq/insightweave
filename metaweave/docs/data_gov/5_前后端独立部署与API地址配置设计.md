# 前后端独立部署与 API 地址配置设计

> 状态：代码与自动化测试已实施，浏览器手工联调待完成。本文承接 [doc 4：阶段一 Web UI 添加数据源](4_阶段一webui添加数据源.md)，只修订前端访问 FastAPI 的地址配置和后端跨域响应；数据源接口、页面、平台表及凭据规则保持不变。新的连接方式已取代 doc 4 中“前端通过 Vite `/api` 代理访问后端”的旧约定。

## 1. 目标与范围

前端 Vue 与后端 FastAPI 继续作为**两个独立进程/部署单元**运行。浏览器按照前端构建时配置的后端地址，直接请求 FastAPI；开发环境不再依赖 Vite 代理。当前后端运行端口为 `8001`，前端开发端口固定为 `5173`。

本次完成：前端 API 地址配置、统一请求入口、FastAPI CORS 配置、配置样例、测试及启动说明。不修改现有 `/api/data-sources` 接口路径或响应结构，不引入新的前端状态库、反向代理、身份认证、运行时配置服务或部署平台。配置了 API 地址与 CORS **不等于**已具备对公网开放管理接口的安全条件；在认证和正式部署方案完成前，后端仍按 doc 4 仅监听本机。

## 2. 当前实现与要消除的依赖

实施前，[前端请求模块](../../frontend/src/api/http.ts) 使用 `fetch('/api/...')`；[Vite 配置](../../frontend/vite.config.ts) 仅在开发服务器运行时把 `/api` 代理到 `http://127.0.0.1:8001`。当时开发环境能调用独立 FastAPI，但 `npm run build` 生成的静态文件本身不含这套代理；若静态文件由另一台 Web 服务器单独提供，浏览器仍会向**前端所在地址**请求 `/api`，除非部署方另配反向代理。

现已改为以下唯一调用链，开发和构建后的页面使用同一规则：

```text
浏览器中的 Vue 页面
  → VITE_API_BASE_URL + /api/data-sources/...
  → 独立运行的 FastAPI
  → 平台库 / 源库
```

`VITE_API_BASE_URL` 是**浏览器能够访问的后端地址**，不是前端服务器访问后端的内部地址。本机开发示例为 `http://127.0.0.1:8001`；以后若从其他电脑打开页面，不能把这台电脑自己的 `127.0.0.1` 当成后端服务器地址。

## 3. 配置契约

### 3.1 前端：`VITE_API_BASE_URL`

| 项目 | 约定 |
| --- | --- |
| 配置名 | `VITE_API_BASE_URL`，例如 `http://127.0.0.1:8001`；生产构建示例为 `https://api.example.com`。示例域名仅用于说明，不是项目默认部署地址。 |
| 值的范围 | 只填写 `http`/`https` 的 origin（协议、主机、可选端口），**不带**末尾 `/`、`/api`、其他路径、查询参数或片段。已有请求路径继续以 `/api/...` 开头。 |
| 缺失或非法 | 前端开发启动/构建时明确报配置错误，不静默回退到同源 `/api`、旧端口 `8000` 或任意本机地址。 |
| 本地配置文件 | 提交 `frontend/.env.example`，包含本机示例；开发者复制为被 Git 忽略的 `frontend/.env.development.local`，按实际后端地址修改。前端实际配置与根目录供后端使用的 `.env` 分开。 |
| 构建部署 | 部署方在 `npm run build` **之前**提供 `VITE_API_BASE_URL`（环境变量或 Vite 的 production 模式 env 文件）。Vite 将其写入构建产物；地址改变后须重新构建，本次不设计“改静态文件旁的配置而无需重建”的运行时机制。 |
| 敏感信息 | `VITE_` 前缀变量可在浏览器代码中看到，只能放公开的 API 地址，禁止放平台库密码、源库密码、API Key 或密钥。 |

前端只保留一处地址拼接：`frontend/src/api/apiBaseUrl.ts` 提供纯地址校验函数，供 `frontend/src/api/http.ts` 和 `frontend/vite.config.ts` 共用；`http.ts` 读取 `import.meta.env.VITE_API_BASE_URL`，将现有绝对路径 `/api/...` 拼接为完整 URL；`frontend/src/api/dataSources.ts` 等业务模块继续只传 API 路径，不各自读取环境变量。`vite.config.ts` 删除 `/api` 的 `server.proxy`，保留 `port=5173` 和 `strictPort=true`；本机绑定 `127.0.0.1` 当前由 `frontend/package.json` 的 `dev` 脚本通过 `vite --host 127.0.0.1` 指定，继续保留在该脚本中。Vite 配置阶段使用 `loadEnv()` 读取对应 mode，不能假定 `.env.*` 已自动进入 `vite.config.ts` 的 `process.env`；为 `process.cwd()` 增加开发类型依赖 `@types/node`。[Vite 官方环境变量说明](https://vite.dev/guide/env-and-mode)、[Vite 配置说明](https://vite.dev/config/)。

### 3.2 后端：`PLATFORM_CORS_ORIGINS`

前后端端口不同，即使主机都叫 `127.0.0.1`，浏览器也将它们视为不同 origin。FastAPI 须用 `CORSMiddleware` 允许明确列出的前端 origin；CORS 仅控制浏览器跨域读取，不是用户认证。[FastAPI 官方 CORS 说明](https://fastapi.tiangolo.com/tutorial/cors/)。

| 项目 | 约定 |
| --- | --- |
| 配置名 | 根目录 `.env` 中的 `PLATFORM_CORS_ORIGINS`，用逗号分隔**前端页面的 origin**，不是后端 API 地址。本机开发填写 `http://127.0.0.1:5173`；若确实使用 `localhost` 访问前端，应另列 `http://localhost:5173`，两者不自动等同。 |
| 加载位置 | `backend/api/main.py` 创建应用时，在 `load_project_env()` 后读取原始环境变量，再调用 `backend/api/cors.py` 的纯函数解析与校验；`create_app(..., cors_origins=None)`（含未传该参数）表示读取环境变量，显式传 `[]` 表示使用空列表且不读取该变量，传入非空列表则使用注入值。测试应显式注入，避免依赖开发者的真实 `.env`；注入值仍须经过同一单项校验函数。根目录 `.env.example` 增加非敏感示例。 |
| 校验与默认 | 原始变量缺失或仅含空白时解析为空列表；否则按逗号拆分、逐项去除首尾空白，再校验。去除的是分隔符附近的格式空白，不修改 origin 本身；拆分后任一项为空（如尾随逗号或连续逗号）须在启动时报错并指出第几项，不得静默跳过。非空项必须是**规范形式**的完整 `http`/`https` origin：scheme 和主机名小写，主机名须为 ASCII（国际化域名显式写成 punycode），显式端口须在 1–65535 之间，不带末尾 `/`、路径、查询参数或片段，不显式填写默认端口（如 `http://example.com:80`、`https://example.com:443`）。`*` 和任何非规范形式均在应用启动时明确报错，不把原始值直接交给中间件。空列表时后端仍可处理直接请求，但浏览器跨域请求不会获得放行。测试注入的 `cors_origins` 也走同一校验。 |
| 中间件 | `allow_origins` 使用上述精确列表；允许 `GET`、`POST`、`PATCH` 和 `Content-Type` 请求头，`allow_credentials=False`。`PATCH` 随 doc 6 的修改数据源接口开放。浏览器的 `OPTIONS` 预检由 CORS 中间件处理，不新增业务路由。未来引入登录凭据时再评估该配置，不提前开放通配符。 |

后端监听地址与 CORS 是两件事：本机开发仍以 `--host 127.0.0.1 --port 8001` 启动。未来跨主机部署所需的网络监听、HTTPS、认证和访问控制另行设计，不能只把 CORS origin 加进列表就对外开放未认证的管理 API。

本阶段通过 FastAPI 的应用中间件注册 `CORSMiddleware`。已处理的业务/校验异常（如 `409`、`422`、`503`）响应会经过它，允许的前端 origin 可以读取原有 JSON；未预期异常由最外层的 `ServerErrorMiddleware` 生成 `500 internal_error` 响应，不经过该 CORS 中间件。直接请求仍能收到安全的 500 JSON，但跨域浏览器请求读不到响应体，会按现有前端请求层的 `network_error` 显示通用提示；该提示不能区分服务不可达与 CORS 遮蔽。本次接受此边界，不为 500 新增另一套错误处理。若以后要求浏览器也能读取 500 JSON，应评估按 Starlette 的建议用 `CORSMiddleware` 包裹整个 ASGI 应用，并同步调整应用工厂契约；仅将 500 改成另一个异常处理器不能保证解决中间件顺序问题。[Starlette 中间件说明](https://www.starlette.io/middleware/#corsmiddleware-global-enforcement)。

前端访问地址发生变化时，须同步检查 `PLATFORM_CORS_ORIGINS` 是否包含新的前端 origin。API 每增加一种浏览器会调用的 HTTP 方法，都要同步更新 CORS `allow_methods`，否则预检会拦截该请求。当前方法为 `GET`、`POST`、`PATCH`、`DELETE`。

校验采用“**不规范即拒绝**”，不悄悄替管理员修改配置。例如 `http://127.0.0.1:5173/`、`HTTP://127.0.0.1:5173`、`http://LOCALHOST:5173`、`http://example.com:80` 和 `http://例え.jp:5173` 都应在启动时指出 `PLATFORM_CORS_ORIGINS` 的非法条目。浏览器会把最后一例的主机名序列化为 punycode；若需配置该域名，应写 `http://xn--r8jz45g.jp:5173`。通过校验的原值才传给 `CORSMiddleware`；这是必要的，因为其中的 origin 白名单按字符串精确匹配，不会替配置值去掉斜杠、调整大小写或转换域名。

`backend/api/cors.py` 只提供两个小的纯函数：`validate_cors_origin(value: str) -> str` 校验并返回原值（不自动规范化），`parse_cors_origins(raw: str | None) -> tuple[str, ...]` 按上述空值、拆分与逐项校验规则解析环境变量。错误信息应指出非法项的位置及具体原因，例如 `PLATFORM_CORS_ORIGINS 第 2 项为空` 或 `PLATFORM_CORS_ORIGINS 第 2 项不是规范形式的 origin: IPv4 地址必须是规范形式`；不回显原始条目，避免误填含凭据 URL 时把凭据写进日志。环境变量中的条目先去除分隔符附近的空白；`create_app(..., cors_origins=...)` 的注入列表则逐项直接调用 `validate_cors_origin()`，不对注入值做 `strip()`，例如 `" http://a:1 "` 应拒绝。不能只校验环境变量而让测试注入绕过规则。本模块不读取 `.env`、不创建 FastAPI 应用，也不引入新的配置类。

## 4. 实施文件与顺序

| 顺序 | 文件 | 改动 |
| --- | --- | --- |
| 1 | `frontend/.env.example`、`.gitignore` | 提供 `VITE_API_BASE_URL=http://127.0.0.1:8001` 样例；确认 `.env.example` 可入库、实际 `frontend/.env*` 配置被忽略。 |
| 2 | `frontend/src/api/apiBaseUrl.ts`、`http.ts` | 前者提供供 Vite 配置与浏览器共用的纯地址校验；后者集中读取并使用 `VITE_API_BASE_URL`，沿用现有错误码解析，不修改业务 API 路径。 |
| 3 | `frontend/vite.config.ts` | 删除开发代理，保留固定开发端口；在开发启动及构建前检查地址配置，避免缺失时生成指向错误地址的页面。 |
| 4 | 根目录 `.env.example`、`backend/api/cors.py`（新增）、`backend/api/main.py` | 加入 `PLATFORM_CORS_ORIGINS` 示例；`cors.py` 负责纯解析与共享校验，`main.py` 只负责读取环境变量、调用校验并注册 FastAPI `CORSMiddleware`，保持现有路由和异常处理。真实根目录 `.env` 由开发者按本机地址填写，不写入 Git。 |
| 5 | `tests/unit/test_web_data_sources.py`、前端构建验证、本文档及 doc 4 | 直接单测 `parse_cors_origins()` 和 `validate_cors_origin()` 的合法/非法输入，并通过 `create_app(..., cors_origins=...)` 验证 `None` 读取环境变量、`[]` 使用空列表、注入项带首尾空白时拒绝；另测允许/拒绝的 Origin 与预检、已处理错误带 CORS 响应头，以及未预期 500 不带该响应头，验证前端配置及构建。现有调用 `create_app(settings)` 的测试夹具须改为显式传 `cors_origins=[]`，避免读取开发者的真实 `.env`；仅测试环境变量读取的用例使用 `None`。实施完成后同步 doc 4，具体范围见下文。 |

doc 4 的同步范围（已完成）：修订 §2 的代理描述、§6.3 的代理目标说明和文件职责表；在 §6.1 新增 `backend/api/cors.py` 一行，注明 `validate_cors_origin()` / `parse_cors_origins()` 是不读 `.env`、不创建应用的纯函数；修订 §6.1 的 `backend/api/main.py` 行，写明 `create_app` 增加 `cors_origins` 注入参数，以及读取 CORS 环境变量、调用共享校验并注册中间件的职责。

本次不需要数据库变更；`backend.data_sources`、CLI 和 `--step db` 不读取前端地址或 CORS 设置。现有 API 单测应保持通过。

## 5. 验收与手工联调

1. 开发者在 `frontend/.env.development.local` 配置 `VITE_API_BASE_URL=http://127.0.0.1:8001`，在根目录 `.env` 配置 `PLATFORM_CORS_ORIGINS=http://127.0.0.1:5173`。两份实际配置均不提交；修改前端 env 后重启 Vite。
2. 分别启动后端 `uv run --extra platform uvicorn backend.api.logged_app:create_logged_app --factory --no-access-log --host 127.0.0.1 --port 8001` 与前端 `cd frontend && npm run dev`。在 `http://127.0.0.1:5173` 打开页面，浏览器网络面板应显示请求直接发往 `http://127.0.0.1:8001/api/...`，而不是发往 `:5173/api/...`。后端启动入口已按 doc 15 改为部署专用工厂：该入口会把每个请求的一条摘要写入 `logs/api.log`、按模块归档业务日志到 `logs/backend/*.log`；继续使用旧入口 `backend.api.main:create_app` 时不会自动得到这些项目文件日志，Uvicorn 也不得改用 `--log-config configs/logging.yaml`（见 doc 15 §3.3）。`--no-access-log` 关闭 Uvicorn 原生访问日志，避免与 `api.log` 请求摘要重复。
3. 允许的 Origin 访问列表、测试连接、创建数据源时，CORS 预检和实际请求成功；未列出的 Origin 不获得 `Access-Control-Allow-Origin`。变量缺失或仅含空白时得到空列表；`PLATFORM_CORS_ORIGINS=http://127.0.0.1:5173,`、`http://a:1,,http://b:2` 等空条目输入须在启动时被拒绝并指出项序号。`PLATFORM_CORS_ORIGINS=http://127.0.0.1:5173/`（末尾多 `/`）及大写 scheme/主机名、冗余默认端口、Unicode 域名等输入也须在启动时被拒绝，不能启动成功后静默不匹配。无 `Origin` 的后端直接请求不因 CORS 配置而失效。原有业务状态码和 JSON 内容不因 CORS 改动：允许的 Origin 可读取正常响应及已处理的 `409`、`422`、`503`，但未预期异常的 `500 internal_error` 不带 CORS 放行头，浏览器只看到通用 `network_error` 提示；用 `raise_server_exceptions=False` 的测试客户端验证这一区别。
   若浏览器提示跨域请求被拦截，先核对当前页面的 origin（协议、主机、端口）是否逐字列在后端 `PLATFORM_CORS_ORIGINS` 中；不要把指向后端的 `VITE_API_BASE_URL` 值误填进该列表。
4. `VITE_API_BASE_URL` 缺失、带 `/api` 或格式非法时，开发启动/构建明确失败；不得继续使用旧 Vite 代理或意外请求前端自身。API 地址配置中不含任何凭据，前端构建产物也不含后端数据库密码或密钥。
5. 执行后端相关单测、前端类型检查和 `npm run build`。本地若只需临时验证构建，可在 `frontend/` 目录执行 `VITE_API_BASE_URL=http://127.0.0.1:8001 npm run build`（WSL/Linux），也可在被 Git 忽略的 `frontend/.env.production.local` 中填写同一键后直接运行 `npm run build`；若进程环境已有同名变量，它优先于该文件。上述本机地址仅供本机验证，正式构建须提供实际可访问的后端地址。使用生产示例地址构建后，确认构建产物访问的是该地址；改地址必须重新构建。静态文件的托管方式及正式部署安全验收不在本次范围。
