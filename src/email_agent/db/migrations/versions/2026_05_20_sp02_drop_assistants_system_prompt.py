"""drop assistants.system_prompt

Per-assistant disposition now lives in the workspace's IDENTITY.md
(seeded + agent-editable), so the operator-set system_prompt column
no longer has a job. Existing content is dropped — workspaces start
on default IDENTITY.md and empty CONTEXT.md.

Revision ID: sp02
Revises: sch03
Create Date: 2026-05-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "sp02"
down_revision: str | None = "sch03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("assistants", "system_prompt")


def downgrade() -> None:
    op.add_column(
        "assistants",
        sa.Column("system_prompt", sa.Text(), nullable=False, server_default=""),
    )
