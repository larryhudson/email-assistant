from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, HttpUrl, PostgresDsn, SecretStr
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

    brave_search_api_key: SecretStr | None = None
    web_search_enabled: bool = True
    brave_search_timeout_seconds: float = 30.0

    # Cognee LLM provider — defaults to Fireworks via LiteLLM's "custom"
    # provider so it shares the agent's existing key. Set
    # `COGNEE_LLM_API_KEY=$FIREWORKS_API_KEY` in .env (same value, separate
    # var so users can swap cognee to a different provider/key without
    # touching the agent's). Override via env to use OpenAI etc.
    cognee_llm_api_key: SecretStr
    cognee_llm_provider: str = "custom"
    cognee_llm_model: str = "fireworks_ai/accounts/fireworks/models/minimax-m2p7"
    cognee_llm_endpoint: str | None = "https://api.fireworks.ai/inference/v1"

    cognee_embedding_api_key: SecretStr
    cognee_embedding_provider: str = "openai"
    cognee_embedding_model: str = "text-embedding-3-small"
    cognee_embedding_endpoint: str | None = None
    # Must match the model: text-embedding-3-small → 1536, text-embedding-3-large → 3072.
    # Cognee defaults to 3072, which fails on the small model with "dimensions must be ≤ 1536".
    cognee_embedding_dimensions: int = 1536

    # Cognee runs an LLM connection test on startup that adds 30s+ to the
    # first call and times out under unreliable network/keys. We bypass by
    # default in dev — a bad key still fails loudly on the actual remember
    # call. Set false to opt back in for a stricter check.
    cognee_skip_connection_test: bool = True

    # Toggle the memory layer wholesale. When False, the runtime, agent,
    # and worker all skip recall/curate and the `memory_search` tool is
    # not registered with the agent. Useful for offline iteration without
    # any cognee deps, or for assistants that don't need durable memory.
    memory_enabled: bool = True

    sandbox_image: str = "email-agent-sandbox:slice4"
    sandbox_provider: Literal["docker", "bashkit", "in_memory"] = "docker"
    sandbox_data_root: Path = Path("data/sandboxes")
    sandbox_idle_shutdown_minutes: int = 30
    sandbox_run_timeout_seconds: int = 300
    sandbox_bash_timeout_seconds: int = 60
    sandbox_bashkit_python_enabled: bool = True
    sandbox_bashkit_sqlite_enabled: bool = True
    sandbox_memory_mb: int = 512
    sandbox_cpu_cores: float = 1.0

    attachments_root: Path = Path("data/attachments")
    cognee_data_root: Path = Path("data/cognee")
    run_inputs_root: Path = Path("data/run_inputs")

    admin_bind_host: str = "127.0.0.1"
    admin_bind_port: int = 8001
    admin_basic_auth_username: str | None = None
    admin_basic_auth_password: SecretStr | None = None

    # Public base URL of the admin UI. When set, the cost/run footer appended
    # to outbound replies includes a deep-link to the run's admin trace page.
    admin_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("EMAIL_AGENT_ADMIN_BASE_URL", "ADMIN_BASE_URL"),
    )
