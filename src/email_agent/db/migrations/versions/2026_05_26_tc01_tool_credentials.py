"""tool_credentials table

Generic per-assistant credential metadata for host-side tool integrations
(Google Workspace, GitHub, Slack, ...). Secret material is referenced via
the opaque `secret_ref`, never stored inline. Non-secret per-provider data
lives in the JSON `metadata` column.

Revision ID: tc01
Revises: mh01
Create Date: 2026-05-26 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "tc01"
down_revision: str | None = "mh01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_credentials",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "assistant_id",
            sa.String(length=64),
            sa.ForeignKey("assistants.id"),
            nullable=False,
        ),
        sa.Column("tool_credential_key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("account_identifier", sa.String(length=320), nullable=True),
        sa.Column("credential_kind", sa.String(length=64), nullable=False),
        sa.Column("secret_ref", sa.String(length=1024), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="active",
        ),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
    op.create_index(
        "ix_tool_credentials_assistant_id_tool_credential_key",
        "tool_credentials",
        ["assistant_id", "tool_credential_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tool_credentials_assistant_id_tool_credential_key",
        table_name="tool_credentials",
    )
    op.drop_table("tool_credentials")
