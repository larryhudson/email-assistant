"""script-gated scheduled tasks

Revision ID: sch03
Revises: cc01
Create Date: 2026-05-17 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "sch03"
down_revision: str | None = "cc01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("scheduled_tasks", sa.Column("command", sa.Text(), nullable=True))
    op.add_column(
        "scheduled_tasks",
        sa.Column("is_agent_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "scheduled_tasks",
        sa.Column("max_unanswered_runs", sa.Integer(), nullable=True, server_default="3"),
    )
    op.add_column(
        "scheduled_tasks",
        sa.Column(
            "consecutive_unanswered_runs",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column("scheduled_tasks", sa.Column("paused_reason", sa.Text(), nullable=True))
    op.create_table(
        "scheduled_task_fires",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "scheduled_task_id",
            sa.String(length=64),
            sa.ForeignKey("scheduled_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("stdout", sa.Text(), nullable=True),
        sa.Column("stderr", sa.Text(), nullable=True),
        sa.Column(
            "agent_run_id",
            sa.String(length=64),
            sa.ForeignKey("agent_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("scheduled_task_fires")
    op.drop_column("scheduled_tasks", "paused_reason")
    op.drop_column("scheduled_tasks", "consecutive_unanswered_runs")
    op.drop_column("scheduled_tasks", "max_unanswered_runs")
    op.drop_column("scheduled_tasks", "is_agent_enabled")
    op.drop_column("scheduled_tasks", "command")
