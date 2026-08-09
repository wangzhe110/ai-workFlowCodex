# 核心基础模块

这里放置没有业务归属的配置和数据库连接能力。

- `config.py`：从环境变量读取运行配置。本地可读取工程根目录 `.env`，但部署环境变量优先；涵盖数据库、任务模式、上传限制与媒体存储（本地/S3）配置，只提供变量名和安全默认值，不保存真实密钥。
- `database.py`：SQLAlchemy Engine、会话生命周期和建表入口；`DATABASE_SCHEMA_MODE=auto` 仅为开发自动建 SQLite 表，生产 `migrate` 模式必须由 Alembic 在发布阶段升级结构。SQLite 开发/测试连接显式启用外键，确保 `CASCADE`、`RESTRICT` 与 `SET NULL` 删除策略真实生效。

业务服务不能自行创建全局数据库连接；应通过 API 依赖注入或 Worker 专用会话获取 Session，这样请求与后台任务的事务边界清晰。

生产升级脚本位于 `server/migrations/`。发布顺序是：备份 → `alembic upgrade head` →
启动 API → 启动/滚动更新 Worker，避免多个服务实例同时执行 DDL。

`init_database()` 在本地 `auto` 模式建表，在生产 `migrate` 模式只验证配置；两种模式
都会幂等补齐旧 V1 与 Commerce 的工作流定义，但不会创建 StoryRun 或改写项目。
