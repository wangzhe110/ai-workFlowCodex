"""新增带货短剧工作流的领域模型基础。

Revision ID: 0010_commerce_domain_foundation
Revises: 0009_phase4_asset_center_and_structured_shots
Create Date: 2026-08-09

本迁移只新增 Commerce 领域表，不修改 V1 的工作流、项目状态、历史数据或既有枚举。
产品主体及其版本没有 project_id，是可共享资产；项目和 StoryRun 只通过引用采用产品
生产版本。因此删除项目只级联删除其脚本、选择与运行，不会删除任何 ProductAsset。
"""

from alembic import op
import sqlalchemy as sa


revision = "0010_commerce_domain_foundation"
down_revision = "0009_phase4_asset_center_and_structured_shots"
branch_labels = None
depends_on = None


def _enum(*values: str, name: str) -> sa.Enum:
    """新枚举使用 VARCHAR + CHECK，SQLite 和 PostgreSQL 的行为保持一致。"""

    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def upgrade() -> None:
    """创建带货短剧的追加式领域表。"""

    op.create_table(
        "script_assets",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("media_asset_id", sa.String(length=36), sa.ForeignKey("media_assets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=180), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("media_asset_id", name="uq_script_asset_media_asset"),
    )
    op.create_index("ix_script_assets_project_id", "script_assets", ["project_id"])
    op.create_index("ix_script_assets_media_asset_id", "script_assets", ["media_asset_id"])

    op.create_table(
        "script_analysis_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("script_asset_id", sa.String(length=36), sa.ForeignKey("script_assets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("timeline_transcript", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("story_beats", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("role_archetypes", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("conflicts", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("turning_points", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("emotional_curve", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("chapter_candidates", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("product_slot_candidates", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("narrative_function_sequence", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("raw_analysis", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "analysis_status",
            _enum("PENDING", "RUNNING", "SUCCEEDED", "FAILED", name="scriptanalysisstatus"),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("script_asset_id", "version", name="uq_script_analysis_version"),
    )
    op.create_index("ix_script_analysis_versions_script_asset_id", "script_analysis_versions", ["script_asset_id"])

    op.create_table(
        "product_assets",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=180), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_product_assets_name", "product_assets", ["name"])

    op.create_table(
        "product_analysis_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("product_asset_id", sa.String(length=36), sa.ForeignKey("product_assets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_media_asset_id", sa.String(length=36), sa.ForeignKey("media_assets.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("raw_analysis", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "analysis_status",
            _enum("PENDING", "RUNNING", "SUCCEEDED", "FAILED", name="productanalysisstatus"),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("product_asset_id", "version", name="uq_product_analysis_version"),
    )
    op.create_index("ix_product_analysis_versions_product_asset_id", "product_analysis_versions", ["product_asset_id"])
    op.create_index("ix_product_analysis_versions_source_media_asset_id", "product_analysis_versions", ["source_media_asset_id"])

    op.create_table(
        "product_asset_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("product_asset_id", sa.String(length=36), sa.ForeignKey("product_assets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_analysis_version_id", sa.String(length=36), sa.ForeignKey("product_analysis_versions.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("product_name", sa.String(length=180), nullable=False),
        sa.Column("appearance_description", sa.Text(), nullable=False, server_default=""),
        sa.Column("selling_points", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("user_pain_points", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("usage_scenarios", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("package_ocr", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("reference_images", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column(
            "status",
            _enum("DRAFT", "CONFIRMED", "ARCHIVED", name="productassetversionstatus"),
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("product_asset_id", "version", name="uq_product_asset_version"),
    )
    op.create_index("ix_product_asset_versions_product_asset_id", "product_asset_versions", ["product_asset_id"])
    op.create_index("ix_product_asset_versions_source_analysis_version_id", "product_asset_versions", ["source_analysis_version_id"])

    op.create_table(
        "project_product_selections",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_asset_id", sa.String(length=36), sa.ForeignKey("product_assets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("product_asset_version_id", sa.String(length=36), sa.ForeignKey("product_asset_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "product_asset_version_id", name="uq_project_product_version_selection"),
    )
    op.create_index("ix_project_product_selections_project_id", "project_product_selections", ["project_id"])
    op.create_index("ix_project_product_selections_product_asset_id", "project_product_selections", ["product_asset_id"])
    op.create_index("ix_project_product_selections_product_asset_version_id", "project_product_selections", ["product_asset_version_id"])

    op.create_table(
        "story_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("topic_candidate_id", sa.String(length=36), sa.ForeignKey("topic_candidates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_product_selection_id", sa.String(length=36), sa.ForeignKey("project_product_selections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_asset_version_id", sa.String(length=36), sa.ForeignKey("product_asset_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("run_number", sa.Integer(), nullable=False),
        sa.Column("mode", _enum("STEPWISE", "AUTO", name="storyrunmode"), nullable=False, server_default="STEPWISE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "topic_candidate_id", "run_number", name="uq_story_run_topic_number"),
    )
    op.create_index("ix_story_runs_project_id", "story_runs", ["project_id"])
    op.create_index("ix_story_runs_topic_candidate_id", "story_runs", ["topic_candidate_id"])
    op.create_index("ix_story_runs_project_product_selection_id", "story_runs", ["project_product_selection_id"])
    op.create_index("ix_story_runs_product_asset_version_id", "story_runs", ["product_asset_version_id"])

    op.create_table(
        "story_run_states",
        sa.Column("story_run_id", sa.String(length=36), sa.ForeignKey("story_runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column(
            "current_stage",
            _enum("TOPIC", "OUTLINE", "CHAPTERS", "STORYBOARD", "VISUAL_ASSETS", "VIDEO_PROMPTS", "SEGMENT_RENDER", "COMPLETED", name="storyrunstage"),
            nullable=False,
            server_default="TOPIC",
        ),
        sa.Column(
            "status",
            _enum("PENDING", "RUNNING", "PAUSED", "FAILED", "COMPLETED", "CANCELLED", name="storyrunstatus"),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("stage_data", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "story_outline_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("story_run_id", sa.String(length=36), sa.ForeignKey("story_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=180), nullable=False),
        sa.Column("premise", sa.Text(), nullable=False),
        sa.Column("story_beats", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("product_placement_strategy", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("status", _enum("DRAFT", "LOCKED", "SUPERSEDED", name="outlineversionstatus"), nullable=False, server_default="DRAFT"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("story_run_id", "version", name="uq_story_outline_version"),
    )
    op.create_index("ix_story_outline_versions_story_run_id", "story_outline_versions", ["story_run_id"])

    op.create_table(
        "chapter_plans",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("story_run_id", sa.String(length=36), sa.ForeignKey("story_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("outline_version_id", sa.String(length=36), sa.ForeignKey("story_outline_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=180), nullable=False),
        sa.Column("narrative_purpose", sa.Text(), nullable=False),
        sa.Column("content_summary", sa.Text(), nullable=False),
        sa.Column("product_plan", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("story_run_id", "chapter_number", name="uq_chapter_plan_number"),
    )
    op.create_index("ix_chapter_plans_story_run_id", "chapter_plans", ["story_run_id"])
    op.create_index("ix_chapter_plans_outline_version_id", "chapter_plans", ["outline_version_id"])

    op.create_table(
        "scene_mapping_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("story_run_id", sa.String(length=36), sa.ForeignKey("story_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("outline_version_id", sa.String(length=36), sa.ForeignKey("story_outline_versions.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("mapping_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="DRAFT"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("story_run_id", "version", name="uq_scene_mapping_version"),
    )
    op.create_index("ix_scene_mapping_versions_story_run_id", "scene_mapping_versions", ["story_run_id"])
    op.create_index("ix_scene_mapping_versions_outline_version_id", "scene_mapping_versions", ["outline_version_id"])

    op.create_table(
        "video_segment_plans",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("story_run_id", sa.String(length=36), sa.ForeignKey("story_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chapter_id", sa.String(length=36), sa.ForeignKey("chapter_plans.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("segment_number", sa.Integer(), nullable=False),
        sa.Column("target_duration_ms", sa.Integer(), nullable=False),
        sa.Column("narrative_target", sa.Text(), nullable=False),
        sa.Column("status", _enum("DRAFT", "READY", "RENDERING", "COMPLETED", "FAILED", name="segmentplanstatus"), nullable=False, server_default="DRAFT"),
        sa.Column("video_prompt_version", sa.String(length=80), nullable=True),
        sa.Column("video_prompt_trace", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("story_run_id", "segment_number", name="uq_video_segment_plan_number"),
        sa.CheckConstraint("target_duration_ms >= 4000 AND target_duration_ms <= 15000", name="ck_video_segment_duration_range"),
    )
    op.create_index("ix_video_segment_plans_story_run_id", "video_segment_plans", ["story_run_id"])
    op.create_index("ix_video_segment_plans_chapter_id", "video_segment_plans", ["chapter_id"])

    op.create_table(
        "sub_shot_plans",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("video_segment_id", sa.String(length=36), sa.ForeignKey("video_segment_plans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("shot_number", sa.Integer(), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("character_refs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("emotion", sa.Text(), nullable=False),
        sa.Column("shot_scale", sa.String(length=80), nullable=False),
        sa.Column("camera_move", sa.Text(), nullable=False),
        sa.Column("lighting", sa.Text(), nullable=False),
        sa.Column("visual_description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("video_segment_id", "shot_number", name="uq_sub_shot_plan_number"),
        sa.CheckConstraint("start_ms >= 0", name="ck_sub_shot_start_nonnegative"),
        sa.CheckConstraint("end_ms > start_ms", name="ck_sub_shot_end_after_start"),
        sa.CheckConstraint("end_ms <= 15000", name="ck_sub_shot_end_maximum"),
    )
    op.create_index("ix_sub_shot_plans_video_segment_id", "sub_shot_plans", ["video_segment_id"])

    op.create_table(
        "dialogue_lines",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("video_segment_id", sa.String(length=36), sa.ForeignKey("video_segment_plans.id", ondelete="CASCADE"), nullable=True),
        sa.Column("sub_shot_id", sa.String(length=36), sa.ForeignKey("sub_shot_plans.id", ondelete="CASCADE"), nullable=True),
        sa.Column("speaker", sa.String(length=160), nullable=False),
        sa.Column("dialogue", sa.Text(), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(video_segment_id IS NOT NULL AND sub_shot_id IS NULL) OR (video_segment_id IS NULL AND sub_shot_id IS NOT NULL)",
            name="ck_dialogue_line_single_owner",
        ),
        sa.CheckConstraint("start_ms >= 0", name="ck_dialogue_line_start_nonnegative"),
        sa.CheckConstraint("end_ms > start_ms", name="ck_dialogue_line_end_after_start"),
    )
    op.create_index("ix_dialogue_lines_video_segment_id", "dialogue_lines", ["video_segment_id"])
    op.create_index("ix_dialogue_lines_sub_shot_id", "dialogue_lines", ["sub_shot_id"])

    op.create_table(
        "product_placement_plans",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("story_run_id", sa.String(length=36), sa.ForeignKey("story_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_asset_version_id", sa.String(length=36), sa.ForeignKey("product_asset_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("chapter_id", sa.String(length=36), sa.ForeignKey("chapter_plans.id", ondelete="CASCADE"), nullable=True),
        sa.Column("video_segment_id", sa.String(length=36), sa.ForeignKey("video_segment_plans.id", ondelete="CASCADE"), nullable=True),
        sa.Column("sub_shot_id", sa.String(length=36), sa.ForeignKey("sub_shot_plans.id", ondelete="CASCADE"), nullable=True),
        sa.Column("placement_method", _enum("SOFT_PROP", "EXPERIENCE_DEMO", "VOICEOVER", "HYBRID", name="productplacementmethod"), nullable=False),
        sa.Column("placement_strength", _enum("LIGHT", "MEDIUM", "STRONG", name="productplacementstrength"), nullable=False),
        sa.Column("pain_point_trigger", sa.Text(), nullable=False),
        sa.Column("product_action", sa.Text(), nullable=False),
        sa.Column("ad_entry_point", sa.Text(), nullable=False),
        sa.Column("story_recovery_point", sa.Text(), nullable=False),
        sa.Column("planned_duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("chapter_id IS NOT NULL OR video_segment_id IS NOT NULL OR sub_shot_id IS NOT NULL", name="ck_product_placement_has_location"),
        sa.CheckConstraint("planned_duration_ms >= 0", name="ck_product_placement_duration_nonnegative"),
    )
    op.create_index("ix_product_placement_plans_story_run_id", "product_placement_plans", ["story_run_id"])
    op.create_index("ix_product_placement_plans_product_asset_version_id", "product_placement_plans", ["product_asset_version_id"])
    op.create_index("ix_product_placement_plans_chapter_id", "product_placement_plans", ["chapter_id"])
    op.create_index("ix_product_placement_plans_video_segment_id", "product_placement_plans", ["video_segment_id"])
    op.create_index("ix_product_placement_plans_sub_shot_id", "product_placement_plans", ["sub_shot_id"])

    op.create_table(
        "render_batches",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("story_run_id", sa.String(length=36), sa.ForeignKey("story_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workflow_run_id", sa.String(length=36), sa.ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("batch_number", sa.Integer(), nullable=False),
        sa.Column("status", _enum("PENDING", "RUNNING", "COMPLETED", "PARTIAL_FAILED", "FAILED", "CANCELLED", name="renderbatchstatus"), nullable=False, server_default="PENDING"),
        sa.Column("total_tasks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_tasks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_tasks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("running_tasks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model_config_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("generation_parameters_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("estimated_cost", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("currency", sa.String(length=12), nullable=False, server_default="CNY"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("story_run_id", "batch_number", name="uq_render_batch_number"),
        sa.CheckConstraint("total_tasks >= 0", name="ck_render_batch_total_nonnegative"),
        sa.CheckConstraint("completed_tasks >= 0", name="ck_render_batch_completed_nonnegative"),
        sa.CheckConstraint("failed_tasks >= 0", name="ck_render_batch_failed_nonnegative"),
        sa.CheckConstraint("running_tasks >= 0", name="ck_render_batch_running_nonnegative"),
    )
    op.create_index("ix_render_batches_story_run_id", "render_batches", ["story_run_id"])
    op.create_index("ix_render_batches_workflow_run_id", "render_batches", ["workflow_run_id"])


def downgrade() -> None:
    """只移除本迁移新增的 Commerce 表，不触碰 0009 及更早版本。"""

    for index_name, table_name in (
        ("ix_render_batches_workflow_run_id", "render_batches"),
        ("ix_render_batches_story_run_id", "render_batches"),
    ):
        op.drop_index(index_name, table_name=table_name)
    op.drop_table("render_batches")

    for index_name, table_name in (
        ("ix_product_placement_plans_sub_shot_id", "product_placement_plans"),
        ("ix_product_placement_plans_video_segment_id", "product_placement_plans"),
        ("ix_product_placement_plans_chapter_id", "product_placement_plans"),
        ("ix_product_placement_plans_product_asset_version_id", "product_placement_plans"),
        ("ix_product_placement_plans_story_run_id", "product_placement_plans"),
    ):
        op.drop_index(index_name, table_name=table_name)
    op.drop_table("product_placement_plans")

    for index_name, table_name in (
        ("ix_dialogue_lines_sub_shot_id", "dialogue_lines"),
        ("ix_dialogue_lines_video_segment_id", "dialogue_lines"),
    ):
        op.drop_index(index_name, table_name=table_name)
    op.drop_table("dialogue_lines")

    op.drop_index("ix_sub_shot_plans_video_segment_id", table_name="sub_shot_plans")
    op.drop_table("sub_shot_plans")

    for index_name, table_name in (
        ("ix_video_segment_plans_chapter_id", "video_segment_plans"),
        ("ix_video_segment_plans_story_run_id", "video_segment_plans"),
    ):
        op.drop_index(index_name, table_name=table_name)
    op.drop_table("video_segment_plans")

    for index_name, table_name in (
        ("ix_scene_mapping_versions_outline_version_id", "scene_mapping_versions"),
        ("ix_scene_mapping_versions_story_run_id", "scene_mapping_versions"),
    ):
        op.drop_index(index_name, table_name=table_name)
    op.drop_table("scene_mapping_versions")

    for index_name, table_name in (
        ("ix_chapter_plans_outline_version_id", "chapter_plans"),
        ("ix_chapter_plans_story_run_id", "chapter_plans"),
    ):
        op.drop_index(index_name, table_name=table_name)
    op.drop_table("chapter_plans")

    op.drop_index("ix_story_outline_versions_story_run_id", table_name="story_outline_versions")
    op.drop_table("story_outline_versions")
    op.drop_table("story_run_states")

    for index_name, table_name in (
        ("ix_story_runs_product_asset_version_id", "story_runs"),
        ("ix_story_runs_project_product_selection_id", "story_runs"),
        ("ix_story_runs_topic_candidate_id", "story_runs"),
        ("ix_story_runs_project_id", "story_runs"),
    ):
        op.drop_index(index_name, table_name=table_name)
    op.drop_table("story_runs")

    for index_name, table_name in (
        ("ix_project_product_selections_product_asset_version_id", "project_product_selections"),
        ("ix_project_product_selections_product_asset_id", "project_product_selections"),
        ("ix_project_product_selections_project_id", "project_product_selections"),
    ):
        op.drop_index(index_name, table_name=table_name)
    op.drop_table("project_product_selections")

    for index_name, table_name in (
        ("ix_product_asset_versions_source_analysis_version_id", "product_asset_versions"),
        ("ix_product_asset_versions_product_asset_id", "product_asset_versions"),
    ):
        op.drop_index(index_name, table_name=table_name)
    op.drop_table("product_asset_versions")

    for index_name, table_name in (
        ("ix_product_analysis_versions_source_media_asset_id", "product_analysis_versions"),
        ("ix_product_analysis_versions_product_asset_id", "product_analysis_versions"),
    ):
        op.drop_index(index_name, table_name=table_name)
    op.drop_table("product_analysis_versions")
    op.drop_index("ix_product_assets_name", table_name="product_assets")
    op.drop_table("product_assets")

    op.drop_index("ix_script_analysis_versions_script_asset_id", table_name="script_analysis_versions")
    op.drop_table("script_analysis_versions")
    op.drop_index("ix_script_assets_media_asset_id", table_name="script_assets")
    op.drop_index("ix_script_assets_project_id", table_name="script_assets")
    op.drop_table("script_assets")
