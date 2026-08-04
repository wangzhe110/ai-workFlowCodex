# Redis/RQ Worker 部署说明

平台默认 `TASK_EXECUTION_MODE=inline`，适合本地模拟：FastAPI 在同一进程调度任务。生产环境应切换为 `TASK_EXECUTION_MODE=rq`，由独立 Worker 从 Redis 队列执行 V1 的视频分析、故事、角色、场景、导演分镜、图片、镜头视频子任务和成片合成。

## 1. 配置

在部署环境或未提交的 `.env` 中设置：

```dotenv
TASK_EXECUTION_MODE=rq
REDIS_URL=redis://redis:6379/0
RQ_QUEUE_NAME=ai_drama
WORKER_JOB_TIMEOUT_SECONDS=1800
```

`WORKER_JOB_TIMEOUT_SECONDS` 应覆盖单个普通生成任务；V1 不会在一个 RQ Job 内串行等待全部视频镜头。视频父运行只聚合状态，每个 `ShotPlan` 都是独立 `VIDEO_SHOT` 子任务。

每个视频子任务第一次提交供应商后立即保存 `provider_task_id`。Worker 重启或查询中断时，会用此任务号恢复轮询，不会重新提交同一供应商任务。系统不会自动重试已明确失败的收费调用；人工确认原因后才可创建新版本。

## 2. 启动顺序

1. 启动 PostgreSQL、Redis、对象存储和 API。
2. 在与 API 使用相同代码、环境变量和素材挂载目录的容器/主机启动 Worker：

   ```bash
   cd server
   rq worker --url "$REDIS_URL" "$RQ_QUEUE_NAME"
   ```

3. 水平扩容时可启动多个 Worker；运行键、步骤幂等键和模型调用幂等键会阻止重复消耗模型额度。

参考视频视觉分析依赖 FFmpeg/FFprobe，使用 `LOCAL_STORAGE_PATH` 时每个 Worker 还
必须挂载同一目录；否则 Worker 无法读取上传的视频。

## 3. 可观测性与故障处理

- API 投递 Redis 失败会将新建运行标记为 `FAILED` 并保存错误信息，而不是留下永远 `PENDING` 的任务；Redis 恢复后由人工创建新的合法版本。
- RQ 记录 Worker 进程级异常；平台数据库记录模型业务失败、镜头子任务、供应商任务号和每一步进度。两者都要纳入监控。
- 前端停止轮询或用户刷新页面不等于 Worker 失败；生产台会重新读取已有运行和每个镜头的状态。
- 生产应对 Redis 启用持久化、访问认证、网络隔离和备份；不要把 Redis 暴露到公网。
- 长视频/慢模型应压测队列并发、超时、失败重试、成本和幂等性，再确定 Worker 数量。
