"""run_memory_recalls table

Stores the memory chunks `MemoryPort.recall(...)` returned for each run
so the admin trace view can show what context the agent saw. We snapshot
rather than re-run recall later because durable memory grows between
calls — a re-run would produce a different answer than the agent saw.

Revision ID: rmr01
Revises: proc01
Create Date: 2026-05-10 19:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "rmr01"
down_revision: str | None = "proc01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_memory_recalls",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("memory_id", sa.String(length=64), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_run_memory_recalls_run_id", "run_memory_recalls", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_run_memory_recalls_run_id", table_name="run_memory_recalls")
    op.drop_table("run_memory_recalls")
