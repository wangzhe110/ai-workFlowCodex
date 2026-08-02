"""V1 初始业务数据结构基线。

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-08-02
"""

from alembic import op

from app.core.database import Base
from app.models import entities  # noqa: F401 注册当前 V1 的全量表定义


revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None

# 初始版本必须固定表名快照，不能随着未来实体类增加而自动把后续版本的表提前建出。
_INITIAL_TABLE_NAMES = (
    "projects",
    "media_assets",
    "model_profiles",
    "creative_library_items",
    "workflow_runs",
    "workflow_steps",
    "topic_candidates",
    "story_packages",
    "storyboard_packages",
    "storyboard_images",
    "video_clips",
    "final_videos",
)


def upgrade() -> None:
    """创建 V1 所有表、索引与约束，作为后续增量迁移的不可变基线。"""

    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables[table_name] for table_name in _INITIAL_TABLE_NAMES],
    )


def downgrade() -> None:
    """仅用于空白测试库回滚；生产回滚前必须先完成备份与数据评估。"""

    Base.metadata.drop_all(bind=op.get_bind())
