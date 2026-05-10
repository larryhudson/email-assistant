"""Unit tests for the curate_memory task body.

We test the underlying coroutine directly — Procrastinate's queue/dispatch
is integration-tested separately against real Postgres. The body is a
plain coroutine that loads a run's inbound + outbound messages and calls
`memory.record_turn` for each.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import (
    AgentRun,
    Assistant,
    AssistantScopeRow,
    Budget,
    EmailMessage,
    EmailThread,
    EndUser,
    Owner,
)
from email_agent.jobs.curate_memory import curate_memory_impl
from email_agent.memory.inmemory import InMemoryMemoryAdapter


async def _seed_run(session: AsyncSession) -> tuple[str, str, str]:
    """Seed an assistant + thread + run with one inbound and one outbound
    message. Returns (assistant_id, thread_id, run_id)."""
    session.add(Owner(id="o-1", name="Larry"))
    session.add(EndUser(id="u-1", owner_id="o-1", email="mum@example.com"))
    session.add(
        Budget(
            id="b-1",
            assistant_id="a-1",
            monthly_limit_usd=Decimal("10.00"),
            period_starts_at=datetime(2026, 5, 1, tzinfo=UTC),
            period_resets_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
    )
    session.add(
        Assistant(
            id="a-1",
            end_user_id="u-1",
            inbound_address="mum@assistants.example.com",
            status="active",
            allowed_senders=["mum@example.com"],
            model="test-model",
            system_prompt="be kind",
        )
    )
    session.add(
        AssistantScopeRow(
            assistant_id="a-1",
            memory_namespace="mum",
            tool_allowlist=["read"],
            budget_id="b-1",
        )
    )
    session.add(
        EmailThread(
            id="t-1",
            assistant_id="a-1",
            end_user_id="u-1",
            root_message_id="<m-in@x>",
            subject_normalized="hello",
        )
    )
    session.add(
        EmailMessage(
            id="m-in",
            thread_id="t-1",
            assistant_id="a-1",
            direction="inbound",
            provider_message_id="prov-in-1",
            message_id_header="<m-in@x>",
            from_email="mum@example.com",
            to_emails=["mum@assistants.example.com"],
            subject="hello",
            body_text="Hi Rose, I just got back from holiday in Italy.",
            body_html=None,
        )
    )
    session.add(
        EmailMessage(
            id="m-out",
            thread_id="t-1",
            assistant_id="a-1",
            direction="outbound",
            provider_message_id="prov-out-1",
            message_id_header="<m-out@x>",
            from_email="mum@assistants.example.com",
            to_emails=["mum@example.com"],
            subject="Re: hello",
            body_text="Welcome back! How was the trip?",
            body_html=None,
        )
    )
    session.add(
        AgentRun(
            id="r-1",
            assistant_id="a-1",
            thread_id="t-1",
            inbound_message_id="m-in",
            reply_message_id="m-out",
            status="completed",
            started_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 5, 10, 12, 0, 30, tzinfo=UTC),
        )
    )
    await session.commit()
    return "a-1", "t-1", "r-1"


@pytest.mark.asyncio
async def test_curate_memory_writes_user_and_assistant_turns(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as session:
        assistant_id, thread_id, run_id = await _seed_run(session)

    memory = InMemoryMemoryAdapter()

    await curate_memory_impl(
        assistant_id=assistant_id,
        thread_id=thread_id,
        run_id=run_id,
        session_factory=sqlite_session_factory,
        memory=memory,
    )

    # Both turns landed under the right (assistant, thread) — InMemoryMemoryAdapter
    # prefixes content with "[<thread>/<role>] ", so we can introspect via search.
    user_hits = await memory.search(assistant_id, "Italy")
    assistant_hits = await memory.search(assistant_id, "Welcome back")
    assert any("user" in m.content for m in user_hits)
    assert any("assistant" in m.content for m in assistant_hits)


@pytest.mark.asyncio
async def test_curate_memory_skips_when_outbound_missing(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A failed/budget-limited run may have no outbound. The user message
    should still be persisted so future runs can recall the question."""
    async with sqlite_session_factory() as session:
        assistant_id, thread_id, run_id = await _seed_run(session)
        # Drop the outbound and unlink it from the run.
        run = await session.get(AgentRun, run_id)
        assert run is not None
        run.reply_message_id = None
        outbound = await session.get(EmailMessage, "m-out")
        assert outbound is not None
        await session.delete(outbound)
        await session.commit()

    memory = InMemoryMemoryAdapter()

    await curate_memory_impl(
        assistant_id=assistant_id,
        thread_id=thread_id,
        run_id=run_id,
        session_factory=sqlite_session_factory,
        memory=memory,
    )

    user_hits = await memory.search(assistant_id, "Italy")
    assistant_hits = await memory.search(assistant_id, "Welcome back")
    assert any("user" in m.content for m in user_hits)
    assert assistant_hits == []
