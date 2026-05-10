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


def make_deepseek_model_factory(
    settings: Settings,
) -> "Callable[[AssistantScope], Model]":
    """Return a factory that builds a DeepSeek model from `scope.model_name`.

    DeepSeek's API is OpenAI-compatible, so we use PydanticAI's OpenAI
    provider class with a custom `base_url`.
    """
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    api_key = settings.deepseek_api_key.get_secret_value()
    base_url = str(settings.deepseek_base_url)

    def factory(scope: AssistantScope) -> "Model":
        # Normalise short names so assistants can reference "deepseek-flash"
        # but the actual model id sent to the API is correct.
        model_id = _resolve_deepseek_model_id(scope.model_name)
        provider = OpenAIProvider(api_key=api_key, base_url=base_url)
        return OpenAIChatModel(model_id, provider=provider)

    return factory


def _resolve_deepseek_model_id(name: str) -> str:
    """Map our short names to DeepSeek's model ids."""
    aliases = {
        "deepseek-flash": "deepseek-chat",
        "deepseek-v4-flash": "deepseek-chat",
    }
    return aliases.get(name, name)


def make_runtime_from_settings(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    email_provider: "EmailProvider",
    sandbox: "AssistantSandbox | None" = None,
    memory: "MemoryPort | None" = None,
    use_real_model: bool = True,
    run_timeout_seconds: float | None = None,
) -> AssistantRuntime:
    """Compose a fully-wired AssistantRuntime for production-ish use.

    `sandbox` defaults to an `InMemorySandbox` since the docker sandbox
    isn't yet wired into composition (slice 6/7 work). `memory` defaults
    to `InMemoryMemoryAdapter` for the same reason — Cognee is slice 6.
    `use_real_model=False` skips wiring DeepSeek so callers can rely on
    a test override; `use_real_model=True` (default) plumbs through
    `make_deepseek_model_factory(settings)`.
    """
    sandbox = sandbox or InMemorySandbox()
    memory = memory or InMemoryMemoryAdapter()
    projector = EmailWorkspaceProjector(run_inputs_root=settings.run_inputs_root)

    settings.attachments_root.mkdir(parents=True, exist_ok=True)
    settings.run_inputs_root.mkdir(parents=True, exist_ok=True)

    model_factory = make_deepseek_model_factory(settings) if use_real_model else None

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
        run_timeout_seconds=settings.sandbox_run_timeout_seconds,
    )
    return runtime, email_provider


__all__ = [
    "make_deepseek_model_factory",
    "make_runtime_for_inject",
    "make_runtime_from_settings",
]
