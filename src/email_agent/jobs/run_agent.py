"""run_agent background task — picks up a queued AgentRun and executes it.

Body is a thin delegation to `runtime.execute_run(run_id)`. The task is
queued by `AssistantRuntime.accept_inbound` (after the queued AgentRun
row commits) and dispatched by a Procrastinate worker.

Per-assistant serialization is enforced via `lock` set at deferral time
(`task.configure(lock=f"assistant-{assistant_id}")`) — keeping multiple
inbounds for the same assistant running sequentially while still allowing
more than one run to be queued.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from email_agent.runtime.assistant_runtime import AssistantRuntime, RunOutcome


async def run_agent_impl(*, run_id: str, runtime: AssistantRuntime) -> RunOutcome:
    return await runtime.execute_run(run_id)


__all__ = ["run_agent_impl"]
