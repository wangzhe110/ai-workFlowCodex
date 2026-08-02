"""数据库连接和事务生命周期。"""

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings


class Base(DeclarativeBase):
    """所有数据库实体的共同基类。"""


# SQLite 仅用于零配置本地演示；生产环境通过 DATABASE_URL 使用 PostgreSQL。
engine_options: dict = {"pool_pre_ping": True}
if settings.database_url.startswith("sqlite"):
    engine_options["connect_args"] = {"check_same_thread": False}

engine = create_engine(settings.database_url, **engine_options)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """为单个 HTTP 请求提供 Session，并保证请求结束后释放连接。"""

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_database() -> None:
    """初始化开发数据库或验证生产迁移模式。

    本地 ``auto`` 模式保留零配置体验；生产 ``migrate`` 模式严禁 API 进程自行改
    表，必须由发布流程运行 ``alembic upgrade head``。这样多实例滚动发布时每台
    服务不会争抢 DDL 锁，也能准确追踪数据库版本。
    """

    from app.models import entities  # noqa: F401 让 SQLAlchemy 注册全部实体

    # SQLite 需要先存在父目录。此逻辑只服务于零配置开发模式；PostgreSQL 的
    # 连接目录和权限必须由部署基础设施管理，不能由应用进程擅自创建。
    database_url = make_url(settings.database_url)
    if database_url.get_backend_name() == "sqlite" and database_url.database:
        Path(database_url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    if settings.database_schema_mode == "auto":
        Base.metadata.create_all(bind=engine)
    elif settings.database_schema_mode != "migrate":
        raise RuntimeError("DATABASE_SCHEMA_MODE 仅支持 auto 或 migrate")

    # 默认模型配置同样是可审计数据：本地建表或生产迁移完成后首次启动写入模拟
    # 配置，后续由配置中心新增版本并切换，历史工作流仍保留原来的快照。
    from app.services.model_profile_service import ensure_default_profiles

    with SessionLocal() as db:
        ensure_default_profiles(db)
