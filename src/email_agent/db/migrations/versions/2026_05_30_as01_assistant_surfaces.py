"""assistant surface settings

Revision ID: as01
Revises: tc01
Create Date: 2026-05-30 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "as01"
down_revision: str | None = "tc01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assistant_surfaces",
        sa.Column("assistant_id", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("port", sa.Integer(), nullable=False, server_default="8000"),
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
        sa.ForeignKeyConstraint(["assistant_id"], ["assistants.id"]),
        sa.PrimaryKeyConstraint("assistant_id"),
    )


def downgrade() -> None:
    op.drop_table("assistant_surfaces")
