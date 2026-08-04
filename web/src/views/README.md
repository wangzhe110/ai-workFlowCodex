# 页面模块

页面负责组合 Store、路由和展示组件；同一业务流程中的用户动作在页面发起，数据请求仍委托给 Store。

## 页面职责

- `ProjectsView.vue`：选择已有项目或创建新项目。创建项目不自动上传素材，便于用户先填写创作方向。
- `ProductionWorkbenchView.vue`：V1 唯一生产台。展示固定阶段、人工审核备注、分析/故事/资产/视频版本，并只通过正式锁定接口推进状态。
- `ProjectWorkbenchView.vue`：历史兼容工作台；保留旧视频分析入口，不再作为新项目默认页面。
- `CreativeLibraryView.vue`：维护爆点元素与爆款开头等抽象创作资产。
- `ProjectTopicsView.vue`：创建原创选题任务、审核候选并人工确认一个选题。
- `ProjectStoryView.vue`：生成、审阅并确认故事大纲、角色卡和场景卡。
- `ProjectStoryboardView.vue`：设置镜头数量，审阅并确认可生成图片/视频的分镜细纲。
- `ProjectImagesView.vue`：批量生成镜头图片，查看版本并按镜头重试。
- `ProjectVideosView.vue`：按可配置的连续镜头组生成短视频片段、重做单组版本，并按冻结顺序导出完整成片。
- `V1ModelCenterView.vue`：V1 默认模型中心。按生产功能创建候选模型、人工启用/停用、单模型替换和故事多模型并行；不接收真实 API Key。
- `V1QualityReportView.vue`：模型质量与成本报表。制作人手动刷新已有调用/审核的统计快照，比较后仍在模型中心手工切换候选。
- `V1PromptTemplatesView.vue`：Prompt 版本管理。只允许新建草稿、人工启用或归档非生效版本，避免覆盖已用于生产的提示词。
- `V1ProductionTraceView.vue`：项目级模型与版本记录。用于定位某次结果实际使用的 Workflow、模型、Prompt 和供应商任务号，不展示生产素材正文。
- `ModelProfilesView.vue`：旧流程模型配置与评测兼容页面，路由为 `/model-profiles/legacy`，不再作为 V1 主入口。

旧选题、故事、分镜、图片、视频页面继续保留在本目录用于历史项目和辅助实验，但不能阻挡 V1 生产台。所有页面通过 URL 中的 `projectId` 共享同一个项目上下文。
