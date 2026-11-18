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
BACKENDS="metaweave nl2sql"
MANIFEST_TOOL="$(cd "$(dirname "$BASH_SOURCE")" && pwd)/release_manifest.py"

STATE_DIR="$DEPLOY_ROOT/state"; STATE_FILE="$STATE_DIR/deployment.json"
RELEASES_DIR="$DEPLOY_ROOT/releases"; CONTROL_DIR="$DEPLOY_ROOT/control"
LOCK_FILE="$STATE_DIR/.lock"
LOG_DIR="$DEPLOY_ROOT/logs"
OPERATION_STARTED=0

log() { printf '[deploy] %s %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { printf '[deploy] 错误：%s\n' "$*" >&2; exit 1; }
compose() { $DOCKER_BIN compose --project-directory "$(dirname "$COMPOSE_FILE")" -f "$COMPOSE_FILE" --env-file "$ENV_FILE" --project-name "$PROJECT" "$@"; }

mkdir -p "$STATE_DIR" "$RELEASES_DIR" "$CONTROL_DIR" "$LOG_DIR"

# ---------------------------------------------------------------- 状态（原子）

state_write() {  # state_write <phase> <current> <previous> <op>
  $PYTHON_BIN - "$STATE_FILE" "$1" "$2" "$3" "$4" <<'PY' || return 1
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
  if [ "$1" = deploying ]; then OPERATION_STARTED=1; fi
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

on_exit() {  # 只收尾本次已经开始的变更，不修改预检拒绝时的成功状态
  local status=$?
  trap - EXIT
  if [ "$status" -ne 0 ] && [ "$OPERATION_STARTED" = 1 ] && [ "$(state_get phase)" = deploying ]; then
    # 失败可能发生在 leave/普通冒烟之后，保留重启时可见的标记并尽力关闭存活入口。
    mkdir -p "$CONTROL_DIR" && : > "$CONTROL_DIR/maintenance" || log "无法重建维护标记"
    local svc
    for svc in $BACKENDS; do
      if backend_running "$svc"; then
        control_call "$svc" enter >/dev/null 2>&1 || log "$svc 入口关闭未确认，请人工核验"
      fi
    done
    state_write recovery_required "$(state_get current)" "$(state_get previous)" \
      "$(state_get last_operation)：失败，需核验恢复" || log "无法记录恢复状态，请人工核对 $STATE_FILE"
  fi
  exit "$status"
}

# ---------------------------------------------------------------- 预检

check_release_snapshot() {
  $PYTHON_BIN "$MANIFEST_TOOL" load --snapshot "$RELEASES_DIR/$1" --env-file "$ENV_FILE" >/dev/null
}

load_release_snapshot() {  # 三应用身份和 Compose 同时切换，保留当前密钥/项目名
  local selected
  selected=$($PYTHON_BIN "$MANIFEST_TOOL" load --snapshot "$RELEASES_DIR/$1" --env-file "$ENV_FILE") || return 1
  local -a settings
  mapfile -t settings <<< "$selected"
  [ "${#settings[@]}" = 4 ] || return 1
  export METAWEAVE_IMAGE="${settings[0]}" NL2SQL_IMAGE="${settings[1]}" FRONTEND_IMAGE="${settings[2]}"
  COMPOSE_FILE="${settings[3]}"
}

verify_running_images() {
  $PYTHON_BIN "$MANIFEST_TOOL" verify-running --snapshot "$RELEASES_DIR/$1" \
    --env-file "$ENV_FILE" --project "$PROJECT"
}

preflight_existing_services() {
  local rid="$1"; shift
  $PYTHON_BIN "$MANIFEST_TOOL" preflight --snapshot "$RELEASES_DIR/$rid" \
    --env-file "$ENV_FILE" --project "$PROJECT" "$@"
}

verify_current_release() {  # 同版本只读核验：镜像、开放状态和普通请求都必须成立
  [ ! -e "$CONTROL_DIR/maintenance" ] || { log "存在维护标记"; return 1; }
  precheck_ports || return 1
  preflight_existing_services "$1" --require-open-backends || return 1
  verify_running_images "$1" || return 1
  apps_smoke public
}

check_same_version() {  # bootstrap/release 同版本不替换；失败明确提示恢复入口
  local op="$1" rid="$2" previous="$3"
  if load_release_snapshot "$rid" && verify_current_release "$rid"; then
    if [ "$op" != bootstrap ] || prepare_neo4j_plugin "$rid" check; then
      log "$op 同版本核验通过，版本引用不变"
      return 0
    fi
  fi
  state_write recovery_required "$rid" "$previous" "$op $rid 核验失败" || return 1
  log "现场核验失败，请执行 rollback $rid 恢复"
  return 1
}

precheck_ports() {
  $PYTHON_BIN "$MANIFEST_TOOL" ports --project "$PROJECT"
}

snapshot_release() {  # CI 身份 + 非敏感配置；全部核验后原子发布目录
  local rid="$1" source_sha candidate
  source_sha=$(git -C "$(dirname "$MANIFEST_TOOL")/../.." rev-parse HEAD) || return 1
  candidate="${CANDIDATE_MANIFEST:-$(dirname "$COMPOSE_FILE")/candidate-$rid.json}"
  $PYTHON_BIN "$MANIFEST_TOOL" snapshot --release-id "$rid" --source-sha "$source_sha" \
    --candidate "$candidate" --compose-file "$COMPOSE_FILE" --env-file "$ENV_FILE" \
    --releases-dir "$RELEASES_DIR"
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

backend_running() {  # 0=运行，1=Docker 确认不存在/退出，2=未知
  local cid status
  cid=$(compose ps --all -q "$1") || return 2
  [ -n "$cid" ] || return 1
  status=$($DOCKER_BIN inspect --format '{{.State.Running}}' "$cid") || return 2
  case "$status" in true) return 0 ;; false) return 1 ;; *) return 2 ;; esac
}

