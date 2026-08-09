# 数据模型模块

本目录定义需要长期保留、可审计的业务事实。新 V1 项目使用生产实体；`TopicCandidate`、`StoryPackage`、`StoryboardPackage`、`StoryboardImage` 等旧实体只保留给历史项目和辅助工具，不能成为 V1 前置条件。

## V1 关键实体

- `WorkflowDefinition` / `WorkflowRun` / `WorkflowStep`：定义、运行与步骤。运行保存 Workflow 版本、完整输入快照、模型与 Prompt 快照、幂等键和步骤任务号。
- `ProjectProductionState`：唯一主链路当前阶段及锁定分析、选中故事、导演方案等当前指针。
- `ReferenceAnalysis`、`StoryGenerationBatch`、`StoryProposal`：视频分析及多模型故事候选；审核后通过指针选定版本。
- `AssetLibrary`、`CharacterAsset` / `CharacterAssetVersion`、`SceneAsset` / `SceneAssetVersion`：跨项目角色/场景资产中心。版本只追加，`reference_images` 可保存角色正侧全身表情或场景多角度图。
- `CharacterDefinition` / `CharacterReferenceImage` 与 `SceneDefinition` / `SceneReferenceImage`：项目内的故事语义和锁图版本；图片可关联资产中心版本。
- `ProjectCharacterAssetReference` / `ProjectSceneAssetReference`：项目采用的资产中心版本指针。改选只切换引用，绝不修改资产版本。
- `DirectorPlan` / `ShotPlan` / `ShotKeyframe`：导演方案、镜头与关键帧。`ShotPlan` 保存动作、情绪、镜头类型、运镜、光线和图片/视频/声音 Prompt，以及锁定关键帧与 `selected_video_clip_id`。
- `ShotAssetBinding` / `VideoClipAssetBinding`：明确镜头和视频实际引用的项目锁图、关键帧和资产中心版本。
- `VideoClip` / `FinalVideo`：独立镜头视频版本与冻结片段列表的成片版本。
- `ModelSlot` / `ModelProfile` / `PromptTemplate` / `ModelInvocation` / `ModelQualityEvaluation`：可替换模型、Prompt、调用审计和人工质量统计。`ModelProfile` 的 `DRAFT` / `ACTIVE` / `HISTORICAL` 只描述版本生命周期；是否可编辑以“是否已存在 `ModelInvocation`”为准。

## 带货短剧（Commerce）领域基础

Commerce 与 LemonFlow V1 同库并存，**不读取也不改写** `ProjectProductionState`。它的
核心单位是一个选题的一次独立 `StoryRun`，因此同一项目的不同选题、同一选题的重跑
和不同产品版本都互不影响。

- `ScriptAsset` / `ScriptAnalysisVersion`：上传 `MediaAsset` 对应的脚本逻辑资产，以及
  时间轴转写、节拍、冲突、情绪曲线、章节和植入候选等不可覆盖分析版本。
- `ProductAsset` / `ProductAnalysisVersion` / `ProductAssetVersion`：共享产品主体、原始
  分析版本与人工确认的生产版本。`ProductAnalysisVersion` 显式保存产品识别、包装 OCR、
  候选多角度参考图、外观/卖点/痛点/使用场景候选，`raw_analysis` 只保留完整原始结果。
  生产版本只追加，包含外观、卖点、痛点、使用场景、OCR 和多角度参考图；它不属于项目，
  项目删除不会影响共享产品。来源媒体删除时，分析版本的来源外键会置空。
- `ProjectProductSelection` / `StoryRun` / `StoryRunState`：项目选择具体产品版本；每个
  StoryRun 冻结该版本，并以 `run_number` 区分同一选题的重跑，维护自己的阶段与状态机。
- `StoryOutlineVersion` / `ChapterPlan` / `SceneMappingVersion`：追加式大纲、章节顺序和
  章节/片段/既有场景资产版本的映射快照。
- `VideoSegmentPlan` / `SubShotPlan` / `DialogueLine`：最终 MP4 片段（4,000–15,000ms）、
  片段内子镜头和可逐条查询的对白。子镜头的基础时间由数据库约束校验，跨表的父片段
  时长由 `commerce_domain_service` 校验。
- `ProductPlacementPlan`：绑定 StoryRun 和冻结的 `ProductAssetVersion`，用枚举保存植入
  方式与强度，并定位章节、片段或子镜头。
- `RenderBatch`：批量渲染的进度、成本与参数快照；只关联并复用既有 `WorkflowRun` /
  `WorkflowStep`，不创建第二套任务调度系统。

## 不可覆盖规则

锁定、选择或审核不是“修改原记录”，而是创建审核事件并更新父对象当前指针。每个镜头只有 `selected_video_clip_id` 指向当前采用版本；被驳回或过期的片段保留历史，但不会阻挡最终闸门或成片合成。

Commerce 的产品、脚本分析和大纲版本同样遵循追加原则：需要修订时创建下一版本，不
覆盖已被项目、StoryRun 或批次快照引用的内容。

数据库负责版本号/计划序号从 1 开始、片段和对白的绝对时长、植入目标三选一、批次
成本和任务计数等单表规则。跨项目、跨 StoryRun、冻结版本和相对时长关系由
`commerce_domain_service.py` 统一校验，不能在未来路由中各自实现。
