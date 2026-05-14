"""Production wiring for the agent runtime.

Centralises the "what does prod look like?" decisions in one place so the
CLI, web app, and worker all build the same `AssistantRuntime`. Tests
construct their own runtimes with InMemory adapters; this module is for
the live system.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.agent.assistant_agent import AssistantAgent
from email_agent.config import Settings
from email_agent.domain.workspace_projector import EmailWorkspaceProjector
from email_agent.memory.inmemory import InMemoryMemoryAdapter
from email_agent.models.assistant import AssistantScope
from email_agent.runtime.assistant_runtime import AssistantRuntime
from email_agent.sandbox.workspace_provider import InMemoryWorkspaceProvider, WorkspaceProvider

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from email_agent.github.port import GitHubPort
    from email_agent.mail.port import EmailProvider
    from email_agent.memory.port import MemoryPort
    from email_agent.pdf.port import PdfRenderPort
    from email_agent.search.port import SearchPort


def make_fireworks_model_factory(
    settings: Settings,
) -> "Callable[[AssistantScope], Model]":
    """Return a factory that builds a Fireworks model from `scope.model_name`.

    `scope.model_name` is the full Fireworks model id (e.g.
    `accounts/fireworks/models/minimax-m2p7`) — short aliases were dropped
    because they fragmented across the model factory, the pricing table,
    and the usage_ledger writer. Storing the full id on the assistant row
    keeps everything consistent.
    """
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.fireworks import FireworksProvider

    api_key = settings.fireworks_api_key.get_secret_value()

    def factory(scope: AssistantScope) -> "Model":
        provider = FireworksProvider(api_key=api_key)
        return OpenAIChatModel(scope.model_name, provider=provider)

    return factory


def make_cognee_memory(settings: Settings) -> "MemoryPort":
    """Build a single shared `CogneeMemoryAdapter`.

    Cognee 1.0 is a single-instance library: one set of relational/vector/
    graph stores per process, with multi-tenancy modeled via cognee `User`
    objects. We point the install at `settings.cognee_data_root` once,
    set the LLM + embedding config, and let the adapter authenticate
    each call as the right user (per-assistant) via `user=...`.
    """
    import os

    import cognee

    from email_agent.memory.cognee import CogneeMemoryAdapter

    if settings.cognee_skip_connection_test:
        os.environ["COGNEE_SKIP_CONNECTION_TEST"] = "true"

    cognee.config.set_llm_provider(settings.cognee_llm_provider)
    cognee.config.set_llm_model(settings.cognee_llm_model)
    cognee.config.set_llm_api_key(settings.cognee_llm_api_key.get_secret_value())
    if settings.cognee_llm_endpoint is not None:
        cognee.config.set_llm_endpoint(settings.cognee_llm_endpoint)

    cognee.config.set_embedding_provider(settings.cognee_embedding_provider)
    cognee.config.set_embedding_model(settings.cognee_embedding_model)
    cognee.config.set_embedding_api_key(settings.cognee_embedding_api_key.get_secret_value())
    if settings.cognee_embedding_endpoint is not None:
        cognee.config.set_embedding_endpoint(settings.cognee_embedding_endpoint)
    cognee.config.set_embedding_dimensions(settings.cognee_embedding_dimensions)

    # Cognee builds file:// URIs from data_root_directory and breaks on
    # relative paths; resolve to absolute up front. system_root holds
    # cognee's own SQLite/graph DBs; data_root holds ingested files.
    data_root = settings.cognee_data_root.resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    cognee.config.data_root_directory(str(data_root / "data"))
    cognee.config.system_root_directory(str(data_root / "system"))

    return CogneeMemoryAdapter()


def make_brave_search(settings: Settings) -> "SearchPort | None":
    if not settings.web_search_enabled or settings.brave_search_api_key is None:
        return None

    from email_agent.search.brave import BraveSearchAdapter

    return BraveSearchAdapter(
        api_key=settings.brave_search_api_key.get_secret_value(),
        timeout_s=settings.brave_search_timeout_seconds,
    )


def make_pdf_renderer(settings: Settings) -> "PdfRenderPort | None":
    if not settings.pdf_tools_enabled:
        return None

    from email_agent.pdf.prince import PrincePdfRenderer

    return PrincePdfRenderer(
        prince_path=settings.prince_path,
        timeout_seconds=settings.prince_timeout_seconds,
        preview_max_dpi=settings.pdf_preview_max_dpi,
        preview_max_bytes=settings.pdf_preview_max_bytes,
    )


def make_github(settings: Settings) -> "GitHubPort | None":
    if not settings.github_enabled or not settings.github_username:
        return None

    from email_agent.github.http import GitHubHttpAdapter

    return GitHubHttpAdapter(
        username=settings.github_username,
        token=settings.github_token,
        timeout_s=settings.github_timeout_seconds,
    )


def make_runtime_from_settings(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    email_provider: "EmailProvider",
    workspace_provider: WorkspaceProvider | None = None,
    memory: "MemoryPort | None" = None,
    search: "SearchPort | None" = None,
    pdf_renderer: "PdfRenderPort | None" = None,
    github: "GitHubPort | None" = None,
    use_real_model: bool = True,
    use_real_memory: bool = True,
    use_docker_sandbox: bool = True,
    use_procrastinate: bool = True,
    run_timeout_seconds: float | None = None,
) -> AssistantRuntime:
    """Compose a fully-wired AssistantRuntime for production-ish use.

    `workspace_provider` defaults to `settings.sandbox_provider`. Docker is
    still the default; set `SANDBOX_PROVIDER=bashkit` to try Bashkit, or pass
    `use_docker_sandbox=False` to force the legacy in-memory test path.
    `memory` defaults to a `CogneeMemoryAdapter` (`use_real_memory=True`),
    falling back to `InMemoryMemoryAdapter` when the toggle is off — useful
    for offline iteration without an embedding API key. `use_real_model=False`
    skips wiring Fireworks so callers can rely on a test override;
    `use_real_model=True` (default) plumbs through
    `make_fireworks_model_factory(settings)`.
    """
    if workspace_provider is None:
        if not use_docker_sandbox:
            workspace_provider = InMemoryWorkspaceProvider()
        elif settings.sandbox_provider == "docker":
            import docker as docker_sdk
            from email_agent.sandbox.docker_environment import DockerWorkspaceProvider

            workspace_provider = DockerWorkspaceProvider(
                client=docker_sdk.from_env(),
                image=settings.sandbox_image,
                sandbox_data_root=settings.sandbox_data_root,
                memory_mb=settings.sandbox_memory_mb,
                cpu_cores=settings.sandbox_cpu_cores,
                bash_timeout_seconds=settings.sandbox_bash_timeout_seconds,
            )
        elif settings.sandbox_provider == "bashkit":
            from email_agent.sandbox.bashkit_environment import (
                BashkitSnapshotStore,
                BashkitWorkspaceProvider,
            )

            workspace_provider = BashkitWorkspaceProvider(
                bash_timeout_seconds=settings.sandbox_bash_timeout_seconds,
                python_enabled=settings.sandbox_bashkit_python_enabled,
                sqlite_enabled=settings.sandbox_bashkit_sqlite_enabled,
                snapshot_store=BashkitSnapshotStore(settings.sandbox_data_root / "bashkit"),
            )
        else:
            workspace_provider = InMemoryWorkspaceProvider()
    if memory is None and settings.memory_enabled:
        memory = make_cognee_memory(settings) if use_real_memory else InMemoryMemoryAdapter()
    # When memory_enabled is False and the caller didn't supply one, `memory`
    # stays None and the runtime/agent skip recall/curate entirely.
    if search is None:
        search = make_brave_search(settings)
    if pdf_renderer is None:
        pdf_renderer = make_pdf_renderer(settings)
    if github is None:
        github = make_github(settings)
    projector = EmailWorkspaceProjector(run_inputs_root=settings.run_inputs_root)

    settings.attachments_root.mkdir(parents=True, exist_ok=True)
    settings.run_inputs_root.mkdir(parents=True, exist_ok=True)

    model_factory = make_fireworks_model_factory(settings) if use_real_model else None

    # Procrastinate defers are wired in by default; tests / inject-email --follow
    # disable them (`use_procrastinate=False`) and call execute_run directly.
    run_agent_defer = None
    curate_memory_defer = None
    if use_procrastinate:
        from email_agent.jobs.app import defer_curate_memory, defer_run_agent

        run_agent_defer = defer_run_agent
        # Nothing to curate into when memory is disabled — skip scheduling
        # post-run curation entirely so the worker doesn't fire no-ops.
        curate_memory_defer = defer_curate_memory if memory is not None else None

    from email_agent.domain.run_recorder import RunRecorder

    recorder = RunRecorder(session_factory, curate_memory_defer=curate_memory_defer)

    return AssistantRuntime(
        session_factory,
        attachments_root=settings.attachments_root,
        email_provider=email_provider,
        workspace_provider=workspace_provider,
        memory=memory,
        agent=AssistantAgent(has_memory=memory is not None, has_web_search=search is not None),
        projector=projector,
        recorder=recorder,
        search=search,
        pdf_renderer=pdf_renderer,
        github=github,
        model_factory=model_factory,
        run_timeout_seconds=run_timeout_seconds,
        run_agent_defer=run_agent_defer,
        admin_base_url=settings.admin_base_url,
    )


@asynccontextmanager
async def inject_session(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    use_real_model: bool = True,
    use_real_memory: bool = True,
    use_docker_sandbox: bool = True,
    use_procrastinate: bool = False,
) -> "AsyncIterator[tuple[AssistantRuntime, EmailProvider]]":
    """Async context manager yielding `(runtime, email_provider)` for the
    `inject-email` entry point.

    Always uses InMemoryEmailProvider so a fixture-driven local run never
    accidentally sends real Mailgun mail. The caller can inspect
    `email_provider.sent` after the run to see what would have gone out.
    Procrastinate defaults to off so `inject-email --follow` calls
    execute_run directly. Pass `use_procrastinate=True` to enqueue a
    `run_agent` job and let a separate `email-agent worker` pick it up —
    the procrastinate App is opened for the duration of the `async with`
    so deferrals work without further plumbing in the caller.
    """
    from email_agent.mail.inmemory import InMemoryEmailProvider

    email_provider = InMemoryEmailProvider()
    runtime = make_runtime_from_settings(
        settings,
        session_factory,
        email_provider=email_provider,
        use_procrastinate=use_procrastinate,
        use_real_model=use_real_model,
        use_real_memory=use_real_memory,
        use_docker_sandbox=use_docker_sandbox,
        run_timeout_seconds=settings.sandbox_run_timeout_seconds,
    )

    if use_procrastinate:
        from email_agent.jobs.app import app as procrastinate_app

        async with procrastinate_app.open_async():
            yield runtime, email_provider
    else:
        yield runtime, email_provider


__all__ = [
    "inject_session",
    "make_brave_search",
    "make_cognee_memory",
    "make_fireworks_model_factory",
    "make_pdf_renderer",
    "make_runtime_from_settings",
]
