import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from croniter import CroniterBadCronError, croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import ScheduledTaskRow
from email_agent.models.scheduled import (
    ScheduledTask,
    ScheduledTaskKind,
    ScheduledTaskStatus,
)


class InvalidScheduledTaskError(ValueError):
    """Raised when a caller passes a bad cron expression or run_at value."""


def _default_clock() -> datetime:
    return datetime.now(UTC)


class ScheduledTaskService:
    """CRUD + scheduling logic for `scheduled_tasks`.

    Pure domain — depends only on the session factory and an injectable
    clock. Returns frozen pydantic `ScheduledTask` instances; ORM rows
    never leak past the boundary.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = _default_clock,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def create_once(
        self,
        *,
        assistant_id: str,
        run_at: datetime,
        subject: str,
        body: str,
        created_by_run_id: str | None = None,
    ) -> ScheduledTask:
        if run_at.tzinfo is None:
            raise InvalidScheduledTaskError("run_at must be timezone-aware")
        row = ScheduledTaskRow(
            id=f"st-{uuid.uuid4().hex[:10]}",
            assistant_id=assistant_id,
            kind=ScheduledTaskKind.ONCE.value,
            run_at=run_at,
            cron_expr=None,
            next_run_at=run_at,
            last_run_at=None,
            status=ScheduledTaskStatus.ACTIVE.value,
            subject=subject,
            body=body,
            created_by_run_id=created_by_run_id,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def create_cron(
        self,
        *,
        assistant_id: str,
        cron_expr: str,
        subject: str,
        body: str,
        created_by_run_id: str | None = None,
    ) -> ScheduledTask:
        now = self._clock()
        try:
            iterator = croniter(cron_expr, now)
        except (CroniterBadCronError, ValueError) as exc:
            raise InvalidScheduledTaskError(f"invalid cron expression: {cron_expr}") from exc
        next_run = iterator.get_next(datetime)
        if next_run.tzinfo is None:
            next_run = next_run.replace(tzinfo=UTC)

        row = ScheduledTaskRow(
            id=f"st-{uuid.uuid4().hex[:10]}",
            assistant_id=assistant_id,
            kind=ScheduledTaskKind.CRON.value,
            run_at=None,
            cron_expr=cron_expr,
            next_run_at=next_run,
            last_run_at=None,
            status=ScheduledTaskStatus.ACTIVE.value,
            subject=subject,
            body=body,
            created_by_run_id=created_by_run_id,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def list_for_assistant(self, assistant_id: str) -> list[ScheduledTask]:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ScheduledTaskRow)
                        .where(ScheduledTaskRow.assistant_id == assistant_id)
                        .order_by(ScheduledTaskRow.next_run_at, ScheduledTaskRow.id)
                    )
                )
                .scalars()
                .all()
            )
            return [_to_domain(r) for r in rows]

    async def delete(self, *, assistant_id: str, task_id: str) -> bool:
        async with self._session_factory() as session:
            row = await session.get(ScheduledTaskRow, task_id)
            if row is None or row.assistant_id != assistant_id:
                return False
            await session.delete(row)
            await session.commit()
            return True

    def compute_next_run(self, cron_expr: str, after: datetime) -> datetime:
        try:
            iterator = croniter(cron_expr, after)
        except (CroniterBadCronError, ValueError) as exc:
            raise InvalidScheduledTaskError(f"invalid cron expression: {cron_expr}") from exc
        nxt = iterator.get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=UTC)
        return nxt

    async def claim_due(self, *, as_of: datetime) -> list[ScheduledTask]:
        """Return active rows whose `next_run_at <= as_of`.

        Postgres-only callers should layer `with_for_update(skip_locked=True)`
        on top via the dedicated tick path (see jobs.app). This base method
        returns the candidate set; the production tick wraps mark_fired and
        the dispatch in a transaction that locks the rows.
        """
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ScheduledTaskRow)
                        .where(
                            ScheduledTaskRow.status == ScheduledTaskStatus.ACTIVE.value,
                            ScheduledTaskRow.next_run_at <= as_of,
                        )
                        .order_by(ScheduledTaskRow.next_run_at, ScheduledTaskRow.id)
                    )
                )
                .scalars()
                .all()
            )
            return [_to_domain(r) for r in rows]

    async def mark_fired(self, *, task_id: str, fired_at: datetime) -> ScheduledTask | None:
        """Advance the row after a successful fire.

        For `once` tasks: set status='completed'. For `cron` tasks: compute
        the next run from `fired_at` and stay active. Returns the updated
        domain task, or None if the row vanished.
        """
        async with self._session_factory() as session:
            row = await session.get(ScheduledTaskRow, task_id)
            if row is None:
                return None
            row.last_run_at = fired_at
            if row.kind == ScheduledTaskKind.ONCE.value:
                row.status = ScheduledTaskStatus.COMPLETED.value
            else:
                assert row.cron_expr is not None
                row.next_run_at = self.compute_next_run(row.cron_expr, fired_at)
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)


def _aware(dt: datetime | None) -> datetime | None:
    """Coerce naive timestamps from sqlite back to UTC. Postgres returns aware
    values already so this is a no-op there.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _to_domain(row: ScheduledTaskRow) -> ScheduledTask:
    next_run_at = _aware(row.next_run_at)
    created_at = _aware(row.created_at)
    updated_at = _aware(row.updated_at)
    assert next_run_at is not None
    assert created_at is not None
    assert updated_at is not None
    return ScheduledTask(
        id=row.id,
        assistant_id=row.assistant_id,
        kind=ScheduledTaskKind(row.kind),
        run_at=_aware(row.run_at),
        cron_expr=row.cron_expr,
        next_run_at=next_run_at,
        last_run_at=_aware(row.last_run_at),
        status=ScheduledTaskStatus(row.status),
        subject=row.subject,
        body=row.body,
        created_by_run_id=row.created_by_run_id,
        created_at=created_at,
        updated_at=updated_at,
    )


__all__ = ["InvalidScheduledTaskError", "ScheduledTaskService"]
