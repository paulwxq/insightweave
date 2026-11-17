# MetaWeave 应用镜像（doc 13 §5.1 / 13.1 §3–7）
FROM docker.io/library/python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

ENV TZ=Asia/Shanghai
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tzdata libstdc++6 libgcc-s1 \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir uv==0.11.2

WORKDIR /app/metaweave
# 项目源码与锁定依赖（保持现有路径/安装方式；§5.1）
COPY metaweave/pyproject.toml metaweave/uv.lock metaweave/README.md ./
COPY metaweave/metaweave ./metaweave
COPY metaweave/backend ./backend
COPY metaweave/services ./services
COPY metaweave/configs ./configs
COPY metaweave/db ./db
COPY metaweave/scripts ./scripts
RUN uv sync --frozen --no-dev --extra platform --python /usr/local/bin/python --no-python-downloads

# 部署运行时与契约（doc 13 §5.1；任务 2A 产物）
COPY deploy/contract.json /app/deploy/contract.json
COPY deploy/runtime/ /app/deploy/runtime/
COPY deploy/scripts/init_platform.py /app/deploy/scripts/init_platform.py
COPY deploy/scripts/smoke.py /app/deploy/scripts/smoke.py

ENV PATH="/app/metaweave/.venv/bin:$PATH"
ENV PYTHONPATH="/app:/app/metaweave"

EXPOSE 8001
CMD ["uvicorn", "backend.api.logged_app:create_logged_app", "--factory", "--no-access-log", "--host", "0.0.0.0", "--port", "8001", "--timeout-graceful-shutdown", "120"]
