from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.agent.toolset import AgentToolset
from email_agent.db.models import Assistant, Budget, EndUser, Owner
from email_agent.memory.inmemory import InMemoryMemoryAdapter
from email_agent.models.scheduled import ScheduledTaskKind
from email_agent.sandbox.inmemory_environment import InMemoryEnvironment
from email_agent.sandbox.workspace import AssistantWorkspace
from email_agent.scheduled.service import ScheduledTaskService


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
            inbound_address="a@x.com",
            allowed_senders=[],
            model="m",
            system_prompt="x",
        )
    )
    session.add(
        Assistant(
            id="a-2",
            end_user_id="u-1",
            inbound_address="b@x.com",
            allowed_senders=[],
            model="m",
            system_prompt="x",
        )
    )
    session.add(
        Budget(
            id="b-2",
            assistant_id="a-2",
            monthly_limit_usd=1,
            period_starts_at=datetime(2026, 1, 1, tzinfo=UTC),
            period_resets_at=datetime(2026, 2, 1, tzinfo=UTC),
        )
    )
    await session.commit()


def _toolset(service: ScheduledTaskService) -> AgentToolset:
    env = InMemoryEnvironment()
    return AgentToolset(
        assistant_id="a-1",
        run_id="r-1",
        env=env,
        workspace=AssistantWorkspace(env),
        memory=InMemoryMemoryAdapter(),
        pending_attachments=[],
        scheduled_tasks=service,
    )


def _clock(moment: datetime):
    def _now() -> datetime:
        return moment

    return _now


async def test_create_scheduled_task_once_persists_under_assistant_scope(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as s:
        await _seed(s)
    now = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
    service = ScheduledTaskService(sqlite_session_factory, clock=_clock(now))
    toolset = _toolset(service)

    fire_at = now + timedelta(hours=1)
    result = await toolset.create_scheduled_task(
        kind="once", when=fire_at.isoformat(), subject="ping", body="ping"
    )

    assert "created" in result
    listed = await service.list_for_assistant("a-1")
    assert len(listed) == 1
    assert listed[0].kind == ScheduledTaskKind.ONCE
    assert listed[0].subject == "ping"


async def test_create_scheduled_task_cron_uses_cron_expression(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as s:
        await _seed(s)
    now = datetime(2026, 5, 11, 12, 30, tzinfo=UTC)
    service = ScheduledTaskService(sqlite_session_factory, clock=_clock(now))
    toolset = _toolset(service)

    result = await toolset.create_scheduled_task(
        kind="cron", when="0 * * * *", subject="hourly", body="tick"
    )

    assert "created" in result
    listed = await service.list_for_assistant("a-1")
    assert listed[0].cron_expr == "0 * * * *"
    assert listed[0].next_run_at == datetime(2026, 5, 11, 13, 0, tzinfo=UTC)


async def test_create_scheduled_task_rejects_invalid_kind(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as s:
        await _seed(s)
    now = datetime(2026, 5, 11, tzinfo=UTC)
    service = ScheduledTaskService(sqlite_session_factory, clock=_clock(now))
    toolset = _toolset(service)

    result = await toolset.create_scheduled_task(
        kind="weekly", when="0 * * * *", subject="x", body="x"
    )
    assert "ERROR" in result


async def test_create_scheduled_task_rejects_bad_once_datetime(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as s:
        await _seed(s)
    now = datetime(2026, 5, 11, tzinfo=UTC)
    service = ScheduledTaskService(sqlite_session_factory, clock=_clock(now))
    toolset = _toolset(service)

    result = await toolset.create_scheduled_task(
        kind="once", when="not a date", subject="x", body="x"
    )
    assert "ERROR" in result


async def test_list_scheduled_tasks_returns_only_this_assistant(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as s:
        await _seed(s)
    now = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
    service = ScheduledTaskService(sqlite_session_factory, clock=_clock(now))
    await service.create_once(
        assistant_id="a-1", run_at=now + timedelta(hours=1), subject="mine", body="x"
    )
    await service.create_once(
        assistant_id="a-2", run_at=now + timedelta(hours=1), subject="theirs", body="x"
    )

    listed = await _toolset(service).list_scheduled_tasks()

    assert {t.assistant_id for t in listed} == {"a-1"}
    assert [t.subject for t in listed] == ["mine"]


async def test_delete_scheduled_task_only_deletes_self_owned(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as s:
        await _seed(s)
    now = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
    service = ScheduledTaskService(sqlite_session_factory, clock=_clock(now))
    mine = await service.create_once(
        assistant_id="a-1", run_at=now + timedelta(hours=1), subject="mine", body="x"
    )
    theirs = await service.create_once(
        assistant_id="a-2", run_at=now + timedelta(hours=1), subject="theirs", body="x"
    )

    toolset = _toolset(service)

    assert "deleted" in await toolset.delete_scheduled_task(mine.id)
    refused = await toolset.delete_scheduled_task(theirs.id)
    assert "ERROR" in refused
    assert [t.id for t in await service.list_for_assistant("a-2")] == [theirs.id]


@pytest.mark.parametrize("missing_field", ["subject", "body"])
async def test_create_scheduled_task_validates_required_text(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    missing_field: str,
) -> None:
    async with sqlite_session_factory() as s:
        await _seed(s)
    now = datetime(2026, 5, 11, tzinfo=UTC)
    service = ScheduledTaskService(sqlite_session_factory, clock=_clock(now))
    toolset = _toolset(service)

    kwargs = {"subject": "s", "body": "b"}
    kwargs[missing_field] = ""
    result = await toolset.create_scheduled_task(
        kind="once", when=(now + timedelta(hours=1)).isoformat(), **kwargs
    )
    assert "ERROR" in result
