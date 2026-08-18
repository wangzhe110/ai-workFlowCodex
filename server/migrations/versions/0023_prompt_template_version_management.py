"""Add immutable system Prompt template versions.

Revision ID: 0023_prompt_template_version_management
Revises: 0022_model_parameter_capabilities
Create Date: 2026-08-18

The existing ``prompt_templates`` table is intentionally retained for legacy V1
history.  The new catalog/version pair manages system-level prompt instructions;
project business outputs such as ``video_prompt_versions`` are untouched.
"""

from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0023_prompt_template_version_management"
down_revision = "0022_model_parameter_capabilities"
branch_labels = None
depends_on = None


def _enum(*values: str, name: str) -> sa.Enum:
    """沿用 Commerce 迁移的 VARCHAR + CHECK，SQLite/PostgreSQL 语义一致。"""

    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def _has_table(name: str) -> bool:
    if context.is_offline_mode():
        return False
    return name in set(inspect(op.get_bind()).get_table_names())


def _has_column(table: str, name: str) -> bool:
    if context.is_offline_mode() or not _has_table(table):
        return False
    return name in {column["name"] for column in inspect(op.get_bind()).get_columns(table)}


def _has_references() -> bool:
    if context.is_offline_mode() or not _has_column("model_invocations", "prompt_template_version_id"):
        return False
    return bool(
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM model_invocations WHERE prompt_template_version_id IS NOT NULL LIMIT 1"))
        .first()
    )


def upgrade() -> None:
    if not _has_table("prompt_template_definitions"):
        op.create_table(
            "prompt_template_definitions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("prompt_key", sa.String(length=120), nullable=False),
            sa.Column("display_name", sa.String(length=160), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("operation_key", sa.String(length=120), nullable=False),
            sa.Column("model_slot_key", sa.String(length=80), nullable=True),
            sa.Column("capability", sa.String(length=80), nullable=False),
            # ``active_version_id`` is checked atomically by the service. Adding a
            # cross-table FK here would form a create-order cycle on SQLite.
            sa.Column("active_version_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("prompt_key", name="uq_prompt_template_definition_key"),
        )
        op.create_index("ix_prompt_template_definitions_prompt_key", "prompt_template_definitions", ["prompt_key"])
        op.create_index("ix_prompt_template_definitions_operation_key", "prompt_template_definitions", ["operation_key"])
        op.create_index("ix_prompt_template_definitions_model_slot_key", "prompt_template_definitions", ["model_slot_key"])
        op.create_index("ix_prompt_template_definitions_active_version_id", "prompt_template_definitions", ["active_version_id"])

    if not _has_table("prompt_template_versions"):
        op.create_table(
            "prompt_template_versions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("prompt_template_id", sa.String(length=36), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("status", _enum("DRAFT", "PUBLISHED", name="prompttemplateversionstatus"), nullable=False),
            sa.Column("system_template", sa.Text(), nullable=False),
            sa.Column("user_template", sa.Text(), nullable=False),
            sa.Column("allowed_variables", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("output_contract_key", sa.String(length=120), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("change_summary", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["prompt_template_id"], ["prompt_template_definitions.id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("prompt_template_id", "version", name="uq_prompt_template_definition_version"),
        )
        op.create_index("ix_prompt_template_versions_prompt_template_id", "prompt_template_versions", ["prompt_template_id"])
        op.create_index("ix_prompt_template_versions_content_hash", "prompt_template_versions", ["content_hash"])

    if not _has_column("model_invocations", "prompt_template_version_id"):
        column = sa.Column(
            "prompt_template_version_id",
            sa.String(length=36),
            nullable=True,
        )
        if op.get_bind().dialect.name == "sqlite":
            # SQLite 不能对现存表直接 ADD FOREIGN KEY，batch 会安全重建该表。
            with op.batch_alter_table("model_invocations") as batch:
                batch.add_column(column)
                batch.create_foreign_key(
                    "fk_model_invocations_prompt_template_version_id",
                    "prompt_template_versions",
                    ["prompt_template_version_id"],
                    ["id"],
                    ondelete="RESTRICT",
                )
        else:
            op.add_column("model_invocations", column)
            op.create_foreign_key(
                "fk_model_invocations_prompt_template_version_id",
                "model_invocations",
                "prompt_template_versions",
                ["prompt_template_version_id"],
                ["id"],
                ondelete="RESTRICT",
            )
        op.create_index("ix_model_invocations_prompt_template_version_id", "model_invocations", ["prompt_template_version_id"])


def downgrade() -> None:
    # Historical ModelInvocation rows are an audit record. A downgrade which
    # silently removes their new version pointer would make schema revision and
    # actual traceability disagree, so it is explicitly refused once used.
    if _has_references():
        raise RuntimeError("0023 已被模型调用引用，拒绝危险降级以保护 Prompt 审计历史")

    if _has_column("model_invocations", "prompt_template_version_id"):
        if op.get_bind().dialect.name == "sqlite":
            # batch 重建表时会复制当前索引；必须先移除依赖待删列的索引，否则
            # SQLite 会在新表上尝试创建一个引用不存在列的旧索引。
            op.drop_index("ix_model_invocations_prompt_template_version_id", table_name="model_invocations")
            with op.batch_alter_table("model_invocations") as batch:
                batch.drop_constraint("fk_model_invocations_prompt_template_version_id", type_="foreignkey")
                batch.drop_column("prompt_template_version_id")
        else:
            op.drop_constraint("fk_model_invocations_prompt_template_version_id", "model_invocations", type_="foreignkey")
            op.drop_index("ix_model_invocations_prompt_template_version_id", table_name="model_invocations")
            op.drop_column("model_invocations", "prompt_template_version_id")

    if _has_table("prompt_template_versions"):
        op.drop_index("ix_prompt_template_versions_content_hash", table_name="prompt_template_versions")
        op.drop_index("ix_prompt_template_versions_prompt_template_id", table_name="prompt_template_versions")
        op.drop_table("prompt_template_versions")
    if _has_table("prompt_template_definitions"):
        op.drop_index("ix_prompt_template_definitions_active_version_id", table_name="prompt_template_definitions")
        op.drop_index("ix_prompt_template_definitions_model_slot_key", table_name="prompt_template_definitions")
        op.drop_index("ix_prompt_template_definitions_operation_key", table_name="prompt_template_definitions")
        op.drop_index("ix_prompt_template_definitions_prompt_key", table_name="prompt_template_definitions")
        op.drop_table("prompt_template_definitions")
