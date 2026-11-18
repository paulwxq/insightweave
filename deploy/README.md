# InsightWeave 部署手册

> 对应设计：[doc 13](../docs/13_项目Docker容器化与本地CI_CD发布设计.md) / [doc 13.1](../docs/13.1_镜像与依赖安装明细.md)
> 首次实施：2026-11-17；全部六项任务已完成。

## 架构概览

```
git push origin main
→ GitHub Actions 触发（.github/workflows/local-release.yml）
→ 本机 Runner（iw-runner 账号，WSL Debian）接单
→ ci.sh：干净 checkout → 离线测试 → 三镜像构建 → 隔离预检
→ deploy.sh release：契约/依赖预检 → 排空 → 替换应用容器 → 两阶段冒烟 → 状态就绪
→ http://localhost:18080 提供完整服务
```

七服务：frontend（Nginx 静态+代理）、metaweave、nl2sql、postgres、neo4j、milvus、attu。

## 部署目录

```
/home/iw-runner/insightweave-runtime/     ← DEPLOY_ROOT（持久，不入 Git）
├── deploy.env               # Compose 插值：数据库密码、镜像 tag
├── config/
│   ├── metaweave.env        # MetaWeave 运行环境（含 LLM key、业务库连接）
│   └── nl2sql.env           # NL2SQL 运行环境（同上 + LangGraph 持久化）
├── control/                 # 维护标记（发布期间自动创建/移除）
├── state/deployment.json    # 当前/上一版本引用与 phase
├── releases/<release_id>/   # manifest.json、Compose、契约及必要相对配置
└── logs/                    # 部署日志（保留最近 20 次）
```

**所有含密钥的文件权限 0600、属主 iw-runner，不入 Git、不进镜像。**

### 发布快照与旧格式过渡

CI 在三个应用镜像预检成功后生成 `deploy/candidate-<release_id>.json`：完整源码 SHA、统一契约版本、三个应用镜像引用和实际 image ID。Actions 显式通过 `CANDIDATE_MANIFEST` 将该路径传给发布脚本；本机手动使用时也可指定此变量，默认查找仓库 `deploy/` 下的同名文件。清单必须与当前 checkout 的源码 SHA 一致。

发布前创建 `releases/<release_id>/manifest.json`，增加四项上游镜像引用，以及 `compose.yaml`、`contract.json`、`postgres/01-create-databases.sh`、`milvus/embedEtcd.yaml`、`milvus/user.yaml` 的 SHA256；这些文件按原相对目录复制。先在同目录临时目录准备，再原子改名；同版本输入不一致时拒绝覆盖。真实应用 env 不复制、不计算凭据哈希。

手动回退和发布失败后的自动恢复均读取目标快照，固定原项目名和当前受限 env 配置，只替换三个应用。实际容器 image ID 与清单一致、普通冒烟通过后才记录成功。仅有候选目录不授予回退资格，目标仍限于 current/previous。

**已有 `manifest.env` 旧快照不会自动视为可信新快照。**如果当前成功版本尚无完整 `manifest.json`，新发布在维护前停止，不影响现有容器。首次使用此修复前，维护者须核对旧版本对应的成功 Actions/完整源码 SHA、三个运行容器和本地镜像的 ID，再从该 Git 提交取出上述五个配置文件（不能用当前 checkout 冒充旧配置）。用新 `release_manifest.py candidate` / `snapshot` 在独立临时目录生成快照，再以 `verify-running` 核验；确认四项上游与当前配置一致后，在持有 `state/.lock` 且 Runner 空闲时保留原目录备份、安装已验证的快照。不改 current/previous，不停容器，不猜测缺失信息。无法还原的历史版本保留为旧格式并明确拒绝自动恢复。

### 维护前的契约与依赖预检

日常 release 在写入 `deploying`、维护标记及调用 enter 之前，核验四项运行中上游容器的实际 image ID，并精确解析两个后端的控制状态：契约必须是整数 `1`，`maintaining` 必须是布尔值，`active` 必须是非负整数。`active > 0` 允许通过预检，在随后排空阶段等待；字符串 `"1"`、布尔值或版本 `10` 均不冒充契约 `1`。预检失败不关闭业务入口、不替换应用，也不改变成功版本引用。

随后分别使用目标 MetaWeave、NL2SQL 镜像及当前 env，调用已有的纯配置解析入口，检查平台数据库配置、MetaWeave CORS 及部署开关/令牌/契约；每项最多 30 秒，不启动 API、不连库或调用模型。两项通过后，再以 `docker compose run --rm --no-deps --pull never -T --entrypoint python metaweave /app/deploy/scripts/smoke.py --scope infra` 执行既有只读检查：两个 PG 库和必要对象、Neo4j/APOC、Milvus 目标库。上述短时检查容器不初始化数据、不发布宿主端口，完成后删除，不增加常驻服务；它们复用服务的卷挂载，检查代码不写这些卷。控制请求最多 10 秒，单次 Docker 状态读取最多 30 秒，infra 检查最多 60 秒；超时后按唯一容器名尝试清理，清理最多 10 秒。发布日志只报告失败组件，不回显配置解析或 SDK 原始异常及凭据。

