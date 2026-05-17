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

from email_agent.db.models import (
    AgentRun,
    Assistant,
    EndUser,
    ScheduledTaskFireRow,
    ScheduledTaskRow,
)
from email_agent.models.email import NormalizedInboundEmail
from email_agent.models.scheduled import ScheduledTaskKind, ScheduledTaskStatus
from email_agent.runtime.assistant_runtime import Accepted, Dropped
from email_agent.sandbox.environment import ShellResult

if TYPE_CHECKING:
    from email_agent.runtime.assistant_runtime import AcceptOutcome
    from email_agent.sandbox.workspace_provider import WorkspaceProvider
    from email_agent.scheduled.service import ScheduledTaskService

logger = logging.getLogger(__name__)


class _RuntimeLike(Protocol):
    async def accept_inbound(self, email: NormalizedInboundEmail) -> AcceptOutcome: ...


class ScheduledCommandRunner(Protocol):
    async def run(self, task: ScheduledTaskRow) -> ShellResult: ...


class ScheduledDirectSender(Protocol):
    async def send(
        self,
        *,
        assistant_id: str,
        to_email: str,
        subject: str,
        body_text: str,
    ) -> None: ...


class WorkspaceScheduledCommandRunner:
    def __init__(self, workspace_provider: WorkspaceProvider) -> None:
        self._workspace_provider = workspace_provider

    async def run(self, task: ScheduledTaskRow) -> ShellResult:
        if task.command is None:
            raise ValueError(f"scheduled_task {task.id} has no command")
        workspace = await self._workspace_provider.get_workspace(task.assistant_id)
        return await workspace.environment.exec(task.command)


async def tick_scheduled_tasks_impl(
    *,
    runtime: _RuntimeLike,
    service: ScheduledTaskService,
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime,
    command_runner: ScheduledCommandRunner | None = None,
    direct_sender: ScheduledDirectSender | None = None,
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

            if row.command is not None:
                if command_runner is None:
                    logger.error(
                        "scheduled_task %s has command configured but no command_runner",
                        row.id,
                    )
                    continue
                result = await command_runner.run(row)
                if result.exit_code == 1:
                    _record_fire(
                        session,
                        row=row,
                        now=now,
                        status="quiet_exited",
                        exit_code=result.exit_code,
                        stdout=result.stdout,
                        stderr=result.stderr,
                    )
                    _mark_fired(row, service=service, now=now)
                    continue
                if result.exit_code != 0:
                    _record_fire(
                        session,
                        row=row,
                        now=now,
                        status="command_failed",
                        exit_code=result.exit_code,
                        stdout=result.stdout,
                        stderr=result.stderr,
                    )
                    continue
                body_text = result.stdout
                if not body_text.strip():
                    _record_fire(
                        session,
                        row=row,
                        now=now,
                        status="command_failed",
                        exit_code=result.exit_code,
                        stdout=result.stdout,
                        stderr="command exited 0 with empty stdout",
                    )
                    logger.warning(
                        "scheduled_task %s command exited 0 with empty stdout; leaving active",
                        row.id,
                    )
                    continue
                if not row.is_agent_enabled:
                    if direct_sender is None:
                        logger.error(
                            "scheduled_task %s has direct email configured but no direct_sender",
                            row.id,
                        )
                        continue
                    try:
                        await direct_sender.send(
                            assistant_id=row.assistant_id,
                            to_email=end_user.email,
                            subject=row.name,
                            body_text=body_text,
                        )
                    except Exception:
                        logger.exception(
                            "scheduled_task %s failed to send direct email; leaving active",
                            row.id,
                        )
                        continue
                    _record_fire(
                        session,
                        row=row,
                        now=now,
                        status="sent_direct",
                        exit_code=result.exit_code,
                        stdout=result.stdout,
                        stderr=result.stderr,
                    )
                    _mark_fired(row, service=service, now=now)
                    await _record_visible_notification(
                        session,
                        row=row,
                        now=now,
                        direct_sender=direct_sender,
                        end_user_email=end_user.email,
                    )
                    continue
            else:
                body_text = row.body

            email = _build_synthetic_inbound(
                from_email=end_user.email,
                to_email=assistant.inbound_address,
                row=row,
                now=now,
                body_text=body_text,
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
            _record_fire(
                session,
                row=row,
                now=now,
                status="continued",
                exit_code=None,
                stdout=body_text if row.command is not None else None,
                stderr=None,
                agent_run_id=run.id if run is not None else None,
            )

            _mark_fired(row, service=service, now=now)

        await session.commit()


def _build_synthetic_inbound(
    *,
    from_email: str,
    to_email: str,
    row: ScheduledTaskRow,
    now: datetime,
    body_text: str,
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
        body_text=marker + body_text,
        received_at=now,
    )


def _mark_fired(
    row: ScheduledTaskRow,
    *,
    service: ScheduledTaskService,
    now: datetime,
) -> None:
    row.last_run_at = now
    if row.kind == ScheduledTaskKind.ONCE.value:
        row.status = ScheduledTaskStatus.COMPLETED.value
    else:
        assert row.cron_expr is not None
        row.next_run_at = service.compute_next_run(row.cron_expr, now)


def _record_fire(
    session: AsyncSession,
    *,
    row: ScheduledTaskRow,
    now: datetime,
    status: str,
    exit_code: int | None,
    stdout: str | None,
    stderr: str | None,
    agent_run_id: str | None = None,
) -> None:
    session.add(
        ScheduledTaskFireRow(
            id=f"stf-{uuid.uuid4().hex[:10]}",
            scheduled_task_id=row.id,
            fired_at=now,
            status=status,
            exit_code=exit_code,
            stdout=_truncate_audit_text(stdout),
            stderr=_truncate_audit_text(stderr),
            agent_run_id=agent_run_id,
        )
    )


def _truncate_audit_text(value: str | None, *, limit: int = 8000) -> str | None:
    if value is None or len(value) <= limit:
        return value
    return value[:limit]


async def _record_visible_notification(
    session: AsyncSession,
    *,
    row: ScheduledTaskRow,
    now: datetime,
    direct_sender: ScheduledDirectSender,
    end_user_email: str,
) -> None:
    row.consecutive_unanswered_runs += 1
    if row.kind != ScheduledTaskKind.CRON.value:
        return
    if row.max_unanswered_runs is None or row.max_unanswered_runs <= 0:
        return
    if row.consecutive_unanswered_runs < row.max_unanswered_runs:
        return

    row.status = ScheduledTaskStatus.PAUSED.value
    row.paused_reason = (
        f"Paused after {row.consecutive_unanswered_runs} scheduled notifications with no replies."
    )
    _record_fire(
        session,
        row=row,
        now=now,
        status="paused",
        exit_code=None,
        stdout=None,
        stderr=row.paused_reason,
    )
    try:
        await direct_sender.send(
            assistant_id=row.assistant_id,
            to_email=end_user_email,
            subject=f"Paused: {row.name}",
            body_text=(
                f"Paused recurring scheduled task '{row.name}' after "
                f"{row.consecutive_unanswered_runs} notifications with no replies."
            ),
        )
    except Exception:
        logger.exception("scheduled_task %s failed to send pause notification", row.id)


__all__ = ["tick_scheduled_tasks_impl"]
