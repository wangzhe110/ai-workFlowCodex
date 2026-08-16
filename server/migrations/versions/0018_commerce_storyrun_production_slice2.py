"""Add versioned Commerce StoryRun director and media production assets.

Revision ID: 0018_commerce_storyrun_production_slice2
Revises: 0017_commerce_mainline_slice1
Create Date: 2026-08-12

This migration is purely additive.  Slice 1 creative batches and the existing
Commerce parent workflow remain untouched.  The new tables give a StoryRun a
separate, immutable production trail without pretending its CommerceCreativeIdea
is a V1 StoryProposal.
"""

from alembic import op
import sqlalchemy as sa


revision = "0018_commerce_storyrun_production_slice2"
down_revision = "0017_commerce_mainline_slice1"
branch_labels = None
depends_on = None


def _versioned_table(name: str, columns: list[sa.Column], constraints: list[object]) -> None:
    op.create_table(name, *columns, *constraints)


def upgrade() -> None:
    # Slice 2 的图片/视频任务没有 Commerce Phase 2 sidecar（该 sidecar 只约束
    # commerce_story_run 父运行）。因此在通用 WorkflowRun 表上添加严格限域的
    # 幂等唯一索引，防止同一冻结生产任务被并发重复提交，而不影响 V1 的并行视频子步。
    # 0001/0003 的历史固定表清单会以“当前 ORM 元数据”新建空库，因此最新
    # WorkflowRun 定义已经可能带有此索引；而真实的旧 0017 数据库则没有。两种
    # 路径都必须可升级，故用 SQLite/PostgreSQL 均支持的 IF NOT EXISTS 保持 DDL
    # 幂等，不去改写任何已发布历史迁移。
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_active_commerce_production_run_idempotency "
        "ON workflow_runs (idempotency_key) "
        "WHERE workflow_key LIKE 'commerce_production_%' "
        "AND status IN ('PENDING', 'RUNNING') AND idempotency_key IS NOT NULL"
    )
    _versioned_table(
        "commerce_character_design_versions",
        [
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("story_run_id", sa.String(36), nullable=False),
            sa.Column("source_outline_version_id", sa.String(36), nullable=False), sa.Column("source_product_asset_version_id", sa.String(36), nullable=False),
            sa.Column("workflow_run_id", sa.String(36)), sa.Column("model_invocation_id", sa.String(36)), sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="DRAFT"), sa.Column("content", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("input_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")), sa.Column("prompt_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("raw_response", sa.JSON(), nullable=False, server_default=sa.text("'{}'")), sa.Column("locked_at", sa.DateTime(timezone=True)), sa.Column("stale_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        ],
        [
            sa.ForeignKeyConstraint(["story_run_id"], ["story_runs.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["source_outline_version_id"], ["story_outline_versions.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["source_product_asset_version_id"], ["product_asset_versions.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="SET NULL"), sa.ForeignKeyConstraint(["model_invocation_id"], ["model_invocations.id"], ondelete="SET NULL"),
            sa.UniqueConstraint("story_run_id", "version", name="uq_commerce_character_design_version"), sa.CheckConstraint("version >= 1", name="ck_commerce_character_design_version_positive"),
        ],
    )
    op.create_index("ix_commerce_character_design_versions_story_run_id", "commerce_character_design_versions", ["story_run_id"])
    op.create_index("ix_commerce_character_design_versions_source_outline_version_id", "commerce_character_design_versions", ["source_outline_version_id"])
    op.create_index("ix_ccdv_product_version", "commerce_character_design_versions", ["source_product_asset_version_id"])
    op.create_index("ix_commerce_character_design_versions_workflow_run_id", "commerce_character_design_versions", ["workflow_run_id"])
    op.create_index("ix_commerce_character_design_versions_model_invocation_id", "commerce_character_design_versions", ["model_invocation_id"])
    op.create_index("ix_commerce_character_design_versions_status", "commerce_character_design_versions", ["status"])

    _versioned_table(
        "commerce_scene_design_versions",
        [
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("story_run_id", sa.String(36), nullable=False), sa.Column("source_outline_version_id", sa.String(36), nullable=False), sa.Column("character_design_version_id", sa.String(36), nullable=False), sa.Column("source_product_asset_version_id", sa.String(36), nullable=False),
            sa.Column("workflow_run_id", sa.String(36)), sa.Column("model_invocation_id", sa.String(36)), sa.Column("version", sa.Integer(), nullable=False), sa.Column("status", sa.String(20), nullable=False, server_default="DRAFT"),
            sa.Column("content", sa.JSON(), nullable=False, server_default=sa.text("'{}'")), sa.Column("input_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")), sa.Column("prompt_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")), sa.Column("raw_response", sa.JSON(), nullable=False, server_default=sa.text("'{}'")), sa.Column("locked_at", sa.DateTime(timezone=True)), sa.Column("stale_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        ],
        [
            sa.ForeignKeyConstraint(["story_run_id"], ["story_runs.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["source_outline_version_id"], ["story_outline_versions.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["character_design_version_id"], ["commerce_character_design_versions.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["source_product_asset_version_id"], ["product_asset_versions.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="SET NULL"), sa.ForeignKeyConstraint(["model_invocation_id"], ["model_invocations.id"], ondelete="SET NULL"),
            sa.UniqueConstraint("story_run_id", "version", name="uq_commerce_scene_design_version"), sa.CheckConstraint("version >= 1", name="ck_commerce_scene_design_version_positive"),
        ],
    )
    for name, columns in (("story_run_id", ["story_run_id"]), ("source_outline_version_id", ["source_outline_version_id"]), ("character_design_version_id", ["character_design_version_id"]), ("source_product_asset_version_id", ["source_product_asset_version_id"]), ("workflow_run_id", ["workflow_run_id"]), ("model_invocation_id", ["model_invocation_id"]), ("status", ["status"])):
        index_name = "ix_csdev_product_version" if name == "source_product_asset_version_id" else f"ix_commerce_scene_design_versions_{name}"
        op.create_index(index_name, "commerce_scene_design_versions", columns)

    _versioned_table(
        "commerce_storyboard_versions",
        [
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("story_run_id", sa.String(36), nullable=False), sa.Column("source_outline_version_id", sa.String(36), nullable=False), sa.Column("character_design_version_id", sa.String(36), nullable=False), sa.Column("scene_design_version_id", sa.String(36), nullable=False), sa.Column("source_product_asset_version_id", sa.String(36), nullable=False), sa.Column("workflow_run_id", sa.String(36)), sa.Column("model_invocation_id", sa.String(36)), sa.Column("version", sa.Integer(), nullable=False), sa.Column("status", sa.String(20), nullable=False, server_default="DRAFT"),
            sa.Column("content", sa.JSON(), nullable=False, server_default=sa.text("'{}'")), sa.Column("input_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")), sa.Column("prompt_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")), sa.Column("raw_response", sa.JSON(), nullable=False, server_default=sa.text("'{}'")), sa.Column("locked_at", sa.DateTime(timezone=True)), sa.Column("stale_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        ],
        [
            sa.ForeignKeyConstraint(["story_run_id"], ["story_runs.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["source_outline_version_id"], ["story_outline_versions.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["character_design_version_id"], ["commerce_character_design_versions.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["scene_design_version_id"], ["commerce_scene_design_versions.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["source_product_asset_version_id"], ["product_asset_versions.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="SET NULL"), sa.ForeignKeyConstraint(["model_invocation_id"], ["model_invocations.id"], ondelete="SET NULL"), sa.UniqueConstraint("story_run_id", "version", name="uq_commerce_storyboard_version"), sa.CheckConstraint("version >= 1", name="ck_commerce_storyboard_version_positive"),
        ],
    )
    for name, columns in (("story_run_id", ["story_run_id"]), ("source_outline_version_id", ["source_outline_version_id"]), ("character_design_version_id", ["character_design_version_id"]), ("scene_design_version_id", ["scene_design_version_id"]), ("source_product_asset_version_id", ["source_product_asset_version_id"]), ("workflow_run_id", ["workflow_run_id"]), ("model_invocation_id", ["model_invocation_id"]), ("status", ["status"])):
        op.create_index(f"ix_commerce_storyboard_versions_{name}", "commerce_storyboard_versions", columns)

    for table, design_column, logical_column, prefix in (
        ("commerce_character_reference_images", "character_design_version_id", "role_id", "character_reference_image"),
        ("commerce_scene_reference_images", "scene_design_version_id", "scene_id", "scene_reference_image"),
    ):
        design_table = "commerce_character_design_versions" if "character" in table else "commerce_scene_design_versions"
        op.create_table(
            table,
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("story_run_id", sa.String(36), nullable=False), sa.Column(design_column, sa.String(36), nullable=False), sa.Column(logical_column, sa.String(80), nullable=False), sa.Column("workflow_run_id", sa.String(36)), sa.Column("model_invocation_id", sa.String(36)), sa.Column("version", sa.Integer(), nullable=False), sa.Column("image_url", sa.Text()), sa.Column("prompt_snapshot", sa.Text(), nullable=False, server_default=""), sa.Column("input_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")), sa.Column("status", sa.String(20), nullable=False, server_default="DRAFT"), sa.Column("error_message", sa.Text()), sa.Column("locked_at", sa.DateTime(timezone=True)), sa.Column("stale_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["story_run_id"], ["story_runs.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint([design_column], [f"{design_table}.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="SET NULL"), sa.ForeignKeyConstraint(["model_invocation_id"], ["model_invocations.id"], ondelete="SET NULL"), sa.UniqueConstraint(design_column, logical_column, "version", name=f"uq_commerce_{prefix}_version"), sa.CheckConstraint("version >= 1", name=f"ck_commerce_{prefix}_version_positive"),
        )
        for suffix, columns in (("story_run_id", ["story_run_id"]), (design_column, [design_column]), ("workflow_run_id", ["workflow_run_id"]), ("model_invocation_id", ["model_invocation_id"]), ("status", ["status"])):
            index_name = "ix_ccri_design_version" if table == "commerce_character_reference_images" and suffix == "character_design_version_id" else f"ix_{table}_{suffix}"
            op.create_index(index_name, table, columns)

    op.create_table(
        "commerce_shot_keyframe_versions",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("story_run_id", sa.String(36), nullable=False), sa.Column("storyboard_version_id", sa.String(36), nullable=False), sa.Column("shot_id", sa.String(80), nullable=False), sa.Column("shot_number", sa.Integer(), nullable=False), sa.Column("workflow_run_id", sa.String(36)), sa.Column("model_invocation_id", sa.String(36)), sa.Column("version", sa.Integer(), nullable=False), sa.Column("image_url", sa.Text()), sa.Column("prompt_snapshot", sa.Text(), nullable=False, server_default=""), sa.Column("input_asset_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")), sa.Column("status", sa.String(20), nullable=False, server_default="DRAFT"), sa.Column("error_message", sa.Text()), sa.Column("locked_at", sa.DateTime(timezone=True)), sa.Column("stale_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["story_run_id"], ["story_runs.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["storyboard_version_id"], ["commerce_storyboard_versions.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="SET NULL"), sa.ForeignKeyConstraint(["model_invocation_id"], ["model_invocations.id"], ondelete="SET NULL"), sa.UniqueConstraint("storyboard_version_id", "shot_id", "version", name="uq_commerce_shot_keyframe_version"), sa.CheckConstraint("version >= 1", name="ck_commerce_shot_keyframe_version_positive"),
    )
    for suffix, columns in (("story_run_id", ["story_run_id"]), ("storyboard_version_id", ["storyboard_version_id"]), ("workflow_run_id", ["workflow_run_id"]), ("model_invocation_id", ["model_invocation_id"]), ("status", ["status"])):
        op.create_index(f"ix_commerce_shot_keyframe_versions_{suffix}", "commerce_shot_keyframe_versions", columns)

    op.create_table(
        "commerce_video_prompt_versions",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("story_run_id", sa.String(36), nullable=False), sa.Column("storyboard_version_id", sa.String(36), nullable=False), sa.Column("shot_id", sa.String(80), nullable=False), sa.Column("shot_number", sa.Integer(), nullable=False), sa.Column("keyframe_version_id", sa.String(36), nullable=False), sa.Column("workflow_run_id", sa.String(36)), sa.Column("model_invocation_id", sa.String(36)), sa.Column("version", sa.Integer(), nullable=False), sa.Column("prompt", sa.Text(), nullable=False), sa.Column("trace", sa.JSON(), nullable=False, server_default=sa.text("'{}'")), sa.Column("status", sa.String(20), nullable=False, server_default="DRAFT"), sa.Column("locked_at", sa.DateTime(timezone=True)), sa.Column("stale_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["story_run_id"], ["story_runs.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["storyboard_version_id"], ["commerce_storyboard_versions.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["keyframe_version_id"], ["commerce_shot_keyframe_versions.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="SET NULL"), sa.ForeignKeyConstraint(["model_invocation_id"], ["model_invocations.id"], ondelete="SET NULL"), sa.UniqueConstraint("storyboard_version_id", "shot_id", "version", name="uq_commerce_video_prompt_version"), sa.CheckConstraint("version >= 1", name="ck_commerce_video_prompt_version_positive"),
    )
    for suffix, columns in (("story_run_id", ["story_run_id"]), ("storyboard_version_id", ["storyboard_version_id"]), ("keyframe_version_id", ["keyframe_version_id"]), ("workflow_run_id", ["workflow_run_id"]), ("model_invocation_id", ["model_invocation_id"]), ("status", ["status"])):
        op.create_index(f"ix_commerce_video_prompt_versions_{suffix}", "commerce_video_prompt_versions", columns)

    op.create_table(
        "commerce_video_clip_versions",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("story_run_id", sa.String(36), nullable=False), sa.Column("storyboard_version_id", sa.String(36), nullable=False), sa.Column("shot_id", sa.String(80), nullable=False), sa.Column("shot_number", sa.Integer(), nullable=False), sa.Column("keyframe_version_id", sa.String(36), nullable=False), sa.Column("video_prompt_version_id", sa.String(36), nullable=False), sa.Column("workflow_run_id", sa.String(36)), sa.Column("model_invocation_id", sa.String(36)), sa.Column("version", sa.Integer(), nullable=False), sa.Column("idempotency_key", sa.String(160), nullable=False), sa.Column("provider_task_id", sa.String(255)), sa.Column("video_url", sa.Text()), sa.Column("input_asset_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")), sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"), sa.Column("error_message", sa.Text()), sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("duration_ms", sa.Integer()), sa.Column("media_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")), sa.Column("reviewed_at", sa.DateTime(timezone=True)), sa.Column("review_note", sa.Text()), sa.Column("stale_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["story_run_id"], ["story_runs.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["storyboard_version_id"], ["commerce_storyboard_versions.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["keyframe_version_id"], ["commerce_shot_keyframe_versions.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["video_prompt_version_id"], ["commerce_video_prompt_versions.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="SET NULL"), sa.ForeignKeyConstraint(["model_invocation_id"], ["model_invocations.id"], ondelete="SET NULL"), sa.UniqueConstraint("storyboard_version_id", "shot_id", "version", name="uq_commerce_video_clip_version"), sa.UniqueConstraint("idempotency_key", name="uq_commerce_video_clip_idempotency"), sa.CheckConstraint("version >= 1", name="ck_commerce_video_clip_version_positive"),
    )
    for suffix, columns in (("story_run_id", ["story_run_id"]), ("storyboard_version_id", ["storyboard_version_id"]), ("keyframe_version_id", ["keyframe_version_id"]), ("video_prompt_version_id", ["video_prompt_version_id"]), ("workflow_run_id", ["workflow_run_id"]), ("model_invocation_id", ["model_invocation_id"]), ("provider_task_id", ["provider_task_id"]), ("status", ["status"])):
        op.create_index(f"ix_commerce_video_clip_versions_{suffix}", "commerce_video_clip_versions", columns)

    op.create_table(
        "commerce_final_videos",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("story_run_id", sa.String(36), nullable=False), sa.Column("storyboard_version_id", sa.String(36), nullable=False), sa.Column("workflow_run_id", sa.String(36)), sa.Column("version", sa.Integer(), nullable=False), sa.Column("clip_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")), sa.Column("input_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")), sa.Column("output_url", sa.Text()), sa.Column("storage_key", sa.String(512), unique=True), sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"), sa.Column("error_message", sa.Text()), sa.Column("media_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")), sa.Column("stale_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["story_run_id"], ["story_runs.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["storyboard_version_id"], ["commerce_storyboard_versions.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="SET NULL"), sa.UniqueConstraint("story_run_id", "version", name="uq_commerce_final_video_version"), sa.CheckConstraint("version >= 1", name="ck_commerce_final_video_version_positive"),
    )
    for suffix, columns in (("story_run_id", ["story_run_id"]), ("storyboard_version_id", ["storyboard_version_id"]), ("workflow_run_id", ["workflow_run_id"]), ("status", ["status"])):
        op.create_index(f"ix_commerce_final_videos_{suffix}", "commerce_final_videos", columns)


def downgrade() -> None:
    tables = (
        "commerce_final_videos", "commerce_video_clip_versions", "commerce_video_prompt_versions", "commerce_shot_keyframe_versions", "commerce_scene_reference_images", "commerce_character_reference_images", "commerce_storyboard_versions", "commerce_scene_design_versions", "commerce_character_design_versions",
    )
    # 每张表由 drop_table 一并撤销本迁移新建的索引/约束；所有删除方向均指向本次
    # 新增的叶子，绝不触碰 Slice 1 的 StoryRun/产品/大纲历史数据。
    for table in tables:
        op.drop_table(table)
    op.execute("DROP INDEX IF EXISTS uq_active_commerce_production_run_idempotency")
