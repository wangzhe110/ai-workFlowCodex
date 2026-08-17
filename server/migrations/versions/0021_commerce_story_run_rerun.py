"""Allow one selected Commerce creative idea to own append-only StoryRun reruns.

Revision ID: 0021_commerce_story_run_rerun
Revises: 0020_phase4_asset_center_foreign_key_repair
Create Date: 2026-08-16

``commerce_story_run_inputs`` originally made ``creative_idea_id`` globally
unique.  That prevented a selected idea from being rerun even though
``StoryRun`` already models append-only attempts through ``run_number``.

The authoritative number remains ``story_runs.run_number``.  This migration
adds a trigger-checked mirror to the frozen input row solely because a normal
database unique constraint cannot span the two existing tables.  The mirror
allows a portable ``(creative_idea_id, run_number)`` uniqueness guarantee
without inventing a second independently editable sequence.
"""

from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0021_commerce_story_run_rerun"
down_revision = "0020_phase4_asset_center_foreign_key_repair"
branch_labels = None
depends_on = None


TABLE = "commerce_story_run_inputs"
LEGACY_UNIQUE = "uq_commerce_story_run_input_idea"
RERUN_UNIQUE = "uq_commerce_story_run_input_idea_run_number"
IDEA_INDEX = "ix_commerce_story_run_inputs_creative_idea_id"
SQLITE_INPUT_TRIGGER_INSERT = "trg_commerce_story_run_input_number_insert"
SQLITE_INPUT_TRIGGER_UPDATE = "trg_commerce_story_run_input_number_update"
SQLITE_PARENT_TRIGGER = "trg_story_runs_input_run_number_immutable"
POSTGRES_INPUT_FUNCTION = "commerce_story_run_input_number_guard"
POSTGRES_PARENT_FUNCTION = "commerce_story_run_number_immutable_guard"


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _is_offline() -> bool:
    return context.is_offline_mode()


def _column_names() -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(TABLE)}


def _constraint_names() -> set[str]:
    return {item.get("name") for item in inspect(op.get_bind()).get_unique_constraints(TABLE)}


def _index_names() -> set[str]:
    return {item.get("name") for item in inspect(op.get_bind()).get_indexes(TABLE)}


def _add_and_backfill_run_number() -> None:
    """Backfill the immutable input mirror from the authoritative parent row."""

    if not _is_offline() and "run_number" in _column_names():
        return
    op.add_column(TABLE, sa.Column("run_number", sa.Integer(), nullable=True))
    if _is_offline():
        # Offline SQL is generated for an empty/up-to-date structure; live migrations use
        # the dialect-specific, data-safe update below before adding NOT NULL.
        return
    if _is_sqlite():
        op.execute(
            """UPDATE commerce_story_run_inputs
            SET run_number = (
                SELECT story_runs.run_number FROM story_runs
                WHERE story_runs.id = commerce_story_run_inputs.story_run_id
            )"""
        )
    else:
        op.execute(
            """UPDATE commerce_story_run_inputs AS input
            SET run_number = story.run_number
            FROM story_runs AS story
            WHERE story.id = input.story_run_id"""
        )
    missing = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM commerce_story_run_inputs WHERE run_number IS NULL")
    ).scalar_one()
    if missing:
        raise RuntimeError(
            "0021 无法回填 Commerce StoryRun 输入编号：存在没有父 StoryRun 的输入记录；迁移未修改数据。"
        )


def _alter_input_constraints_for_upgrade() -> None:
    """Replace the published single-idea unique constraint with the rerun key."""

    if _is_sqlite():
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_constraint(LEGACY_UNIQUE, type_="unique")
            batch.alter_column("run_number", existing_type=sa.Integer(), nullable=False)
            batch.create_unique_constraint(RERUN_UNIQUE, ["creative_idea_id", "run_number"])
        return
    op.drop_constraint(LEGACY_UNIQUE, TABLE, type_="unique")
    op.alter_column(TABLE, "run_number", existing_type=sa.Integer(), nullable=False)
    op.create_unique_constraint(RERUN_UNIQUE, TABLE, ["creative_idea_id", "run_number"])


def _create_sqlite_triggers() -> None:
    for name in (SQLITE_INPUT_TRIGGER_INSERT, SQLITE_INPUT_TRIGGER_UPDATE, SQLITE_PARENT_TRIGGER):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.execute(
        f"""CREATE TRIGGER {SQLITE_INPUT_TRIGGER_INSERT}
        BEFORE INSERT ON {TABLE}
        WHEN NOT EXISTS (
            SELECT 1 FROM story_runs story
             WHERE story.id = NEW.story_run_id AND story.run_number = NEW.run_number
        )
        BEGIN SELECT RAISE(ABORT, 'commerce story run input number invalid'); END"""
    )
    op.execute(
        f"""CREATE TRIGGER {SQLITE_INPUT_TRIGGER_UPDATE}
        BEFORE UPDATE OF story_run_id, run_number ON {TABLE}
        WHEN NOT EXISTS (
            SELECT 1 FROM story_runs story
             WHERE story.id = NEW.story_run_id AND story.run_number = NEW.run_number
        )
        BEGIN SELECT RAISE(ABORT, 'commerce story run input number invalid'); END"""
    )
    op.execute(
        f"""CREATE TRIGGER {SQLITE_PARENT_TRIGGER}
        BEFORE UPDATE OF run_number ON story_runs
        WHEN NEW.run_number <> OLD.run_number
         AND EXISTS (SELECT 1 FROM {TABLE} input WHERE input.story_run_id = NEW.id)
        BEGIN SELECT RAISE(ABORT, 'commerce story run number immutable after input freeze'); END"""
    )


