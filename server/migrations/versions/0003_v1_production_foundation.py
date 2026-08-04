"""创建 LemonFlow V1 生产链路基础表与兼容列。

Revision ID: 0003_v1_production_foundation
Revises: 0002_model_evaluations
Create Date: 2026-08-03

本迁移只增不删：旧选题、故事包、单镜图片和视频数据继续保留。历史 0001
迁移会从当前 SQLAlchemy 元数据创建初始表，因此本文件对旧表字段采用“存在则跳过”
策略，确保全新数据库与已经运行过 0001/0002 的数据库都能安全升级。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from app.core.database import Base
from app.models import entities  # noqa: F401 注册 V1 新实体到 Base.metadata


revision = "0003_v1_production_foundation"
down_revision = "0002_model_evaluations"
branch_labels = None
depends_on = None


_NEW_TABLE_NAMES = (
    "workflow_definitions",
    "project_production_states",
    "reference_analyses",
    "review_decisions",
    "story_generation_batches",
    "story_proposals",
    "director_plans",
    "character_definitions",
    "scene_definitions",
    "character_reference_images",
    "scene_reference_images",
    "shot_plans",
    "shot_asset_bindings",
    "shot_keyframes",
    "model_slots",
    "model_slot_profile_bindings",
    "prompt_templates",
    "model_invocations",
    "model_quality_evaluations",
    "video_clip_asset_bindings",
)


def _column_names(table_name: str) -> set[str]:
    """读取当前数据库列，兼容从旧基线和全新基线升级两种情况。"""

    return {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    """以跨 SQLite/PostgreSQL 的方式增量增加一个可空列。"""

    if column.name in _column_names(table_name):
        return
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(table_name) as batch:
            batch.add_column(column)
    else:
        op.add_column(table_name, column)


def _foreign_key_names(table_name: str) -> set[str]:
    """读取已存在的命名外键，避免重复创建。"""

    return {foreign_key.get("name") for foreign_key in inspect(op.get_bind()).get_foreign_keys(table_name)}


def _create_foreign_key_if_supported(name: str, source_table: str, referent_table: str, local_column: str, remote_column: str) -> None:
    """为旧表补强外键；SQLite 的重建成本交给服务层前置校验和集成测试承担。"""

    if op.get_bind().dialect.name == "sqlite" or name in _foreign_key_names(source_table):
        return
    op.create_foreign_key(name, source_table, referent_table, [local_column], [remote_column])


def upgrade() -> None:
    """创建 V1 专属表，并以兼容方式扩展旧表。"""

    # 仅创建 V1 新表；初始表仍由 0001 的固定表名清单创建。
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables[table_name] for table_name in _NEW_TABLE_NAMES],
    )

    # Workflow 版本冻结字段。
    _add_column_if_missing("workflow_runs", sa.Column("workflow_definition_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("workflow_runs", sa.Column("workflow_version", sa.String(length=80), nullable=True))
    _add_column_if_missing("workflow_runs", sa.Column("input_snapshot", sa.JSON(), nullable=True))

    # 旧模型配置继续兼容，同时补充 V1 Adapter/模型展示信息。
    _add_column_if_missing("model_profiles", sa.Column("adapter_key", sa.String(length=80), nullable=True))
    _add_column_if_missing("model_profiles", sa.Column("model_version", sa.String(length=160), nullable=True))
    _add_column_if_missing("model_profiles", sa.Column("display_name", sa.String(length=160), nullable=True))

    # 保留旧视频字段，让 V1 新视频逐步采用分镜与锁定资产绑定。
    _add_column_if_missing("video_clips", sa.Column("shot_plan_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("video_clips", sa.Column("model_invocation_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("video_clips", sa.Column("generation_status", sa.String(length=20), nullable=True))
    _add_column_if_missing("video_clips", sa.Column("review_status", sa.String(length=20), nullable=True))
    _add_column_if_missing("video_clips", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    _add_column_if_missing("video_clips", sa.Column("review_note", sa.Text(), nullable=True))
    _add_column_if_missing("video_clips", sa.Column("input_asset_snapshot", sa.JSON(), nullable=True))

    # 新成片不再依赖旧 storyboard_package；历史列继续保留并允许为空。
    _add_column_if_missing("final_videos", sa.Column("director_plan_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("final_videos", sa.Column("workflow_definition_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("final_videos", sa.Column("workflow_version", sa.String(length=80), nullable=True))
    _add_column_if_missing("final_videos", sa.Column("approved_clip_ids", sa.JSON(), nullable=True))
    _add_column_if_missing("final_videos", sa.Column("input_snapshot", sa.JSON(), nullable=True))
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("final_videos") as batch:
            batch.alter_column("storyboard_package_id", existing_type=sa.String(length=36), nullable=True)
    else:
        op.alter_column("final_videos", "storyboard_package_id", existing_type=sa.String(length=36), nullable=True)

    # 新表中的循环引用由 SQLAlchemy use_alter 创建；旧表的 V1 外键在 PostgreSQL
    # 补齐。SQLite 由服务层项目范围校验保证，避免重建大表影响历史数据。
    _create_foreign_key_if_supported(
        "fk_workflow_run_definition", "workflow_runs", "workflow_definitions", "workflow_definition_id", "id"
    )
    _create_foreign_key_if_supported("fk_video_clip_shot_plan", "video_clips", "shot_plans", "shot_plan_id", "id")
    _create_foreign_key_if_supported(
        "fk_video_clip_invocation", "video_clips", "model_invocations", "model_invocation_id", "id"
    )
    _create_foreign_key_if_supported("fk_final_video_director_plan", "final_videos", "director_plans", "director_plan_id", "id")
    _create_foreign_key_if_supported(
        "fk_final_video_definition", "final_videos", "workflow_definitions", "workflow_definition_id", "id"
    )


def downgrade() -> None:
    """仅删除尚未承载生产数据的 V1 结构；生产降级前必须先备份。"""

    # 真实生产禁止直接 downgrade；此顺序仅服务于空白测试库。
    if op.get_bind().dialect.name != "sqlite":
        for name, table in (
            ("fk_final_video_definition", "final_videos"),
            ("fk_final_video_director_plan", "final_videos"),
            ("fk_video_clip_invocation", "video_clips"),
            ("fk_video_clip_shot_plan", "video_clips"),
            ("fk_workflow_run_definition", "workflow_runs"),
        ):
            if name in _foreign_key_names(table):
                op.drop_constraint(name, table, type_="foreignkey")

    for table_name, columns in (
        ("final_videos", ("input_snapshot", "approved_clip_ids", "workflow_version", "workflow_definition_id", "director_plan_id")),
        ("video_clips", ("input_asset_snapshot", "review_note", "reviewed_at", "review_status", "generation_status", "model_invocation_id", "shot_plan_id")),
        ("model_profiles", ("display_name", "model_version", "adapter_key")),
        ("workflow_runs", ("input_snapshot", "workflow_version", "workflow_definition_id")),
    ):
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table(table_name) as batch:
                for column in columns:
                    if column in _column_names(table_name):
                        batch.drop_column(column)
        else:
            for column in columns:
                if column in _column_names(table_name):
                    op.drop_column(table_name, column)

    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables[table_name] for table_name in reversed(_NEW_TABLE_NAMES)],
    )
