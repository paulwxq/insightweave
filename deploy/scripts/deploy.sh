#!/usr/bin/env bash
# deploy.sh —— 本地发布编排（doc 13 §6/§7/§8.1；任务 4）
# 用法：
#   deploy.sh bootstrap <release_id>   首次安装/重试（§6）
#   deploy.sh release   <release_id>   日常发布（§7.1/§7.4）
#   deploy.sh rollback  <release_id>   仅 current/previous（§7.3.1）
# 仅在本机执行；与 GitHub Actions 调同一脚本。测试经 DOCKER_BIN/CURL_BIN/
# PYTHON_BIN/COMPOSE_FILE/DEPLOY_ROOT 注入替身。
set -euo pipefail

DEPLOY_ROOT="${DEPLOY_ROOT:?需要 DEPLOY_ROOT（部署目录，§7.3）}"
COMPOSE_FILE="${COMPOSE_FILE:-$(cd "$(dirname "$BASH_SOURCE")/.." && pwd)/compose.yaml}"
ENV_FILE="${ENV_FILE:?需要 ENV_FILE（deploy.env 实值）}"
PROJECT="${PROJECT:-insightweave-local}"
DRAIN_TIMEOUT="${DEPLOY_DRAIN_TIMEOUT:-900}"
DOCKER_BIN="${DOCKER_BIN:-docker --context default}"
CURL_BIN="${CURL_BIN:-curl}"
PYTHON_BIN="${PYTHON_BIN:-${INSIGHTWEAVE_CI_PYTHON:-python3}}"
APPS="${APPS:-frontend metaweave nl2sql}"
BACKENDS="metaweave nl2sql"

STATE_DIR="$DEPLOY_ROOT/state"; STATE_FILE="$STATE_DIR/deployment.json"
RELEASES_DIR="$DEPLOY_ROOT/releases"; CONTROL_DIR="$DEPLOY_ROOT/control"
LOCK_FILE="$STATE_DIR/.lock"
LOG_DIR="$DEPLOY_ROOT/logs"

log() { printf '[deploy] %s %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { printf '[deploy] 错误：%s\n' "$*" >&2; exit 1; }
compose() { $DOCKER_BIN compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" --project-name "$PROJECT" "$@"; }

mkdir -p "$STATE_DIR" "$RELEASES_DIR" "$CONTROL_DIR" "$LOG_DIR"

# ---------------------------------------------------------------- 状态（原子）

state_write() {  # state_write <phase> <current> <previous> <op>
  $PYTHON_BIN - "$STATE_FILE" "$1" "$2" "$3" "$4" <<'PY'
import json, os, sys, tempfile
path, phase, current, previous, op = sys.argv[1:6]
data = {"phase": phase, "current": current or None, "previous": previous or None,
        "last_operation": op, "updated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds")}
d = os.path.dirname(path)
fd, tmp = tempfile.mkstemp(dir=d)
with os.fdopen(fd, "w") as f:
    json.dump(data, f, ensure_ascii=False, indent=1); f.flush(); os.fsync(f.fileno())
os.replace(tmp, path)
PY
}
state_get() {  # state_get <field>
  $PYTHON_BIN - "$STATE_FILE" "$1" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1]))[sys.argv[2]] or "")
except Exception:
    print("")
PY
}

lock_run() { exec 9>"$LOCK_FILE"; flock -n 9 || die "另一发布进程持有锁（§8.1）"; }

# ---------------------------------------------------------------- 预检

require_images() {  # 三个应用镜像必须本地存在且同名 tag；OS 环境变量覆盖 deploy.env 插值
  local rid="$1"
  for app in $APPS; do
    $DOCKER_BIN image inspect "insightweave/$app:$rid" >/dev/null 2>&1 \
      || die "缺少本地镜像 insightweave/$app:$rid（先由 ci.sh 构建；不在线拉取）"
  done
  # compose 插值优先级：OS env > --env-file；确保替换的就是目标 release
  export METAWEAVE_IMAGE="insightweave/metaweave:$rid"
  export NL2SQL_IMAGE="insightweave/nl2sql:$rid"
  export FRONTEND_IMAGE="insightweave/frontend:$rid"
}

precheck_ports() {
  local p
  for p in 18080 3000 7474 7687 6432; do
    if $CURL_BIN -s --noproxy '*' -o /dev/null --max-time 1 "http://127.0.0.1:$p/" 2>/dev/null; then
      # 端口有响应：若是本项目的容器则继续，否则按占用报告
      if ! compose ps --status running -q >/dev/null 2>&1 \
         || [ -z "$(compose ps -q 2>/dev/null)" ]; then
        die "宿主端口 $p 被非本项目进程占用：请按约定停旧服务后重试（doc 13 §4）"
      fi
    fi
  done
}

