# 页面模块

页面负责组合 Store、路由和展示组件；同一业务流程中的用户动作在页面发起，数据请求仍委托给 Store。

## 页面职责

- `ProjectsView.vue`：选择已有项目或创建新项目。创建项目不自动上传素材，便于用户先填写创作方向。
- `ProjectWorkbenchView.vue`：V1 的工作台。负责上传授权参考视频、启动分析、管理轮询生命周期并展示结果。
- `CreativeLibraryView.vue`：维护爆点元素与爆款开头等抽象创作资产。
- `ProjectTopicsView.vue`：创建原创选题任务、审核候选并人工确认一个选题。
- `ProjectStoryView.vue`：生成、审阅并确认故事大纲、角色卡和场景卡。
- `ProjectStoryboardView.vue`：设置镜头数量，审阅并确认可生成图片/视频的分镜细纲。
- `ProjectImagesView.vue`：批量生成镜头图片，查看版本并按镜头重试。
- `ProjectVideosView.vue`：按可配置的连续镜头组生成短视频片段、重做单组版本，并按冻结顺序导出完整成片。
- `ModelProfilesView.vue`：新增、无扣费预检、录入/对比小样本评测、审阅和启用各工作流步骤的非敏感模型配置版本。

未来的选题、故事、分镜、图片、视频页面将继续放入本目录，并通过 URL 中的 `projectId` 共享同一个项目上下文。
