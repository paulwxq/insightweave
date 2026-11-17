# NL2SQL 旧前端清理设计

- 编写日期：2026-11-10。
- 状态：**已实施（2026-11-10）**——旧文件删除、`web` extra 与锁清理、文档同步、既有回归与前端检查完成；真实联调验收结果与遗留项见第 11 节实施记录。设计分析基于实施前工作区代码。
- 对应规划：[0_项目后续规划与实施顺序](0_项目后续规划与实施顺序.md)第 1 阶段。
- 路径约定：除链接外，本文文件路径均相对于仓库根目录 `/opt/projects/insightweave/`。

## 1. 目标与设计结论

删除 `nl2sql/` 内部的 Streamlit 页面、专用客户端与配置，移除旧前端的直接依赖声明和失效启动说明，以根目录 `frontend/` 中的 Vue 查询模块作为唯一受维护的产品前端。

本次属于清理工作，不改变查询业务链、接口协议、数据库结构、已有会话或结果文件。保留 FastAPI、DeepAgents、CLI 和旧图实现；旧图的迁移与退役属于后续第 2、3 阶段。

代码分析得到以下结论：

1. 旧前端已经与当前 API 脱节：客户端请求 `/api/v1`，现有应用仅挂载 `/api/v2`。无需为了删除旧前端而恢复旧接口，也不把旧页面能够成功查询作为验收前提。
2. `web` extra 中的三个包在非归档 Python 源码中的直接引用都集中在旧前端，可以移除整个 `web` extra。`pandas`、`requests` 仍被其他依赖使用，不要求它们从锁文件或环境中完全消失。
3. Vue 已有查询、历史、SQL、结果分页和 CSV 导出实现，但代码存在不等于实际联调通过，清理实施时仍要做回归。
4. **原阶段完成标准中的“终止功能正常”存在事实缺口：当前 NL2SQL 未实现用户主动终止查询的前后端入口。** 此项需按第 5 节单独记录与处理，不能将超时、预算停止或 MetaWeave 的运行收尾算作 NL2SQL 主动终止。

## 2. 当前代码与调用关系

### 2.1 旧前端文件

| 文件 | 当前职责 | 处理 |
| --- | --- | --- |
| `nl2sql/frontend/app.py` | Streamlit 页面；加载 `.env`，维护 `user_id/thread_id/messages`，展示问答、SQL 和表格 | 删除 |
| `nl2sql/frontend/api_client.py` | 用 requests 调用旧 API，封装健康检查、查询、会话列表与轮次读取 | 删除 |
| `nl2sql/frontend/.streamlit/config.toml` | 旧页面主题及上传大小配置 | 删除 |

删除上述三个文件，并清理 `nl2sql/frontend/__pycache__/` 中属于旧前端的 Python 编译缓存，再移除空目录。目录中若存在未跟踪的配置或其他文件，先核对用途；不以整目录强制删除代替清点，也不处理旧模块之外的用户文件。

旧链路为：

```text
nl2sql/frontend/app.py
  → NL2SQLApiClient
  → API_BASE_URL（默认 http://localhost:8008/api/v1）
      ├── 旧 query 路由 → nl2sql_father → sql_generation
      └── 旧 history 路由 → chat_history_reader（历史持久化读取）
```

依据（清理前定位，源文件已于 2026-11-10 删除）：`nl2sql/frontend/app.py`（旧页面）、`nl2sql/frontend/api_client.py`（旧客户端）、`nl2sql/frontend/.streamlit/config.toml`（旧配置）。原文中以链接形式指向这三处的内容已改为历史路径文字。

扫描未发现 `nl2sql/src/`、`nl2sql/scripts/` 或普通测试代码导入旧前端客户端。归档的 `streamlit_apptest.py` 明确引用旧页面，属于历史证据，处理见第 6 节。

### 2.2 当前受维护链路

```text
frontend/ 统一 Vue 应用
  → /query 或 /query/:sessionId
  → modules/nl2sql/api/client.ts
  → VITE_NL2SQL_API_BASE_URL + /api/v2/*
  → nl2sql/src/api/main.py
  → src/api/v2 的运行与持久化服务
  → db_deepagent
```

数据源列表另走 `useChat.ts → listDataSources → VITE_API_BASE_URL → MetaWeave /api/data-sources`；上图中的查询、会话与结果 API 使用 `VITE_NL2SQL_API_BASE_URL`。这两类请求分别指向各自后端，不做地址回退，因此查询页初始化还需要 MetaWeave 数据源列表接口可用。

关键事实：

