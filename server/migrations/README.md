# 数据库迁移模块

本目录使用 Alembic 管理生产数据库。生产 API 与 Worker 不执行建表/改表；每次发布先备份，再执行：

```bash
cd server
DATABASE_SCHEMA_MODE=migrate alembic upgrade head
```

迁移成功后再启动 API 和 RQ Worker。

## 当前迁移链

- `0001_initial_schema`：旧项目基线。
- `0002_model_evaluations`：早期模型评测记录。
- `0003_v1_production_foundation`：V1 Workflow、审核、资产引用、模型槽位、Prompt 与调用审计。
- `0004_v1_legacy_backfill`：历史 Workflow/模型兼容数据回填。
- `0005_v1_asset_ownership_and_versions`：角色/场景归属与多版本锁图指针。
- `0006_v1_quality_review_metrics`：模型质量人工评分、采用率等指标。
- `0007_v1_production_integrity`：视频当前采用指针、冻结快照补全、任务/调用幂等键、镜头子任务供应商任务号和数据库唯一约束。
- `0008_v1_model_profile_editing`：模型配置版本的 `DRAFT` / `ACTIVE` / `HISTORICAL` 生命周期状态，支持安全编辑与复制新版本。

## 新增迁移规则

1. 先修改 `app/models/entities.py` 并添加中文说明。
2. 新建迁移文件，禁止改写已发布版本。
3. 在隔离 PostgreSQL 上执行 `alembic upgrade head` 和回归测试。
4. 审查索引、唯一约束、默认值和旧数据回填；任何不可逆数据变更先备份。

早期迁移对已有列采用兼容检查，支持空数据库和已升级旧版本继续到 `head`。结构以实际迁移文件为准。
