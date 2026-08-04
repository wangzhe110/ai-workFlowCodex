# 后端服务模块

后端采用 FastAPI + SQLAlchemy，负责 LemonFlow V1 的 API、状态机、持久化、媒体校验和任务调度。长耗时模型调用不在 HTTP 请求中执行，而由 `app/services/worker_runtime.py` 投递到本地或 RQ Worker。

## 目录

- `app/api/`：参数校验、调用领域服务、返回契约；不直接修改状态或调用供应商。
- `app/core/`：配置、数据库和运行模式。
- `app/models/`：数据库事实与约束。
- `app/schemas/`：稳定 API 契约。
- `app/services/`：V1 工作流、审核、冻结快照、Adapter 与媒体服务。
- `migrations/`：Alembic 数据库迁移。
- `tests/`：不使用真实 Key 的回归、媒体和状态机测试。

## V1 后端保证

- 唯一主链路由 `ProjectProductionState` 控制，不能由浏览器跳过审核。
- 创建 `WorkflowRun` 时冻结源素材、Workflow、模型、Prompt 和已锁定上游资产。
- 相同项目和 `run_key` 只允许一个未完成任务；收费模型调用以 `idempotency_key` 追溯。
- 视频按 `ShotPlan` 建立独立子任务，保存供应商任务号并可恢复轮询。
- 每个镜头通过 `selected_video_clip_id` 指向当前采用片段；合成只消费当前采用且 `APPROVED` 的版本。

## 开发与迁移

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
pytest -q
```

生产发布先执行 `DATABASE_SCHEMA_MODE=migrate alembic upgrade head`，再启动 API 与 Worker。Worker 配置见 `../docs/Redis_RQ_Worker部署说明.md`，迁移规则见 `migrations/README.md`。