drain() {  # 写 marker → enter → 等 active==0；超时取消发布（不动容器）
  local waited=0 line active svc status had_marker=0 current previous
  local -a draining=()
  [ ! -e "$CONTROL_DIR/maintenance" ] || had_marker=1
  mkdir -p "$CONTROL_DIR"; : > "$CONTROL_DIR/maintenance"
  for svc in $BACKENDS; do
    if backend_running "$svc"; then
      control_call "$svc" enter >/dev/null || return 1
      draining+=("$svc")
    else
      status=$?
      [ "$status" = 1 ] || { log "无法确认 $svc 运行状态，取消替换"; return 1; }
    fi
  done
  while :; do
    local ok=1
    for svc in "${draining[@]}"; do
      line=$(control_call "$svc" state) || { log "$svc 控制状态读取失败"; return 1; }
      active=$(printf '%s' "$line" | $PYTHON_BIN "$MANIFEST_TOOL" active) || return 1
      [ "$active" = 0 ] || ok=0
    done
    [ "$ok" = 1 ] && { log "排空完成（${waited}s）"; return 0; }
    waited=$((waited+2)); [ "$waited" -ge "$DRAIN_TIMEOUT" ] && break
    sleep 2
  done
  printf '[deploy] 排空超时（%ss）：未替换任何容器\n' "$DRAIN_TIMEOUT" >&2
  current=$(state_get current); previous=$(state_get previous)
  # 原先已经维护的未知现场继续保持关闭；正常发布取消后须核验旧版本才可恢复 ready。
  if [ "$had_marker" = 0 ] && [ -n "$current" ] && undrain \
     && load_release_snapshot "$current" && verify_current_release "$current"; then
    state_write ready "$current" "$previous" "排空超时，原版本已核验恢复" || return 1
  else
    : > "$CONTROL_DIR/maintenance"
    for svc in "${draining[@]}"; do control_call "$svc" enter >/dev/null 2>&1 || true; done
    state_write recovery_required "$current" "$previous" "排空超时，现场未确认恢复" || return 1
  fi
  return 1
}
undrain() {  # 成功路径收尾：移除 marker → 两个后端 leave（§7.4 成功边界）
  rm -f "$CONTROL_DIR/maintenance" || return 1
  for svc in $BACKENDS; do control_call "$svc" leave >/dev/null || return 1; done
}

