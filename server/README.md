# 后端服务模块

本目录是平台的业务中枢，采用 FastAPI + SQLAlchemy。它只负责接口、领域规则、持久化和工作流调度；耗时模型调用通过 `app/services/worker_runtime.py` 的调度边界执行。

## 目录说明

- `app/api/`：HTTP 接口层，只做鉴权（V1 暂无）、参数校验、响应编码和任务投递。
- `app/core/`：配置与数据库连接等横切能力。
- `app/models/`：数据库实体；保存可审计的事实，不在这里写业务流程。
- `app/schemas/`：接口输入输出契约，避免把数据库模型直接暴露给前端。
- `app/services/`：项目、资产、模型适配和工作流业务规则。
- `tests/`：后端接口与工作流测试。

## 当前功能

已实现从视频分析到按镜头分组视频片段的 V1 工作流，并包含模型配置中心。开发模式由进程内后台任务模拟 Worker；生产可将 `TASK_EXECUTION_MODE` 切换为 `rq`，由 Redis/RQ 独立 Worker 消费相同的执行函数。投递失败会记录为可重试失败，不能静默滞留为 PENDING。

模型配置使用版本化 `ModelProfile`：每次运行冻结具体配置，真实密钥只能用 `secret_env_name` 从运行环境读取。参考视频分析被拆为独立的语音转写和画面综合两个步骤；转写原文只在 Worker 内存中传递，不会写入 API 或数据库。当前可直接执行的是模拟适配器；接入新的中转站需要新增并测试对应适配器后才能启用配置。

## 启动

在本目录下执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

启动后访问 `http://localhost:8000/docs` 查看接口契约。

运行测试：

```bash
pytest -q
```

生产 Worker 启动和环境变量参见 `../docs/Redis_RQ_Worker部署说明.md`。本机可用根目录
`docker-compose.yml` 同时启动迁移、API、Worker、PostgreSQL、Redis 与 MinIO；命令和
存储切换边界见 `../infra/README.md`。

## 生产数据库升级

生产环境设置 `DATABASE_SCHEMA_MODE=migrate`，每次发布先执行：

```bash
cd server
alembic upgrade head
```

迁移成功后再启动 API 和 Worker。迁移历史与新增字段流程见 `migrations/README.md`；
本地 `DATABASE_SCHEMA_MODE=auto` 仅服务于零配置演示，不应用于生产。
