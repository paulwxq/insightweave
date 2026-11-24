# 13. InsightWeave Docker 容器化与本地 CI/CD 发布设计

> 状态：静态复核后可按 §10 分步实施；正式容器化及 GitHub Actions 流水线尚未实施，运行可行性以各阶段实测为准。
> 日期：2026-11-16；代码依据：`6b0c396`。
> 范围：整个 InsightWeave 项目，包括 `frontend/`、`metaweave/`、`nl2sql/`、三个数据库基础服务及 Attu 管理界面。
> 执行者按本文任务逐项实施；需要执行计划流程时使用 `executing-plans` 技能。本次仅编写设计，不启动正式部署、不注册 Runner、不修改业务代码。

**目标：**代码推送 GitHub 后，由本机自动完成测试、构建及发布；Windows Docker Desktop 中最终运行七个服务。

**架构：**GitHub 负责版本托管、触发任务和展示结果；Debian WSL2 中的 self-hosted Runner 操作现有 Windows Docker Desktop。采用本地构建、本地部署，不使用 GHCR，不使用 GitHub 托管计算。

**技术栈：**GitHub Actions、Linux self-hosted Runner、Docker Desktop / WSL2、Docker Compose、Python 3.12、uv、Node.js 22、Nginx。

**需求依据：**用户后续确认将 Attu 纳入同一份 Compose，部署数量由最初六个调整为七个镜像/容器；对外端口以 §4 的明确清单为准。不考虑电脑关机及现有数据迁移，需要数据库初始化；访问 GitHub 可使用 Windows 上已有的本机代理，配置参考 §4.2。服务入口参见 [服务启动指南](1_服务启动指南.md)。

**配套实施明细（开发前必读）：**[13.1_镜像与依赖安装明细](13.1_镜像与依赖安装明细.md)。数据库完整仓库地址与 digest、三个应用的基础镜像/系统、系统包和 Python/npm 包名及版本、安装命令和构建验证统一维护在该补充中；本文负责整体流程、边界和任务顺序。

## 1. 已确认的范围

- **七种部署镜像、七个常驻容器**：三个应用、三个数据库服务及一个 Attu 管理工具，每种服务一个实例，统一由同一份 `deploy/compose.yaml` 管理。
- 三个应用镜像由项目构建；PostgreSQL、Neo4j、Milvus、Attu 使用上游固定版本镜像。
- Runner 直接运行在现有 Debian WSL2 中，不额外作为容器；不另装一套 Docker daemon。
- 前端镜像内含 Nginx，同时提供静态页面和 API 反向代理，不另加 Nginx 容器。
- Milvus 使用单容器 standalone 配置，不额外启动 etcd、MinIO 或 Pulsar；Attu 是已计入七容器总数的独立管理界面。
- GitHub Actions 和命令行手动发布调用同一套本地脚本。
- 本次不搬运旧平台数据、不升级现有业务逻辑，不引入 Kubernetes、独立任务队列或新的作业执行容器。
- PostgreSQL 中保存平台数据及问数持久化；用户的业务源数据库仍是外部数据源，不额外加入部署容器。
- 日常发布只替换应用容器，不清空数据库、知识、历史对话或结果文件。
- Attu 与三个数据库一样在日常应用 release/rollback 中保留，不加入三个自建应用的 release_id；其版本升级作为显式配置变更处理。
- 首版按“本人或少数完全可信维护者的专用仓库、专用 Runner”设计；实际仓库可见性和写权限尚未确定，接入要求见 §8.3，不把分支名当成安全隔离。

数量口径：七种镜像指**部署运行时使用的镜像种类**。Node、Python 等构建基础镜像、BuildKit 临时步骤，以及同一应用保留的新旧版本标签不计作新增服务。不能承诺 `docker image ls` 永远只有七行。

候选镜像预检可以用同一应用镜像启动短时 `--rm` 容器，不增加部署镜像种类或 Compose 常驻服务；该步骤结束后仍只有七个常驻容器。首版不同时运行两整套应用做蓝绿发布。

## 2. 当前事实和已验证条件

| 项目 | 当前事实 |
| --- | --- |
| 总仓库 | 当前三个模块由项目根 Git 仓库统一管理；尚未配置根仓库远端 |
| GitHub | 当前 CLI 可访问账号 `paulwxq`；最终承载此总仓库的 GitHub 仓库和发布分支需在接入时设置，不自动覆盖现有独立仓库 |
| Docker | 本机 Docker Desktop 4.55.0、Engine 29.1.3，Linux/amd64；Compose v2.40.3 |
| WSL | Debian WSL2；Windows 和 WSL Docker CLI 已验证连接同一 Docker Desktop 引擎 |
| 构建工具 | 当前 uv 0.11.2；Node.js 22；两个 Python 模块各有 `uv.lock`，前端有 `package-lock.json` |
| 网络 | Git、GitHub、PyPI、Python wheel、npm 下载及 Docker 镜像拉取已验证可用；出现过一次 GitHub EOF，复测恢复 |
| 实际构建 | 按 doc 13.1 固定基础镜像完成三个临时依赖镜像构建；前端通过类型检查、Vite 构建及静态访问，两个后端通过生产依赖安装、原生库导入和 API 启动，见 doc 13.1 §8.2 |
| 运行测试 | 独立 Compose 临时容器可被 Windows/WSL 访问；替换容器后命名卷内容保留，测试资源已清理 |
| 尚未验证 | 包含部署运行时代码的正式镜像、实际 Nginx API 代理、七服务联调、正式初始化及 GitHub Runner 任务闭环 |

基础环境测试不等于正式部署已通过。测试时没有启动 PostgreSQL、Neo4j、Milvus，没有修改源码或现有数据库。

WSL 使用 `default` Docker context 和 builder；当前另一个 `desktop-linux` context 指向 Windows named pipe，Linux CLI 枚举时会提示协议不可用，不能误选。Windows Docker CLI 则可以使用该 context。实施时检查实际 endpoint 和引擎身份，不通过切换全局配置碰运气。

## 3. 七个镜像和容器

| 序号 | Compose 服务 / 容器职责 | 运行镜像 | 来源 | 常驻数量 |
| --- | --- | --- | --- | --- |
| 1 | `frontend`：Vue 静态页面与 API 代理 | `insightweave/frontend:<release_id>` | 项目 Dockerfile，多阶段构建 | 1 |
| 2 | `metaweave`：平台管理、采集及发布 API | `insightweave/metaweave:<release_id>` | 项目 Dockerfile | 1 |
| 3 | `nl2sql`：问数 API、Agent 与历史 | `insightweave/nl2sql:<release_id>` | 项目 Dockerfile | 1 |
| 4 | `postgres`：平台和问数持久化 | `postgres:18.1` | 官方镜像，首版候选 | 1 |
| 5 | `neo4j`：表与字段关系图 | `neo4j:5.26.19-community` | 官方镜像，首版候选 | 1 |
| 6 | `milvus`：向量知识 | `milvusdb/milvus:v2.5.2` | 官方镜像，首版候选 | 1 |
| 7 | `attu`：Milvus 管理界面 | `zilliz/attu:v2.5.10` | 上游镜像，固定引用见 doc 13.1 §2.3 | 1 |
| 合计 | 七个部署服务 | **七种运行镜像** | **三个自建、三个数据库、一个管理工具** | **七个常驻容器** |

数据库候选版本来自当前已使用的服务版本，不借容器化顺便升级。实施时先验证标签存在及运行兼容性，记录实际 image ID / digest；若不可用，明确提出版本调整，不改用 `latest` 或悄悄升级大版本。Python、Node、Nginx、uv 构建基线也应固定版本或 digest。

### 3.1 Milvus 保持一个容器

使用 standalone、内嵌 etcd、本地对象存储与 RocksMQ，数据统一落在 `/var/lib/milvus` 持久卷。配置要点：

```yaml
# 设计片段，不是完整 compose.yaml
environment:
  ETCD_USE_EMBED: "true"
  ETCD_DATA_DIR: /var/lib/milvus/etcd
  ETCD_CONFIG_PATH: /milvus/configs/embedEtcd.yaml
  COMMON_STORAGETYPE: local
command: ["milvus", "run", "standalone"]
```

