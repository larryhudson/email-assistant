"""agent_runs user_prompt

Revision ID: up01
Revises: sp01
Create Date: 2026-05-12 06:50:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "up01"
down_revision: str | None = "sp01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("user_prompt", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "user_prompt")
