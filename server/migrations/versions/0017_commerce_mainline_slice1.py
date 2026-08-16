"""Connect V1 reference analysis to the Commerce Slice 1 mainline.

Revision ID: 0017_commerce_mainline_slice1
Revises: 0016_commerce_phase3_integrity_hardening
Create Date: 2026-08-12

This migration is deliberately additive.  It does not redirect old V1 story
proposals or existing Commerce StoryRuns.  New rows record the frozen bridge
from a completed V1 ReferenceAnalysis to exactly ten commerce ideas and, once
selected, to the existing Commerce StoryRun workflow.
"""

from alembic import op
import sqlalchemy as sa


revision = "0017_commerce_mainline_slice1"
down_revision = "0016_commerce_phase3_integrity_hardening"
branch_labels = None
depends_on = None


def _enum(*values: str, name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def upgrade() -> None:
    op.create_table(
        "commerce_reference_intakes",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("reference_analysis_id", sa.String(length=36), nullable=False),
        sa.Column("script_asset_id", sa.String(length=36), nullable=False),
        sa.Column("script_analysis_version_id", sa.String(length=36), nullable=False),
        sa.Column("product_asset_id", sa.String(length=36), nullable=False),
        sa.Column("product_analysis_version_id", sa.String(length=36), nullable=False),
        sa.Column("product_asset_version_id", sa.String(length=36), nullable=False),
        sa.Column("input_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reference_analysis_id"], ["reference_analyses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["script_asset_id"], ["script_assets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["script_analysis_version_id"], ["script_analysis_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_asset_id"], ["product_assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_analysis_version_id"], ["product_analysis_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_asset_version_id"], ["product_asset_versions.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("reference_analysis_id", name="uq_commerce_reference_intake_analysis"),
        sa.UniqueConstraint("script_analysis_version_id", name="uq_commerce_reference_intake_script_analysis"),
        sa.UniqueConstraint("product_asset_version_id", name="uq_commerce_reference_intake_product_version"),
    )
    op.create_index("ix_commerce_reference_intakes_project_id", "commerce_reference_intakes", ["project_id"])
    op.create_index("ix_commerce_reference_intakes_reference_analysis_id", "commerce_reference_intakes", ["reference_analysis_id"])
    op.create_index("ix_commerce_reference_intakes_script_asset_id", "commerce_reference_intakes", ["script_asset_id"])
    op.create_index("ix_commerce_reference_intakes_product_asset_id", "commerce_reference_intakes", ["product_asset_id"])

    op.create_table(
        "commerce_creative_batches",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("reference_intake_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_run_id", sa.String(length=36), nullable=False),
        sa.Column("batch_number", sa.Integer(), nullable=False),
        sa.Column("status", _enum("PENDING", "RUNNING", "SUCCEEDED", "FAILED", name="commercecreativebatchstatus"), nullable=False, server_default="PENDING"),
        sa.Column("input_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("model_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("prompt_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("raw_response", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("structured_response", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reference_intake_id"], ["commerce_reference_intakes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", "batch_number", name="uq_commerce_creative_batch_project_number"),
        sa.UniqueConstraint("workflow_run_id", name="uq_commerce_creative_batch_workflow_run"),
        sa.CheckConstraint("batch_number >= 1", name="ck_commerce_creative_batch_number_positive"),
    )
    op.create_index("ix_commerce_creative_batches_project_id", "commerce_creative_batches", ["project_id"])
    op.create_index("ix_commerce_creative_batches_reference_intake_id", "commerce_creative_batches", ["reference_intake_id"])
    op.create_index("ix_commerce_creative_batches_workflow_run_id", "commerce_creative_batches", ["workflow_run_id"])

    op.create_table(
        "commerce_creative_ideas",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("model_invocation_id", sa.String(length=36), nullable=True),
        sa.Column("topic_candidate_id", sa.String(length=36), nullable=True),
        sa.Column("candidate_number", sa.Integer(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("status", _enum("CANDIDATE", "SELECTED", "REJECTED", name="commercecreativeideastatus"), nullable=False, server_default="CANDIDATE"),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["commerce_creative_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_invocation_id"], ["model_invocations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["topic_candidate_id"], ["topic_candidates.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("batch_id", "candidate_number", name="uq_commerce_creative_idea_batch_number"),
        sa.UniqueConstraint("topic_candidate_id", name="uq_commerce_creative_idea_topic_candidate"),
        sa.CheckConstraint("candidate_number >= 1 AND candidate_number <= 10", name="ck_commerce_creative_idea_number"),
    )
    op.create_index("ix_commerce_creative_ideas_batch_id", "commerce_creative_ideas", ["batch_id"])
    op.create_index("ix_commerce_creative_ideas_project_id", "commerce_creative_ideas", ["project_id"])
    op.create_index("ix_commerce_creative_ideas_model_invocation_id", "commerce_creative_ideas", ["model_invocation_id"])

    op.create_table(
        "commerce_story_run_inputs",
        sa.Column("story_run_id", sa.String(length=36), primary_key=True),
        sa.Column("creative_batch_id", sa.String(length=36), nullable=False),
        sa.Column("creative_idea_id", sa.String(length=36), nullable=False),
        sa.Column("reference_analysis_id", sa.String(length=36), nullable=False),
        sa.Column("script_analysis_version_id", sa.String(length=36), nullable=False),
        sa.Column("product_asset_version_id", sa.String(length=36), nullable=False),
        sa.Column("input_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["story_run_id"], ["story_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["creative_batch_id"], ["commerce_creative_batches.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["creative_idea_id"], ["commerce_creative_ideas.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reference_analysis_id"], ["reference_analyses.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["script_analysis_version_id"], ["script_analysis_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_asset_version_id"], ["product_asset_versions.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("creative_idea_id", name="uq_commerce_story_run_input_idea"),
    )
    op.create_index("ix_commerce_story_run_inputs_creative_batch_id", "commerce_story_run_inputs", ["creative_batch_id"])
    op.create_index("ix_commerce_story_run_inputs_script_analysis_version_id", "commerce_story_run_inputs", ["script_analysis_version_id"])
    op.create_index("ix_commerce_story_run_inputs_product_asset_version_id", "commerce_story_run_inputs", ["product_asset_version_id"])


def downgrade() -> None:
    op.drop_index("ix_commerce_story_run_inputs_product_asset_version_id", table_name="commerce_story_run_inputs")
    op.drop_index("ix_commerce_story_run_inputs_script_analysis_version_id", table_name="commerce_story_run_inputs")
    op.drop_index("ix_commerce_story_run_inputs_creative_batch_id", table_name="commerce_story_run_inputs")
    op.drop_table("commerce_story_run_inputs")
    op.drop_index("ix_commerce_creative_ideas_model_invocation_id", table_name="commerce_creative_ideas")
    op.drop_index("ix_commerce_creative_ideas_project_id", table_name="commerce_creative_ideas")
    op.drop_index("ix_commerce_creative_ideas_batch_id", table_name="commerce_creative_ideas")
    op.drop_table("commerce_creative_ideas")
    op.drop_index("ix_commerce_creative_batches_workflow_run_id", table_name="commerce_creative_batches")
    op.drop_index("ix_commerce_creative_batches_reference_intake_id", table_name="commerce_creative_batches")
    op.drop_index("ix_commerce_creative_batches_project_id", table_name="commerce_creative_batches")
    op.drop_table("commerce_creative_batches")
    op.drop_index("ix_commerce_reference_intakes_product_asset_id", table_name="commerce_reference_intakes")
    op.drop_index("ix_commerce_reference_intakes_script_asset_id", table_name="commerce_reference_intakes")
    op.drop_index("ix_commerce_reference_intakes_reference_analysis_id", table_name="commerce_reference_intakes")
    op.drop_index("ix_commerce_reference_intakes_project_id", table_name="commerce_reference_intakes")
    op.drop_table("commerce_reference_intakes")
