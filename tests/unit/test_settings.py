import pytest
from pydantic import SecretStr, ValidationError

from email_agent.config import Settings


def test_settings_loads_required_fields(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://u:p@localhost:5432/db",
    )
    monkeypatch.setenv("MAILGUN_SIGNING_KEY", "sig")
    monkeypatch.setenv("MAILGUN_API_KEY", "api")
    monkeypatch.setenv("MAILGUN_DOMAIN", "mg.example.com")
    monkeypatch.setenv("MAILGUN_WEBHOOK_URL", "https://example.com/hook")
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw")
    monkeypatch.setenv("COGNEE_LLM_API_KEY", "cog-llm")
    monkeypatch.setenv("COGNEE_EMBEDDING_API_KEY", "cog-emb")

    s = Settings()  # ty: ignore[missing-argument]

    assert str(s.database_url).startswith("postgresql+asyncpg://")
    assert isinstance(s.mailgun_signing_key, SecretStr)
    assert s.mailgun_signing_key.get_secret_value() == "sig"
    assert s.sandbox_idle_shutdown_minutes == 30
    assert s.sandbox_run_timeout_seconds == 300
    assert s.admin_bind_port == 8001


def test_settings_missing_required_field_raises(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MAILGUN_SIGNING_KEY", raising=False)

    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None)  # ty: ignore[missing-argument, unknown-argument]
