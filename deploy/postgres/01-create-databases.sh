#!/bin/bash
# doc 13 §6.1：幂等创建两个目标逻辑库（metaweave / lg_persist）。
#
# 自包含约束（doc 13 §6.1、§8）：
# - 直接调用 psql（容器内 Unix socket + trust），不依赖官方入口的
#   docker_process_sql 等未导出函数，也不用 exec 替换进程；
# - 官方入口对本脚本（可执行 .sh）直接执行；bootstrap 通过
#   `docker compose exec -T postgres bash /docker-entrypoint-initdb.d/01-create-databases.sh`
#   独立重入，两种上下文行为一致；
# - 已存在的库保留原样，只创建明确缺失的目标库，不删除、不清空。

set -euo pipefail

: "${POSTGRES_USER:?需要 POSTGRES_USER（官方镜像环境）}"
: "${POSTGRES_PASSWORD:?需要 POSTGRES_PASSWORD（官方镜像环境）}"

PLATFORM_DB_NAME="${PLATFORM_DB_NAME:-metaweave}"
LANGGRAPH_DB_NAME="${LANGGRAPH_DB_NAME:-lg_persist}"

for db in "$PLATFORM_DB_NAME" "$LANGGRAPH_DB_NAME"; do
    case "$db" in
        ""|[0-9]*|*[!a-z0-9_]*)
            echo "[initdb] 非法库名：${db}" >&2
            exit 64
            ;;
    esac
done

psql_opts=(-v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres --no-psqlrc)

for db in "$PLATFORM_DB_NAME" "$LANGGRAPH_DB_NAME"; do
    exists="$(psql "${psql_opts[@]}" --tuples-only --no-align \
        --command "SELECT 1 FROM pg_database WHERE datname = '${db}'")"
    if [ "$exists" = "1" ]; then
        echo "[initdb] database '${db}' 已存在，保留"
    else
        psql "${psql_opts[@]}" --command "CREATE DATABASE \"${db}\""
        echo "[initdb] 已创建 database '${db}'"
    fi
done
