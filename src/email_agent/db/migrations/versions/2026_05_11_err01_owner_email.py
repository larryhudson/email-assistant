"""owners.email

Add the owner's notification email so the runtime can send error
notifications to the admin when an agent run fails.

Pre-prod: NOT NULL with empty-string server_default so existing dev rows
backfill cleanly. Operators are expected to reseed (`seed-assistant
--owner-email ...`) before relying on error notifications.

Revision ID: err01
Revises: sch02
Create Date: 2026-05-11 02:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "err01"
down_revision: str | None = "sch02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "owners",
        sa.Column("email", sa.String(length=320), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("owners", "email")