首次 bootstrap 在完成库表初始化、四项上游启动后执行完整预检，再启动前端并冒烟。手动回退与失败恢复允许跳过 Docker 已确认不存在/已停止的后端；仍在运行但控制接口不可达或契约不符时拒绝恢复。排空阶段重新读取容器状态，只对当时仍运行的后端 enter，不将读取失败当作已经停止。每次排空轮询复用同一严格 JSON 解析，并要求 `maintaining=true`；传输失败、截断 JSON 或尚未进入维护均不能当作活动数 0，不替换容器。

### 同版本核验与恢复

`bootstrap <current>`、`release <current>` 只核验，不替换容器或重新初始化。除镜像身份和普通请求外，还要求无维护标记、两个后端均已开放；失败保留 current/previous 并标记 `recovery_required`，提示执行 `rollback <current>`。

`rollback <current>` 在 ready 且现场核验通过时不做替换。发现维护标记残留、门闩未开放、镜像不符或普通冒烟失败时，进入已有的预检、排空、应用恢复及两阶段冒烟流程；恢复当前版本不改变 previous。存活后端控制不可达时仍拒绝替换，不以恢复为理由强杀。

首次 bootstrap 即使没有旧容器，也先写维护标记，再启动服务并初始化。中断重试只排空已确认仍运行的后端，只有数据库容器的现场可以继续。排空超时不替换容器：正常发布尝试 leave 后，须核验原版本的镜像、开放状态和普通请求，全部通过才写回 ready；失败则保持/重新进入维护并标记 `recovery_required`。原先已处于维护的恢复现场，超时继续保持关闭。leave 或普通冒烟失败后，自动恢复先重新写维护标记、enter 并排空，再执行旧版本恢复预检；预检失败也不开放未知现场。本次已经开始的变更若异常退出，退出处理对尚未收尾的 deploying 重建维护标记、尽力 enter 存活后端，再记录 `recovery_required`，不自动标绿或改写版本引用；控制不可达时不能保证该进程已关闭入口，须人工处理。进程被强制终止仍可能保留 deploying，同属需要核验的非就绪现场。

前端保留同时要求镜像引用与实际 image ID 均匹配目标；同标签但实际 ID 不符时强制重建。保留前端则在后端就绪后执行 `nginx -t` 和 reload，再做两阶段冒烟，不能只凭标签相同认定已恢复。

### APOC 文件校验（问题 7，已修正）