- [API 入口](../src/api/main.py)只调用 `app.include_router(v2_router)`；启动阶段不再预编译旧父图。
- [Vue 路由](../../frontend/src/modules/nl2sql/routes.ts)提供 `/query` 与 `/query/:sessionId`。
- [Vue API 客户端](../../frontend/src/modules/nl2sql/api/client.ts)使用独立的 NL2SQL origin，不回退到 MetaWeave 地址。
- [会话交互](../../frontend/src/modules/nl2sql/composables/useChat.ts)调用新建会话、提问、历史读取等接口。
- `nl2sql/check_startup.py` 检查的是 FastAPI 导入，不属于旧前端入口，保留。
- `nl2sql/main.py` 当前只是打印问候语，也不是 Streamlit 启动器，本阶段不顺带删除。

### 2.3 当前 API 能力

以下接口来自 [v2 路由实现](../src/api/v2/router.py)，清理前后保持一致：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v2/health` | API 健康检查，不连接数据库 |
| GET | `/api/v2/sources/{source_id}/readiness` | 数据源就绪检查 |
| POST | `/api/v2/sessions` | 创建查询会话 |
| GET | `/api/v2/sessions` | 会话列表 |
| GET | `/api/v2/sessions/{session_id}` | 会话详情 |
| GET | `/api/v2/sessions/{session_id}/turns` | 历史问答及可回放的 SQL、结果引用 |
| POST | `/api/v2/sessions/{session_id}/query` | 同步执行一轮查询 |
| GET | `/api/v2/sessions/{session_id}/results/{result_ref}` | 结果分页及快照回退 |
| GET | `/api/v2/sessions/{session_id}/results/{result_ref}/export` | CSV 导出 |

本表没有主动终止端点。早期接入设计和启动说明中的接口表、历史展示范围与当前实现存在差异，本次分析以代码为准。

## 3. 依赖清理设计

### 3.1 直接依赖

[pyproject.toml](../pyproject.toml)当前声明：

```toml
[project.optional-dependencies]
web = [
    "streamlit==1.67.0",
    "pandas==3.1.1",
    "requests==2.34.2",
]
```

按 Python AST 扫描 `.py` 文件中的直接导入，排除虚拟环境、运行产物后，引用分布如下。历史 Markdown 文档内嵌的代码片段不计为 Python 文件的直接导入，保留其历史正文。

| 包 | 非归档源码中的直接引用 | 归档中的直接引用 | 设计决策 |
| --- | --- | --- | --- |
| streamlit | `frontend/app.py` | 升级记录中的 `streamlit_apptest.py` | 删除 web 声明，不迁入基础依赖 |
| pandas | `frontend/app.py` | 升级记录中的 `pandas_probe.py` | 删除 web 声明，保留其他组件所需的传递依赖 |
| requests | `frontend/app.py`、`frontend/api_client.py` | 本次 AST 扫描未发现其他归档直接导入 | 删除 web 声明，保留其他组件所需的传递依赖 |

以上表格中的 `frontend/` 是 `nl2sql/frontend/`，不是根目录 Vue 应用。

保留 `dev`、`monitoring` extras，以及 FastAPI、Uvicorn、Starlette、DeepAgents、LangChain、LangGraph、数据库驱动等当前主依赖与约束。本次不调整 SDK 版本，不扩大为新一轮升级。

### 3.2 锁文件与实际环境

当前 `uv.lock` 中：

- `pymilvus` 依赖 `pandas`。
- `dashscope`、`langchain-community`、`langsmith`、`pymilvus` 等依赖 `requests`。
- `streamlit` 自身还引入 Altair、PyDeck、Watchdog 等依赖，其中只有失去所有其他依赖路径的包才应被移除。

实施方式：先删除 `web` extra，再用 `uv lock` 正常更新锁文件，**不使用 `uv lock --upgrade`，不手工删除锁文件条目**。检查锁差异，要求根项目的 web 元数据和 Streamlit 引用消失；其他包是否移除由剩余依赖图决定。

重点检查共享包的版本变化。删除 pandas、requests 的直接精确约束后，如果解析出现与清理无关的版本变更，应查明原因并控制差异，不把版本升级混入清理结果。不能为保持“看起来没变”而把旧 UI 的全部依赖搬到主依赖。

锁更新后同步 `nl2sql/.venv`。还应使用新的临时环境按新锁安装一次，避免旧环境残留掩盖运行依赖缺失。安装 `--all-extras` 时已不再包含 web；当前运行文档（README、启动指南）及有效启动脚本中的显式 `--extra web` 或 `.[web]` 指令如有残留，须删除或替换。历史设计和归档命令保留原有时点语义，按 §4.2、§6.2 注明使用边界，不按本条改写历史正文。

不要以“pandas/requests 已卸载”作为完成标准。应验证删除旧 UI 的直接依赖声明后，后端和现有 CLI 不依赖 Streamlit 仍可使用。

## 4. 配置、启动入口与文档

### 4.1 配置边界

- 删除旧页面专用 `.streamlit/config.toml`。
- `API_BASE_URL` 在本次扫描的 NL2SQL 运行源码中只被旧客户端读取；删除该消费者和运行文档中的示例。当前 `nl2sql/.env.example` 没有该变量，无需为此改模板。
- 不批量编辑用户的 `.env` 或输出其内容。若本地仍有 `API_BASE_URL`，可在实施记录中提示其已无旧前端消费者。
- 保留根前端的 `VITE_API_BASE_URL`、`VITE_NL2SQL_API_BASE_URL`，以及后端 CORS、Host 和运行预算配置。
- 本阶段不修改端口、同域代理、构建时配置方式，这些属于后续部署改造。

### 4.2 当前说明的更新清单

| 文件 | 当前问题 | 设计中的修改 |
| --- | --- | --- |
| `docs/1_服务启动指南.md` | 端口表仍列 8501；保留 Streamlit 启动命令、web 依赖与故障排查 | 删除这些运行指引；保留 5173/8001/8008 的 Vue 与双后端说明；补齐现有 export 接口及历史回放描述 |
| `nl2sql/README.md` | 路线图仍标记 Streamlit；默认 Web 示例指向 8000 上的 `/api/v1` | 标明统一 Vue 入口，当前 Web 启动与 API 使用说明转为 v2 或链接根启动指南；旧父图与旧 API 内容明确标为历史/脚本参考 |
| `docs/2_NL2SQL_Vue接入设计.md` | 旧页面源码链接及 P5 清理条件仍按迁移期描述 | 实施验收后追加带日期的清理状态与本文链接；旧页面能力表保留历史含义，将失效源码链接改为历史路径文字；注明历史回放及 CSV 导出已实现，早期“不新增结果下载”等首版范围仅具历史含义，以当前代码为准 |
| `nl2sql/docs/0_项目后续规划与实施顺序.md` | 第 1 阶段尚未执行；“终止”完成标准存在缺口 | 根据实际结果记录清理状态及未完成项，不提前标记整个阶段完成 |

README 的更新限于与本次旧 UI 退出直接有关的产品入口、安装和 Web 使用说明，不顺带重写整份旧图架构文档。安装说明统一采用模块内 uv 锁定同步，按需要选择 `dev`/`monitoring`。

`docs/0_项目整合讨论与共识.md`、`nl2sql/docs/1_*`、`nl2sql/docs/2_*` 及 `docs/old/`、`docs/upgrade/` 中的历史版本、曾执行的命令和验收结论保留原有时点语义。必要时只追加状态提示，避免全仓替换“Streamlit”篡改历史。

## 5. 功能覆盖与“终止”缺口

### 5.1 覆盖矩阵

“已有实现”是静态分析结论，以下能力尚未在本轮实际执行验收。

| 能力 | 旧 Streamlit | 当前 Vue / API | 清理验收要求 |
| --- | --- | --- | --- |
| 新建查询、连续提问 | thread_id + 同步提交 | 数据源选择、readiness、创建会话、同步 query | 同源会话可连续提问，响应与历史对应 |
| 历史列表与恢复 | sessions/turns | v2 会话列表、详情和历史轮次 | 刷新和切换会话后能恢复，不混入旧链历史 |
| SQL 展示 | 当前消息 detail | EvidenceBlock / SqlBlock，历史 display 带 SQL | 当前轮及有 display 的历史正常；旧记录缺 display 时允许仅问答 |
| 结果展示 | pandas DataFrame | ResultTable、分页读取、结果快照 | 列名、数值、NULL、空结果与分页正常，文件缺失时按现有快照规则处理 |
| CSV 导出 | 未发现实现 | exportResultCsv + export 路由 | 保留当前已实现能力，不能因其不在早期范围中而删掉 |
| 任意输入 user_id | 支持 | 固定 guest | 沿用现有产品契约，不新增用户切换或认证 |
| 主动终止查询 | 未发现实现 | 未发现按钮、客户端方法或 API 端点 | 记录缺口，不宣称已覆盖 |

历史回放依据 [webstore.py](../src/api/v2/webstore.py) 的 `normalize_turn_display` 及轮次投影；结果依据 [router.py](../src/api/v2/router.py)、[snapshots.py](../src/api/v2/snapshots.py)；前端依据 `EvidenceBlock.vue`、`ResultTable.vue` 与 `useChat.ts`。

### 5.2 终止能力的处理建议

当前 [QuestionInput.vue](../../frontend/src/modules/nl2sql/components/QuestionInput.vue)只发出 `submit` 事件，没有停止操作。[API 客户端](../../frontend/src/modules/nl2sql/api/client.ts)中的 AbortController 用于等待超时；其错误提示明确说明后端可能仍在执行。`useChat.ts` 的 `stopElapsed()` 只停止计时显示。

Agent 内的预算耗尽、deadline 与异常收尾是执行限制，不是用户主动取消。[运行终止功能设计](../../docs/11_运行终止功能设计.md)描述的是 MetaWeave 采集/发布遗留运行的状态收尾，也不能作为 NL2SQL 查询终止能力的依据。

**建议将主动终止拆为独立功能设计，旧前端清理按本文件先推进。** 若后续实施要求仍完整保留原阶段的“终止功能正常”标准，则本次只能交付“旧前端清理子项”，整个第 1 阶段仍有未完成项；不能在未确认范围调整时默认为该要求已取消。

独立终止设计至少需要确定运行标识、取消入口、协作停止边界、正在调用模型或执行 SQL 时的处理，以及锁、执行名额、历史与结果的收尾规则。不能只给 Vue 增加一个中断 fetch 的按钮就视为实现完成。本文件不设计或实施该功能。

## 6. 保留范围与历史证据

### 6.1 保留的代码与数据

- `nl2sql/src/api/main.py`、`src/api/v2/`、`src/api/core/` 及其现有测试。
- `nl2sql/src/modules/db_deepagent/`、共享服务、结果存储与持久化实现。
- `nl2sql/src/modules/nl2sql_father/`、`nl2sql/src/modules/sql_generation/`，保留现有 CLI 调用。旧路由 `nl2sql/src/api/routers/` 和旧 Schema `nl2sql/src/api/schemas/` 作为旧链参考保留；当前 `scripts/`、`tests/`、`src/tests/` 未发现对这两个 API 包的直接导入，不把入口注释中的“保留供脚本用途”表述为已有脚本调用。
- `nl2sql/scripts/`、现有 CLI、`check_startup.py`。
- 根目录 Vue 前端及 MetaWeave 的全部业务代码和依赖。
- 原有会话、checkpoint、Store 记录、结果快照、结果文件、日志及业务配置。

本阶段不增加数据迁移、不初始化或清理数据库、不把 v1 历史转成 v2 历史，不执行维护删除命令。

### 6.2 归档脚本

`nl2sql/docs/upgrade/implementation_2026-11-09_run01/` 中的以下文件保留：

- `streamlit_apptest.py`：依赖即将删除的页面及旧 SDK 环境，清理后不在当前环境执行。
- `pandas_probe.py`：记录升级时的 pandas 构造行为，不是当前 UI 运行依赖。
- 升级前后的 pyproject、lock、包清单、日志和验收记录。

实施时在该目录 README 追加“旧前端已清理后的重放边界”：旧脚本需恢复当时源码和依赖环境，不能直接在新环境重放。保留脚本原文，不为让它继续运行而重新安装 Streamlit，也不把历史冒烟结果当作本次 Vue 验收。

### 6.3 旧 API 测试

`nl2sql/tests/unit/api/test_api_routes.py` 仍按 `/api/v1` 编写，和当前只挂载 v2 的应用有前提差异。已有升级记录也登记了旧 API 测试失败。

这些测试不直接导入旧前端，不随本次删除。实施前后如运行该范围，应比较具体失败 ID 与原因，不能把新失败统称为历史失败，也不能通过删除旧 API 测试使报告变绿。旧 API 测试的退役或重写留给后续旧链整理。

注意 `pytest.ini` 默认 `testpaths = src/tests`，不会自动覆盖根层 `tests/`；API v2 与 DeepAgents 测试也需要显式列出目标范围。

## 7. 实施步骤

1. **记录基线**：确认工作区已有未提交改动；为本次将删除或修改的文件保存清理前内容副本（包括 `nl2sql/pyproject.toml`、`nl2sql/uv.lock`、`nl2sql/README.md` 和相关文档），并记录已有差异与回归结果，作为 §9 回退基准，不覆盖此前依赖升级与 Agent 修改。
2. **确认替代能力与授权**：按第 8 节验证 Vue 查询、历史与结果链，呈现验收结果及未覆盖项，并核对、记录现有会话中用户对 Vue 替代旧入口的明确确认，承接根接入设计 §9.3 的删除条件。已有明确替代确认或用户明确要求按本设计执行清理时，沿用该授权，不重复询问；仅缺少这类授权时，在删除前就替代范围集中确认。主动终止按第 5.2 节单独记录，替代授权不等于取消该验收要求。本轮仅审核文档，不执行清理。
3. **删除旧前端文件**：仅删除第 2.1 节列出的三个文件及旧前端编译缓存，核对其他未跟踪文件后整理空目录。
4. **移除依赖并更新锁**：删除 `web` extra，执行普通 `uv lock`，审查共享包版本与依赖差异。
5. **验证环境**：锁定同步当前模块环境，并在新的临时环境安装与运行针对性回归，证明不靠 Streamlit 残留运行。
6. **同步文档**：按第 4 节更新当前使用说明和第 6.2 节归档说明；完成后再更新规划状态。
7. **完成验收记录**：区分静态、依赖、单元、构建、真实接口与浏览器结果，逐项列出未通过或未执行项。

建议本次清理作为一组独立变更交付，便于检查和回退，不混入旧图删除、SDK 升级、页面改版或 Docker 配置。

## 8. 验证与完成标准

以下命令是实施时的验证建议，本轮文档编写未执行这些回归或安装操作。

### 8.1 静态与依赖检查

从仓库根目录搜索运行代码与使用说明：

```bash
rg -n -i 'streamlit|frontend/app\.py|8501|--extra[ =]+web|\[web\]' nl2sql/src nl2sql/scripts nl2sql/pyproject.toml nl2sql/README.md docs/1_服务启动指南.md
```

预期无继续指导运行旧前端的内容；如有明确的历史说明，应人工检查语义。历史设计、归档和本文允许出现这些文字，不以全仓零匹配为目标。

在 `nl2sql/` 执行：

```bash
uv lock --check
uv sync --locked --extra dev --extra monitoring
uv pip check --python .venv/bin/python
uv run --no-sync python -c 'import importlib.util; assert importlib.util.find_spec("streamlit") is None'
```

实施时还须检查锁的根项目元数据已无 web extra、没有 Streamlit 依赖条目，并确认 pandas/requests 剩余路径合理。新临时环境中的同类检查应单独记录，不能仅凭旧环境检查通过。

### 8.2 现有后端回归

在 `nl2sql/` 显式运行：

```bash
uv run --no-sync pytest src/tests/unit/api/v2/ src/tests/db_deepagent/unit/ -q
```

v2 公共 fixture 注入 fake Store、readiness、runner 等组件，适合验证协议和收尾逻辑。重点检查会话创建、连续提问、历史投影、结果分页/快照、CSV 导出、Origin/Host 限制及资源释放。数据库或模型实测仍需单独记录，fake 测试不替代联调。

本次纯删除原则上复用现有测试。仅在发现“删除后启动仍依赖旧前端”等实际缺口时，补充有业务意义的回归测试，不新增只断言文件不存在的测试。

### 8.3 统一前端检查

在 `frontend/` 使用现有有效配置运行：

```bash
npm run typecheck
npm run build
node --experimental-strip-types --test src/modules/nl2sql/sqlHighlight.test.ts src/modules/nl2sql/utils/cellDisplay.test.ts src/modules/nl2sql/utils/resultSummary.test.ts
```

最后一条使用支持 TypeScript 类型剥离的 Node 22 版本；实施前核对 Node 版本。当前没有 `npm test` 脚本，不假设该命令可用。构建需要符合现有校验的 `VITE_API_BASE_URL`，查询联调另需 `VITE_NL2SQL_API_BASE_URL`，不在文档内写真实配置或凭据。

### 8.4 真实接口与浏览器验收

使用已准备好的验收数据源，按根启动指南运行两个后端与 Vue。浏览器操作遵循仓库 AGENTS.md，先执行 `ego-browser skill` 并使用 ego-browser。不要修改或发布知识数据来凑本次清理验收。

| 场景 | 预期结果 |
| --- | --- |
| 选择已就绪数据源，新建查询 | 创建成功，请求指向 NL2SQL `/api/v2`，不访问 8501 或 `/api/v1` |
| 正常提问、同会话追问 | 答案和 SQL 可见，追问使用正确会话与数据源 |
| 打开历史、刷新页面 | 问答恢复；有 display 的记录恢复 SQL/结果引用，缺 display 的旧记录正常展示文本 |
| 展开结果、翻页、导出 | 列和值正常，CSV 可下载，分页不重复执行 SQL |
| 空结果或查询失败 | 正常展示状态；不会因为没有表格而出现页面异常 |
| 请求超时或断连 | 提示结果未确认，不自动重复提交，不显示“已终止” |
| 切换会话、数据源 | 不串用会话结果，不改变既有会话来源绑定 |
| 打开 MetaWeave 页面 | 原有数据源、工作区等页面不因旧 UI 清理而退化 |

健康检查 200 只证明 API 可响应；构建成功只证明代码能构建。若模型、知识库或浏览器不可用，应明确写“未验收”，不能将静态覆盖当作联调通过。

### 8.5 交付判定

旧前端清理子项完成需要同时满足：按 §7 步骤 2 核对并记录 Vue 替代授权；三个旧文件删除；web 直接依赖和锁元数据清理；当前运行文档无失效入口；新环境后端检查及现有回归无新增问题；Vue 查询、历史与结果实际验收通过；历史数据与归档证据保留。

原规划要求的主动终止当前未实现，验收记录必须保留这一未完成项，直至独立功能交付或明确确认调整阶段范围。本文不把清理子项完成等同于原阶段全部完成。

## 9. 回退方式与本轮分析限制

清理不涉及数据库迁移。若实施后出现问题，以 §7 步骤 1 保存的清理前副本为基准，恢复本次删除的三个文件，并撤销本次依赖声明、锁文件及文档修改，再按恢复后的锁同步环境。

`nl2sql/pyproject.toml`、`nl2sql/uv.lock`、`nl2sql/README.md` 等文件在清理前已有未提交改动，不能用 `git restore`、`git checkout` 等从 HEAD 或旧提交覆盖，也不能整仓重置。若备份后还叠加了其他工作，应仅撤销本次清理差异，保留后续改动；回退后与基线记录核对，确认原有升级与文档差异仍在。

恢复 Streamlit 文件只是恢复旧代码，不会让已下线的 `/api/v1` 自动重新可用；统一 Vue 与 v2 API 仍是当前产品入口。

本轮已完成源码、隐藏 Streamlit 配置、直接导入、锁文件依赖路径、启动与设计文档、测试入口的静态核对。未安装或删除依赖、未运行单元回归或构建、未调用真实模型/数据库、未做浏览器验收，因此本文不宣称现有功能已经实测通过。

## 10. 文档审核记录

2026-11-10，先通过 Orca 当前左侧现有 Claude agent 审核，随后按用户要求由 `kimi code`、`deepseek-claude` tab 的现有 agent 分别独立审核。各次审核分别最多三轮，任一次第三轮仍有未解决问题时交由用户判断。协调者核对反馈后修改本文。

### Claude 第 1 轮

结论：**PASS WITH SUGGESTIONS**，无必须修复项，三条可选建议处置如下。

| 编号 | 建议 | 判断与修改 |
| --- | --- | --- |
| R1-01 | 补全旧路由和旧 Schema 的保留路径 | 采纳，已核实实际目录，在 §6.1 明确列出 |
| R1-02 | 明确旧前端 `__pycache__` 的处置 | 采纳，缓存实际存在；§2.1、§7 仅补充旧前端编译缓存的清理，不扩大删除范围 |
| R1-03 | 说明历史 Markdown 中内嵌 requests 代码与 AST 扫描的区别 | 采纳其澄清意图，在 §3.1 统一限定 `.py` 扫描范围，不逐一扩展归档文件清单 |

以上修改已由第 2 轮复审确认。本记录仅表示文档审核状态，不表示旧前端清理或功能验收已完成。

### Claude 第 2 轮

结论：**PASS**。同一 Claude agent 复核第 1 轮三项修改及全文，确认修改与源码一致，无新增或遗留问题，无范围扩大，也未取消主动终止的原验收要求。

Claude 审核循环在第 2 轮结束，无需其第 3 轮；本次仅修订设计文档及记录审核结果，未实施旧前端清理、依赖变更或功能验收。

### Kimi 第 1 轮

结论：**NEEDS REVISION**，提出一项必须修复意见和一项可选建议；经核对处置如下。

| 编号 | 建议 | 判断与修改 |
| --- | --- | --- |
| K1-01 | 承接根 Vue 接入设计 §9.3 的用户替代确认条件 | 部分采纳：§7、§8.5 补充授权核对和记录；沿用现有会话的明确授权，不新增无条件重复确认，也不把替代授权视为取消主动终止要求 |
| K1-02 | 状态注记同时说明 CSV 导出已演进 | 采纳，§4.2 在原有历史演进注记中补充 CSV 导出，不重写旧设计正文 |

以上修改已由 Kimi 第 2 轮复审确认。Kimi 首轮报告核验摘要误将 API 端点数写为 8，已在第二轮报告中更正；实际装饰器和本文表格均为 9 个，本文无需改动端点清单。

### Kimi 第 2 轮

结论：**PASS**。Kimi 确认 K1-01、K1-02 均已充分处置，未发现新增或遗留实质问题；授权核对沿用现有会话的明确授权，不重复询问，且未取消主动终止的原验收要求。

Kimi 审核循环在第 2 轮结束，无需第 3 轮。通过 Orca 的现有 Kimi tab 完成两轮审核，未更换 agent；因监督任务接口不支持接管该会话，实际使用 Orca 终端收发说明与读取报告。本次仅修改本文及审核记录，未实施旧前端清理、依赖变更或功能验收。

### DeepSeek 第 1 轮

结论：**NEEDS REVISION**，提出两项必须修复和两项可选澄清；协调者核对后均采纳。

| 编号 | 问题或建议 | 判断与修改 |
| --- | --- | --- |
| D1-01 | 旧链路混淆查询与历史读取，旧路由/Schema 的脚本用途缺少当前调用证据 | §2.1 分开 query 与 history 分支；§6.1 区分旧图的 CLI 调用和旧 API 包的历史参考地位 |
| D1-02 | 回退未明确使用清理前副本，文件级 HEAD 恢复也可能丢失已有升级改动 | §7 步骤 1 明确保留文件内容副本；§9 以该副本为基准回退，保留已有和后续叠加改动，不从 HEAD 覆盖已有未提交成果 |
| D1-03 | 当前调用关系未说明 MetaWeave 数据源列表分支 | §2.2 增加一段说明，不增加服务或改变调用方式 |
| D1-04 | web 安装指令清理范围与历史保留表述有歧义，搜索未覆盖 `.[web]` | §3.2 限定当前运行文档和有效脚本，保留归档原文；§8.1 搜索同时覆盖显式 extra 与 `[web]` |

以上修改已由 DeepSeek 第 2 轮复审确认。该审核者首轮另实测现有三个 Node 测试文件，共 10 项通过，仅用于核对命令可执行性；未执行 Python 回归、构建、依赖同步或真实联调，不作为本次清理验收通过的依据。

### DeepSeek 第 2 轮

结论：**PASS**。D1-01 至 D1-04 均已正确、充分处置；全文复查未发现新增或遗留实质问题、范围扩大或过度设计，主动终止的原验收要求仍保留。

DeepSeek 审核循环在第 2 轮结束，无需第 3 轮。第一轮通过 `orchestration worker-start` 委托现有 `deepseek-claude` tab；第二轮监督接口续接失败，重新绑定也返回 `agent_unconfigured`，确认未发送后改用 `terminal send` 向同一会话提交复审，并读取完整报告。未更换 agent，本次仅修订本文与审核记录，未实施旧前端清理。

## 11. 实施记录（2026-11-10）

实施由 Orca 协调者委派当前 Claude pane 执行，依据本文与既有授权，未新增确认循环。基线为协调者备份 `/tmp/insightweave-doc3-implementation-d0h5zbvv/`（HEAD `975977db`，当时工作区干净）；实施前重新核对，工作区与基线一致、无并发改动。

### 11.1 实际改动（11 个文件，与第 2 节范围一致）

- 删除 `nl2sql/frontend/app.py`、`api_client.py`、`.streamlit/config.toml` 及 `__pycache__/api_client.cpython-312.pyc`，目录清点后移除（无未知文件）。git 呈现 `D`×3；编译缓存本就不被跟踪。
- `nl2sql/pyproject.toml` 移除 `web` extra；普通 `uv lock` 更新 `nl2sql/uv.lock`。
- 文档同步：`docs/1_服务启动指南.md`（删 8501 端口行/Streamlit 启动节/排障条目，改依赖说明，接口表补 export 与 evidence 回放）、`nl2sql/README.md`（安装改 uv 锁定同步、启动改 v2+根指南、旧 `/api/v1` 内容标注历史、路线图改 Vue）、`docs/2_NL2SQL_Vue接入设计.md`（失效源码链接改历史路径文字、P5 标注已实施、历史展示与 CSV 导出演进注记）、`nl2sql/docs/0_项目后续规划与实施顺序.md`（第 1 阶段子项状态：清理已实施、终止未实现、阶段未完成）、run01 README 追加 §J 重放边界、本文状态行与依据行更新。

### 11.2 锁差异（对照基线锁逐项）

移除 15 个包且仅此 15 个：`streamlit`、`altair`、`pydeck`、`watchdog`、`pyarrow`、`pillow`、`jinja2`、`markupsafe`、`jsonschema`、`jsonschema-specifications`、`referencing`、`rpds-py`、`narwhals`、`itsdangerous`、`python-multipart`（全部因失去旧 UI 依赖路径；锁条目 134→119）。**零新增、零版本变化**；`pandas`、`requests` 按 §3.2 预期经 `pymilvus` 等保留。根项目锁元数据已无 web extra，锁内 Streamlit 引用 0 处。

### 11.3 验证结果

- 后端回归（`uv run --no-sync pytest src/tests/unit/api/v2/ src/tests/db_deepagent/unit/ -q`）：基线 **787 passed / 4 skipped / exit 0**，实施后同为 **787 / 4 / 0**，零新增失败；证据 `/tmp/doc3-cleanup-implementation/logs/`。
- 当前环境：`uv lock --check`、`uv sync --locked --extra dev --extra monitoring`、`uv pip check`（116 包兼容）、`find_spec("streamlit") is None` 全部通过。
- 新临时环境按新锁安装：`pip check` 兼容、streamlit 缺席、同范围回归 **787 / 4 / 0**，与旧环境一致。
- 前端：`npm run typecheck` ✓、`npm run build` ✓（1.58s）、三个 Node 测试 **10/10 pass**。
- §8.1 复扫：运行代码零匹配；使用说明仅余 4 处带日期的清理状态记录（属允许的历史说明）。
- 真实接口验收（自起临时服务 8008/8001/5173，验收后已全部清理）：health 200；`db_src_1` readiness 全项 pass；API 建会话→真实提问（5 次模型调用，"500 条订单"）→同会话追问（"2024 年 0 条"，上下文正确；注意该轮是 COUNT 返回单行值 0 的正常结果，不是空结果集）→ turns 2 轮含 evidence→ 单行结果 offset=1 越界返回空（仅作分页边界检查）→ export HTTP 200 CSV 内容正确。
- 浏览器验收（ego-browser）：选源→就绪提示→新建会话（URL 进入 `/query/s_*`）→提问（答案"98 个不同客户"+表格+耗时）→查看 SQL（`SELECT COUNT(DISTINCT user_id)`）→刷新后问答/标题/SQL 入口完整恢复→切换到另一会话两轮内容各自恢复→MetaWeave 数据源管理页正常（db_src_1 在列）→performance 核验 **0 次 8501//api/v1 请求**、/api/v2 请求 4+ 次→导出按钮点击无错误（API 级 CSV 已验）。
- C1 复审补测（2026-11-10，全部零生产代码改动，测试替身仅注入浏览器运行时）：**多页翻页（真实 500 行列表）**——API 分页 page1/page2/page5（limit=100）行值边界与 `has_more`/`next_offset` 正确（401–500 页 `has_more=false`）；浏览器首屏"已显示 10 / 共 500 条"，"查看全部"展开至 200 条封顶（展示上限 200 为既有产品行为）。**真零行结果**——"列出2024年下单的全部订单" 返回 `status: no_data`、`row_count: 0`、`rows: []`，浏览器正常展示解释性答案、无表格无异常。**查询超时分支（模拟）**——真实提交问题后以页面运行时将客户端查询定时器 480s 缩至 2.5s：客户端按预期放弃并提示"结果尚未确认，后端可能仍在执行；请勿重复提交，稍后在历史中查看"（messages.ts 对 `timeout` 本地码的映射），后端实际继续执行完成、轮次入历史（刷新后完整可见），无自动重发（会话仅 1 轮）、无"已终止"字样。

### 11.4 未执行项与已知边界

- **查询超时为模拟覆盖，非真实 480 秒等待**：客户端超时区分两档——普通读取默认 30 秒（`DEFAULT_TIMEOUT_MS`），问答提交显式 480 秒（`QUERY_TIMEOUT_MS`，doc 2 §7.3）。本轮以运行时定时器替身（480s→2.5s）+ 真实后端提交验证了客户端超时分支、未确认提示、后台继续执行与历史落盘；未执行真实 480 秒等待，也未实测 30 秒普通读取超时。
- **真实查询失败状态未实测**：服务端返回 `failed`/`partial` 等失败终态时的 UI 展示路径未触发（需真实故障注入，本次不人为制造）；现有覆盖为 §8.2 fake 测试与上述模拟超时。
- 主动终止缺口维持 §5.2 结论：未实现、未新增代码，第 1 阶段整体未完成。
- 旧 API 测试（`tests/unit/api/test_api_routes.py`）未运行、未删除：本次改动不触及该范围，其既有失败属升级记录已登记的历史问题。
- 根 Vue 与 MetaWeave 生产代码保持**零改动**（git 状态仅本文档列出的 11 个文件）；C1 补测全部通过临时外部手段（临时服务、浏览器运行时替身）完成，未新增测试框架或生产功能。

## 12. 实施后的代码审核（2026-11-10）

本节与第 10 节设计审核分开计数。按用户要求，左侧 Claude pane 实施，协调者亲自审核；最多三轮，第三轮仍有问题时交由用户判断。任务与反馈均通过 Orca `orchestration` 传递。

- **第 1 轮：要求修订。** 协调者核实清理范围、依赖差异与验证日志后，提出三项问题：CSV 演进注记遗漏、将查询 480 秒超时误写为普通请求 30 秒、验收误将 COUNT=0 当空结果且单行 offset 检查不足以证明多页翻页。Claude 逐项核实后全部采纳，无拒绝项；修正文档并用临时外部手段补测，未修改现有 UI 功能。
- **第 2 轮：PASS。** 协调者核对修订、补测证据、全仓基线哈希及当前依赖/导入检查，确认三项均已处理，无新增阻塞问题。实际变化仍限于约定的 11 个文件；根 Vue、MetaWeave 及 NL2SQL 后端业务代码未变。旧前端已删除，锁仅移除 15 包、无新增或版本变化；无 Streamlit 环境下 API 导入与接口定义检查正常。

代码审核循环在第 2 轮结束，无需第 3 轮。清理代码与依赖变更通过审核；第 11.4 节未测项和主动终止缺口仍保留，不据此宣称原规划第 1 阶段整体验收完成。变更留在工作区，未执行 Git 提交。
