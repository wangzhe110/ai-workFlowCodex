# 数据模型模块

本目录定义需要长期保留、可审计的业务事实。新 V1 项目使用生产实体；`TopicCandidate`、`StoryPackage`、`StoryboardPackage`、`StoryboardImage` 等旧实体只保留给历史项目和辅助工具，不能成为 V1 前置条件。

## V1 关键实体

- `WorkflowDefinition` / `WorkflowRun` / `WorkflowStep`：定义、运行与步骤。运行保存 Workflow 版本、完整输入快照、模型与 Prompt 快照、幂等键和步骤任务号。
- `ProjectProductionState`：唯一主链路当前阶段及锁定分析、选中故事、导演方案等当前指针。
- `ReferenceAnalysis`、`StoryGenerationBatch`、`StoryProposal`：视频分析及多模型故事候选；审核后通过指针选定版本。
- `CharacterDefinition` / `CharacterReferenceImage`：角色文字资产与不可覆盖图片版本。
- `SceneDefinition` / `SceneReferenceImage`：场景文字资产与不可覆盖图片版本。
- `DirectorPlan` / `ShotPlan` / `ShotKeyframe`：导演方案、镜头与关键帧。`ShotPlan` 同时保存锁定关键帧和 `selected_video_clip_id`。
- `ShotAssetBinding` / `VideoClipAssetBinding`：明确镜头和视频实际引用的角色图、场景图、关键帧版本。
- `VideoClip` / `FinalVideo`：独立镜头视频版本与冻结片段列表的成片版本。
- `ModelSlot` / `ModelProfile` / `PromptTemplate` / `ModelInvocation` / `ModelQualityEvaluation`：可替换模型、Prompt、调用审计和人工质量统计。

## 不可覆盖规则

锁定、选择或审核不是“修改原记录”，而是创建审核事件并更新父对象当前指针。每个镜头只有 `selected_video_clip_id` 指向当前采用版本；被驳回或过期的片段保留历史，但不会阻挡最终闸门或成片合成。