snapshot_release() {  # 记录发布清单（§7.3：镜像身份+上游引用，非敏感）
  local rid="$1" dir="$RELEASES_DIR/$rid"
  mkdir -p "$dir"
  $PYTHON_BIN - "$ENV_FILE" "$dir/manifest.env" <<'PY'
import sys
src, dst = sys.argv[1:3]
keys = ("POSTGRES_IMAGE", "NEO4J_IMAGE", "MILVUS_IMAGE", "ATTU_IMAGE",
        "METAWEAVE_IMAGE", "NL2SQL_IMAGE", "FRONTEND_IMAGE")
with open(src) as f, open(dst, "w") as out:
    for line in f:
        k = line.split("=", 1)[0]
        if k in keys:
            out.write(line)
PY
  cp "$COMPOSE_FILE" "$dir/compose.yaml"
}

upstream_match() {  # rollback 前四项上游引用一致性（§7.3）
  local dir="$RELEASES_DIR/$1"
  [ -f "$dir/manifest.env" ] || die "目标版本无发布清单"
  for key in POSTGRES_IMAGE NEO4J_IMAGE MILVUS_IMAGE ATTU_IMAGE; do
    local want have
    want=$(grep "^$key=" "$dir/manifest.env" | cut -d= -f2-)
    have=$(grep "^$key=" "$ENV_FILE" | cut -d= -f2-)
    [ "$want" = "$have" ] || die "rollback 拒绝：上游镜像 $key 与当前配置不一致（$want ≠ $have）"
  done
}

# ---------------------------------------------------------------- 控制与排空（§7.5）

control_call() {  # control_call <service> <enter|state|leave>
  local svc="$1" op="$2" port path_prefix method=GET
  case "$svc" in metaweave) port=8001; path_prefix=/api/_deploy ;; nl2sql) port=8008; path_prefix=/api/v2/_deploy ;; *) die "未知服务 $svc" ;; esac
  [ "$op" = "enter" ] || [ "$op" = "leave" ] && method=POST
  compose exec -T "$svc" sh -eu -c "
    : \"\${DEPLOY_CONTROL_TOKEN:?容器缺少 DEPLOY_CONTROL_TOKEN}\"
    if [ '$method' = POST ]; then
      printf 'X-InsightWeave-Deploy-Token: %s\n' \"\$DEPLOY_CONTROL_TOKEN\" |
        $CURL_BIN --noproxy '*' --fail --silent --show-error --connect-timeout 3 --max-time 10 --header @- -X POST http://127.0.0.1:$port$path_prefix/$op
    else
      printf 'X-InsightWeave-Deploy-Token: %s\n' \"\$DEPLOY_CONTROL_TOKEN\" |
        $CURL_BIN --noproxy '*' --fail --silent --show-error --connect-timeout 3 --max-time 10 --header @- http://127.0.0.1:$port$path_prefix/$op
    fi"
}

drain() {  # 写 marker → enter → 等 active==0；超时取消发布（不动容器）
  local waited=0 line
  mkdir -p "$CONTROL_DIR"; : > "$CONTROL_DIR/maintenance"
  for svc in $BACKENDS; do control_call "$svc" enter >/dev/null; done
  while :; do
    local ok=1
    for svc in $BACKENDS; do
      line=$(control_call "$svc" state 2>/dev/null || true)
      echo "$line" | grep -q '"active": *0' || ok=0
    done
    [ "$ok" = 1 ] && { log "排空完成（${waited}s）"; return 0; }
    waited=$((waited+2)); [ "$waited" -ge "$DRAIN_TIMEOUT" ] && break
    sleep 2
  done
  rm -f "$CONTROL_DIR/maintenance"
  for svc in $BACKENDS; do control_call "$svc" leave >/dev/null 2>&1 || true; done
  die "排空超时（${DRAIN_TIMEOUT}s）：已 leave 并移除标记，未替换任何容器（§7.4）"
}
undrain() {  # 成功路径收尾：移除 marker → 两个后端 leave（§7.4 成功边界）
  rm -f "$CONTROL_DIR/maintenance"
  for svc in $BACKENDS; do control_call "$svc" leave >/dev/null; done
}

# ---------------------------------------------------------------- 应用替换/恢复

