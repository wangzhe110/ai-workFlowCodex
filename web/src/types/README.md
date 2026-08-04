# 前端数据模型模块

类型与后端 `server/app/schemas/contracts.py` 的响应契约对应。这里定义的是平台自己的稳定模型，不是任一中转平台的响应结构。

新增工作流步骤时，先更新后端契约与本目录类型，再修改页面和组件，避免隐式字段依赖。

`VideoClip` 是视频供应商无关的片段模型：保留分组范围、图片版本、提示词和结果地址，前端不依赖任何中转站的原始响应。

`FinalVideo` 表示一版已经冻结片段顺序的完整成片；下载地址只在服务器真实生成并保存 MP4 后出现，模拟结果不会冒充可下载文件。

`ModelProfile` 仅表示非敏感的模型版本和适配状态；密钥以服务器环境变量引用，不会进入 TypeScript 状态。`ModelProfilePreflight` 只返回启用前的检查结论和中文说明，仍不包含真实密钥。`ModelEvaluation` 则保存人工小样本的汇总指标，不携带原始视频、提示词或模型输出。

`ProductionState`、`ReferenceAnalysis`、`StoryProposalV1`、角色/场景/关键帧/视频片段 V1 类型描述
唯一生产链路的可审核版本；`ModelSlot` 与 `PromptTemplate` 把具体模型和生产 Prompt 从业务页面中分离。
