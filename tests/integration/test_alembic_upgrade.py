import os

import pytest
from sqlalchemy import text

from email_agent.config import Settings
from email_agent.db.session import make_engine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _have_db() -> bool:
    return "DATABASE_URL" in os.environ


@pytest.mark.skipif(not _have_db(), reason="DATABASE_URL not set")
async def test_alembic_upgrade_head_creates_expected_tables():
    engine = make_engine(Settings())  # ty: ignore[missing-argument]
    expected = {
        "owners",
        "admins",
        "end_users",
        "assistants",
        "assistant_scopes",
        "email_threads",
        "email_messages",
        "email_attachments",
        "message_index",
        "agent_runs",
        "run_steps",
        "run_memory_recalls",
        "usage_ledger",
        "budgets",
        "alembic_version",
        # Procrastinate-owned tables — applied via the proc01 alembic revision.
        "procrastinate_jobs",
        "procrastinate_events",
        "procrastinate_periodic_defers",
        "procrastinate_workers",
    }
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))
        present = {r[0] for r in rows}
    await engine.dispose()
    missing = expected - present
    assert not missing, f"missing tables: {missing}"
