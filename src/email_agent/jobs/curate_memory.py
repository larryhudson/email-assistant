"""curate_memory background task — persists a run's user + assistant
turns into the assistant's cognee memory after `record_completion`.

The task body is split:
- `curate_memory_impl(...)` — the testable coroutine that takes explicit
  deps. Unit-tested against SQLite + InMemoryMemoryAdapter.
- `curate_memory(...)` (registered with the Procrastinate app in
  `jobs/app.py`) — the queue-facing wrapper that resolves production
  deps and calls the impl.

Curation surface (V1): the inbound user message and the outbound reply
body, recorded under the thread's session id via `MemoryPort.record_turn`.
Cognee handles session memory + auto-bridging into the durable graph.
Tool-call traces are deliberately out of scope for V1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from email_agent.db.models import AgentRun, EmailMessage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from email_agent.memory.port import MemoryPort


async def curate_memory_impl(
    *,
    assistant_id: str,
    thread_id: str,
    run_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    memory: MemoryPort,
) -> None:
    """Load a run's inbound + outbound message bodies and write them to
    memory under the thread's session id."""
    async with session_factory() as session:
        run = await session.get(AgentRun, run_id)
        if run is None:
            return

        inbound = await session.get(EmailMessage, run.inbound_message_id)
        outbound = (
            await session.get(EmailMessage, run.reply_message_id) if run.reply_message_id else None
        )
        # Pull the columns we care about while still attached to the session.
        inbound_text = inbound.body_text if inbound is not None else None
        outbound_text = outbound.body_text if outbound is not None else None

    if inbound_text:
        await memory.record_turn(
            assistant_id=assistant_id,
            thread_id=thread_id,
            role="user",
            content=inbound_text,
        )

    if outbound_text:
        await memory.record_turn(
            assistant_id=assistant_id,
            thread_id=thread_id,
            role="assistant",
            content=outbound_text,
        )


__all__ = ["curate_memory_impl"]
