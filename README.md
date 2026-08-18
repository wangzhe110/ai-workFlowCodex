# LemonFlow V1

LemonFlow V1 是一个带人工审核闸门的多模型 AI 视频生产工作流。它只保留一条正式生产链路：

```text
上传授权参考视频
→ 视频分析
→ 锁定创作简报
→ 多模型故事生成
→ 选择故事
→ 角色资产与角色图锁定
→ 场景资产与场景图锁定
→ AI 导演分镜
→ 分镜关键帧锁定
→ 视频片段生成与审核
→ 合成成片
```

参考视频仅用于提炼结构、节奏、开头机制和视觉规律；不得复制未经授权的台词、人物、画面、音乐或故事表达。

## 当前能力

- **人工审核与不可覆盖版本**：分析、故事、角色图、场景图、关键帧和视频片段均有正式状态流转。重新生成只会创建新版本。
- **正确的视频版本选择**：每个 `ShotPlan` 只有一个当前采用视频版本。成片仅冻结每个镜头当前采用且已审核通过的片段，历史驳回片段永久保留但绝不参与合成。
- **任务快照与幂等保护**：创建任务时冻结源视频、Workflow、模型配置、Prompt、已锁定分析/故事/资产；模型中心或 Prompt 中心之后的切换不会影响已创建任务。重复点击或网络重试会返回同一未完成任务，不重复提交收费任务。
- **独立视频子任务**：每个镜头是独立的后台视频任务，保存脱敏展示的供应商任务号；Worker 中断后只恢复查询，不会重新提交供应商任务。
- **可替换模型**：业务代码依赖能力槽位和 Adapter，不写死模型名称。当前默认目标模型为 Gemini 视频分析、Claude/Gemini 并行故事、Seedream 5.0 Pro 图片、Seedance 视频；实际选择由模型中心的已启用配置决定。
- **全程可追溯**：每次调用会记录冻结的模型、Prompt、输入快照、耗时、可用用量/成本数据和供应商任务号。质量报表只给人工比较，不会自动切换模型。
- **资产中心与跨项目复用**：角色、场景在人工锁图后会沉淀为资产中心的不可变版本；新项目可把已验收版本作为待锁图候选采用，仍不能跳过审核。镜头、关键帧和视频片段同时记录项目锁图与资产中心版本。
- **结构化 AI 导演方案**：每个镜头保存角色/场景版本、动作、情绪、镜头类型、运镜、光线以及图片、视频、声音 Prompt，供后续生产步骤直接冻结使用。

## 目录

- `web/`：Vue 3 + TypeScript + Element Plus 生产台、资产中心、模型中心、Prompt 中心、质量报表和项目追溯页。
- `server/`：FastAPI、领域模型、状态机、模型 Adapter、媒体处理与 Worker。
- `infra/`：Docker Compose、PostgreSQL/Redis/MinIO 配置与备份、发布检查脚本。
- `docs/`：面向制作人员和运维人员的当前操作文档。

## 快速启动（本地模拟，不调用真实模型）

1. 复制环境示例：`cp .env.example .env`。
2. 启动后端：

   ```bash
   cd server
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   uvicorn app.main:app --reload --port 8000
   ```

3. 新开终端启动前端：

   ```bash
   cd web
   npm install
   npm run dev
   ```

4. 打开 `http://127.0.0.1:5173`，创建项目后进入生产台。未配置真实模型时，请在模型中心使用本地模拟配置，按 [V1 本地闭环验收操作](docs/V1本地闭环验收操作.md) 验证流程。

生产或近生产本机验收请使用 Docker Compose：复制 `infra/compose.env.example` 和 `infra/backend.env.example` 为不提交的同名文件，然后执行：

```bash
docker compose --env-file infra/compose.env up --build
```

生产部署必须先执行数据库迁移：`cd server && DATABASE_SCHEMA_MODE=migrate alembic upgrade head`；再启动 API 与 RQ Worker。详细步骤见 [部署与模型配置快速指南](docs/部署与模型配置快速指南.md)。

## 使用入口

- 制作人员从 [用户使用手册](docs/用户使用手册.md) 开始。
- 跨项目复用角色与场景请阅读 [资产中心操作](docs/资产中心操作.md)。
- 第一次配置模型，从 [模型配置小白操作卡](docs/模型配置小白操作卡.md) 开始；真实 Key 只写服务器环境变量，绝不写进网页、数据库或 Git。
- 需要优化系统级模型执行说明时，使用 [Prompt 版本管理操作](docs/LemonFlow_V1_Prompt模板操作.md) 的“复制 Draft → 本地预览 → 发布 → 显式启用”流程；项目镜头的视频 Prompt 仍在生产台审核。
- 启用 Seedance 前阅读 [豆包 Seedance 视频操作说明](docs/豆包Seedance视频操作说明.md)。
- 模型、Prompt 与供应商任务号的历史定位见 [生产追溯操作](docs/LemonFlow_V1_生产追溯操作.md)。

当前代码已完成本地 Mock、数据库迁移、任务幂等、媒体真实性和视频驳回重做的自动化验证。真实模型 Key、小样本成本和供应商实际输出尚未由本仓库自动验证，必须先走一条授权样本的人工验收，不能直接批量生产。
