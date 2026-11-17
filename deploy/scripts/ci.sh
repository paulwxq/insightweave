#!/usr/bin/env bash
# ci.sh —— 本地 CI（doc 13 §7.2；任务 4）：干净源码 → 离线测试 → 构建三镜像 → 候选预检
# 用法：ci.sh <release_id>   与 GitHub Actions 调同一脚本；仅本机执行。
set -euo pipefail

RELEASE_ID="${1:?用法：ci.sh <release_id>}"
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$BASH_SOURCE")/../.." && pwd)}"
PYTHON_BIN="${INSIGHTWEAVE_CI_PYTHON:?需要 INSIGHTWEAVE_CI_PYTHON（Python 3.12 绝对路径，§7.2）}"
BUILD_PROXY="${INSIGHTWEAVE_BUILD_PROXY_URL:-}"
export http_proxy="${INSIGHTWEAVE_WSL_PROXY_URL:-}" https_proxy="${INSIGHTWEAVE_WSL_PROXY_URL:-}"
export no_proxy="localhost,127.0.0.1,::1,postgres,neo4j,milvus,metaweave,nl2sql,attu,host.docker.internal"
export NO_PROXY="$no_proxy"
DOCKER_BIN="${DOCKER_BIN:-docker --context default}"
MANIFEST_OUT="${MANIFEST_OUT:-$REPO_ROOT/deploy/candidate-$RELEASE_ID.json}"

log() { printf '[ci] %s %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { printf '[ci] 错误：%s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 1) 干净源码（§7.2）
cd "$REPO_ROOT"
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  die "工作区不干净（tracked 修改/未跟踪文件）：按 §7.2 从固定提交的干净 checkout 构建，无跳过开关"
fi
GIT_SHA=$(git rev-parse HEAD)
log "干净源码：$GIT_SHA"

"$PYTHON_BIN" -c 'import sys; assert sys.version_info[:2] == (3, 12), "需要 Python 3.12"' \
  || die "INSIGHTWEAVE_CI_PYTHON 不是 Python 3.12"
node --version | grep -q '^v22\.' || die "需要 Node 22.x（≥22.18，§7.2）"

# ---------------------------------------------------------------- 1.5) 依赖环境（§7.2/13.1 §5.4：独立 checkout 各自锁文件独立 venv）
NL="$REPO_ROOT/nl2sql"; MW="$REPO_ROOT/metaweave"; FE="$REPO_ROOT/frontend"
log "准备依赖环境（uv sync --frozen）"
uv --version >/dev/null 2>&1 || die "需要 uv（Runner 环境准备，13.1 §5.4）"
(cd "$NL" && uv sync --frozen --python "$INSIGHTWEAVE_CI_PYTHON" --no-python-downloads --extra dev) \
  || die "NL2SQL 依赖安装失败"
(cd "$MW" && uv sync --frozen --python "$INSIGHTWEAVE_CI_PYTHON" --no-python-downloads --extra platform --extra dev) \
  || die "MetaWeave 依赖安装失败"

# ---------------------------------------------------------------- 2) 离线测试（§7.2）
# 离线哑配置：干净 checkout 无 .env（被忽略，正确）；部分单测要求配置
# 形状完整（不连库），与开发机 .env 等价的最小集合在此集中注入。
export DB_NAME=orders DB_USER=orders DB_PASSWORD=ci-dummy
export LANGGRAPH_DB_NAME=lg_persist LANGGRAPH_DB_USER=postgres LANGGRAPH_DB_PASSWORD=ci-dummy
export PLATFORM_DB_NAME=metaweave PLATFORM_DB_USER=postgres PLATFORM_DB_PASSWORD=ci-dummy
export NEO4J_PASSWORD=ci-dummy NEO4J_USER=neo4j NEO4J_URI=bolt://127.0.0.1:1 NEO4J_DATABASE=neo4j
export MILVUS_HOST=127.0.0.1 MILVUS_PORT=19530 MILVUS_DATABASE=nl2sql
export DASHSCOPE_API_KEY=ci-dummy DEEPSEEK_API_KEY=ci-dummy MINIMAX_API_KEY=ci-dummy
export OPENAI_API_KEY=ci-dummy OPENROUTER_API_KEY=ci-dummy LANGSMITH_API_KEY=ci-dummy
export REDIS_PASSWORD=ci-dummy
export SOURCE_CREDENTIAL_KEY=ci-dummy-credential-key
# MetaWeave 形状键（实库 live 用例已由 METAWEAVE_RUN_LIVE_TESTS 显式开关门控跳过）
export PLATFORM_DB_HOST=127.0.0.1 PLATFORM_DB_PORT=5432
export PYTHONPATH="$REPO_ROOT:$NL"
(cd "$NL" && .venv/bin/python -m pytest src/tests/unit/api src/tests/unit/services \
   tests/unit/db_deepagent/test_independent_summarizer.py -q) || die "NL2SQL 离线套件失败"
