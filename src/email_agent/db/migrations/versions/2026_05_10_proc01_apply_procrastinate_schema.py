"""apply procrastinate schema

Installs Procrastinate's tables (procrastinate_jobs, procrastinate_events,
procrastinate_periodic_defers, etc.) alongside our own. Single
`alembic upgrade head` brings up everything.

The schema SQL is sourced from `procrastinate.schema.SchemaManager.get_schema()`
so we're locked to whatever version is currently installed (3.8.1 at
write time). If procrastinate updates its schema, add a new alembic
revision rather than editing this one — same as any other DDL change.

Downgrade drops every procrastinate-owned table/type. Destructive, but
that's what alembic downgrade implies; in practice we don't downgrade.

Revision ID: proc01
Revises: ece0061869b1
Create Date: 2026-05-10 18:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
from procrastinate import SyncPsycopgConnector
from procrastinate.schema import SchemaManager
from sqlalchemy import text
from sqlalchemy.engine.url import make_url

revision: str = "proc01"
down_revision: str | None = "ece0061869b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Procrastinate's schema is multi-statement DDL (CREATE FUNCTION bodies
    # etc.). Asyncpg — which alembic uses via SQLAlchemy create_async_engine —
    # refuses multi-statement prepared queries, so we use procrastinate's own
    # sync psycopg connector for this one DDL apply. Same DB, different driver.
    from email_agent.config import Settings

    settings = Settings()  # ty: ignore[missing-argument]
    psycopg_url = make_url(str(settings.database_url)).set(drivername="postgresql")
    connector = SyncPsycopgConnector(conninfo=psycopg_url.render_as_string(hide_password=False))
    connector.open()
    try:
        SchemaManager(connector=connector).apply_schema()
    finally:
        connector.close()


def downgrade() -> None:
    # Procrastinate doesn't ship a downgrade SQL and explicitly punts on the
    # question (procrastinate-org/procrastinate#1040). Hardcoding a drop list
    # bit-rots when procrastinate adds tables in a future release. Instead,
    # discover everything in the `procrastinate_*` namespace and drop it.
    # Same psycopg connector trick as upgrade — we issue separate single-
    # statement queries so asyncpg's prepared protocol doesn't choke.
    bind = op.get_bind()

    table_rows = bind.execute(
        text(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename LIKE 'procrastinate_%'"
        )
    ).fetchall()
    for (name,) in table_rows:
        op.execute(f'DROP TABLE IF EXISTS "{name}" CASCADE')

    type_rows = bind.execute(
        text(
            "SELECT typname FROM pg_type t "
            "JOIN pg_namespace n ON t.typnamespace = n.oid "
            "WHERE n.nspname = 'public' AND t.typname LIKE 'procrastinate_%'"
        )
    ).fetchall()
    for (name,) in type_rows:
        op.execute(f'DROP TYPE IF EXISTS "{name}" CASCADE')

    fn_rows = bind.execute(
        text(
            "SELECT proname FROM pg_proc p "
            "JOIN pg_namespace n ON p.pronamespace = n.oid "
            "WHERE n.nspname = 'public' AND p.proname LIKE 'procrastinate_%'"
        )
    ).fetchall()
    for (name,) in fn_rows:
        op.execute(f'DROP FUNCTION IF EXISTS "{name}" CASCADE')
