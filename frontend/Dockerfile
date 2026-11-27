# 前端应用镜像：Node 构建 + Nginx 运行（doc 13 §5.1 / 13.1 §3/§6）
FROM docker.io/library/node:22-bookworm-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee AS build
WORKDIR /app/frontend
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --include=dev
COPY frontend/index.html frontend/vite.config.ts frontend/tsconfig.json ./
COPY frontend/src ./src
COPY frontend/public ./public
# API origin 由 ci.sh 传入，Vite 构建时写入静态文件。
ARG VITE_API_BASE_URL
ARG VITE_NL2SQL_API_BASE_URL
ARG APP_GIT_SHA
RUN : "${VITE_API_BASE_URL:?需要 VITE_API_BASE_URL}" \
      "${VITE_NL2SQL_API_BASE_URL:?需要 VITE_NL2SQL_API_BASE_URL}" \
    && npm run build

FROM docker.io/library/nginx:1.30.6-trixie@sha256:42c68eab6f7e22611786a9fe1d1c019e275c50185bd54b8f75dc2b9184322703
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/frontend/dist /usr/share/nginx/html
COPY deploy/nginx/default.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
