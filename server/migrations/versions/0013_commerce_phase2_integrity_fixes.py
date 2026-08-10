"""Harden Commerce attempt ownership and review-result integrity.

Revision ID: 0013_commerce_phase2_integrity_fixes
Revises: 0012_commerce_workflow_orchestration
Create Date: 2026-08-10

``0012`` is already published.  This revision deliberately leaves every prior
migration untouched and adds the missing database guards as an incremental
layer.  The chapter-attempt relation keeps retry outputs append-only without
changing the Phase 1 ``chapter_plans`` unique constraint.
"""

from alembic import op
import sqlalchemy as sa


revision = "0013_commerce_phase2_integrity_fixes"
down_revision = "0012_commerce_workflow_orchestration"
branch_labels = None
depends_on = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _create_sqlite_0012_step_scope_triggers() -> None:
    """Restore the exact 0012 scope behaviour when this revision is removed."""

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


def _create_sqlite_triggers() -> None:
    """SQLite equivalents of the PostgreSQL cross-table guards.

    SQLite cannot express these joins with a formal foreign key because the
    business relation spans a link row plus columns on both parent tables.  The
    triggers run for both INSERT and UPDATE and therefore preserve the same
    invariants as PostgreSQL in production.
    """

    for name in (
        "trg_commerce_workflow_step_scope_insert",
        "trg_commerce_workflow_step_scope_update",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.execute(
        """CREATE TRIGGER trg_commerce_workflow_link_scope_insert
        BEFORE INSERT ON commerce_workflow_links
        WHEN NOT EXISTS (
            SELECT 1
              FROM workflow_runs run
              JOIN story_runs story ON story.id = NEW.story_run_id
             WHERE run.id = NEW.workflow_run_id
               AND run.workflow_key = 'commerce_story_run'
               AND run.project_id = story.project_id
        )
        BEGIN SELECT RAISE(ABORT, 'commerce workflow link scope invalid'); END"""
    )
    op.execute(
        """CREATE TRIGGER trg_commerce_workflow_link_scope_update
        BEFORE UPDATE OF workflow_run_id, story_run_id ON commerce_workflow_links
        WHEN NOT EXISTS (
            SELECT 1
              FROM workflow_runs run
              JOIN story_runs story ON story.id = NEW.story_run_id
             WHERE run.id = NEW.workflow_run_id
               AND run.workflow_key = 'commerce_story_run'
               AND run.project_id = story.project_id
        )
        BEGIN SELECT RAISE(ABORT, 'commerce workflow link scope invalid'); END"""
    )
    for operation in ("INSERT", "UPDATE OF workflow_step_id, workflow_run_id, story_run_id, stage, attempt, status"):
        suffix = "insert" if operation == "INSERT" else "update"
        op.execute(
            f"""CREATE TRIGGER trg_commerce_workflow_step_scope_{suffix}
            BEFORE {operation} ON commerce_workflow_steps
            WHEN NOT EXISTS (
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
                   AND step.status = NEW.status
            )
            BEGIN SELECT RAISE(ABORT, 'commerce workflow step scope invalid'); END"""
        )
    op.execute(
        """CREATE TRIGGER trg_workflow_runs_commerce_link_scope
        BEFORE UPDATE OF workflow_key, project_id ON workflow_runs
        WHEN EXISTS (SELECT 1 FROM commerce_workflow_links link WHERE link.workflow_run_id = NEW.id)
         AND (
            NEW.workflow_key <> 'commerce_story_run'
            OR NOT EXISTS (
                SELECT 1 FROM commerce_workflow_links link
                JOIN story_runs story ON story.id = link.story_run_id
                 WHERE link.workflow_run_id = NEW.id AND story.project_id = NEW.project_id
            )
         )
        BEGIN SELECT RAISE(ABORT, 'commerce workflow run scope invalid'); END"""
    )
    op.execute(
        """CREATE TRIGGER trg_story_runs_commerce_link_scope
        BEFORE UPDATE OF project_id ON story_runs
        WHEN EXISTS (SELECT 1 FROM commerce_workflow_links link WHERE link.story_run_id = NEW.id)
         AND NOT EXISTS (
            SELECT 1 FROM commerce_workflow_links link
            JOIN workflow_runs run ON run.id = link.workflow_run_id
             WHERE link.story_run_id = NEW.id AND run.project_id = NEW.project_id
         )
        BEGIN SELECT RAISE(ABORT, 'commerce story run scope invalid'); END"""
    )
    op.execute(
        """CREATE TRIGGER trg_workflow_steps_commerce_identity_guard
        BEFORE UPDATE OF workflow_run_id, step_key, attempt ON workflow_steps
        WHEN EXISTS (
            SELECT 1 FROM commerce_workflow_steps commerce
             WHERE commerce.workflow_step_id = OLD.id
               AND (
                   commerce.workflow_run_id <> NEW.workflow_run_id
                   OR commerce.stage <> NEW.step_key
                   OR commerce.attempt <> NEW.attempt
               )
        )
        BEGIN SELECT RAISE(ABORT, 'commerce workflow step identity immutable'); END"""
    )
    op.execute(
        """CREATE TRIGGER trg_commerce_chapter_attempt_scope_insert
        BEFORE INSERT ON commerce_chapter_attempt_chapters
        WHEN NOT EXISTS (
            SELECT 1
              FROM commerce_workflow_steps commerce
              JOIN chapter_plans chapter ON chapter.id = NEW.chapter_plan_id
              JOIN story_outline_versions outline ON outline.id = NEW.outline_version_id
             WHERE commerce.workflow_step_id = NEW.workflow_step_id
               AND commerce.story_run_id = NEW.story_run_id
               AND commerce.stage = 'CHAPTERS'
               AND chapter.story_run_id = NEW.story_run_id
               AND chapter.outline_version_id = NEW.outline_version_id
               AND outline.story_run_id = NEW.story_run_id
        )
        BEGIN SELECT RAISE(ABORT, 'commerce chapter attempt scope invalid'); END"""
    )
    op.execute(
        """CREATE TRIGGER trg_commerce_chapter_attempt_scope_update
        BEFORE UPDATE OF workflow_step_id, story_run_id, outline_version_id, chapter_plan_id, position
        ON commerce_chapter_attempt_chapters
        WHEN NOT EXISTS (
            SELECT 1
              FROM commerce_workflow_steps commerce
              JOIN chapter_plans chapter ON chapter.id = NEW.chapter_plan_id
              JOIN story_outline_versions outline ON outline.id = NEW.outline_version_id
             WHERE commerce.workflow_step_id = NEW.workflow_step_id
               AND commerce.story_run_id = NEW.story_run_id
               AND commerce.stage = 'CHAPTERS'
               AND chapter.story_run_id = NEW.story_run_id
               AND chapter.outline_version_id = NEW.outline_version_id
               AND outline.story_run_id = NEW.story_run_id
        )
        BEGIN SELECT RAISE(ABORT, 'commerce chapter attempt scope invalid'); END"""
    )


def _drop_sqlite_0013_triggers() -> None:
    for name in (
        "trg_commerce_chapter_attempt_scope_update",
        "trg_commerce_chapter_attempt_scope_insert",
        "trg_workflow_steps_commerce_identity_guard",
        "trg_story_runs_commerce_link_scope",
        "trg_workflow_runs_commerce_link_scope",
        "trg_commerce_workflow_link_scope_update",
        "trg_commerce_workflow_link_scope_insert",
        "trg_commerce_workflow_step_scope_update",
        "trg_commerce_workflow_step_scope_insert",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")


def _create_postgresql_0012_step_scope_trigger() -> None:
    """Restore the original 0012 function body during downgrade."""

    op.execute(
        """CREATE OR REPLACE FUNCTION commerce_workflow_step_scope_guard() RETURNS trigger AS $$
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
        """CREATE TRIGGER trg_commerce_workflow_step_scope
        BEFORE INSERT OR UPDATE OF workflow_step_id, workflow_run_id, story_run_id, stage, attempt, status
        ON commerce_workflow_steps
        FOR EACH ROW EXECUTE FUNCTION commerce_workflow_step_scope_guard()"""
    )


def _create_postgresql_triggers() -> None:
    """PostgreSQL implementation with the same predicates as SQLite."""

    op.execute("DROP TRIGGER IF EXISTS trg_commerce_workflow_step_scope ON commerce_workflow_steps")
    op.execute(
        """CREATE OR REPLACE FUNCTION commerce_workflow_step_scope_guard() RETURNS trigger AS $$
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
                   AND step.status = NEW.status
            ) THEN
                RAISE EXCEPTION 'commerce workflow step scope invalid';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE FUNCTION commerce_workflow_link_scope_guard() RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM workflow_runs run
                JOIN story_runs story ON story.id = NEW.story_run_id
                 WHERE run.id = NEW.workflow_run_id
                   AND run.workflow_key = 'commerce_story_run'
                   AND run.project_id = story.project_id
            ) THEN
                RAISE EXCEPTION 'commerce workflow link scope invalid';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE FUNCTION commerce_workflow_run_scope_guard() RETURNS trigger AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM commerce_workflow_links link WHERE link.workflow_run_id = NEW.id)
               AND (
                   NEW.workflow_key <> 'commerce_story_run'
                   OR NOT EXISTS (
                       SELECT 1 FROM commerce_workflow_links link
                       JOIN story_runs story ON story.id = link.story_run_id
                        WHERE link.workflow_run_id = NEW.id AND story.project_id = NEW.project_id
                   )
               ) THEN
                RAISE EXCEPTION 'commerce workflow run scope invalid';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE FUNCTION commerce_story_run_scope_guard() RETURNS trigger AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM commerce_workflow_links link WHERE link.story_run_id = NEW.id)
               AND NOT EXISTS (
                   SELECT 1 FROM commerce_workflow_links link
                   JOIN workflow_runs run ON run.id = link.workflow_run_id
                    WHERE link.story_run_id = NEW.id AND run.project_id = NEW.project_id
               ) THEN
                RAISE EXCEPTION 'commerce story run scope invalid';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE FUNCTION commerce_workflow_step_identity_guard() RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM commerce_workflow_steps commerce
                 WHERE commerce.workflow_step_id = OLD.id
                   AND (
                       commerce.workflow_run_id <> NEW.workflow_run_id
                       OR commerce.stage <> NEW.step_key
                       OR commerce.attempt <> NEW.attempt
                   )
            ) THEN
                RAISE EXCEPTION 'commerce workflow step identity immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE FUNCTION commerce_chapter_attempt_scope_guard() RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                  FROM commerce_workflow_steps commerce
                  JOIN chapter_plans chapter ON chapter.id = NEW.chapter_plan_id
                  JOIN story_outline_versions outline ON outline.id = NEW.outline_version_id
                 WHERE commerce.workflow_step_id = NEW.workflow_step_id
                   AND commerce.story_run_id = NEW.story_run_id
                   AND commerce.stage = 'CHAPTERS'
                   AND chapter.story_run_id = NEW.story_run_id
                   AND chapter.outline_version_id = NEW.outline_version_id
                   AND outline.story_run_id = NEW.story_run_id
            ) THEN
                RAISE EXCEPTION 'commerce chapter attempt scope invalid';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE TRIGGER trg_commerce_workflow_step_scope
        BEFORE INSERT OR UPDATE OF workflow_step_id, workflow_run_id, story_run_id, stage, attempt, status
        ON commerce_workflow_steps FOR EACH ROW EXECUTE FUNCTION commerce_workflow_step_scope_guard()"""
    )
    op.execute(
        """CREATE TRIGGER trg_commerce_workflow_link_scope
        BEFORE INSERT OR UPDATE OF workflow_run_id, story_run_id ON commerce_workflow_links
        FOR EACH ROW EXECUTE FUNCTION commerce_workflow_link_scope_guard()"""
    )
    op.execute(
        """CREATE TRIGGER trg_workflow_runs_commerce_link_scope
        BEFORE UPDATE OF workflow_key, project_id ON workflow_runs
        FOR EACH ROW EXECUTE FUNCTION commerce_workflow_run_scope_guard()"""
    )
    op.execute(
        """CREATE TRIGGER trg_story_runs_commerce_link_scope
        BEFORE UPDATE OF project_id ON story_runs
        FOR EACH ROW EXECUTE FUNCTION commerce_story_run_scope_guard()"""
    )
    op.execute(
        """CREATE TRIGGER trg_workflow_steps_commerce_identity_guard
        BEFORE UPDATE OF workflow_run_id, step_key, attempt ON workflow_steps
        FOR EACH ROW EXECUTE FUNCTION commerce_workflow_step_identity_guard()"""
    )
    op.execute(
        """CREATE TRIGGER trg_commerce_chapter_attempt_scope
        BEFORE INSERT OR UPDATE OF workflow_step_id, story_run_id, outline_version_id, chapter_plan_id, position
        ON commerce_chapter_attempt_chapters
        FOR EACH ROW EXECUTE FUNCTION commerce_chapter_attempt_scope_guard()"""
    )


def _drop_postgresql_0013_triggers() -> None:
    for trigger, table in (
        ("trg_commerce_chapter_attempt_scope", "commerce_chapter_attempt_chapters"),
        ("trg_workflow_steps_commerce_identity_guard", "workflow_steps"),
        ("trg_story_runs_commerce_link_scope", "story_runs"),
        ("trg_workflow_runs_commerce_link_scope", "workflow_runs"),
        ("trg_commerce_workflow_link_scope", "commerce_workflow_links"),
        ("trg_commerce_workflow_step_scope", "commerce_workflow_steps"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    for function in (
        "commerce_chapter_attempt_scope_guard()",
        "commerce_workflow_step_identity_guard()",
        "commerce_story_run_scope_guard()",
        "commerce_workflow_run_scope_guard()",
        "commerce_workflow_link_scope_guard()",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function}")


def upgrade() -> None:
    op.create_table(
        "commerce_chapter_attempt_chapters",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "workflow_step_id",
            sa.String(length=36),
            sa.ForeignKey("commerce_workflow_steps.workflow_step_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("story_run_id", sa.String(length=36), sa.ForeignKey("story_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "outline_version_id",
            sa.String(length=36),
            sa.ForeignKey("story_outline_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("chapter_plan_id", sa.String(length=36), sa.ForeignKey("chapter_plans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("chapter_plan_id", name="uq_commerce_chapter_attempt_chapter"),
        sa.UniqueConstraint("workflow_step_id", "position", name="uq_commerce_chapter_attempt_position"),
        sa.CheckConstraint("position >= 1", name="ck_commerce_chapter_attempt_position_positive"),
    )
    op.create_index("ix_commerce_chapter_attempt_chapters_workflow_step_id", "commerce_chapter_attempt_chapters", ["workflow_step_id"])
    op.create_index("ix_commerce_chapter_attempt_chapters_story_run_id", "commerce_chapter_attempt_chapters", ["story_run_id"])
    op.create_index("ix_commerce_chapter_attempt_chapters_outline_version_id", "commerce_chapter_attempt_chapters", ["outline_version_id"])
    op.create_index("ix_commerce_chapter_attempt_chapters_chapter_plan_id", "commerce_chapter_attempt_chapters", ["chapter_plan_id"])
    if _is_sqlite():
        _create_sqlite_triggers()
    else:
        _create_postgresql_triggers()


def downgrade() -> None:
    # First restore exactly the 0012 Commerce step trigger semantics.  The chapter-attempt
    # relation is 0013-only metadata and can be removed without changing Phase 1 chapters.
    if _is_sqlite():
        _drop_sqlite_0013_triggers()
        _create_sqlite_0012_step_scope_triggers()
    else:
        _drop_postgresql_0013_triggers()
        _create_postgresql_0012_step_scope_trigger()
    op.drop_index("ix_commerce_chapter_attempt_chapters_chapter_plan_id", table_name="commerce_chapter_attempt_chapters")
    op.drop_index("ix_commerce_chapter_attempt_chapters_outline_version_id", table_name="commerce_chapter_attempt_chapters")
    op.drop_index("ix_commerce_chapter_attempt_chapters_story_run_id", table_name="commerce_chapter_attempt_chapters")
    op.drop_index("ix_commerce_chapter_attempt_chapters_workflow_step_id", table_name="commerce_chapter_attempt_chapters")
    op.drop_table("commerce_chapter_attempt_chapters")
