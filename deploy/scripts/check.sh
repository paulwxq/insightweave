#!/usr/bin/env bash
# check.sh —— 受维护的离线检查（doc 13 §7.2；doc 15 §7.1）：干净源码、依赖准备、离线哑配置、
# 各套件测试与前端类型检查。与 ci.sh 共用同一份套件定义；云端 CI 的每个 job 只选一个套件。
# 用法：check.sh [all|nl2sql|agent_core|metaweave|deploy_tests|frontend]   缺省 all
# 经 DOCKER_BIN/CURL_BIN 等替身测试的行为见 deploy/tests（TestCheckSh、TestCiSh）。
set -euo pipefail

SELECTOR="${1-all}"  # 只有不传参数才缺省为 all；显式传入空串按未知 selector 拒绝
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$BASH_SOURCE")/../.." && pwd)}"

log() { printf '[check] %s %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { printf '[check] 错误：%s\n' "$*" >&2; exit 1; }

# 用法固定为单个 selector（doc 15 §7.1）；多余参数在检查任何东西之前拒绝。
[ "$#" -le 1 ] || die "未知 selector：'$*'（只接受一个：all|nl2sql|agent_core|metaweave|deploy_tests|frontend）"

# ---------------------------------------------------------------- 0) 干净源码（§7.2）
cd "$REPO_ROOT"
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  die "工作区不干净（tracked 修改/未跟踪文件）：按 §7.2 从固定提交的干净 checkout 检查，无跳过开关"
fi
log "干净源码：$(git rev-parse HEAD)"

NL="$REPO_ROOT/nl2sql"; MW="$REPO_ROOT/metaweave"; FE="$REPO_ROOT/frontend"

# 代理只服务于依赖安装：本地 Runner 经 WSL 代理，云端不配置时为空；不是业务配置。
export http_proxy="${INSIGHTWEAVE_WSL_PROXY_URL:-}" https_proxy="${INSIGHTWEAVE_WSL_PROXY_URL:-}"
export no_proxy="localhost,127.0.0.1,::1,postgres,neo4j,milvus,metaweave,nl2sql,attu,host.docker.internal"
export NO_PROXY="$no_proxy"

# ---------------------------------------------------------------- 套件共用的准备步骤

require_ci_python() {  # 需要 Python 的套件：解释器路径固定，且必须是 3.12
  PYTHON_BIN="${PYTHON_BIN:-${INSIGHTWEAVE_CI_PYTHON:?需要 INSIGHTWEAVE_CI_PYTHON（Python 3.12 绝对路径，§7.2）}}"
  "$PYTHON_BIN" -c 'import sys; assert sys.version_info[:2] == (3, 12), "需要 Python 3.12"' \
    || die "INSIGHTWEAVE_CI_PYTHON 不是 Python 3.12"
  uv --version >/dev/null 2>&1 || die "需要 uv（Runner 环境准备，13.1 §5.4）"
}

offline_dummy_config() {  # 干净 checkout 无 .env（被忽略，正确）；部分单测要求配置形状
  # 完整（不连库），与开发机 .env 等价的最小集合在此集中注入。
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
}

prepare_nl2sql() {  # NL2SQL / Agent 核心 / deploy 三个套件共用 NL2SQL 锁文件的 .venv
  (cd "$NL" && uv sync --frozen --python "$INSIGHTWEAVE_CI_PYTHON" --no-python-downloads --extra dev) \
    || die "NL2SQL 依赖安装失败"
}

prepare_metaweave() {
  (cd "$MW" && uv sync --frozen --python "$INSIGHTWEAVE_CI_PYTHON" --no-python-downloads --extra platform --extra dev) \
    || die "MetaWeave 依赖安装失败"
}

# ---------------------------------------------------------------- 套件（all 的固定顺序，doc 15 §7.1）

suite_nl2sql() {
  require_ci_python; offline_dummy_config; prepare_nl2sql
  (cd "$NL" && .venv/bin/python -m pytest src/tests/unit/api src/tests/unit/services src/tests/unit/utils \
     tests/unit/db_deepagent/test_independent_summarizer.py -q) || die "NL2SQL 离线套件失败"
}

suite_agent_core() {
  require_ci_python; offline_dummy_config; prepare_nl2sql
  log "运行 NL2SQL Agent 核心离线测试"
  # 不传 --run-db、--run-llm；实库/真实模型用例由收集钩子默认跳过并显示原因。
  (cd "$NL" && .venv/bin/python -m pytest src/tests/db_deepagent -q -ra) \
    || die "NL2SQL Agent 核心离线测试失败"
}

suite_deploy_tests() {
  require_ci_python; offline_dummy_config; prepare_nl2sql
  (cd "$NL" && .venv/bin/python -m pytest "$REPO_ROOT/deploy/tests" -q) || die "deploy/tests 失败"
}

suite_metaweave() {
  require_ci_python; prepare_metaweave
  # 排除原因（§7.2 记入 README）：test_repository.py 12 例硬依赖本机历史产物
  # output/json/*.json（开发环境产物，干净 checkout 不存在；实测空目录亦失败）。
  (cd "$MW" && .venv/bin/python -m pytest tests/unit -q    --ignore=tests/unit/metaweave/relationships/test_repository.py)   || die "MetaWeave tests/unit 失败"
}

suite_frontend() {
  node --version | grep -q '^v22\.' || die "需要 Node 22.x（≥22.18，§7.2）"
  (cd "$FE" && npm ci --include=dev) || die "前端依赖安装失败"
  (cd "$FE" && mapfile_test_count=$(find src -type f \( -name '*.test.cjs' -o -name '*.test.ts' \) | wc -l) \
     && [ "$mapfile_test_count" -gt 0 ] || die "未发现前端测试文件")
  (cd "$FE" && node --version && mapfile -d '' files < <(find src -type f \( -name '*.test.cjs' -o -name '*.test.ts' \) -print0 | sort -z) \
     && node --test "${files[@]}" && npm run typecheck) || die "前端测试/类型检查失败"
}

case "$SELECTOR" in
  all)
    for suite in nl2sql agent_core deploy_tests metaweave frontend; do "suite_$suite"; done ;;
  nl2sql|agent_core|deploy_tests|metaweave|frontend)
    "suite_$SELECTOR" ;;
  *)
    die "未知 selector：'$SELECTOR'（可选 all|nl2sql|agent_core|metaweave|deploy_tests|frontend）" ;;
esac
log "检查完成：$SELECTOR"
