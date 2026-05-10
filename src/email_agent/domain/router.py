from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import (
    Assistant,
    AssistantScopeRow,
    EndUser,
    Owner,
)
from email_agent.models.assistant import AssistantScope, AssistantStatus
from email_agent.models.email import NormalizedInboundEmail


class RouteRejectionReason(StrEnum):
    """Why an inbound email was dropped before reaching the agent."""

    UNKNOWN_ADDRESS = "unknown_address"
    ASSISTANT_PAUSED = "assistant_paused"
    ASSISTANT_DISABLED = "assistant_disabled"
    SENDER_NOT_ALLOWED = "sender_not_allowed"


@dataclass(frozen=True)
class Routed:
    """Successful route — the inbound belongs to this assistant."""

    scope: AssistantScope


@dataclass(frozen=True)
class RouteRejection:
    """Inbound dropped before any DB writes for the agent run."""

    reason: RouteRejectionReason
    detail: str


RouteOutcome = Routed | RouteRejection


class AssistantRouter:
    """Resolves an inbound email's `to` address to an `AssistantScope`.

    Drops unknown addresses, paused/disabled assistants, and senders not in
    the assistant's allowlist. Owns its own session because routing happens
    at the very edge of the webhook fast path, before any orchestrator.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def resolve(self, email: NormalizedInboundEmail) -> RouteOutcome:
        async with self._session_factory() as session:
            return await self._resolve(session, email)

    async def _resolve(self, session: AsyncSession, email: NormalizedInboundEmail) -> RouteOutcome:
        for to_address in email.to_emails:
            scope = await self._lookup(session, to_address)
            if scope is None:
                continue
            if scope.status is AssistantStatus.PAUSED:
                return RouteRejection(
                    reason=RouteRejectionReason.ASSISTANT_PAUSED,
                    detail=f"assistant {scope.assistant_id} is paused",
                )
            if scope.status is AssistantStatus.DISABLED:
                return RouteRejection(
                    reason=RouteRejectionReason.ASSISTANT_DISABLED,
                    detail=f"assistant {scope.assistant_id} is disabled",
                )
            if not scope.is_sender_allowed(email.from_email):
                return RouteRejection(
                    reason=RouteRejectionReason.SENDER_NOT_ALLOWED,
                    detail=f"{email.from_email} not in allowlist for {scope.assistant_id}",
                )
            return Routed(scope=scope)
        return RouteRejection(
            reason=RouteRejectionReason.UNKNOWN_ADDRESS,
            detail=f"no assistant for {email.to_emails}",
        )

    async def _lookup(self, session: AsyncSession, inbound_address: str) -> AssistantScope | None:
        stmt = (
            select(Owner, EndUser, Assistant, AssistantScopeRow)
            .join(EndUser, EndUser.owner_id == Owner.id)
            .join(Assistant, Assistant.end_user_id == EndUser.id)
            .join(AssistantScopeRow, AssistantScopeRow.assistant_id == Assistant.id)
            .where(Assistant.inbound_address == inbound_address)
        )
        row = (await session.execute(stmt)).first()
        if row is None:
            return None
        owner, end_user, assistant, scope_row = row
        return AssistantScope.from_rows(
            owner=owner,
            end_user=end_user,
            assistant=assistant,
            scope_row=scope_row,
        )