# ---------------------------------------------------------------- 应用替换/恢复

frontend_image_matches() {  # frontend 容器在运行且镜像即目标
  local want="$1" running image actual expected
  running=$(compose ps --status running -q frontend 2>/dev/null | head -1)
  [ -n "$running" ] || return 1
  image=$($DOCKER_BIN inspect --format '{{.Config.Image}}' "$running" 2>/dev/null || true)
  [ "$image" = "insightweave/frontend:$want" ] || return 1
  actual=$($DOCKER_BIN inspect --format '{{.Image}}' "$running") || return 1
  expected=$($DOCKER_BIN image inspect --format '{{.Id}}' "insightweave/frontend:$want") || return 1
  [ "$actual" = "$expected" ]
}

frontend_reload() {  # 保留容器时刷新代理解析（§4.1）
  compose exec -T frontend nginx -t >/dev/null 2>&1 || return 1
  compose exec -T frontend nginx -s reload >/dev/null 2>&1
}

bring_up_apps() {  # 后端先替换并等健康，再 frontend（保留匹配则 reload）
  # 显式 return 1：调用侧 || ok=0 会禁用 set -e，不能依赖隐式失败（审核 P1）
  local rid="$1"
  if ! compose up -d --no-deps --force-recreate metaweave nl2sql >/dev/null; then
    log "后端容器替换命令失败"
    return 1
  fi
  wait_healthy metaweave nl2sql || return 1
  if frontend_image_matches "$rid"; then
    if ! frontend_reload; then
      log "frontend 保留但 nginx reload 失败"
      return 1
    fi
    log "frontend 容器保留（镜像匹配），已 nginx reload"
  else
    compose up -d --no-deps --force-recreate frontend >/dev/null || return 1
  fi
  wait_healthy frontend || return 1
  return 0
}

