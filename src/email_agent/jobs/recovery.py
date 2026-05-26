"""Recovery helpers for Procrastinate jobs interrupted by local dev shutdown."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class InterruptedJobRecovery:
    """Summary of jobs/runs moved out of active states."""

    job_ids: tuple[int, ...]
    run_ids: tuple[str, ...]

    @property
    def job_count(self) -> int:
        return len(self.job_ids)

    @property
    def run_count(self) -> int:
        return len(self.run_ids)


async def cancel_interrupted_jobs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    reason: str = "Interrupted by worker shutdown.",
) -> InterruptedJobRecovery:
    """Cancel jobs left in Procrastinate's `doing` state.

    This is intentionally for local/dev recovery paths. Procrastinate can
    recover worker-owned stalled jobs when `worker_id` is populated, but our
    current worker rows can be left with `worker_id IS NULL` after a hard
    Ctrl+C. In that state the queue and `agent_runs` disagree forever unless
    we move both sides out of their active states.
    """

    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, task_name, args "
                    "FROM procrastinate_jobs "
                    "WHERE status = 'doing' "
                    "AND task_name IN ('run_agent', 'curate_memory')"
                )
            )
        ).mappings()
        jobs = list(rows)

        job_ids = tuple(int(row["id"]) for row in jobs)
        run_ids = tuple(
            run_id
            for row in jobs
            if row["task_name"] == "run_agent"
            for run_id in [_run_id_from_args(row["args"])]
            if run_id is not None
        )

        now = datetime.now(UTC)
        for run_id in run_ids:
            await session.execute(
                text(
                    "UPDATE agent_runs "
                    "SET status = 'failed', error = :reason, completed_at = :now "
                    "WHERE id = :run_id "
                    "AND status IN ('queued', 'running') "
                    "AND reply_message_id IS NULL "
                    "AND completed_at IS NULL"
                ),
                {"run_id": run_id, "reason": reason, "now": now},
            )

        for job_id in job_ids:
            await session.execute(
                text(
                    "UPDATE procrastinate_jobs "
                    "SET status = 'cancelled', worker_id = NULL "
                    "WHERE id = :job_id AND status = 'doing'"
                ),
                {"job_id": job_id},
            )
            await session.execute(
                text(
                    "INSERT INTO procrastinate_events (job_id, type, at) "
                    "VALUES (:job_id, 'cancelled', :now)"
                ),
                {"job_id": job_id, "now": now},
            )

        await session.commit()
        return InterruptedJobRecovery(job_ids=job_ids, run_ids=run_ids)


def _run_id_from_args(args: Any) -> str | None:
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            return None
    elif isinstance(args, dict):
        parsed = args
    else:
        return None

    run_id = parsed.get("run_id")
    return run_id if isinstance(run_id, str) else None


__all__ = ["InterruptedJobRecovery", "cancel_interrupted_jobs"]
