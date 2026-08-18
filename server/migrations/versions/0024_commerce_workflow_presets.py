"""Add Commerce workflow presets and immutable StoryRun configuration freezes.

Revision ID: 0024_commerce_workflow_presets
Revises: 0023_prompt_template_version_management
Create Date: 2026-08-18

The preset catalog is system configuration, while the one-to-one StoryRun row
is the immutable execution snapshot.  Existing runs intentionally receive no
row: their historical Worker snapshots remain readable through the legacy
compatibility path and are never rewritten by this migration.
"""

from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0024_commerce_workflow_presets"
down_revision = "0023_prompt_template_version_management"
branch_labels = None
depends_on = None


PRESET_DEFINITIONS = "commerce_workflow_preset_definitions"
PRESET_VERSIONS = "commerce_workflow_preset_versions"
RUN_CONFIGS = "commerce_story_run_workflow_configs"


def _offline() -> bool:
    return context.is_offline_mode()


def _has_table(name: str) -> bool:
    return False if _offline() else name in set(inspect(op.get_bind()).get_table_names())


def _has_config_rows() -> bool:
    if _offline() or not _has_table(RUN_CONFIGS):
        return False
    return bool(op.get_bind().execute(sa.text(f"SELECT 1 FROM {RUN_CONFIGS} LIMIT 1")).first())


def _enum(*values: str, name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def upgrade() -> None:
    if not _has_table(PRESET_DEFINITIONS):
        op.create_table(
            PRESET_DEFINITIONS,
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("preset_key", sa.String(length=80), nullable=False),
            sa.Column("display_name", sa.String(length=160), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
            # 相同于 Prompt 目录：活动指针由同一事务服务校验，避免与版本表构成
            # SQLite create-order 循环。
            sa.Column("active_version_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("preset_key", name="uq_commerce_workflow_preset_key"),
        )
        op.create_index("ix_commerce_workflow_preset_definitions_preset_key", PRESET_DEFINITIONS, ["preset_key"])
        op.create_index("ix_commerce_workflow_preset_definitions_active_version_id", PRESET_DEFINITIONS, ["active_version_id"])

    if not _has_table(PRESET_VERSIONS):
        op.create_table(
            PRESET_VERSIONS,
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("preset_definition_id", sa.String(length=36), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("status", _enum("DRAFT", "PUBLISHED", name="commerceworkflowpresetversionstatus"), nullable=False),
            sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("change_summary", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["preset_definition_id"], [f"{PRESET_DEFINITIONS}.id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("preset_definition_id", "version", name="uq_commerce_workflow_preset_version"),
            sa.CheckConstraint("status IN ('DRAFT', 'PUBLISHED')", name="ck_commerce_workflow_preset_version_status"),
        )
        op.create_index("ix_commerce_workflow_preset_versions_preset_definition_id", PRESET_VERSIONS, ["preset_definition_id"])
        op.create_index("ix_commerce_workflow_preset_versions_content_hash", PRESET_VERSIONS, ["content_hash"])

    if not _has_table(RUN_CONFIGS):
        op.create_table(
            RUN_CONFIGS,
            sa.Column("story_run_id", sa.String(length=36), nullable=False),
            sa.Column("preset_definition_id", sa.String(length=36), nullable=True),
            sa.Column("preset_version_id", sa.String(length=36), nullable=True),
            sa.Column("preset_version", sa.Integer(), nullable=True),
            sa.Column("preset_content_hash", sa.String(length=64), nullable=True),
            sa.Column("requested_overrides", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("effective_workflow_config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("config_sources", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("estimates", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("model_bindings", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("prompt_templates", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["story_run_id"], ["story_runs.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["preset_definition_id"], [f"{PRESET_DEFINITIONS}.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["preset_version_id"], [f"{PRESET_VERSIONS}.id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("story_run_id"),
        )
        op.create_index("ix_commerce_story_run_workflow_configs_preset_definition_id", RUN_CONFIGS, ["preset_definition_id"])
        op.create_index("ix_commerce_story_run_workflow_configs_preset_version_id", RUN_CONFIGS, ["preset_version_id"])


def downgrade() -> None:
    # 配置快照是已经启动的运行审计的一部分。不得在降级时悄悄删除它，让 revision
    # 号看似回退但实际丢失可复现性；空库/隔离迁移仍可以完整往返。
    if _has_config_rows():
        raise RuntimeError("0024 已被 StoryRun 配置冻结引用，拒绝危险降级以保护运行审计")
    if _offline() or _has_table(RUN_CONFIGS):
        op.drop_index("ix_commerce_story_run_workflow_configs_preset_version_id", table_name=RUN_CONFIGS)
        op.drop_index("ix_commerce_story_run_workflow_configs_preset_definition_id", table_name=RUN_CONFIGS)
        op.drop_table(RUN_CONFIGS)
    if _offline() or _has_table(PRESET_VERSIONS):
        op.drop_index("ix_commerce_workflow_preset_versions_content_hash", table_name=PRESET_VERSIONS)
        op.drop_index("ix_commerce_workflow_preset_versions_preset_definition_id", table_name=PRESET_VERSIONS)
        op.drop_table(PRESET_VERSIONS)
    if _offline() or _has_table(PRESET_DEFINITIONS):
        op.drop_index("ix_commerce_workflow_preset_definitions_active_version_id", table_name=PRESET_DEFINITIONS)
        op.drop_index("ix_commerce_workflow_preset_definitions_preset_key", table_name=PRESET_DEFINITIONS)
        op.drop_table(PRESET_DEFINITIONS)
