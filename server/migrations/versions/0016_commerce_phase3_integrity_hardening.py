"""Enforce immutable Commerce metering events at the database boundary.

Revision ID: 0016_commerce_phase3_integrity_hardening
Revises: 0015_commerce_phase3_knowledge_generation_scaffolding
Create Date: 2026-08-11

Usage events are append-only accounting records.  This migration deliberately
protects only UPDATE: existing deletion/cascade semantics remain unchanged.
"""

from alembic import op


revision = "0016_commerce_phase3_integrity_hardening"
down_revision = "0015_commerce_phase3_knowledge_generation_scaffolding"
branch_labels = None
depends_on = None


SQLITE_TRIGGER = "trg_usage_events_immutable_update_0016"
POSTGRES_TRIGGER = "trg_usage_events_immutable_update_0016"
POSTGRES_FUNCTION = "fn_usage_events_immutable_update_0016"


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            f"""
            CREATE TRIGGER {SQLITE_TRIGGER}
            BEFORE UPDATE ON usage_events
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'usage_events are immutable');
            END
            """
        )
        return
    if dialect == "postgresql":
        op.execute(
            f"""
            CREATE FUNCTION {POSTGRES_FUNCTION}()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'usage_events are immutable';
            END;
            $$
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {POSTGRES_TRIGGER}
            BEFORE UPDATE ON usage_events
            FOR EACH ROW
            EXECUTE FUNCTION {POSTGRES_FUNCTION}()
            """
        )
        return
    raise RuntimeError(f"0016 does not support dialect: {dialect}")


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {SQLITE_TRIGGER}")
        return
    if dialect == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS {POSTGRES_TRIGGER} ON usage_events")
        op.execute(f"DROP FUNCTION IF EXISTS {POSTGRES_FUNCTION}()")
        return
    raise RuntimeError(f"0016 does not support dialect: {dialect}")
