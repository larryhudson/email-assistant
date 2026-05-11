"""scheduled_tasks table

Backs the scheduled-tasks capability: the agent can create one-shot or
cron-recurring rows, and the `tick_scheduled_tasks` periodic procrastinate
task drains them once per minute, building synthetic `NormalizedInboundEmail`s
that feed into `AssistantRuntime.accept_inbound`. Each fire creates a new
thread because the synthetic email carries fresh headers and no in_reply_to.

Revision ID: sch01
Revises: rmr01
Create Date: 2026-05-11 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "sch01"
down_revision: str | None = "rmr01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduled_tasks",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "assistant_id", sa.String(length=64), sa.ForeignKey("assistants.id"), nullable=False
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cron_expr", sa.String(length=255), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("subject", sa.String(length=998), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_by_run_id",
            sa.String(length=64),
            sa.ForeignKey("agent_runs.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_scheduled_tasks_next_run_at", "scheduled_tasks", ["next_run_at"])


def downgrade() -> None:
    op.drop_index("ix_scheduled_tasks_next_run_at", table_name="scheduled_tasks")
    op.drop_table("scheduled_tasks")
