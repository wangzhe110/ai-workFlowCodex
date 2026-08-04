# LemonFlow V1 Phase 6：Prompt 模板版本管理

> 阶段状态：`完成`
>
> 目标：让非技术制作负责人可以安全维护 Prompt 模板，而不覆盖历史生产输入，也不让多个生效版本造成新任务行为不确定。

## 完成内容

- 新增前端页面 `/prompt-templates`：可按生产用途查看 Prompt 历史、创建新草稿、人工启用和归档草稿。
- 模板页面只允许“新建版本”，没有编辑旧版本的入口；历史模型调用已冻结的 Prompt 内容不受影响。
- 每个任务类型现在最多只有一个 `ACTIVE` Prompt。启用任意新版本时，系统自动归档该任务的原生效版本，不再按模板名称猜测生产 Prompt。
- V1 初始化只会为“尚无任何生效 Prompt”的任务补建默认模板，绝不会在制作人激活自定义版本后悄悄补回默认版本。
- 禁止直接归档当前生效 Prompt；必须先启用同一任务的另一版，避免新任务找不到可用模板。
- 页面默认隐藏变量 Schema 的 JSON 输入；普通制作人保持默认值即可，只有开发人员明确需要时才使用高级区域。
- 增加小白操作文档 `LemonFlow_V1_Prompt模板操作.md`。

## 修改文件列表

- 后端规则：`server/app/services/v1_configuration_service.py`、`server/app/api/routes/v1_modeling.py`
- 前端：`web/src/views/V1PromptTemplatesView.vue`、`web/src/api/production.ts`、`web/src/router/index.ts`、`web/src/App.vue`、`web/src/views/V1ModelCenterView.vue`
- 测试与文档：`server/tests/test_v1_production_state_machine.py`、`web/src/views/README.md`、`docs/LemonFlow_V1_Prompt模板操作.md`、本文档。

## 状态与数据变化

数据库表没有新增或修改。`PromptTemplate` 原有状态仍是：

```text
DRAFT → ACTIVE → ARCHIVED
```

新增的业务约束是：同一 `task_type` 在任何时刻只能存在一个 `ACTIVE`。正在执行的任务不读取当前 ACTIVE，而是读取开始时写入 `ModelInvocation.prompt_snapshot` 的不可变快照。

## 测试结果

- 后端：`PYTHONDONTWRITEBYTECODE=1 pytest -q` → `39 passed`
- 前端：`npm run build` → 成功
- 新增/扩展状态机测试，覆盖激活新版后同一任务只保留一个 ACTIVE，以及拒绝归档当前生效版本。

## 下一阶段建议

1. 以真实渠道做小样本测试，并在质量报表中对比模型版本和 Prompt 版本。
2. 接入可信的供应商用量数据后，以真实成本覆盖当前人工填写的预计成本。
3. 生产部署时从进程内 Worker 切换至 Redis/RQ，并增加任务监控与告警。
