# LemonFlow V1 Phase 4：本地闭环与 Adapter 运行边界

> 阶段状态：`本地模拟闭环完成；V1 真实 Adapter 与人工模型中心已接通，待由项目方填写密钥后逐渠道小样本验收`  
> 目标：验证 V1 完整生产顺序、审核闸门、模型/Prompt 审计和资产引用；真实调用只能由人工启用，系统不自动扣费或切换模型。

## 已完成

- 增加 V1 生成任务入口：`POST /api/v1/production/projects/{project_id}/generation-runs/{run_key}`。
- 支持的正式节点：
  - `reference_analysis`
  - `story_generation`
  - `character_design`、`character_images`
  - `scene_design`、`scene_images`
  - `director_plan`
  - `shot_keyframes`
  - `video_generation`
  - `final_compose`
- 新运行会冻结：Workflow 定义版本、模型槽位、模型配置快照、活动 Prompt 模板版本与输入快照。
- 每一次模型执行创建 `ModelInvocation`，写入模型、Prompt、输入、输出引用、状态和完成时间。
- 自动创建明确标识为“本地模拟”的 V1 绑定，供没有 API Key 的开发环境完整验收；它们不会在旧模型配置页冒充已启用生产模型。
- `mock_v1` 能跑通完整 V1 闭环：上传 → 分析 → 锁定 → 三份故事候选 → 选择 → 角色/场景图锁定 → 导演分镜 → 关键帧锁定 → 视频审核 → 成片。
- 视频生成时为每个片段写入角色图、场景图、关键帧的 `VideoClipAssetBinding`，确保任何片段可以追溯实际使用的锁定版本。
- 新增 `v1_model_adapter_service.py`：V1 工作流只调用能力 Adapter；视频分析使用视觉 Adapter、故事/导演使用结构化文本 Adapter、图片使用图片 Adapter、视频使用异步视频 Adapter、成片使用 FFmpeg Adapter。
- V1 真实视频会先保存供应商任务号，再按受限间隔轮询到终态；失败会同时写入 `VideoClip`、`ModelInvocation` 和 `WorkflowRun`，不会伪装成审核通过。
- V1 图片关键帧会明确传入锁定角色图/场景图；若图片模型没有在配置中声明参考图字段，系统会在收费调用前拒绝执行，避免悄悄退化为文生图。
- 新增 V1 模型中心接口与页面：候选版本、人工启用/停用、单模型人工替换、故事多模型并行均与旧流程配置隔离。

## 真实模型的接入状态

现有旧流程的 Adapter 没有删除，仍可用于历史兼容。V1 正式调用不会把模型名写进工作流：它必须先在模型中心将候选配置绑定给 `VIDEO_ANALYSIS`、`STORY_GENERATE`、`IMAGE_GENERATE`、`VIDEO_GENERATE` 等槽位，再由对应 Provider Adapter 执行。

V1 当前已接通的协议能力如下；模型名称只是配置项，因此后续更换模型不需要修改业务代码：

| V1 槽位 | 可用 Adapter | 默认生产建议 |
| --- | --- | --- |
| `VIDEO_ANALYSIS` | `openai_compatible_vision` | Gemini 3.1 Pro Preview（中转站需支持多模态 Chat Completions） |
| `STORY_GENERATE`、角色/场景设计、导演分镜 | `openai_compatible` | Claude Sonnet / Opus / Gemini 等支持 JSON 输出的文本模型 |
| 三类图片资产 | `openai_compatible_image` | Banana 2 或其他图片中转模型；关键帧必须配置参考图字段 |
| `VIDEO_GENERATE` | `volcengine_ark_video`、`configurable_async_video` | 火山方舟 `doubao-seedance-2-0-mini-260615` |
| `FINAL_COMPOSE` | `ffmpeg_concat` | FFmpeg + 自有媒体存储 |

当前阶段没有使用任何真实 Key，也没有向任何第三方提交请求。Adapter “已接通”表示代码路径、配置校验、版本冻结和错误记录已完成；项目方仍必须在每个渠道上用 1 个小样本验收实际模型名、账单、响应格式、图片稳定地址和成片质量。

下一次真实渠道接入应按以下顺序逐个完成小样本验收：

1. 在 [V1 模型中心](/model-profiles) 保存候选版本，不要立即替换当前生产模型；
2. 将对应真实 Key 只写入 `infra/backend.env` 或部署平台 Secret，并重启 API 与 Worker；
3. 逐个用 1 个小样本完成：Gemini 视频分析 → 文本故事/导演 → Banana 图片 → Seedance 视频 → FFmpeg 成片；
4. 检查 `ModelInvocation` 中的模型、Prompt、任务号、时延和输出引用；补录人工评分、成本、成功率与采用率；
5. 由人工在模型中心启用/替换生产槽位，绝不让评分系统自动替换。

## 验证结果

新增集成测试 `test_v1_mock_production_closure.py` 覆盖从上传参考视频到完成成片的所有 V1 阶段，并确认每个调用都有 Prompt 版本和成功的 `ModelInvocation` 审计记录。新增 `test_v1_model_adapter_service.py`、`test_v1_model_configuration.py` 和真实协议单元测试，覆盖能力隔离、视频轮询、参考图字段、V1 五类视觉输出以及人工替换模拟配置。

本地验证结果：`38 passed`。所有测试只使用 SQLite 临时数据库、伪 HTTP 响应和 `mock://` 结果，不生成、下载或付费调用真实媒体。
