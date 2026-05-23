"""agent_runs.message_history

Persist the raw Pydantic AI `ModelMessage` history for a completed (or
quiet-exited) run as JSON, so a same-thread follow-up can be handed prior
tool calls and returns via `Agent.run(message_history=...)`.

Revision ID: mh01
Revises: sp02
Create Date: 2026-05-23 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "mh01"
down_revision: str | None = "sp02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("message_history", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "message_history")
