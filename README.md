# AI 短剧生产工作流平台

这是面向生产环境演进的 V1 工程。目前已打通：**创建项目 → 上传授权参考视频 → 抽象分析 → 原创选题 → 故事包 → 分镜 → 分镜图片 → 按镜头分组的视频片段**。所有关键阶段均有人工确认、任务状态与版本记录。

## 工程原则

- **模块化单体**：先保持一个可部署、可调试的后端；业务边界明确，后续可按 Worker、模型或媒体处理能力拆分。
- **任务不依赖请求进程**：接口仅创建任务；耗时模型调用由 Worker 执行并留下可追踪的步骤记录。
- **模型可替换**：业务工作流只调用统一的模型适配器，不直接依赖任一中转平台。
- **不复制参考作品**：分析结果只保存抽象创作特征，如开头机制、冲突、节奏与镜头结构；不得复用未经授权的具体台词、人物形象、音乐或画面。
- **密钥不进代码**：所有 API Key 仅从服务端运行环境读取，前端与数据库只保存供应商/模型标识和脱敏信息。

## 目录

- `web/`：Vue 3 前端，负责用户操作与任务进度展示。
- `server/`：FastAPI 业务 API、领域模型、工作流与本地开发 Worker。
- `infra/`：本地开发所需 PostgreSQL、Redis、MinIO 等基础设施定义。
- `docs/`：接口、部署及模型接入说明。

每个业务代码目录均有中文 `README.md`，用于说明模块职责、文件/页面作用、数据模型依赖和扩展方式。

## 本地启动（开发阶段）

1. 复制 `.env.example` 为 `.env`，先保留默认的 SQLite 与本地文件存储配置。
2. 在 `server/` 创建虚拟环境并安装 `requirements.txt`。
3. 启动 API：`uvicorn app.main:app --reload --port 8000`。
4. 在 `web/` 安装依赖后执行 `npm run dev`。

开发模式使用进程内 Worker 以降低首次启动门槛；生产环境可设置 `TASK_EXECUTION_MODE=rq`，由 Redis/RQ 独立 Worker 消费任务。生产发布还须设置 `DATABASE_SCHEMA_MODE=migrate`，先运行 `cd server && alembic upgrade head`，再启动 API/Worker。若要在一台电脑先验收完整前端、独立 API、Worker、PostgreSQL、Redis 与 MinIO，可将 `infra/compose.env.example`、`infra/backend.env.example` 各复制为不提交的同名无 `.example` 文件，再执行 `docker compose --env-file infra/compose.env up --build`，然后打开 `http://127.0.0.1:5173`。没有部署经验时，优先按 `docs/部署与模型配置快速指南.md` 操作；它将启动、模型类型、密钥配置、预检和启用步骤串成了一条可执行路径。详细规则见 `infra/README.md`、`docs/Redis_RQ_Worker部署说明.md` 和 `server/migrations/README.md`。

上线前要同时准备数据库和媒体对象的备份。仓库提供本机 Compose 的 PostgreSQL 备份/恢复脚本；媒体对象必须由 S3/MinIO 的版本化与复制策略保护。详见 `docs/运维备份恢复说明.md`。

每次发布前可运行 `bash infra/scripts/verify_release.sh`，它会在临时数据库上验证迁移，
再运行后端测试与前端生产构建，作为发布的最低质量门禁。

## 当前 V1 工作流

1. 上传有权使用的参考视频；可独立转写开头有限音频并抽取画面帧，综合提取开场机制、冲突、节奏等抽象特征。转写原文仅在任务内存中使用，禁止复刻具体表达。
2. 人工维护创作资产库，生成并确认原创选题、故事包和分镜。
3. 生成单镜图片；每次单镜重做都新增版本，历史图片不覆盖。
4. 在图片齐备后将连续镜头按可配置数量（默认 4 镜）分组，生成独立视频片段；每组可单独重做并保留版本。
5. 按组号合成完整成片；导出会冻结采用的片段版本，重做某组后再次导出会生成新的成片版本，真实 FFmpeg 模式可下载 MP4。
6. 在“模型配置中心”为每个步骤维护模型配置版本。配置只保存非敏感参数与密钥环境变量名；当前本地模拟适配器可直接运行，新的中转站适配器需接入后才可启用。

## 真实模型接入边界

模型适配器集中在 `server/app/services/analysis_provider.py`，业务服务只读取 `ModelProfile` 快照。接入一个中转站时：新增适配器、使用 `secret_env_name` 从部署环境读取 Key、把其返回值归一化为平台契约，再允许对应配置启用。不要把 Key 写进前端、数据库或任务输入输出。配置中心还支持无扣费预检和人工小样本评测记录，可比较成本、耗时、成功率与质量评分后再切换版本。

当前已实现 `openai_compatible` 文本适配器（原创选题、故事包、分镜细纲）、`openai_compatible_image` 同步图片适配器（分镜图片）、`openai_compatible_transcription` 语音转写适配器（有限开头音轨、仅内存传递）和 `openai_compatible_vision` 参考视频分析适配器（FFmpeg 抽帧后发送给视觉模型）。云雾文本/图片/转写/视觉配置可使用 `https://yunwu.ai/v1` 与 `YUNWU_API_KEY` 环境变量；实际模型名必须以账户后台的可用模型列表为准。

“视频片段”已实现 `configurable_async_video`：它提交任务、保存供应商任务号并轮询最终视频地址，请求/响应字段均由该步骤配置决定，便于以后更换中转站。真实运行只能使用供应商可访问的 HTTPS 图片地址。参见 `docs/云雾异步图生视频接入说明.md`、`docs/云雾视频分析接入说明.md` 与 `docs/云雾语音转写接入说明.md`。

“完整成片导出”支持默认模拟流程和 `ffmpeg_concat` 真实合成模式。后者在 Worker 下载已冻结顺序的 HTTPS 视频片段，用 FFmpeg 生成 MP4；`FINAL_VIDEO_DELIVERY_MODE=local` 用于单机下载，生产可切换 `s3` 将 MP4 流式上传至 S3/MinIO。详见 `docs/完整成片导出说明.md`。
