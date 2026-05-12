"""agent_runs system_prompt

Revision ID: sp01
Revises: err01
Create Date: 2026-05-12 06:33:13.079200

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "sp01"
down_revision: str | None = "err01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("system_prompt", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "system_prompt")
