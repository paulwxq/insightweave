# InsightWeave 部署手册

> 对应设计：[doc 13](../docs/13_项目Docker容器化与本地CI_CD发布设计.md) / [doc 13.1](../docs/13.1_镜像与依赖安装明细.md)
> 首次实施：2026-11-17；全部六项任务已完成。

## 架构概览

```
git push origin main
→ GitHub Actions 触发（.github/workflows/local-release.yml）
→ 本机 Runner（iw-runner 账号，WSL Debian）接单
→ ci.sh：干净 checkout → 离线测试 → 三镜像构建 → 隔离预检
→ deploy.sh release：排空 → 替换应用容器 → 两阶段冒烟 → 状态就绪
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
├── releases/<release_id>/   # 每个版本的 Compose 快照与镜像清单
└── logs/                    # 部署日志（保留最近 20 次）
```

**所有含密钥的文件权限 0600、属主 iw-runner，不入 Git、不进镜像。**

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
# 生效：
sudo -u iw-runner docker --context default compose \
  -f deploy/compose.yaml --env-file $D/deploy.env \
  --project-name insightweave-local restart metaweave nl2sql
```

## 测试排除原因（doc 13 §7.2）

| 被排除的测试 | 原因 |
|---|---|
| `metaweave/tests/unit/metaweave/relationships/test_repository.py` | 12 例硬依赖本机历史产物 `output/json/*.json`（干净 checkout 不存在，空目录亦失败） |
| `metaweave/tests/unit/*_live.py`、`test_read_tx_isolation.py` | 实库断言，需 `METAWEAVE_RUN_LIVE_TESTS=1` 显式开关 + 真实数据库（本地开发时按需启用） |

## 已知边界（M-4 / M-7）

- **M-4**：`init_platform.py nl2sql --mode initialize` 将连接错误也包装为 `DeployInitConflict`（exit 4），与结构冲突共用退出码。错误消息中已含"连接问题"提示，人工排查可区分；如需脚本自动分支，可按异常类型拆分。
- **M-7**：MetaWeave 结构核验为"表名 + 列名 + 列类型 + 序列/视图/索引存在性"级别，不校验约束、默认值、列长度精度。首版可接受；如有需要可在 `verify_metaweave_objects` 中扩展。

## 手动备份与恢复（doc 13 §5.5）

### 备份

```bash
D=/home/iw-runner/insightweave-runtime
BACKUP=/path/to/backup-$(date +%Y%m%d)
mkdir -p "$BACKUP"

# 1. 进入维护排空后，PG 两库逻辑备份
PGPW=$(sudo grep '^POSTGRES_PASSWORD=' $D/deploy.env | cut -d= -f2)
docker exec insightweave-local-postgres-1 \
  pg_dump -U postgres -d metaweave > "$BACKUP/metaweave.sql"
docker exec insightweave-local-postgres-1 \
  pg_dump -U postgres -d lg_persist > "$BACKUP/lg_persist.sql"

# 2. 停库后冷拷贝卷（Neo4j / Milvus）
docker compose -p insightweave-local stop neo4j milvus
docker run --rm -v insightweave-local_neo4j-data:/data -v "$BACKUP":/backup alpine \
  cp -a /data /backup/neo4j-data
docker run --rm -v insightweave-local_milvus-data:/var/lib/milvus -v "$BACKUP":/backup alpine \
  cp -a /var/lib/milvus /backup/milvus-data
docker compose -p insightweave-local start neo4j milvus

# 3. 配置与密钥
sudo cp -a $D/config "$BACKUP/"
sudo cp $D/deploy.env "$BACKUP/"

# ⚠️ 备份放在 Docker/WSL 数据目录之外；SOURCE_CREDENTIAL_KEY 丢失 = 已登记数据源密码不可解密
```

### 恢复

用相同镜像版本恢复到独立项目名和独立卷（禁止覆盖现有卷演练），验证库表、历史、结果文件及解密能力。**实际恢复演练完成前，交付记录标"恢复未验证"。**

## 旧版本清理

```bash
D=/home/iw-runner/insightweave-runtime
CURRENT=$(python3 -c "import json; print(json.load(open('$D/state/deployment.json'))['current'])")
PREVIOUS=$(python3 -c "import json; print(json.load(open('$D/state/deployment.json'))['previous'])")

# 列出所有应用镜像 tag
docker images --format '{{.Repository}}:{{.Tag}}' | grep insightweave/ | sort

# 手动删除：始终保留 current 和 previous 对应的 tag
# 只删 deploy.env / manifest.env 中不再被引用的旧 tag
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
