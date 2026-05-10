# Slice 1 — Core Data + Ports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lay the foundation for the email assistant: normalized data models, port protocols, in-memory adapters, Postgres schema (Alembic), Settings, Docker Compose, and a typer CLI skeleton — all behind the seams that later slices will plug real adapters into.

**Architecture:** Ports & adapters (a.k.a. hexagonal). The core — pure pydantic data models in `src/email_agent/models/` and abstract `Protocol` interfaces ("ports") in `src/email_agent/ports/` — has no knowledge of Mailgun, Postgres, Docker, Cognee, or PydanticAI. Each external dependency is reached through a port; concrete "adapters" implement those ports. This slice ships in-memory adapters (`src/email_agent/adapters/inmemory/`) for tests; real adapters land in later slices. Persistence uses SQLAlchemy 2.0 async + Alembic in `src/email_agent/db/`. Config via `pydantic-settings`, CLI via `typer`.

**Tech Stack:** Python 3.13 · `uv` for env/deps · `pydantic` v2 · `pydantic-settings` · `SQLAlchemy` 2.0 async · `asyncpg` · `Alembic` · `typer` · `pytest` + `pytest-asyncio` · `ruff` (lint + format) · `ty` (Astral's type checker) · `pre-commit` · Docker Compose (Postgres 16).

**Out of scope (later slices):** Mailgun adapter, Cognee adapter, Docker sandbox, PydanticAI agent, runtime/orchestrator, web/admin, Procrastinate jobs, structured logging, CI workflow.

---

## File structure

Files this plan creates (all paths relative to repo root `/Users/larryhudson/github.com/larryhudson/email-assistant`):

```
pyproject.toml                                   # rewritten with deps + tool config
.gitignore                                       # extended (data/, .venv/, __pycache__, etc.)
docker-compose.yml                               # Postgres service for dev + tests
alembic.ini                                      # alembic config pointing at src migrations
.env.example                                     # documented sample env
.pre-commit-config.yaml                          # ruff + ty on commit, pytest on push
src/email_agent/
  __init__.py
  config.py                                      # Settings (pydantic-settings)
  cli.py                                         # typer app: migrate, hello
  models/
    __init__.py                                  # re-exports
    email.py                                     # NormalizedInboundEmail, NormalizedOutboundEmail, EmailAttachment, SentEmail, WebhookRequest
    assistant.py                                 # AssistantScope, AssistantStatus
    memory.py                                    # Memory, MemoryContext
    sandbox.py                                   # ToolCall variants, ToolResult, BashResult, ProjectedFile, PendingAttachment
  ports/
    __init__.py
    email_provider.py                            # EmailProvider Protocol
    memory.py                                    # MemoryPort Protocol
    sandbox.py                                   # AssistantSandbox Protocol
  adapters/
    __init__.py
    inmemory/
      __init__.py
      email_provider.py                          # InMemoryEmailProvider
      memory.py                                  # InMemoryMemoryAdapter
      sandbox.py                                 # InMemorySandbox
  db/
    __init__.py
    base.py                                      # DeclarativeBase + naming convention
    models.py                                    # ORM tables (owners, admins, end_users, assistants, ...)
    session.py                                   # async engine + session factory
    migrations/
      env.py                                     # alembic env, async-aware
      script.py.mako                             # standard alembic template
      versions/
        0001_initial.py                          # initial schema
tests/
  __init__.py
  conftest.py                                    # shared fixtures (settings override, db engine)
  unit/
    __init__.py
    test_email_models.py
    test_inmemory_email_provider.py
    test_inmemory_memory.py
    test_inmemory_sandbox.py
  integration/
    __init__.py
    test_alembic_upgrade.py                      # spins up via docker-compose pg, runs alembic upgrade head, checks tables
    test_db_roundtrip.py                         # insert + query an assistants row
```

Each file has one responsibility:

- `models/*.py` — frozen pydantic models, no behaviour.
- `ports/*.py` — `Protocol` definitions only.
- `adapters/inmemory/*.py` — in-process implementations of the ports for tests.
- `db/models.py` — SQLAlchemy 2.0 typed ORM models matching the spec's data model section.
- `db/session.py` — engine + `async_sessionmaker`.
- `db/migrations/` — Alembic files, `env.py` reads URL from `Settings`.
- `config.py` — single `Settings` class.
- `cli.py` — `typer` app with `migrate` and a `hello` smoke command (more commands in later slices).

---

## Task 1: Project bootstrap (pyproject, uv, tooling)

**Files:**
- Modify: `pyproject.toml`
- Modify: `.gitignore`
- Create: `.env.example`

- [ ] **Step 1: Set project description**

The repo already has a minimal `pyproject.toml`. Edit it to give it a real description:

```toml
[project]
name = "email-assistant"
version = "0.1.0"
description = "Email-based AI assistant runtime"
readme = "README.md"
requires-python = ">=3.13"
dependencies = []
```

(Leave `dependencies = []` — `uv add` populates it in the next step.)

- [ ] **Step 2: Add runtime dependencies via `uv add`**

Run them as one command so `uv` resolves them together:

```bash
uv add pydantic pydantic-settings 'sqlalchemy[asyncio]' asyncpg alembic typer
```

Expected: each package added to `[project.dependencies]` at its current latest version, `uv.lock` written, `.venv/` created.

- [ ] **Step 3: Add dev dependencies via `uv add --dev`**

```bash
uv add --dev pytest pytest-asyncio ruff
```

Expected: each added under `[dependency-groups].dev` (or `[tool.uv].dev-dependencies`, depending on uv version — either form is fine).

- [ ] **Step 4: Append build, script, and tool config to pyproject.toml**

Append these sections to the end of `pyproject.toml` (do **not** touch what `uv add` wrote — only add new sections):

```toml
[project.scripts]
email-agent = "email_agent.cli:app"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/email_agent"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["src"]
markers = [
  "integration: requires Postgres running via docker-compose",
]

[tool.ruff]
line-length = 100
target-version = "py313"

[tool.ruff.lint]
# Sensible default set. Add/remove as the codebase teaches us what's noisy.
select = [
  "E", "F", "W",   # pycodestyle + pyflakes
  "I",             # isort
  "UP",            # pyupgrade (modern syntax)
  "B",             # flake8-bugbear (likely bugs)
  "SIM",           # flake8-simplify
  "N",             # pep8-naming
  "C4",            # comprehension cleanups
  "PIE",           # misc lints
  "PT",            # pytest style
  "RET",           # return-statement lints
  "TID",           # tidy imports
  "ASYNC",         # async correctness
  "RUF",           # ruff-specific
]
ignore = [
  "E501",          # line length — let formatter handle
  "B008",          # function call in default arg (pydantic Field, typer.Option)
]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["N802"]   # allow snake_case test names that violate pep8-naming edge cases
```

- [ ] **Step 5: Re-sync to pick up the new sections**

Run: `uv sync`
Expected: no changes to dependencies, project re-installed in editable mode.

- [ ] **Step 6: Extend .gitignore**

Append to `.gitignore`:

```
# Python
__pycache__/
*.pyc
.venv/
.pytest_cache/
.ruff_cache/

# Project data
data/
.env

# Editor
.vscode/
.idea/
```

- [ ] **Step 7: Create .env.example**

```
DATABASE_URL=postgresql+asyncpg://email_agent:devpassword@localhost:5432/email_agent
```

- [ ] **Step 8: Sanity-check tooling**

Run: `uv run python -c "import pydantic, sqlalchemy, alembic, typer; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 9: Delete the placeholder main.py**

It conflicts with the package layout we're about to add.

Run: `git rm main.py`

- [ ] **Step 10: Add `ty` (Astral type checker) and `pre-commit` as dev deps**

```bash
uv add --dev ty pre-commit
```

Expected: both added at their current latest versions. `ty` is Astral's pre-release Python type checker (https://github.com/astral-sh/ty); we use it instead of mypy.

- [ ] **Step 11: Append `ty` config to pyproject.toml**

Append (keys confirmed against https://docs.astral.sh/ty/reference/configuration/):

```toml
[tool.ty.environment]
python-version = "3.13"

[tool.ty.src]
include = ["src", "tests"]
```

`ty` is pre-1.0 (versioned `0.0.x`); config keys may shift between releases. If a future `uv sync` upgrades `ty` and these keys become invalid, check the reference URL above for the current schema.

- [ ] **Step 12: Smoke-test `ty`**

Run: `uv run ty check`
Expected: exits 0 (no source files yet, so nothing to flag).

- [ ] **Step 13: Create `.pre-commit-config.yaml`**

```yaml
# Fast checks on every commit; tests on push (slower).
default_install_hook_types: [pre-commit, pre-push]

repos:
  - repo: local
    hooks:
      - id: ruff-lint
        name: ruff lint
        entry: uv run ruff check --fix
        language: system
        types_or: [python, pyi]
        require_serial: true

      - id: ruff-format
        name: ruff format
        entry: uv run ruff format
        language: system
        types_or: [python, pyi]
        require_serial: true

      - id: ty
        name: ty type-check
        entry: uv run ty check
        language: system
        pass_filenames: false
        types: [python]

      - id: pytest-unit
        name: pytest (unit)
        entry: uv run pytest tests/unit -q
        language: system
        pass_filenames: false
        types: [python]
        stages: [pre-push]
```

Why "local" hooks: keeps pre-commit pinned to the same `uv`-managed versions as the rest of the project, instead of pre-commit fetching its own copies.

Why pytest is `pre-push`: keeps every commit fast; the unit suite still runs before code leaves the machine.

- [ ] **Step 14: Install the hooks**

Run: `uv run pre-commit install --install-hooks`
Expected: writes `.git/hooks/pre-commit` and `.git/hooks/pre-push`.

- [ ] **Step 15: Run hooks once to verify**

Run: `uv run pre-commit run --all-files`
Expected: every hook passes (there's no source code yet, so ruff/ty have nothing to flag). The pytest hook is `pre-push` only and won't run here.

- [ ] **Step 16: Commit**

```bash
git add pyproject.toml .gitignore .env.example uv.lock .pre-commit-config.yaml
git commit -m "chore: bootstrap uv project with core deps, ruff, ty, and pre-commit"
```

---

## Task 2: Package skeleton

**Files:**
- Create: `src/email_agent/__init__.py`

- [ ] **Step 1: Create the package init**

```python
"""Email Assistant — see docs/superpowers/specs/2026-05-10-email-assistant-design.md."""

__version__ = "0.1.0"
```

- [ ] **Step 2: Verify import path works**

Run: `uv run python -c "import email_agent; print(email_agent.__version__)"`
Expected: prints `0.1.0`.

- [ ] **Step 3: Commit**

```bash
git add src/email_agent/__init__.py
git commit -m "chore: add email_agent package skeleton"
```

---

## Task 3: Settings (pydantic-settings)

**Files:**
- Create: `src/email_agent/config.py`
- Create: `tests/__init__.py`
- Create: `tests/unit/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/unit/test_settings.py`

- [ ] **Step 1: Write the failing test**

Create `tests/__init__.py` (empty), `tests/unit/__init__.py` (empty), `tests/conftest.py` (empty for now), and `tests/unit/test_settings.py`:

```python
from pydantic import SecretStr

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
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dsk")
    monkeypatch.setenv("COGNEE_LLM_API_KEY", "cog-llm")
    monkeypatch.setenv("COGNEE_EMBEDDING_API_KEY", "cog-emb")

    s = Settings()

    assert str(s.database_url).startswith("postgresql+asyncpg://")
    assert isinstance(s.mailgun_signing_key, SecretStr)
    assert s.mailgun_signing_key.get_secret_value() == "sig"
    assert s.sandbox_idle_shutdown_minutes == 30
    assert s.sandbox_run_timeout_seconds == 300
    assert s.admin_bind_port == 8001


def test_settings_missing_required_field_raises(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MAILGUN_SIGNING_KEY", raising=False)
    import pytest

    with pytest.raises(Exception):
        Settings(_env_file=None)
```

- [ ] **Step 2: Run the test to confirm it fails**

Run: `uv run pytest tests/unit/test_settings.py -v`
Expected: ImportError / ModuleNotFoundError on `email_agent.config`.

- [ ] **Step 3: Implement Settings**

Create `src/email_agent/config.py`:

```python
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

    deepseek_api_key: SecretStr
    deepseek_base_url: HttpUrl = HttpUrl("https://api.deepseek.com/v1")

    cognee_llm_api_key: SecretStr
    cognee_embedding_api_key: SecretStr
    cognee_embedding_model: str = "text-embedding-3-small"

    sandbox_image: str = "email-assistant-sandbox:latest"
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
```

- [ ] **Step 4: Run the test to confirm it passes**

Run: `uv run pytest tests/unit/test_settings.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/email_agent/config.py tests/__init__.py tests/unit/__init__.py tests/conftest.py tests/unit/test_settings.py
git commit -m "feat(config): add Settings via pydantic-settings"
```

---

## Task 4: Email models

**Files:**
- Create: `src/email_agent/models/__init__.py`
- Create: `src/email_agent/models/email.py`
- Create: `tests/unit/test_email_models.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_email_models.py`:

```python
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from email_agent.models.email import (
    EmailAttachment,
    NormalizedInboundEmail,
    NormalizedOutboundEmail,
    SentEmail,
    WebhookRequest,
)


def test_inbound_email_round_trips():
    email = NormalizedInboundEmail(
        provider_message_id="mg-123",
        message_id_header="<abc@mg.example>",
        in_reply_to_header=None,
        references_headers=[],
        from_email="mum@example.com",
        to_emails=["assistant+mum@example.com"],
        subject="Hi",
        body_text="hello",
        body_html=None,
        attachments=[],
        received_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
    )
    assert email.from_email == "mum@example.com"
    assert email.attachments == []


def test_inbound_email_rejects_unknown_field():
    with pytest.raises(ValidationError):
        NormalizedInboundEmail(
            provider_message_id="x",
            message_id_header="<x>",
            from_email="a@b.com",
            to_emails=["c@d.com"],
            subject="s",
            body_text="b",
            received_at=datetime.now(UTC),
            bogus_field="nope",  # type: ignore[call-arg]
        )


def test_inbound_email_is_immutable():
    email = NormalizedInboundEmail(
        provider_message_id="x",
        message_id_header="<x>",
        from_email="a@b.com",
        to_emails=["c@d.com"],
        subject="s",
        body_text="b",
        received_at=datetime.now(UTC),
    )
    with pytest.raises(ValidationError):
        email.subject = "changed"  # type: ignore[misc]


def test_attachment_holds_bytes():
    a = EmailAttachment(
        filename="a.pdf",
        content_type="application/pdf",
        size_bytes=4,
        data=b"%PDF",
    )
    assert a.data == b"%PDF"


def test_outbound_email_requires_in_reply_to_when_threading():
    out = NormalizedOutboundEmail(
        from_email="assistant+mum@example.com",
        to_emails=["mum@example.com"],
        subject="Re: Hi",
        body_text="hello back",
        message_id_header="<reply@mg.example>",
        in_reply_to_header="<abc@mg.example>",
        references_headers=["<abc@mg.example>"],
        attachments=[],
    )
    assert out.in_reply_to_header == "<abc@mg.example>"


def test_sent_email_records_provider_id():
    sent = SentEmail(provider_message_id="mg-out-1", message_id_header="<reply@mg.example>")
    assert sent.provider_message_id == "mg-out-1"


def test_webhook_request_is_a_carrier():
    req = WebhookRequest(headers={"X-Sig": "..."}, body=b"raw", form={"from": "a@b"})
    assert req.form["from"] == "a@b"
```

- [ ] **Step 2: Run the tests to confirm failure**

Run: `uv run pytest tests/unit/test_email_models.py -v`
Expected: ImportError on the models module.

- [ ] **Step 3: Implement the models**

Create `src/email_agent/models/__init__.py`:

```python
from email_agent.models.email import (
    EmailAttachment,
    NormalizedInboundEmail,
    NormalizedOutboundEmail,
    SentEmail,
    WebhookRequest,
)

__all__ = [
    "EmailAttachment",
    "NormalizedInboundEmail",
    "NormalizedOutboundEmail",
    "SentEmail",
    "WebhookRequest",
]
```

Create `src/email_agent/models/email.py`:

```python
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EmailAttachment(_Frozen):
    filename: str
    content_type: str
    size_bytes: int
    data: bytes


class NormalizedInboundEmail(_Frozen):
    provider_message_id: str
    message_id_header: str
    in_reply_to_header: str | None = None
    references_headers: list[str] = Field(default_factory=list)
    from_email: str
    to_emails: list[str]
    subject: str
    body_text: str
    body_html: str | None = None
    attachments: list[EmailAttachment] = Field(default_factory=list)
    received_at: datetime


class NormalizedOutboundEmail(_Frozen):
    from_email: str
    to_emails: list[str]
    subject: str
    body_text: str
    message_id_header: str
    in_reply_to_header: str | None = None
    references_headers: list[str] = Field(default_factory=list)
    attachments: list[EmailAttachment] = Field(default_factory=list)


class SentEmail(_Frozen):
    provider_message_id: str
    message_id_header: str


class WebhookRequest(_Frozen):
    headers: dict[str, str]
    body: bytes
    form: dict[str, str] = Field(default_factory=dict)
```

- [ ] **Step 4: Run the tests to confirm pass**

Run: `uv run pytest tests/unit/test_email_models.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/email_agent/models/__init__.py src/email_agent/models/email.py tests/unit/test_email_models.py
git commit -m "feat(models): add normalized email + attachment models"
```

---

## Task 5: Assistant, memory, sandbox models

**Files:**
- Create: `src/email_agent/models/assistant.py`
- Create: `src/email_agent/models/memory.py`
- Create: `src/email_agent/models/sandbox.py`
- Modify: `src/email_agent/models/__init__.py`
- Create: `tests/unit/test_domain_models.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_domain_models.py`:

```python
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from email_agent.models.assistant import AssistantScope, AssistantStatus
from email_agent.models.memory import Memory, MemoryContext
from email_agent.models.sandbox import (
    BashResult,
    PendingAttachment,
    ProjectedFile,
    ToolCall,
    ToolResult,
)


def test_assistant_scope_carries_owner_chain():
    scope = AssistantScope(
        assistant_id="a-1",
        owner_id="o-1",
        end_user_id="u-1",
        inbound_address="assistant+mum@example.com",
        status=AssistantStatus.ACTIVE,
        allowed_senders=("mum@example.com",),
        memory_namespace="a-1",
        tool_allowlist=("read", "write", "edit", "bash", "memory_search", "attach_file"),
        budget_id="b-1",
        model_name="deepseek-flash",
        system_prompt="You are kind.",
    )
    assert scope.is_sender_allowed("mum@example.com")
    assert not scope.is_sender_allowed("spam@example.com")
    assert not scope.is_sender_allowed("MUM@example.com") is False  # case-insensitive
    assert scope.is_sender_allowed("MUM@example.com")


def test_assistant_scope_rejects_mutation():
    scope = AssistantScope(
        assistant_id="a",
        owner_id="o",
        end_user_id="u",
        inbound_address="x@y",
        status=AssistantStatus.ACTIVE,
        allowed_senders=(),
        memory_namespace="a",
        tool_allowlist=(),
        budget_id="b",
        model_name="m",
        system_prompt="p",
    )
    with pytest.raises(ValidationError):
        scope.status = AssistantStatus.PAUSED  # type: ignore[misc]


def test_memory_and_context():
    m = Memory(id="m-1", content="user prefers short replies", source_run_id="r-1")
    ctx = MemoryContext(memories=[m], retrieved_at=datetime.now(UTC))
    assert ctx.memories[0].content.endswith("short replies")


def test_tool_call_variants():
    read_call = ToolCall(kind="read", path="/workspace/x.md")
    write_call = ToolCall(kind="write", path="/workspace/y.md", content="hi")
    bash_call = ToolCall(kind="bash", command="ls /workspace")
    assert read_call.kind == "read"
    assert write_call.content == "hi"
    assert bash_call.command == "ls /workspace"


def test_tool_call_rejects_missing_required_field():
    with pytest.raises(ValidationError):
        ToolCall(kind="write", path="/workspace/y.md")  # content missing


def test_bash_result_carries_streams():
    r = BashResult(exit_code=0, stdout="ok\n", stderr="", duration_ms=12)
    assert r.exit_code == 0


def test_tool_result_can_wrap_bash():
    r = ToolResult(ok=True, output=BashResult(exit_code=0, stdout="", stderr="", duration_ms=1))
    assert r.ok is True


def test_projected_file_holds_bytes():
    f = ProjectedFile(path="emails/0001-from-mum.md", content=b"---\nsubject: hi\n---\n")
    assert f.path.startswith("emails/")


def test_pending_attachment_records_filename():
    pa = PendingAttachment(sandbox_path="/workspace/out.pdf", filename="report.pdf")
    assert pa.filename == "report.pdf"
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `uv run pytest tests/unit/test_domain_models.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement assistant.py**

Create `src/email_agent/models/assistant.py`:

```python
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class AssistantStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"


class AssistantScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    assistant_id: str
    owner_id: str
    end_user_id: str
    inbound_address: str
    status: AssistantStatus
    allowed_senders: tuple[str, ...]
    memory_namespace: str
    tool_allowlist: tuple[str, ...]
    budget_id: str
    model_name: str
    system_prompt: str

    def is_sender_allowed(self, email: str) -> bool:
        target = email.lower()
        return any(s.lower() == target for s in self.allowed_senders)
```

- [ ] **Step 4: Implement memory.py**

Create `src/email_agent/models/memory.py`:

```python
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Memory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    content: str
    source_run_id: str | None = None
    score: float | None = None


class MemoryContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    memories: list[Memory] = Field(default_factory=list)
    retrieved_at: datetime
```

- [ ] **Step 5: Implement sandbox.py**

Create `src/email_agent/models/sandbox.py`:

```python
from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProjectedFile(_Frozen):
    path: str
    content: bytes


class BashResult(_Frozen):
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


class PendingAttachment(_Frozen):
    sandbox_path: str
    filename: str


class ToolCall(_Frozen):
    kind: Literal["read", "write", "edit", "bash", "attach_file"]
    path: str | None = None
    content: str | None = None
    old: str | None = None
    new: str | None = None
    command: str | None = None
    filename: str | None = None

    def model_post_init(self, _ctx) -> None:
        required = {
            "read": ("path",),
            "write": ("path", "content"),
            "edit": ("path", "old", "new"),
            "bash": ("command",),
            "attach_file": ("path",),
        }[self.kind]
        for field in required:
            if getattr(self, field) is None:
                raise ValueError(f"{self.kind} tool call requires {field}")


class ToolResult(_Frozen):
    ok: bool
    output: BashResult | str | None = None
    error: str | None = None
```

- [ ] **Step 6: Update models __init__.py**

Replace `src/email_agent/models/__init__.py`:

```python
from email_agent.models.assistant import AssistantScope, AssistantStatus
from email_agent.models.email import (
    EmailAttachment,
    NormalizedInboundEmail,
    NormalizedOutboundEmail,
    SentEmail,
    WebhookRequest,
)
from email_agent.models.memory import Memory, MemoryContext
from email_agent.models.sandbox import (
    BashResult,
    PendingAttachment,
    ProjectedFile,
    ToolCall,
    ToolResult,
)

__all__ = [
    "AssistantScope",
    "AssistantStatus",
    "BashResult",
    "EmailAttachment",
    "Memory",
    "MemoryContext",
    "NormalizedInboundEmail",
    "NormalizedOutboundEmail",
    "PendingAttachment",
    "ProjectedFile",
    "SentEmail",
    "ToolCall",
    "ToolResult",
    "WebhookRequest",
]
```

- [ ] **Step 7: Run tests to confirm pass**

Run: `uv run pytest tests/unit/test_domain_models.py -v`
Expected: 9 passed.

Note: `ValidationError` from pydantic wraps `ValueError` raised in `model_post_init`, so the `test_tool_call_rejects_missing_required_field` test should pass. If it fails, switch to `pytest.raises((ValidationError, ValueError))` in the test.

- [ ] **Step 8: Commit**

```bash
git add src/email_agent/models/ tests/unit/test_domain_models.py
git commit -m "feat(models): add assistant, memory, and sandbox models"
```

---

## Task 6: Port protocols

**Files:**
- Create: `src/email_agent/ports/__init__.py`
- Create: `src/email_agent/ports/email_provider.py`
- Create: `src/email_agent/ports/memory.py`
- Create: `src/email_agent/ports/sandbox.py`
- Create: `tests/unit/test_ports.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_ports.py`:

```python
from typing import get_type_hints

from email_agent.ports.email_provider import EmailProvider
from email_agent.ports.memory import MemoryPort
from email_agent.ports.sandbox import AssistantSandbox


def test_email_provider_has_required_methods():
    for name in ("verify_webhook", "parse_inbound", "send_reply"):
        assert hasattr(EmailProvider, name)


def test_memory_port_has_required_methods():
    for name in ("recall", "record_turn", "search", "delete_assistant"):
        assert hasattr(MemoryPort, name)


def test_sandbox_has_required_methods():
    for name in (
        "ensure_started",
        "project_emails",
        "project_attachments",
        "run_tool",
        "read_attachment_out",
        "reset",
    ):
        assert hasattr(AssistantSandbox, name)


def test_protocols_use_assistant_id_for_isolation():
    hints = get_type_hints(MemoryPort.recall)
    assert "assistant_id" in hints
    assert hints["assistant_id"] is str
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `uv run pytest tests/unit/test_ports.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement email_provider port**

Create `src/email_agent/ports/email_provider.py`:

```python
from typing import Protocol, runtime_checkable

from email_agent.models.email import (
    NormalizedInboundEmail,
    NormalizedOutboundEmail,
    SentEmail,
    WebhookRequest,
)


@runtime_checkable
class EmailProvider(Protocol):
    async def verify_webhook(self, request: WebhookRequest) -> None: ...
    async def parse_inbound(self, request: WebhookRequest) -> NormalizedInboundEmail: ...
    async def send_reply(self, reply: NormalizedOutboundEmail) -> SentEmail: ...
```

- [ ] **Step 4: Implement memory port**

Create `src/email_agent/ports/memory.py`:

```python
from typing import Protocol, runtime_checkable

from email_agent.models.memory import Memory, MemoryContext


@runtime_checkable
class MemoryPort(Protocol):
    async def recall(
        self, assistant_id: str, thread_id: str, query: str
    ) -> MemoryContext: ...

    async def record_turn(
        self, assistant_id: str, thread_id: str, role: str, content: str
    ) -> None: ...

    async def search(self, assistant_id: str, query: str) -> list[Memory]: ...

    async def delete_assistant(self, assistant_id: str) -> None: ...
```

- [ ] **Step 5: Implement sandbox port**

Create `src/email_agent/ports/sandbox.py`:

```python
from typing import Protocol, runtime_checkable

from email_agent.models.sandbox import ProjectedFile, ToolCall, ToolResult


@runtime_checkable
class AssistantSandbox(Protocol):
    async def ensure_started(self, assistant_id: str) -> None: ...

    async def project_emails(
        self, assistant_id: str, files: list[ProjectedFile]
    ) -> None: ...

    async def project_attachments(
        self, assistant_id: str, run_id: str, files: list[ProjectedFile]
    ) -> None: ...

    async def run_tool(
        self, assistant_id: str, run_id: str, call: ToolCall
    ) -> ToolResult: ...

    async def read_attachment_out(
        self, assistant_id: str, run_id: str, path: str
    ) -> bytes: ...

    async def reset(self, assistant_id: str) -> None: ...
```

- [ ] **Step 6: Create the ports init**

Create `src/email_agent/ports/__init__.py`:

```python
from email_agent.ports.email_provider import EmailProvider
from email_agent.ports.memory import MemoryPort
from email_agent.ports.sandbox import AssistantSandbox

__all__ = ["AssistantSandbox", "EmailProvider", "MemoryPort"]
```

- [ ] **Step 7: Run tests to confirm pass**

Run: `uv run pytest tests/unit/test_ports.py -v`
Expected: 4 passed.

- [ ] **Step 8: Commit**

```bash
git add src/email_agent/ports/ tests/unit/test_ports.py
git commit -m "feat(ports): add EmailProvider, MemoryPort, AssistantSandbox protocols"
```

---

## Task 7: InMemoryEmailProvider

**Files:**
- Create: `src/email_agent/adapters/__init__.py`
- Create: `src/email_agent/adapters/inmemory/__init__.py`
- Create: `src/email_agent/adapters/inmemory/email_provider.py`
- Create: `tests/unit/test_inmemory_email_provider.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_inmemory_email_provider.py`:

```python
from datetime import UTC, datetime

import pytest

from email_agent.adapters.inmemory.email_provider import InMemoryEmailProvider
from email_agent.models.email import (
    NormalizedInboundEmail,
    NormalizedOutboundEmail,
    WebhookRequest,
)


def _inbound() -> NormalizedInboundEmail:
    return NormalizedInboundEmail(
        provider_message_id="mg-1",
        message_id_header="<mg-1@in>",
        from_email="mum@example.com",
        to_emails=["assistant+mum@example.com"],
        subject="hi",
        body_text="hi there",
        received_at=datetime(2026, 5, 10, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_verify_webhook_is_a_noop_by_default():
    p = InMemoryEmailProvider()
    await p.verify_webhook(WebhookRequest(headers={}, body=b""))


@pytest.mark.asyncio
async def test_verify_webhook_can_be_made_to_fail():
    p = InMemoryEmailProvider(verify_should_raise=ValueError("bad sig"))
    with pytest.raises(ValueError):
        await p.verify_webhook(WebhookRequest(headers={}, body=b""))


@pytest.mark.asyncio
async def test_parse_inbound_returns_queued_email():
    p = InMemoryEmailProvider()
    pre = _inbound()
    p.queue_inbound(pre)
    got = await p.parse_inbound(WebhookRequest(headers={}, body=b""))
    assert got == pre


@pytest.mark.asyncio
async def test_parse_inbound_raises_when_empty():
    p = InMemoryEmailProvider()
    with pytest.raises(LookupError):
        await p.parse_inbound(WebhookRequest(headers={}, body=b""))


@pytest.mark.asyncio
async def test_send_reply_records_and_returns_id():
    p = InMemoryEmailProvider()
    out = NormalizedOutboundEmail(
        from_email="assistant+mum@example.com",
        to_emails=["mum@example.com"],
        subject="Re: hi",
        body_text="hi back",
        message_id_header="<reply@out>",
        in_reply_to_header="<mg-1@in>",
    )
    sent = await p.send_reply(out)
    assert sent.message_id_header == "<reply@out>"
    assert sent.provider_message_id.startswith("inmem-")
    assert p.sent == [out]
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `uv run pytest tests/unit/test_inmemory_email_provider.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the adapter**

Create `src/email_agent/adapters/__init__.py` (empty).

Create `src/email_agent/adapters/inmemory/__init__.py`:

```python
from email_agent.adapters.inmemory.email_provider import InMemoryEmailProvider

__all__ = ["InMemoryEmailProvider"]
```

Create `src/email_agent/adapters/inmemory/email_provider.py`:

```python
from collections import deque
from itertools import count

from email_agent.models.email import (
    NormalizedInboundEmail,
    NormalizedOutboundEmail,
    SentEmail,
    WebhookRequest,
)


class InMemoryEmailProvider:
    """In-process EmailProvider for tests.

    `queue_inbound(...)` enqueues emails that subsequent `parse_inbound` calls
    will return in FIFO order. `sent` records every reply.
    """

    def __init__(self, *, verify_should_raise: Exception | None = None) -> None:
        self._inbox: deque[NormalizedInboundEmail] = deque()
        self.sent: list[NormalizedOutboundEmail] = []
        self._verify_should_raise = verify_should_raise
        self._counter = count(1)

    def queue_inbound(self, email: NormalizedInboundEmail) -> None:
        self._inbox.append(email)

    async def verify_webhook(self, request: WebhookRequest) -> None:
        if self._verify_should_raise is not None:
            raise self._verify_should_raise

    async def parse_inbound(self, request: WebhookRequest) -> NormalizedInboundEmail:
        if not self._inbox:
            raise LookupError("no inbound emails queued")
        return self._inbox.popleft()

    async def send_reply(self, reply: NormalizedOutboundEmail) -> SentEmail:
        self.sent.append(reply)
        return SentEmail(
            provider_message_id=f"inmem-{next(self._counter)}",
            message_id_header=reply.message_id_header,
        )
```

- [ ] **Step 4: Run tests to confirm pass**

Run: `uv run pytest tests/unit/test_inmemory_email_provider.py -v`
Expected: 5 passed.

- [ ] **Step 5: Confirm Protocol conformance**

Run: `uv run python -c "from email_agent.ports import EmailProvider; from email_agent.adapters.inmemory import InMemoryEmailProvider; assert isinstance(InMemoryEmailProvider(), EmailProvider); print('ok')"`
Expected: prints `ok`.

- [ ] **Step 6: Commit**

```bash
git add src/email_agent/adapters/ tests/unit/test_inmemory_email_provider.py
git commit -m "feat(adapters): add InMemoryEmailProvider"
```

---

## Task 8: InMemoryMemoryAdapter

**Files:**
- Create: `src/email_agent/adapters/inmemory/memory.py`
- Modify: `src/email_agent/adapters/inmemory/__init__.py`
- Create: `tests/unit/test_inmemory_memory.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_inmemory_memory.py`. Critical contract: never returns memory from another assistant.

```python
import pytest

from email_agent.adapters.inmemory.memory import InMemoryMemoryAdapter


@pytest.mark.asyncio
async def test_record_and_recall_round_trip():
    m = InMemoryMemoryAdapter()
    await m.record_turn("a-1", "t-1", "user", "I love bread")
    await m.record_turn("a-1", "t-1", "assistant", "ok")
    ctx = await m.recall("a-1", "t-1", query="bread")
    assert any("bread" in mem.content for mem in ctx.memories)


@pytest.mark.asyncio
async def test_recall_is_scoped_per_assistant():
    m = InMemoryMemoryAdapter()
    await m.record_turn("a-1", "t-1", "user", "secret-A")
    await m.record_turn("a-2", "t-1", "user", "secret-B")
    ctx = await m.recall("a-2", "t-1", query="secret")
    contents = [mem.content for mem in ctx.memories]
    assert "secret-B" in str(contents)
    assert "secret-A" not in str(contents)


@pytest.mark.asyncio
async def test_search_is_scoped_per_assistant():
    m = InMemoryMemoryAdapter()
    await m.record_turn("a-1", "t-1", "user", "alpha bravo")
    await m.record_turn("a-2", "t-1", "user", "alpha charlie")
    hits = await m.search("a-1", "alpha")
    assert all("bravo" in hit.content or "alpha" in hit.content for hit in hits)
    assert not any("charlie" in hit.content for hit in hits)


@pytest.mark.asyncio
async def test_delete_assistant_only_clears_that_assistant():
    m = InMemoryMemoryAdapter()
    await m.record_turn("a-1", "t-1", "user", "keep me?")
    await m.record_turn("a-2", "t-1", "user", "keep me!")
    await m.delete_assistant("a-1")
    a1 = await m.search("a-1", "keep")
    a2 = await m.search("a-2", "keep")
    assert a1 == []
    assert any("keep me!" in m.content for m in a2)
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `uv run pytest tests/unit/test_inmemory_memory.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the adapter**

Create `src/email_agent/adapters/inmemory/memory.py`:

```python
from datetime import UTC, datetime
from itertools import count

from email_agent.models.memory import Memory, MemoryContext


class InMemoryMemoryAdapter:
    """Per-assistant scoped in-memory store. Recall/search are simple substring
    matches — good enough for tests and to enforce the isolation contract."""

    def __init__(self) -> None:
        self._by_assistant: dict[str, list[Memory]] = {}
        self._counter = count(1)

    async def record_turn(
        self, assistant_id: str, thread_id: str, role: str, content: str
    ) -> None:
        bucket = self._by_assistant.setdefault(assistant_id, [])
        bucket.append(
            Memory(
                id=f"mem-{next(self._counter)}",
                content=f"[{thread_id}/{role}] {content}",
            )
        )

    async def recall(
        self, assistant_id: str, thread_id: str, query: str
    ) -> MemoryContext:
        bucket = self._by_assistant.get(assistant_id, [])
        scoped = [m for m in bucket if f"[{thread_id}/" in m.content]
        hits = [m for m in scoped if _matches(m.content, query)]
        return MemoryContext(memories=hits, retrieved_at=datetime.now(UTC))

    async def search(self, assistant_id: str, query: str) -> list[Memory]:
        bucket = self._by_assistant.get(assistant_id, [])
        return [m for m in bucket if _matches(m.content, query)]

    async def delete_assistant(self, assistant_id: str) -> None:
        self._by_assistant.pop(assistant_id, None)


def _matches(content: str, query: str) -> bool:
    return query.lower() in content.lower()
```

- [ ] **Step 4: Update inmemory init**

Replace `src/email_agent/adapters/inmemory/__init__.py`:

```python
from email_agent.adapters.inmemory.email_provider import InMemoryEmailProvider
from email_agent.adapters.inmemory.memory import InMemoryMemoryAdapter

__all__ = ["InMemoryEmailProvider", "InMemoryMemoryAdapter"]
```

- [ ] **Step 5: Run tests to confirm pass**

Run: `uv run pytest tests/unit/test_inmemory_memory.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add src/email_agent/adapters/inmemory/ tests/unit/test_inmemory_memory.py
git commit -m "feat(adapters): add InMemoryMemoryAdapter with per-assistant isolation"
```

---

## Task 9: InMemorySandbox

**Files:**
- Create: `src/email_agent/adapters/inmemory/sandbox.py`
- Modify: `src/email_agent/adapters/inmemory/__init__.py`
- Create: `tests/unit/test_inmemory_sandbox.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_inmemory_sandbox.py`:

```python
import pytest

from email_agent.adapters.inmemory.sandbox import InMemorySandbox
from email_agent.models.sandbox import BashResult, ProjectedFile, ToolCall


@pytest.mark.asyncio
async def test_project_and_read_email_file():
    s = InMemorySandbox()
    await s.ensure_started("a-1")
    await s.project_emails(
        "a-1",
        [ProjectedFile(path="emails/t/0001.md", content=b"hi")],
    )
    result = await s.run_tool(
        "a-1",
        "r-1",
        ToolCall(kind="read", path="/workspace/emails/t/0001.md"),
    )
    assert result.ok
    assert result.output == "hi"


@pytest.mark.asyncio
async def test_write_under_emails_is_rejected():
    s = InMemorySandbox()
    await s.ensure_started("a-1")
    result = await s.run_tool(
        "a-1",
        "r-1",
        ToolCall(kind="write", path="/workspace/emails/x.md", content="x"),
    )
    assert not result.ok
    assert "read-only" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_write_then_read_round_trips():
    s = InMemorySandbox()
    await s.ensure_started("a-1")
    await s.run_tool(
        "a-1", "r-1", ToolCall(kind="write", path="/workspace/notes.md", content="hello")
    )
    out = await s.run_tool("a-1", "r-1", ToolCall(kind="read", path="/workspace/notes.md"))
    assert out.output == "hello"


@pytest.mark.asyncio
async def test_edit_replaces_substring():
    s = InMemorySandbox()
    await s.ensure_started("a-1")
    await s.run_tool(
        "a-1", "r-1", ToolCall(kind="write", path="/workspace/x.md", content="abc def")
    )
    await s.run_tool(
        "a-1", "r-1", ToolCall(kind="edit", path="/workspace/x.md", old="abc", new="ABC")
    )
    out = await s.run_tool("a-1", "r-1", ToolCall(kind="read", path="/workspace/x.md"))
    assert out.output == "ABC def"


@pytest.mark.asyncio
async def test_bash_runs_and_captures_stdout():
    s = InMemorySandbox()
    await s.ensure_started("a-1")
    out = await s.run_tool(
        "a-1", "r-1", ToolCall(kind="bash", command="echo hello")
    )
    assert out.ok
    assert isinstance(out.output, BashResult)
    assert out.output.exit_code == 0
    assert "hello" in out.output.stdout


@pytest.mark.asyncio
async def test_filesystem_is_isolated_per_assistant():
    s = InMemorySandbox()
    await s.ensure_started("a-1")
    await s.ensure_started("a-2")
    await s.run_tool(
        "a-1", "r-1", ToolCall(kind="write", path="/workspace/a.md", content="A")
    )
    out = await s.run_tool(
        "a-2", "r-1", ToolCall(kind="read", path="/workspace/a.md")
    )
    assert not out.ok


@pytest.mark.asyncio
async def test_reset_wipes_workspace():
    s = InMemorySandbox()
    await s.ensure_started("a-1")
    await s.run_tool(
        "a-1", "r-1", ToolCall(kind="write", path="/workspace/x.md", content="x")
    )
    await s.reset("a-1")
    await s.ensure_started("a-1")
    out = await s.run_tool(
        "a-1", "r-1", ToolCall(kind="read", path="/workspace/x.md")
    )
    assert not out.ok


@pytest.mark.asyncio
async def test_attachments_round_trip():
    s = InMemorySandbox()
    await s.ensure_started("a-1")
    await s.project_attachments(
        "a-1", "r-1", [ProjectedFile(path="report.pdf", content=b"%PDF-data")]
    )
    data = await s.read_attachment_out("a-1", "r-1", "report.pdf")
    assert data == b"%PDF-data"
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `uv run pytest tests/unit/test_inmemory_sandbox.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the adapter**

Create `src/email_agent/adapters/inmemory/sandbox.py`:

```python
import shlex
import subprocess
import time
from pathlib import PurePosixPath

from email_agent.models.sandbox import (
    BashResult,
    ProjectedFile,
    ToolCall,
    ToolResult,
)

WORKSPACE_ROOT = "/workspace"
EMAILS_PREFIX = "/workspace/emails/"


class InMemorySandbox:
    """In-process sandbox for tests. Filesystem is a per-assistant dict.
    `bash` runs on the host via subprocess — fine for tests, not for prod."""

    def __init__(self) -> None:
        self._fs: dict[str, dict[str, bytes]] = {}
        self._attachments: dict[tuple[str, str], dict[str, bytes]] = {}
        self._started: set[str] = set()

    async def ensure_started(self, assistant_id: str) -> None:
        self._started.add(assistant_id)
        self._fs.setdefault(assistant_id, {})

    async def project_emails(
        self, assistant_id: str, files: list[ProjectedFile]
    ) -> None:
        self._require_started(assistant_id)
        fs = self._fs[assistant_id]
        for k in list(fs):
            if k.startswith(EMAILS_PREFIX):
                del fs[k]
        for f in files:
            full = self._normalize(f"emails/{_strip_leading(f.path, 'emails/')}")
            fs[full] = f.content

    async def project_attachments(
        self, assistant_id: str, run_id: str, files: list[ProjectedFile]
    ) -> None:
        self._require_started(assistant_id)
        bucket = self._attachments.setdefault((assistant_id, run_id), {})
        for f in files:
            bucket[f.path] = f.content

    async def run_tool(
        self, assistant_id: str, run_id: str, call: ToolCall
    ) -> ToolResult:
        self._require_started(assistant_id)
        fs = self._fs[assistant_id]
        match call.kind:
            case "read":
                path = self._normalize(call.path or "")
                if path not in fs:
                    return ToolResult(ok=False, error=f"not found: {path}")
                return ToolResult(ok=True, output=fs[path].decode())
            case "write":
                path = self._normalize(call.path or "")
                if path.startswith(EMAILS_PREFIX):
                    return ToolResult(
                        ok=False, error=f"{EMAILS_PREFIX} is read-only"
                    )
                fs[path] = (call.content or "").encode()
                return ToolResult(ok=True)
            case "edit":
                path = self._normalize(call.path or "")
                if path.startswith(EMAILS_PREFIX):
                    return ToolResult(
                        ok=False, error=f"{EMAILS_PREFIX} is read-only"
                    )
                if path not in fs:
                    return ToolResult(ok=False, error=f"not found: {path}")
                old, new = call.old or "", call.new or ""
                content = fs[path].decode()
                if old not in content:
                    return ToolResult(ok=False, error="old string not found")
                fs[path] = content.replace(old, new, 1).encode()
                return ToolResult(ok=True)
            case "bash":
                t0 = time.monotonic()
                proc = subprocess.run(
                    shlex.split(call.command or ""),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                dur = int((time.monotonic() - t0) * 1000)
                return ToolResult(
                    ok=proc.returncode == 0,
                    output=BashResult(
                        exit_code=proc.returncode,
                        stdout=proc.stdout,
                        stderr=proc.stderr,
                        duration_ms=dur,
                    ),
                )
            case "attach_file":
                bucket = self._attachments.setdefault((assistant_id, run_id), {})
                path = self._normalize(call.path or "")
                if path not in fs:
                    return ToolResult(ok=False, error=f"not found: {path}")
                fname = call.filename or PurePosixPath(path).name
                bucket[fname] = fs[path]
                return ToolResult(ok=True)

    async def read_attachment_out(
        self, assistant_id: str, run_id: str, path: str
    ) -> bytes:
        return self._attachments[(assistant_id, run_id)][path]

    async def reset(self, assistant_id: str) -> None:
        self._fs.pop(assistant_id, None)
        for key in [k for k in self._attachments if k[0] == assistant_id]:
            self._attachments.pop(key, None)
        self._started.discard(assistant_id)

    def _require_started(self, assistant_id: str) -> None:
        if assistant_id not in self._started:
            raise RuntimeError(f"sandbox for {assistant_id} not started")

    @staticmethod
    def _normalize(path: str) -> str:
        if path.startswith(WORKSPACE_ROOT):
            return path
        return f"{WORKSPACE_ROOT}/{path.lstrip('/')}"


def _strip_leading(s: str, prefix: str) -> str:
    return s[len(prefix):] if s.startswith(prefix) else s
```

- [ ] **Step 4: Update inmemory init**

Replace `src/email_agent/adapters/inmemory/__init__.py`:

```python
from email_agent.adapters.inmemory.email_provider import InMemoryEmailProvider
from email_agent.adapters.inmemory.memory import InMemoryMemoryAdapter
from email_agent.adapters.inmemory.sandbox import InMemorySandbox

__all__ = ["InMemoryEmailProvider", "InMemoryMemoryAdapter", "InMemorySandbox"]
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/unit/test_inmemory_sandbox.py -v`
Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add src/email_agent/adapters/inmemory/sandbox.py src/email_agent/adapters/inmemory/__init__.py tests/unit/test_inmemory_sandbox.py
git commit -m "feat(adapters): add InMemorySandbox with /workspace/emails read-only enforcement"
```

---

## Task 10: SQLAlchemy ORM models

**Files:**
- Create: `src/email_agent/db/__init__.py`
- Create: `src/email_agent/db/base.py`
- Create: `src/email_agent/db/models.py`
- Create: `tests/unit/test_db_models_metadata.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_db_models_metadata.py`:

```python
from email_agent.db.models import Base


def test_expected_tables_are_registered():
    expected = {
        "owners",
        "admins",
        "end_users",
        "assistants",
        "assistant_scopes",
        "email_threads",
        "email_messages",
        "email_attachments",
        "message_index",
        "agent_runs",
        "run_steps",
        "usage_ledger",
        "budgets",
    }
    assert expected.issubset(set(Base.metadata.tables.keys()))


def test_message_index_has_assistant_scope_unique():
    t = Base.metadata.tables["message_index"]
    uniques = {tuple(sorted(c.name for c in u.columns)) for u in t.constraints if u.__class__.__name__ == "UniqueConstraint"}
    assert ("assistant_id", "message_id_header") in uniques
```

- [ ] **Step 2: Run test to confirm failure**

Run: `uv run pytest tests/unit/test_db_models_metadata.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement Base**

Create `src/email_agent/db/__init__.py` (empty for now).

Create `src/email_agent/db/base.py`:

```python
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
```

- [ ] **Step 4: Implement ORM models**

Create `src/email_agent/db/models.py`:

```python
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from email_agent.db.base import Base


def _str_pk() -> Mapped[str]:
    return mapped_column(String(64), primary_key=True)


class Owner(Base):
    __tablename__ = "owners"

    id: Mapped[str] = _str_pk()
    name: Mapped[str] = mapped_column(String(255))
    primary_admin_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    billing_scope: Mapped[str] = mapped_column(String(64), default="self")


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[str] = _str_pk()
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    role: Mapped[str] = mapped_column(String(32), default="admin")


class EndUser(Base):
    __tablename__ = "end_users"

    id: Mapped[str] = _str_pk()
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id"))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Assistant(Base):
    __tablename__ = "assistants"

    id: Mapped[str] = _str_pk()
    end_user_id: Mapped[str] = mapped_column(ForeignKey("end_users.id"))
    inbound_address: Mapped[str] = mapped_column(String(320), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    allowed_senders: Mapped[list[str]] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(64))
    system_prompt: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    scope: Mapped["AssistantScopeRow"] = relationship(
        back_populates="assistant", uselist=False
    )


class AssistantScopeRow(Base):
    __tablename__ = "assistant_scopes"

    assistant_id: Mapped[str] = mapped_column(
        ForeignKey("assistants.id"), primary_key=True
    )
    memory_namespace: Mapped[str] = mapped_column(String(128))
    tool_allowlist: Mapped[list[str]] = mapped_column(JSON, default=list)
    budget_id: Mapped[str] = mapped_column(ForeignKey("budgets.id"))

    assistant: Mapped[Assistant] = relationship(back_populates="scope")


class EmailThread(Base):
    __tablename__ = "email_threads"

    id: Mapped[str] = _str_pk()
    assistant_id: Mapped[str] = mapped_column(ForeignKey("assistants.id"))
    end_user_id: Mapped[str] = mapped_column(ForeignKey("end_users.id"))
    root_message_id: Mapped[str] = mapped_column(String(998))
    subject_normalized: Mapped[str] = mapped_column(String(998))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EmailMessage(Base):
    __tablename__ = "email_messages"

    id: Mapped[str] = _str_pk()
    thread_id: Mapped[str] = mapped_column(ForeignKey("email_threads.id"))
    assistant_id: Mapped[str] = mapped_column(ForeignKey("assistants.id"))
    direction: Mapped[str] = mapped_column(String(16))  # inbound | outbound
    provider_message_id: Mapped[str] = mapped_column(String(255))
    message_id_header: Mapped[str] = mapped_column(String(998))
    in_reply_to_header: Mapped[str | None] = mapped_column(String(998), nullable=True)
    references_headers: Mapped[list[str]] = mapped_column(JSON, default=list)
    from_email: Mapped[str] = mapped_column(String(320))
    to_emails: Mapped[list[str]] = mapped_column(JSON, default=list)
    subject: Mapped[str] = mapped_column(String(998))
    body_text: Mapped[str] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("assistant_id", "provider_message_id"),
    )


class EmailAttachmentRow(Base):
    __tablename__ = "email_attachments"

    id: Mapped[str] = _str_pk()
    message_id: Mapped[str] = mapped_column(ForeignKey("email_messages.id"))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(127))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_path: Mapped[str] = mapped_column(String(1024))


class MessageIndex(Base):
    __tablename__ = "message_index"

    assistant_id: Mapped[str] = mapped_column(
        ForeignKey("assistants.id"), primary_key=True
    )
    message_id_header: Mapped[str] = mapped_column(String(998), primary_key=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("email_threads.id"))
    provider_message_id: Mapped[str] = mapped_column(String(255))

    __table_args__ = (
        UniqueConstraint("assistant_id", "message_id_header"),
    )


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = _str_pk()
    assistant_id: Mapped[str] = mapped_column(ForeignKey("assistants.id"))
    thread_id: Mapped[str] = mapped_column(ForeignKey("email_threads.id"))
    inbound_message_id: Mapped[str] = mapped_column(ForeignKey("email_messages.id"))
    reply_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("email_messages.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RunStep(Base):
    __tablename__ = "run_steps"

    id: Mapped[str] = _str_pk()
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))
    kind: Mapped[str] = mapped_column(String(32))
    input_summary: Mapped[str] = mapped_column(Text)
    output_summary: Mapped[str] = mapped_column(Text)
    cost_cents: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UsageLedger(Base):
    __tablename__ = "usage_ledger"

    id: Mapped[str] = _str_pk()
    assistant_id: Mapped[str] = mapped_column(ForeignKey("assistants.id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cost_cents: Mapped[int] = mapped_column(Integer)
    budget_period: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Budget(Base):
    __tablename__ = "budgets"

    id: Mapped[str] = _str_pk()
    assistant_id: Mapped[str] = mapped_column(ForeignKey("assistants.id"))
    monthly_limit_cents: Mapped[int] = mapped_column(Integer)
    period_starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_resets_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


__all__ = [
    "Admin",
    "AgentRun",
    "Assistant",
    "AssistantScopeRow",
    "Base",
    "Budget",
    "EmailAttachmentRow",
    "EmailMessage",
    "EmailThread",
    "EndUser",
    "MessageIndex",
    "Owner",
    "RunStep",
    "UsageLedger",
]
```

Note: `assistant_scopes.budget_id` references `budgets.id`, but `budgets.assistant_id` references `assistants.id` — a cycle is fine here because both columns are nullable at the schema level via Alembic table creation order, but neither is `nullable=True` here. To keep it simple in this initial migration, we'll create tables in dependency order in Task 11; SQLAlchemy doesn't enforce FK ordering at metadata-definition time.

- [ ] **Step 5: Run test to confirm pass**

Run: `uv run pytest tests/unit/test_db_models_metadata.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add src/email_agent/db/ tests/unit/test_db_models_metadata.py
git commit -m "feat(db): add SQLAlchemy 2.0 ORM models"
```

---

## Task 11: docker-compose Postgres + DB session helpers

**Files:**
- Create: `docker-compose.yml`
- Create: `src/email_agent/db/session.py`
- Modify: `src/email_agent/db/__init__.py`

- [ ] **Step 1: Create docker-compose.yml**

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: email_agent
      POSTGRES_PASSWORD: devpassword
      POSTGRES_DB: email_agent
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U email_agent"]
      interval: 2s
      timeout: 3s
      retries: 20

volumes:
  pgdata:
```

- [ ] **Step 2: Create session helpers**

Create `src/email_agent/db/session.py`:

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from email_agent.config import Settings


def make_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(str(settings.database_url), future=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

- [ ] **Step 3: Update db/__init__.py**

Replace `src/email_agent/db/__init__.py`:

```python
from email_agent.db.base import Base
from email_agent.db.session import (
    make_engine,
    make_session_factory,
    session_scope,
)

__all__ = ["Base", "make_engine", "make_session_factory", "session_scope"]
```

- [ ] **Step 4: Bring up Postgres**

Run: `docker compose up -d postgres`
Expected: container `email-assistant-postgres-1` (or similar) running.

Run: `docker compose ps`
Expected: postgres shows `(healthy)` after a few seconds.

- [ ] **Step 5: Smoke-test the engine**

Run:
```bash
DATABASE_URL='postgresql+asyncpg://email_agent:devpassword@localhost:5432/email_agent' \
MAILGUN_SIGNING_KEY=x MAILGUN_API_KEY=x MAILGUN_DOMAIN=x MAILGUN_WEBHOOK_URL=https://x \
DEEPSEEK_API_KEY=x COGNEE_LLM_API_KEY=x COGNEE_EMBEDDING_API_KEY=x \
uv run python -c "
import asyncio
from email_agent.config import Settings
from email_agent.db.session import make_engine
from sqlalchemy import text

async def main():
    e = make_engine(Settings())
    async with e.connect() as conn:
        r = await conn.execute(text('SELECT 1'))
        print(r.scalar())

asyncio.run(main())
"
```
Expected: prints `1`.

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml src/email_agent/db/session.py src/email_agent/db/__init__.py
git commit -m "feat(db): add docker-compose Postgres and async session helpers"
```

---

## Task 12: Alembic setup + initial migration

**Files:**
- Create: `alembic.ini`
- Create: `src/email_agent/db/migrations/env.py`
- Create: `src/email_agent/db/migrations/script.py.mako`
- Create: `src/email_agent/db/migrations/versions/0001_initial.py`
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/test_alembic_upgrade.py`

- [ ] **Step 1: Create alembic.ini**

```ini
[alembic]
script_location = src/email_agent/db/migrations
prepend_sys_path = src
file_template = %%(year)04d_%%(month)02d_%%(day)02d_%%(rev)s_%%(slug)s

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

- [ ] **Step 2: Create migrations env.py**

Create `src/email_agent/db/migrations/env.py`:

```python
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from email_agent.config import Settings
from email_agent.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return str(Settings().database_url)


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url(), poolclass=pool.NullPool, future=True)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
```

- [ ] **Step 3: Create script template**

Create `src/email_agent/db/migrations/script.py.mako`:

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

- [ ] **Step 4: Generate the initial migration via autogenerate**

First make sure Postgres is up: `docker compose up -d postgres`

Run with env vars:
```bash
DATABASE_URL='postgresql+asyncpg://email_agent:devpassword@localhost:5432/email_agent' \
MAILGUN_SIGNING_KEY=x MAILGUN_API_KEY=x MAILGUN_DOMAIN=x MAILGUN_WEBHOOK_URL=https://x \
DEEPSEEK_API_KEY=x COGNEE_LLM_API_KEY=x COGNEE_EMBEDDING_API_KEY=x \
uv run alembic revision --autogenerate -m "initial schema"
```
Expected: writes `src/email_agent/db/migrations/versions/<datestamp>_<slug>_initial_schema.py`.

- [ ] **Step 5: Rename + sanity-check the generated file**

Rename it to `0001_initial.py` for stability. Inspect the file: confirm it creates all 13 tables (`owners`, `admins`, `end_users`, `assistants`, `assistant_scopes`, `email_threads`, `email_messages`, `email_attachments`, `message_index`, `agent_runs`, `run_steps`, `usage_ledger`, `budgets`).

If the generated file uses `down_revision = None` and our naming convention isn't reflected, that's expected for the first migration. Update `revision = "0001"` and `down_revision = None` at the top so subsequent migrations can chain off a stable id.

- [ ] **Step 6: Apply the migration**

Run:
```bash
DATABASE_URL='postgresql+asyncpg://email_agent:devpassword@localhost:5432/email_agent' \
MAILGUN_SIGNING_KEY=x MAILGUN_API_KEY=x MAILGUN_DOMAIN=x MAILGUN_WEBHOOK_URL=https://x \
DEEPSEEK_API_KEY=x COGNEE_LLM_API_KEY=x COGNEE_EMBEDDING_API_KEY=x \
uv run alembic upgrade head
```
Expected: `INFO  [alembic.runtime.migration] Running upgrade -> 0001, initial schema`.

- [ ] **Step 7: Write the integration test**

Create `tests/integration/__init__.py` (empty).

Create `tests/integration/test_alembic_upgrade.py`:

```python
import os

import pytest
from sqlalchemy import text

from email_agent.config import Settings
from email_agent.db.session import make_engine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _have_db() -> bool:
    return "DATABASE_URL" in os.environ


@pytest.mark.skipif(not _have_db(), reason="DATABASE_URL not set")
async def test_alembic_upgrade_head_creates_expected_tables():
    engine = make_engine(Settings())
    expected = {
        "owners",
        "admins",
        "end_users",
        "assistants",
        "assistant_scopes",
        "email_threads",
        "email_messages",
        "email_attachments",
        "message_index",
        "agent_runs",
        "run_steps",
        "usage_ledger",
        "budgets",
        "alembic_version",
    }
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        )
        present = {r[0] for r in rows}
    await engine.dispose()
    missing = expected - present
    assert not missing, f"missing tables: {missing}"
```

- [ ] **Step 8: Run the integration test**

Run with the same env vars exported:
```bash
export DATABASE_URL='postgresql+asyncpg://email_agent:devpassword@localhost:5432/email_agent'
export MAILGUN_SIGNING_KEY=x MAILGUN_API_KEY=x MAILGUN_DOMAIN=x MAILGUN_WEBHOOK_URL=https://x
export DEEPSEEK_API_KEY=x COGNEE_LLM_API_KEY=x COGNEE_EMBEDDING_API_KEY=x
uv run pytest tests/integration/test_alembic_upgrade.py -v -m integration
```
Expected: 1 passed.

- [ ] **Step 9: Commit**

```bash
git add alembic.ini src/email_agent/db/migrations/ tests/integration/
git commit -m "feat(db): add Alembic with initial schema migration"
```

---

## Task 13: DB round-trip integration test

**Files:**
- Create: `tests/integration/test_db_roundtrip.py`

- [ ] **Step 1: Write the failing test**

```python
import os
import uuid

import pytest

from email_agent.config import Settings
from email_agent.db.models import Assistant, EndUser, Owner
from email_agent.db.session import make_engine, make_session_factory, session_scope

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="needs db")
async def test_insert_and_query_assistant():
    engine = make_engine(Settings())
    factory = make_session_factory(engine)

    owner_id = f"o-{uuid.uuid4().hex[:8]}"
    user_id = f"u-{uuid.uuid4().hex[:8]}"
    asst_id = f"a-{uuid.uuid4().hex[:8]}"

    async with session_scope(factory) as s:
        s.add_all([
            Owner(id=owner_id, name="Larry"),
            EndUser(id=user_id, owner_id=owner_id, email=f"{user_id}@example.com"),
            Assistant(
                id=asst_id,
                end_user_id=user_id,
                inbound_address=f"{asst_id}@example.com",
                model="deepseek-flash",
                system_prompt="be kind",
            ),
        ])

    async with session_scope(factory) as s:
        got = await s.get(Assistant, asst_id)
        assert got is not None
        assert got.inbound_address == f"{asst_id}@example.com"

    await engine.dispose()
```

- [ ] **Step 2: Run the test to confirm pass**

(With the same env vars exported as in Task 12 Step 8.)

Run: `uv run pytest tests/integration/test_db_roundtrip.py -v -m integration`
Expected: 1 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_db_roundtrip.py
git commit -m "test(db): add round-trip integration test for Assistant ORM"
```

---

## Task 14: Typer CLI skeleton with `migrate`

**Files:**
- Create: `src/email_agent/cli.py`
- Create: `tests/unit/test_cli.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_cli.py`:

```python
from typer.testing import CliRunner

from email_agent.cli import app

runner = CliRunner()


def test_app_help_lists_expected_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("migrate", "hello"):
        assert cmd in result.stdout


def test_hello_prints_greeting():
    result = runner.invoke(app, ["hello", "--name", "Mum"])
    assert result.exit_code == 0
    assert "Mum" in result.stdout
```

- [ ] **Step 2: Run test to confirm failure**

Run: `uv run pytest tests/unit/test_cli.py -v`
Expected: ImportError on `email_agent.cli`.

- [ ] **Step 3: Implement the CLI**

Create `src/email_agent/cli.py`:

```python
import subprocess
import sys

import typer

app = typer.Typer(help="Email Assistant operator CLI", no_args_is_help=True)


@app.command()
def hello(name: str = typer.Option("world", help="Who to greet")) -> None:
    """Smoke command — confirms the CLI is wired."""
    typer.echo(f"hello, {name}")


@app.command()
def migrate() -> None:
    """Run `alembic upgrade head`."""
    code = subprocess.call([sys.executable, "-m", "alembic", "upgrade", "head"])
    raise typer.Exit(code)


if __name__ == "__main__":
    app()
```

- [ ] **Step 4: Run test to confirm pass**

Run: `uv run pytest tests/unit/test_cli.py -v`
Expected: 2 passed.

- [ ] **Step 5: Smoke-test the script entry point**

Run: `uv run email-agent hello --name Larry`
Expected: prints `hello, Larry`.

- [ ] **Step 6: Commit**

```bash
git add src/email_agent/cli.py tests/unit/test_cli.py
git commit -m "feat(cli): add typer skeleton with migrate and hello commands"
```

---

## Task 15: Final sweep

- [ ] **Step 1: Run the full unit suite**

Run: `uv run pytest tests/unit -v`
Expected: all green.

- [ ] **Step 2: Run the full integration suite**

(Postgres up, env vars exported as in Task 12 Step 8.)

Run: `uv run pytest tests/integration -v -m integration`
Expected: all green.

- [ ] **Step 3: Lint**

Run: `uv run ruff check src tests`
Expected: no errors. If anything, fix in place.

- [ ] **Step 4: Format check**

Run: `uv run ruff format --check src tests`
Expected: would-not-reformat. If reformats are needed, run `uv run ruff format src tests` and commit.

- [ ] **Step 5: Verify Protocol conformance for all in-memory adapters**

Run:
```bash
uv run python -c "
from email_agent.ports import EmailProvider, MemoryPort, AssistantSandbox
from email_agent.adapters.inmemory import (
    InMemoryEmailProvider, InMemoryMemoryAdapter, InMemorySandbox,
)
assert isinstance(InMemoryEmailProvider(), EmailProvider)
assert isinstance(InMemoryMemoryAdapter(), MemoryPort)
assert isinstance(InMemorySandbox(), AssistantSandbox)
print('all adapters conform')
"
```
Expected: prints `all adapters conform`.

- [ ] **Step 6: Tag the slice**

```bash
git tag -a slice-1-complete -m "Slice 1: core data + ports + in-memory adapters + Postgres schema"
```

(Don't push the tag automatically — leave that to the operator.)

---

## Self-review notes

**Spec coverage for Slice 1** (per the spec's "normalized email models, port protocols, Postgres schema (Alembic), in-memory adapters" + the user-confirmed additions of Settings, CLI skeleton, and docker-compose):

| Spec item | Covered by |
| --- | --- |
| `NormalizedInboundEmail` / outbound / attachments / SentEmail | Task 4 |
| `AssistantScope` + `allowed_senders` / status | Task 5 |
| `Memory`, `MemoryContext` | Task 5 |
| `ToolCall`, `ToolResult`, `BashResult`, `ProjectedFile`, `PendingAttachment` | Task 5 |
| `EmailProvider` Protocol | Task 6 |
| `MemoryPort` Protocol | Task 6 |
| `AssistantSandbox` Protocol | Task 6 |
| In-memory `EmailProvider` (test adapter) | Task 7 |
| In-memory `MemoryAdapter` with isolation invariant | Task 8 |
| In-memory `Sandbox` with `/workspace/emails/` read-only enforcement | Task 9 |
| All Postgres tables from spec data model | Tasks 10, 12 |
| Alembic-managed schema | Task 12 |
| `(assistant_id, provider_message_id)` uniqueness | Task 10 (`EmailMessage` constraint) |
| `(assistant_id, message_id_header)` uniqueness for `message_index` | Task 10 |
| `pydantic-settings` Settings | Task 3 |
| `typer` CLI with `migrate` | Task 14 |
| docker-compose Postgres | Task 11 |

Items intentionally **not** in this slice (deferred to later slices per the user's choice):
- structured JSON logging
- GitHub Actions CI
- web/admin/Mailgun/Cognee/Docker-sandbox/PydanticAI/Procrastinate (later slices)

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-10-slice-1-core-data-and-ports.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
