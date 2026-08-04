# 数据模型模块

这里定义数据库中需要长期保留、可审计的业务事实。

## 实体说明

- `Project`：一次原创短剧生产项目的容器。
- `MediaAsset`：项目素材元信息；原文件存储在对象存储，不直接写入数据库。
- `WorkflowRun`：一次工作流运行，例如一次“视频分析”。
- `WorkflowStep`：工作流中的可单独重试步骤，保存进度、输入快照、输出和错误。
- `ModelProfile`：一个步骤可选用的模型配置版本；不存储真实 API Key。
- `ModelEvaluation`：人工小样本验收的聚合统计，按模型配置版本记录成本、速度、成功率和质量评分；不保存原视频或模型原始输出。
- `CreativeLibraryItem`：可人工维护的爆点元素和爆款开头模式。
- `TopicCandidate`：由一次选题工作流生成的原创候选，含人工确认状态。
- `StoryPackage`：从已确认选题生成的一版故事大纲、角色卡、场景卡及其人工确认状态。
- `StoryboardPackage`：镜头数量可配置的一版分镜细纲及其确认状态。
- `StoryboardImage`：单镜图片的可追溯版本；视频生成只消费每镜最新成功版本。
- `VideoClip`：一组连续镜头的一版视频片段，冻结采用的图片版本、提示词、供应商任务号与结果地址。
- `FinalVideo`：一版完整成片，冻结按顺序选用的视频片段版本和合成状态；不覆盖旧成片。

## LemonFlow V1 生产实体

旧的 `TopicCandidate`、`StoryPackage`、`StoryboardPackage` 和 `StoryboardImage` 仅服务于历史
兼容或可选工具。新项目的唯一主流程使用以下实体：

- `WorkflowDefinition`：发布后的工作流版本，例如 `LemonFlow_V1`；历史运行也必须能追溯到定义版本。
- `ProjectProductionState`：项目当前所处的生产阶段，以及已锁定分析、已选故事和导演方案的指针。
- `ReferenceAnalysis`：视频脚本结构、爆款开头、爆款元素、场景分析和创作简报；人工锁定后不可修改。
- `StoryGenerationBatch` 与 `StoryProposal`：同一份锁定简报的多模型并行故事候选，以及唯一的人工选择结果。
- `CharacterDefinition`、`SceneDefinition`：从已选故事先行设计的基础资产；锁定图版本由各自的当前指针选择。
- `DirectorPlan`、`ShotPlan`：角色和场景锁定后生成的导演规划；分镜只引用已锁定的基础资产。
- `CharacterReferenceImage`、`SceneReferenceImage`、`ShotKeyframe`：角色图、场景图和关键帧的不可覆盖版本。
- `ShotAssetBinding` 与 `VideoClipAssetBinding`：强制记录分镜和视频实际使用的角色、场景、关键帧版本。
- `ModelSlot` 与 `ModelSlotProfileBinding`：业务能力槽位和可替换模型配置的绑定；故事槽位可并行使用多个模型。
- `PromptTemplate`：带变量 Schema 的 Prompt 版本，生产调用不能直接硬编码 Prompt。
- `ModelInvocation`：一次调用的模型、Prompt、输入快照、用量、成本、耗时和结果引用。
- `ModelQualityEvaluation`：按模型、Prompt、任务和场景汇总的成功率、人工评分与采用率；V1 只提供推荐，不自动换模型。

## 设计原则

任务状态和模型配置必须快照化。日后切换模型后，仍可以知道历史结果使用了什么模型、什么参数、何时失败以及重试了几次。

任何人工锁定的分析、图片、视频或成片内容都不得被后续重试覆盖。重做必须创建新版本；父对象的当前指针可以选择新版，但旧锁定版本永久保留，视频和成片只允许消费经过相应审核闸门的版本。

创作库只保存抽象机制和用户维护的原创表达，不能将参考视频的具体人物、台词或画面当成可复用资产。
