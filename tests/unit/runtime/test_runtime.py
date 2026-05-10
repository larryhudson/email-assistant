from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import (
    Assistant,
    AssistantScopeRow,
    Budget,
    EmailMessage,
    EndUser,
    MessageIndex,
    Owner,
)
from email_agent.domain.router import RouteRejectionReason
from email_agent.models.email import NormalizedInboundEmail
from email_agent.runtime.assistant_runtime import (
    Accepted,
    AssistantRuntime,
    Dropped,
)


async def _seed_assistant(
    session: AsyncSession,
    *,
    inbound_address: str = "mum@assistants.example.com",
    sender: str = "mum@example.com",
) -> None:
    session.add(Owner(id="o-1", name="Larry"))
    await session.flush()
    session.add(EndUser(id="u-1", owner_id="o-1", email=sender))
    await session.flush()
    session.add(
        Assistant(
            id="a-1",
            end_user_id="u-1",
            inbound_address=inbound_address,
            status="active",
            allowed_senders=[sender],
            model="deepseek-flash",
            system_prompt="be kind",
        )
    )
    await session.flush()
    session.add(
        Budget(
            id="b-1",
            assistant_id="a-1",
            monthly_limit_usd=Decimal("10.00"),
            period_starts_at=datetime(2026, 5, 1, tzinfo=UTC),
            period_resets_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
    )
    await session.flush()
    session.add(
        AssistantScopeRow(
            assistant_id="a-1",
            memory_namespace="mum",
            tool_allowlist=["read"],
            budget_id="b-1",
        )
    )
    await session.commit()


def _inbound(
    *,
    to: str = "mum@assistants.example.com",
    sender: str = "mum@example.com",
    provider_message_id: str = "prov-1",
    message_id: str = "<m-1@example.com>",
) -> NormalizedInboundEmail:
    return NormalizedInboundEmail(
        provider_message_id=provider_message_id,
        message_id_header=message_id,
        from_email=sender,
        to_emails=[to],
        subject="hello",
        body_text="body",
        received_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
    )


async def test_accept_inbound_drops_unknown_address(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    runtime = AssistantRuntime(sqlite_session_factory, attachments_root=tmp_path)
    outcome = await runtime.accept_inbound(_inbound(to="who@example.com"))

    assert isinstance(outcome, Dropped)
    assert outcome.reason is RouteRejectionReason.UNKNOWN_ADDRESS

    async with sqlite_session_factory() as session:
        rows = (await session.execute(select(EmailMessage))).scalars().all()
        assert rows == []


async def test_accept_inbound_persists_message_and_index(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

    runtime = AssistantRuntime(sqlite_session_factory, attachments_root=tmp_path)
    outcome = await runtime.accept_inbound(_inbound())

    assert isinstance(outcome, Accepted)
    assert outcome.assistant_id == "a-1"
    assert outcome.message_id.startswith("m-")
    assert outcome.thread_id.startswith("t-")

    async with sqlite_session_factory() as session:
        messages = (await session.execute(select(EmailMessage))).scalars().all()
        index_rows = (await session.execute(select(MessageIndex))).scalars().all()
        assert len(messages) == 1
        assert len(index_rows) == 1


async def test_accept_inbound_is_idempotent_on_duplicate_delivery(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

    runtime = AssistantRuntime(sqlite_session_factory, attachments_root=tmp_path)
    first = await runtime.accept_inbound(_inbound())
    second = await runtime.accept_inbound(_inbound())

    assert isinstance(first, Accepted)
    assert isinstance(second, Accepted)
    assert first.message_id == second.message_id
    assert first.created is True
    assert second.created is False

    async with sqlite_session_factory() as session:
        messages = (await session.execute(select(EmailMessage))).scalars().all()
        assert len(messages) == 1


async def test_accept_inbound_writes_agent_run_queued(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    from email_agent.db.models import AgentRun

    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

    runtime = AssistantRuntime(sqlite_session_factory, attachments_root=tmp_path)
    first = await runtime.accept_inbound(_inbound())
    assert isinstance(first, Accepted)

    async with sqlite_session_factory() as session:
        runs = (await session.execute(select(AgentRun))).scalars().all()
        assert len(runs) == 1
        run = runs[0]
        assert run.assistant_id == "a-1"
        assert run.thread_id == first.thread_id
        assert run.inbound_message_id == first.message_id
        assert run.status == "queued"

    # Duplicate delivery → still one AgentRun row.
    second = await runtime.accept_inbound(_inbound())
    assert isinstance(second, Accepted)

    async with sqlite_session_factory() as session:
        runs = (await session.execute(select(AgentRun))).scalars().all()
        assert len(runs) == 1


async def test_accept_inbound_calls_run_agent_defer_callback(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    """Once the queued AgentRun row commits, accept_inbound must invoke
    the injected `run_agent_defer` callback so a Procrastinate worker
    will pick the run up. Drops, sender-rejections, and duplicate deliveries
    must NOT enqueue."""
    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

    deferred: list[dict[str, str]] = []

    async def fake_defer(*, run_id: str, assistant_id: str) -> None:
        deferred.append({"run_id": run_id, "assistant_id": assistant_id})

    runtime = AssistantRuntime(
        sqlite_session_factory,
        attachments_root=tmp_path,
        run_agent_defer=fake_defer,
    )

    accepted = await runtime.accept_inbound(_inbound())
    assert isinstance(accepted, Accepted)
    assert len(deferred) == 1
    assert deferred[0]["assistant_id"] == "a-1"
    # The run_id should reference the AgentRun row that was just queued.
    async with sqlite_session_factory() as session:
        from email_agent.db.models import AgentRun as _AgentRun

        run = (await session.execute(select(_AgentRun))).scalar_one()
        assert deferred[0]["run_id"] == run.id

    # Duplicate delivery: still one AgentRun, no second deferral.
    again = await runtime.accept_inbound(_inbound())
    assert isinstance(again, Accepted)
    assert len(deferred) == 1


async def test_accept_inbound_does_not_defer_on_drop(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    deferred: list[dict[str, str]] = []

    async def fake_defer(*, run_id: str, assistant_id: str) -> None:
        deferred.append({"run_id": run_id, "assistant_id": assistant_id})

    runtime = AssistantRuntime(
        sqlite_session_factory,
        attachments_root=tmp_path,
        run_agent_defer=fake_defer,
    )

    outcome = await runtime.accept_inbound(_inbound(to="who@example.com"))
    assert isinstance(outcome, Dropped)
    assert deferred == []
