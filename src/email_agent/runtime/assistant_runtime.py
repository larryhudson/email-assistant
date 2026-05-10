from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.domain.inbound_persister import persist_inbound
from email_agent.domain.router import (
    AssistantRouter,
    Routed,
    RouteRejection,
    RouteRejectionReason,
)
from email_agent.domain.thread_resolver import ThreadResolver
from email_agent.models.email import NormalizedInboundEmail


@dataclass(frozen=True)
class Accepted:
    """Inbound persisted; webhook should return 200."""

    assistant_id: str
    thread_id: str
    message_id: str
    created: bool


@dataclass(frozen=True)
class Dropped:
    """Inbound rejected before persistence; webhook should still return 200."""

    reason: RouteRejectionReason
    detail: str


AcceptOutcome = Accepted | Dropped


class AssistantRuntime:
    """Webhook fast-path orchestrator.

    Composes router → thread resolver → persister. Slice-2 scope: stops at
    persisting the inbound message + message_index entry. Procrastinate
    enqueue and AgentRun row creation land in slice 5 alongside execute_run.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        attachments_root: Path,
    ) -> None:
        self._session_factory = session_factory
        self._attachments_root = attachments_root
        self._router = AssistantRouter(session_factory)
        self._resolver = ThreadResolver(session_factory)

    async def accept_inbound(self, email: NormalizedInboundEmail) -> AcceptOutcome:
        outcome = await self._router.resolve(email)
        if isinstance(outcome, RouteRejection):
            return Dropped(reason=outcome.reason, detail=outcome.detail)
        assert isinstance(outcome, Routed)
        scope = outcome.scope

        thread = await self._resolver.resolve(email, scope)

        async with self._session_factory() as session:
            attached_thread = await session.merge(thread)
            persisted = await persist_inbound(
                session,
                email=email,
                scope=scope,
                thread=attached_thread,
                attachments_root=self._attachments_root,
            )
            await session.commit()
            return Accepted(
                assistant_id=scope.assistant_id,
                thread_id=attached_thread.id,
                message_id=persisted.message.id,
                created=persisted.created,
            )
