# 领域服务模块

服务层承载业务规则，路由层不得绕过它直接修改工作流或审核状态。

## V1 核心服务

- `v1_production_service.py`：唯一主流程阶段、人工锁定/选择/审核和当前版本指针。
- `v1_execution_service.py`：创建并执行 V1 生成任务；冻结全部上下文、模型和 Prompt；记录 `ModelInvocation`；管理镜头视频子任务与幂等键。
- `worker_runtime.py`：本地/RQ 投递边界。视频父任务只聚合，`VIDEO_SHOT` 子任务独立提交或恢复轮询。
- `v1_model_adapter_service.py`：按照冻结配置调用视觉、文本、图片、异步视频与 FFmpeg 合成 Adapter；不写死模型。
- `analysis_provider.py`：OpenAI 兼容视觉、文本、图片、火山方舟 Seedance 和可配置异步视频协议的参数转换与返回值标准化。
- `video_frame_service.py`：通过 FFprobe/FFmpeg 验证视频并抽帧，供参考视频分析使用。
- `storage.py`：源视频、生成图片和最终成片的本地/S3 存储边界。
- `v1_configuration_service.py`：Workflow 定义、能力槽位、候选模型版本、Prompt 模板及人工启用规则。
- `v1_quality_service.py` / `v1_trace_service.py`：只读取已有调用和审核，提供质量比较与版本追溯；不自动换模型、不暴露密钥。
- `asset_library_service.py`：资产中心的角色/场景版本、项目采用关系和旧锁图惰性补齐。它不调用模型、不改变生产阶段；采用资产仍必须回到现有锁图审核。
- `commerce_configuration_service.py`：单独、幂等发布 `LEMONFLOW_COMMERCE` /
  `LemonFlow_Commerce_V1` 七节点定义；不会初始化或覆盖 V1 工作流、模型配置和项目状态。
- `commerce_domain_service.py`：带货短剧的跨表领域校验。目前负责验证 `SubShotPlan` 的
  时间不超过 `VideoSegmentPlan.target_duration_ms`；后续创建 Commerce 分镜的服务必须
  经过该边界。

## 任务与模型规则

Worker 执行时只读取 `WorkflowRun`/`WorkflowStep` 的冻结快照，不能回查当前启用模型、ACTIVE Prompt 或最新上传素材。视频任务首次提交后必须先保存 `provider_task_id`；恢复时只轮询该任务号。`final` 执行器只使用运行创建时冻结的当前采用、审核通过片段 ID。

Phase 4 的导演任务会把资产中心版本一并冻结。关键帧和视频子任务只读取此快照，不会因资产中心之后新增 v2 而替换本次生产的角色或场景。

旧 `topic_service.py`、`story_service.py`、`storyboard_service.py`、`image_service.py`、`video_service.py` 和 `final_video_service.py` 仅为历史兼容/辅助功能保留，不属于新项目的 V1 主生产链路。

Commerce 第一阶段仅建设数据库领域和工作流定义，不提供路由、前端、ASR/OCR 或模型
调用。后续批量渲染仍必须复用本目录现有的 WorkflowRun/WorkflowStep 异步调度边界。
