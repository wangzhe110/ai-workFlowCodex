"""数据库连接和事务生命周期。"""

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
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

# SQLite 默认关闭外键，导致本地测试无法验证生产依赖的 CASCADE / SET NULL 语义。
# 连接级启用后，开发 SQLite 与 PostgreSQL 的 Commerce 删除策略保持一致。
if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _) -> None:
        """让 SQLite 对每条新连接实际执行外键删除策略。"""

        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

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
    from app.services.commerce_configuration_service import ensure_commerce_foundation
    from app.services.commerce_workflow_preset_service import ensure_commerce_workflow_preset_foundation
    from app.services.model_profile_service import ensure_default_profiles
    from app.services.prompt_template_service import ensure_prompt_template_foundation
    from app.services.v1_configuration_service import ensure_v1_foundation

    with SessionLocal() as db:
        ensure_default_profiles(db)
        # 两份工作流定义均以自身唯一业务键幂等创建。它们只补齐缺失的定义和配置，
        # 不为旧项目创建 StoryRun，也不会切换任何项目的既有工作流。
        ensure_v1_foundation(db)
        ensure_commerce_foundation(db)
        # Commerce 预设只写入三份缺失的初始 Published 版本；它们不替换既有活动
        # Profile/Prompt，也不会为历史 StoryRun 回填或改写配置。
        ensure_commerce_workflow_preset_foundation(db)
        # 系统 Prompt 与项目内 video_prompt_versions 是两类数据。这里只幂等补齐
        # 未出现过的系统初始版本，绝不覆盖人工后来激活的版本或历史运行快照。
        ensure_prompt_template_foundation(db)
        db.commit()
