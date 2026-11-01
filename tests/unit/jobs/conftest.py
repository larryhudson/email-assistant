import pytest


@pytest.fixture
def worker_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dummy settings for importing registered tasks without a local .env.

    Task tests substitute worker dependencies and do not connect to these services.
    """
    values = {
        "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
        "MAILGUN_SIGNING_KEY": "unit-test-signing-key",
        "MAILGUN_API_KEY": "unit-test-mailgun-key",
        "MAILGUN_DOMAIN": "example.com",
        "MAILGUN_WEBHOOK_URL": "https://example.com/webhooks/mailgun",
        "FIREWORKS_API_KEY": "unit-test-model-key",
        "COGNEE_LLM_API_KEY": "unit-test-memory-key",
        "COGNEE_EMBEDDING_API_KEY": "unit-test-embedding-key",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
