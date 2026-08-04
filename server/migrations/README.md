# 数据库迁移模块

本目录使用 Alembic 管理生产数据库结构。**生产 API 和 Worker 不执行建表或改表**，
发布流程必须先运行：

```bash
cd server
DATABASE_SCHEMA_MODE=migrate alembic upgrade head
```

然后再启动 API 与 Redis/RQ Worker。首次部署、升级和回滚都应先备份 PostgreSQL。

## 新增数据库字段/表的流程

1. 修改 `app/models/entities.py`，并为模型添加中文说明。
2. 在使用 PostgreSQL 的隔离环境运行：

   ```bash
   alembic revision --autogenerate -m "说明本次结构变更"
   ```

3. 审查生成脚本：尤其确认索引、非空字段、默认值和数据回填逻辑正确。
4. 先在备份/预发布数据库执行 `alembic upgrade head` 并回归测试。
5. 发布时先迁移、再部署 API/Worker；不要让线上服务进程执行 `Base.metadata.create_all()`。

`versions/0001_initial_schema.py` 是旧工作流基线，`0002_model_evaluations.py` 新增模型小样本
验收统计。`0003_v1_production_foundation.py` 创建 LemonFlow V1 的审核、资产引用、模型槽位、
Prompt 与调用审计结构；`0004_v1_legacy_backfill.py` 回填历史 Workflow 和模型兼容数据；
`0005_v1_asset_ownership_and_versions.py` 将角色/场景主归属修正为已选故事，并允许多个已锁定
资产版本并存、由当前指针选择本轮采用版本。

由于早期 `0001` 以固定表名从 ORM 元数据创建初始表，`0003` 对旧表使用“列已存在则跳过”的
兼容迁移策略，保证全新数据库与已运行过 `0002` 的数据库都能升级。后续变更仍必须新增版本文件，
不得改写已经发布的 V1 迁移逻辑。
