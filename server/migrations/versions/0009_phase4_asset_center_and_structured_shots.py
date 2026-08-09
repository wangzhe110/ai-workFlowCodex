"""新增可跨项目复用的资产中心和结构化导演分镜字段。

Revision ID: 0009_phase4_asset_center_and_structured_shots
Revises: 0008_v1_model_profile_editing
Create Date: 2026-08-07

本迁移只新增表、字段和索引，不删除或改写任何既有项目、锁图、分镜、视频片段或
Workflow 快照。已有锁图会在下一次进入导演阶段时由服务层惰性补齐资产中心版本，
避免迁移期间擅自改变历史生产结果。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0009_phase4_asset_center_and_structured_shots"
down_revision = "0008_v1_model_profile_editing"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}


def _indexes(table_name: str) -> set[str]:
    return {item.get("name") for item in inspect(op.get_bind()).get_indexes(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name in _columns(table_name):
        return
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(table_name) as batch:
            batch.add_column(column)
    else:
        op.add_column(table_name, column)


def _create_asset_tables() -> None:
    """创建新资产中心表；此处没有历史数据迁移或删除操作。"""

    if "asset_libraries" not in _tables():
        op.create_table(
            "asset_libraries",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("kind", sa.Enum("CHARACTER", "SCENE", name="assetlibrarykind"), nullable=False),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="ACTIVE"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("kind", "name", name="uq_asset_library_kind_name"),
        )
        op.create_index("ix_asset_libraries_kind", "asset_libraries", ["kind"])
    if "character_assets" not in _tables():
        op.create_table(
            "character_assets",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("library_id", sa.String(length=36), sa.ForeignKey("asset_libraries.id"), nullable=False),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="ACTIVE"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("library_id", "name", name="uq_character_asset_library_name"),
        )
        op.create_index("ix_character_assets_library_id", "character_assets", ["library_id"])
    if "character_asset_versions" not in _tables():
        op.create_table(
            "character_asset_versions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("character_asset_id", sa.String(length=36), sa.ForeignKey("character_assets.id"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("age", sa.String(length=120), nullable=True),
            sa.Column("gender", sa.String(length=40), nullable=True),
            sa.Column("personality", sa.Text(), nullable=True),
            sa.Column("style", sa.Text(), nullable=True),
            sa.Column("appearance", sa.Text(), nullable=True),
            sa.Column("costume", sa.Text(), nullable=True),
            sa.Column("reference_images", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("character_asset_id", "version", name="uq_character_asset_version"),
        )
        op.create_index("ix_character_asset_versions_character_asset_id", "character_asset_versions", ["character_asset_id"])
    if "scene_assets" not in _tables():
        op.create_table(
            "scene_assets",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("library_id", sa.String(length=36), sa.ForeignKey("asset_libraries.id"), nullable=False),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="ACTIVE"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("library_id", "name", name="uq_scene_asset_library_name"),
        )
        op.create_index("ix_scene_assets_library_id", "scene_assets", ["library_id"])
    if "scene_asset_versions" not in _tables():
        op.create_table(
            "scene_asset_versions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("scene_asset_id", sa.String(length=36), sa.ForeignKey("scene_assets.id"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("style", sa.Text(), nullable=True),
            sa.Column("weather", sa.String(length=120), nullable=True),
            sa.Column("time_of_day", sa.String(length=120), nullable=True),
            sa.Column("location", sa.Text(), nullable=True),
            sa.Column("environment", sa.Text(), nullable=True),
            sa.Column("mood", sa.Text(), nullable=True),
            sa.Column("reference_images", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("scene_asset_id", "version", name="uq_scene_asset_version"),
        )
        op.create_index("ix_scene_asset_versions_scene_asset_id", "scene_asset_versions", ["scene_asset_id"])


def _create_project_reference_tables() -> None:
    if "project_character_asset_references" not in _tables():
        op.create_table(
            "project_character_asset_references",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("character_definition_id", sa.String(length=36), sa.ForeignKey("character_definitions.id"), nullable=False),
            sa.Column("character_asset_id", sa.String(length=36), sa.ForeignKey("character_assets.id"), nullable=False),
            sa.Column("character_asset_version_id", sa.String(length=36), sa.ForeignKey("character_asset_versions.id"), nullable=False),
            sa.Column("source_reference_image_id", sa.String(length=36), sa.ForeignKey("character_reference_images.id"), nullable=True),
            sa.Column("is_selected", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("character_definition_id", "character_asset_version_id", name="uq_project_character_asset_version_reference"),
        )
        op.create_index("ix_project_character_asset_references_project_id", "project_character_asset_references", ["project_id"])
        op.create_index("ix_project_character_asset_references_character_definition_id", "project_character_asset_references", ["character_definition_id"])
        op.create_index("ix_project_character_asset_references_character_asset_id", "project_character_asset_references", ["character_asset_id"])
        op.create_index("ix_project_character_asset_references_character_asset_version_id", "project_character_asset_references", ["character_asset_version_id"])
        op.create_index("ix_project_character_asset_references_source_reference_image_id", "project_character_asset_references", ["source_reference_image_id"])
        op.create_index(
            "uq_selected_project_character_asset", "project_character_asset_references", ["character_definition_id"], unique=True,
            postgresql_where=sa.text("is_selected = true"), sqlite_where=sa.text("is_selected = 1"),
        )
    if "project_scene_asset_references" not in _tables():
        op.create_table(
            "project_scene_asset_references",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("scene_definition_id", sa.String(length=36), sa.ForeignKey("scene_definitions.id"), nullable=False),
            sa.Column("scene_asset_id", sa.String(length=36), sa.ForeignKey("scene_assets.id"), nullable=False),
            sa.Column("scene_asset_version_id", sa.String(length=36), sa.ForeignKey("scene_asset_versions.id"), nullable=False),
            sa.Column("source_reference_image_id", sa.String(length=36), sa.ForeignKey("scene_reference_images.id"), nullable=True),
            sa.Column("is_selected", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("scene_definition_id", "scene_asset_version_id", name="uq_project_scene_asset_version_reference"),
        )
        op.create_index("ix_project_scene_asset_references_project_id", "project_scene_asset_references", ["project_id"])
        op.create_index("ix_project_scene_asset_references_scene_definition_id", "project_scene_asset_references", ["scene_definition_id"])
        op.create_index("ix_project_scene_asset_references_scene_asset_id", "project_scene_asset_references", ["scene_asset_id"])
        op.create_index("ix_project_scene_asset_references_scene_asset_version_id", "project_scene_asset_references", ["scene_asset_version_id"])
        op.create_index("ix_project_scene_asset_references_source_reference_image_id", "project_scene_asset_references", ["source_reference_image_id"])
        op.create_index(
            "uq_selected_project_scene_asset", "project_scene_asset_references", ["scene_definition_id"], unique=True,
            postgresql_where=sa.text("is_selected = true"), sqlite_where=sa.text("is_selected = 1"),
        )


def upgrade() -> None:
    _create_asset_tables()
    _add_column_if_missing("character_definitions", sa.Column("asset_library_character_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("scene_definitions", sa.Column("asset_library_scene_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("character_reference_images", sa.Column("asset_version_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("scene_reference_images", sa.Column("asset_version_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("shot_asset_bindings", sa.Column("character_asset_version_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("shot_asset_bindings", sa.Column("scene_asset_version_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("video_clip_asset_bindings", sa.Column("character_asset_version_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("video_clip_asset_bindings", sa.Column("scene_asset_version_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("shot_plans", sa.Column("emotion", sa.Text(), nullable=False, server_default="未指定"))
    _add_column_if_missing("shot_plans", sa.Column("camera_type", sa.String(length=120), nullable=False, server_default="中景"))
    _add_column_if_missing("shot_plans", sa.Column("camera_move", sa.Text(), nullable=False, server_default="固定机位"))
    _add_column_if_missing("shot_plans", sa.Column("lighting", sa.Text(), nullable=False, server_default="自然光"))
    _add_column_if_missing("shot_plans", sa.Column("image_prompt", sa.Text(), nullable=False, server_default=""))
    _add_column_if_missing("shot_plans", sa.Column("video_prompt", sa.Text(), nullable=False, server_default=""))
    _add_column_if_missing("shot_plans", sa.Column("sound_prompt", sa.Text(), nullable=False, server_default=""))
    _create_project_reference_tables()

    # 新增字段的 FK 不作为历史项目升级前提；服务层会在写入新记录时保证对应实体存在。
    # 为热点追溯路径补充普通索引，避免资产中心进入后项目生产台 N+1 扫描。
    for table, name, columns in (
        ("character_definitions", "ix_character_definitions_asset_library_character_id", ["asset_library_character_id"]),
        ("scene_definitions", "ix_scene_definitions_asset_library_scene_id", ["asset_library_scene_id"]),
        ("character_reference_images", "ix_character_reference_images_asset_version_id", ["asset_version_id"]),
        ("scene_reference_images", "ix_scene_reference_images_asset_version_id", ["asset_version_id"]),
        ("shot_asset_bindings", "ix_shot_asset_bindings_character_asset_version_id", ["character_asset_version_id"]),
        ("shot_asset_bindings", "ix_shot_asset_bindings_scene_asset_version_id", ["scene_asset_version_id"]),
        ("video_clip_asset_bindings", "ix_video_clip_asset_bindings_character_asset_version_id", ["character_asset_version_id"]),
        ("video_clip_asset_bindings", "ix_video_clip_asset_bindings_scene_asset_version_id", ["scene_asset_version_id"]),
    ):
        if name not in _indexes(table):
            op.create_index(name, table, columns)


def downgrade() -> None:
    raise RuntimeError("资产中心含跨项目生产引用；请从升级前备份恢复，禁止危险回退")
