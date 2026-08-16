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


def _v1_foundation_metadata() -> tuple[sa.MetaData, list[sa.Table]]:
    """返回冻结在 0003 时点的 V1 建表快照。

    历史版本曾直接调用 ``Base.metadata.create_all()``。ORM 元数据会随着后续版本
    演进：例如 0009 的资产中心外键会被错误地提前带到 ``character_definitions``，
    从而使 PostgreSQL 在 ``character_assets`` 尚未创建时失败。

    这里故意不导入业务实体，也不从当前 ``Base.metadata`` 复制任何表。该快照包括
    0003 的 V1 新表，以及当时存在的字段、索引和约束；0005--0009 的归属、幂等、
    模型状态和资产中心变更仍只能由各自迁移引入。
    """

    metadata = sa.MetaData()

    # 这些是 0001 已创建的外部表。只作为 ForeignKey 解析锚点，绝不能传给
    # create_all，否则又会把当前模型的初始表结构带进历史迁移。
    for table_name in ("projects", "workflow_runs", "workflow_steps", "model_profiles", "video_clips"):
        sa.Table(table_name, metadata, sa.Column("id", sa.String(length=36), primary_key=True))

    run_status = sa.Enum("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", name="runstatus")
    workflow_definition_status = sa.Enum("DRAFT", "PUBLISHED", "ARCHIVED", name="workflowdefinitionstatus")
    production_stage = sa.Enum(
        "LEGACY_READONLY",
        "REFERENCE_ANALYSIS",
        "ANALYSIS_REVIEW",
        "STORY_GENERATION",
        "STORY_REVIEW",
        "CHARACTER_ASSETS",
        "SCENE_ASSETS",
        "DIRECTOR_PLANNING",
        "SHOT_KEYFRAMES",
        "VIDEO_GENERATION",
        "VIDEO_REVIEW",
        "FINAL_EXPORT",
        "COMPLETED",
        name="productionstage",
    )
    review_status = sa.Enum("PENDING_REVIEW", "LOCKED", "REJECTED", name="reviewstatus")
    story_proposal_status = sa.Enum("CANDIDATE", "SELECTED", "REJECTED", name="storyproposalstatus")
    director_plan_status = sa.Enum("PENDING", "RUNNING", "READY", "FAILED", name="directorplanstatus")
    design_status = sa.Enum("DRAFT", "READY", "LOCKED", name="designstatus")
    model_selection_mode = sa.Enum("SINGLE", "MULTI_PARALLEL", "AB_TEST", name="modelselectionmode")
    prompt_template_status = sa.Enum("DRAFT", "ACTIVE", "ARCHIVED", name="prompttemplatestatus")

    workflow_definitions = sa.Table(
        "workflow_definitions",
        metadata,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workflow_code", sa.String(length=80), nullable=False),
        sa.Column("version", sa.String(length=80), nullable=False),
        sa.Column("definition_json", sa.JSON(), nullable=False),
        sa.Column("status", workflow_definition_status, nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workflow_code", "version", name="uq_workflow_definition_version"),
    )
    sa.Index("ix_workflow_definitions_workflow_code", workflow_definitions.c.workflow_code)

    project_production_states = sa.Table(
        "project_production_states",
        metadata,
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), primary_key=True),
        sa.Column("active_stage", production_stage, nullable=False),
        sa.Column("workflow_definition_id", sa.String(length=36), sa.ForeignKey("workflow_definitions.id"), nullable=False),
        sa.Column(
            "locked_reference_analysis_id",
            sa.String(length=36),
            sa.ForeignKey("reference_analyses.id", use_alter=True, name="fk_state_locked_analysis"),
        ),
        sa.Column(
            "selected_story_proposal_id",
            sa.String(length=36),
            sa.ForeignKey("story_proposals.id", use_alter=True, name="fk_state_selected_story"),
        ),
        sa.Column(
            "director_plan_id",
            sa.String(length=36),
            sa.ForeignKey("director_plans.id", use_alter=True, name="fk_state_director_plan"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    reference_analyses = sa.Table(
        "reference_analyses",
        metadata,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("workflow_run_id", sa.String(length=36), sa.ForeignKey("workflow_runs.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("video_script_structure", sa.JSON(), nullable=False),
        sa.Column("opening_analysis", sa.JSON(), nullable=False),
        sa.Column("viral_elements", sa.JSON(), nullable=False),
        sa.Column("scene_analysis", sa.JSON(), nullable=False),
        sa.Column("creative_brief", sa.JSON(), nullable=False),
        sa.Column("generation_status", run_status, nullable=False),
        sa.Column("review_status", review_status, nullable=False),
        sa.Column("locked_snapshot", sa.JSON()),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "version", name="uq_reference_analysis_version"),
    )
    sa.Index("ix_reference_analyses_project_id", reference_analyses.c.project_id)
    sa.Index("ix_reference_analyses_workflow_run_id", reference_analyses.c.workflow_run_id)

    review_decisions = sa.Table(
        "review_decisions",
        metadata,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("target_type", sa.String(length=60), nullable=False),
        sa.Column("target_id", sa.String(length=36), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("reviewer_label", sa.String(length=120), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    sa.Index("ix_review_decisions_project_id", review_decisions.c.project_id)
    sa.Index("ix_review_decision_target", review_decisions.c.target_type, review_decisions.c.target_id, review_decisions.c.created_at)

    story_generation_batches = sa.Table(
        "story_generation_batches",
        metadata,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("reference_analysis_id", sa.String(length=36), sa.ForeignKey("reference_analyses.id"), nullable=False),
        sa.Column("workflow_run_id", sa.String(length=36), sa.ForeignKey("workflow_runs.id"), nullable=False),
        sa.Column("request_snapshot", sa.JSON(), nullable=False),
        sa.Column("status", run_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    sa.Index("ix_story_generation_batches_project_id", story_generation_batches.c.project_id)
    sa.Index("ix_story_generation_batches_reference_analysis_id", story_generation_batches.c.reference_analysis_id)
    sa.Index("ix_story_generation_batches_workflow_run_id", story_generation_batches.c.workflow_run_id)

    story_proposals = sa.Table(
        "story_proposals",
        metadata,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("batch_id", sa.String(length=36), sa.ForeignKey("story_generation_batches.id"), nullable=False),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column(
            "model_invocation_id",
            sa.String(length=36),
            sa.ForeignKey("model_invocations.id", use_alter=True, name="fk_story_invocation"),
        ),
        sa.Column("candidate_number", sa.Integer(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("status", story_proposal_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("batch_id", "candidate_number", name="uq_story_proposal_batch_number"),
    )
    sa.Index("ix_story_proposals_batch_id", story_proposals.c.batch_id)
    sa.Index("ix_story_proposals_project_id", story_proposals.c.project_id)
    # 0005 将初版“每项目只能选择一个”替换为“每批选择一个”。
    sa.Index(
        "uq_selected_story_per_project",
        story_proposals.c.project_id,
        unique=True,
        postgresql_where=sa.text("status = 'SELECTED'"),
        sqlite_where=sa.text("status = 'SELECTED'"),
    )

    director_plans = sa.Table(
        "director_plans",
        metadata,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("story_proposal_id", sa.String(length=36), sa.ForeignKey("story_proposals.id"), nullable=False),
        sa.Column("workflow_run_id", sa.String(length=36), sa.ForeignKey("workflow_runs.id"), nullable=False),
        sa.Column("visual_bible", sa.JSON(), nullable=False),
        sa.Column("status", director_plan_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    sa.Index("ix_director_plans_project_id", director_plans.c.project_id)
    sa.Index("ix_director_plans_story_proposal_id", director_plans.c.story_proposal_id)
    sa.Index("ix_director_plans_workflow_run_id", director_plans.c.workflow_run_id)

    character_definitions = sa.Table(
        "character_definitions",
        metadata,
        sa.Column("id", sa.String(length=36), primary_key=True),
        # 0005 才增加 story_proposal_id，并将旧导演方案引用改为可空。
        sa.Column("director_plan_id", sa.String(length=36), sa.ForeignKey("director_plans.id"), nullable=False),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("character_code", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("age_description", sa.String(length=120), nullable=False),
        sa.Column("appearance", sa.Text(), nullable=False),
        sa.Column("costume", sa.Text(), nullable=False),
        sa.Column("temperament", sa.Text(), nullable=False),
        sa.Column("design_status", design_status, nullable=False),
        sa.Column(
            "locked_reference_image_id",
            sa.String(length=36),
            sa.ForeignKey("character_reference_images.id", use_alter=True, name="fk_character_locked_image"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("director_plan_id", "character_code", name="uq_character_plan_code"),
    )
    sa.Index("ix_character_definitions_director_plan_id", character_definitions.c.director_plan_id)
    sa.Index("ix_character_definitions_project_id", character_definitions.c.project_id)

    scene_definitions = sa.Table(
        "scene_definitions",
        metadata,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("director_plan_id", sa.String(length=36), sa.ForeignKey("director_plans.id"), nullable=False),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("scene_code", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("location", sa.Text(), nullable=False),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("visual_style", sa.Text(), nullable=False),
        sa.Column("mood", sa.Text(), nullable=False),
        sa.Column("design_status", design_status, nullable=False),
        sa.Column(
            "locked_reference_image_id",
            sa.String(length=36),
            sa.ForeignKey("scene_reference_images.id", use_alter=True, name="fk_scene_locked_image"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("director_plan_id", "scene_code", name="uq_scene_plan_code"),
    )
    sa.Index("ix_scene_definitions_director_plan_id", scene_definitions.c.director_plan_id)
    sa.Index("ix_scene_definitions_project_id", scene_definitions.c.project_id)

    character_reference_images = sa.Table(
        "character_reference_images",
        metadata,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("character_id", sa.String(length=36), sa.ForeignKey("character_definitions.id"), nullable=False),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("generation_run_id", sa.String(length=36), sa.ForeignKey("workflow_runs.id"), nullable=False),
        sa.Column(
            "model_invocation_id",
            sa.String(length=36),
            sa.ForeignKey("model_invocations.id", use_alter=True, name="fk_character_image_invocation"),
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("prompt_snapshot", sa.Text(), nullable=False),
        sa.Column("image_url", sa.Text()),
        sa.Column("generation_status", run_status, nullable=False),
        sa.Column("review_status", review_status, nullable=False),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("character_id", "version", name="uq_character_reference_image_version"),
    )
    sa.Index("ix_character_reference_images_character_id", character_reference_images.c.character_id)
    sa.Index("ix_character_reference_images_project_id", character_reference_images.c.project_id)
    sa.Index("ix_character_reference_images_generation_run_id", character_reference_images.c.generation_run_id)
    sa.Index(
        "uq_locked_character_reference_image",
        character_reference_images.c.character_id,
        unique=True,
        postgresql_where=sa.text("review_status = 'LOCKED'"),
        sqlite_where=sa.text("review_status = 'LOCKED'"),
    )

    scene_reference_images = sa.Table(
        "scene_reference_images",
        metadata,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("scene_id", sa.String(length=36), sa.ForeignKey("scene_definitions.id"), nullable=False),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("generation_run_id", sa.String(length=36), sa.ForeignKey("workflow_runs.id"), nullable=False),
        sa.Column(
            "model_invocation_id",
            sa.String(length=36),
            sa.ForeignKey("model_invocations.id", use_alter=True, name="fk_scene_image_invocation"),
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("prompt_snapshot", sa.Text(), nullable=False),
        sa.Column("image_url", sa.Text()),
        sa.Column("generation_status", run_status, nullable=False),
        sa.Column("review_status", review_status, nullable=False),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("scene_id", "version", name="uq_scene_reference_image_version"),
    )
    sa.Index("ix_scene_reference_images_scene_id", scene_reference_images.c.scene_id)
    sa.Index("ix_scene_reference_images_project_id", scene_reference_images.c.project_id)
    sa.Index("ix_scene_reference_images_generation_run_id", scene_reference_images.c.generation_run_id)
    sa.Index(
        "uq_locked_scene_reference_image",
        scene_reference_images.c.scene_id,
        unique=True,
        postgresql_where=sa.text("review_status = 'LOCKED'"),
        sqlite_where=sa.text("review_status = 'LOCKED'"),
    )

    shot_plans = sa.Table(
        "shot_plans",
        metadata,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("director_plan_id", sa.String(length=36), sa.ForeignKey("director_plans.id"), nullable=False),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("shot_number", sa.Integer(), nullable=False),
        sa.Column("action_description", sa.Text(), nullable=False),
        sa.Column("camera_description", sa.Text(), nullable=False),
        sa.Column("duration_seconds", sa.Numeric(6, 2), nullable=False),
        sa.Column("video_action_prompt", sa.Text(), nullable=False),
        sa.Column(
            "locked_keyframe_id",
            sa.String(length=36),
            sa.ForeignKey("shot_keyframes.id", use_alter=True, name="fk_shot_locked_keyframe"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("director_plan_id", "shot_number", name="uq_shot_plan_number"),
    )
    sa.Index("ix_shot_plans_director_plan_id", shot_plans.c.director_plan_id)
    sa.Index("ix_shot_plans_project_id", shot_plans.c.project_id)

    shot_asset_bindings = sa.Table(
        "shot_asset_bindings",
        metadata,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("shot_id", sa.String(length=36), sa.ForeignKey("shot_plans.id"), nullable=False),
        sa.Column("character_id", sa.String(length=36), sa.ForeignKey("character_definitions.id")),
        sa.Column("character_reference_image_id", sa.String(length=36), sa.ForeignKey("character_reference_images.id")),
        sa.Column("scene_id", sa.String(length=36), sa.ForeignKey("scene_definitions.id"), nullable=False),
        sa.Column("scene_reference_image_id", sa.String(length=36), sa.ForeignKey("scene_reference_images.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "character_id IS NULL OR character_reference_image_id IS NOT NULL",
            name="ck_shot_character_image_required",
        ),
    )
    sa.Index("ix_shot_asset_bindings_shot_id", shot_asset_bindings.c.shot_id)

    shot_keyframes = sa.Table(
        "shot_keyframes",
        metadata,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("shot_id", sa.String(length=36), sa.ForeignKey("shot_plans.id"), nullable=False),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("generation_run_id", sa.String(length=36), sa.ForeignKey("workflow_runs.id"), nullable=False),
        sa.Column(
            "model_invocation_id",
            sa.String(length=36),
            sa.ForeignKey("model_invocations.id", use_alter=True, name="fk_keyframe_invocation"),
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("prompt_snapshot", sa.Text(), nullable=False),
        sa.Column("image_url", sa.Text()),
        sa.Column("input_asset_snapshot", sa.JSON(), nullable=False),
        sa.Column("generation_status", run_status, nullable=False),
        sa.Column("review_status", review_status, nullable=False),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("shot_id", "version", name="uq_shot_keyframe_version"),
    )
    sa.Index("ix_shot_keyframes_shot_id", shot_keyframes.c.shot_id)
    sa.Index("ix_shot_keyframes_project_id", shot_keyframes.c.project_id)
    sa.Index("ix_shot_keyframes_generation_run_id", shot_keyframes.c.generation_run_id)
    sa.Index(
        "uq_locked_shot_keyframe",
        shot_keyframes.c.shot_id,
        unique=True,
        postgresql_where=sa.text("review_status = 'LOCKED'"),
        sqlite_where=sa.text("review_status = 'LOCKED'"),
    )

    model_slots = sa.Table(
        "model_slots",
        metadata,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("slot_key", sa.String(length=80), nullable=False, unique=True),
        sa.Column("capability", sa.String(length=80), nullable=False),
        sa.Column("selection_mode", model_selection_mode, nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    model_slot_profile_bindings = sa.Table(
        "model_slot_profile_bindings",
        metadata,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("slot_id", sa.String(length=36), sa.ForeignKey("model_slots.id"), nullable=False),
        sa.Column("model_profile_id", sa.String(length=36), sa.ForeignKey("model_profiles.id"), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("weight", sa.Numeric(8, 4)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("slot_id", "model_profile_id", name="uq_slot_profile_binding"),
    )
    sa.Index("ix_model_slot_profile_bindings_slot_id", model_slot_profile_bindings.c.slot_id)
    sa.Index("ix_model_slot_profile_bindings_model_profile_id", model_slot_profile_bindings.c.model_profile_id)

    prompt_templates = sa.Table(
        "prompt_templates",
        metadata,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("task_type", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("variables_schema", sa.JSON(), nullable=False),
        sa.Column("status", prompt_template_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_type", "name", "version", name="uq_prompt_template_version"),
    )
    sa.Index("ix_prompt_templates_task_type", prompt_templates.c.task_type)

    model_invocations = sa.Table(
        "model_invocations",
        metadata,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("workflow_run_id", sa.String(length=36), sa.ForeignKey("workflow_runs.id")),
        sa.Column("workflow_step_id", sa.String(length=36), sa.ForeignKey("workflow_steps.id")),
        sa.Column("model_slot_id", sa.String(length=36), sa.ForeignKey("model_slots.id"), nullable=False),
        sa.Column("model_profile_id", sa.String(length=36), sa.ForeignKey("model_profiles.id"), nullable=False),
        sa.Column("prompt_template_id", sa.String(length=36), sa.ForeignKey("prompt_templates.id")),
        sa.Column("task_type", sa.String(length=80), nullable=False),
        sa.Column("model_profile_snapshot", sa.JSON(), nullable=False),
        sa.Column("prompt_snapshot", sa.JSON(), nullable=False),
        sa.Column("input_snapshot", sa.JSON(), nullable=False),
        sa.Column("output_reference", sa.JSON()),
        sa.Column("provider_task_id", sa.String(length=255)),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("media_units", sa.JSON(), nullable=False),
        sa.Column("cost_amount", sa.Numeric(14, 6)),
        sa.Column("currency", sa.String(length=12), nullable=False),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("status", run_status, nullable=False),
        sa.Column("error_code", sa.String(length=120)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    sa.Index("ix_model_invocations_project_id", model_invocations.c.project_id)
    sa.Index("ix_model_invocations_workflow_run_id", model_invocations.c.workflow_run_id)
    sa.Index("ix_model_invocations_workflow_step_id", model_invocations.c.workflow_step_id)
    sa.Index("ix_model_invocations_model_slot_id", model_invocations.c.model_slot_id)
    sa.Index("ix_model_invocations_model_profile_id", model_invocations.c.model_profile_id)
    sa.Index("ix_model_invocations_prompt_template_id", model_invocations.c.prompt_template_id)
    sa.Index("ix_model_invocation_profile_task", model_invocations.c.model_profile_id, model_invocations.c.task_type, model_invocations.c.created_at)
    sa.Index("ix_model_invocation_project", model_invocations.c.project_id, model_invocations.c.created_at)

    model_quality_evaluations = sa.Table(
        "model_quality_evaluations",
        metadata,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("model_profile_id", sa.String(length=36), sa.ForeignKey("model_profiles.id"), nullable=False),
        sa.Column("prompt_template_id", sa.String(length=36), sa.ForeignKey("prompt_templates.id")),
        sa.Column("task_type", sa.String(length=80), nullable=False),
        sa.Column("scenario", sa.String(length=160), nullable=False),
        sa.Column("aggregation_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("aggregation_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("success_rate", sa.Numeric(8, 4), nullable=False),
        sa.Column("average_cost_amount", sa.Numeric(14, 6)),
        sa.Column("average_latency_ms", sa.Integer()),
        sa.Column("average_human_score", sa.Numeric(5, 2)),
        sa.Column("adoption_rate", sa.Numeric(8, 4)),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    sa.Index("ix_model_quality_evaluations_model_profile_id", model_quality_evaluations.c.model_profile_id)
    sa.Index("ix_model_quality_evaluations_prompt_template_id", model_quality_evaluations.c.prompt_template_id)
    sa.Index("ix_model_quality_evaluations_task_type", model_quality_evaluations.c.task_type)

    video_clip_asset_bindings = sa.Table(
        "video_clip_asset_bindings",
        metadata,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("video_clip_id", sa.String(length=36), sa.ForeignKey("video_clips.id"), nullable=False),
        sa.Column("asset_type", sa.String(length=40), nullable=False),
        sa.Column("character_reference_image_id", sa.String(length=36), sa.ForeignKey("character_reference_images.id")),
        sa.Column("scene_reference_image_id", sa.String(length=36), sa.ForeignKey("scene_reference_images.id")),
        sa.Column("shot_keyframe_id", sa.String(length=36), sa.ForeignKey("shot_keyframes.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(asset_type = 'CHARACTER_REFERENCE' AND character_reference_image_id IS NOT NULL "
            "AND scene_reference_image_id IS NULL AND shot_keyframe_id IS NULL) OR "
            "(asset_type = 'SCENE_REFERENCE' AND character_reference_image_id IS NULL "
            "AND scene_reference_image_id IS NOT NULL AND shot_keyframe_id IS NULL) OR "
            "(asset_type = 'SHOT_KEYFRAME' AND character_reference_image_id IS NULL "
            "AND scene_reference_image_id IS NULL AND shot_keyframe_id IS NOT NULL)",
            name="ck_video_clip_asset_kind",
        ),
    )
    sa.Index("ix_video_clip_asset_bindings_video_clip_id", video_clip_asset_bindings.c.video_clip_id)

    return metadata, [
        workflow_definitions,
        project_production_states,
        reference_analyses,
        review_decisions,
        story_generation_batches,
        story_proposals,
        director_plans,
        character_definitions,
        scene_definitions,
        character_reference_images,
        scene_reference_images,
        shot_plans,
        shot_asset_bindings,
        shot_keyframes,
        model_slots,
        model_slot_profile_bindings,
        prompt_templates,
        model_invocations,
        model_quality_evaluations,
        video_clip_asset_bindings,
    ]


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

    # 仅创建 V1 新表；初始表仍由 0001 的固定表名清单创建。这里必须使用本修订
    # 固定快照，不能读取会随 ORM 演进的 Base.metadata。
    metadata, new_tables = _v1_foundation_metadata()
    metadata.create_all(bind=op.get_bind(), tables=new_tables)

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

    metadata, new_tables = _v1_foundation_metadata()
    metadata.drop_all(bind=op.get_bind(), tables=list(reversed(new_tables)))
