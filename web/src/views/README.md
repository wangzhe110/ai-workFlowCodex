# 页面模块

页面组合 Store、路由和展示组件；生成、锁定和审核仍由后端状态机决定。

## V1 主要页面

- `ProjectsView.vue`：创建/选择项目。
- `ProductionWorkbenchView.vue`：唯一主生产台。按固定阶段展示分析、故事、角色、场景、分镜、关键帧、镜头视频与成片；只提供合法的审核和生成入口。
- `V1ModelCenterView.vue`：能力槽位、候选模型、人工启用和故事多模型并行；不接收 API Key。
- `V1QualityReportView.vue`：只汇总已有调用/审核的质量与成本数据，不自动切换模型。
- `V1PromptTemplatesView.vue`：系统 Prompt 目录、Draft、受控变量预览、发布、显式启用/回滚与版本正文差异；不编辑项目业务 Prompt。
- `V1ProductionTraceView.vue`：定位项目冻结的 Workflow、模型、Prompt、调用和脱敏供应商任务号。
- `AssetLibraryView.vue`：角色和场景资产中心。制作人只能新建资产或追加新版本；历史版本不可编辑。项目采用资产后仍须返回生产台锁图。

`ProjectWorkbenchView.vue`、`ProjectTopicsView.vue`、`ProjectStoryView.vue`、`ProjectStoryboardView.vue`、`ProjectImagesView.vue`、`ProjectVideosView.vue`、`ModelProfilesView.vue` 和 `CreativeLibraryView.vue` 为历史兼容或辅助工具；它们不得阻挡 V1 生产台。

页面可停止前端等待，但不能把等待超时写为后台失败。视频区需逐镜显示状态、版本和脱敏任务号；用户驳回后只能重做该镜头的新版本。生产台会展示结构化导演方案，并允许在角色/场景锁图阶段将资产中心版本加入为待审核候选。
