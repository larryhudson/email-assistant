"""Periodic tick: turn due `scheduled_tasks` rows into synthetic inbounds.

`tick_scheduled_tasks_impl` is the pure-domain body invoked once per minute
by the procrastinate periodic task in `jobs/app.py`. It claims due rows with
a row-level lock so concurrent ticks can't double-fire the same task:

  1. `SELECT FOR UPDATE SKIP LOCKED` the active rows whose next_run_at <= now.
  2. For each: build a `NormalizedInboundEmail` with fresh headers
     (no in_reply_to, empty references — guaranteed new thread) and feed
     to `runtime.accept_inbound`.
  3. On success, `mark_fired` advances the row (cron → reschedules,
     once → completes).
  4. On failure, the transaction is rolled back and the row stays active
     — the next tick will retry.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import Assistant, ScheduledTaskRow
from email_agent.models.email import NormalizedInboundEmail
from email_agent.models.scheduled import ScheduledTaskStatus

if TYPE_CHECKING:
    from email_agent.scheduled.service import ScheduledTaskService

logger = logging.getLogger(__name__)


class _RuntimeLike(Protocol):
    async def accept_inbound(self, email: NormalizedInboundEmail): ...


async def tick_scheduled_tasks_impl(
    *,
    runtime: _RuntimeLike,
    service: ScheduledTaskService,
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime,
) -> None:
    """Drain due scheduled tasks once. Safe to call concurrently.

    `now` is injected so tests pin time without mocking the clock; the
    production driver passes `datetime.now(UTC)`.
    """
    async with session_factory() as session:
        stmt = (
            select(ScheduledTaskRow)
            .where(
                ScheduledTaskRow.status == ScheduledTaskStatus.ACTIVE.value,
                ScheduledTaskRow.next_run_at <= now,
            )
            .order_by(ScheduledTaskRow.next_run_at, ScheduledTaskRow.id)
        )
        # Postgres path: SKIP LOCKED keeps two workers from grabbing the same
        # row. sqlite (used in unit tests) ignores the hint, which is fine
        # because the tests don't run concurrent ticks.
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        result = await session.execute(stmt)
        rows = result.scalars().all()

        for row in rows:
            assistant = await session.get(Assistant, row.assistant_id)
            if assistant is None:
                logger.warning("scheduled_task %s references missing assistant", row.id)
                continue

            email = _build_synthetic_inbound(assistant.inbound_address, row, now)
            try:
                await runtime.accept_inbound(email)
            except Exception:
                logger.exception(
                    "scheduled_task %s failed to dispatch; leaving active for retry",
                    row.id,
                )
                continue

            await service.mark_fired(task_id=row.id, fired_at=now)

        await session.commit()


def _build_synthetic_inbound(
    inbound_address: str, row: ScheduledTaskRow, now: datetime
) -> NormalizedInboundEmail:
    return NormalizedInboundEmail(
        provider_message_id=f"sched-{uuid.uuid4().hex[:12]}",
        message_id_header=f"<sched-{uuid.uuid4().hex[:12]}@email-agent>",
        in_reply_to_header=None,
        references_headers=[],
        from_email=inbound_address,
        to_emails=[inbound_address],
        subject=row.subject,
        body_text=row.body,
        received_at=now,
    )


__all__ = ["tick_scheduled_tasks_impl"]
