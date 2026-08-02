"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from alembic import op
import sqlalchemy as sa


revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    """应用本版本的结构变更。"""

    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """回滚本版本的结构变更；上线前须确认数据可逆。"""

    ${downgrades if downgrades else "pass"}
