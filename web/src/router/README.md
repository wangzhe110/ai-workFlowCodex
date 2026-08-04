# 路由模块

路由将 URL 映射为页面，不保存页面数据。

- `/`：项目列表和新建项目。
- `/projects/:projectId`：LemonFlow V1 唯一生产工作台，包含审核和锁定节点。
- `/projects/:projectId/legacy`：旧工作台兼容入口。
- `/projects/:projectId/topics`、`story`、`storyboard`、`images`、`videos`：历史兼容或辅助实验页面，不是 V1 的强制前置步骤。
- `/model-profiles`：LemonFlow V1 模型中心，按能力槽位管理候选模型与人工启用状态。
- `/model-profiles/legacy`：旧流程模型配置兼容页面。

以后加入登录与权限时，在这里补充路由守卫；不要在单个页面中分散判断登录状态。
