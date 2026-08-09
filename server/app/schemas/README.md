# 接口契约模块

本目录定义 API 输入输出，不直接返回 SQLAlchemy 实体或供应商原始响应。

`contracts.py` 包含项目、媒体、工作流、生成运行和审核操作的稳定字段。V1 契约覆盖：

- `ProductionStateResponse`：唯一主链路阶段、当前锁定/选择指针和阻塞原因；
- `ReferenceAnalysis`、`StoryProposalV1`、角色/场景定义、图片资产、导演分镜和关键帧的版本化响应；
- `VideoClip`：镜头、版本、当前采用关系、审核状态和脱敏供应商任务号；
- `FinalVideoResponse`：冻结片段 ID、版本、状态及真实成片可用后的下载地址；
- `ModelSlot`、`ModelProfile`、`PromptTemplate`、`ModelInvocation` 和质量报告的非敏感响应。
- `CharacterAsset*` / `SceneAsset*`：资产中心主体和不可变版本；资产版本仅以创建契约追加。
- `DirectorPlanV1Response` / `DirectorShotResponse`：结构化导演镜头，含资产版本、动作、情绪、机位、运镜、光线和图片/视频/声音 Prompt。

模型 Key、存储内部路径、第三方原始错误、完整 Prompt 输入和素材正文不会出现在浏览器契约中。旧流程契约仍可存在以兼容历史数据，但不能改变 V1 生产台的前置条件。
