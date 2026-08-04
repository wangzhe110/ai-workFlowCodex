"""补齐 V1 人工质量评分与模型质量报表币种。

Revision ID: 0006_v1_quality_review_metrics
Revises: 0005_v1_asset_ownership_and_versions
Create Date: 2026-08-04

审核记录原本只保存“锁定/采用/驳回”决定。本迁移新增可选 1 至 10 分主观质量
评分；历史审核不强行补分，报表会将其显示为“暂无评分”。同时为质量报表快照保存
成本币种，避免将不同币种的金额错误相加或平均。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0006_v1_quality_review_metrics"
down_revision = "0005_v1_asset_ownership_and_versions"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    """读取列名，使已手工补过字段的部署可安全重复升级。"""

    return {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    """兼容 SQLite 与 PostgreSQL 的增量加列写法。"""

    if column.name in _columns(table_name):
        return
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(table_name) as batch:
            batch.add_column(column)
    else:
        op.add_column(table_name, column)


def upgrade() -> None:
    """新增评分与币种列；不修改任何历史锁定资产或审核决定。"""

    _add_column_if_missing("review_decisions", sa.Column("quality_score", sa.Integer(), nullable=True))
    _add_column_if_missing(
        "model_quality_evaluations",
        sa.Column("currency", sa.String(length=12), nullable=False, server_default="CNY"),
    )


def downgrade() -> None:
    """仅移除本迁移新增的统计辅助列，不触碰生产资产数据。"""

    for table_name, column_name in (
        ("model_quality_evaluations", "currency"),
        ("review_decisions", "quality_score"),
    ):
        if column_name not in _columns(table_name):
            continue
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table(table_name) as batch:
                batch.drop_column(column_name)
        else:
            op.drop_column(table_name, column_name)
