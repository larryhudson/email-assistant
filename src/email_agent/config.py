from pathlib import Path

from pydantic import HttpUrl, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    database_url: PostgresDsn

    mailgun_signing_key: SecretStr
    mailgun_api_key: SecretStr
    mailgun_domain: str
    mailgun_webhook_url: HttpUrl

    fireworks_api_key: SecretStr
    fireworks_model_id: str = "accounts/fireworks/models/minimax-m2p7"

    cognee_llm_api_key: SecretStr
    cognee_embedding_api_key: SecretStr
    cognee_embedding_model: str = "text-embedding-3-small"

    sandbox_image: str = "email-agent-sandbox:slice4"
    sandbox_data_root: Path = Path("data/sandboxes")
    sandbox_idle_shutdown_minutes: int = 30
    sandbox_run_timeout_seconds: int = 300
    sandbox_bash_timeout_seconds: int = 60
    sandbox_memory_mb: int = 512
    sandbox_cpu_cores: float = 1.0

    attachments_root: Path = Path("data/attachments")
    cognee_data_root: Path = Path("data/cognee")
    run_inputs_root: Path = Path("data/run_inputs")

    admin_bind_host: str = "127.0.0.1"
    admin_bind_port: int = 8001