wait_healthy() {
  # return 1 而非 die：由调用侧决定是退出（bootstrap/set -e）还是恢复（release/||）
  local svc i
  for svc in "$@"; do
    for i in $(seq 1 60); do
      [ "$($DOCKER_BIN inspect --format '{{.State.Health.Status}}' "$(compose ps -q "$svc" | head -1)" 2>/dev/null || true)" = "healthy" ] && break
      sleep 3
    done
    [ "$($DOCKER_BIN inspect --format '{{.State.Health.Status}}' "$(compose ps -q "$svc" | head -1)" 2>/dev/null || true)" = "healthy" ] \
      || { log "$svc 未达 healthy"; return 1; }
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

prepare_neo4j_plugin() {  # prepare_neo4j_plugin <已核验的 release_id> [prepare|check]
  local rid="$1" mode="${2:-prepare}" img cid running mounted
  local vol="insightweave-local_neo4j-plugins"  # 与 Compose external name 一致
  local mount="type=volume,source=$vol,target=/plugins,volume-nocopy"
  local -a user_args=()
  # 快照已与当前 Compose/env 对齐，避免再次手工解析带引号或被环境覆盖的 env 值。
  img=$($PYTHON_BIN - "$RELEASES_DIR/$rid/manifest.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["upstream_images"]["NEO4J_IMAGE"])
PY
  ) || return 1
  cid=$(compose ps --all -q neo4j) || return 1
  if [ -n "$cid" ]; then
    running=$($DOCKER_BIN inspect --format '{{.State.Running}}' "$cid") || return 1
    case "$running" in
      true)
        mounted=$($DOCKER_BIN inspect --format '{{range .Mounts}}{{if eq .Destination "/plugins"}}{{.Type}}:{{.Name}}{{end}}{{end}}' "$cid") || return 1
        [ "$mounted" = "volume:$vol" ] || { log "Neo4j /plugins 挂载与预期不符"; return 1; }
        mode=check ;;
      false) : ;;
      *) log "无法确认 Neo4j 运行状态，不准备插件"; return 1 ;;
    esac
  fi
  case "$mode" in
    check)
      $DOCKER_BIN volume inspect "$vol" >/dev/null || return 1
      mount+=",readonly"
      user_args=(--user 7474:7474) ;;
    prepare)
      $DOCKER_BIN volume create "$vol" >/dev/null || return 1 ;;
    *) log "未知 APOC 准备模式：$mode"; return 1 ;;
  esac
  $DOCKER_BIN run --rm --pull never --network none --entrypoint sh \
    "${user_args[@]}" --mount "$mount" "$img" -c '
      set -eu
      mode=$1; jar=/plugins/apoc.jar
      fail() { echo "[deploy] APOC: $*" >&2; exit 1; }
      set -- /var/lib/neo4j/labs/apoc-*-core.jar
      [ "$#" = 1 ] && [ -f "$1" ] && [ -r "$1" ] || fail "固定镜像中的源 JAR 缺失、不可读或不唯一"
      src=$1
      expected=$(sha256sum "$src"); expected=${expected%% *}
      count=0
      for candidate in /plugins/* /plugins/.[!.]* /plugins/..?*; do
        [ -e "$candidate" ] || [ -L "$candidate" ] || continue
        case "${candidate##*/}" in
          *[Aa][Pp][Oo][Cc]*.[Jj][Aa][Rr]) count=$((count+1)); found=$candidate ;;
        esac
      done
      if [ "$count" != 0 ]; then
        [ "$count" = 1 ] && [ "$found" = "$jar" ] && [ -f "$jar" ] && [ ! -L "$jar" ] && [ -r "$jar" ] \
          || fail "插件卷有重复或未知的 APOC JAR，保留原文件"
        actual=$(sha256sum "$jar"); actual=${actual%% *}
        [ "$actual" = "$expected" ] || fail "已有 apoc.jar SHA256 不一致，保留原文件"
        echo "[deploy] APOC 已核验，保留原文件 SHA256=$expected"
        exit 0
      fi
      [ "$mode" = prepare ] || fail "运行中或只读核验的插件卷缺少 apoc.jar"
      tmp=$(mktemp /plugins/.apoc.XXXXXX)
      trap '\''rm -f "$tmp"'\'' EXIT
      trap '\''exit 1'\'' HUP INT TERM
      cp "$src" "$tmp"
      chmod 0644 "$tmp"
      actual=$(sha256sum "$tmp"); actual=${actual%% *}
      [ "$actual" = "$expected" ] || fail "临时 JAR SHA256 不一致"
      [ ! -e "$jar" ] && [ ! -L "$jar" ] || fail "目标文件已出现，拒绝覆盖"
      mv "$tmp" "$jar"
      trap - EXIT
      echo "[deploy] APOC 已离线准备 SHA256=$expected"
    ' apoc-plugin "$mode" || { log "APOC 插件准备/核验失败（13.1 §2.2）"; return 1; }
}

cmd_bootstrap() {
  local rid="$1" phase current previous
  phase=$(state_get phase); current=$(state_get current)
  previous=$(state_get previous)
  if [ -n "$current" ]; then
    [ "$phase" = ready ] || die "非 ready 且已有 current：请用 rollback 恢复（§7.3.1）"
    if [ "$current" = "$rid" ]; then
      check_same_version bootstrap "$rid" "$previous"; return $?
    fi
    die "已有 current=$current：新版本请用 release（§7.3.1）"
  fi
  snapshot_release "$rid" || return 1
  load_release_snapshot "$rid" || return 1
  precheck_ports || return 1
  state_write deploying "" "" "bootstrap $rid"        # §7.3.1：任何变更前记录
  # 首次启动也先写维护标记；重试只排空 Docker 确认仍在运行的后端。
  drain
  prepare_neo4j_plugin "$rid"
  compose up -d postgres neo4j milvus >/dev/null
  wait_healthy postgres neo4j milvus
  compose exec -T postgres bash /docker-entrypoint-initdb.d/01-create-databases.sh >/dev/null  # 幂等重入（§6.1）
  compose up -d --no-deps metaweave nl2sql >/dev/null; wait_healthy metaweave nl2sql
  compose exec -T metaweave python /app/deploy/scripts/init_platform.py metaweave --mode initialize
  compose exec -T nl2sql   python /app/deploy/scripts/init_platform.py nl2sql   --mode initialize
  compose exec -T metaweave python /app/deploy/scripts/init_platform.py milvus   --mode initialize
  compose up -d attu >/dev/null; wait_healthy attu
  # 首次安装此时才有目标库/表和四项上游，初始化前不执行完整依赖检查。
  preflight_existing_services "$rid" || return 1
  compose up -d --no-deps frontend >/dev/null; wait_healthy frontend
  apps_smoke maintenance
  undrain
  apps_smoke public
  verify_running_images "$rid" || return 1
  state_write ready "$rid" "" "bootstrap $rid"
  log "bootstrap 完成：current=$rid"
}

