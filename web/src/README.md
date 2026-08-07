# 前端源码模块

这里保存应用启动、全局样式和按职责划分的页面模块。

- `main.ts`：创建 Vue、Pinia、Element Plus 和路由实例。
- `App.vue`：应用壳，只承载全局导航和路由出口。
- `style.css`：V1 统一的基础颜色、排版与可访问焦点样式。

页面的业务状态应放入 `stores/`，接口请求应放入 `api/`；避免在多个页面复制轮询、错误处理或 URL 逻辑。

新项目默认进入 `ProductionWorkbenchView.vue` 的 LemonFlow V1 主链路。人工审核、锁图和视频片段
审核均调用后端状态机；浏览器只能提出审核决定，不能将模型任务伪造为成功。

Vue 3 对应的 Element UI 官方版本为 Element Plus（`element-plus`）。模型中心使用
`el-card`、`el-form`、`el-table`、`el-tag` 与确认弹窗，所有删除权限仍由后端返回。
