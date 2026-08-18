"""Add internal Model Lab experiment definitions and immutable variants.

Revision ID: 0025_model_lab
Revises: 0024_commerce_workflow_presets
Create Date: 2026-08-18

Execution facts intentionally remain in workflow_runs, workflow_steps and
model_invocations.  The new tables only group a comparison, freeze the selected
inputs/configuration and store human evaluation.  No history is backfilled.
"""

from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0025_model_lab"
down_revision = "0024_commerce_workflow_presets"
branch_labels = None
depends_on = None


EXPERIMENTS = "model_experiments"
VARIANTS = "model_experiment_variants"
EVALUATIONS = "model_experiment_evaluations"


def _offline() -> bool:
    return context.is_offline_mode()


def _has_table(name: str) -> bool:
    return False if _offline() else name in set(inspect(op.get_bind()).get_table_names())


def _has_rows(name: str) -> bool:
    if _offline() or not _has_table(name):
        return False
    return bool(op.get_bind().execute(sa.text(f"SELECT 1 FROM {name} LIMIT 1")).first())


def _enum(*values: str, name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def upgrade() -> None:
    if not _has_table(EXPERIMENTS):
        op.create_table(
            EXPERIMENTS,
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("project_id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("operation_key", sa.String(length=120), nullable=False),
            sa.Column("model_slot_key", sa.String(length=80), nullable=False),
            sa.Column("capability", _enum("text", "image", "video", name="modelexperimentcapability"), nullable=False),
            sa.Column("comparison_mode", _enum("MODEL_ONLY", "PROMPT_ONLY", "PARAMETER_ONLY", "CUSTOM", "NATIVE_PRESET", name="modelexperimentcomparisonmode"), nullable=False),
            sa.Column("input_source_type", sa.String(length=40), nullable=False),
            sa.Column("sanitized_input_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("input_hash", sa.String(length=64), nullable=False),
            sa.Column("max_create_calls", sa.Integer(), nullable=False),
            sa.Column("preflight_hash", sa.String(length=64), nullable=True),
            sa.Column("preflight_variant_hash", sa.String(length=64), nullable=True),
            sa.Column("preflight_expected_create_calls", sa.Integer(), nullable=True),
            sa.Column("preflight_checked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", _enum("DRAFT", "READY", "RUNNING", "PAUSED", "COMPLETED", "FAILED", "ARCHIVED", name="modelexperimentstatus"), nullable=False),
            sa.Column("workflow_run_id", sa.String(length=36), nullable=True),
            sa.Column("winner_variant_id", sa.String(length=36), nullable=True),
            sa.Column("promotion_metadata", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("workflow_run_id", name="uq_model_experiment_workflow_run"),
            sa.CheckConstraint("max_create_calls >= 1", name="ck_model_experiment_max_calls_positive"),
        )
        op.create_index("ix_model_experiments_project_id", EXPERIMENTS, ["project_id"])
        op.create_index("ix_model_experiments_operation_key", EXPERIMENTS, ["operation_key"])
        op.create_index("ix_model_experiments_model_slot_key", EXPERIMENTS, ["model_slot_key"])
        op.create_index("ix_model_experiments_input_hash", EXPERIMENTS, ["input_hash"])
        op.create_index("ix_model_experiments_preflight_hash", EXPERIMENTS, ["preflight_hash"])
        op.create_index("ix_model_experiments_winner_variant_id", EXPERIMENTS, ["winner_variant_id"])

    if not _has_table(VARIANTS):
        op.create_table(
            VARIANTS,
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("experiment_id", sa.String(length=36), nullable=False),
            sa.Column("label", sa.String(length=120), nullable=False),
            sa.Column("model_profile_id", sa.String(length=36), nullable=False),
            sa.Column("model_profile_version", sa.Integer(), nullable=False),
            sa.Column("prompt_template_version_id", sa.String(length=36), nullable=False),
            sa.Column("parameter_preset", sa.String(length=20), nullable=False),
            sa.Column("requested_overrides", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("effective_parameters", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("model_profile_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("prompt_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("input_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("repeat_index", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("status", _enum("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", name="runstatus"), nullable=False),
            sa.Column("workflow_step_id", sa.String(length=36), nullable=True),
            sa.Column("model_invocation_id", sa.String(length=36), nullable=True),
            sa.Column("provider_task_id", sa.String(length=255), nullable=True),
            sa.Column("output_reference", sa.JSON(), nullable=True),
            sa.Column("metrics", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("error_code", sa.String(length=120), nullable=True),
            sa.Column("sanitized_error_summary", sa.Text(), nullable=True),
            sa.Column("provider_create_post_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("recovered_from_variant_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["experiment_id"], [f"{EXPERIMENTS}.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["model_profile_id"], ["model_profiles.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["prompt_template_version_id"], ["prompt_template_versions.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["workflow_step_id"], ["workflow_steps.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["model_invocation_id"], ["model_invocations.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("experiment_id", "label", "repeat_index", name="uq_model_experiment_variant_repeat"),
            sa.UniqueConstraint("workflow_step_id", name="uq_model_experiment_variant_workflow_step"),
            sa.UniqueConstraint("model_invocation_id", name="uq_model_experiment_variant_invocation"),
            sa.CheckConstraint("repeat_index >= 1 AND repeat_index <= 3", name="ck_model_experiment_repeat_range"),
            sa.CheckConstraint("provider_create_post_count >= 0", name="ck_model_experiment_post_count_nonnegative"),
        )
        op.create_index("ix_model_experiment_variants_experiment_id", VARIANTS, ["experiment_id"])
        op.create_index("ix_model_experiment_variants_model_profile_id", VARIANTS, ["model_profile_id"])
        op.create_index("ix_model_experiment_variants_prompt_template_version_id", VARIANTS, ["prompt_template_version_id"])
        op.create_index("ix_model_experiment_variants_recovered_from_variant_id", VARIANTS, ["recovered_from_variant_id"])
        op.create_index("ix_model_experiment_variant_experiment_status", VARIANTS, ["experiment_id", "status"])

    if not _has_table(EVALUATIONS):
        op.create_table(
            EVALUATIONS,
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("experiment_id", sa.String(length=36), nullable=False),
            sa.Column("variant_id", sa.String(length=36), nullable=False),
            sa.Column("scores", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("notes", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("is_winner", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["experiment_id"], [f"{EXPERIMENTS}.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["variant_id"], [f"{VARIANTS}.id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("experiment_id", "variant_id", name="uq_model_experiment_evaluation_variant"),
        )
        op.create_index("ix_model_experiment_evaluations_experiment_id", EVALUATIONS, ["experiment_id"])
        op.create_index("ix_model_experiment_evaluations_variant_id", EVALUATIONS, ["variant_id"])
        op.create_index(
            "uq_model_experiment_winner",
            EVALUATIONS,
            ["experiment_id"],
            unique=True,
            postgresql_where=sa.text("is_winner = true"),
            sqlite_where=sa.text("is_winner = 1"),
        )


def downgrade() -> None:
    # 已执行的实验是模型比较及人工选择的审计事实，禁止危险降级默默删除它们。
    if _has_rows(EXPERIMENTS):
        raise RuntimeError("0025 已存在 Model Lab 实验记录，拒绝危险降级以保护审计")
    if _offline() or _has_table(EVALUATIONS):
        op.drop_index("uq_model_experiment_winner", table_name=EVALUATIONS)
        op.drop_index("ix_model_experiment_evaluations_variant_id", table_name=EVALUATIONS)
        op.drop_index("ix_model_experiment_evaluations_experiment_id", table_name=EVALUATIONS)
        op.drop_table(EVALUATIONS)
    if _offline() or _has_table(VARIANTS):
        op.drop_index("ix_model_experiment_variant_experiment_status", table_name=VARIANTS)
        op.drop_index("ix_model_experiment_variants_recovered_from_variant_id", table_name=VARIANTS)
        op.drop_index("ix_model_experiment_variants_prompt_template_version_id", table_name=VARIANTS)
        op.drop_index("ix_model_experiment_variants_model_profile_id", table_name=VARIANTS)
        op.drop_index("ix_model_experiment_variants_experiment_id", table_name=VARIANTS)
        op.drop_table(VARIANTS)
    if _offline() or _has_table(EXPERIMENTS):
        op.drop_index("ix_model_experiments_winner_variant_id", table_name=EXPERIMENTS)
        op.drop_index("ix_model_experiments_input_hash", table_name=EXPERIMENTS)
        op.drop_index("ix_model_experiments_preflight_hash", table_name=EXPERIMENTS)
        op.drop_index("ix_model_experiments_model_slot_key", table_name=EXPERIMENTS)
        op.drop_index("ix_model_experiments_operation_key", table_name=EXPERIMENTS)
        op.drop_index("ix_model_experiments_project_id", table_name=EXPERIMENTS)
        op.drop_table(EXPERIMENTS)