frontend_image_matches() {  # frontend 容器在运行且镜像即目标
  local want="$1" running image
  running=$(compose ps --status running -q frontend 2>/dev/null | head -1)
  [ -n "$running" ] || return 1
  image=$($DOCKER_BIN inspect --format '{{.Config.Image}}' "$running" 2>/dev/null || true)
  [ "$image" = "insightweave/frontend:$want" ]
}

frontend_reload() {  # 保留容器时刷新代理解析（§4.1）
  compose exec -T frontend nginx -t >/dev/null 2>&1 || return 1
  compose exec -T frontend nginx -s reload >/dev/null 2>&1
}

bring_up_apps() {  # 后端先替换并等健康，再 frontend（保留匹配则 reload）
  local rid="$1"
  compose up -d --no-deps --force-recreate metaweave nl2sql >/dev/null
  wait_healthy metaweave nl2sql
  if frontend_image_matches "$rid"; then
    frontend_reload || die "frontend 保留但 nginx reload 失败：按恢复规则处理（§9.2）"
    log "frontend 容器保留（镜像匹配），已 nginx reload"
  else
    compose up -d --no-deps frontend >/dev/null
  fi
  wait_healthy frontend
}

wait_healthy() {
  local svc i
  for svc in "$@"; do
    for i in $(seq 1 60); do
      [ "$($DOCKER_BIN inspect --format '{{.State.Health.Status}}' "$(compose ps -q "$svc" | head -1)" 2>/dev/null || true)" = "healthy" ] && break
      sleep 3
    done
    [ "$($DOCKER_BIN inspect --format '{{.State.Health.Status}}' "$(compose ps -q "$svc" | head -1)" 2>/dev/null || true)" = "healthy" ] \
      || die "$svc 未达 healthy"
  done
}

# ---------------------------------------------------------------- 冒烟（两阶段，§7.4）

