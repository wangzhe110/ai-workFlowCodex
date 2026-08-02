# Redis/RQ Worker 部署说明

平台默认 `TASK_EXECUTION_MODE=inline`，适合本地演示：FastAPI 请求结束后在同一进程
执行模型任务。生产环境应切换为 `TASK_EXECUTION_MODE=rq`，由独立 Worker 从 Redis
队列取走视频分析、选题、故事、分镜、图片和视频片段任务。

## 1. 配置

在部署环境或未提交的 `.env` 中设置：

```dotenv
TASK_EXECUTION_MODE=rq
REDIS_URL=redis://redis:6379/0
RQ_QUEUE_NAME=ai_drama
WORKER_JOB_TIMEOUT_SECONDS=1800
```

`WORKER_JOB_TIMEOUT_SECONDS` 应覆盖最慢的视频模型轮询时间；V1 默认 30 分钟。
`WORKFLOW_STALE_AFTER_SECONDS` 默认比它多 5 分钟，用于识别 Worker 已中断但数据库
仍显示 `RUNNING` 的任务。系统不会自动重放模型调用：中转站超时后无法可靠判断请求
是否已被接受，自动重试可能重复扣费或生成重复素材。到期任务会自动转为 `FAILED`，
保留可读原因，必须由操作者检查后手动重试。

## 2. 启动顺序

1. 启动 PostgreSQL、Redis、对象存储和 API。
2. 在与 API 使用相同代码、环境变量和素材挂载目录的容器/主机启动 Worker：

   ```bash
   cd server
   rq worker --url "$REDIS_URL" "$RQ_QUEUE_NAME"
   ```

3. 水平扩容时可启动多个 Worker；每个任务的领域状态机仍会阻止已成功运行重复消耗
   模型额度。

参考视频视觉分析依赖 FFmpeg/FFprobe，使用 `LOCAL_STORAGE_PATH` 时每个 Worker 还
必须挂载同一目录；否则 Worker 无法读取上传的视频。

## 3. 可观测性与故障处理

- API 投递 Redis 失败会将新建运行标记为 `FAILED` 并保存错误信息，而不是留下永远
  `PENDING` 的任务；Redis 恢复后可从前端重试。
- RQ 会记录 Worker 进程级异常；平台数据库则记录模型业务失败、分组视频失败和
  每一步进度。两者都要纳入监控。
- 生产应对 Redis 启用持久化、访问认证、网络隔离和备份；不要把 Redis 暴露到公网。
- 长视频/慢模型应压测队列并发、超时、失败重试、成本和幂等性，再确定 Worker 数量。
