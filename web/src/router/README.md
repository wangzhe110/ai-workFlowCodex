# 路由模块

路由只映射 URL 到页面，不保存业务数据。

- `/`：项目列表与新建项目。
- `/projects/:projectId`：LemonFlow V1 唯一生产台。
- `/projects/:projectId/trace`：该项目的 Workflow、模型、Prompt 和供应商任务号追溯。
- `/model-profiles`：V1 模型中心。
- `/model-quality`：质量/成本比较报表。
- `/prompt-templates`：Prompt 版本管理。
- `/creative-library`：可选的抽象创作资产库。
- `/projects/:projectId/legacy`、`topics`、`story`、`storyboard`、`images`、`videos` 与 `/model-profiles/legacy`：历史兼容或辅助实验入口，不能成为新项目的 V1 前置步骤。

后续添加登录时在此处增加路由守卫；不要把权限判断分散到页面。
