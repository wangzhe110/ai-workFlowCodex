"""加固 V1 生产任务的版本指针、幂等键和视频子任务追溯。

Revision ID: 0007_v1_production_integrity
Revises: 0006_v1_quality_review_metrics
Create Date: 2026-08-04

本迁移不删除任何历史视频、模型调用或审核记录。它只补充“当前采用视频版本”
指针以及任务/供应商调用的幂等追溯字段，并用部分唯一索引阻止同项目同节点并发
创建多个活动 V1 任务。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0007_v1_production_integrity"
down_revision = "0006_v1_quality_review_metrics"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}


def _indexes(table_name: str) -> set[str]:
    return {item.get("name") for item in inspect(op.get_bind()).get_indexes(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name in _columns(table_name):
        return
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(table_name) as batch:
            batch.add_column(column)
    else:
        op.add_column(table_name, column)


def _cancel_duplicate_active_runs() -> None:
    """让遗留重复活动任务显式终止，避免新增唯一索引时静默选错收费任务。"""

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT project_id, workflow_key "
            "FROM workflow_runs WHERE workflow_key LIKE 'v1_%' "
            "AND status IN ('PENDING', 'RUNNING') GROUP BY project_id, workflow_key HAVING COUNT(*) > 1"
        )
    ).mappings().all()
    for row in rows:
        keep_id = bind.execute(
            sa.text(
                "SELECT id FROM workflow_runs WHERE project_id = :project_id AND workflow_key = :workflow_key "
                "AND status IN ('PENDING', 'RUNNING') ORDER BY created_at ASC, id ASC LIMIT 1"
            ),
            dict(row),
        ).scalar_one()
        bind.execute(
            sa.text(
                "UPDATE workflow_runs SET status = 'CANCELLED', finished_at = CURRENT_TIMESTAMP "
                "WHERE project_id = :project_id AND workflow_key = :workflow_key "
                "AND status IN ('PENDING', 'RUNNING') AND id <> :keep_id"
            ),
            {**dict(row), "keep_id": keep_id},
        )


def _backfill_selected_approved_clips() -> None:
    """旧数据只回填已审核通过的最新版本，绝不把 REJECTED 片段变成当前采用。"""

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id FROM shot_plans WHERE selected_video_clip_id IS NULL"
        )
    ).mappings().all()
    for row in rows:
        clip_id = bind.execute(
            sa.text(
                "SELECT id FROM video_clips WHERE shot_plan_id = :shot_id "
                "AND generation_status = 'SUCCEEDED' AND review_status = 'APPROVED' "
                "ORDER BY version DESC, created_at DESC LIMIT 1"
            ),
            {"shot_id": row["id"]},
        ).scalar()
        if clip_id:
            bind.execute(
                sa.text("UPDATE shot_plans SET selected_video_clip_id = :clip_id WHERE id = :shot_id"),
                {"clip_id": clip_id, "shot_id": row["id"]},
            )


def upgrade() -> None:
    _add_column_if_missing("workflow_runs", sa.Column("idempotency_key", sa.String(length=160), nullable=True))
    _add_column_if_missing("workflow_steps", sa.Column("idempotency_key", sa.String(length=160), nullable=True))
    _add_column_if_missing("workflow_steps", sa.Column("shot_plan_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("workflow_steps", sa.Column("video_clip_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("workflow_steps", sa.Column("provider_task_id", sa.String(length=255), nullable=True))
    _add_column_if_missing("video_clips", sa.Column("idempotency_key", sa.String(length=160), nullable=True))
    _add_column_if_missing("shot_plans", sa.Column("selected_video_clip_id", sa.String(length=36), nullable=True))
    _add_column_if_missing("model_invocations", sa.Column("idempotency_key", sa.String(length=160), nullable=True))

    _cancel_duplicate_active_runs()
    _backfill_selected_approved_clips()

    indexes = _indexes("workflow_runs")
    if "uq_v1_active_run_project_key" not in indexes:
        op.create_index(
            "uq_v1_active_run_project_key",
            "workflow_runs",
            ["project_id", "workflow_key"],
            unique=True,
            postgresql_where=sa.text("workflow_key LIKE 'v1_%' AND status IN ('PENDING', 'RUNNING')"),
            sqlite_where=sa.text("workflow_key LIKE 'v1_%' AND status IN ('PENDING', 'RUNNING')"),
        )
    if "uq_workflow_steps_idempotency_key" not in _indexes("workflow_steps"):
        op.create_index("uq_workflow_steps_idempotency_key", "workflow_steps", ["idempotency_key"], unique=True)
    if "ix_workflow_steps_shot_plan_id" not in _indexes("workflow_steps"):
        op.create_index("ix_workflow_steps_shot_plan_id", "workflow_steps", ["shot_plan_id"])
    if "ix_workflow_steps_video_clip_id" not in _indexes("workflow_steps"):
        op.create_index("ix_workflow_steps_video_clip_id", "workflow_steps", ["video_clip_id"])
    if "uq_video_clips_idempotency_key" not in _indexes("video_clips"):
        op.create_index("uq_video_clips_idempotency_key", "video_clips", ["idempotency_key"], unique=True)
    if "ix_shot_plans_selected_video_clip_id" not in _indexes("shot_plans"):
        op.create_index("ix_shot_plans_selected_video_clip_id", "shot_plans", ["selected_video_clip_id"])
    if "uq_model_invocations_idempotency_key" not in _indexes("model_invocations"):
        op.create_index("uq_model_invocations_idempotency_key", "model_invocations", ["idempotency_key"], unique=True)


def downgrade() -> None:
    raise RuntimeError("0007 保护生产任务幂等与视频版本选择；请从升级前备份恢复，禁止危险回退")
