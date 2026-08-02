# 前端 Nginx 模块

`default.conf` 用于 Docker 生产镜像：它提供 Vue 编译后的静态页面，并将同源的
`/api/` 请求转发到 Compose 内的 FastAPI `api` 服务。浏览器不需要知道 Docker 服务名、
后端端口或任何模型密钥。

规则还包含：

- 与后端默认 500 MB 上限匹配的上传体积上限；
- SPA 深层路由刷新回退到 `index.html`；
- 适合视频生成与上传等待的代理超时；
- 最基础的浏览器安全响应头。

生产云环境可用 Traefik、Nginx Ingress 或云网关替换本文件，但必须保留同源 `/api/` 转发、
上传上限和长任务等待策略。
