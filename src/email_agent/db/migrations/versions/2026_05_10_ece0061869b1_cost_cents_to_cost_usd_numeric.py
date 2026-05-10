"""cost_cents to cost_usd numeric

Switch cost columns from integer cents to Numeric(10,4) USD so we
don't lose precision on small per-run costs (Fireworks pricing makes
typical runs sub-cent). Same change applies to budgets.monthly_limit.

Data conversion: existing values are in cents, divide by 100 to get
USD. Postgres preserves the rows; the column type+name change happens
atomically via ALTER COLUMN ... TYPE ... USING.

Revision ID: ece0061869b1
Revises: 0001
Create Date: 2026-05-10 16:56:15.043411
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ece0061869b1"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # budgets.monthly_limit_cents (Integer cents) → monthly_limit_usd (Numeric(10,4))
    op.alter_column(
        "budgets",
        "monthly_limit_cents",
        new_column_name="monthly_limit_usd",
        existing_type=sa.Integer(),
        type_=sa.Numeric(10, 4),
        postgresql_using="monthly_limit_cents::numeric / 100",
    )

    # run_steps.cost_cents → cost_usd
    op.alter_column(
        "run_steps",
        "cost_cents",
        new_column_name="cost_usd",
        existing_type=sa.Integer(),
        type_=sa.Numeric(10, 4),
        existing_server_default=sa.text("0"),
        server_default=sa.text("0"),
        postgresql_using="cost_cents::numeric / 100",
    )

    # usage_ledger.cost_cents → cost_usd
    op.alter_column(
        "usage_ledger",
        "cost_cents",
        new_column_name="cost_usd",
        existing_type=sa.Integer(),
        type_=sa.Numeric(10, 4),
        postgresql_using="cost_cents::numeric / 100",
    )


def downgrade() -> None:
    op.alter_column(
        "usage_ledger",
        "cost_usd",
        new_column_name="cost_cents",
        existing_type=sa.Numeric(10, 4),
        type_=sa.Integer(),
        postgresql_using="(cost_usd * 100)::integer",
    )
    op.alter_column(
        "run_steps",
        "cost_usd",
        new_column_name="cost_cents",
        existing_type=sa.Numeric(10, 4),
        type_=sa.Integer(),
        existing_server_default=sa.text("0"),
        server_default=sa.text("0"),
        postgresql_using="(cost_usd * 100)::integer",
    )
    op.alter_column(
        "budgets",
        "monthly_limit_usd",
        new_column_name="monthly_limit_cents",
        existing_type=sa.Numeric(10, 4),
        type_=sa.Integer(),
        postgresql_using="(monthly_limit_usd * 100)::integer",
    )
