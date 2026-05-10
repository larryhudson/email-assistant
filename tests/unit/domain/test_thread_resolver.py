from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import EmailThread, MessageIndex
from email_agent.domain.thread_resolver import ThreadResolver
from email_agent.models.assistant import AssistantScope, AssistantStatus
from email_agent.models.email import NormalizedInboundEmail


def _scope(assistant_id: str = "a-1", end_user_id: str = "u-1") -> AssistantScope:
    return AssistantScope(
        assistant_id=assistant_id,
        owner_id="o-1",
        end_user_id=end_user_id,
        inbound_address=f"{assistant_id}@assistants.example.com",
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
    message_id: str = "<m-new@example.com>",
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    subject: str = "hi",
) -> NormalizedInboundEmail:
    return NormalizedInboundEmail(
        provider_message_id=message_id.strip("<>"),
        message_id_header=message_id,
        in_reply_to_header=in_reply_to,
        references_headers=references or [],
        from_email="mum@example.com",
        to_emails=["a-1@assistants.example.com"],
        subject=subject,
        body_text="body",
        received_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
    )


async def _seed_indexed_message(
    session: AsyncSession,
    *,
    assistant_id: str,
    end_user_id: str,
    thread_id: str,
    message_id_header: str,
    provider_message_id: str = "prov-prev",
    subject: str = "hi",
) -> None:
    session.add(
        EmailThread(
            id=thread_id,
            assistant_id=assistant_id,
            end_user_id=end_user_id,
            root_message_id=message_id_header,
            subject_normalized=subject,
        )
    )
    session.add(
        MessageIndex(
            assistant_id=assistant_id,
            message_id_header=message_id_header,
            thread_id=thread_id,
            provider_message_id=provider_message_id,
        )
    )
    await session.commit()


async def test_resolver_creates_new_thread_when_no_headers_match(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    resolver = ThreadResolver(sqlite_session_factory)
    thread = await resolver.resolve(_inbound(), _scope())

    assert thread.assistant_id == "a-1"
    assert thread.subject_normalized == "hi"
    assert thread.root_message_id == "<m-new@example.com>"

    async with sqlite_session_factory() as session:
        rows = (await session.execute(select(EmailThread))).scalars().all()
        assert len(rows) == 1
        assert rows[0].id == thread.id


async def test_resolver_matches_thread_by_in_reply_to(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    async with sqlite_session_factory() as session:
        await _seed_indexed_message(
            session,
            assistant_id="a-1",
            end_user_id="u-1",
            thread_id="t-existing",
            message_id_header="<prev@example.com>",
        )

    resolver = ThreadResolver(sqlite_session_factory)
    thread = await resolver.resolve(
        _inbound(in_reply_to="<prev@example.com>"),
        _scope(),
    )

    assert thread.id == "t-existing"


async def test_resolver_falls_back_to_references_when_in_reply_to_misses(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    async with sqlite_session_factory() as session:
        await _seed_indexed_message(
            session,
            assistant_id="a-1",
            end_user_id="u-1",
            thread_id="t-references",
            message_id_header="<root@example.com>",
        )

    resolver = ThreadResolver(sqlite_session_factory)
    thread = await resolver.resolve(
        _inbound(
            in_reply_to="<missing@example.com>",
            references=["<root@example.com>", "<missing@example.com>"],
        ),
        _scope(),
    )

    assert thread.id == "t-references"


async def test_resolver_does_not_match_threads_from_other_assistants(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    async with sqlite_session_factory() as session:
        await _seed_indexed_message(
            session,
            assistant_id="a-other",
            end_user_id="u-other",
            thread_id="t-other",
            message_id_header="<shared@example.com>",
        )

    resolver = ThreadResolver(sqlite_session_factory)
    thread = await resolver.resolve(
        _inbound(in_reply_to="<shared@example.com>"),
        _scope(assistant_id="a-1", end_user_id="u-1"),
    )

    assert thread.id != "t-other"
    assert thread.assistant_id == "a-1"


async def test_resolver_normalizes_subject_strips_re_and_fwd(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    resolver = ThreadResolver(sqlite_session_factory)
    thread = await resolver.resolve(
        _inbound(subject="Re: Fwd: hello there"),
        _scope(),
    )
    assert thread.subject_normalized == "hello there"
