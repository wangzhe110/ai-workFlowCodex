"""Commerce Phase 2 编排关联、专属 attempt 约束和视频提示词版本。

Revision ID: 0012_commerce_workflow_orchestration
Revises: 0011_commerce_domain_integrity_fixes
Create Date: 2026-08-09

本迁移只创建新表和其触发器，绝不向 ``workflow_runs`` / ``workflow_steps`` 两张
历史表追加列或约束。这样从 0012 降级到 0011 时可以先删除 Phase 2 专属运行，随后
完全移除本迁移的表、索引和触发器，恢复真实 0011 schema。
"""

from alembic import op
import sqlalchemy as sa


revision = "0012_commerce_workflow_orchestration"
down_revision = "0011_commerce_domain_integrity_fixes"
branch_labels = None
depends_on = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _create_sqlite_triggers() -> None:
    """为 SQLite 补齐跨表作用域校验、状态同步和父运行级联删除。"""

    op.execute(
        """CREATE TRIGGER trg_commerce_workflow_step_scope_insert
        BEFORE INSERT ON commerce_workflow_steps
        WHEN NOT EXISTS (
            SELECT 1 FROM commerce_workflow_links link
             WHERE link.workflow_run_id = NEW.workflow_run_id
               AND link.story_run_id = NEW.story_run_id
        ) OR NOT EXISTS (
            SELECT 1 FROM workflow_steps step
             WHERE step.id = NEW.workflow_step_id
               AND step.workflow_run_id = NEW.workflow_run_id
               AND step.status = NEW.status
        )
        BEGIN SELECT RAISE(ABORT, 'commerce workflow step scope invalid'); END"""
    )
    op.execute(
        """CREATE TRIGGER trg_commerce_workflow_step_scope_update
        BEFORE UPDATE OF workflow_step_id, workflow_run_id, story_run_id, stage, attempt, status
        ON commerce_workflow_steps
        WHEN NOT EXISTS (
            SELECT 1 FROM commerce_workflow_links link
             WHERE link.workflow_run_id = NEW.workflow_run_id
               AND link.story_run_id = NEW.story_run_id
        ) OR NOT EXISTS (
            SELECT 1 FROM workflow_steps step
             WHERE step.id = NEW.workflow_step_id
               AND step.workflow_run_id = NEW.workflow_run_id
               AND step.status = NEW.status
        )
        BEGIN SELECT RAISE(ABORT, 'commerce workflow step scope invalid'); END"""
    )
    op.execute(
        """CREATE TRIGGER trg_workflow_steps_sync_commerce_status
        AFTER UPDATE OF status ON workflow_steps
        WHEN EXISTS (SELECT 1 FROM commerce_workflow_steps WHERE workflow_step_id = NEW.id)
        BEGIN
            UPDATE commerce_workflow_steps SET status = NEW.status WHERE workflow_step_id = NEW.id;
        END"""
    )
    op.execute(
        """CREATE TRIGGER trg_commerce_workflow_link_delete
        AFTER DELETE ON commerce_workflow_links
        BEGIN
            DELETE FROM workflow_steps WHERE workflow_run_id = OLD.workflow_run_id;
            DELETE FROM workflow_runs WHERE id = OLD.workflow_run_id;
        END"""
    )


