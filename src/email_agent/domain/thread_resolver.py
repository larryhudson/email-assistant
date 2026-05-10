import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import EmailThread, MessageIndex
from email_agent.models.assistant import AssistantScope
from email_agent.models.email import NormalizedInboundEmail

_RE_PREFIX = re.compile(r"^\s*(re|fwd?):\s*", re.IGNORECASE)


def _normalize_subject(subject: str) -> str:
    """Strip leading Re:/Fwd: noise so replies group under the same subject."""
    prev = None
    current = subject.strip()
    while current != prev:
        prev = current
        current = _RE_PREFIX.sub("", current).strip()
    return current


class ThreadResolver:
    """Maps an inbound email to its `EmailThread` row.

    Resolution order, all scoped by `assistant_id`:
    1. `In-Reply-To` header → `message_index`.
    2. `References` headers → `message_index` (last-to-first).
    3. Otherwise create a new thread.

    Cross-assistant lookups never match — the unique key is
    `(assistant_id, message_id_header)`.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def resolve(self, email: NormalizedInboundEmail, scope: AssistantScope) -> EmailThread:
        async with self._session_factory() as session:
            thread = await self._resolve(session, email, scope)
            await session.commit()
            return thread

    async def _resolve(
        self,
        session: AsyncSession,
        email: NormalizedInboundEmail,
        scope: AssistantScope,
    ) -> EmailThread:
        if email.in_reply_to_header:
            existing = await self._find_thread_by_message_id(
                session, scope.assistant_id, email.in_reply_to_header
            )
            if existing is not None:
                return existing
        for ref in reversed(email.references_headers):
            existing = await self._find_thread_by_message_id(session, scope.assistant_id, ref)
            if existing is not None:
                return existing
        return await self._create_new_thread(session, email, scope)

    async def _find_thread_by_message_id(
        self,
        session: AsyncSession,
        assistant_id: str,
        message_id_header: str,
    ) -> EmailThread | None:
        stmt = (
            select(EmailThread)
            .join(MessageIndex, MessageIndex.thread_id == EmailThread.id)
            .where(
                MessageIndex.assistant_id == assistant_id,
                MessageIndex.message_id_header == message_id_header,
            )
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _create_new_thread(
        self,
        session: AsyncSession,
        email: NormalizedInboundEmail,
        scope: AssistantScope,
    ) -> EmailThread:
        thread = EmailThread(
            id=f"t-{uuid.uuid4().hex[:12]}",
            assistant_id=scope.assistant_id,
            end_user_id=scope.end_user_id,
            root_message_id=email.message_id_header,
            subject_normalized=_normalize_subject(email.subject),
        )
        session.add(thread)
        await session.flush()
        return thread
