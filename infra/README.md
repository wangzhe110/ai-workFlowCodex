# 基础设施模块

本目录承载部署运行依赖，而不承载业务代码。

## 当前组件

- PostgreSQL：生产业务数据，保存项目、素材元信息、工作流运行记录、步骤状态和模型配置版本。
- Redis：生产任务队列、进度事件和限流状态。第一天的本地演示不依赖 Redis。
- MinIO：S3 兼容对象存储，生产时保存源视频、生成图片、视频片段和最终成片；抽帧仅在 Worker 临时目录中处理，不作为 V1 生产资产长期保存。

## 运行说明

根目录 `docker-compose.yml` 是一套可本机验收的近生产编排：`migrate` 一次性执行
数据库迁移，之后才启动 API 和 RQ Worker；两者共用同一个服务镜像、同一个媒体卷。
先复制 `compose.env.example` 和 `backend.env.example` 为未提交的 `compose.env`、
`backend.env`。后者会同时注入 API 和 Worker，填写模型配置中心对应
`secret_env_name` 的真实 Key 以及 S3 凭据。V1 固定使用三条独立通道：

- `YUNWU_REASONING_API_KEY`：云雾推理、视觉理解、故事、角色、场景和导演文本模型；同一 Key 可被多个 Profile 引用。
- `ARK_API_KEY`：火山方舟官方 `volcengine_ark_image`（Seedream 图片）和 `volcengine_ark_video`（Seedance 视频）可共享这一渠道 Key；图片和视频仍由各自 Profile、Adapter 独立调用。

旧云雾/Fal 图片 Profile 与 Adapter 仅为历史审计保留，当前三个图片槽位不再绑定它；默认部署与 Run 1 不需要云雾图片 Key。

不要把模型 ID、API Base URL 或生成参数放入 `backend.env`；它们属于模型中心各 Profile 的非敏感配置。再在项目根目录执行：

```bash
docker compose --env-file infra/compose.env up --build
```

首次拉取镜像、安装 Python/Node 依赖和构建服务镜像需要一些时间。启动完成后，直接访问
`http://127.0.0.1:5173` 使用前端；Nginx 会把页面请求同源转发给 API。`/health` 只表示 API
进程仍在运行；访问 `http://127.0.0.1:8000/ready` 返回 `ok`，才表示数据库和 Redis
都已可用。停止服务使用
`docker compose --env-file infra/compose.env down`，不会
删除命名卷中的数据库和媒体。账号密码仅供本机使用，生产必须改为密钥服务或部署环境
变量，且对象存储应启用私有 Bucket、生命周期规则与备份。

这套本机编排默认让 API 与 Worker 通过共享媒体卷读取源视频、生成本地成片；因此适合
功能验收。正式多机生产应设置 `SOURCE_VIDEO_STORAGE_MODE=s3`、
`GENERATED_IMAGE_DELIVERY_MODE=s3`、`FINAL_VIDEO_DELIVERY_MODE=s3`，并给 Worker
注入最小权限的 S3 凭据和公网 HTTPS CDN 地址。Compose 自带的 MinIO 是本机调试用，
其 HTTP 地址不能直接交给第三方图生视频模型。

## 备份与恢复

本机 Compose 可使用 `scripts/backup_postgres.sh` 导出 PostgreSQL 逻辑备份；恢复脚本
必须显式确认，并会在恢复前自动留存当前数据库。媒体文件不在 PostgreSQL 内，正式生产
必须同时启用对象存储的版本化和备份。完整命令、恢复检查和演练频率见
`../docs/运维备份恢复说明.md`。

PostgreSQL 的结构升级由 `server/migrations/` 管理。生产发布顺序固定为：备份数据库 →
`alembic upgrade head` → 启动 API → 启动/滚动更新 Worker；不要由多个 API 实例自行建表。

## 扩展边界

未来新增监控、日志采集、反向代理或独立 Worker 编排时，均在本目录增加部署定义，不把部署逻辑混入 `server/` 业务模块。
