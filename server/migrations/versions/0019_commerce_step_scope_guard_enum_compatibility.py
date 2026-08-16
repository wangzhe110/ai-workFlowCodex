"""Repair PostgreSQL Commerce step scope enum/VARCHAR comparison.

Revision ID: 0019_commerce_step_scope_guard_enum_compatibility
Revises: 0018_commerce_storyrun_production_slice2
Create Date: 2026-08-13

``commerce_workflow_steps.status`` is a historical VARCHAR sidecar while the
pre-existing ``workflow_steps.status`` column is PostgreSQL's native
``runstatus`` enum.  PostgreSQL does not implicitly compare those two types.
This revision replaces only the already-bound trigger function body, leaving
the function signature, trigger, scope predicates, error message, tables and
data untouched.
"""

from alembic import op


revision = "0019_commerce_step_scope_guard_enum_compatibility"
down_revision = "0018_commerce_storyrun_production_slice2"
branch_labels = None
depends_on = None


# Keep this statement as a single named constant so migration tests can compile
# and inspect the exact PostgreSQL function body without applying DDL manually.
POSTGRES_SCOPE_GUARD_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION commerce_workflow_step_scope_guard() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM commerce_workflow_links link
          JOIN workflow_runs run ON run.id = link.workflow_run_id
          JOIN story_runs story ON story.id = link.story_run_id
          JOIN workflow_steps step ON step.id = NEW.workflow_step_id
         WHERE link.workflow_run_id = NEW.workflow_run_id
           AND link.story_run_id = NEW.story_run_id
           AND run.workflow_key = 'commerce_story_run'
           AND run.project_id = story.project_id
           AND step.workflow_run_id = NEW.workflow_run_id
           AND step.step_key = NEW.stage
           AND step.attempt = NEW.attempt
           AND CAST(step.status AS TEXT) = NEW.status
    ) THEN
        RAISE EXCEPTION 'commerce workflow step scope invalid';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    """Install the safe comparison on deployed PostgreSQL trigger functions."""

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(POSTGRES_SCOPE_GUARD_FUNCTION_SQL)
        return
    if dialect == "sqlite":
        # SQLite stores both values with text affinity and has its own 0013
        # trigger implementation, so there is no PostgreSQL function to alter.
        return
    raise RuntimeError(f"0019 does not support dialect: {dialect}")


def downgrade() -> None:
    """Refuse to reinstall the known-broken PostgreSQL 0018 function body."""

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        raise RuntimeError(
            "0019 修复 Commerce scope guard 的 runstatus/VARCHAR 比较；"
            "降级会恢复已知会导致 PostgreSQL 运行失败的函数，请从升级前备份恢复"
        )
    if dialect == "sqlite":
        # 0019 在 SQLite 没有 DDL 或数据变更，安全地回到 0018。
        return
    raise RuntimeError(f"0019 does not support dialect: {dialect}")
