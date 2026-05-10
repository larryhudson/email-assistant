from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import (
    EmailAttachmentRow,
    EmailMessage,
    EmailThread,
    MessageIndex,
)
from email_agent.domain.inbound_persister import PersistedInbound, persist_inbound
from email_agent.models.assistant import AssistantScope, AssistantStatus
from email_agent.models.email import EmailAttachment, NormalizedInboundEmail


def _scope() -> AssistantScope:
    return AssistantScope(
        assistant_id="a-1",
        owner_id="o-1",
        end_user_id="u-1",
        inbound_address="a-1@assistants.example.com",
        status=AssistantStatus.ACTIVE,
        allowed_senders=("mum@example.com",),
        memory_namespace="mum",
        tool_allowlist=("read",),
        budget_id="b-1",
        model_name="deepseek-flash",
        system_prompt="be kind",
    )


def _inbound(
    *,
    provider_message_id: str = "prov-1",
    message_id: str = "<m-1@example.com>",
    attachments: list[EmailAttachment] | None = None,
) -> NormalizedInboundEmail:
    return NormalizedInboundEmail(
        provider_message_id=provider_message_id,
        message_id_header=message_id,
        from_email="mum@example.com",
        to_emails=["a-1@assistants.example.com"],
        subject="hi",
        body_text="hello",
        attachments=attachments or [],
        received_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
    )


async def _seed_thread(session: AsyncSession, *, thread_id: str = "t-1") -> EmailThread:
    thread = EmailThread(
        id=thread_id,
        assistant_id="a-1",
        end_user_id="u-1",
        root_message_id="<m-1@example.com>",
        subject_normalized="hi",
    )
    session.add(thread)
    await session.commit()
    return thread


async def test_persist_inbound_writes_message_and_index(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    async with sqlite_session_factory() as session:
        thread = await _seed_thread(session)

    async with sqlite_session_factory() as session:
        result = await persist_inbound(
            session,
            email=_inbound(),
            scope=_scope(),
            thread=thread,
            attachments_root=tmp_path,
        )
        await session.commit()

    assert isinstance(result, PersistedInbound)
    assert result.created is True
    assert result.message.thread_id == "t-1"
    assert result.message.assistant_id == "a-1"
    assert result.message.direction == "inbound"

    async with sqlite_session_factory() as session:
        messages = (await session.execute(select(EmailMessage))).scalars().all()
        index_rows = (await session.execute(select(MessageIndex))).scalars().all()
        assert len(messages) == 1
        assert len(index_rows) == 1
        assert index_rows[0].message_id_header == "<m-1@example.com>"
        assert index_rows[0].thread_id == "t-1"


async def test_persist_inbound_writes_attachment_bytes_to_disk(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    attachment = EmailAttachment(
        filename="receipt.pdf",
        content_type="application/pdf",
        size_bytes=4,
        data=b"%PDF",
    )
    async with sqlite_session_factory() as session:
        thread = await _seed_thread(session)

    async with sqlite_session_factory() as session:
        result = await persist_inbound(
            session,
            email=_inbound(attachments=[attachment]),
            scope=_scope(),
            thread=thread,
            attachments_root=tmp_path,
        )
        await session.commit()

    async with sqlite_session_factory() as session:
        rows = (await session.execute(select(EmailAttachmentRow))).scalars().all()
    assert len(rows) == 1
    stored = Path(rows[0].storage_path)
    assert stored.exists()  # noqa: ASYNC240
    assert stored.read_bytes() == b"%PDF"  # noqa: ASYNC240
    assert stored.parent.name == result.message.id


async def test_persist_inbound_is_idempotent_on_duplicate_delivery(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    async with sqlite_session_factory() as session:
        thread = await _seed_thread(session)

    async with sqlite_session_factory() as session:
        first = await persist_inbound(
            session,
            email=_inbound(),
            scope=_scope(),
            thread=thread,
            attachments_root=tmp_path,
        )
        await session.commit()

    async with sqlite_session_factory() as session:
        thread = await session.get(EmailThread, "t-1")
        assert thread is not None
        second = await persist_inbound(
            session,
            email=_inbound(),
            scope=_scope(),
            thread=thread,
            attachments_root=tmp_path,
        )
        await session.commit()

    assert first.created is True
    assert second.created is False
    assert first.message.id == second.message.id

    async with sqlite_session_factory() as session:
        message_count = len((await session.execute(select(EmailMessage))).scalars().all())
        index_count = len((await session.execute(select(MessageIndex))).scalars().all())
        assert message_count == 1
        assert index_count == 1
