"""Add versioned model parameter capability configuration.

Revision ID: 0022_model_parameter_capabilities
Revises: 0021_commerce_story_run_rerun
Create Date: 2026-08-17

Existing Profile rows intentionally receive an empty JSON object.  Runtime/API
code derives a compatibility capability view from each Adapter's already
validated ``provider_config``; this migration never changes keys, slots,
bindings, historical calls or provider settings.
"""

from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0022_model_parameter_capabilities"
down_revision = "0021_commerce_story_run_rerun"
branch_labels = None
depends_on = None


def _has_column(name: str) -> bool:
    if context.is_offline_mode():
        return False
    return name in {column["name"] for column in inspect(op.get_bind()).get_columns("model_profiles")}


def upgrade() -> None:
    if _has_column("parameter_config"):
        return
    op.add_column(
        "model_profiles",
        sa.Column("parameter_config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    if not context.is_offline_mode() and not _has_column("parameter_config"):
        return
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("model_profiles") as batch:
            batch.drop_column("parameter_config")
    else:
        op.drop_column("model_profiles", "parameter_config")
