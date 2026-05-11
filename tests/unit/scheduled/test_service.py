from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import (
    Assistant,
    Budget,
    EndUser,
    Owner,
)
from email_agent.models.scheduled import ScheduledTaskKind, ScheduledTaskStatus
from email_agent.scheduled.service import (
    InvalidScheduledTaskError,
    ScheduledTaskService,
)


async def _seed_assistant(session: AsyncSession, assistant_id: str = "a-1") -> None:
    session.add(Owner(id="o-1", name="Larry"))
    session.add(EndUser(id="u-1", owner_id="o-1", email="mum@example.com"))
    session.add(
        Budget(
            id="b-1",
            assistant_id=assistant_id,
            monthly_limit_usd=1,
            period_starts_at=datetime(2026, 1, 1, tzinfo=UTC),
            period_resets_at=datetime(2026, 2, 1, tzinfo=UTC),
        )
    )
    session.add(
        Assistant(
            id=assistant_id,
            end_user_id="u-1",
            inbound_address=f"{assistant_id}@assist.example.com",
            allowed_senders=[],
            model="m",
            system_prompt="be kind",
        )
    )
    await session.commit()


def _clock_at(moment: datetime):
    def _now() -> datetime:
        return moment

    return _now


async def test_create_once_persists_row_and_sets_next_run_to_run_at(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as s:
        await _seed_assistant(s)
    now = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
    fire_at = now + timedelta(hours=2)
    service = ScheduledTaskService(sqlite_session_factory, clock=_clock_at(now))

    task = await service.create_once(
        assistant_id="a-1", run_at=fire_at, subject="ping", body="poke"
    )

    assert task.kind == ScheduledTaskKind.ONCE
    assert task.next_run_at == fire_at
    assert task.status == ScheduledTaskStatus.ACTIVE
    assert task.subject == "ping"

    listed = await service.list_for_assistant("a-1")
    assert [t.id for t in listed] == [task.id]


async def test_create_cron_sets_next_run_from_cron_expr(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as s:
        await _seed_assistant(s)
    now = datetime(2026, 5, 11, 12, 30, tzinfo=UTC)
    service = ScheduledTaskService(sqlite_session_factory, clock=_clock_at(now))

    task = await service.create_cron(
        assistant_id="a-1", cron_expr="0 * * * *", subject="hourly", body="tick"
    )

    # Next top-of-the-hour after 12:30 is 13:00.
    assert task.next_run_at == datetime(2026, 5, 11, 13, 0, tzinfo=UTC)
    assert task.cron_expr == "0 * * * *"


async def test_create_cron_rejects_invalid_expression(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as s:
        await _seed_assistant(s)
    service = ScheduledTaskService(
        sqlite_session_factory, clock=_clock_at(datetime(2026, 5, 11, tzinfo=UTC))
    )

    with pytest.raises(InvalidScheduledTaskError):
        await service.create_cron(assistant_id="a-1", cron_expr="not a cron", subject="x", body="y")


async def test_list_for_assistant_only_returns_that_assistants_tasks(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as s:
        await _seed_assistant(s, "a-1")
        await _seed_assistant_extra(s, "a-2")
    now = datetime(2026, 5, 11, tzinfo=UTC)
    service = ScheduledTaskService(sqlite_session_factory, clock=_clock_at(now))

    await service.create_once(
        assistant_id="a-1", run_at=now + timedelta(minutes=5), subject="a", body="a"
    )
    await service.create_once(
        assistant_id="a-2", run_at=now + timedelta(minutes=10), subject="b", body="b"
    )

    listed = await service.list_for_assistant("a-1")
    assert {t.assistant_id for t in listed} == {"a-1"}


async def _seed_assistant_extra(session: AsyncSession, assistant_id: str) -> None:
    session.add(
        Budget(
            id=f"b-{assistant_id}",
            assistant_id=assistant_id,
            monthly_limit_usd=1,
            period_starts_at=datetime(2026, 1, 1, tzinfo=UTC),
            period_resets_at=datetime(2026, 2, 1, tzinfo=UTC),
        )
    )
    session.add(
        Assistant(
            id=assistant_id,
            end_user_id="u-1",
            inbound_address=f"{assistant_id}@assist.example.com",
            allowed_senders=[],
            model="m",
            system_prompt="x",
        )
    )
    await session.commit()


async def test_delete_removes_only_matching_task_when_scoped(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as s:
        await _seed_assistant(s)
    now = datetime(2026, 5, 11, tzinfo=UTC)
    service = ScheduledTaskService(sqlite_session_factory, clock=_clock_at(now))

    task = await service.create_once(
        assistant_id="a-1", run_at=now + timedelta(minutes=5), subject="a", body="a"
    )

    deleted = await service.delete(assistant_id="a-1", task_id=task.id)
    assert deleted is True
    assert await service.list_for_assistant("a-1") == []


async def test_delete_refuses_other_assistants_task(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as s:
        await _seed_assistant(s, "a-1")
        await _seed_assistant_extra(s, "a-2")
    now = datetime(2026, 5, 11, tzinfo=UTC)
    service = ScheduledTaskService(sqlite_session_factory, clock=_clock_at(now))

    task = await service.create_once(
        assistant_id="a-1", run_at=now + timedelta(minutes=5), subject="a", body="a"
    )

    deleted = await service.delete(assistant_id="a-2", task_id=task.id)
    assert deleted is False
    assert [t.id for t in await service.list_for_assistant("a-1")] == [task.id]


async def test_compute_next_run_for_cron_at_boundary_advances(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = ScheduledTaskService(
        sqlite_session_factory, clock=_clock_at(datetime(2026, 5, 11, tzinfo=UTC))
    )
    base = datetime(2026, 5, 11, 13, 0, tzinfo=UTC)
    # Hourly cron at the top of the hour: next from exactly 13:00 should be 14:00.
    nxt = service.compute_next_run("0 * * * *", base)
    assert nxt == datetime(2026, 5, 11, 14, 0, tzinfo=UTC)


async def test_claim_due_returns_active_tasks_with_next_run_in_past(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as s:
        await _seed_assistant(s)
    now = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
    service = ScheduledTaskService(sqlite_session_factory, clock=_clock_at(now))

    past = await service.create_once(
        assistant_id="a-1", run_at=now - timedelta(minutes=1), subject="past", body="x"
    )
    await service.create_once(
        assistant_id="a-1", run_at=now + timedelta(hours=1), subject="future", body="x"
    )

    due = await service.claim_due(as_of=now)
    assert [t.id for t in due] == [past.id]


async def test_mark_fired_once_marks_completed(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as s:
        await _seed_assistant(s)
    now = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
    service = ScheduledTaskService(sqlite_session_factory, clock=_clock_at(now))

    task = await service.create_once(
        assistant_id="a-1", run_at=now - timedelta(minutes=1), subject="past", body="x"
    )

    fired_at = now
    await service.mark_fired(task_id=task.id, fired_at=fired_at)

    after = (await service.list_for_assistant("a-1"))[0]
    assert after.status == ScheduledTaskStatus.COMPLETED
    assert after.last_run_at == fired_at


async def test_mark_fired_cron_reschedules_next_run(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_session_factory() as s:
        await _seed_assistant(s)
    now = datetime(2026, 5, 11, 13, 0, tzinfo=UTC)
    service = ScheduledTaskService(sqlite_session_factory, clock=_clock_at(now))

    task = await service.create_cron(
        assistant_id="a-1", cron_expr="0 * * * *", subject="hourly", body="x"
    )

    await service.mark_fired(task_id=task.id, fired_at=now)
    after = (await service.list_for_assistant("a-1"))[0]
    assert after.status == ScheduledTaskStatus.ACTIVE
    assert after.last_run_at == now
    assert after.next_run_at == datetime(2026, 5, 11, 14, 0, tzinfo=UTC)
