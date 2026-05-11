"""End-to-end integration test for the scheduled-tasks tick against real
Postgres. Verifies the cross-session flow (claim with FOR UPDATE SKIP LOCKED
→ accept_inbound in a child session → tag agent_run + mark_fired in the
outer session) that the unit tests can't exercise on sqlite.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from email_agent.config import Settings
from email_agent.db.models import (
    AgentRun,
    Assistant,
    AssistantScopeRow,
    Budget,
    EmailMessage,
    EndUser,
    Owner,
    ScheduledTaskRow,
)
from email_agent.db.session import make_engine, make_session_factory, session_scope
from email_agent.models.scheduled import ScheduledTaskKind, ScheduledTaskStatus
from email_agent.runtime.assistant_runtime import AssistantRuntime
from email_agent.scheduled.tick import tick_scheduled_tasks_impl

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="needs db")
async def test_tick_fires_once_and_cron_against_real_postgres(tmp_path):
    settings = Settings()  # ty: ignore[missing-argument]
    engine = make_engine(settings)
    factory = make_session_factory(engine)

    suffix = uuid.uuid4().hex[:8]
    sender = f"user-{suffix}@example.com"
    inbound = f"assistant-{suffix}@assistants.example.com"

    async with session_scope(factory) as s:
        s.add(Owner(id=f"o-{suffix}", name="Larry"))
        await s.flush()
        s.add(EndUser(id=f"u-{suffix}", owner_id=f"o-{suffix}", email=sender))
        await s.flush()
        s.add(
            Assistant(
                id=f"a-{suffix}",
                end_user_id=f"u-{suffix}",
                inbound_address=inbound,
                status="active",
                allowed_senders=[sender],
                model="deepseek-flash",
                system_prompt="be kind",
            )
        )
        await s.flush()
        s.add(
            Budget(
                id=f"b-{suffix}",
                assistant_id=f"a-{suffix}",
                monthly_limit_usd=Decimal("10.00"),
                period_starts_at=datetime(2026, 5, 1, tzinfo=UTC),
                period_resets_at=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )
        await s.flush()
        s.add(
            AssistantScopeRow(
                assistant_id=f"a-{suffix}",
                memory_namespace=f"ns-{suffix}",
                tool_allowlist=["read"],
                budget_id=f"b-{suffix}",
            )
        )

    # `run_agent_defer=None` keeps accept_inbound from trying to enqueue a
    # procrastinate job — we only care about routing + persistence here.
    runtime = AssistantRuntime(factory, attachments_root=tmp_path)

    service = runtime.scheduled_tasks

    now = datetime.now(UTC)
    once = await service.create_once(
        assistant_id=f"a-{suffix}",
        run_at=now - timedelta(minutes=1),
        name="qa-once",
        body="once-body",
    )
    cron = await service.create_cron(
        assistant_id=f"a-{suffix}",
        cron_expr="*/5 * * * *",
        name="qa-cron",
        body="cron-body",
    )
    # Pretend the cron was due alongside the once-task so a single tick fires both.
    async with session_scope(factory) as s:
        row = await s.get(ScheduledTaskRow, cron.id)
        assert row is not None
        row.next_run_at = now - timedelta(seconds=1)

    fire_at = now
    await tick_scheduled_tasks_impl(
        runtime=runtime,
        service=service,
        session_factory=factory,
        now=fire_at,
    )

    async with session_scope(factory) as s:
        runs = (
            (
                await s.execute(
                    select(AgentRun)
                    .where(AgentRun.assistant_id == f"a-{suffix}")
                    .order_by(AgentRun.started_at)
                )
            )
            .scalars()
            .all()
        )
        # One AgentRun per fired task, each tagged with its triggering row.
        assert len(runs) == 2
        triggered_ids = {r.triggered_by_scheduled_task_id for r in runs}
        assert triggered_ids == {once.id, cron.id}
        assert all(r.status == "queued" for r in runs)

        # The synthetic inbound's body carries the trigger marker so the agent
        # (and admin UI) can tell where the run came from.
        inbounds = (
            (
                await s.execute(
                    select(EmailMessage)
                    .where(
                        EmailMessage.assistant_id == f"a-{suffix}",
                        EmailMessage.direction == "inbound",
                    )
                    .order_by(EmailMessage.created_at)
                )
            )
            .scalars()
            .all()
        )
        assert len(inbounds) == 2
        bodies = sorted(m.body_text for m in inbounds)
        assert any(b.startswith("[Triggered by scheduled task 'qa-cron'") for b in bodies)
        assert any(b.startswith("[Triggered by scheduled task 'qa-once'") for b in bodies)
        # The user-supplied body is preserved after the marker.
        assert any("cron-body" in b for b in bodies)
        assert any("once-body" in b for b in bodies)

        # The 'once' row is completed; the cron row advanced to its next slot.
        scheduled = (
            (
                await s.execute(
                    select(ScheduledTaskRow)
                    .where(ScheduledTaskRow.assistant_id == f"a-{suffix}")
                    .order_by(ScheduledTaskRow.id)
                )
            )
            .scalars()
            .all()
        )
        by_id = {r.id: r for r in scheduled}
        assert by_id[once.id].status == ScheduledTaskStatus.COMPLETED.value
        assert by_id[once.id].last_run_at == fire_at
        assert by_id[cron.id].kind == ScheduledTaskKind.CRON.value
        assert by_id[cron.id].status == ScheduledTaskStatus.ACTIVE.value
        assert by_id[cron.id].next_run_at > fire_at
        assert by_id[cron.id].last_run_at == fire_at
