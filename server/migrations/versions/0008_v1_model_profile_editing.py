"""新增 V1 模型配置草稿/历史状态，支持安全编辑与复制新版本。

Revision ID: 0008_v1_model_profile_editing
Revises: 0007_v1_production_integrity
Create Date: 2026-08-06

本迁移只新增状态列并回填现有槽位绑定，不删除或改写任何模型配置、调用或历史资产。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0008_v1_model_profile_editing"
down_revision = "0007_v1_production_integrity"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    """读取已有列，允许已部署环境安全重复升级。"""

    return {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    """为现有模型版本补充生命周期状态。"""

    if "profile_status" not in _column_names("model_profiles"):
        op.add_column(
            "model_profiles",
            sa.Column("profile_status", sa.String(length=20), nullable=False, server_default="DRAFT"),
        )

    # 已绑定且当前启用的版本是现有 V1 生产配置，应显示为 ACTIVE；其他历史记录保留
    # DRAFT，绝不删除、覆盖或重新编号。
    op.execute(
        """
        UPDATE model_profiles
        SET profile_status = 'ACTIVE'
        WHERE id IN (
            SELECT model_profile_id
            FROM model_slot_profile_bindings
            WHERE is_enabled = TRUE
        )
        """
    )


def downgrade() -> None:
    """仅移除新增状态列；生产降级前必须先备份。"""

    if "profile_status" not in _column_names("model_profiles"):
        return
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("model_profiles") as batch:
            batch.drop_column("profile_status")
    else:
        op.drop_column("model_profiles", "profile_status")
