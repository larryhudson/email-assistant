from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import (
    Assistant,
    AssistantScopeRow,
    Budget,
    EndUser,
    Owner,
)
from email_agent.domain.router import (
    AssistantRouter,
    Routed,
    RouteRejection,
    RouteRejectionReason,
)
from email_agent.models.assistant import AssistantStatus
from email_agent.models.email import NormalizedInboundEmail


async def _seed_assistant(
    session: AsyncSession,
    *,
    inbound_address: str = "mum@assistants.example.com",
    status: str = "active",
    allowed_senders: list[str] | None = None,
) -> None:
    if allowed_senders is None:
        allowed_senders = ["mum@example.com"]
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
            inbound_address=inbound_address,
            status=status,
            allowed_senders=allowed_senders,
            model="deepseek-flash",
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
    await session.commit()


def _inbound(
    *,
    to: str = "mum@assistants.example.com",
    sender: str = "mum@example.com",
) -> NormalizedInboundEmail:
    return NormalizedInboundEmail(
        provider_message_id="prov-1",
        message_id_header="<msg-1@example.com>",
        from_email=sender,
        to_emails=[to],
        subject="hi",
        body_text="hello",
        received_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
    )


async def test_router_resolves_known_address_to_assistant_scope(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

    router = AssistantRouter(sqlite_session_factory)
    outcome = await router.resolve(_inbound())

    assert isinstance(outcome, Routed)
    assert outcome.scope.assistant_id == "a-1"
    assert outcome.scope.status is AssistantStatus.ACTIVE
    assert outcome.scope.allowed_senders == ("mum@example.com",)


async def test_router_rejects_unknown_address(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    router = AssistantRouter(sqlite_session_factory)
    outcome = await router.resolve(_inbound(to="who-dis@example.com"))

    assert isinstance(outcome, RouteRejection)
    assert outcome.reason is RouteRejectionReason.UNKNOWN_ADDRESS


async def test_router_rejects_paused_assistant(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    async with sqlite_session_factory() as session:
        await _seed_assistant(session, status="paused")

    router = AssistantRouter(sqlite_session_factory)
    outcome = await router.resolve(_inbound())

    assert isinstance(outcome, RouteRejection)
    assert outcome.reason is RouteRejectionReason.ASSISTANT_PAUSED


async def test_router_rejects_disabled_assistant(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    async with sqlite_session_factory() as session:
        await _seed_assistant(session, status="disabled")

    router = AssistantRouter(sqlite_session_factory)
    outcome = await router.resolve(_inbound())

    assert isinstance(outcome, RouteRejection)
    assert outcome.reason is RouteRejectionReason.ASSISTANT_DISABLED


async def test_router_rejects_sender_not_in_allowlist(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    async with sqlite_session_factory() as session:
        await _seed_assistant(session, allowed_senders=["mum@example.com"])

    router = AssistantRouter(sqlite_session_factory)
    outcome = await router.resolve(_inbound(sender="randoms@example.com"))

    assert isinstance(outcome, RouteRejection)
    assert outcome.reason is RouteRejectionReason.SENDER_NOT_ALLOWED


async def test_router_accepts_allowlisted_sender_case_insensitively(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    async with sqlite_session_factory() as session:
        await _seed_assistant(session, allowed_senders=["Mum@Example.com"])

    router = AssistantRouter(sqlite_session_factory)
    outcome = await router.resolve(_inbound(sender="MUM@example.COM"))

    assert isinstance(outcome, Routed)
