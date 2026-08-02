# 路由模块

路由将 URL 映射为页面，不保存页面数据。

- `/`：项目列表和新建项目。
- `/projects/:projectId`：单个项目的生产工作台。
- `/projects/:projectId/topics`、`story`、`storyboard`、`images`、`videos`：按生产顺序进入选题、故事、分镜、图片与视频片段页面。
- `/model-profiles`：全局模型配置中心，管理各步骤的可替换供应商版本。

以后加入登录与权限时，在这里补充路由守卫；不要在单个页面中分散判断登录状态。