def _drop_sqlite_triggers() -> None:
    for name in (
        "trg_commerce_workflow_link_delete",
        "trg_workflow_steps_sync_commerce_status",
        "trg_commerce_workflow_step_scope_update",
        "trg_commerce_workflow_step_scope_insert",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")


def _create_postgresql_triggers() -> None:
    """为 PostgreSQL 提供与 SQLite 等价的跨表校验与级联语义。"""

    op.execute(
        """CREATE FUNCTION commerce_workflow_step_scope_guard() RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM commerce_workflow_links link
                 WHERE link.workflow_run_id = NEW.workflow_run_id
                   AND link.story_run_id = NEW.story_run_id
            ) OR NOT EXISTS (
                SELECT 1 FROM workflow_steps step
                 WHERE step.id = NEW.workflow_step_id
                   AND step.workflow_run_id = NEW.workflow_run_id
                   AND step.status = NEW.status
            ) THEN
                RAISE EXCEPTION 'commerce workflow step scope invalid';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE FUNCTION sync_commerce_workflow_step_status() RETURNS trigger AS $$
        BEGIN
            UPDATE commerce_workflow_steps SET status = NEW.status WHERE workflow_step_id = NEW.id;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE FUNCTION delete_commerce_workflow_parent() RETURNS trigger AS $$
        BEGIN
            DELETE FROM workflow_steps WHERE workflow_run_id = OLD.workflow_run_id;
            DELETE FROM workflow_runs WHERE id = OLD.workflow_run_id;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE TRIGGER trg_commerce_workflow_step_scope
        BEFORE INSERT OR UPDATE OF workflow_step_id, workflow_run_id, story_run_id, stage, attempt, status
        ON commerce_workflow_steps
        FOR EACH ROW EXECUTE FUNCTION commerce_workflow_step_scope_guard()"""
    )
    op.execute(
        """CREATE TRIGGER trg_workflow_steps_sync_commerce_status
        AFTER UPDATE OF status ON workflow_steps
        FOR EACH ROW EXECUTE FUNCTION sync_commerce_workflow_step_status()"""
    )
    op.execute(
        """CREATE TRIGGER trg_commerce_workflow_link_delete
        AFTER DELETE ON commerce_workflow_links
        FOR EACH ROW EXECUTE FUNCTION delete_commerce_workflow_parent()"""
    )


def _drop_postgresql_triggers() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_commerce_workflow_link_delete ON commerce_workflow_links")
    op.execute("DROP TRIGGER IF EXISTS trg_workflow_steps_sync_commerce_status ON workflow_steps")
    op.execute("DROP TRIGGER IF EXISTS trg_commerce_workflow_step_scope ON commerce_workflow_steps")
    op.execute("DROP FUNCTION IF EXISTS delete_commerce_workflow_parent()")
    op.execute("DROP FUNCTION IF EXISTS sync_commerce_workflow_step_status()")
    op.execute("DROP FUNCTION IF EXISTS commerce_workflow_step_scope_guard()")


def upgrade() -> None:
    """创建完全隔离于 V1 表结构的 Commerce 编排表。"""

    op.create_table(
        "commerce_workflow_links",
        sa.Column("workflow_run_id", sa.String(length=36), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("story_run_id", sa.String(length=36), sa.ForeignKey("story_runs.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_commerce_workflow_links_story_run_id", "commerce_workflow_links", ["story_run_id"])
    op.create_table(
        "commerce_workflow_steps",
        sa.Column("workflow_step_id", sa.String(length=36), sa.ForeignKey("workflow_steps.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("workflow_run_id", sa.String(length=36), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("story_run_id", sa.String(length=36), sa.ForeignKey("story_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stage", sa.String(length=80), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_commerce_workflow_steps_workflow_run_id", "commerce_workflow_steps", ["workflow_run_id"])
    op.create_index("ix_commerce_workflow_steps_story_run_id", "commerce_workflow_steps", ["story_run_id"])
    op.create_index(
        "uq_commerce_workflow_step_attempt",
        "commerce_workflow_steps",
        ["workflow_run_id", "stage", "attempt"],
        unique=True,
    )
    op.create_index(
        "uq_active_commerce_workflow_step",
        "commerce_workflow_steps",
        ["workflow_run_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('PENDING', 'RUNNING')"),
        postgresql_where=sa.text("status IN ('PENDING', 'RUNNING')"),
    )
    op.create_table(
        "video_prompt_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("video_segment_id", sa.String(length=36), sa.ForeignKey("video_segment_plans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workflow_step_id", sa.String(length=36), sa.ForeignKey("workflow_steps.id", ondelete="SET NULL"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("trace", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="DRAFT"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("video_segment_id", "version", name="uq_video_prompt_version"),
        sa.CheckConstraint("version >= 1", name="ck_video_prompt_version_positive"),
    )
    op.create_index("ix_video_prompt_versions_video_segment_id", "video_prompt_versions", ["video_segment_id"])
    op.create_index("ix_video_prompt_versions_workflow_step_id", "video_prompt_versions", ["workflow_step_id"])
    if _is_sqlite():
        _create_sqlite_triggers()
    else:
        _create_postgresql_triggers()


def downgrade() -> None:
    """删除 Phase 2 专属数据和 schema，恢复真实 0011 结构。"""

    # 降级允许删除只属于 0012 的 Commerce 父运行、步骤和提示词版本；Phase 1 的
    # StoryRun、产品、选择、大纲、章节、片段和所有 V1 WorkflowRun/Step 均不触碰。
    if _is_sqlite():
        _drop_sqlite_triggers()
    else:
        _drop_postgresql_triggers()
    op.drop_index("ix_video_prompt_versions_workflow_step_id", table_name="video_prompt_versions")
    op.drop_index("ix_video_prompt_versions_video_segment_id", table_name="video_prompt_versions")
    op.drop_table("video_prompt_versions")
    op.execute(
        """DELETE FROM commerce_workflow_steps
         WHERE workflow_run_id IN (SELECT workflow_run_id FROM commerce_workflow_links)"""
    )
    op.execute(
        """DELETE FROM workflow_steps
         WHERE workflow_run_id IN (SELECT workflow_run_id FROM commerce_workflow_links)"""
    )
    op.execute(
        """DELETE FROM workflow_runs
         WHERE id IN (SELECT workflow_run_id FROM commerce_workflow_links)"""
    )
    op.drop_index("uq_active_commerce_workflow_step", table_name="commerce_workflow_steps")
    op.drop_index("uq_commerce_workflow_step_attempt", table_name="commerce_workflow_steps")
    op.drop_index("ix_commerce_workflow_steps_story_run_id", table_name="commerce_workflow_steps")
    op.drop_index("ix_commerce_workflow_steps_workflow_run_id", table_name="commerce_workflow_steps")
    op.drop_table("commerce_workflow_steps")
    op.drop_index("ix_commerce_workflow_links_story_run_id", table_name="commerce_workflow_links")
    op.drop_table("commerce_workflow_links")
