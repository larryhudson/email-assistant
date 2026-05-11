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

from email_agent.db.models import AgentRun, Assistant, EndUser, ScheduledTaskRow
from email_agent.models.email import NormalizedInboundEmail
from email_agent.models.scheduled import ScheduledTaskKind, ScheduledTaskStatus
from email_agent.runtime.assistant_runtime import Accepted, Dropped

if TYPE_CHECKING:
    from email_agent.runtime.assistant_runtime import AcceptOutcome
    from email_agent.scheduled.service import ScheduledTaskService

logger = logging.getLogger(__name__)


class _RuntimeLike(Protocol):
    async def accept_inbound(self, email: NormalizedInboundEmail) -> AcceptOutcome: ...


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

        # Updates to scheduled_tasks (mark_fired) and agent_runs (tag) all run
        # against the OUTER session below — opening a separate session for them
        # would deadlock against the SELECT FOR UPDATE we just took.
        for row in rows:
            assistant = await session.get(Assistant, row.assistant_id)
            if assistant is None:
                logger.warning("scheduled_task %s references missing assistant", row.id)
                continue
            end_user = await session.get(EndUser, assistant.end_user_id)
            if end_user is None:
                logger.warning("scheduled_task %s references missing end_user", row.id)
                continue

            email = _build_synthetic_inbound(
                from_email=end_user.email,
                to_email=assistant.inbound_address,
                row=row,
                now=now,
            )
            try:
                outcome = await runtime.accept_inbound(email)
            except Exception:
                logger.exception(
                    "scheduled_task %s failed to dispatch; leaving active for retry",
                    row.id,
                )
                continue

            if isinstance(outcome, Dropped):
                logger.warning(
                    "scheduled_task %s dropped by router: %s (%s); leaving active",
                    row.id,
                    outcome.reason,
                    outcome.detail,
                )
                continue

            assert isinstance(outcome, Accepted)
            run = (
                await session.execute(
                    select(AgentRun).where(AgentRun.inbound_message_id == outcome.message_id)
                )
            ).scalar_one_or_none()
            if run is None:
                logger.warning(
                    "scheduled_task %s: no AgentRun for inbound_message_id=%s",
                    row.id,
                    outcome.message_id,
                )
            else:
                run.triggered_by_scheduled_task_id = row.id

            row.last_run_at = now
            if row.kind == ScheduledTaskKind.ONCE.value:
                row.status = ScheduledTaskStatus.COMPLETED.value
            else:
                assert row.cron_expr is not None
                row.next_run_at = service.compute_next_run(row.cron_expr, now)

        await session.commit()


def _build_synthetic_inbound(
    *,
    from_email: str,
    to_email: str,
    row: ScheduledTaskRow,
    now: datetime,
) -> NormalizedInboundEmail:
    marker = f"[Triggered by scheduled task {row.name!r} ({row.id}) at {now.isoformat()}]\n\n"
    return NormalizedInboundEmail(
        provider_message_id=f"sched-{uuid.uuid4().hex[:12]}",
        message_id_header=f"<sched-{uuid.uuid4().hex[:12]}@email-agent>",
        in_reply_to_header=None,
        references_headers=[],
        from_email=from_email,
        to_emails=[to_email],
        subject=row.name,
        body_text=marker + row.body,
        received_at=now,
    )


__all__ = ["tick_scheduled_tasks_impl"]
