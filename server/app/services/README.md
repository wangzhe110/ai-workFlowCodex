# 领域服务模块

服务层承载业务规则，接口层不得绕过它直接修改工作流状态。

## 文件职责

- `storage.py`：源文件存储与模型图片转存边界。默认本地文件/直连图片 URL 仅用于开发；`S3GeneratedImageDelivery` 可把模型图片立即转存到 MinIO/S3 并返回稳定 HTTPS 地址。
- `video_frame_service.py`：使用 FFmpeg 在 Worker 内存中均匀抽取、缩放并限制字节数的参考视频帧，仅为视觉模型调用提供 data URL，不持久化视频帧。
- `video_audio_service.py`：将参考视频开头压缩为受时长/大小限制的临时 MP3；音频只交给本次转写请求，随后立即删除。
- `analysis_provider.py`：视频分析、语音转写、文本、图片和异步视频模型适配边界；所有第三方返回值都在此归一化。
- `workflow_service.py`：创建运行、执行状态机、失败记录与结果编码。
- `worker_runtime.py`：把任务投递到后台的边界。支持进程内演示和 Redis/RQ 独立 Worker；路由和领域服务无需因部署模式切换而修改。
- `library_service.py`：爆点元素库、爆款开头库的创建、查询、更新和软停用规则。
- `topic_service.py`：冻结分析/资产库输入、生成原创选题候选并保留人工确认状态。
- `story_service.py`：从人工确认选题生成并确认故事、角色、场景的一版快照。
- `storyboard_service.py`：从确认故事生成镜头数量可配置的分镜细纲，并等待人工确认。
- `image_service.py`：从确认分镜批量或单镜生成图片版本，保存提示词、状态与版本号。
- `video_service.py`：校验每镜成功图片，将确认分镜按可配置镜头数分组，生成并保留独立视频片段版本。
- `final_video_service.py`：冻结最近完整分组方案与各组最新成功片段版本，用 FFmpeg 合成为完整 MP4，并保存成片版本与失败原因。
- `model_profile_service.py`：维护步骤模型配置的不可变版本、激活门禁和运行快照；拒绝数据库保存真实密钥。

## 模型适配规则

业务工作流只能依赖 `VideoAnalysisProvider.analyze()` 的标准返回值，不能访问任一中转平台的私有字段。接入真实模型时，新建适配器并在部署配置中选择它；不得把 Key 传给前端或存到 `ModelProfile.provider_config`。

`analysis_provider.py` 当前还提供 `OpenAICompatibleJsonProvider`：它已用于原创选题、故事包和分镜细纲。配置 `provider_key=openai_compatible`、`api_base_url`、`secret_env_name`、真实模型名后即可启用。`OpenAICompatibleImageProvider` 已用于同步分镜图片生成，配置 `provider_key=openai_compatible_image` 和 `image_size` 即可启用。

`ConfigurableAsyncVideoProvider` 已用于按组图生视频：通过 `submit_path`、`query_path_template`、输入字段、状态映射和视频 URL 路径配置，归一化不同中转站的异步协议。`video_service.py` 会在创建任务前校验每组所需首帧是否为公网 HTTPS 地址，阻止无效请求扣费；随后保存每组的任务号、轮询状态和版本。生产环境必须由独立队列 Worker 执行。视频理解仍需独立适配器。

`final_video_service.py` 使用独立的 `assemble_final_video` 配置步骤。默认 `mock_provider` 只验证流程；启用 `ffmpeg_concat` 后，Worker 会下载冻结顺序的 HTTPS 视频片段并重新编码合并。最终 MP4 由 `final_video_delivery` 交付：`local` 通过受控 API 下载，`s3` 流式上传至 S3/MinIO 并返回稳定 HTTPS 地址。业务服务不应直接拼接存储路径。

`OpenAICompatibleTranscriptionProvider` 已用于独立的 `transcribe_reference_audio` 节点：先通过 `video_audio_service.py` 提取有限时长 MP3，再调用标准 `audio/transcriptions`。转写原文只在当前 Worker 内存中交给后续视觉综合分析，绝不写入步骤输出。`OpenAICompatibleVisionAnalysisProvider` 则用于 `analyze_reference_mechanisms` 节点：通过 `video_frame_service.py` 均匀抽帧，以 OpenAI 兼容多模态消息调用视觉模型，并只保存受契约限制的抽象机制结果。两个真实节点都要求 Worker 部署 FFmpeg；画面抽帧还需 FFprobe。当前源视频为本地存储时，Worker 还必须挂载同一素材目录。

`workflow_service.py` 在读取运行详情时会检查 `WORKFLOW_STALE_AFTER_SECONDS`：超过安全时间仍为 `RUNNING` 的任务会被标记为失败，并记录给操作者的排查说明。它不会自动重放模型请求；对第三方中转站而言，网络超时不代表请求未被处理，自动重放会造成重复扣费或重复素材。确认后仍使用原有重试接口。

`model_profile_service.py` 的预检只检查部署依赖，不提交任何生成任务。OpenAI 兼容模型会尝试只读 `/models` 目录接口；通用异步视频没有统一的零成本探针，预检只验证适配器、参数和密钥，再要求用 1 组镜头做真实小样本验收。
