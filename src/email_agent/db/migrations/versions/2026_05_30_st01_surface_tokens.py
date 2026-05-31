"""surface tokens

Revision ID: st01
Revises: as01
Create Date: 2026-05-30 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "st01"
down_revision: str | None = "as01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "surface_tokens",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("assistant_id", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["assistant_id"], ["assistants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_surface_tokens_assistant_id", "surface_tokens", ["assistant_id"])


def downgrade() -> None:
    op.drop_index("ix_surface_tokens_assistant_id", table_name="surface_tokens")
    op.drop_table("surface_tokens")
