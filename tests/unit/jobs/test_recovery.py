from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from email_agent.db.models import AgentRun
from email_agent.jobs.recovery import cancel_interrupted_jobs


@pytest.mark.asyncio
async def test_cancel_interrupted_jobs_cancels_job_and_marks_run_failed(
    sqlite_session_factory,
):
    async with sqlite_session_factory() as session:
        await _create_procrastinate_tables(session)
        session.add(
            AgentRun(
                id="r-1",
                assistant_id="a-1",
                thread_id="t-1",
                inbound_message_id="m-1",
                reply_message_id=None,
                status="running",
                error=None,
                completed_at=None,
            )
        )
        await session.execute(
            text(
                "INSERT INTO procrastinate_jobs (id, task_name, args, status, worker_id) "
                "VALUES (1, 'run_agent', :args, 'doing', NULL)"
            ),
            {"args": '{"run_id": "r-1"}'},
        )
        await session.commit()

    result = await cancel_interrupted_jobs(sqlite_session_factory, reason="stopped")

    async with sqlite_session_factory() as session:
        run = await session.get(AgentRun, "r-1")
        assert run is not None
        job = (
            await session.execute(text("SELECT status FROM procrastinate_jobs WHERE id = 1"))
        ).one()
        events = (
            await session.execute(text("SELECT type FROM procrastinate_events WHERE job_id = 1"))
        ).all()

    assert result.job_ids == (1,)
    assert result.run_ids == ("r-1",)
    assert run.status == "failed"
    assert run.error == "stopped"
    assert run.completed_at is not None
    assert job.status == "cancelled"
    assert [event.type for event in events] == ["cancelled"]


@pytest.mark.asyncio
async def test_cancel_interrupted_jobs_does_not_reopen_completed_run(
    sqlite_session_factory,
):
    completed_at = datetime(2026, 5, 26, tzinfo=UTC)
    async with sqlite_session_factory() as session:
        await _create_procrastinate_tables(session)
        session.add(
            AgentRun(
                id="r-1",
                assistant_id="a-1",
                thread_id="t-1",
                inbound_message_id="m-1",
                reply_message_id="m-out",
                status="completed",
                error=None,
                completed_at=completed_at,
            )
        )
        await session.execute(
            text(
                "INSERT INTO procrastinate_jobs (id, task_name, args, status, worker_id) "
                "VALUES (1, 'run_agent', :args, 'doing', NULL)"
            ),
            {"args": '{"run_id": "r-1"}'},
        )
        await session.commit()

    result = await cancel_interrupted_jobs(sqlite_session_factory, reason="stopped")

    async with sqlite_session_factory() as session:
        run = await session.get(AgentRun, "r-1")

    assert result.job_ids == (1,)
    assert result.run_ids == ("r-1",)
    assert run is not None
    assert run.status == "completed"
    assert run.error is None
    assert run.completed_at == completed_at.replace(tzinfo=None)


async def _create_procrastinate_tables(session) -> None:
    await session.execute(
        text(
            "CREATE TABLE procrastinate_jobs ("
            "id INTEGER PRIMARY KEY, "
            "task_name TEXT NOT NULL, "
            "args TEXT NOT NULL, "
            "status TEXT NOT NULL, "
            "worker_id INTEGER NULL"
            ")"
        )
    )
    await session.execute(
        text(
            "CREATE TABLE procrastinate_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "job_id INTEGER NOT NULL, "
            "type TEXT NOT NULL, "
            "at DATETIME NULL"
            ")"
        )
    )
