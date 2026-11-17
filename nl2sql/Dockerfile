# NL2SQL 应用镜像（doc 13 §5.1 / 13.1 §3–7）
FROM docker.io/library/python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

ENV TZ=Asia/Shanghai
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tzdata libstdc++6 libgcc-s1 \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir uv==0.11.2

WORKDIR /app/nl2sql
COPY nl2sql/pyproject.toml nl2sql/uv.lock nl2sql/README.md ./
COPY nl2sql/src ./src
RUN uv sync --frozen --no-dev --python /usr/local/bin/python --no-python-downloads

# 部署运行时与契约（doc 13 §5.1；任务 2A 产物）
COPY deploy/contract.json /app/deploy/contract.json
COPY deploy/runtime/ /app/deploy/runtime/
COPY deploy/scripts/init_platform.py /app/deploy/scripts/init_platform.py
COPY deploy/scripts/smoke.py /app/deploy/scripts/smoke.py

ENV PATH="/app/nl2sql/.venv/bin:$PATH"
ENV PYTHONPATH="/app:/app/nl2sql"
# Docker 默认 INFO（覆盖 YAML 的 DEBUG，减少生产日志噪音与提示词泄漏；
# 临时排查时在部署 env 设 NL2SQL_LOG_LEVEL=DEBUG 并重新创建容器）
ENV NL2SQL_LOG_LEVEL=INFO

EXPOSE 8008
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8008", "--timeout-graceful-shutdown", "120"]
