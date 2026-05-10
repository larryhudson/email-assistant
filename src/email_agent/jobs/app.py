"""Procrastinate app + task registrations.

Owns the `procrastinate.App` instance the worker subscribes to. Tasks
are registered here so a single `procrastinate worker -a
email_agent.jobs.app:app` picks them all up.

Production deps (runtime, memory, session_factory) are resolved lazily
on first task invocation via `build_worker_deps`. Unit tests bypass
this entirely by calling `*_impl` coroutines directly with stub deps —
see `tests/unit/jobs/`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from procrastinate import App, PsycopgConnector

from email_agent.config import Settings
from email_agent.jobs.curate_memory import curate_memory_impl
from email_agent.jobs.run_agent import run_agent_impl

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from email_agent.memory.port import MemoryPort
    from email_agent.runtime.assistant_runtime import AssistantRuntime


def make_procrastinate_app(database_url: str) -> App:
    """Return a Procrastinate app wired to `database_url`. Tasks attach
    via the module-level `app` (see below); this factory exists so tests
    and alternate configs can construct their own."""
    return App(connector=PsycopgConnector(conninfo=database_url))


def _settings() -> Settings:
    return Settings()  # ty: ignore[missing-argument]


app = make_procrastinate_app(str(_settings().database_url))


@lru_cache(maxsize=1)
def build_worker_deps() -> _WorkerDeps:
    """Lazily construct runtime + memory + session_factory for tasks.

    Cached for the worker process lifetime — tasks reuse the same
    runtime/sandbox/cognee config across invocations. Workers should
    call this once on startup; ad-hoc invocations call it on first use.
    """
    from email_agent.composition import make_cognee_memory, make_runtime_from_settings
    from email_agent.db.session import make_engine, make_session_factory
    from email_agent.mail.mailgun import MailgunEmailProvider

    settings = _settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    # Build the memory adapter once and share it: the runtime uses it for
    # recall + memory_search inside execute_run; curate_memory uses it for
    # post-run record_turn writes. Both must hit the same per-assistant
    # cognee root + share the global asyncio.Lock.
    memory = make_cognee_memory(settings)

    email_provider = MailgunEmailProvider(
        signing_key=settings.mailgun_signing_key.get_secret_value(),
        api_key=settings.mailgun_api_key.get_secret_value(),
        domain=settings.mailgun_domain,
    )
    runtime = make_runtime_from_settings(
        settings,
        session_factory,
        email_provider=email_provider,
        memory=memory,
        run_timeout_seconds=settings.sandbox_run_timeout_seconds,
    )
    return _WorkerDeps(
        runtime=runtime,
        memory=memory,
        session_factory=session_factory,
    )


class _WorkerDeps:
    """Bundle of resolved production dependencies for task bodies."""

    __slots__ = ("memory", "runtime", "session_factory")

    def __init__(
        self,
        *,
        runtime: AssistantRuntime,
        memory: MemoryPort,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.runtime = runtime
        self.memory = memory
        self.session_factory = session_factory


@app.task(name="run_agent")
async def run_agent(run_id: str) -> str:
    deps = build_worker_deps()
    outcome = await run_agent_impl(run_id=run_id, runtime=deps.runtime)
    return outcome.__class__.__name__


@app.task(name="curate_memory")
async def curate_memory(*, assistant_id: str, thread_id: str, run_id: str) -> None:
    deps = build_worker_deps()
    await curate_memory_impl(
        assistant_id=assistant_id,
        thread_id=thread_id,
        run_id=run_id,
        session_factory=deps.session_factory,
        memory=deps.memory,
    )


async def defer_run_agent(*, run_id: str, assistant_id: str) -> None:
    """Production `run_agent_defer` callback for `AssistantRuntime`.

    Sets `queueing_lock=f"assistant-{assistant_id}"` so concurrent inbounds
    for the same assistant serialize while different assistants run in
    parallel.
    """
    await run_agent.configure(queueing_lock=f"assistant-{assistant_id}").defer_async(run_id=run_id)


async def defer_curate_memory(*, assistant_id: str, thread_id: str, run_id: str) -> None:
    """Production `curate_memory_defer` callback for `RunRecorder`.

    No queueing_lock — curate jobs are independent of run ordering and
    cognee adapter holds its own process-wide lock.
    """
    await curate_memory.defer_async(assistant_id=assistant_id, thread_id=thread_id, run_id=run_id)


__all__ = [
    "app",
    "build_worker_deps",
    "curate_memory",
    "defer_curate_memory",
    "defer_run_agent",
    "make_procrastinate_app",
    "run_agent",
]
