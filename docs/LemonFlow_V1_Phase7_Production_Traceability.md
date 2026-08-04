# LemonFlow V1 Phase 7：生产版本追溯

> 阶段状态：`完成`
>
> 目标：让制作负责人能对任意项目回答“这一版结果到底用了什么”，同时不把 Prompt 正文、参考素材或模型原始输出暴露到普通页面。

## 完成内容

- 新增项目级接口：`GET /api/v1/production/projects/{project_id}/model-invocations`。
- 每行返回一次模型调用冻结的：Workflow 键/版本、模型显示名/版本/配置版、Prompt 名称/版本、状态、耗时、预计成本、Token、媒体计量与供应商任务号。
- API 绝不返回 `input_snapshot`、`output_reference`、Prompt 正文或模型输入输出，避免参考视频和内部指令外泄。
- 新增前端页面 `/projects/:projectId/trace`，支持按用途筛选、查看状态、刷新记录，并从 V1 生产台一键进入。
- 新增操作说明 `LemonFlow_V1_生产追溯操作.md`，指导制作人以“项目 + 模型版本 + Prompt 版本 + 任务号”反馈问题。

## 修改文件列表

- 后端：`server/app/services/v1_trace_service.py`、`server/app/api/routes/production.py`、`server/app/schemas/contracts.py`、`server/app/schemas/__init__.py`
- 前端：`web/src/views/V1ProductionTraceView.vue`、`web/src/views/ProductionWorkbenchView.vue`、`web/src/api/production.ts`、`web/src/types/domain.ts`、`web/src/router/index.ts`
- 测试与文档：`server/tests/test_v1_mock_production_closure.py`、各模块 README、本文档与操作说明。

## 数据结构变化

没有新增数据库表或迁移。该阶段只把已有的不可变 `ModelInvocation`、`WorkflowRun`、`ModelSlot` 与 `PromptTemplate` 组合为安全的只读视图。

```text
WorkflowRun (workflow_version)
        +
ModelInvocation (model / prompt snapshot / status / usage)
        ↓
Production trace API / 制作人页面
```

## 测试结果

- 后端：`PYTHONDONTWRITEBYTECODE=1 pytest -q` → `39 passed`
- 前端：`npm run build` → 成功
- 完整 mock V1 闭环测试额外验证追溯接口可返回 Workflow、模型和 Prompt 版本，且不包含原始输入输出字段。

## 下一阶段建议

1. 使用真实 Key 从每个渠道跑一个小样本，检查追溯页中的模型版本、Prompt 版本、任务号和耗时是否和供应商后台一致。
2. 将生产 Worker 切换至 Redis/RQ，并增加任务超时、失败告警和供应商回调处理。
3. 在获得可靠供应商用量后，以实际账单覆盖当前“预计成本”。
