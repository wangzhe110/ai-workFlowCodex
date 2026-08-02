"""新增模型小样本验收统计表。

Revision ID: 0002_model_evaluations
Revises: 0001_initial_schema
Create Date: 2026-08-02
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_model_evaluations"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保存按模型配置版本归属的成本、速度、成功率和质量评分汇总。"""

    op.create_table(
        "model_evaluations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("model_profile_id", sa.String(length=36), nullable=False),
        sa.Column("scenario", sa.String(length=120), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("total_cost_yuan", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("average_latency_seconds", sa.Float(), nullable=False),
        sa.Column("quality_score", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["model_profile_id"], ["model_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_model_evaluations_model_profile_id", "model_evaluations", ["model_profile_id"], unique=False)


def downgrade() -> None:
    """删除评测汇总；生产回滚前必须先导出业务需要的历史统计。"""

    op.drop_index("ix_model_evaluations_model_profile_id", table_name="model_evaluations")
    op.drop_table("model_evaluations")