(cd "$NL" && .venv/bin/python -m pytest "$REPO_ROOT/deploy/tests" -q) || die "deploy/tests 失败"
(cd "$MW" && .venv/bin/python -m pytest tests/unit -q    --ignore=tests/unit/metaweave/relationships/test_repository.py)   || die "MetaWeave tests/unit 失败"
# 排除原因（§7.2 记入 README）：test_repository.py 12 例硬依赖本机历史产物
# output/json/*.json（开发环境产物，干净 checkout 不存在；实测空目录亦失败）。
(cd "$FE" && mapfile_test_count=$(find src -type f \( -name '*.test.cjs' -o -name '*.test.ts' \) | wc -l) \
   && [ "$mapfile_test_count" -gt 0 ] || die "未发现前端测试文件") \
  && (cd "$FE" && node --version && mapfile -d '' files < <(find src -type f \( -name '*.test.cjs' -o -name '*.test.ts' \) -print0 | sort -z) \
   && node --test "${files[@]}" && npm run typecheck) || die "前端测试/类型检查失败"

# ---------------------------------------------------------------- 3) 构建三镜像（§7.2）
build_args=()
[ -n "$BUILD_PROXY" ] && build_args=(--build-arg HTTP_PROXY="$BUILD_PROXY" --build-arg HTTPS_PROXY="$BUILD_PROXY" --build-arg NO_PROXY="$no_proxy")
for spec in "metaweave metaweave/Dockerfile" "nl2sql nl2sql/Dockerfile" "frontend frontend/Dockerfile"; do
  set -- $spec
  log "构建 insightweave/$1:$RELEASE_ID"
  $DOCKER_BIN buildx build --builder default --platform linux/amd64 --load \
    "${build_args[@]}" -f "$2" -t "insightweave/$1:$RELEASE_ID" "$REPO_ROOT" \
    || die "镜像构建失败：$1"
done

# ---------------------------------------------------------------- 4) 候选预检（§7.2）
precheck_backend() {  # 无端口/无数据卷/无外网；真实部署开关
  local app="$1" probe="ci-probe-$app-$RELEASE_ID"
  $DOCKER_BIN run --rm --network none -d --name "$probe" \
    -e DEPLOY_CONTROL_ENABLED=1 -e DEPLOY_CONTROL_TOKEN=probe-token \
    -e DEPLOY_CONTROL_DIR=/run/probe-control \
    -e METAWEAVE_DELETION_STARTUP_SCAN=0 \
    -e PLATFORM_DB_HOST=127.0.0.1 -e PLATFORM_DB_PORT=1 -e PLATFORM_DB_NAME=x \
    -e PLATFORM_DB_USER=x -e PLATFORM_DB_PASSWORD=x \
    -e PLATFORM_CORS_ORIGINS=http://localhost:18080 \
    -e SOURCE_CREDENTIAL_KEY=dummy -e DASHSCOPE_API_KEY=dummy \
    -e NEO4J_URI=bolt://127.0.0.1:1 -e NEO4J_USER=x -e NEO4J_PASSWORD=x -e NEO4J_DATABASE=neo4j \
    -e MILVUS_HOST=127.0.0.1 -e MILVUS_PORT=1 -e MILVUS_DATABASE=x \
    "insightweave/$app:$RELEASE_ID" >/dev/null
  $DOCKER_BIN exec "$probe" curl -s --noproxy '*' --fail --max-time 5 \
    -H 'x-insightweave-deploy-token: probe-token' \
    "http://127.0.0.1:$([ "$app" = metaweave ] && echo 8001/api/_deploy/state || echo 8008/api/v2/_deploy/state)" \
    | grep -q '"deployment_contract_version": *1' \
    && $DOCKER_BIN exec "$probe" python -c "import deploy.runtime.lifecycle, deploy.scripts.init_platform, deploy.scripts.smoke" \
    || { $DOCKER_BIN rm -f "$probe" >/dev/null 2>&1; die "候选预检失败：$app（控制状态/契约/脚本导入）"; }
  $DOCKER_BIN rm -f "$probe" >/dev/null
}
precheck_backend metaweave
precheck_backend nl2sql
FE_PROBE="ci-probe-frontend-$RELEASE_ID"
$DOCKER_BIN run --rm --network none --add-host metaweave:127.0.0.1 --add-host nl2sql:127.0.0.1 \
  -d --name "$FE_PROBE" "insightweave/frontend:$RELEASE_ID" >/dev/null
$DOCKER_BIN exec "$FE_PROBE" nginx -t >/dev/null 2>&1 \
  && $DOCKER_BIN exec "$FE_PROBE" curl -s --noproxy '*' --fail http://127.0.0.1:80/ >/dev/null \
  && $DOCKER_BIN exec "$FE_PROBE" curl -s --noproxy '*' -o /dev/null -w '%{http_code}' http://127.0.0.1:80/query | grep -q 200 \
  || { $DOCKER_BIN rm -f "$FE_PROBE" >/dev/null 2>&1; die "前端候选预检失败（nginx -t/静态页/SPA 回退）"; }
$DOCKER_BIN rm -f "$FE_PROBE" >/dev/null

# ---------------------------------------------------------------- 5) 候选清单
for app in metaweave nl2sql frontend; do
  echo "insightweave/$app:$RELEASE_ID $($DOCKER_BIN image inspect --format '{{.Id}}' insightweave/$app:$RELEASE_ID)"
done > "$MANIFEST_OUT"
echo "source_sha=$GIT_SHA" >> "$MANIFEST_OUT"
log "候选清单：$MANIFEST_OUT"
log "CI 完成：$RELEASE_ID"
