"""agent_runs.triggered_by_scheduled_task_id

Mark agent runs that were created by a `scheduled_tasks` fire so the
admin UI can filter and the system can audit cron-driven activity.

Revision ID: sch02
Revises: sch01
Create Date: 2026-05-11 00:01:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "sch02"
down_revision: str | None = "sch01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("triggered_by_scheduled_task_id", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_runs_scheduled_task",
        "agent_runs",
        "scheduled_tasks",
        ["triggered_by_scheduled_task_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_agent_runs_scheduled_task", "agent_runs", type_="foreignkey")
    op.drop_column("agent_runs", "triggered_by_scheduled_task_id")
