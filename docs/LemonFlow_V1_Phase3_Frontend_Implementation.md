# LemonFlow V1 Phase 3：前端生产台改造记录

> 阶段状态：`已完成`  
> 范围：Vue 3 生产台、V1 审核/锁定操作、状态展示；不在浏览器直接调用任何模型。

## 完成内容

- 新项目默认路由 `/projects/{projectId}` 已切换为 `ProductionWorkbenchView.vue`，展示唯一 V1 主流程。
- 原 `ProjectWorkbenchView.vue` 移到 `/projects/{projectId}/legacy`，旧选题、故事、单镜图片等页面只作为历史兼容或辅助实验入口。
- 新生产台包含：
  - 固定阶段进度条；
  - 授权参考视频上传；
  - 人工审核人和备注；
  - 分析结果与创作简报确认/驳回；
  - 多模型故事方案选择；
  - 角色图、场景图、关键帧版本锁定；
  - 视频片段通过/驳回；
  - 对空结果和当前阻塞阶段的中文说明。
- 新增 `production.ts`，集中调用 V1 生产台接口；页面没有直接使用 `fetch`，也不会存储或显示 API Key。
- 新增 V1 TypeScript 契约，分别表达生产状态、审核请求、分析、故事、图片、关键帧、视频、模型槽位和 Prompt 模板，避免沿用旧单镜图片数据结构。

## 重要交互规则

页面不会在前端猜测“下一步”。每次审核动作成功后，都会重新读取后端 `ProductionState`，由后端状态机决定是否进入下一阶段。因此用户无法通过修改页面或连续点击跳过人工审核闸门。

对 `mock://` 等无真实文件的开发结果，页面不会伪装为图片或视频预览；只有 HTTP(S) 或图片 data URL 才会显示为可点击预览。

## 修改文件

- `web/src/views/ProductionWorkbenchView.vue`
- `web/src/api/production.ts`
- `web/src/types/domain.ts`
- `web/src/router/index.ts`
- `web/src/views/ProjectsView.vue`
- `web/src/views/README.md`
- `web/src/api/README.md`
- `web/src/types/README.md`
- `web/src/router/README.md`
- `web/src/README.md`
- `web/README.md`

## 验证结果

```bash
cd web
npm run build
```

通过 `vue-tsc -b` 和 Vite production build。该验证不读取用户模型密钥，也不向第三方模型发起请求。

## 下一阶段

Phase 4 将以 Adapter 方式接入 V1 生成任务：先提供可无密钥验证完整闭环的本地模拟 Adapter，再接入 Gemini/Claude/Banana/Seedance 的正式 Provider Adapter。业务服务只调用模型槽位，实际模型名称、密钥变量名和供应商协议仍放在配置与 Adapter 层。
