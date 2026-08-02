# 第一阶段只负责编译 Vue 静态文件；运行镜像不携带 Node、npm 或源码。
FROM node:22-alpine AS builder

WORKDIR /app/web

COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/index.html ./index.html
COPY web/tsconfig.json ./tsconfig.json
COPY web/tsconfig.app.json ./tsconfig.app.json
COPY web/tsconfig.node.json ./tsconfig.node.json
COPY web/vite.config.ts ./vite.config.ts
COPY web/src ./src
RUN npm run build

# Nginx 只提供静态页面和同源 /api 反向代理，浏览器不会直接看到后端容器地址。
FROM nginx:1.27-alpine

COPY web/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/web/dist /usr/share/nginx/html

EXPOSE 80