插件准备及同版本 bootstrap 的只读核验已接入；实施细节、修改文件及测试范围见 [doc 13.1 §2.2](../docs/13.1_镜像与依赖安装明细.md#22-apoc-离线准备首次启动与重启)。

`prepare_neo4j_plugin()` 从已核验快照读取 `NEO4J_IMAGE`，以镜像中唯一的 APOC Core JAR 为基准核对 SHA256。Neo4j 未运行且卷中没有 APOC 时才复制：先写卷内临时文件、设置 `0644`、校验，再改名为 `apoc.jar`。已有同 SHA 的 `apoc.jar` 保留不动；SHA 不同、重复版本或未知命名的 APOC 文件均停止并交由人工处理，不覆盖或清卷，不影响其他正常插件。建卷及文件操作失败会非零退出，不继续启动服务；失败只清理本次临时文件。

首次 bootstrap 和无成功版本的中断重试，在启动 Neo4j 前准备或核验；运行中的 Neo4j 只读检查其实际插件卷，缺失或冲突时报错，不写卷，状态未知同样停止。同版本 `bootstrap <current>` 也执行只读插件核验，失败标记 `recovery_required` 并保留版本引用；日常应用 release/rollback 不重新安装或升级插件。

22 项专项回归已通过；另用固定 Neo4j 镜像和独立临时卷验证了首次准备、UID/GID `7474:7474` 只读校验、损坏 JAR 拒绝且原文件保留，临时资源已清理。文件 SHA 一致不能替代运行检查：首次启动及重启仍须验证 APOC 版本和应用所需过程。本轮未启动数据库进程，未修改现有 Neo4j 卷，也未执行正式部署故障注入。

### 宿主端口预检（M01）

端口预检使用 Python 标准库 TCP 探测，检查 `18080`、`3000`、`7474`、`7687`、`6432`，不发送 HTTP、SQL 或 Bolt 请求。先读取运行容器的 Compose 项目/服务标签和实际 TCP 发布映射：只有本项目正确服务的 `127.0.0.1:宿主端口 → 预期容器端口` 才放行。其他项目、错误服务、错误容器端口或通配地址发布都拒绝；不会因为“本项目有任一容器运行”而放行其他占用。读取只包含必要字段，不输出完整 inspect 或容器环境。

对未被合法映射占用的端口，WSL 还通过 Windows 自带 PowerShell 的 `Get-NetTCPConnection` 只读查询宿主回环/通配 TCP 监听，再检查 WSL 本地 TCP。实测 Windows 独立监听并不一定能从 WSL 的 `127.0.0.1` 连接到，所以两侧不能混为一个检查。Runner 账号须允许执行 Windows PowerShell：优先从 PATH 查找 `powershell.exe`，其次使用标准 `/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe`；查询限时 10 秒，不查询/停止进程、不修改 Windows 配置，也不经过代理。无法执行、返回失败或响应无效时停止发布，不按“端口空闲”处理。

TCP 连接被拒绝表示未发现监听；超时、权限错误或 Docker 状态不明均报错。预检在维护及替换前执行，实际 rollback 和失败恢复同样检查；同版本只读核验也检查归属，失败按原规则标记 `recovery_required` 并保留版本引用。发现冲突后由维护者释放端口，不自动停服务、清理容器或换端口。预检不锁定端口，也不能证明不存在非监听状态的占用/系统端口保留；随后 Docker 的真实绑定失败仍按现有失败恢复规则处理。

本轮已用随机临时端口验证 Windows 独立监听被识别、真实 Docker 正确项目/服务映射被放行且换项目后被拒绝；临时监听和容器均已清理。`iw-runner` 账号的只读 PowerShell 查询也已通过，未触发正式 Actions 发布。验证日志：`/tmp/iw-m01-live-probe.log`。

## 日常操作

### 发布（全自动）

```bash
git push origin main    # 这就是你唯一需要做的事
```

### 手动发布/回退

```bash
D=/home/iw-runner/insightweave-runtime
sudo -u iw-runner env DEPLOY_ROOT=$D ENV_FILE=$D/deploy.env \
  PROJECT=insightweave-local \
  PYTHON_BIN=/home/iw-runner/.venv-smoke/bin/python \
  bash deploy/scripts/deploy.sh release <release_id>     # 发布指定版本

sudo -u iw-runner env DEPLOY_ROOT=$D ENV_FILE=$D/deploy.env \
  PROJECT=insightweave-local \
  PYTHON_BIN=/home/iw-runner/.venv-smoke/bin/python \
  bash deploy/scripts/deploy.sh rollback <release_id>    # 回退（仅 current/previous）
```

### 修改配置（换 LLM key、改业务库连接等）

```bash
sudo nano /home/iw-runner/insightweave-runtime/config/metaweave.env
sudo nano /home/iw-runner/insightweave-runtime/config/nl2sql.env
```

**生效方式**：在 GitHub → Actions → InsightWeave local release → Run workflow 手动触发。流水线会自动选择正确版本、排空在途任务、重新创建后端容器（读取新配置）、两阶段冒烟验证后标记就绪。

**注意**：`docker compose restart` 不会加载新配置（沿用创建时的 env）；手动 `--force-recreate` 会使用 deploy.env 中的镜像 tag（可能指向旧版本）且跳过排空与冒烟。**不推荐绕过流水线手动操作**；确需手动时理解上述风险。

## 受维护离线测试与排除原因（doc 13 §7.2）

`ci.sh` 每次在构建镜像之前执行以下范围，任何一组失败即停止，不进入构建或发布：

| 环境 | 测试范围 |
|---|---|
| NL2SQL `.venv` | `src/tests/unit/api`、`src/tests/unit/services`、`src/tests/unit/utils`、`tests/unit/db_deepagent/test_independent_summarizer.py` |
| NL2SQL `.venv`，独立 pytest 进程 | **`src/tests/db_deepagent`**，Agent 核心离线回归 |
| NL2SQL `.venv` | 仓库根 `deploy/tests/` |
| MetaWeave `.venv` | `tests/unit`，排除项见下表 |
| 前端 Node 22 | `src/` 下全部 `.test.cjs`、`.test.ts`，以及类型检查 |

Agent 核心套件使用 `pytest src/tests/db_deepagent -q -ra`，不传 `--run-db`、`--run-llm`；已有收集钩子默认跳过 `requires_db`、`requires_llm` 标记的实库/真实模型用例，并显示跳过原因。CI 仅注入哑配置，不依赖开发机 `.env` 或真实密钥。跳过不等于通过，实际通过/跳过数量以当次 CI 输出为准，不固定测试数量。

CLI JSON 输出单测仍运行真实子进程，但对 doctor 的业务库/平台库诊断使用替身，并在子进程内禁止打开真实 PG 连接池；它验证 JSON 单文档和诊断信息重定向，不以数据库可达性作为通过条件。

| 被排除的测试 | 原因 |
|---|---|
| `metaweave/tests/unit/metaweave/relationships/test_repository.py` | 12 例硬依赖本机历史产物 `output/json/*.json`（干净 checkout 不存在，空目录亦失败） |
| `metaweave/tests/unit/*_live.py`、`test_read_tx_isolation.py` | 实库断言，需 `METAWEAVE_RUN_LIVE_TESTS=1` 显式开关 + 真实数据库（本地开发时按需启用） |

## NL2SQL 日志级别

Docker 镜像默认 `NL2SQL_LOG_LEVEL=INFO`（覆盖 YAML 的 DEBUG），完整提示词不输出、日志量大幅减少。

**临时排查**：编辑 `/home/iw-runner/insightweave-runtime/config/nl2sql.env`，取消注释 `NL2SQL_LOG_LEVEL=DEBUG`，然后在 Actions 手动 Run workflow（自动排空+重建+冒烟）。恢复时改回 INFO 并重跑。

**注意**：修改后需**重新创建容器**（`--force-recreate`），`restart` 不加载新 env（见上）。

## 已知边界（M-4 / M-7）

- **M-4**：`init_platform.py nl2sql --mode initialize` 将连接错误也包装为 `DeployInitConflict`（exit 4），与结构冲突共用退出码。错误消息中已含"连接问题"提示，人工排查可区分；如需脚本自动分支，可按异常类型拆分。
- **M-7**：MetaWeave 结构核验为"表名 + 列名 + 列类型 + 序列/视图/索引存在性"级别，不校验约束、默认值、列长度精度。首版可接受；如有需要可在 `verify_metaweave_objects` 中扩展。

## 手动备份与恢复（doc 13 §5.5）

**状态：M02 操作说明已补齐，真实备份及恢复演练未执行，恢复能力仍标记“未验证”。**本节是人工维护步骤，不新增备份服务或定时任务；只保留卷不能代替备份。备份保存到 Docker/WSL 数据盘之外的受限目录，并另存一份配置和原 `SOURCE_CREDENTIAL_KEY` 到独立安全保管位置，不放入 Git、Actions artifact 或普通日志。

本轮仅静态检查命令，并在固定 PostgreSQL 镜像中读取工具版本：GNU tar 1.35、pg_dump/pg_restore 18.1；该检查没有挂载数据卷或启动数据库，不能作为备份/恢复演练通过的证据。

### 备份内容和一致性窗口

| 内容 | 当前容器路径/来源 | 方式 |
|---|---|---|
| MetaWeave 平台库、NL2SQL 持久化库 | PostgreSQL 的 `metaweave`、`lg_persist`（使用容器内实际库名） | 同一停写窗口中的两个 custom-format dump |
| Neo4j 数据及 APOC | `neo4j` 的 `/data`、`/plugins` | 停止 Neo4j 后分别归档完整卷 |
| Milvus 完整数据 | `milvus` 的 `/var/lib/milvus` | 停止 Milvus 后归档完整卷，包含本方案内嵌 etcd、消息队列和本地存储 |
| MetaWeave 产物 | `/app/metaweave/output` | 应用停止后归档 `metaweave-output` 卷 |
| NL2SQL 本地工作目录及结果文件 | `/app/nl2sql/var/db_deepagent` | 应用停止后归档 `nl2sql-workspace` 卷，与同次 PG dump 配套 |
| 配置、原密钥及版本证据 | `deploy.env`、实际后端 env、`state/deployment.json`、成功版本快照、实际镜像引用/ID | 受限复制；当前三个应用镜像另行 `docker image save` |
| 日志（按排障需要） | `neo4j-logs`、`metaweave-logs`、`nl2sql-logs`、部署 `logs/` | 停止写入后可用相同方法额外备份，不作为业务恢复必需文件 |

PG 两个 dump 不天然是跨库一致快照。整个窗口禁止应用及 PG 客户端、Neo4j Browser、Attu 等管理入口写入，不提交采集/发布/问数；不备份或改写外部业务库。应用先维护排空，再停止，随后备份两库及文件。不要仅停止数据库而让应用继续写文件或接收任务。

备份前确认受限 env 与现有容器的启动配置一致；窗口中不修改配置或轮换密钥，避免把使用旧密钥写入的数据与新配置混为一套备份。

### 1. 固定操作对象并进入维护

以 `iw-runner` 在同一个 Bash 维护终端按顺序执行各段；先设置 `BACKUP` 为一个**不存在的绝对目录**，例如 Windows 独立盘下的 `/mnt/d/InsightWeave-backups/backup-20261118-120000`，按本机实际盘符调整。目录应仅维护者可访问，Windows 目录还须核对 NTFS 权限。失败后停在当前步骤，保留维护标记和诊断，不用 `down -v`、删卷或自动开放入口“收尾”。

```bash
set -euo pipefail
umask 077
D=/home/iw-runner/insightweave-runtime
PROJECT=insightweave-local
CODE_ROOT=/opt/projects/insightweave  # 改为本次经审核的源码位置
PYTHON_BIN=/home/iw-runner/.venv-smoke/bin/python
: "${BACKUP:?先设置 Docker/WSL 数据目录之外的新备份目录}"
test "${BACKUP:0:1}" = / && test ! -e "$BACKUP"
mkdir -p "$BACKUP/private"
exec 9>"$D/state/.lock"
flock -n 9  # 与发布共用锁；拿不到锁就停止，不能并发发布/备份
RID=$(python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); assert s["phase"]=="ready" and s["current"]; print(s["current"])' "$D/state/deployment.json")
COMPOSE_FILE="$D/releases/$RID/compose.yaml"
test -f "$COMPOSE_FILE" && test ! -e "$D/control/maintenance"
DC=(docker --context default compose --project-directory "$(dirname "$COMPOSE_FILE")"
    -f "$COMPOSE_FILE" --env-file "$D/deploy.env" --project-name "$PROJECT")

control() {
  local svc="$1" op="$2" port prefix method=POST
  case "$svc" in
    metaweave) port=8001; prefix=/api/_deploy ;;
    nl2sql) port=8008; prefix=/api/v2/_deploy ;;
    *) return 1 ;;
  esac
  [ "$op" != state ] || method=GET
  "${DC[@]}" exec -T "$svc" sh -eu -c '
    : "${DEPLOY_CONTROL_TOKEN:?缺少控制令牌}"
    printf "X-InsightWeave-Deploy-Token: %s\n" "$DEPLOY_CONTROL_TOKEN" |
      curl --noproxy "*" --fail --silent --show-error --connect-timeout 3 --max-time 10 \
        --header @- -X "$1" "$2"' _ "$method" "http://127.0.0.1:$port$prefix/$op"
}
read_active() {
  control "$1" state | python3 -c 'import json,sys; s=json.load(sys.stdin); assert type(s["deployment_contract_version"]) is int and s["deployment_contract_version"]==1 and s["maintaining"] is True; n=s["active"]; assert type(n) is int and n>=0; print(n)'
}
mkdir -p "$D/control"
: > "$D/control/maintenance"
control metaweave enter >/dev/null
control nl2sql enter >/dev/null
backup_deadline=$((SECONDS+900))
while :; do
  active_mw=$(read_active metaweave)
  active_nl=$(read_active nl2sql)
  [ "$active_mw" -ne 0 ] || [ "$active_nl" -ne 0 ] || break
  [ "$SECONDS" -lt "$backup_deadline" ] || { printf '排空超时，停止备份并保留维护状态\n' >&2; exit 1; }
  sleep 2
done
"${DC[@]}" stop frontend metaweave nl2sql attu
```

控制不可达或状态未知时停止，不以停止宽限期替代排空。这里的 `stop/start/exec` 都固定同一 Compose、env 和项目，不执行 `up`，不因 `deploy.env` 中旧应用标签而重建容器。

### 2. 备份两库、配置和镜像

密码只从 PG 容器现有环境供命令使用，不在宿主读取/打印密码。两个后端及其他客户端已停止写入后执行：

```bash
"${DC[@]}" exec -T postgres sh -eu -c '
  export PGPASSWORD="$POSTGRES_PASSWORD"
  exec pg_dump -U "$POSTGRES_USER" -d "$PLATFORM_DB_NAME" -Fc --no-owner --no-acl' > "$BACKUP/metaweave.dump.partial"
test -s "$BACKUP/metaweave.dump.partial" && mv "$BACKUP/metaweave.dump.partial" "$BACKUP/metaweave.dump"
"${DC[@]}" exec -T postgres sh -eu -c '
  export PGPASSWORD="$POSTGRES_PASSWORD"
  exec pg_dump -U "$POSTGRES_USER" -d "$LANGGRAPH_DB_NAME" -Fc --no-owner --no-acl' > "$BACKUP/lg_persist.dump.partial"
test -s "$BACKUP/lg_persist.dump.partial" && mv "$BACKUP/lg_persist.dump.partial" "$BACKUP/lg_persist.dump"
"${DC[@]}" exec -T postgres pg_restore --list < "$BACKUP/metaweave.dump" >/dev/null
"${DC[@]}" exec -T postgres pg_restore --list < "$BACKUP/lg_persist.dump" >/dev/null
cp -a "$D/config" "$BACKUP/private/"
cp -a "$D/deploy.env" "$BACKUP/private/"
cp -a "$D/state/deployment.json" "$BACKUP/"
cp -a "$D/releases/$RID" "$BACKUP/release"

record_images() {
  local svc cid
  for svc in frontend metaweave nl2sql postgres neo4j milvus attu; do
    cid=$("${DC[@]}" ps --all -q "$svc"); test -n "$cid"
    printf '%s ' "$svc"
    docker --context default inspect --format '{{.Config.Image}} {{.Image}}' "$cid"
  done
}
record_images > "$BACKUP/images.txt"
PG_IMAGE=$(awk '$1=="postgres" {print $3}' "$BACKUP/images.txt")
app_images=()
while read -r svc ref image_id; do
  case "$svc" in
    frontend|metaweave|nl2sql)
      test "$(docker --context default image inspect --format '{{.Id}}' "$ref")" = "$image_id"
      app_images+=("$ref") ;;
  esac
done < "$BACKUP/images.txt"
test "${#app_images[@]}" = 3
docker --context default image save -o "$BACKUP/app-images.tar.partial" "${app_images[@]}"
mv "$BACKUP/app-images.tar.partial" "$BACKUP/app-images.tar"
"${DC[@]}" stop postgres neo4j milvus
```

若实际后端 env 在 `$D/config/` 之外，也须受限备份这些文件，并记录恢复时的新路径。两个后端的 `SOURCE_CREDENTIAL_KEY` 必须与备份数据库中的加密凭据匹配；不能为恢复重新生成密钥。本地构建的应用镜像没有 GHCR 副本，因此同时保存三个应用镜像；四项固定上游镜像按记录的 digest 获取，完全离线恢复时也需事先保存。额外自定义的 PG 角色/授权不包含在上述两库 dump 中，如有则另行记录。旧格式发布快照可以先随数据保留，但完整恢复前仍须按前文补齐核验信息，不能猜测源码或镜像身份。

### 3. 冷备完整数据卷及应用文件卷

复用已有 PostgreSQL 镜像中的 tar，不新增 Alpine 等镜像。卷名从本项目实际容器挂载读取，先确认卷存在，源卷只读挂载，不允许拼错卷名后自动创建空卷冒充备份。

```bash
archive_volume() {
  local svc="$1" destination="$2" archive="$3" cid volume
  cid=$("${DC[@]}" ps --all -q "$svc"); test -n "$cid"
  volume=$(docker --context default inspect --format "{{range .Mounts}}{{if and (eq .Destination \"$destination\") (eq .Type \"volume\")}}{{.Name}}{{end}}{{end}}" "$cid")
  test -n "$volume"
  docker --context default volume inspect "$volume" >/dev/null
  printf '%s %s %s\n' "$svc" "$destination" "$volume" >> "$BACKUP/volumes.txt"
  docker --context default run --rm --pull never --network none --user 0:0 --entrypoint tar \
    --mount "type=volume,source=$volume,target=/source,readonly,volume-nocopy" \
    --mount "type=bind,source=$BACKUP,target=/backup" \
    "$PG_IMAGE" -cpf "/backup/$archive.tar.partial" -C /source .
  mv "$BACKUP/$archive.tar.partial" "$BACKUP/$archive.tar"
}
archive_volume neo4j /data neo4j-data
archive_volume neo4j /plugins neo4j-plugins
archive_volume milvus /var/lib/milvus milvus-data
archive_volume metaweave /app/metaweave/output metaweave-output
archive_volume nl2sql /app/nl2sql/var/db_deepagent nl2sql-workspace
(cd "$BACKUP" && sha256sum *.dump *.tar images.txt volumes.txt > SHA256SUMS)
```

归档保留文件权限和 UID/GID。`pg-data` 不在冷拷贝清单内，PG 以两个逻辑 dump 恢复；日志卷如需保留，在服务停止期间用同一函数另存。任一步失败都只得到不完整备份，不能因已有几个 tar 文件就宣称整套备份成功；`pg_restore --list` 和校验和也不能替代实际恢复演练。

### 4. 恢复本次维护前的服务

先启动数据库并确认健康，再启动原应用容器；保持维护标记直到原镜像身份、基础设施及应用冒烟全部通过。以下轮询最多等待每组 300 秒，失败不会强制停止其他服务或开放入口。

```bash
backup_wait_healthy() {
  local svc cid health backup_health_deadline=$((SECONDS+300))
  for svc in "$@"; do
    while :; do
      cid=$("${DC[@]}" ps -q "$svc"); test -n "$cid"
      health=$(docker --context default inspect --format '{{.State.Health.Status}}' "$cid") || return 1
      [ "$health" != healthy ] || break
      [ "$SECONDS" -lt "$backup_health_deadline" ] || return 1
      sleep 3
    done
  done
}
"${DC[@]}" start postgres neo4j milvus
backup_wait_healthy postgres neo4j milvus
"${DC[@]}" start metaweave nl2sql
backup_wait_healthy metaweave nl2sql
"${DC[@]}" start frontend attu
backup_wait_healthy frontend attu
record_images > "$BACKUP/images-after.txt"
cmp "$BACKUP/images.txt" "$BACKUP/images-after.txt"
"${DC[@]}" exec -T metaweave python /app/deploy/scripts/smoke.py --scope infra
backup_token=$("${DC[@]}" exec -T metaweave sh -eu -c 'printf "%s" "$DEPLOY_CONTROL_TOKEN"')
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
  "DEPLOY_CONTROL_TOKEN=$backup_token" "$PYTHON_BIN" "$CODE_ROOT/deploy/scripts/smoke.py" --scope apps --mode maintenance
unset backup_token
if rm -f "$D/control/maintenance" && control metaweave leave >/dev/null && control nl2sql leave >/dev/null &&
   env -u DEPLOY_CONTROL_TOKEN -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
     "$PYTHON_BIN" "$CODE_ROOT/deploy/scripts/smoke.py" --scope apps --mode public; then
  flock -u 9
  exec 9>&-
else
  : > "$D/control/maintenance"
  control metaweave enter >/dev/null 2>&1 || true
  control nl2sql enter >/dev/null 2>&1 || true
  printf '入口恢复未确认，请保留维护状态并人工核验\n' >&2
  exit 1
fi
```

本节不改写 current/previous 或复制旧状态回生产。失败时先核验现场，必要时按既有 `rollback <current>` 入口恢复应用；应用 rollback 不恢复数据库或结果文件，不能把它当作数据恢复命令。

### 5. 在独立环境恢复备份并验收（尚未演练）

1. **检查介质和版本。**在备份目录执行 `sha256sum -c SHA256SUMS`，确认两库 dump、五个数据/插件/文件卷归档、配置和原密钥齐全。用 `docker --context default image load -i "$BACKUP/app-images.tar"` 加载应用镜像，按 `images.txt` 核对实际 image ID；四项上游使用同一固定版本，不在恢复中升级。检查 tar 内容与 PG dump 清单只能作为预检。
2. **准备独立副本。**选择不存在的新演练目录，禁止使用生产 `$D` 或其子目录；复制成功版本的 Compose 及其相对配置、私有 env 到该目录，并修正 `METAWEAVE_ENV_FILE`、`NL2SQL_ENV_FILE`、`DEPLOY_CONTROL_DIR` 路径。镜像使用 `images.txt` 的实际引用，不照抄可能过期的应用标签。项目名另取如 `insightweave-restore-20261118`，所有数据卷使用下方命令对应的独立名称。尤其须把 Compose 中固定的 external `neo4j-plugins.name` 改为 `${RESTORE_PROJECT}_neo4j-plugins` 的实际值；只改 `--project-name` 不会隔离这个外部卷。端口改为确认空闲的临时端口或删除不需要的映射，不能沿用生产五个端口。
3. **保持演练入口关闭。**建立独立 `control/maintenance`，两个后端保留部署门闩，并在演练 MetaWeave env 中设置 `METAWEAVE_DELETION_STARTUP_SCAN=0`。先不启动应用和 Neo4j/Milvus，不执行 bootstrap 或初始化器补写已恢复数据。演练 Compose 与 env 必须经人工核对卷名、端口和全部文件路径，且 `config --quiet` 通过；不要把其修改后的 Compose 冒充生产发布快照，也不要交给固定生产端口/插件卷的 `deploy.sh` 执行。
4. **先恢复五个文件卷。**在任何 Compose `up` 之前，逐个创建不存在的独立目标卷，用下方 tar 命令恢复同次归档，避免 Compose 提前创建卷后与“新卷”检查冲突。保留数字 UID/GID；APOC 卷同样恢复并按固定镜像核验。未归档日志卷可以使用新的空卷。确认 PG、Neo4j、Milvus 和结果文件属于同一备份窗口，不能拼接不同日期的数据。
5. **再恢复 PG。**只启动演练 PG，在其新卷完成建库并健康后，通过容器内实际 `POSTGRES_USER`、`PLATFORM_DB_NAME`、`LANGGRAPH_DB_NAME` 向两个空库执行下方 `pg_restore`。不使用 `--clean`、不删除冲突对象；若目标已有业务对象，停止并换新的独立空库/卷，不能覆盖现有环境。
6. **启动并检查。**先检查数据库健康、PG 必要表/历史记录、Neo4j 节点关系及 APOC、Milvus 目标库/集合和已知样本，再启动门闩关闭的应用。检查历史与本地结果文件对应、已登记源的加密凭据能用原密钥解密；只报告成功/失败，不输出明文、不连接原业务库。空库没有样本时不能宣称解密验证通过。文件/SDK 检查不需要真实 LLM，不自动提交采集、发布或问数。

演练命令必须固定新的路径和项目；后续操作统一使用 `RDC`，不复用生产 `DC`：

```bash
set -euo pipefail
umask 077
D=/home/iw-runner/insightweave-runtime
PROJECT=insightweave-local
: "${BACKUP:?先指定本次备份目录}"
: "${RESTORE_ROOT:?先设置独立演练目录的绝对路径}"
: "${RESTORE_PROJECT:?先设置独立演练项目名}"
test "$RESTORE_PROJECT" != "$PROJECT" && test "$RESTORE_ROOT" != "$D"
case "$RESTORE_ROOT/" in "$D/"*) printf '拒绝使用生产目录或其子目录\n' >&2; exit 1 ;; esac
while read -r svc ref image_id; do
  test "$(docker --context default image inspect --format '{{.Id}}' "$ref")" = "$image_id"
  case "$svc" in frontend|metaweave|nl2sql|postgres|neo4j|milvus|attu) export "${svc^^}_IMAGE=$ref" ;; esac
done < "$BACKUP/images.txt"
PG_IMAGE=$(awk '$1=="postgres" {print $3}' "$BACKUP/images.txt")
RDC=(docker --context default compose --project-directory "$RESTORE_ROOT/release"
     -f "$RESTORE_ROOT/release/compose.yaml" --env-file "$RESTORE_ROOT/deploy.env"
     --project-name "$RESTORE_PROJECT")
"${RDC[@]}" config --quiet
restore_volume() {
  local archive="$1" target="$RESTORE_PROJECT"_"$1"
  test -f "$BACKUP/$archive.tar"
  if docker --context default volume inspect "$target" >/dev/null 2>&1; then
    printf '目标卷已存在，停止以避免覆盖：%s\n' "$target" >&2; return 1
  fi
  docker --context default volume create "$target" >/dev/null
  docker --context default run --rm --pull never --network none --user 0:0 --entrypoint tar \
    --mount "type=bind,source=$BACKUP,target=/backup,readonly" \
    --mount "type=volume,source=$target,target=/target,volume-nocopy" \
    "$PG_IMAGE" --numeric-owner -xpf "/backup/$archive.tar" -C /target
}
restore_volume neo4j-data
restore_volume neo4j-plugins
restore_volume milvus-data
restore_volume metaweave-output
restore_volume nl2sql-workspace
"${RDC[@]}" up -d --no-deps postgres
```

确认演练 PG 已 healthy、两库为空后，再单独执行下一段；随后才启动演练 Neo4j/Milvus 和应用并按步骤 6 验收：

```bash
"${RDC[@]}" exec -T postgres sh -eu -c '
  export PGPASSWORD="$POSTGRES_PASSWORD"
  exec pg_restore -U "$POSTGRES_USER" -d "$PLATFORM_DB_NAME" --exit-on-error --single-transaction --no-owner --no-acl' < "$BACKUP/metaweave.dump"
"${RDC[@]}" exec -T postgres sh -eu -c '
  export PGPASSWORD="$POSTGRES_PASSWORD"
  exec pg_restore -U "$POSTGRES_USER" -d "$LANGGRAPH_DB_NAME" --exit-on-error --single-transaction --no-owner --no-acl' < "$BACKUP/lg_persist.dump"
```

演练前端的构建 API origin 仍可能固定为 `localhost:18080`；只改宿主映射不改变 JavaScript 请求地址。不要直接在演练 Vue 页面操作，以免请求生产 API。首轮以内部 SDK/后端检查和显式演练地址的 HTTP 验收为主；如需浏览器演练，应另行准备 API origin 与演练入口一致的前端。演练结束只按独立项目及明确列出的新卷清理，不运行生产 `down -v` 或全局 prune。

恢复成功后记录备份标识、实际镜像 ID、独立项目/卷/端口、验收结果和失败项，再将“恢复未验证”改为相应的已验证范围。命令静态检查、归档存在、应用启动或健康检查通过，都不足以单独证明数据、历史、结果文件和解密能力已经恢复。

## 旧版本清理

```bash
D=/home/iw-runner/insightweave-runtime
CURRENT=$(python3 -c "import json; print(json.load(open('$D/state/deployment.json'))['current'])")
PREVIOUS=$(python3 -c "import json; print(json.load(open('$D/state/deployment.json'))['previous'])")

# 列出所有应用镜像 tag
docker images --format '{{.Repository}}:{{.Tag}}' | grep insightweave/ | sort

# 手动删除：始终保留 current 和 previous 对应的 tag
# 核对 releases/<release_id>/manifest.json 的镜像引用后再删除旧 tag
# 不按模糊名称匹配、不删数据卷
```

BuildKit 缓存另行查看（`docker system df`），人工判断后处理，不让日常发布顺便执行全局 prune。

## 浏览器入口

| 入口 | 地址 |
|---|---|
| 前端（主入口） | http://localhost:18080 |
| Attu（Milvus 管理） | http://localhost:3000 |
| Neo4j Browser | http://localhost:7474 （连接地址 `bolt://localhost:7687`） |
| 本机 PG 客户端 | `127.0.0.1:6432`（库名 `metaweave` / `lg_persist`） |