`deploy/milvus/user.yaml` 固定 `mq.type: rocksmq`；etcd 配置和 user.yaml 只读挂载。该方式依据 [官方内嵌部署脚本](https://github.com/milvus-io/milvus/blob/v2.5.2/scripts/standalone_embed.sh) 及 [同版本配置](https://github.com/milvus-io/milvus/blob/v2.5.2/configs/milvus.yaml)。注意该 tag 内历史脚本的镜像字面值仍指向 beta，**只参考配置机制，不能原样执行脚本或照抄 beta 镜像**。

首个部署技术验收必须验证候选 `v2.5.2` 的启动、建临时库/集合、写入/检索及重启持久性。未通过就先解决这个前提或向用户说明，不用额外的 etcd/MinIO 容器改变七容器约定。

### 3.2 PostgreSQL 一个实例、两个逻辑库

首版沿用现有命名：

| 逻辑库 | 用途 | 应用配置 |
| --- | --- | --- |
| `metaweave` | 数据源、工作区、采集/发布作业及业务元数据 | 两个后端共同使用 `PLATFORM_DB_*` |
| `lg_persist` | NL2SQL checkpoint、Store、QA、运行及结果快照 | NL2SQL 使用 `LANGGRAPH_DB_*` |

两个库由同一个 `postgres` 容器提供，容器间地址均为 `postgres:5432`。本方案选择 Milvus，平台 PG 不额外引入 pgvector。业务源库如当前 `orders` 不由此容器负责创建或复制。

## 4. Windows、网络和文件路径

```text
Windows 电脑
├─ GitHub / 本机代理网络连接
├─ Debian WSL2
│  ├─ self-hosted Runner：接收 GitHub 任务
│  ├─ Runner 独立 checkout：测试和构建源码
│  └─ 本地部署目录：配置、Compose 快照、发布记录
└─ Docker Desktop（同一个 Linux 引擎）
   └─ frontend / metaweave / nl2sql / postgres / neo4j / milvus / attu
```

- 代码 checkout 与 `/opt/projects/insightweave` 开发目录分开，发布不修改开发工作区。
- 设置本机 `INSIGHTWEAVE_DEPLOY_ROOT`，建议位于 WSL 文件系统，例如 `/home/paul/insightweave-runtime`。密钥与运行配置放在此目录，不能放入会被 Actions 清理的 checkout。
- 部署命令使用 `docker --context default`；构建明确 `--builder default`，不安装第二套 Docker。
- 使用稳定 Compose 项目名 `insightweave-local`，每次发布都指向相同数据库卷。
- 首版主浏览器入口为 `http://localhost:18080`，同时支持 `http://127.0.0.1:18080`，宿主绑定 `127.0.0.1:18080`。若已被占用，明确调整部署配置，不停止原进程。
- 仅映射下表的五个宿主端口；MetaWeave、NL2SQL 和 Milvus 服务端口保持内部访问。部署前检查这些宿主端口是否被占用；若冲突明确报告，不停止现有服务或静默换端口。
- 用户确认：本次 Docker 部署开发和测试过程中，会预先停止原有 Neo4j、Milvus 及其 Attu 管理容器，以及原有 Vue 前端、MetaWeave 和 NL2SQL 两个后端服务，不与新 Compose 同时运行，避免这些原有服务造成端口冲突；停止操作由用户完成，部署脚本仍保留端口占用预检。
- 外部业务库使用原配置及原登记记录中的 Host:Port，不因 Docker 部署改写为 Compose 服务名、宿主映射端口或 `host.docker.internal`；仅验证容器到原地址的可达性。若某条原地址为 `localhost`/回环 IP，它在容器内指容器自身，需明确报告并由维护者处理可达性，不能静默改写业务库配置。
- 代理需要分别验证 Runner、依赖下载和 Docker 构建网络；配置参见 §4.2，保留合理的连接重试，不在脚本里硬编码当前 WSL 网关 IP。

**宿主机端口映射清单（部署约定）：**

| Compose 服务 | 宿主机端口 → 容器内部端口 | Compose `ports` 值 | 用途 |
| --- | --- | --- | --- |
| `frontend` | **18080 → 80** | `127.0.0.1:18080:80` | Vue/Nginx；浏览器访问 `http://localhost:18080` |
| `attu` | **3000 → 3000** | `127.0.0.1:3000:3000` | Milvus 管理界面，访问 `http://localhost:3000` |
| `neo4j` | **7474 → 7474** | `127.0.0.1:7474:7474` | Neo4j Browser 网页，访问 `http://localhost:7474/browser/` |
| `neo4j` | **7687 → 7687** | `127.0.0.1:7687:7687` | Neo4j Browser 登录、执行查询所需的 Bolt/WebSocket 连接 |
| `postgres` | **6432 → 5432** | `127.0.0.1:6432:5432` | 本机数据库客户端通过 `127.0.0.1:6432` 连接平台 PG |

**端口预检实施（M01）：**`deploy.sh` 调用 `release_manifest.py ports --project <项目名>`，按上述固定五项映射核对运行容器的 Compose 项目/服务标签、实际宿主 IP/端口和容器 TCP 端口；预检表与 Compose 由同一契约用例交叉核对。只有本项目正确服务的精确回环映射可沿用，其他程序、其他项目、错误服务/容器端口及通配地址发布都拒绝，不再使用 HTTP 响应或“有任一本项目容器”作为判断依据。未匹配合法 Docker 映射时，以标准库 TCP 连接检测监听，不发送应用协议数据；WSL 同时通过 Windows PowerShell `Get-NetTCPConnection` 只读查询宿主回环/通配监听，因为实测 Windows 监听可能对 WSL 回环连接不可见。Windows 查询限时 10 秒，执行失败、超时、无效响应及 Docker/TCP 状态未知均停止，不视为空闲。bootstrap/release/实际 rollback 和自动恢复在维护、替换前检查，同版本只读核验也检查归属；失败保留版本引用，现场核验失败仍按 §7.3.1 收尾。预检不占住端口，Docker 实际绑定失败继续走既有错误处理，不新增端口管理服务或自动杀进程。Runner 要求和实测边界见 deploy/README.md 的“宿主端口预检（M01）”。

其余端口均不设置 `ports`：MetaWeave `8001`、NL2SQL `8008`、Milvus `19530/9091`、内嵌 etcd `2379`。容器间通过 `metaweave:8001`、`nl2sql:8008`、`postgres:5432`、`neo4j:7687` 和 `milvus:19530` 通信，不绕行宿主映射。Attu 设置 `MILVUS_URL=milvus:19530`，由其容器连接 Milvus，浏览器不需要访问 19530。

PostgreSQL 的宿主端口固定为 6432，容器内部仍为 5432。部署环境中的 `PLATFORM_DB_PORT`、`LANGGRAPH_DB_PORT` 仍设为 5432，配合主机名 `postgres`；只有宿主机数据库客户端使用 `127.0.0.1:6432`。不因此修改现有本地开发的数据库配置。

宿主机的 80 不被本方案占用，只有前端容器内部使用 80；现有开发环境的 5173 也不被本方案占用。Neo4j Browser 的网页来自 7474，实际数据库连接使用 7687，所以该管理界面需要两条映射；浏览器连接地址使用 `bolt://localhost:7687`，不使用仅容器内部可解析的 `neo4j` 服务名。映射全部仅绑定本机回环地址，不开放到局域网。

### 4.1 前端反向代理和现有 origin 契约

当前两个前端 API 地址配置只接受完整 origin，不接受 `/api` 相对路径。本次不重写客户端配置机制。

构建前端时把 `VITE_API_BASE_URL` 和 `VITE_NL2SQL_API_BASE_URL` 都设为 `http://localhost:18080`。Nginx 路由：

| 请求路径 | 目标 | 注意事项 |
| --- | --- | --- |
| `/api/v2/` | `http://nl2sql:8008` | 保留完整 URI；优先于通用 `/api/` |
| `/api/` | `http://metaweave:8001` | 保留完整 URI |
| Vue 页面与静态文件 | 前端 `dist/` | SPA 路由使用 `try_files` 回退 `index.html` |

代理传递浏览器 Host，NL2SQL `NL2SQL_ALLOWED_HOSTS` 允许 `localhost`、`127.0.0.1`；两个后端的 Origin 白名单包含实际浏览器 origin。Nginx 查询代理读取超时设为 600 秒，覆盖现有前端 480 秒查询超时，不能让代理默认超时先截断请求。

两个后端从首版同时放行 `http://localhost:18080,http://127.0.0.1:18080`。前端构建的两个 API origin 仍固定为 `http://localhost:18080`；用户从 `127.0.0.1:18080` 打开页面时，会跨 Origin 请求该固定 API 地址，因此同样需要这两个精确白名单，不使用通配符。未放行时，NL2SQL 的 Origin 守卫可能返回 403，MetaWeave 则可能因缺少允许头被浏览器拦截，并非两个后端都会返回 403。

用户当前的开发启动方式是：Vue `127.0.0.1:5173` 直连 MetaWeave `127.0.0.1:8001`、NL2SQL `127.0.0.1:8002`，两个后端放行 `http://127.0.0.1:5173,http://localhost:5173`。Docker 仍复用 MetaWeave 的 `--factory backend.api.logged_app:create_logged_app` 和 NL2SQL 的 `src.api.main:app` 入口，但监听 `0.0.0.0`，内部端口按本节定为 8001/8008；NL2SQL 的开发端口 8002 不必照搬为容器端口。浏览器改经 `localhost:18080` 访问，Vite 构建值和后端白名单必须一起切换，不修改当前开发服务。

采用静态 upstream 名称时，实际发布/恢复必须在后端就绪后刷新代理解析：前端新建容器会重新解析；前端容器被保留时执行 `nginx -t` 并 reload，再做入口冒烟，避免沿用被替换后端的旧 IP。保留须同时核对镜像引用与实际 image ID；标签相同但实际 ID 不符时强制重建，不能仅凭标签沿用。

部署控制路径 `/api/_deploy/` 和 `/api/v2/_deploy/` 在 Nginx 中显式拒绝，不能跟随普通 API 代理暴露给浏览器；控制调用只从容器内执行，见 §7.5。

首版使用上述主入口及回环 IP 别名。以后修改端口或增加其他 origin，要同步前端构建参数、端口映射和后端白名单，再构建新的应用版本，不能只改 Compose 端口。

<a id="runner-proxy"></a>

### 4.2 GitHub 访问与本机代理备注

当前 Windows 已有可用的本机代理。开发或 Runner 接入时若无法直接访问 GitHub，先按本机代理配置重试，不必因此改换 CI/CD 方案，也不绑定某个代理软件。

**已解析的当前代理配置（本机 WSL 环境的验证记录，不是脚本默认常量）：**

| 项目 | 实际值 |
| --- | --- |
| Windows 宿主代理 IP | **172.29.128.1** |
| 代理端口 | **7890** |
| HTTP / HTTPS 请求使用的代理 URL | **`http://172.29.128.1:7890`** |
| 脚本中的 SOCKS5 代理 URL | `socks5://172.29.128.1:7890`（Runner 优先使用上面的 HTTP 代理） |

这里的 IP/端口是用户要求保留的本机验证记录，不是凭据。将来仓库转公开前复核是否应将此类机器网络信息替换为通用示例；真正的运行配置及凭据仍只放在不受 Git 管理的本机文件中。

当前实际地址已经列出，无需读取个人脚本。把确认后的值保存到不受 Git 管理的本机配置变量 `INSIGHTWEAVE_WSL_PROXY_URL`，构建阶段另用 `INSIGHTWEAVE_BUILD_PROXY_URL`；项目模板只声明变量名和空值。脚本读取并验证配置，失效时明确失败，不自动猜地址或改系统代理。启用 WSL 代理时，在已加载本机配置的终端执行：

```bash
: "${INSIGHTWEAVE_WSL_PROXY_URL:?请先配置本机 WSL 代理 URL}"
export http_proxy="$INSIGHTWEAVE_WSL_PROXY_URL"
export https_proxy="$INSIGHTWEAVE_WSL_PROXY_URL"
export HTTP_PROXY="$http_proxy"
export HTTPS_PROXY="$https_proxy"
export no_proxy="localhost,127.0.0.1,::1,postgres,neo4j,milvus,metaweave,nl2sql,attu,host.docker.internal"
export NO_PROXY="$no_proxy"
```

上述 IP 是按 `/home/paul/set_proxy.sh` 的实际规则，从当前 WSL 默认路由解析得到的；端口取自脚本配置。WSL 网络重建后 IP 可能变化，届时再核对默认网关并更新本机配置，不把该地址写死进应用镜像。

本次显式指定 `http://172.29.128.1:7890` 访问 `https://github.com`，实际返回 HTTP 200；这只验证当前 WSL 的该次访问，Runner 运行账号和 Docker 网络仍按各自接入步骤核验。

Runner 必须在注册、启动**之前**设置这些标准代理变量；不能等工作流开始才设置，也不能只提供项目自定义变量、假定 Runner 会自动映射为 `http_proxy`。服务方式的操作顺序：

1. 在 Runner 自己的安装目录 `.env` 或其服务启动环境中配置代理。`.env` 使用 `key=value`、不写 `export`；将已核验的本机变量展开成实际值，不依赖该文件自动做 shell 插值。以下仅演示当前本机文件内容，文件不入 Git，地址变化后更新：

   ```dotenv
   INSIGHTWEAVE_WSL_PROXY_URL=http://172.29.128.1:7890
   http_proxy=http://172.29.128.1:7890
   https_proxy=http://172.29.128.1:7890
   HTTP_PROXY=http://172.29.128.1:7890
   HTTPS_PROXY=http://172.29.128.1:7890
   no_proxy=localhost,127.0.0.1,::1,postgres,neo4j,milvus,metaweave,nl2sql,attu,host.docker.internal
   NO_PROXY=localhost,127.0.0.1,::1,postgres,neo4j,milvus,metaweave,nl2sql,attu,host.docker.internal
   ```

2. 项目的构建代理和 CI Python 路径也须从本机配置传入 Runner/工作流环境，具体见 doc 13.1；安装目录 `.env` 与两个应用的运行 env 是不同文件，不能混用。
3. 对**已经注册且已安装 systemd 服务**的 Linux Runner，在其安装目录、Runner 空闲时执行：

   ```bash
   sudo ./svc.sh stop
   sudo ./svc.sh start
   sudo ./svc.sh status
   ```

   首次接入须先按 GitHub 仓库给出的指令注册并安装服务；没有采用服务方式时，在上述标准变量已导出的同一环境执行 `./run.sh`，变更代理后重新启动该进程。不能同时启动服务和另一份手工 Runner。
4. 仅检查服务状态、GitHub 显示的 Runner Online 和必要的脱敏连接诊断；不要在 Actions 日志输出 `.env` 全文或带认证信息的代理 URL。确认新启动的 Runner 能接收任务，不能只以交互终端 curl 成功替代。

依据：[GitHub Runner 代理配置](https://docs.github.com/en/actions/how-tos/manage-runners/use-proxy-servers)、[Linux Runner 服务管理](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/configure-the-application)。本次不实际注册或重启服务。

本地服务和冒烟请求按上述绕过名单直连。用 Runner 实际运行账号验证 GitHub/Actions 访问；Docker 镜像拉取和构建下载单独验证，不能假定它们自动继承当前 shell 的代理。

## 5. 配置、镜像内容与持久化

### 5.1 三个应用镜像

具体 FROM、Debian 版本及包清单按 [doc 13.1 §3–6](13.1_镜像与依赖安装明细.md) 实施：两个后端使用 Debian 12 Bookworm 的 Python 基础镜像；前端用 Debian 12 的 Node 阶段构建、Debian 13 Trixie 的 Nginx 阶段运行。不能仅凭本文的 Python/Node 大版本自行选择未固定的系统镜像。

- `frontend/Dockerfile`：Node.js 22 阶段执行 `npm ci` 和 `npm run build`；最终 Nginx 阶段只保留静态产物及代理配置，作为一个前端运行镜像。
- `metaweave/Dockerfile`：Python 3.12、uv 固定版本；按 `uv.lock` 安装运行依赖及 `platform` extra。保留 `/app/metaweave` 源码、配置、SQL、脚本及项目根定位文件；工作目录为 `/app/metaweave`。
- `nl2sql/Dockerfile`：Python 3.12、uv 固定版本；按本模块 `uv.lock` 安装运行依赖。保留 `/app/nl2sql/src`、提示词、Skill、embedding release 配置和项目文件；工作目录为 `/app/nl2sql`。
- 两个后端使用独立 Linux 虚拟环境，不能复制宿主 `.venv`。保持现有源码路径和安装方式，使 `__file__`、`get_project_root()` 能正确定位配置；不同时做包结构改造。部署支持模块复制到 `/app/deploy/runtime/`，契约文件复制到 `/app/deploy/contract.json`，容器 `PYTHONPATH` 包含 `/app`；两个 API 仅在部署开关启用时加载它，默认开发启动不依赖额外路径。
- 构建 context 为仓库根目录，由根 `.dockerignore` 排除 `.git`、`.env*` 实值、`.venv*`、`node_modules`、日志和运行产物；保留安全模板及受 Git 跟踪的必要配置模板。

完整镜像首次构建前，先完成 §10 的任务 2A：真实的部署运行时代码、API 接线、初始化/冒烟脚本及契约文件必须已存在。两个后端显式复制 `deploy/runtime/`、`deploy/contract.json`、`deploy/scripts/init_platform.py` 和 `deploy/scripts/smoke.py`；其中 `smoke.py --scope infra` 要在容器内执行，也不能留到镜像构建之后才实现。`ci.sh`、`deploy.sh` 留在本机 Runner 执行，不作为镜像运行依赖；不复制整个 deploy 目录掩盖文件依赖。

现有启动入口直接复用，调整监听地址及优雅退出参数：

```text
MetaWeave：uvicorn backend.api.logged_app:create_logged_app --factory --no-access-log --host 0.0.0.0 --port 8001 --timeout-graceful-shutdown 120
NL2SQL：uvicorn src.api.main:app --host 0.0.0.0 --port 8008 --timeout-graceful-shutdown 120
```

首版每个后端单进程运行，不增加 Uvicorn workers 或额外作业 worker。

### 5.2 本机配置

部署目录保存两个后端的独立环境文件和一份部署配置，模板入库、实值不入库。重点配置：

- `PLATFORM_DB_*` 与 `LANGGRAPH_DB_*` 正确指向同一 PG 容器中的不同逻辑库。
- 两个后端使用相同的 `SOURCE_CREDENTIAL_KEY`，该密钥在容器替换后保留，不每次生成。
- `NEO4J_*`、`MILVUS_*` 指向容器服务名，两个后端知识库名称一致。
- 首版两个浏览器地址对应三个显式配置：MetaWeave 的 `PLATFORM_CORS_ORIGINS=http://localhost:18080,http://127.0.0.1:18080`；NL2SQL 的 `NL2SQL_CORS_ORIGINS=http://localhost:18080,http://127.0.0.1:18080`、`NL2SQL_ALLOWED_HOSTS=localhost,127.0.0.1`。Origin 值含协议和端口、无尾斜杠；Allowed Hosts 仅填主机名、不含协议或端口。NL2SQL 默认 CORS 仍是开发地址 `http://127.0.0.1:5173`，不能依赖默认值；携带未放行 Origin 的请求会被 `LocalOriginGuard` 返回 403。无 Origin 的请求可放行，因此只做普通 curl 冒烟可能漏掉该错配。Allowed Hosts 当前默认已含这两个主机，仍在模板中显式固定以便核对。
- LLM / Embedding 凭据只在运行时注入，不作为 Dockerfile `ARG`、不复制进镜像；构建和离线测试不需要真实密钥。
- 七个服务运行时均直连，不使用代理。代理仅用于 Runner 访问 GitHub、镜像拉取、确有需要的构建下载及部署准备阶段的外部插件文件下载；Neo4j 优先从固定镜像离线提取内置 APOC，见 doc 13.1 §2.2，不增加首次启动代理例外。Compose 对七服务显式将 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 及其小写形式设为空字符串，覆盖 Docker CLI 可能注入的容器代理，不把宿主代理继承到服务中。不为运行环境新增 SOCKS 依赖。
- Docker 部署显式启用 `DEPLOY_CONTROL_ENABLED=1`，配置独立随机 `DEPLOY_CONTROL_TOKEN`，并只读挂载部署目录中的 `control/` 到 `/run/insightweave-deploy/`，供 §7.5 的启动维护标记、控制与只读冒烟使用。令牌不复用数据源加密密钥、不下发前端；启用但缺少必要配置时启动失败，不能静默放开。普通开发启动默认不启用部署门闩。
- 配置加载器会整体解析 YAML 中的变量；即使启动时未使用某个模型或业务库字段，也须按现有配置补齐必填变量。部署预检只报告缺失的变量名，不输出实值。
- 不手工 `source` 未转义的密码文本；脚本分别使用 Compose env-file 和应用环境注入，不能混淆 Compose 插值变量与容器环境变量。

首版不支持在自动发布中悄悄更改数据库版本、数据库名称、持久卷名称或入口地址；这些属于显式部署配置变更。

**连接配置分界：外部业务库保持原 Host:Port；本项目内部服务仅在独立 Docker 部署环境文件中改用 Compose 服务名和内部端口。**不修改本地开发 `.env` 或已登记业务数据源的连接记录，尤其不能批量将所有 `DB_HOST`/`DB_PORT` 改成平台库地址。

| 连接目标 | Docker 环境中的主机/端口 | 配置落点及逻辑库 |
| --- | --- | --- |
| 外部业务库 | 原 Host:Port，保持不变 | 原 `DB_HOST`、`DB_PORT` 或平台登记的源连接信息；业务库名保持原值 |
| MetaWeave 平台 PG（两个后端共享读取） | `postgres:5432` | `PLATFORM_DB_HOST=postgres`、`PLATFORM_DB_PORT=5432`、`PLATFORM_DB_NAME=metaweave` |
| NL2SQL 持久化 PG | `postgres:5432` | `LANGGRAPH_DB_HOST=postgres`、`LANGGRAPH_DB_PORT=5432`、`LANGGRAPH_DB_NAME=lg_persist` |
| Milvus（两个后端共用） | `milvus:19530` | `MILVUS_HOST=milvus`、`MILVUS_PORT=19530`、`MILVUS_DATABASE=nl2sql`；双方保持同一知识库名 |
| Neo4j（两个后端共用） | `bolt://neo4j:7687` | `NEO4J_URI=bolt://neo4j:7687`、`NEO4J_DATABASE=neo4j` |

上述内部服务的用户名、密码等从对应 Compose 服务的本机实值配置取得并保持一致，不在文档写凭据。宿主 `6432` 只供本机客户端访问新平台 PG，既不用于容器内部平台库连接，也不用于替换原业务库端口。任务 3 分别验证“内部服务按上述地址连通”和“外部业务库原地址可达”，不把两类连接混为一项。

### 5.3 持久化位置

| 内容 | 挂载位置建议 | 发布时处理 |
| --- | --- | --- |
| PostgreSQL 数据 | 命名卷挂载到所选 PG 18 镜像要求的父数据目录 `/var/lib/postgresql` | 保留；实施时核对该 tag 的 `PGDATA`，不沿用旧大版本路径猜测 |
| Neo4j 数据、日志及插件 | 官方镜像 `/data`、`/logs`、`/plugins` 对应卷 | 保留 |
| Milvus etcd、RocksMQ、本地对象数据 | `/var/lib/milvus` 命名卷 | 保留 |
| MetaWeave 作业产物 | `/app/metaweave/output` 命名卷 | 保留，不被新镜像覆盖 |
| MetaWeave 日志 | `/app/metaweave/logs` 命名卷 | 保留 |
| NL2SQL 会话文件及结果 | `/app/nl2sql/var/db_deepagent` 命名卷 | 保留，`DB_DEEPAGENT_WORKSPACE` 指向该位置 |
| NL2SQL 日志 | `/app/nl2sql/logs` 命名卷 | 保留 |

与代码一起维护的模板、提示词和默认配置留在镜像中；运行环境文件保存在部署目录。首版不挂载整个源码目录覆盖镜像，也不把采集产物打包进镜像。

### 5.4 重启、资源和日志

- 七个服务使用 `restart: unless-stopped`；`unhealthy` 本身不会触发 Docker 自动重启，发布脚本仍须主动检查健康结果。首次初始化失败时停止发布并保留现场，不靠反复重启修复缺表。
- 两个后端设置 `stop_grace_period: 150s`，大于 Uvicorn 的 120 秒退出等待。后者约束服务器任务等待，不是所有 lifespan/线程关闭的硬上限；二者是排空后的退出余量，不能代替 §7.5 的替换前排空。
- 健康检查初始值：应用 `start_period: 60s`，数据库 `start_period: 120s`，`interval: 10s`、`timeout: 5s`、`retries: 6`；实施时按固定镜像实际冷启动校准，不无限等待。
- Milvus 固定镜像已验证自带 `/usr/bin/curl` 7.81.0，Compose 直接使用它检查容器内 `http://127.0.0.1:9091/healthz`，命令及验证边界见 doc 13.1 §2.1；不依赖未经核实的 wget 或为探针重打数据库镜像。任务 1 仍须验证真实服务的响应与退出码。
- Attu 沿用上游运行入口，Compose healthcheck 使用镜像已有 Node 发起容器内 3000 的 HTTP 请求，明确命令见 doc 13.1 §2.3；镜像没有 curl/wget 或内置 Healthcheck，不依赖这些工具。该探针只证明页面服务可达，到 `milvus:19530` 的实际连接由任务 3 单独验收，不为管理界面增加探测服务。
- 当前只读核实 Docker 引擎可见约 31.3 GiB 内存、12 CPU。内存上限初值为前端 256 MiB、MetaWeave 2 GiB、NL2SQL 3 GiB、PG 2 GiB、Neo4j 3 GiB、Milvus 4 GiB、Attu 512 MiB；CPU 上限分别为 1/2/2/2/2/4/1。上限不是预留，允许 CPU 超配；数据库内部缓存、Neo4j heap/page cache 也要低于各自容器上限。构建串行执行，实测运行和构建峰值后再调整，不能把这些初值当成性能验收结论。
- Docker 输出使用 `json-file` 的 `max-size: 10m`、`max-file: "3"`。应用文件日志复用现有配置：MetaWeave 每文件 10 MiB、5 份备份；NL2SQL 按日轮转、保留 30 份。后者不是容量硬上限，README 要列出检查卷占用及处理过大日志的步骤，不再增加一套日志平台。
- 部署日志每次发布单独保存，首版保留最近 20 次；current、previous 的清单和快照不随日志删除。日志及人工保留资料均不作为成功验收或回退资格的证明。

### 5.5 备份和恢复边界

保留卷只防止正常替换容器时丢数据，不等于已有备份。首版不开发自动备份服务，但 `deploy/README.md` 必须给出手动操作步骤：

1. 进入维护并排空应用后暂停应用写入；对 PG 的 `metaweave`、`lg_persist` 分别做逻辑备份，记录镜像版本。两个 dump 不天然构成跨库一致快照，应在同一停止写入窗口完成。
2. 对 Neo4j、Milvus 采用固定版本支持的离线方式，停止相应数据库后复制其完整数据卷；同时备份应用产物/结果卷。不能直接打包正在写入的数据库目录冒充一致备份。
3. 独立、安全地备份本机配置和 `SOURCE_CREDENTIAL_KEY`，不放进 Git、Actions artifact 或普通部署日志；密钥丢失会使已有数据源密码不可解密。
4. 备份放在 Docker/WSL 数据目录之外；用相同镜像版本恢复到独立项目名、独立卷和临时端口，验证库表、历史、结果文件及解密能力，禁止覆盖现有卷演练。

实际恢复演练完成前交付记录标记“恢复未验证”。自动定期备份、异地容灾及定期演练调度不作为首版发布阻塞项，不宣称已具备这些能力。

**M02 文档收尾（2026-11-18）：**`deploy/README.md` 已补齐维护锁/排空、两库逻辑备份、Neo4j/Milvus 完整冷卷、APOC 与两个应用文件卷、配置/原密钥和实际应用镜像的配套备份，以及固定 Compose/env/项目参数的原服务重启和独立环境恢复顺序。演练必须单独修改固定 external APOC 卷名，不能只换项目名；前端构建 origin 也不能因临时端口改变而误连生产 API。复用已锁定 PG 镜像中的工具，不引入备份平台或新增常驻服务。此处只完成操作说明，未执行真实备份、恢复演练或数据解密验收。

## 6. 首次部署和初始化

首次安装与日常发布是两个明确入口：

```text
首次安装 bootstrap
→ 检查配置、镜像和 Docker 引擎
→ 识别已有容器：没有 current 也可能是中断后的重试
→ 原子记录 phase=deploying，写维护标记
→ 若有运行中的应用，先 enter 并等待 HTTP/future 排空
→ 首次启动 Neo4j 前离线准备 APOC 插件卷；已有 Neo4j 仅核验，不改运行中的插件
→ 复用或启动 postgres / neo4j / milvus，等待基础服务可用
→ 显式核对/幂等补齐两个目标 PG 库，不依赖入口脚本自动续跑
→ 复用或启动 metaweave / nl2sql（新建时仅内部网络，暂不开放前端）
→ 在已有容器中 exec 执行初始化及校验
→ 应用就绪检查通过
→ 启动或核验 attu，检查页面及到 milvus:19530 的连接
→ 启动 frontend，维护令牌冒烟通过
→ 移除标记、确认两个后端 leave、无令牌普通冒烟通过
→ 写入首次成功部署记录，phase=ready
```

初始化通过 `docker compose exec -T` 在现有容器内执行，不新增 init 服务、容器镜像或常驻进程。后端启动接口和存储是现有的惰性机制；首次数据库空表时可能出现 MetaWeave 启动恢复扫描的缺表日志，初始化和最终验证完成前不能据此宣布部署成功，也不在此阶段开放前端。

Compose 的后端启动依赖只等待基础服务可连接，不能反过来要求项目表已建好才允许启动用于初始化的后端容器。bootstrap 先启动两个 API 容器并执行初始化，随后再等待完整应用就绪，避免初始化与健康检查互相等待。

**bootstrap 重试按实际容器处理，不能用“没有 current”推断没有任务。**例如首次普通冒烟已通过、写成功记录前中断，应用可能已经开放。重试只要涉及停止/替换已有应用或执行初始化，就与 release/rollback 一样：预检后记录 deploying，写维护标记、enter、等待已有 HTTP/future 排空，再进行变更。尚不存在或 Docker 已确认退出的容器无需排空；仍运行但控制不可达则转人工。已有匹配的数据库容器和数据卷继续复用，不在重试时默认重建。

若三个应用的实际镜像、契约、已初始化对象均与目标 R 匹配，且无维护标记、门闩已开放，只需重新核验普通请求并补写首次成功记录，可保留全部容器，不强制重建或排空。此分支不执行初始化、不修改服务配置；任何前提不满足就回到上述受控变更流程，不能借“仅核验”解除未知维护状态。

### 6.1 PostgreSQL 与 MetaWeave 表

`deploy/postgres/01-create-databases.sh` 由官方 PG 镜像首次空数据卷初始化执行，创建 `metaweave`、`lg_persist` 两个逻辑库；脚本幂等，存在则保留，只创建明确配置的缺失库，不删除、清空或改造已有库。

官方入口脚本中途失败后，已经初始化的数据目录在重启时不会自动续跑剩余脚本。因此 bootstrap 在 PG 最终 TCP 服务就绪后，显式通过 `exec` 再运行同一建库脚本并检查两库均存在；不另写第二份建库逻辑。PG 启动健康检查使用 TCP 回环及已有管理库，避免把 initdb 阶段只监听 Unix socket 的临时服务当作正式就绪。该补齐仅用于明确的 bootstrap/重试，日常 release 仍只检查、不创建库。依据：[PG 官方初始化说明](https://hub.docker.com/_/postgres#initialization-scripts)。

**同一脚本必须自包含。**官方入口对可执行 `.sh` 直接执行，对不可执行 `.sh` 使用 source；bootstrap 则通过 `docker compose exec -T postgres bash /docker-entrypoint-initdb.d/01-create-databases.sh` 重入，没有入口函数上下文。脚本直接调用 `psql`，自行读取/校验 `POSTGRES_USER`、`POSTGRES_PASSWORD` 等所需变量，默认管理库使用 `postgres`；不得依赖 `docker_process_sql`、入口脚本定义的 `psql` 数组或其他未导出的函数，也不能用 `exec psql` 替换被 source 的官方入口进程。初始化阶段的临时 PG 只监听 Unix socket，脚本连接需兼容该场景，不强制 TCP；bootstrap 等待最终 TCP 就绪后，脚本本身仍可使用容器内 Unix socket。测试必须覆盖入口首次执行和独立 Bash 重入两种上下文。

随后，项目部署初始化脚本读取镜像内已有的 `metaweave/db/rebuild_platform_schema.sql`，在 `metaweave` 库执行：

- 平台目标对象全部不存在时，在一个显式 PostgreSQL 事务内执行整份 DDL 并核验，成功才提交；失败整体回滚。现有 SQL 没有 `CREATE DATABASE` 或 `CREATE INDEX CONCURRENTLY` 等需要移出事务的语句；创建两个逻辑库仍由 PG 入口脚本单独完成。
- 目标对象完整且契约校验通过时允许重复 bootstrap，不重建、不清空。
- 若提交结果因断连不明确，重试先重新核验：完整则保留，全部缺失则重试事务。来源不明的部分对象或结构冲突停止并说明，不执行删库或自动改造旧结构。

直接复用静态建表 SQL；`manual_reset_platform_pg.py` 和 `rebuild_platform_schema.py` 的旧对象改名、整库重置用途不接入自动发布。已存在的空 `public` schema 本身不算“平台对象已创建”。

PG 官方镜像的入口初始化脚本只在空数据目录运行，后续表升级不能指望它自动重新执行。

### 6.2 NL2SQL 持久化表

空环境仍需要创建 NL2SQL 的持久化表。正常 Web 启动只校验，不会自行删除重建，代码依据为 `nl2sql/src/api/v2/runtime.py:_build_store()`。

在 `nl2sql` 容器执行部署初始化脚本，构造现有 `PlatformDbConfig`，复用 `persistence.platform_pool.init_platform()`：创建 `db_deepagent` schema、checkpoint / Store 表及结果快照表，不重新抄写 SDK 表结构。参数仅从持久化连接配置构造，不因首次尚无业务数据源而要求执行完整业务源校验或查询模型。

该入口本来就是幂等初始化，不套用 MetaWeave 的“部分对象一律拒绝”规则。bootstrap 中断后，若现有对象可确认为本版本支持的 SDK/项目结构，可重新执行原入口补齐缺失对象并复核；来源不明、字段/类型冲突或不支持的结构版本停止。不要把整个 SDK setup 强行包入一个外部事务，也不要绕过它自行补表。测试需覆盖原入口中断后的真实重入，而非仅给完成标记置位。

不重复开发持久化框架，不在每次应用启动中偷偷调用重置；日常发布只检查这些对象存在且可使用。

### 6.3 Milvus 和 Neo4j

- Milvus：复用现有客户端 `connect(create_database=True)` 创建目标逻辑库，存在时只核对。集合结构及索引继续由 MetaWeave 发布流程创建，部署脚本不预造一套可能与 Embedding 维度冲突的集合。
- `manual_reset_platform_milvus.py` 会删除库和集合，不能作为每次发布的初始化入口。
- Neo4j：官方镜像创建配置账户和默认库；按 [doc 13.1 §2.2](13.1_镜像与依赖安装明细.md) 在启动前从固定镜像提取内置 APOC Core，校验后放入 `/plugins` 卷。正式 Compose 设置 `NEO4J_PLUGINS: ""`，显式配置 `NEO4J_dbms_security_procedures_unrestricted: "apoc.*"`，不在服务启动时在线安装。首次启动及重启均验证插件 SHA、APOC 版本和应用需要的过程，运行期间无代理；插件缺失/冲突在启动前失败。现有实体身份约束由既有 CQL 生成/发布流程创建，不运行清图或历史身份迁移脚本。
- 数据库刚初始化、知识尚未发布时，服务可以正常管理数据源和工作区，但某个源的问数 readiness 可以合理地提示知识未就绪。不能为了让空环境验收通过而自动调用 LLM 采集或伪造发布记录。

## 7. 日常 CI/CD 发布流程

### 7.1 GitHub 触发与本地执行

> **2026-11-24 更新（doc 15 取代本节的触发方式）**：`local-release.yml` 已停用并删除，公开仓不再注册本机 Runner，也没有手动触发入口。现行链路是：PR 合并到公开仓 main → 云端 CI（五个套件加 `ci-required`）成功 → `notify-deploy.yml` 请求私有部署仓 → 私有 `deploy.yml` 的 verify（托管）→ release（本机 Runner，先复核再检出该源码提交）→ finalize。重新发布只按 doc 15 §8.3 的完整重跑命令。下面流程图中“独立 checkout”及其后的步骤仍是 release 阶段的实际内容；操作说明以[部署手册](../deploy/README.md)为准。

首版使用发布分支 `main`，提供 `workflow_dispatch` 手动重发。现有开发分支不会未经用户安排就被改名或合并。

```text
可信代码推送 main / 手动触发 main
→ GitHub 分配任务给本机 Runner
→ 独立 checkout 精确到该任务的 commit
→ 环境及部署配置预检
→ 运行三个模块的离线检查
→ 本地构建三个应用镜像
→ 候选镜像离线导入、配置和启动预检
→ 保存该版本非敏感 Compose 配置、镜像身份及源码 SHA
→ 检查已初始化的数据库服务
→ 原子记录 phase=deploying（早于任何维护标记和入口变更）
→ 进入部署维护、等待在途请求和后台任务排空；超时取消替换
→ 更新两个后端并验证，再更新前端
→ 维护期间携带控制令牌，经前端入口完成读取冒烟
→ 移除维护标记，确认两个后端 leave 成功
→ 不带控制令牌，经前端执行普通请求冒烟
→ 成功后原子更新 current / previous，phase=ready
→ GitHub 展示成功或失败及脱敏日志
```

第一次安装另行执行 `bootstrap`；自动 push 发布不隐式初始化或重建数据库。没有已成功 bootstrap 的环境时，日常发布明确失败，并提示首次安装入口。

### 7.2 CI 检查内容

- 两个 Python 模块在独立 checkout 使用各自锁文件及独立虚拟环境；CI 显式使用本机 `INSIGHTWEAVE_CI_PYTHON` 指向的 Python 3.12，校验绝对路径和版本并禁止 CI 自动下载解释器，命令见 doc 13.1 §5.4。MetaWeave 安装 `platform`、`dev`，NL2SQL 安装 `dev` extra（`--extra dev`），不修改锁文件。
- MetaWeave 执行 `tests/unit` 的离线套件；实施时核实其外部依赖隔离，不将历史手工作业脚本纳入自动测试，不靠真实数据库或模型跑通。
- NL2SQL 受维护离线清单至少包含 `src/tests/db_deepagent`、`src/tests/unit/api`、`src/tests/unit/services`、`tests/unit/db_deepagent/test_independent_summarizer.py`，不传 `--run-db`、`--run-llm`。在 `ci.sh` 显式列出路径，并将已知历史套件排除原因写入 README；这些目录的存在已核实，新增范围是否全绿仍需实际执行，不虚报通过。
- **额外显式执行 `deploy/tests/` 公共离线套件**，它不在两个后端现有 pytest 默认收集目录内，不能因后端套件全绿就跳过部署测试。公共套件在 NL2SQL 测试环境运行并配置仓库根导入路径；两个 API 的实际接线回归分别放入其所属后端的受维护套件，不在一个测试进程中混装两套项目依赖。Docker/外部数据库边界用替身，真实联调不混入默认离线 CI。
- 前端按锁文件 `npm ci`，执行全部现有 `.test.cjs` 和 `.test.ts` 测试、类型检查；使用 Node 22.18 及以上的 22.x，按 [doc 13.1 §6.1](13.1_镜像与依赖安装明细.md) 的 NUL 分隔枚举命令执行 `node --test`。该版本范围默认支持类型剥离，无需强制添加实验参数；当前没有 `npm test` 脚本，`ci.sh` 直接使用已验证命令，拒绝空测试清单并记录 Node 版本。
- 正式前端产物在 Dockerfile 中再次按冻结输入构建，最终镜像不依赖宿主已有 `dist/`。
- 失败即停止，不能为通过流水线删除失败用例；网络下载可有限重试，业务测试失败不盲目重试。
- 手动构建同样要求可追溯：`ci.sh` 默认拒绝 tracked 修改、暂存修改及未跟踪源码，不提供跳过开关。先用 `git ls-files --others --exclude-standard` 检查，再从指定 SHA 的干净独立 checkout 构建；构建上下文以该提交的受跟踪文件为准，不吸入本地被忽略的 `.env` 等文件。
- 三个应用镜像替换前进行最小预检：静态文件/代理配置及固定 API origin、Python 导入、必需配置/Skill 路径、配置解析、启动入口和本机环回健康请求。临时预检容器使用候选应用镜像、无宿主端口、无现有数据卷及外网连接；MetaWeave 的启动恢复扫描在此步骤关闭。它能发现打包/入口错误，不能代替真实数据库联调；不建设并行候选集群。
- 前端独立预检时，Nginx 的静态 upstream 名称仍须可解析。使用 `--network none --add-host metaweave:127.0.0.1 --add-host nl2sql:127.0.0.1`，先执行 `nginx -t`，再启动 Nginx 检查本机环回静态页/资源及 SPA 回退；不把未启动的后端当作可用服务，不在这一步请求 API 代理。真实服务名解析和 API 连通性由七服务联调及发布冒烟验证。这样不需要为包装检查启动一套三应用临时集群。依据：[Docker hosts 映射](https://docs.docker.com/reference/cli/docker/container/run/#add-host)、[Nginx 配置检查](https://nginx.org/en/docs/switches.html)。

此处测试结果是待实施要求，不沿用历史 1364 / 58 等数字冒充 CI 已运行。实际报告记录当前收集数、跳过原因和结果。

### 7.3 版本和本机发布目录

应用版本统一使用 `release_id = <commit前12位>-<run_id>-<run_attempt>`，三个应用使用同一 release_id；本机手动构建用独立时间戳后缀。每次发布还保存完整源码 SHA 和三个实际 image ID，不能只写 `latest`。

```text
INSIGHTWEAVE_DEPLOY_ROOT/
├─ config/                  # 本机密钥、运行环境；不入 Git
├─ releases/<release_id>/   # 该版本 Compose、相关配置快照和镜像清单
├─ state/deployment.json    # 一份文件记录 current、previous、phase、最近操作结果
├─ control/maintenance     # 仅维护期间存在；应用只读，替换/重启时仍保持维护
└─ logs/                    # 部署日志，不写凭据
```

快照只包括 Compose 模板、其引用的非敏感相对配置、源码 SHA、镜像 ID/digest 和非敏感配置版本/哈希。部署时显式固定 env-file、项目名和路径，不能依赖 Runner 当时的 cwd。命名卷使用稳定显式名称或稳定项目级命名，不能随 `releases/<release_id>` 改变。

**实施落点（问题 3 补充）：**`ci.sh` 在候选预检全部通过后写入真正的 JSON 候选清单，记录 release_id、完整源码 SHA、deployment_contract_version 和三个应用的镜像引用/实际 image ID；工作流显式传递清单路径。`snapshot_release()` 从该清单取应用身份，不从可能过期的 deploy.env 复制应用标签，并在维护前核验清单与当前 checkout、本地镜像一致。快照按原相对结构保存 compose.yaml、contract.json、postgres/01-create-databases.sh、milvus/embedEtcd.yaml、milvus/user.yaml，并在 manifest.json 记录各文件 SHA256 和四项上游引用。目录先完整准备再原子改名，不覆盖输入不同的既有版本。自动/手动恢复共用目标快照加载入口，显式切换到目标 Compose 及其目录，继续使用原项目名与当前密钥；普通冒烟通过后核对三个运行容器的实际 image ID，再写成功状态。缺失/损坏快照或镜像 ID 不符在维护前拒绝；旧 manifest.env 的缺项只能经人工核验补齐，不自动追认。过渡操作见 deploy/README.md。

快照清单同时记录 PostgreSQL、Neo4j、Milvus、Attu 四项完整固定镜像引用，对应 `POSTGRES_IMAGE`、`NEO4J_IMAGE`、`MILVUS_IMAGE`、`ATTU_IMAGE`。**应用 rollback 前，目标快照的四项上游镜像引用必须与当前生效的部署配置一致，运行容器身份也须核验匹配；任一不一致就在进入维护前拒绝自动回退。**告警后继续套用旧引用不可接受。应用回退只恢复三个应用服务（Compose 操作限定服务名并使用 `--no-deps`），不以旧 Compose 全量 up 改动数据库或 Attu。发生显式数据库/Attu 版本变更后，旧应用快照能否继续使用由人工确认，不在首版增加上游自动降级或迁移回退机制。

真实 env、令牌及 `SOURCE_CREDENTIAL_KEY` 只保留在权限受限的 `config/`，不复制到 release 目录或 artifact；不能保存/打印会展开密钥的 `docker compose config` 输出，校验可用 `config --quiet`。应用回退复用当前密钥，不自动恢复旧凭据。配置回退是独立显式操作；若当前非敏感配置与旧应用不兼容，拒绝自动回退并提示处理，不能静默覆盖配置。

兼容判断不依赖配置哈希。首版在 `deploy/contract.json` 集中定义 **`deployment_contract_version: 1`**，同时覆盖配置字段语义、Compose 接线和部署控制协议；发布清单、应用镜像标签及控制 `state` 响应声明该版本。部署脚本首版只支持精确相等的版本 1，不引入三套版本号、范围匹配或自动协议转换。

替换前核对脚本、目标发布清单、镜像标签及当前存活应用的控制响应版本，并用当前运行配置对目标镜像做 §7.2 预检；配置解析/必需项不通过则拒绝操作。哈希只用于追溯，不证明兼容。密钥值正常轮换不升级契约；必需环境变量语义、控制路径/请求头/响应或 Compose 挂载约定发生不兼容变化时升级统一版本并给出显式操作说明。契约不匹配时在进入维护前停止，README 提供人工处理说明，不临时猜旧协议。

**实施落点（问题 4 补充）：**复用 `release_manifest.py preflight`，日常 release 在写 `deploying`、维护标记、调用 enter 之前，核对四项运行中上游的实际 image ID，并严格解析两个后端的 `/state` JSON（契约为整数 1，maintaining 为布尔值，active 为非负整数；允许 active>0，随后按正常流程排空）。以无宿主端口的 `compose run --rm --no-deps --pull never -T --entrypoint python`，分别在目标 MetaWeave、NL2SQL 镜像内用当前 env 调用已有纯配置解析入口，检查平台数据库配置、MetaWeave CORS、部署开关/令牌/契约，每项限时 30 秒，不启动 API、不连库或调用模型。再用目标 MetaWeave 镜像运行既有 `smoke.py --scope infra`，只读验证两个 PG 库/对象、Neo4j/APOC、Milvus，不执行初始化，不新增常驻服务。infra 检查限时 60 秒并按唯一容器名清理，控制请求限时 10 秒；失败在维护前退出，日志不回显配置解析或 SDK 原始异常及凭据。首次 bootstrap 在初始化及四项上游启动后执行完整检查；恢复时仅可跳过 Docker 确认不存在或已停止的后端，存活但控制不可达仍拒绝，排空阶段再次核实后端状态。操作说明及旧快照过渡见 deploy/README.md。

相同源码的重复运行生成不同 release_id，避免覆盖上次成功镜像。保留当前和上一套应用镜像供回退，不在首版自动执行全局镜像/卷清理。

**首版 rollback 的目标只允许是 `deployment.json` 中的 current 或非空 previous。**这两个引用仅在无令牌普通冒烟通过后原子写入，作为可恢复版本的资格依据；候选目录、镜像标签、构建成功、日志或人工保留旧目录都不授予回退资格。已经移出这两个引用的历史版本不支持直接 rollback；需要重新部署时按显式 release 流程重新预检和验收，不增加任意历史回退及每个版本的成功标记机制。部署状态缺失/损坏时不能扫描日志或快照猜测成功版本。

旧镜像和目录的首版清理是 README 中的手动清单操作：始终排除 current、previous、最近操作涉及的候选及人工明确保留的资料；保留资料本身不扩展 rollback 范围。只删除本项目清单明确记录的更旧应用标签和 release 目录，不按模糊名称匹配，不删数据卷。BuildKit 缓存另行查看占用、人工处理，不让日常发布顺便执行全局 prune。

### 7.3.1 发布状态转换

`deployment.json` 的 `current`、`previous` 均为最近已验证成功的版本引用；`phase` 为 `ready`、`deploying` 或 `recovery_required`。不是通用状态机服务，只由持有同一部署锁的脚本读写。更新在同目录写临时文件、flush/fsync 后 `os.replace()` 原子替换；不能分写两份 current/previous 造成半更新。

| 操作及前态 | 成功后 | 失败/限制 |
| --- | --- | --- |
| 无成功 current，bootstrap R | current=R，previous=null，phase=ready | 重试涉及已有服务变更/初始化时先排空；满足 §6 的完整同镜像核验条件时可保留容器；失败不生成成功 current |
| ready 且 current=A，bootstrap A | 仅核验，版本引用不变 | 不替换容器或重新初始化；现场不匹配则标记 recovery_required，提示 rollback A |
| 非 ready 且已有 current，bootstrap | 拒绝并提示 rollback 恢复 | 不靠 bootstrap 覆盖未知现场 |
| ready 且 current=A，bootstrap B | 拒绝，提示使用 release B | 不允许 bootstrap 绕过发布或降级规则 |
| ready 且 current=A，release A | 仅核验，引用 A/原 previous 不变 | 不生成 previous=A；核验失败标记 recovery_required，提示 rollback A |
| ready 且 current=A，release B（B≠A） | current=B，previous=A，phase=ready | 自动恢复成功则引用仍为 A/原 previous；原发布报告失败 |
| ready 且 current=A，rollback A，现场核验通过 | 仅核验，引用不变 | 若维护标记残留、实际镜像或普通请求不符，转下行的恢复语义 |
| 非 ready 或现场核验失败，rollback A（A=current） | **实际恢复 A 的三个应用**，保留原 previous，phase=ready | 不因元数据 current=A 就跳过替换；仍须通过预检、排空和普通请求冒烟 |
| current=A，previous=P，rollback P（P 非空且 P≠A） | current=P，previous=A，phase=ready | 先检查镜像/快照/契约；失败不宣称 P 已成功运行 |
| rollback T，T 不属于 current/previous | 拒绝，引用和容器不变 | 在进入维护前拒绝，即使 T 的目录、日志或镜像存在 |

所有预检通过后，**在写维护标记、调用 enter 或启动/替换容器之前**，原子记录 `phase=deploying`、操作类型、目标版本和操作前的版本引用。bootstrap、release、rollback 的实际变更都遵守此顺序，不能等到排空结束才记录。不替换应用的同版本核验不进入 deploying；核验失败记录 recovery_required。

恢复失败或执行中断后无法确认现场时保留非 ready 状态，不把 current 当成正在健康运行的证明。自动 release 只接受 ready 且无维护标记的现场；`rollback <current或previous的release_id>` 同时作为人工触发的明确恢复入口，不另外新增 recover 命令。恢复时按目标快照执行应用恢复并检查三个实际 image ID，不能因元数据同版本提前返回；已经匹配的应用可保留，不强制重建数据库。无成功 current 的首次安装失败仍走 §6 的 bootstrap 重试及已有任务排空规则。配置和业务数据不随状态文件回退。

排空超时且**尚未替换容器**时，移除维护标记、确认两个后端 leave，再做无令牌普通请求冒烟；恢复入口及现场均核验成功才将原版本引用以 phase=ready 写回，原发布仍失败。脚本异常退出、标记残留、leave 失败或现场未知，不能仅靠 finally 将 phase 写回 ready。

**实施落点（问题 5 补充）：**`verify_current_release()` 统一核验无维护标记、两个后端开放（复用 preflight 的严格状态检查）、三个实际镜像 ID 和普通冒烟。bootstrap/release 同版本失败仅记录 recovery_required 并提示 rollback，保留 current/previous；rollback current 的同版本核验不通过则进入已有实际恢复流程，恢复成功保留 previous。首次 bootstrap 无论是否存在旧应用，都先写维护标记；已确认不存在/退出的后端不参与排空。排空超时未替换容器时，仅在原先无维护标记、leave 全部成功且原版本完整核验通过后写回 ready；否则保持/重新进入维护并记录 recovery_required。原本处于维护的恢复现场超时继续关闭，不自动开放未知现场。退出处理只收尾本次已开始、仍停留 deploying 的失败操作：重建维护标记，尽力 enter 存活后端，再记录 recovery_required，不改写成功版本引用；控制不可达时仍须人工处理，不能声称该进程已关闭入口。强制终止保留非 ready 状态，下一次仍需人工触发核验恢复。

### 7.4 更新、失败和回退

- 测试或构建失败：不触碰现有运行容器。
- 数据库状态检查失败：不进入应用替换阶段，报告具体依赖；不自动清库修复。
- 应用替换：使用已经构建的镜像和 `--no-build`；首次 bootstrap 成功后，正常发布不重建数据库容器，不执行 `compose down`。
- 成功边界固定为：维护令牌冒烟通过 → 移除维护标记 → 两个后端 leave 均确认成功 → 经 frontend 的**无控制令牌普通请求冒烟**通过 → 原子写入 current/previous 和 phase=ready。只完成带令牌冒烟或收到 leave 的单个 200 都不能标绿。
- 应用替换或冒烟失败：在数据库版本、卷和结构未变化的前提下，使用本次替换前 `deployment.json.current` 指向的成功版本及其非敏感部署快照恢复三个应用，再验证。替换前先保存该身份，不能误读 previous 多回退一版。密钥按 §7.3 使用当前值。原发布仍标记失败，即使回退成功。
- 初次部署没有旧版本可退时如实失败；回退失败时保留诊断，不能把流水线标绿。
- 不自动回滚数据库结构，也不承诺单机 Compose 零停机。停止旧应用前先完成 §7.5 的排空，再利用 §5.4 的优雅退出余量；排空超时不发送停止命令。
- leave 或开放后的普通冒烟失败时，保持 deploying，重新写维护标记并 enter，再按同一排空规则尝试恢复；无法安全恢复则写 recovery_required。开放后可能已有新业务请求，不能假定候选容器没有任务而直接停止。
- 手动回退（doc 15 §8.4）：先暂停自动发布，在独立运维检出 `/home/iw-runner/insightweave-ops/source/` 里执行；工具版本取操作开始时 current 对应的源码提交，脚本写绝对路径。见[部署手册](../deploy/README.md)。

本次不引入数据库版本升级或迁移框架。今后新增表字段或升级数据库引擎时，随实际需求提供升级步骤和回退边界，不简单执行“换个数据库镜像标签”。

### 7.5 在途工作与最小部署门闩

**代码事实：**MetaWeave 的采集/发布/删除任务使用同进程 `BackgroundTasks`；`backend.jobs.active_runs` 只覆盖采集/发布，不能当作全部活动任务清单。NL2SQL `WebRuntime.shutdown()` 已等待两个执行器，但没有替换前可撤销的排空接口；HTTP 结束/断连也不代表 executor 中的查询已结束。单看数据库状态或延长 stop 超时不足以解决问题。

首版只增加进程内的部署接入，不改任务调度、Agent 或任务账本：

- 共享小模块 `deploy/runtime/lifecycle.py` 提供入口门闩、HTTP 活动计数与 future 活动计数。两个应用单 worker 下接入；用同一把进程内锁完成“检查是否维护/登记请求”，避免先检查空闲、后又进入新请求的竞态。
- 控制操作只有 `enter`、`state`、`leave`，按各自 `/api/_deploy/`、`/api/v2/_deploy/` 前缀处理；要求独立控制令牌，Nginx 永久拒绝代理这些路径。普通业务 API 请求在维护期间返回 503 和 `Retry-After`。门闩只安装在两个后端，前端静态页面和 Vue 资源仍可访问，不新增前端维护状态管理或静态页面封锁。
- 活动计数只统计已放行的业务请求：从 ASGI 调用开始直到其 `finally`，包含响应发送后的 `BackgroundTasks`。**enter/state/leave、MetaWeave `/openapi.json`、NL2SQL `/api/v2/health` 不计入排空指标**，被维护门闩拒绝的请求也不登记；控制操作先分派，再进入业务计数，避免 state 自己返回 active=1。携令牌放行的业务读取冒烟仍计数，不能把所有含令牌的请求一概排除。
- NL2SQL 的 `submit_run()`、`submit_read()` 在提交 executor **之前**登记、提交失败撤销、future 完成才减计数，不能只统计 HTTP、查看线程池私有队列或按超时假定工作已结束。计数不记录请求正文或保留 future 结果。
- 部署脚本先在稳定 `control/` 目录写入维护标记，再令两个后端进入维护，等待活动请求和 future 均为 0；默认最多等待 900 秒，可由部署配置调整。只有此时才替换应用。等待超时、状态未知或控制失败均不停止旧容器、取消本次发布；仅正常发布的排空超时可按 §7.3.1 尝试恢复原入口，完整核验后才能写回 ready。状态未知、控制失败或恢复受阻时保持/重建维护标记并记录 recovery_required，提示人工恢复，不冒充已排空或已恢复。
- 每次排空轮询复用预检的严格 JSON 解析，并额外要求 `maintaining=true`；传输失败、截断响应、错误字段类型或未处于维护均不能当作 active=0。leave 或普通冒烟失败后的自动恢复先重新维护并排空，再做恢复预检；预检失败继续保留维护标记与非 ready 状态。
- 新后端启动时读取维护标记，存在就保持入口关闭；维护阶段冒烟通过后移除标记并 leave，随后执行普通请求冒烟，再记录发布成功。后续正常容器重启时无标记则恢复服务，不能配置成“每次重启永久维护”。脚本中断遗留标记时保持关闭并提示人工核验；标记不包含凭据、不是数据库初始化完成证明。
- 维护期间 MetaWeave `/openapi.json` 和 NL2SQL `/api/v2/health` 可用于进程健康检查；读取冒烟 GET/HEAD 可携带 `X-InsightWeave-Deploy-Token` 通过门闩，不能让令牌为普通业务写接口开放例外。控制操作单独校验方法和令牌。普通业务 API 请求在维护阶段仍被拒绝；开放业务入口及后续验证顺序按 §7.4，不能把维护阶段通过当作最终成功。令牌不进入普通日志或浏览器；本机 CLI、其他直连内部 API 的进程须遵守同一维护窗口，不能声称控制了用户独立启动的作业。
- 回退同样先排空待替换的候选应用；不能为了回退去强杀它刚接受的查询。NL2SQL 现有 `shutdown(wait=True)` 保留作为退出保障，不重复开发任务取消或恢复引擎。
- 已由 Docker 确认退出的容器可跳过活动计数；仍在运行但无法获取控制状态的容器不能被当成“已排空”，保留现场并报恢复受阻。**接受这会降低自动回退成功率**：候选容器控制接口故障时，可能必须由人工处理。README 必须说明该边界；维护标记由脚本写入，本身不能证明应用正确装配了门闩或从未放行业务。首版不据此推断无任务，也不增加证明账本或强杀分支。

默认开发环境不启用门闩。部署门闩的必要验证为：空闲时连续 state 轮询及同时执行进程健康检查仍返回业务活动数 0；长查询、采集/发布及删除后台任务未结束时不替换；客户端断连而 future 未完成仍等待；超时保持旧容器与任务；正常排空后发布成功。这里只保证受控发布，不保证进程崩溃、OOM 或用户手工强杀不中断任务。

映射后的 PG 客户端、Neo4j Browser 及 Attu 管理操作不受两个应用门闩控制，维护期遵守同一人工停止写入约定，不新增管理工具门闩。

**控制调用通道固定为 WSL Runner → `docker compose exec -T` → 后端容器内 curl → `127.0.0.1`。**不经前端/Nginx，不发布额外宿主端口，不走代理。MetaWeave 容器使用端口 8001、前缀 `/api/_deploy/`；NL2SQL 使用端口 8008、前缀 `/api/v2/_deploy/`。`state` 为 GET，`enter`/`leave` 为 POST。在 `deploy.sh` 中用局部 `call_deploy_control()` 复用这一调用方式，服务名和操作仅允许上述固定集合，并检查退出码、响应契约版本及字段。

以下是计划接口实现后的 NL2SQL `state` 调用示例；三个变量分别由脚本指向固定 Compose 项目、本机部署配置及当前实际运行版本的 Compose 快照，不能误用另一个候选版本的配置：

```bash
docker compose --project-name "$compose_project" --env-file "$deploy_env" \
  -f "$release_compose" exec -T nl2sql sh -eu -c '
  : "${DEPLOY_CONTROL_TOKEN:?missing DEPLOY_CONTROL_TOKEN}"
  printf "X-InsightWeave-Deploy-Token: %s\n" "$DEPLOY_CONTROL_TOKEN" |
    curl --noproxy "*" --fail --silent --show-error \
      --connect-timeout 3 --max-time 10 --header @- \
      http://127.0.0.1:8008/api/v2/_deploy/state
'
```

令牌从后端容器已有的运行环境读取，通过 curl 的标准输入传入请求头，不把令牌值展开进宿主命令、curl 参数或日志；实现时关闭该调用附近的 shell xtrace，不使用 curl verbose/trace。`enter`/`leave` 在同一通道使用对应固定 URL 和 POST；请求超时只是单次控制调用失败，不能据此认为任务已排空。`--header @-` 用法见 [curl 官方说明](https://curl.se/docs/manpage.html#-H)。控制接口尚未实现，示例不代表已完成真实排空联调。

## 8. 计划开发的文件和脚本

以下均为计划文件，除已注明的现有文件外，当前尚不存在；不能直接把示例命令当成已经可用。

| 文件 | 类型 | 负责内容 |
| --- | --- | --- |
| `.dockerignore` | 新增 | 根构建上下文过滤，排除凭据、宿主依赖和运行产物 |
| `frontend/Dockerfile` | 新增 | Node 构建 + Nginx 最终运行镜像 |
| `metaweave/Dockerfile` | 新增 | Python 3.12、uv、平台 extra、源码/配置/DDL/部署初始化脚本、API 入口 |
| `nl2sql/Dockerfile` | 新增 | Python 3.12、uv、源码/Skill/配置/部署初始化脚本、API 入口 |
| `deploy/compose.yaml` | 新增 | 严格七个 services，同一网络、健康检查、稳定命名卷、§4 的五条回环端口映射和运行环境接线 |
| `deploy/contract.json` | 新增 | 任务 2A 创建；集中声明 deployment_contract_version=1，供脚本、镜像和控制响应使用；不包含密钥 |
| `deploy/nginx/default.conf` | 新增 | 静态页面、SPA 回退、API 分流、Host 传递、长查询超时、部署控制路径拒绝代理 |
| `deploy/milvus/embedEtcd.yaml` | 新增 | 任务 1 固化经验证的同容器 etcd 配置，无宿主 2379 暴露 |
| `deploy/milvus/user.yaml` | 新增 | 任务 1 固化版本对应的 RocksMQ 与本地存储覆盖配置 |
| `deploy/postgres/01-create-databases.sh` | 新增 | 任务 2A 创建，2B 接线；自包含 psql 调用，兼容官方入口 source/直接执行与 bootstrap 独立 Bash 重入，仅补目标缺失库 |
| `deploy/env/deploy.env.example` | 新增 | Compose 插值、入口、镜像版本、CI Python 和可选 WSL/构建代理输入；机器相关值由本机配置提供 |
| `deploy/env/metaweave.env.example` | 新增 | MetaWeave 运行环境模板，显式 `PLATFORM_CORS_ORIGINS=http://localhost:18080,http://127.0.0.1:18080` |
| `deploy/env/nl2sql.env.example` | 新增 | 平台登记库与持久化库、相同解密密钥，显式 `NL2SQL_CORS_ORIGINS=http://localhost:18080,http://127.0.0.1:18080` 和 `NL2SQL_ALLOWED_HOSTS=localhost,127.0.0.1` |
| `deploy/scripts/ci.sh` | 新增 | 干净源码检查、独立依赖环境、显式离线套件、三个镜像构建与候选镜像预检、输出候选清单 |
| `deploy/scripts/deploy.sh` | 新增 | bootstrap 的离线 APOC 准备/核验、重试/发布/恢复的统一排空、仅 current/previous 回退、前端保留时 reload、冒烟与原子状态 |
| `deploy/scripts/init_platform.py` | 新增 | 任务 2A 实现；封装现有建表/建库入口及对象检查，在后端容器内运行；任务 3 做真实初始化验证 |
| `deploy/scripts/smoke.py` | 新增 | 任务 2A 实现 infra/apps 和 maintenance/public 两种读取模式；任务 3/4 接入实际联调；普通模式不带令牌 |
| `deploy/runtime/lifecycle.py` | 新增 | 任务 2A 实现并接入两个 API；共用可选 ASGI 门闩、业务计数和带版本的控制操作；排除控制/健康计数 |
| `metaweave/backend/api/main.py` | 修改 | create_app 在部署开关启用时装配门闩；保留已有任务执行链路 |
| `nl2sql/src/api/main.py` | 修改 | Web API 在部署开关启用时装配同一门闩，不改 Agent 接线 |
| `nl2sql/src/api/v2/runtime.py` | 修改 | submit_run/submit_read 通过提交前登记、完成后释放的 future 计数接入排空；保留 shutdown |
| `.github/workflows/local-release.yml` | 新增（2026-11-24 已停用并删除） | main/手动触发，self-hosted Runner，串行发布，调用同一套脚本；现行入口见 doc 15 |
| `deploy/tests/test_deployment_contract.py` | 新增 | 七服务、初始化及 bootstrap 重试排空、两版本回退范围、同版本恢复、reload 失败、原子状态与两阶段冒烟 |
| `deploy/tests/test_lifecycle.py` | 新增 | BackgroundTasks、断连 future、超时不停止、空闲控制/健康轮询计数为零、控制鉴权回归 |
| `deploy/README.md` | 新增 | 本机配置、受维护测试清单、Runner 信任边界、安装/发布/回退、手动备份恢复/清理及浏览器验收 |
| `.gitignore` | 按需修改 | 忽略本机部署实值与临时报告；安全模板可入库 |
| `docs/1_服务启动指南.md` | 修改 | 追加 Docker 入口与本设计链接；保留现有开发启动说明 |

生产源码修改限定在上述部署生命周期接入，不改 SQL、Agent、任务账本、采集和发布的业务流程。如容器构建另外发现路径或依赖声明的真实缺陷，只做有证据的最小修正并同步测试，不重构三个模块。

### 8.1 脚本接口及核心函数建议

| 接口 | 输入、行为和输出 |
| --- | --- |
| `ci.sh <release_id>` | 核对干净源码 SHA、工具和网络，离线测试、构建和候选预检；全部成功才写候选清单；失败非零退出 |
| `deploy.sh bootstrap <release_id>` | 无成功 current 时初始化/重试，按 §6 检查实际运行容器并在变更前排空；ready 同版本仅核验；非 ready 且已有 current 提示 rollback |
| `deploy.sh release <release_id>` | 只接受 ready 且无维护标记；同版本仅核验且保留 previous，不同版本才更新引用；失败按 §7.4 恢复 |
| `deploy.sh rollback <release_id>` | 目标必须属于 current/previous，再检查快照/镜像/契约；ready 同版本现场一致则仅核验，否则实际恢复；不改数据库，不开放任意历史回退 |
| `init_platform.py <metaweave|nl2sql|milvus> --mode <initialize|check>` | 在相应后端镜像环境内执行，职责按 §6；不加载真实业务源或调用模型 |
| `smoke.py --scope infra` | 通过 `exec` 在具备数据库驱动的后端容器内检查 Compose 网络中的基础服务和持久化对象；不要求业务知识已发布 |
| `smoke.py --scope apps --mode maintenance` | WSL Runner 经 `http://localhost:18080` 带令牌读取冒烟；令牌从受限配置读取，不写命令行参数或日志 |
| `smoke.py --scope apps --mode public` | 默认模式，明确不携带控制令牌，经同一前端入口发普通请求；通过后才可记成功，不从容器内把 localhost 当 Windows 入口 |

`init_platform.py` 建议函数：`ensure_metaweave_schema()`、`ensure_nl2sql_persistence()`、`ensure_milvus_database()`；`check` 模式只核验；`initialize` 按 §6 区分 MetaWeave 事务创建、NL2SQL 已知结构的幂等续建及未知结构冲突，不能套同一“部分即失败”规则。

`deploy.sh` 的 `prepare_neo4j_plugin()` 按 doc 13.1 §2.2 在首次启动前用现有官方镜像准备插件卷；已有相同文件仅核验，冲突停止，已运行的 Neo4j 不写插件。该短时文件准备容器不启动数据库、不挂载其数据卷，不新增常驻服务；任务 1/3 可先用等价手工命令验证，任务 4 再接入本函数。

生命周期接口建议收敛在 `DeploymentGate`：`enter()`、`state()`、`leave()` 以及 `submit_tracked(submit, fn)`；ASGI 包装使用同一进程内实例，`state()` 排除自身活动数并返回契约版本。部署脚本的 `write_deployment_state()` 只原子替换一份 JSON，`wait_drained()` 超时必须在调用 Compose 替换之前返回失败。不再添加作业中心或通用事件总线。

`deploy.sh` 使用固定部署目录中的本机锁覆盖手动操作和 Actions 发布，GitHub `concurrency` 同时禁止中途取消正在部署的任务。无需增加发布数据库、调度服务或通用工作流引擎。

### 8.2 GitHub Actions 最小流程示意

> 本示意对应已删除的 `local-release.yml`，仅作历史记录。现行任务图见 doc 15 §5.3（公开通知 `notify-deploy.yml`）和私有部署仓的 `deploy.yml`（verify → release → finalize）。

```yaml
name: InsightWeave local release
on:
  push:
    branches: [main]
  workflow_dispatch:
permissions:
  contents: read
concurrency:
  group: insightweave-local-release
  cancel-in-progress: false
jobs:
  release:
    if: github.ref == 'refs/heads/main'
    runs-on: [self-hosted, Linux, X64, insightweave-local]
    steps:
      # 正式文件将 checkout 固定到经过核对的 action commit SHA。
      - uses: actions/checkout@<固定提交SHA>
        with:
          persist-credentials: false
      - name: Test, build and deploy locally
        shell: bash
        run: |
          release_id="${GITHUB_SHA:0:12}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
          bash deploy/scripts/ci.sh "$release_id"
          bash deploy/scripts/deploy.sh release "$release_id"
```

示意中的占位 action SHA 在实施时必须替换。只允许可信发布分支使用这个本机 Runner，不注册外部 PR 或 `pull_request_target` 触发，不把个人开发目录作为 checkout。

Runner 注册、标签和 GitHub 仓库设置由接入步骤完成；不能把注册令牌提交到仓库。2026-11-24 起本机 Runner 只注册到私有部署仓 `paulwxq/insightweave-deploy`（doc 15 §8.1）。Runner 启动环境需显式包含 Python、uv、Node 和 Docker 路径，不能依赖交互 shell 的 nvm 自动加载。访问 GitHub 失败时按 §4.2 设置本机代理，并在 Runner 启动前落实；已有网络测试结果不能替代实际运行账号的环境验证。

### 8.3 Runner 的信任边界

> **2026-11-24 更新**：源码仓已公开（doc 15 §10 步骤 7），隔离方式相应改为：公开仓只运行托管 CI，本机 Runner 只注册在私有部署仓；公开仓 main 已配置分支保护（要求 PR 和 `ci-required` 成功，对管理员同样生效），单人阶段不要求人工批准（doc 15 §4.2）。下面各条是首版按私有仓设计时的判断，保留作对照。

- 接入时先记录总仓库是否私有、维护者名单、谁能修改工作流和发布分支。首版推荐本人维护的私有仓库；不把当前尚未配置的仓库说成已经私有。公开仓库、外部贡献者或多团队共享 Runner 不直接套用本方案，需要重新确认隔离方式。
- Runner 仅注册到本项目仓库，不用于其他项目；不运行外部 PR、fork、任意 ref 或用户输入拼接出的脚本。`contents: read` 只限制 GitHub token，不能限制 checkout 中脚本访问本机。
- 使用独立运行账号，部署目录仅该账号可读写（目录 0700、密钥文件 0600），不使用日常开发 checkout。**有 Docker socket 权限的账号仍能控制该引擎的容器、卷及可挂载目录，不能称为安全沙箱；低权限账号和目录权限不撤销该能力。**
- 平台/套餐支持时为 main 禁止强推和删除、限制写入者；多人协作时增加必要审核。GitHub 私有仓库分支保护的可用性取决于套餐，不为此默认引入付费升级；功能不可用时保留“仅本人有写权限、无不可信代码执行”的限制，并在接入记录写明，不能声称已有平台强制保护。
- GitHub Environment 审批是多人或更高风险部署时的可选增强，首版单人本机流程不强制增加审批和多阶段发布平台。审批本身也不能把已执行的不可信脚本变安全。

依据：[GitHub Actions 安全使用](https://docs.github.com/en/actions/reference/security/secure-use)、[分支保护及套餐适用范围](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)。

## 9. 健康检查和验收

### 9.1 检查内容

- PostgreSQL：`pg_isready` 只说明数据库进程可接入；另外核对两个目标库及项目表，不以它代替初始化完成。
- Milvus：服务 `/healthz` 加 SDK 连接和目标库存在性；单容器持久性专项使用独立临时库/集合验证，不写现有知识。
- Neo4j：驱动执行 `RETURN 1`，核对实际 APOC 能力；业务图内容可以为空。
- Attu：宿主 `http://localhost:3000` 页面可访问，管理界面实际连接新 Compose 的 `milvus:19530`；不以旧 Attu 已连通原 Milvus 代替验收。
- MetaWeave：内部 `/openapi.json` 验证 API 启动，再通过 `/api/data-sources` 验证平台库读取；当前没有独立 `/health`，不凭空调用。
- NL2SQL：`/api/v2/health` 加持久化表和 Store 基础读取检查；该 health 本身不连库，不能单独证明就绪或门闩已开放。
- 前端：维护/普通两阶段都经 `http://localhost:18080` 验证静态页面、Vue 深层路由回退、`/api/data-sources`、`/api/v2/health` 以及 `GET /api/v2/sessions?limit=1`。最后一个是现有普通业务读取路径，不在门闩白名单，不要求已登记数据源；必须验证 JSON 业务响应，不能接受 SPA HTML 或仅凭 health 的 200 认定开放成功。不提交采集/发布/付费问数。

会话列表 GET 可能触发现有按需校正（`router.py:list_sessions()`），不宣称其绝对无存储副作用；其读取/校正 future 仍按 §7.5 跟踪，不新增修复逻辑或付费调用。

以上是自动化 HTTP/依赖冒烟，不能证明浏览器 JavaScript 已正确执行。首次正式部署及前端构建/路由/代理配置变更后，补一次 **Windows 真实浏览器验收**：打开首页及 `/query` 对应深层路由、刷新页面，检查控制台和资源请求、实际 API origin，分两档：

- **干净空环境：**数据源列表为空、问数页面正常渲染，显示“暂无已启用的数据源”等现有提示；不编造 source_id，也不要求调用 readiness。
- **已登记受控测试源：**使用真实 source_id 检查 `/api/v2/sources/{source_id}/readiness`；无知识时成功返回 ready=false 可以接受，源不存在、依赖不可访问或页面崩溃/CORS 错误不能当作“缺知识”通过。

Windows 浏览器分别从 `http://localhost:18080` 和 `http://127.0.0.1:18080` 打开首页及问数页，确认数据源读取及适用的 readiness 没有 CORS 错误；回环 IP 页面访问固定 localhost API 的跨 Origin 场景也须覆盖。无需付费问数；首版人工记录结果，不建设自动浏览器回归平台，也不把 HTTP 冒烟成功叫作浏览器验收通过。

### 9.2 关键验收矩阵

| 场景 | 预期 |
| --- | --- |
| 干净环境首次 bootstrap | 同一 Compose 七个常驻容器、七种部署运行镜像；库表完整；Windows 页面、两个 API 和管理入口可用 |
| 同一完整环境重复 bootstrap | 无删库、清图或清集合，原记录保留 |
| Neo4j 插件离线准备及无代理重启 | 固定镜像 JAR 复制到独立插件卷后启动；NEO4J_PLUGINS 为空，重启前后插件 SHA 相同、APOC 过程可用，六个代理变量为空；无启动下载 |
| APOC JAR 缺失、重复版本或 SHA 冲突 | 启动前明确失败；不覆盖未知插件、不自动联网下载、不写运行中的 Neo4j 插件卷 |
| PG 建完第一个目标库后入口脚本中断、容器重启 | bootstrap 显式重入同一脚本补齐另一库，保留已有库和记录；不依赖 Docker 入口自动续跑 |
| 首次已开放、写 current 前中断，重试 bootstrap 需变更已有应用 | 没有 current 也先 enter/等待 HTTP 与 future 排空；不可控且仍运行则转人工，不直接停止或替换 |
| 无 current，但目标镜像/对象完整且入口已正常开放 | 按 §6 仅核验并补写成功记录，保留容器与在途工作，不重新初始化或强制重建 |
| MetaWeave DDL 中途失败及提交结果不明确 | 事务回滚；重入先核验，完整保留、空对象重建；未知部分或冲突停止，不执行重置 |
| NL2SQL 初始化中断后重试 | 已知兼容对象由原幂等入口补齐，未知结构冲突停止；不丢已有记录 |
| Milvus 单容器运行 | 不依赖额外容器；临时测试数据重启后仍可查询 |
| 离线测试或镜像构建失败 | 原应用版本和数据保持运行 |
| 候选镜像导入、路径、origin 或入口预检失败 | 替换旧容器之前失败，不造成应用停机 |
| 前端候选无后端容器的离线预检 | 两个 upstream 通过临时 hosts 解析，nginx -t 与静态页通过；不将未测试的 API 代理宣称已通过 |
| bootstrap 同版本重入 / 换版本 / 手动 rollback | 状态引用严格符合 §7.3.1，失败不伪造成功；两引用原子更新 |
| current=A 再次 release A | 仅核验且保留原 previous，不写 previous=A、不重建匹配容器 |
| 非 ready，元数据 current=A，实际混合 A/B，再 rollback A | 实际恢复到 A，保留原 previous；无法排空则转人工，不因同版本跳过 |
| 非 ready，frontend 已匹配 A、后端为 B/混合或缺失 | 实际联调恢复 A：保留 frontend 容器，至少一处后端 IP 实际改变；后端就绪后 nginx -t + reload，再通过两条 API 代理及普通业务读取冒烟 |
| 保留 frontend 的 nginx -t / reload 失败 | 不开放业务入口、不记录 ready；按恢复规则处理，无法恢复则 recovery_required，不以旧静态页面仍可访问当作成功 |
| 发布状态写入中断 / 恢复失败 | 不出现半份 JSON 或半更新的版本对；非 ready 状态阻止后续自动覆盖 |
| 写维护标记后或进入维护时脚本中断 | 此前已记录 deploying，不能留下 ready+维护的假成功状态；rollback 提供恢复入口 |
| 令牌冒烟通过但 leave 失败或普通请求仍为 503 | 不更新 current、不标 ready；重新维护并安全恢复，无法恢复则 recovery_required |
| 脚本、镜像、控制响应或目标清单契约版本不兼容 | 进入维护前拒绝；不以配置哈希相同/不同推断兼容性 |
| 长查询、采集/发布及删除任务运行中发布 | 阻止新请求并等待，未排空不替换；超时取消发布，旧任务继续 |
| 空闲时连续 state 轮询及进程健康检查 | 两种请求均不计入业务活动，state 始终返回 0，不出现自身 active=1 |
| 浏览器 Origin 配置 | 分别携带 `Origin: http://localhost:18080` 和 `Origin: http://127.0.0.1:18080` 经 frontend 读取验证接口；NL2SQL 不返回 origin_not_allowed，两个后端允许头精确匹配对应 Origin；错误 Origin 被拒绝或无允许头，不用无 Origin 的 curl 代替 |
| 客户端断连但 NL2SQL future 未完成 | 仍算活动任务，不因 HTTP 已结束就停止容器 |
| 普通重启 / 维护中重启 / 部署脚本中断 | 无维护标记正常恢复；有标记保持关闭；中断后需核验，不能默认放开或永久误关 |
| 控制令牌缺失、错误或请求经 Nginx 进入控制路径 | 启用缺配置时启动失败；无授权不能操作，Nginx 不转发控制路径；日志不泄露令牌 |
| 发布新应用版本 | 三个应用匹配同一 release_id；三个数据库、Attu 容器及已有命名卷保持不变 |
| 新应用无法启动或代理失败 | 原发布失败；有上一成功版本时恢复并验证；首次安装无旧版本时如实失败 |
| 回退版本缺镜像 / 回退失败 | 不报成功、不删除数据，提供诊断 |
| rollback 目标不属于 current/previous | 进入维护前拒绝；仅构建/失败候选或更早历史版本即使有镜像、快照、日志也不获得资格 |
| rollback 快照的任一上游镜像引用与当前配置/运行身份不一致 | 覆盖 PG、Neo4j、Milvus、Attu 四项；包括仅 ATTU_IMAGE 不一致的用例。进入维护前拒绝，不启动旧上游版本、不改这些容器或卷；即使目标属于 current/previous 也不能绕过 |
| 候选仍运行但控制接口不可达 | 接受自动回退受阻，保留现场并转人工，不根据维护标记推断无任务后强杀 |
| checkout 目录被清理或重新生成 | 持久卷、配置、deployment.json 和历史部署快照仍在 |
| 密钥轮换后回退应用 / 检查发布快照 | 使用当前密钥；release、日志和 artifact 没有 env 实值或展开配置 |
| 脏工作区手动构建 | 明确拒绝，不能用旧 SHA 描述新工作树镜像 |
| 所需宿主端口被原 PG、Neo4j、Attu 或其他开发服务占用 | 发布预检明确失败；不停止、不清理原服务，也不静默换端口；由维护者处理后再执行 |
| 端口暴露及管理界面 | 只发布 §4 的五条回环映射；18080 页面/API、3000 Attu 连接、7474 页面加 7687 登录查询及 6432 PG 客户端连接分别验收；容器内部 PG 连接仍使用 postgres:5432，其他端口不发布 |
| VPN 短暂断开 | 下载步骤按有限重试处理；耗尽后明确失败，不误标发布成功 |
| 本地手动与 GitHub 同时发布 | 使用同一部署锁，避免并行替换应用 |
| 无已登记数据源的空环境 | 空列表和无数据源提示正确；不要求 readiness、不伪造 source_id 或调用模型 |
| 已登记测试源但无业务知识 | 检查该 source_id 的 readiness，ready=false 可接受；依赖读取失败不算通过 |
| Windows 真实浏览器打开、刷新问数页面 | 页面和资源正确加载，API origin 正确；按空环境/已登记源两档验收，不要求付费查询 |
| 维护期间浏览器访问页面和 API | 静态页面/Vue 资源可获取，普通业务 API 被门闩拒绝；不把本方案描述成整个网站停止访问 |

纯脚本测试只替换 Docker/文件边界，断言真实编排函数的命令与状态结果；关键容器替换、卷保留和七服务初始化需实际联调，不用“YAML 解析成功”代替运行验证。

“前端保留、后端恢复”用例必须记录前端容器 ID 未变、后端新旧 IP 和 reload 结果；不能因为后端偶然复用了旧 IP、代理恰好仍可用，就宣称覆盖了静态 upstream 更新路径。reload 失败分支另做受控异常注入，不在真实现有环境破坏代理配置。

### 9.3 首版业务联调边界

空环境部署验收不等于“采集—发布—问数”全链路验收。首版应另外验证容器能够连接独立的外部测试 PG（不新增长驻服务、不写原业务库），并记录网络/凭据检查结果。真实发布产生的 Milvus 集合、索引、Neo4j 约束及 NL2SQL readiness，仍需用受控数据走实际发布验证。

两个后端到当前启用的 LLM/Embedding endpoint 也需进行独立的运行期直连验证；用户已明确服务运行时不使用代理，清单见 [doc 13.1 §1.4](13.1_镜像与依赖安装明细.md)。外部 PG 按数据库协议直连，HTTP 代理不能替代容器路由/VPN 和数据库端口检查。使用供应商支持的只读元数据接口核对连接/认证；仅目录查询或网络层可达时，不将其记作模型推理/Embedding 功能已通过，不自动发起付费问数。

当前发布可能需要真实 Embedding 服务，不能把固定向量/模型替身的结果称为真实语义验证。为控制范围，本轮不新建离线采集模拟框架，也不自动调用付费模型；如果尚未完成该业务验收，交付说明必须写明“基础部署已验证，核心采集—发布—问数链路尚未验收”，不能以临时 Milvus 集合或 APOC 检查代替。

## 10. 分步实施清单

依赖顺序固定为 **1 → 2A → 2B → 3 → 4 → 5 → 6**。2A 准备镜像内要运行的代码，2B 首次构建包含这些真实文件的完整应用镜像；不采用临时基础镜像或占位文件过渡。任务 3/4 验证并编排前面已有的运行代码，不在首次构建之后才补齐其必需文件。

- [ ] **任务 1：七服务部署前提。** 验证三个数据库及 Attu 的固定上游镜像、Milvus 内嵌模式，核对固定 Neo4j 镜像的内置 APOC 并验证离线复制到临时插件卷，冻结版本，将 Milvus 验证配置固化到 `deploy/milvus/embedEtcd.yaml` 和 `user.yaml`；此时不依赖尚未创建的项目 Compose。若此步不成立，先报告，不启动其余重构。
- [ ] **任务 2A：部署运行时代码及离线测试。** 创建 `deploy/contract.json`、`deploy/runtime/lifecycle.py`、`deploy/scripts/init_platform.py`、`deploy/scripts/smoke.py` 和 PG 入口建库脚本，完成两个 API 和 NL2SQL executor 的门闩接线。先在 `deploy/tests/test_lifecycle.py`、`deploy/tests/test_deployment_contract.py` 补测试，再实现真实逻辑：包括启用/关闭部署开关、控制鉴权/版本、控制和健康请求排除计数、BackgroundTasks/断连 future、MetaWeave 事务调用、NL2SQL 幂等入口、未知冲突及两种冒烟模式。数据库/HTTP 边界可用替身隔离，分别在所需后端依赖环境验证；此阶段只验离线逻辑，不宣称数据库事务/持久化已通过，实际验证放在任务 3。
- [ ] **任务 2B：完整应用镜像和 Compose。** 以前项真实文件和通过的离线测试为输入，编写三个 Dockerfile、ignore、Nginx、环境模板及重启/资源/日志配置；补并通过严格七服务检查。按 §5.1 的明确路径复制全部运行依赖，再构建和预检三个应用镜像：后端启用 `DEPLOY_CONTROL_ENABLED=1`，使用隔离的预检配置/控制目录，核对控制状态、契约及维护门闩；初始化和冒烟脚本在镜像中可导入并解析参数；前端按 §7.2 的 hosts 映射完成独立预检。此时直接使用 Docker/Compose 命令，不调用任务 4 才提供的 ci/deploy 脚本；不能靠关闭门闩、空文件、stub 或宿主源码挂载绕过最终运行方式。
- [ ] **任务 3：真实初始化和持久化联调。** 使用任务 2B 的完整镜像与 Compose，在独立新卷中通过 `exec` 执行任务 2A 已实现的初始化和基础设施冒烟。验证 MetaWeave DDL 实际事务回滚、NL2SQL 初始化中断后的幂等重入、未知结构拒绝、重复初始化和重启保留；不重新实现初始化封装，不重置当前真实数据。
- [ ] **任务 4：本地 CI/CD 编排与发布故障验收。** 扩展部署脚本测试，开发仅在本机执行的 `ci.sh`、`deploy.sh`，将任务 2B 的构建/预检、任务 3 的初始化验证及已实现的 `smoke.py` 接入统一流程。验证两版本回退、bootstrap 重试、非 ready 恢复、维护前记录 deploying、契约/密钥边界、排空超时和断连 future、两阶段冒烟与并发锁；实际验证前端保留/后端 IP 改变后的 reload 和失败分支。此阶段完成完整发布验收，不再首次创建门闩或容器内必需脚本。
- [ ] **任务 5：GitHub 接入。** 确认 §8.3 的仓库/账号信任边界，绑定远端与可信分支，注册专用 Runner；先验证手动触发，再验证 main 推送触发，均回报明确结果。（2026-11-24：发布入口与 Runner 归属已按 doc 15 §10 步骤 3—8 迁移，这里的手动触发与 main 推送触发不再适用。）
- [ ] **任务 6：交付记录。** 更新 README、启动指南、Windows 浏览器验收和实际结果；记录备份/业务链路未验证边界，提供手动恢复/精确清理步骤；提交配置与脚本，不提交密钥、数据卷或日志。

任务 3 包含上述建库中断重试的实际验证；任务 4 的 `ci.sh` 必须显式执行 `deploy/tests/`，不能仅复用原有两个后端的测试目录。

builder 代理、依赖构建/导入、供应商只读元数据接口及实际 SDK 运行直连的结果，按阶段回填 [doc 13.1 §8.1：验证事项与实测记录](13.1_镜像与依赖安装明细.md)。本次已完成临时依赖镜像预验证，详见其 §8.2；不因此勾选任务 2B，正式部署文件及完整预检仍须实施。原“运行代理验证”不再作为交付要求；服务运行时按用户确认的直连约束验收。

构建后的容器代码不会随宿主文件编辑自动更新。任务 3/4 若发现并修改了镜像内代码、依赖或配置，必须用新 release_id 重建受影响镜像，重新执行候选预检及受影响联调，再作为发布输入；不能继续用旧镜像宣称新代码已经验证。这是修正后的正常重建要求，不是预先安排两套镜像口径。

本方案不要求先做完善的多环境发布平台、数据库自动迁移体系或零停机升级。第一版交付标准是：**GitHub 触发、同机自动测试和构建、七服务可重复部署、日常只更新应用、失败能准确报告并按条件恢复。**

## 11. 官方依据与当前文档边界

- [GitHub self-hosted Runner 要求与通信](https://docs.github.com/en/actions/reference/runners/self-hosted-runners)
- [GitHub Workflow 配置](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)
- [Docker Desktop WSL2 集成](https://docs.docker.com/desktop/features/wsl/)
- [Docker Compose services](https://docs.docker.com/reference/compose-file/services/)
- [PostgreSQL 官方镜像初始化约定](https://hub.docker.com/_/postgres)
- [Neo4j Docker 插件配置](https://neo4j.com/docs/operations-manual/current/docker/plugins/)

本文规定本地执行方案，不以 GHCR 或 GitHub 云端计算为前提。self-hosted Runner 的执行计费政策以 [GitHub 官方说明](https://docs.github.com/en/actions/concepts/billing-and-usage) 为准；本次不配置云端付费构建。

当前变更为设计文档及实测记录。基础网络、临时依赖构建和部分运行检查已完成，但正式部署文件、脚本、七服务联调和 Actions 工作流尚未实施；不可将本文示例视为已经可以一键发布。

## 12. 审核意见裁定

### 12.1 第一轮

以下是范围裁定，不代表对应部署能力已经实现。

| 编号 | 判断与首版取舍 | 落点 |
| --- | --- | --- |
| 1 | 信任边界缺失成立；采用专用、可信维护者前提，不强制引入付费分支保护或 Environment 审批 | §8.3 |
| 2 | 状态语义缺口成立；明确重入/回退，用单文件原子更新，无需状态机服务 | §7.3.1 |
| 3 | 初始化重试问题成立；MetaWeave 单事务，NL2SQL 复用幂等入口，未知结构停止 | §6.1–6.2 |
| 4 | 在途任务问题成立；只加入口门闩与活动计数，超时取消替换，不开发任务取消/恢复平台 | §7.5 |
| 5 | 卷与备份不是同一能力；补手动说明和未演练边界，自动备份/定期演练调度后续按需 | §5.5 |
| 6 | HTTP 冒烟局限成立；补首次及前端配置变更后的 Windows 浏览器人工验收 | §9.1 |
| 7 | 快照和密钥语义缺口成立；密钥独立保存，应用回退使用当前密钥 | §7.3 |
| 8 | 手动构建追溯问题成立；脏工作区拒绝，从固定提交的干净源码构建 | §7.2 |
| 9 | 测试目录遗漏成立；补服务层及独立摘要离线套件，保留历史排除理由 | §7.2 |
| 10 | 候选预检缺失成立；增加无端口/无真实卷的短时预检，不上双套部署 | §7.2 |
| 11 | 增长问题成立；日志轮转，旧版本首版手动按清单清理，不自动 prune 卷 | §5.4、§7.3 |
| 12 | 运维参数遗漏成立；补重启/启动宽限/资源初值，实测后校准 | §5.4 |
| 13 | 业务验收边界建议合理；补外部测试库连接要求与未验收声明，不以模拟模型冒充真实链路 | §9.3 |

### 12.2 第二轮

| 编号 | 判断与首版取舍 | 落点 |
| --- | --- | --- |
| 1 | 恢复入口缺口成立；复用 rollback，非 ready 同版本也执行实际恢复，不新增 recover 命令 | §7.3.1、§8.1 |
| 2 | deploying 记录过晚成立；预检后、任何维护变更前原子写入，失败不能无条件复原 ready | §6、§7.1、§7.3.1 |
| 3 | 成功边界缺口成立；两个 leave 确认及无令牌普通读取冒烟通过后才记成功 | §7.4、§9.1 |
| 4 | 自计数风险成立；控制及进程健康请求排除，业务读取和其 future 仍计数 | §7.5、§9.2 |
| 5 | upstream 解析问题成立；首版使用离线容器临时 hosts 映射，只验静态页面，不增加三应用预检网络 | §7.2 |
| 6 | 哈希不等于兼容的意见成立；一个统一版本、精确匹配及目标配置预检足够，三套版本范围暂不需要 | §7.3 |
| 7 | 同 release_id 语义遗漏成立；ready 同版本只核验，保留原 previous | §7.3.1 |
| 8 | readiness 前提遗漏成立；空环境检查空列表/提示，已登记测试源才查 readiness | §9.1–9.2 |
| 9 | 是明确接受的恢复边界，不是自动强杀理由；接口不可达且仍运行则转人工，暂不实现“从未开放”的证明机制 | §7.5、README 计划 |

### 12.3 第三轮

| 编号 | 判断与首版取舍 | 落点 |
| --- | --- | --- |
| 1 | 任意历史版本的成功资格依据缺失成立；首版限 current/previous，采用已有原子状态，不增加逐版本成功标记或历史回退机制 | §5.4、§7.3–7.3.1、§8.1 |
| 2 | bootstrap 重试排空遗漏成立；按实际容器/任务决定，变更前统一排空，完整同镜像核验可保留容器 | §6、§7.3.1、§9.2 |
| 3 | 保留前端的 reload 验收遗漏成立；补真实后端 IP 改变的联调及 reload 失败不记 ready | §9.2、任务 4 |
| 4 | 访问范围表述过大成立；维护仅拒绝普通业务 API，页面/资源仍可访问，不增加前端门闩 | §7.5、§9.2 |

### 12.4 第四轮

| 问题 | 判断与调整 | 落点 |
| --- | --- | --- |
| 任务 2 构建依赖任务 3/4 才创建的文件 | 成立，属于开发顺序倒置；任务 2A 提前实现全部镜像内运行依赖和离线测试（包括 infra 冒烟脚本），2B 再构建完整镜像；3 做真实初始化联调，4 做本机脚本与完整发布验收。不增加功能或两阶段镜像方案 | §5.1、§8、§10 |

### 12.5 实施就绪复核

任务依赖、七服务边界、运行入口及初始化、发布/恢复和验收条件已具备实施依据。补齐两处执行细节：CI 显式收集新增 `deploy/tests/`；bootstrap 对 PG 首次建库中断，通过同一幂等脚本显式重入，不等待 Docker 自动续跑。

可以从任务 1 开始，验证通过后继续 2A → 2B → 3 → 4 → 5 → 6。官方候选镜像和 Milvus 内嵌运行模式、完整后端镜像、七服务真实联调及 Runner 闭环仍是待实测项，不等同于设计阻塞，也不代表已经通过部署验收。接入 GitHub 前落实仓库/账号配置；此前任务不需要先完成 Runner 注册。
