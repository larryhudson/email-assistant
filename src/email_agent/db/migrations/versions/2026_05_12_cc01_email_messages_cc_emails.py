"""email_messages cc_emails

Revision ID: cc01
Revises: up01
Create Date: 2026-05-12 07:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cc01"
down_revision: str | None = "up01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "email_messages",
        sa.Column(
            "cc_emails",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )


def downgrade() -> None:
    op.drop_column("email_messages", "cc_emails")
