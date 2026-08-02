"""Alembic 迁移环境：只从服务端运行配置读取数据库地址。"""

from logging.config import fileConfig

from alembic import context

from app.core.config import settings
from app.core.database import Base
from app.models import entities  # noqa: F401 注册全部 SQLAlchemy 表到 Base.metadata


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 迁移生成/执行都使用当前实体元数据。不要从 HTTP 路由或前端导入模型。
target_metadata = Base.metadata
config.set_main_option("sqlalchemy.url", settings.database_url)


def run_migrations_offline() -> None:
    """生成离线 SQL 时不建立数据库连接。"""

    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线执行迁移，使用 Alembic 管理事务而非应用请求 Session。"""

    connectable = context.config.attributes.get("connection")
    if connectable is None:
        from sqlalchemy import engine_from_config, pool

        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
    if hasattr(connectable, "connect"):
        with connectable.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
    else:
        context.configure(connection=connectable, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
