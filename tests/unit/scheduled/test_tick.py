from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import Assistant, Budget, EndUser, Owner
from email_agent.models.email import NormalizedInboundEmail
from email_agent.models.scheduled import ScheduledTaskKind, ScheduledTaskStatus
from email_agent.scheduled.service import ScheduledTaskService
from email_agent.scheduled.tick import tick_scheduled_tasks_impl


class _FakeRuntime:
    def __init__(self, service: ScheduledTaskService) -> None:
        self.scheduled_tasks = service
        self.accepted: list[NormalizedInboundEmail] = []
        self.assistants_by_id: dict[str, dict[str, str]] = {}

    async def accept_inbound(self, email):
        self.accepted.append(email)


async def _seed(session: AsyncSession) -> None:
    session.add(Owner(id="o-1", name="L"))
    session.add(EndUser(id="u-1", owner_id="o-1", email="m@example.com"))
    session.add(
        Budget(
            id="b-1",
            assistant_id="a-1",
            monthly_limit_usd=1,
            period_starts_at=datetime(2026, 1, 1, tzinfo=UTC),
            period_resets_at=datetime(2026, 2, 1, tzinfo=UTC),
        )
    )
    session.add(
        Assistant(
            id="a-1",
            end_user_id="u-1",
            inbound_address="assistant-a1@assist.example.com",
            allowed_senders=["assistant-a1@assist.example.com"],
            model="m",
            system_prompt="x",
        )
    )
    await session.commit()


async def test_tick_dispatches_due_once_task_and_marks_it_completed(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as s:
        await _seed(s)
    now = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
    service = ScheduledTaskService(sqlite_session_factory, clock=lambda: now)
    runtime = _FakeRuntime(service)

    task = await service.create_once(
        assistant_id="a-1",
        run_at=now - timedelta(minutes=1),
        subject="reminder: groceries",
        body="don't forget",
    )

    await tick_scheduled_tasks_impl(
        runtime=runtime,
        service=service,
        session_factory=sqlite_session_factory,
        now=now,
    )

    assert len(runtime.accepted) == 1
    email = runtime.accepted[0]
    assert isinstance(email, NormalizedInboundEmail)
    # New-thread headers: no in_reply_to, no references.
    assert email.in_reply_to_header is None
    assert email.references_headers == []
    # Synthetic from = assistant's inbound_address so the router accepts it.
    assert email.from_email == "assistant-a1@assist.example.com"
    assert email.to_emails == ["assistant-a1@assist.example.com"]
    assert email.subject == "reminder: groceries"
    assert email.body_text == "don't forget"
    # Fresh ids: not the task id, not empty.
    assert email.provider_message_id
    assert email.message_id_header
    assert email.received_at == now

    after = (await service.list_for_assistant("a-1"))[0]
    assert after.id == task.id
    assert after.status == ScheduledTaskStatus.COMPLETED
    assert after.last_run_at == now


async def test_tick_reschedules_cron_task_for_next_iteration(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as s:
        await _seed(s)
    now = datetime(2026, 5, 11, 13, 0, tzinfo=UTC)
    service = ScheduledTaskService(sqlite_session_factory, clock=lambda: now - timedelta(hours=1))
    runtime = _FakeRuntime(service)
    task = await service.create_cron(
        assistant_id="a-1",
        cron_expr="0 * * * *",
        subject="hourly",
        body="tick",
    )
    # Task created with next_run_at = 13:00 (next from clock at 12:00).
    assert task.next_run_at == datetime(2026, 5, 11, 13, 0, tzinfo=UTC)

    await tick_scheduled_tasks_impl(
        runtime=runtime,
        service=service,
        session_factory=sqlite_session_factory,
        now=now,
    )

    assert len(runtime.accepted) == 1
    after = (await service.list_for_assistant("a-1"))[0]
    assert after.kind == ScheduledTaskKind.CRON
    assert after.status == ScheduledTaskStatus.ACTIVE
    assert after.next_run_at == datetime(2026, 5, 11, 14, 0, tzinfo=UTC)
    assert after.last_run_at == now


async def test_tick_skips_future_and_paused_tasks(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as s:
        await _seed(s)
    now = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
    service = ScheduledTaskService(sqlite_session_factory, clock=lambda: now)
    runtime = _FakeRuntime(service)

    await service.create_once(
        assistant_id="a-1",
        run_at=now + timedelta(hours=1),
        subject="later",
        body="x",
    )

    await tick_scheduled_tasks_impl(
        runtime=runtime,
        service=service,
        session_factory=sqlite_session_factory,
        now=now,
    )

    assert runtime.accepted == []


async def test_tick_recovers_when_accept_inbound_fails_leaves_task_active(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as s:
        await _seed(s)
    now = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
    service = ScheduledTaskService(sqlite_session_factory, clock=lambda: now)

    class _BoomRuntime(_FakeRuntime):
        async def accept_inbound(self, email):
            raise RuntimeError("boom")

    runtime = _BoomRuntime(service)

    task = await service.create_once(
        assistant_id="a-1",
        run_at=now - timedelta(minutes=1),
        subject="x",
        body="y",
    )

    await tick_scheduled_tasks_impl(
        runtime=runtime,
        service=service,
        session_factory=sqlite_session_factory,
        now=now,
    )

    after = (await service.list_for_assistant("a-1"))[0]
    assert after.id == task.id
    assert after.status == ScheduledTaskStatus.ACTIVE
    assert after.last_run_at is None
