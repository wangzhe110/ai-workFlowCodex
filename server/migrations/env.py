"""Alembic 迁移环境：只从服务端运行配置读取数据库地址。"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

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


def _sqlite_pragma_value(connection: Connection, pragma: str) -> int:
    """读取 SQLite PRAGMA，并结束仅由该读取产生的 SQLAlchemy 隐式事务。"""

    value = int(connection.exec_driver_sql(f"PRAGMA {pragma}").scalar_one())
    # SQLAlchemy 2 会把 PRAGMA 标记为隐式事务；SQLite 只允许在事务外切换
    # ``foreign_keys``，因此每次读取后都显式结束这个空事务。
    if connection.in_transaction():
        connection.commit()
    return value


def _set_sqlite_foreign_keys(connection: Connection, *, enabled: bool) -> None:
    """在无事务边界安全切换迁移连接的 SQLite 外键开关。"""

    if connection.in_transaction():
        raise RuntimeError("SQLite Alembic 外键切换必须发生在迁移事务开始前或结束后")
    expected = 1 if enabled else 0
    connection.exec_driver_sql(f"PRAGMA foreign_keys={expected}")
    if connection.in_transaction():
        connection.commit()
    actual = _sqlite_pragma_value(connection, "foreign_keys")
    if actual != expected:
        raise RuntimeError(f"SQLite Alembic 无法将 foreign_keys 切换为 {expected}")


def _run_sqlite_online_migrations(connection: Connection) -> None:
    """为 batch 重建临时关闭 SQLite 外键，随后校验并无条件恢复。

    ``0011`` 需要重建被 ``product_asset_versions`` 引用的表。SQLite 在外键开启时
    不允许删除旧表；这个临时策略严格局限于 Alembic 的同一在线连接，绝不影响 API/
    Worker 的 ORM 连接。调用方不得传入已经开启事务的连接，否则 PRAGMA 会被 SQLite
    静默忽略而留下不可靠的迁移结果。
    """

    if connection.in_transaction():
        raise RuntimeError("SQLite Alembic 在线迁移不能使用已开启事务的外部连接")

    was_enabled = _sqlite_pragma_value(connection, "foreign_keys")
    # 用于诊断和集成测试：这个值来自执行 Alembic 的同一 Connection，而非另一个
    # SQLite 连接。它不参与任何业务逻辑。
    connection.info["lemonflow_alembic_foreign_keys_before_disable"] = was_enabled
    try:
        _set_sqlite_foreign_keys(connection, enabled=False)
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()

        # 迁移 DDL 完成后先结束 Alembic/SQLAlchemy 事务，再做最终关系检查；这样
        # ``foreign_key_check`` 观察到的是最终已落库结构和数据。
        if connection.in_transaction():
            connection.commit()
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if connection.in_transaction():
            connection.commit()
        if violations:
            details = "; ".join(
                f"table={row[0]}, rowid={row[1]}, parent={row[2]}, fk={row[3]}" for row in violations
            )
            raise RuntimeError(f"SQLite Alembic 迁移后 foreign_key_check 失败：{details}")
    finally:
        # 出错时先结束可能仍处于活动状态的迁移事务，才能让恢复 PRAGMA 生效。绝不
        # 删除 ``_alembic_tmp_%`` 表来掩盖失败；成功迁移自然不会留下临时表。
        if connection.in_transaction():
            connection.rollback()
        _set_sqlite_foreign_keys(connection, enabled=True)


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
            if connection.dialect.name == "sqlite":
                _run_sqlite_online_migrations(connection)
            else:
                context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
                with context.begin_transaction():
                    context.run_migrations()
    else:
        if connectable.dialect.name == "sqlite":
            _run_sqlite_online_migrations(connectable)
        else:
            context.configure(connection=connectable, target_metadata=target_metadata, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
