from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.composition import make_runtime_from_settings
from email_agent.config import Settings
from email_agent.mail.inmemory import InMemoryEmailProvider
from email_agent.sandbox.bashkit_environment import BashkitWorkspaceProvider


def _settings(monkeypatch) -> Settings:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    monkeypatch.setenv("MAILGUN_SIGNING_KEY", "sig")
    monkeypatch.setenv("MAILGUN_API_KEY", "api")
    monkeypatch.setenv("MAILGUN_DOMAIN", "mg.example.com")
    monkeypatch.setenv("MAILGUN_WEBHOOK_URL", "https://example.com/hook")
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw")
    monkeypatch.setenv("COGNEE_LLM_API_KEY", "cog-llm")
    monkeypatch.setenv("COGNEE_EMBEDDING_API_KEY", "cog-emb")
    monkeypatch.setenv("MEMORY_ENABLED", "false")
    monkeypatch.setenv("SANDBOX_PROVIDER", "bashkit")
    return Settings()  # ty: ignore[missing-argument]


def test_make_runtime_can_select_bashkit_workspace_provider(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    runtime = make_runtime_from_settings(
        _settings(monkeypatch),
        sqlite_session_factory,
        email_provider=InMemoryEmailProvider(),
        use_real_model=False,
        use_real_memory=False,
        use_procrastinate=False,
    )

    assert isinstance(runtime._workspace_provider, BashkitWorkspaceProvider)
