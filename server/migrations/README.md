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

`versions/0001_initial_schema.py` 是 V1 基线，固定创建其发布时的表名快照；
`0002_model_evaluations.py` 是首个增量示例，新增模型小样本验收统计表。后续变更必须
新增版本文件，不能让基线迁移随着当前实体自动增加未来表。
