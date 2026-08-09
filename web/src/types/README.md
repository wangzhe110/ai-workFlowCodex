# 前端数据模型模块

这里定义与 `server/app/schemas/contracts.py` 对齐的平台稳定类型，不使用任何供应商的原始响应结构。

`ProductionState`、`ReferenceAnalysis`、`StoryProposalV1`、角色/场景/关键帧、`VideoClip` 和 `FinalVideo` 描述 V1 唯一主链路的版本化结果。`VideoClip` 必须包含所属镜头、版本、审核状态、当前采用关系和脱敏供应商任务号；`FinalVideo` 必须包含创建时冻结的片段 ID 列表。

`ModelSlot`、`ModelProfile`、`PromptTemplate`、`ModelInvocation` 和质量类型只包含非敏感配置、快照摘要和统计。真实 Key、内部存储路径、供应商完整错误与原始媒体不得进入 TypeScript 状态。

Phase 4 的 `CharacterAsset`、`SceneAsset` 与对应 `Version` 类型表示跨项目资产的不可变版本；`DirectorPlanV1` / `DirectorShot` 表示能被图片、视频、声音步骤直接消费的导演结构化输出。二者都必须与后端契约同步修改。

新增工作流字段时，先更新后端契约和本目录类型，再修改 API、Store 与页面，避免隐式字段依赖。
