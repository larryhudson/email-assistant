"""Verify the procrastinate queue plumbing: defer → worker → task body.

Uses `procrastinate.testing.InMemoryConnector` so we don't need Postgres.
Builds a parallel App that wraps the same task bodies but resolves deps
to stubs, then defers + runs the worker to drain the queue.

The point isn't to test procrastinate itself; it's to verify that *our*
defer wrappers and task registrations connect correctly — that
accept_inbound's run_agent_defer actually invokes execute_run when a
worker is running.
"""

from __future__ import annotations

import pytest
from procrastinate import App
from procrastinate.testing import InMemoryConnector


@pytest.mark.asyncio
async def test_run_agent_defer_then_worker_invokes_runtime() -> None:
    seen: list[str] = []

    class _StubRuntime:
        async def execute_run(self, run_id: str) -> object:
            seen.append(run_id)

            class _Outcome:
                pass

            return _Outcome()

    test_app = App(connector=InMemoryConnector())

    @test_app.task(name="run_agent")
    async def run_agent(run_id: str) -> str:
        from email_agent.jobs.run_agent import run_agent_impl

        outcome = await run_agent_impl(run_id=run_id, runtime=_StubRuntime())  # ty: ignore[invalid-argument-type]
        return outcome.__class__.__name__

    async with test_app.open_async():
        await run_agent.configure(queueing_lock="assistant-a-1").defer_async(run_id="r-42")
        await test_app.run_worker_async(wait=False)

    assert seen == ["r-42"]


@pytest.mark.asyncio
async def test_curate_memory_defer_then_worker_invokes_impl(
    sqlite_session_factory,
) -> None:
    """End-to-end: defer curate_memory → worker → curate_memory_impl runs against
    real DB rows + InMemoryMemoryAdapter. Mirrors what production does, minus
    Postgres."""
    from datetime import UTC, datetime
    from decimal import Decimal

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
    from email_agent.memory.inmemory import InMemoryMemoryAdapter

    async with sqlite_session_factory() as session:
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
                model="t",
                system_prompt="be kind",
            )
        )
        session.add(
            AssistantScopeRow(
                assistant_id="a-1", memory_namespace="mum", tool_allowlist=["read"], budget_id="b-1"
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
                body_text="The dog is called Biscuit.",
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
                body_text="Got it — Biscuit.",
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
            )
        )
        await session.commit()

    memory = InMemoryMemoryAdapter()
    test_app = App(connector=InMemoryConnector())

    @test_app.task(name="curate_memory")
    async def curate_memory(*, assistant_id: str, thread_id: str, run_id: str) -> None:
        from email_agent.jobs.curate_memory import curate_memory_impl

        await curate_memory_impl(
            assistant_id=assistant_id,
            thread_id=thread_id,
            run_id=run_id,
            session_factory=sqlite_session_factory,
            memory=memory,
        )

    async with test_app.open_async():
        await curate_memory.defer_async(assistant_id="a-1", thread_id="t-1", run_id="r-1")
        await test_app.run_worker_async(wait=False)

    biscuit_hits = await memory.search("a-1", "Biscuit")
    assert any("Biscuit" in m.content for m in biscuit_hits)