# ---------------------------------------------------------------- release（§7.1/§7.4）

cmd_release() {
  local rid="$1" phase current previous
  phase=$(state_get phase); current=$(state_get current); previous=$(state_get previous)
  [ "$phase" = ready ] || die "release 只接受 ready 现场（当前：${phase:-无状态}）"
  if [ "$current" = "$rid" ]; then
    check_same_version release "$rid" "$previous"; return $?
  fi
  [ ! -e "$CONTROL_DIR/maintenance" ] || die "存在维护标记：先恢复（rollback）"
  # 在变更前确认旧版本具备可用快照，不猜测旧格式里缺失的信息。
  check_release_snapshot "$current" || return 1
  snapshot_release "$rid" || return 1
  load_release_snapshot "$rid" || return 1
  precheck_ports || return 1
  preflight_existing_services "$rid" || return 1
  state_write deploying "$current" "$previous" "release $rid"
  local ok=1
  drain
  bring_up_apps "$rid" || ok=0
  if [ "$ok" = 1 ]; then
    apps_smoke maintenance || ok=0
    if [ "$ok" = 1 ]; then
      if undrain; then apps_smoke public || ok=0; else ok=0; fi
    fi
    if [ "$ok" = 1 ]; then verify_running_images "$rid" || ok=0; fi
  fi
  if [ "$ok" = 0 ]; then
    log "发布失败：恢复 current=$current（§7.4）"
    # 候选可能已经开放过入口；先重新关闭并排空，再做恢复预检。
    drain
    if [ -n "$current" ]; then
      precheck_ports || return 1
      preflight_existing_services "$current" --allow-missing-backends || return 1
      load_release_snapshot "$current" || return 1
      bring_up_apps "$current"
      apps_smoke maintenance
      undrain; apps_smoke public
      verify_running_images "$current" || return 1
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
  if ! load_release_snapshot "$rid"; then
    if [ "$rid" = "$current" ]; then
      state_write recovery_required "$current" "$previous" "rollback $rid 快照核验失败"
    fi
    return 1
  fi
  if [ "$phase" = ready ] && [ "$rid" = "$current" ]; then
    if verify_current_release "$rid"; then
      log "rollback 同版本核验通过，引用不变"; return 0
    fi
    state_write recovery_required "$current" "$previous" "rollback $rid 现场不符，准备恢复"
    log "同版本现场不符，按目标快照实际恢复 $rid"
  fi
  precheck_ports || return 1
  preflight_existing_services "$rid" --allow-missing-backends || return 1
  state_write deploying "$current" "$previous" "rollback $rid"
  drain
  bring_up_apps "$rid"
  apps_smoke maintenance
  undrain; apps_smoke public
  verify_running_images "$rid" || return 1
  if [ "$rid" = "$current" ]; then
    state_write ready "$current" "$previous" "rollback $current（实际恢复，保留 previous）"
  else
    state_write ready "$rid" "$current" "rollback $rid"
  fi
  log "rollback 完成：current=$(state_get current) previous=$(state_get previous)"
}

main() {
  lock_run
  trap on_exit EXIT
  local cmd="${1:-}"; shift || true
  case "$cmd" in
    bootstrap) cmd_bootstrap "${1:?用法：deploy.sh bootstrap <release_id>}" ;;
    release)   cmd_release   "${1:?用法：deploy.sh release <release_id>}" ;;
    rollback)  cmd_rollback  "${1:?用法：deploy.sh rollback <release_id>}" ;;
    *) die "用法：deploy.sh bootstrap|release|rollback <release_id>" ;;
  esac
}
main "$@"
