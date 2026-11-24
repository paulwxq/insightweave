#!/usr/bin/env bash
# ci.sh —— 本地 CI（doc 13 §7.2；任务 4）：干净源码 → 离线检查 → 构建三镜像 → 候选预检
# 用法：ci.sh <release_id>   与 GitHub Actions 调同一脚本；仅本机执行。
# 干净源码检查、版本要求、依赖准备和各套件测试由 check.sh all 执行（doc 15 §7.1，
# 与云端 CI 共用同一入口）；本脚本负责其后的镜像构建与候选预检。
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

# ---------------------------------------------------------------- 1) 干净源码与离线检查（§7.2）
# 干净源码检查、版本要求和五个套件统一由 check.sh 执行；失败即停止，不进入构建。
cd "$REPO_ROOT"
bash deploy/scripts/check.sh all
GIT_SHA=$(git rev-parse HEAD)
log "源码提交：$GIT_SHA"

# ---------------------------------------------------------------- 2) 构建三镜像（§7.2）
build_args=()
[ -n "$BUILD_PROXY" ] && build_args=(--build-arg HTTP_PROXY="$BUILD_PROXY" --build-arg HTTPS_PROXY="$BUILD_PROXY" --build-arg NO_PROXY="$no_proxy")
for spec in "metaweave metaweave/Dockerfile" "nl2sql nl2sql/Dockerfile" "frontend frontend/Dockerfile"; do
  set -- $spec
  app_build_args=("${build_args[@]}")
  [ "$1" = frontend ] && app_build_args+=(--build-arg "APP_GIT_SHA=$GIT_SHA")
  log "构建 insightweave/$1:$RELEASE_ID"
  $DOCKER_BIN buildx build --builder default --platform linux/amd64 --load \
    "${app_build_args[@]}" -f "$2" -t "insightweave/$1:$RELEASE_ID" "$REPO_ROOT" \
    || die "镜像构建失败：$1"
done

# ---------------------------------------------------------------- 3) 候选预检（§7.2）
precheck_backend() {  # 无端口/无数据卷/无外网；真实部署开关
  local app="$1"
  local probe="ci-probe-$app-$RELEASE_ID"
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
  local state_url="http://127.0.0.1:$([ "$app" = metaweave ] && echo 8001/api/_deploy/state || echo 8008/api/v2/_deploy/state)"
  local probe_ok=0 waited=0
  while [ "$waited" -lt 60 ]; do
    if $DOCKER_BIN exec "$probe" curl -s --noproxy '*' --fail --max-time 5 \
        -H 'x-insightweave-deploy-token: probe-token' "$state_url" \
        | grep -q '"deployment_contract_version": *1'; then
      probe_ok=1; break
    fi
    waited=$((waited+3)); sleep 3
  done
  [ "$probe_ok" = 1 ] \
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

# ---------------------------------------------------------------- 4) 候选清单
export DOCKER_BIN
"$PYTHON_BIN" "$REPO_ROOT/deploy/scripts/release_manifest.py" candidate \
  --release-id "$RELEASE_ID" --source-sha "$GIT_SHA" \
  --contract "$REPO_ROOT/deploy/contract.json" --output "$MANIFEST_OUT"
log "候选清单：$MANIFEST_OUT"
log "CI 完成：$RELEASE_ID"
