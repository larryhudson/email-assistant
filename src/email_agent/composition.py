"""Production wiring for the agent runtime.

Centralises the "what does prod look like?" decisions in one place so the
CLI, web app, and worker all build the same `AssistantRuntime`. Tests
construct their own runtimes with InMemory adapters; this module is for
the live system.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.agent.assistant_agent import AssistantAgent
from email_agent.config import Settings
from email_agent.domain.workspace_projector import EmailWorkspaceProjector
from email_agent.memory.inmemory import InMemoryMemoryAdapter
from email_agent.models.assistant import AssistantScope
from email_agent.runtime.assistant_runtime import AssistantRuntime
from email_agent.sandbox.inmemory import InMemorySandbox

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from email_agent.mail.port import EmailProvider
    from email_agent.memory.port import MemoryPort
    from email_agent.sandbox.port import AssistantSandbox


def make_fireworks_model_factory(
    settings: Settings,
) -> "Callable[[AssistantScope], Model]":
    """Return a factory that builds a Fireworks model.

    `scope.model_name` is treated as a short alias; the actual Fireworks
    model id comes from `settings.fireworks_model_id`. If the scope's name
    already looks like a fully-qualified Fireworks id (`accounts/...`), it
    overrides the setting — useful for per-assistant model overrides later.
    """
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.fireworks import FireworksProvider

    api_key = settings.fireworks_api_key.get_secret_value()
    default_model_id = settings.fireworks_model_id

    def factory(scope: AssistantScope) -> "Model":
        model_id = (
            scope.model_name if scope.model_name.startswith("accounts/") else default_model_id
        )
        provider = FireworksProvider(api_key=api_key)
        return OpenAIChatModel(model_id, provider=provider)

    return factory


def make_docker_sandbox(settings: Settings) -> "AssistantSandbox":
    """Build a DockerSandbox from Settings.

    Caller is responsible for ensuring the docker daemon is reachable and
    the base image (`settings.sandbox_image`) is built — see
    `docker/sandbox/Dockerfile` for the build recipe.
    """
    import docker as docker_sdk
    from email_agent.sandbox.docker import DockerSandbox

    client = docker_sdk.from_env()
    return DockerSandbox(
        client=client,
        image=settings.sandbox_image,
        sandbox_data_root=settings.sandbox_data_root,
        memory_mb=settings.sandbox_memory_mb,
        cpu_cores=settings.sandbox_cpu_cores,
        bash_timeout_seconds=settings.sandbox_bash_timeout_seconds,
    )


def make_runtime_from_settings(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    email_provider: "EmailProvider",
    sandbox: "AssistantSandbox | None" = None,
    memory: "MemoryPort | None" = None,
    use_real_model: bool = True,
    use_docker_sandbox: bool = True,
    run_timeout_seconds: float | None = None,
) -> AssistantRuntime:
    """Compose a fully-wired AssistantRuntime for production-ish use.

    `sandbox` defaults to a `DockerSandbox` (`use_docker_sandbox=True`),
    falling back to `InMemorySandbox` when the toggle is off — useful for
    quick iteration without docker. `memory` defaults to
    `InMemoryMemoryAdapter`; the real Cognee adapter lands in slice 6.
    `use_real_model=False` skips wiring Fireworks so callers can rely on
    a test override; `use_real_model=True` (default) plumbs through
    `make_fireworks_model_factory(settings)`.
    """
    if sandbox is None:
        sandbox = make_docker_sandbox(settings) if use_docker_sandbox else InMemorySandbox()
    memory = memory or InMemoryMemoryAdapter()
    projector = EmailWorkspaceProjector(run_inputs_root=settings.run_inputs_root)

    settings.attachments_root.mkdir(parents=True, exist_ok=True)
    settings.run_inputs_root.mkdir(parents=True, exist_ok=True)

    model_factory = make_fireworks_model_factory(settings) if use_real_model else None

    return AssistantRuntime(
        session_factory,
        attachments_root=settings.attachments_root,
        email_provider=email_provider,
        sandbox=sandbox,
        memory=memory,
        agent=AssistantAgent(),
        projector=projector,
        model_factory=model_factory,
        run_timeout_seconds=run_timeout_seconds,
    )


def make_runtime_for_inject(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    use_real_model: bool = True,
    use_docker_sandbox: bool = True,
) -> tuple[AssistantRuntime, "EmailProvider"]:
    """Build a runtime suitable for `inject-email --follow`.

    Always uses InMemoryEmailProvider so a fixture-driven local run never
    accidentally sends real Mailgun mail. The caller can inspect
    `email_provider.sent` after the run to see what would have gone out.
    """
    from email_agent.mail.inmemory import InMemoryEmailProvider

    email_provider = InMemoryEmailProvider()
    runtime = make_runtime_from_settings(
        settings,
        session_factory,
        email_provider=email_provider,
        use_real_model=use_real_model,
        use_docker_sandbox=use_docker_sandbox,
        run_timeout_seconds=settings.sandbox_run_timeout_seconds,
    )
    return runtime, email_provider


__all__ = [
    "make_docker_sandbox",
    "make_fireworks_model_factory",
    "make_runtime_for_inject",
    "make_runtime_from_settings",
]
