# 前端镜像：Vite 构建产物由 nginx 托管，并同源反代后端 API 与 WebSocket

FROM node:22-alpine AS build

WORKDIR /build

# package.json 的 packageManager 字段固定 pnpm@10.33.0，corepack 按其拉取
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN corepack enable

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .
RUN pnpm build

FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html

EXPOSE 80
