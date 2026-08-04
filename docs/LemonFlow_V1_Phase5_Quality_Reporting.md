# LemonFlow V1 Phase 5：模型质量与成本评估

> 阶段状态：`完成`
>
> 目标：把 V1 已有的模型调用审计、人工审核和模型槽位配置连接成可追溯的质量报表；只帮助人选择模型，绝不自动改变生产模型。

## 完成内容

- `ReviewDecision` 新增可选 `quality_score`（1 至 10 分）；评分与锁定/选择/通过等采用决定独立保存。
- 新增迁移 `0006_v1_quality_review_metrics`：安全增加审核评分列，并为质量快照增加成本币种列；历史审核不强制补分。
- 新增 `v1_quality_service.py`：按“模型配置版本 + Prompt 模板版本 + 任务类型”聚合终态 `ModelInvocation`，生成不可覆盖的 `ModelQualityEvaluation` 快照。
- 统计内容包括：样本量、成功量/成功率、人工平均评分、已审核采用率、平均时延和平均预计成本。
- 质量报表不运行模型、不提交第三方请求、不改变 `ModelSlotProfileBinding`；模型切换仍仅能在模型中心由人工明确操作。
- 模型中心增加“预计每次生成费用（元，可不填）”。它只作为比较预估值，不能替代真实供应商账单。
- 生产台增加可选质量评分下拉框；确认、选择、锁图、通过或驳回时统一写入审核审计。
- 新增质量报表接口：
  - `GET /api/v1/production/model-quality-evaluations`
  - `POST /api/v1/production/model-quality-evaluations/refresh`
- 新增 Vue 页面 `/model-quality`，显示用途、模型、Prompt、样本、成功率、人工评分、采用率、成本、时延和统计时间。
- 新增 `LemonFlow_V1_模型质量报表操作.md`，供非技术制作人员按正常审核流程使用。

## 修改文件列表

- 数据库与实体：`server/migrations/versions/0006_v1_quality_review_metrics.py`、`server/app/models/entities.py`
- 后端服务与接口：`server/app/services/v1_quality_service.py`、`server/app/services/v1_production_service.py`、`server/app/services/v1_execution_service.py`、`server/app/services/v1_configuration_service.py`、`server/app/api/routes/v1_modeling.py`、`server/app/api/routes/production.py`
- API 契约：`server/app/schemas/contracts.py`、`server/app/schemas/__init__.py`
- 前端：`web/src/views/V1QualityReportView.vue`、`web/src/views/ProductionWorkbenchView.vue`、`web/src/views/V1ModelCenterView.vue`、`web/src/api/production.ts`、`web/src/types/domain.ts`、`web/src/router/index.ts`、`web/src/App.vue`
- 测试与说明：`server/tests/test_v1_quality_reporting.py`、各模块 README、本文档与操作说明。

## 数据结构变化

```text
ReviewDecision
  └─ quality_score (nullable, 1..10)

ModelInvocation + ReviewDecision
  └─ ModelQualityEvaluation (immutable snapshot)
       ├─ model_profile_id
       ├─ prompt_template_id
       ├─ task_type
       ├─ success_rate / average_human_score / adoption_rate
       ├─ average_cost_amount + currency
       └─ average_latency_ms
```

一个快照只统计已终态的调用。没有审核就不显示采用率；没有人工评分就不显示平均评分；没有配置预计成本就不显示平均成本。系统不会把“未审核”误当成“未采用”，也不会把混合币种错误平均。

## 测试结果

- 后端：`PYTHONDONTWRITEBYTECODE=1 pytest -q` → `39 passed`
- 前端：`npm run build` → 成功
- `test_v1_quality_reporting.py` 覆盖评分范围校验、评分进入质量快照、采用率统计与报表只读边界。

## 下一阶段建议

1. 由项目方用真实 Key 按“分析 → 故事 → 图片 → Seedance”逐渠道跑 1 个小样本，核对实际模型名、返回结构、图片 HTTPS 交付和账单。
2. 为真实 Adapter 增加供应商原生用量解析，在可获得可靠用量时覆盖当前“预计成本”。
3. 真实生产部署前，将进程内 Worker 切换到 Redis/RQ，补充超时、回调、重试与运行监控。