apps_smoke() {  # apps_smoke <public|maintenance>
  local mode="$1" token
  token=$(grep -h '^DEPLOY_CONTROL_TOKEN=' "$DEPLOY_ROOT"/config/*.env 2>/dev/null | head -1 | cut -d= -f2-)
  [ -n "$token" ] || token="${DEPLOY_CONTROL_TOKEN:-}"
  if [ "$mode" = maintenance ]; then
    [ -n "$token" ] || die "maintenance 冒烟缺少令牌（config/*.env 或 DEPLOY_CONTROL_TOKEN）"
    DEPLOY_CONTROL_TOKEN="$token" $PYTHON_BIN "$(dirname "$BASH_SOURCE")/smoke.py" --scope apps --mode maintenance
  else
    env -u DEPLOY_CONTROL_TOKEN $PYTHON_BIN "$(dirname "$BASH_SOURCE")/smoke.py" --scope apps --mode public
  fi
}

# ---------------------------------------------------------------- bootstrap（§6）

prepare_neo4j_plugin() {  # compose 命名卷（项目名_neo4j-plugins，任务 3 实证）
  local vol="${PROJECT}_neo4j-plugins" img
  img=$(grep '^NEO4J_IMAGE=' "$ENV_FILE" | cut -d= -f2-)
  # 显式确保卷存在（compose 声明为 external:true，不存在时 compose 会明确报错）
  $DOCKER_BIN volume create "$vol" >/dev/null 2>&1 || true
  $DOCKER_BIN run --rm --network none --entrypoint sh -v "$vol":/plugins "$img" \
    -c 'set -eu; jar=/plugins/apoc.jar;
        if [ -f "$jar" ]; then echo "[deploy] APOC 已存在，核验"; exit 0; fi
        src=$(ls /var/lib/neo4j/labs/apoc-*-core.jar); cp "$src" "$jar"; chmod 0644 "$jar"' \
    || die "APOC 插件准备失败（13.1 §2.2）"
}

cmd_bootstrap() {
  local rid="$1" phase current
  phase=$(state_get phase); current=$(state_get current)
  if [ -n "$current" ]; then
    [ "$phase" = ready ] || die "非 ready 且已有 current：请用 rollback 恢复（§7.3.1）"
    if [ "$current" = "$rid" ]; then
      log "bootstrap 同版本：仅核验（§7.3.1）"
      compose up -d >/dev/null 2>&1 || true
      apps_smoke public
      log "核验通过，引用不变"; return 0
    fi
    die "已有 current=$current：新版本请用 release（§7.3.1）"
  fi
  require_images "$rid"; precheck_ports
  state_write deploying "" "" "bootstrap $rid"        # §7.3.1：任何变更前记录
  # 已有应用在跑（中断重试）→ 先排空
  if [ -n "$(compose ps --status running -q 2>/dev/null | head -1)" ]; then
    log "检测到运行中容器：先排空（§6 重试规则）"
    drain
  fi
  prepare_neo4j_plugin
  compose up -d postgres neo4j milvus >/dev/null
  wait_healthy postgres neo4j milvus
  compose exec -T postgres bash /docker-entrypoint-initdb.d/01-create-databases.sh >/dev/null  # 幂等重入（§6.1）
  compose up -d --no-deps metaweave nl2sql >/dev/null; wait_healthy metaweave nl2sql
  compose exec -T metaweave python /app/deploy/scripts/init_platform.py metaweave --mode initialize
  compose exec -T nl2sql   python /app/deploy/scripts/init_platform.py nl2sql   --mode initialize
  compose exec -T metaweave python /app/deploy/scripts/init_platform.py milvus   --mode initialize
  compose up -d attu >/dev/null; wait_healthy attu
  compose up -d --no-deps frontend >/dev/null; wait_healthy frontend
  apps_smoke maintenance
  undrain
  apps_smoke public
  snapshot_release "$rid"
  state_write ready "$rid" "" "bootstrap $rid"
  log "bootstrap 完成：current=$rid"
}

# ---------------------------------------------------------------- release（§7.1/§7.4）

cmd_release() {
  local rid="$1" phase current previous
  phase=$(state_get phase); current=$(state_get current); previous=$(state_get previous)
  [ "$phase" = ready ] || die "release 只接受 ready 现场（当前：${phase:-无状态}）"
  [ ! -e "$CONTROL_DIR/maintenance" ] || die "存在维护标记：先恢复（rollback）"
  require_images "$rid"; precheck_ports
  if [ "$current" = "$rid" ]; then
    log "release 同版本：仅核验，引用与 previous 不变（§7.3.1）"
    apps_smoke public; return 0
  fi
  state_write deploying "$current" "$previous" "release $rid"
  snapshot_release "$rid"
  local ok=1
  drain
  bring_up_apps "$rid" || ok=0
  if [ "$ok" = 1 ]; then
    apps_smoke maintenance || ok=0
    if [ "$ok" = 1 ]; then undrain; apps_smoke public || ok=0; fi
  fi
  if [ "$ok" = 0 ]; then
    log "发布失败：恢复 current=$current（§7.4）"
    rm -f "$CONTROL_DIR/maintenance"
    if [ -n "$current" ]; then
      drain
      bring_up_apps "$current"
      undrain; apps_smoke public
      state_write ready "$current" "$previous" "release $rid 失败已回退"
      die "原发布失败（已恢复 $current）"
    fi
    state_write recovery_required "" "" "release $rid 失败且无旧版本"
    die "首次安装失败：无旧版本可退（§7.4）"
  fi
  state_write ready "$rid" "$current" "release $rid"
  log "发布完成：current=$rid previous=$current"
}

# ---------------------------------------------------------------- rollback（§7.3.1）

cmd_rollback() {
  local rid="$1" phase current previous
  phase=$(state_get phase); current=$(state_get current); previous=$(state_get previous)
  [ "$rid" = "$current" ] || { [ -n "$previous" ] && [ "$rid" = "$previous" ]; } \
    || die "rollback 目标必须是 current($current) 或 previous($previous)（§7.3.1）"
  upstream_match "$rid" || die
  require_images "$rid"
  if [ "$phase" = ready ] && [ "$rid" = "$current" ]; then
    log "rollback 同版本：仅核验现场（§7.3.1）"
    apps_smoke public; log "核验通过，引用不变"; return 0
  fi
  state_write deploying "$current" "$previous" "rollback $rid"
  drain
  bring_up_apps "$rid"
  apps_smoke maintenance
  undrain; apps_smoke public
  if [ "$rid" = "$current" ]; then
    state_write ready "$current" "$previous" "rollback $current（实际恢复，保留 previous）"
  else
    state_write ready "$rid" "$current" "rollback $rid"
  fi
  log "rollback 完成：current=$(state_get current) previous=$(state_get previous)"
}

main() {
  lock_run
  local cmd="${1:-}"; shift || true
  case "$cmd" in
    bootstrap) cmd_bootstrap "${1:?用法：deploy.sh bootstrap <release_id>}" ;;
    release)   cmd_release   "${1:?用法：deploy.sh release <release_id>}" ;;
    rollback)  cmd_rollback  "${1:?用法：deploy.sh rollback <release_id>}" ;;
    *) die "用法：deploy.sh bootstrap|release|rollback <release_id>" ;;
  esac
}
main "$@"
