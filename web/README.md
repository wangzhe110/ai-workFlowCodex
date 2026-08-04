# 前端模块

本目录是 Vue 3 + TypeScript 单页应用。前端只展示和编辑平台业务数据；它不保存模型 API Key、不调用中转模型 API，也不在浏览器执行视频处理。

## 页面

- `src/views/ProjectsView.vue`：项目列表与新建项目。
- `src/views/ProductionWorkbenchView.vue`：V1 默认生产台，展示唯一主链路、审核备注、分析/故事/资产/视频版本和正式锁定按钮。
- `src/views/V1ProductionTraceView.vue`：查看项目冻结的 Workflow、模型、Prompt、调用和脱敏供应商任务号。
- `src/views/V1QualityReportView.vue`、`V1PromptTemplatesView.vue`：人工比较模型质量/成本与管理 Prompt 版本；均不会自动调用或切换模型。
- `src/views/ProjectWorkbenchView.vue`：旧工作台兼容入口。
- `src/views/ProjectTopicsView.vue`、`ProjectStoryView.vue`、`ProjectStoryboardView.vue`、`ProjectImagesView.vue`、`ProjectVideosView.vue`：历史兼容或辅助实验页面，不是 V1 的强制前置步骤。
- `src/views/ModelProfilesView.vue`：旧流程模型配置兼容页面；V1 模型中心为 `V1ModelCenterView.vue`，前端绝不接收真实 API Key。

## 目录边界

- `src/api/`：唯一的 HTTP 调用入口。
- `src/stores/`：跨组件状态及其加载/提交动作。
- `src/types/`：与后端 OpenAPI 对齐的 TypeScript 契约。
- `src/components/`：不拥有业务请求的可复用展示组件。
- `src/router/`：页面路由和 URL 参数解析。

页面不能直接写 `fetch`、不能存储密钥、不能解析第三方模型返回值；这些职责必须分别放在 API 层和服务端模型适配层。

开发环境下前端默认请求同源 `/api/v1`，由 Vite 转发到 `http://127.0.0.1:8000`；Docker 生产编排中则由 `nginx/default.conf` 转发到 API 容器。因此通常不需要填写 `VITE_API_BASE_URL`。仅在前后端确实跨域部署时才设置它，且该变量只能是公开 API 地址。

本目录的 `.env.example` 只声明可公开的构建变量；它不能包含模型 Key、数据库密码或对象存储凭据。