def _create_postgresql_triggers() -> None:
    op.execute(
        f"""CREATE OR REPLACE FUNCTION {POSTGRES_INPUT_FUNCTION}() RETURNS trigger AS $$
        DECLARE parent_run_number integer;
        BEGIN
            SELECT run_number INTO parent_run_number
              FROM story_runs WHERE id = NEW.story_run_id;
            IF parent_run_number IS NULL OR NEW.run_number <> parent_run_number THEN
                RAISE EXCEPTION 'commerce story run input number invalid';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        f"""CREATE OR REPLACE FUNCTION {POSTGRES_PARENT_FUNCTION}() RETURNS trigger AS $$
        BEGIN
            IF NEW.run_number <> OLD.run_number
               AND EXISTS (SELECT 1 FROM {TABLE} input WHERE input.story_run_id = NEW.id) THEN
                RAISE EXCEPTION 'commerce story run number immutable after input freeze';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(f"DROP TRIGGER IF EXISTS {SQLITE_INPUT_TRIGGER_INSERT} ON {TABLE}")
    op.execute(f"DROP TRIGGER IF EXISTS {SQLITE_PARENT_TRIGGER} ON story_runs")
    op.execute(
        f"""CREATE TRIGGER {SQLITE_INPUT_TRIGGER_INSERT}
        BEFORE INSERT OR UPDATE OF story_run_id, run_number ON {TABLE}
        FOR EACH ROW EXECUTE FUNCTION {POSTGRES_INPUT_FUNCTION}()"""
    )
    op.execute(
        f"""CREATE TRIGGER {SQLITE_PARENT_TRIGGER}
        BEFORE UPDATE OF run_number ON story_runs
        FOR EACH ROW EXECUTE FUNCTION {POSTGRES_PARENT_FUNCTION}()"""
    )


def _drop_sqlite_triggers() -> None:
    for name in (SQLITE_PARENT_TRIGGER, SQLITE_INPUT_TRIGGER_UPDATE, SQLITE_INPUT_TRIGGER_INSERT):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")


def _drop_postgresql_triggers() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {SQLITE_PARENT_TRIGGER} ON story_runs")
    op.execute(f"DROP TRIGGER IF EXISTS {SQLITE_INPUT_TRIGGER_INSERT} ON {TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS {POSTGRES_PARENT_FUNCTION}()")
    op.execute(f"DROP FUNCTION IF EXISTS {POSTGRES_INPUT_FUNCTION}()")


def _assert_downgrade_safe() -> None:
    duplicate = op.get_bind().execute(
        sa.text(
            """SELECT creative_idea_id
            FROM commerce_story_run_inputs
            GROUP BY creative_idea_id
            HAVING COUNT(*) > 1
            LIMIT 1"""
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise RuntimeError(
            "0021 降级被拒绝：同一已选创意已经存在多个 StoryRun；"
            "旧版单创意唯一约束无法无损恢复，迁移不会删除任何数据。"
        )


def upgrade() -> None:
    _add_and_backfill_run_number()
    _alter_input_constraints_for_upgrade()
    if _is_offline():
        op.create_index(IDEA_INDEX, TABLE, ["creative_idea_id"])
        if _is_sqlite():
            _create_sqlite_triggers()
        else:
            _create_postgresql_triggers()
        return
    if IDEA_INDEX not in _index_names():
        op.create_index(IDEA_INDEX, TABLE, ["creative_idea_id"])
    if _is_sqlite():
        _create_sqlite_triggers()
    else:
        _create_postgresql_triggers()


def downgrade() -> None:
    if _is_offline():
        # 离线 SQL 也必须完整表达触发器/函数的撤销顺序，不能只删列和唯一约束，
        # 否则审阅者无法从生成的 PostgreSQL/SQLite DDL 判断回滚是否保留旧守卫。
        if _is_sqlite():
            _drop_sqlite_triggers()
        else:
            _drop_postgresql_triggers()
        op.drop_index(IDEA_INDEX, table_name=TABLE)
        op.drop_constraint(RERUN_UNIQUE, TABLE, type_="unique")
        op.drop_column(TABLE, "run_number")
        op.create_unique_constraint(LEGACY_UNIQUE, TABLE, ["creative_idea_id"])
        return
    _assert_downgrade_safe()
    if _is_sqlite():
        _drop_sqlite_triggers()
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_constraint(RERUN_UNIQUE, type_="unique")
            batch.drop_column("run_number")
            batch.create_unique_constraint(LEGACY_UNIQUE, ["creative_idea_id"])
    else:
        _drop_postgresql_triggers()
        op.drop_constraint(RERUN_UNIQUE, TABLE, type_="unique")
        op.drop_column(TABLE, "run_number")
        op.create_unique_constraint(LEGACY_UNIQUE, TABLE, ["creative_idea_id"])
    if IDEA_INDEX in _index_names():
        op.drop_index(IDEA_INDEX, table_name=TABLE)
