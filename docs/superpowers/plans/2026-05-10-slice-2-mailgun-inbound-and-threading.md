# Slice 2 — Mailgun Inbound + Threading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept inbound Mailgun webhooks, normalize them, route to the correct assistant, resolve their email thread, and persist the inbound message + thread index — stopping before any agent execution.

**Architecture:** Adds a real `MailgunEmailProvider` adapter alongside the existing `InMemoryEmailProvider`, and three new domain modules under `src/email_agent/domain/`: `AssistantRouter`, `ThreadResolver`, and an `inbound_persister` function. These are the pieces the webhook fast path described in the design will compose; the HTTP entrypoint and `AssistantRuntime.accept_inbound` orchestrator are deferred to slice 5.

**Tech Stack:** Python 3.13, pydantic v2, SQLAlchemy 2.0 async, pytest-asyncio, hmac/hashlib (stdlib) for Mailgun signature verification, ULID-style ids via `uuid.uuid4().hex[:N]` (matches slice 1 fixture pattern).

**Out of scope (slices 3–8):** sending replies (`send_reply` stays a stub here), budget gating, sandbox, agent execution, agent_runs row creation, Procrastinate enqueue, FastAPI HTTP route, admin UI.

---

## File Structure

**Create:**
- `src/email_agent/mail/mailgun.py` — `MailgunEmailProvider` adapter.
- `src/email_agent/domain/__init__.py` — empty package marker.
- `src/email_agent/domain/router.py` — `AssistantRouter` + `RouteOutcome` types.
- `src/email_agent/domain/thread_resolver.py` — `ThreadResolver`.
- `src/email_agent/domain/inbound_persister.py` — `persist_inbound` function.
- `tests/unit/test_mailgun_provider.py` — unit tests for verify + parse.
- `tests/unit/test_assistant_router.py` — unit tests for routing rejections (uses sqlite in-memory).
- `tests/unit/test_thread_resolver.py` — unit tests for resolution rules (sqlite in-memory).
- `tests/unit/test_inbound_persister.py` — unit tests for persistence + idempotency (sqlite in-memory).
- `tests/integration/test_inbound_pipeline.py` — round-trip test using the in-memory adapters + a real Postgres session.
- `tests/fixtures/mailgun/` — sample Mailgun form payloads (`.json` files) used by the unit tests.

**Modify:**
- `src/email_agent/mail/__init__.py` — export `MailgunEmailProvider`.
- `src/email_agent/models/assistant.py` — add `AssistantScope.from_rows` classmethod that joins `Assistant + AssistantScopeRow` to build the wire model.

**Why split into four domain modules instead of one:** each has a single responsibility from the design (route, resolve thread, persist) and lives behind its own seam in the webhook fast path. Tests target each interface directly, and slice 5's `AssistantRuntime.accept_inbound` will compose them without further restructuring.

---

## Conventions used in this plan

- Tests use `pytest-asyncio` auto mode (already configured in `pyproject.toml`); no `@pytest.mark.asyncio` decorator needed.
- Unit tests for SQLAlchemy code use an **in-process sqlite+aiosqlite** engine to keep the unit suite fast and offline. The integration test uses real Postgres via `session_scope`.
- ID generation in tests uses `uuid.uuid4().hex[:8]` with a per-entity prefix (`a-`, `t-`, `m-`, …) — matches slice 1 fixtures.
- Every commit message follows the `<type>(<scope>): <subject>` style already in the log (`feat(db):`, `test(db):`, etc.).

---

## Task 0: Add aiosqlite dev dependency for fast unit tests

**Files:** `pyproject.toml` (auto-edited by uv).

- [ ] **Step 1: Install aiosqlite as a dev dep**

Run:

```bash
uv add --dev aiosqlite
```

Expected: `pyproject.toml` `[dependency-groups].dev` gains `aiosqlite>=…`; `uv.lock` updated.

- [ ] **Step 2: Verify it imports**

Run:

```bash
uv run python -c "import aiosqlite; print(aiosqlite.__version__)"
```

Expected: a version string printed.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore(deps): add aiosqlite for fast async unit tests"
```

---

## Task 1: Shared sqlite test fixture

**Files:**
- Create: `tests/conftest.py` (currently empty).

A reusable `sqlite_session_factory` fixture so router/resolver/persister tests share one setup pattern.

- [ ] **Step 1: Write the fixture**

Replace the contents of `tests/conftest.py` with:

```python
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from email_agent.db.base import Base
from email_agent.db import models  # noqa: F401  # registers ORM tables on Base.metadata


@pytest.fixture
async def sqlite_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def sqlite_session_factory(
    sqlite_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(sqlite_engine, expire_on_commit=False)
```

- [ ] **Step 2: Smoke-test the fixture**

Create a tiny throwaway test in the same file body? No — instead verify by running the existing suite, which should still pass.

Run:

```bash
uv run pytest tests/unit -q
```

Expected: PASS, no collection errors.

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "test: add sqlite async session factory fixture"
```

---

## Task 2: AssistantScope.from_rows mapper

**Files:**
- Modify: `src/email_agent/models/assistant.py`
- Test: `tests/unit/test_domain_models.py` (extend)

`AssistantRouter.resolve` needs to flatten three ORM rows into the wire `AssistantScope`. Centralize that mapping on the model where it belongs.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_domain_models.py`:

```python
from email_agent.db.models import Assistant as AssistantRow
from email_agent.db.models import AssistantScopeRow, Budget, EndUser, Owner
from email_agent.models.assistant import AssistantScope, AssistantStatus


def test_assistant_scope_from_rows_flattens_three_rows():
    owner = Owner(id="o-1", name="Larry", primary_admin_id=None)
    end_user = EndUser(id="u-1", owner_id="o-1", email="mum@example.com")
    assistant = AssistantRow(
        id="a-1",
        end_user_id="u-1",
        inbound_address="mum@assistants.example.com",
        status="active",
        allowed_senders=["mum@example.com"],
        model="deepseek-flash",
        system_prompt="be kind",
    )
    scope_row = AssistantScopeRow(
        assistant_id="a-1",
        memory_namespace="mum",
        tool_allowlist=["read", "write"],
        budget_id="b-1",
    )

    scope = AssistantScope.from_rows(
        owner=owner,
        end_user=end_user,
        assistant=assistant,
        scope_row=scope_row,
    )

    assert scope.assistant_id == "a-1"
    assert scope.owner_id == "o-1"
    assert scope.end_user_id == "u-1"
    assert scope.inbound_address == "mum@assistants.example.com"
    assert scope.status is AssistantStatus.ACTIVE
    assert scope.allowed_senders == ("mum@example.com",)
    assert scope.memory_namespace == "mum"
    assert scope.tool_allowlist == ("read", "write")
    assert scope.budget_id == "b-1"
    assert scope.model_name == "deepseek-flash"
    assert scope.system_prompt == "be kind"
```

- [ ] **Step 2: Run to verify failure**

Run:

```bash
uv run pytest tests/unit/test_domain_models.py::test_assistant_scope_from_rows_flattens_three_rows -v
```

Expected: FAIL — `AttributeError: type object 'AssistantScope' has no attribute 'from_rows'`.

- [ ] **Step 3: Implement the classmethod**

Append to `src/email_agent/models/assistant.py`:

```python
    @classmethod
    def from_rows(
        cls,
        *,
        owner: "Owner",
        end_user: "EndUser",
        assistant: "AssistantRow",
        scope_row: "AssistantScopeRow",
    ) -> "AssistantScope":
        """Flatten the three persisted rows into the wire-side scope.

        Lives on the wire model because that's the side that owns the shape;
        the ORM is a dumb storage representation.
        """
        return cls(
            assistant_id=assistant.id,
            owner_id=owner.id,
            end_user_id=end_user.id,
            inbound_address=assistant.inbound_address,
            status=AssistantStatus(assistant.status),
            allowed_senders=tuple(assistant.allowed_senders),
            memory_namespace=scope_row.memory_namespace,
            tool_allowlist=tuple(scope_row.tool_allowlist),
            budget_id=scope_row.budget_id,
            model_name=assistant.model,
            system_prompt=assistant.system_prompt,
        )
```

Add the imports at the top of the file (under `from enum import StrEnum`):

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from email_agent.db.models import Assistant as AssistantRow
    from email_agent.db.models import AssistantScopeRow, EndUser, Owner
```

- [ ] **Step 4: Run to verify pass**

Run:

```bash
uv run pytest tests/unit/test_domain_models.py -v
```

Expected: PASS, all tests including the new one.

- [ ] **Step 5: Commit**

```bash
git add src/email_agent/models/assistant.py tests/unit/test_domain_models.py
git commit -m "feat(models): add AssistantScope.from_rows mapper"
```

---

## Task 3: AssistantRouter — happy path resolves inbound address

**Files:**
- Create: `src/email_agent/domain/__init__.py`
- Create: `src/email_agent/domain/router.py`
- Create: `tests/unit/test_assistant_router.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_assistant_router.py`:

```python
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import (
    Assistant,
    AssistantScopeRow,
    Budget,
    EndUser,
    Owner,
)
from email_agent.domain.router import AssistantRouter, Routed
from email_agent.models.assistant import AssistantStatus
from email_agent.models.email import NormalizedInboundEmail


async def _seed_assistant(
    session: AsyncSession,
    *,
    inbound_address: str = "mum@assistants.example.com",
    status: str = "active",
    allowed_senders: list[str] | None = None,
) -> None:
    if allowed_senders is None:
        allowed_senders = ["mum@example.com"]
    session.add(Owner(id="o-1", name="Larry"))
    session.add(EndUser(id="u-1", owner_id="o-1", email="mum@example.com"))
    session.add(
        Budget(
            id="b-1",
            assistant_id="a-1",
            monthly_limit_cents=1000,
            period_starts_at=datetime(2026, 5, 1, tzinfo=UTC),
            period_resets_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
    )
    session.add(
        Assistant(
            id="a-1",
            end_user_id="u-1",
            inbound_address=inbound_address,
            status=status,
            allowed_senders=allowed_senders,
            model="deepseek-flash",
            system_prompt="be kind",
        )
    )
    session.add(
        AssistantScopeRow(
            assistant_id="a-1",
            memory_namespace="mum",
            tool_allowlist=["read"],
            budget_id="b-1",
        )
    )
    await session.commit()


def _inbound(
    *,
    to: str = "mum@assistants.example.com",
    sender: str = "mum@example.com",
) -> NormalizedInboundEmail:
    return NormalizedInboundEmail(
        provider_message_id="prov-1",
        message_id_header="<msg-1@example.com>",
        from_email=sender,
        to_emails=[to],
        subject="hi",
        body_text="hello",
        received_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
    )


async def test_router_resolves_known_address_to_assistant_scope(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    async with sqlite_session_factory() as session:
        await _seed_assistant(session)

    router = AssistantRouter(sqlite_session_factory)
    outcome = await router.resolve(_inbound())

    assert isinstance(outcome, Routed)
    assert outcome.scope.assistant_id == "a-1"
    assert outcome.scope.status is AssistantStatus.ACTIVE
    assert outcome.scope.allowed_senders == ("mum@example.com",)
```

- [ ] **Step 2: Run to verify failure**

Run:

```bash
uv run pytest tests/unit/test_assistant_router.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'email_agent.domain'`.

- [ ] **Step 3: Implement the router (happy path)**

Create `src/email_agent/domain/__init__.py` as an empty file.

Create `src/email_agent/domain/router.py`:

```python
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import (
    Assistant,
    AssistantScopeRow,
    EndUser,
    Owner,
)
from email_agent.models.assistant import AssistantScope
from email_agent.models.email import NormalizedInboundEmail


class RouteRejectionReason(StrEnum):
    """Why an inbound email was dropped before reaching the agent."""

    UNKNOWN_ADDRESS = "unknown_address"
    ASSISTANT_PAUSED = "assistant_paused"
    ASSISTANT_DISABLED = "assistant_disabled"
    SENDER_NOT_ALLOWED = "sender_not_allowed"


@dataclass(frozen=True)
class Routed:
    """Successful route — the inbound belongs to this assistant."""

    scope: AssistantScope


@dataclass(frozen=True)
class RouteRejection:
    """Inbound dropped before any DB writes for the agent run."""

    reason: RouteRejectionReason
    detail: str


RouteOutcome = Routed | RouteRejection


class AssistantRouter:
    """Resolves an inbound email's `to` address to an `AssistantScope`.

    Drops unknown addresses, paused/disabled assistants, and senders not in
    the assistant's allowlist. Owns its own session because routing happens
    at the very edge of the webhook fast path, before any orchestrator.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def resolve(self, email: NormalizedInboundEmail) -> RouteOutcome:
        async with self._session_factory() as session:
            return await self._resolve(session, email)

    async def _resolve(
        self, session: AsyncSession, email: NormalizedInboundEmail
    ) -> RouteOutcome:
        for to_address in email.to_emails:
            scope = await self._lookup(session, to_address)
            if scope is not None:
                return Routed(scope=scope)
        return RouteRejection(
            reason=RouteRejectionReason.UNKNOWN_ADDRESS,
            detail=f"no assistant for {email.to_emails}",
        )

    async def _lookup(
        self, session: AsyncSession, inbound_address: str
    ) -> AssistantScope | None:
        stmt = (
            select(Owner, EndUser, Assistant, AssistantScopeRow)
            .join(EndUser, EndUser.owner_id == Owner.id)
            .join(Assistant, Assistant.end_user_id == EndUser.id)
            .join(AssistantScopeRow, AssistantScopeRow.assistant_id == Assistant.id)
            .where(Assistant.inbound_address == inbound_address)
        )
        row = (await session.execute(stmt)).first()
        if row is None:
            return None
        owner, end_user, assistant, scope_row = row
        return AssistantScope.from_rows(
            owner=owner,
            end_user=end_user,
            assistant=assistant,
            scope_row=scope_row,
        )
```

- [ ] **Step 4: Run to verify pass**

Run:

```bash
uv run pytest tests/unit/test_assistant_router.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/email_agent/domain/__init__.py src/email_agent/domain/router.py tests/unit/test_assistant_router.py
git commit -m "feat(domain): add AssistantRouter happy-path resolution"
```

---

## Task 4: AssistantRouter — reject unknown address, paused, disabled

**Files:**
- Modify: `src/email_agent/domain/router.py`
- Modify: `tests/unit/test_assistant_router.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/unit/test_assistant_router.py`:

```python
async def test_router_rejects_unknown_address(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    router = AssistantRouter(sqlite_session_factory)
    outcome = await router.resolve(_inbound(to="who-dis@example.com"))

    assert isinstance(outcome, RouteRejection)
    assert outcome.reason is RouteRejectionReason.UNKNOWN_ADDRESS


async def test_router_rejects_paused_assistant(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    async with sqlite_session_factory() as session:
        await _seed_assistant(session, status="paused")

    router = AssistantRouter(sqlite_session_factory)
    outcome = await router.resolve(_inbound())

    assert isinstance(outcome, RouteRejection)
    assert outcome.reason is RouteRejectionReason.ASSISTANT_PAUSED


async def test_router_rejects_disabled_assistant(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    async with sqlite_session_factory() as session:
        await _seed_assistant(session, status="disabled")

    router = AssistantRouter(sqlite_session_factory)
    outcome = await router.resolve(_inbound())

    assert isinstance(outcome, RouteRejection)
    assert outcome.reason is RouteRejectionReason.ASSISTANT_DISABLED
```

Add `RouteRejection, RouteRejectionReason` to the import at the top:

```python
from email_agent.domain.router import (
    AssistantRouter,
    RouteRejection,
    RouteRejectionReason,
    Routed,
)
```

- [ ] **Step 2: Run to verify failure**

Run:

```bash
uv run pytest tests/unit/test_assistant_router.py -v
```

Expected: 1 PASS (happy path), 3 FAIL (paused/disabled treated as Routed; unknown already returns rejection so that one may pass — verify which fail).

- [ ] **Step 3: Add status check in `_resolve`**

In `src/email_agent/domain/router.py`, replace the body of `_resolve` with:

```python
    async def _resolve(
        self, session: AsyncSession, email: NormalizedInboundEmail
    ) -> RouteOutcome:
        for to_address in email.to_emails:
            scope = await self._lookup(session, to_address)
            if scope is None:
                continue
            if scope.status is AssistantStatus.PAUSED:
                return RouteRejection(
                    reason=RouteRejectionReason.ASSISTANT_PAUSED,
                    detail=f"assistant {scope.assistant_id} is paused",
                )
            if scope.status is AssistantStatus.DISABLED:
                return RouteRejection(
                    reason=RouteRejectionReason.ASSISTANT_DISABLED,
                    detail=f"assistant {scope.assistant_id} is disabled",
                )
            return Routed(scope=scope)
        return RouteRejection(
            reason=RouteRejectionReason.UNKNOWN_ADDRESS,
            detail=f"no assistant for {email.to_emails}",
        )
```

Add the import at the top of `router.py`:

```python
from email_agent.models.assistant import AssistantScope, AssistantStatus
```

- [ ] **Step 4: Run to verify pass**

Run:

```bash
uv run pytest tests/unit/test_assistant_router.py -v
```

Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/email_agent/domain/router.py tests/unit/test_assistant_router.py
git commit -m "feat(domain): reject paused/disabled/unknown in AssistantRouter"
```

---

## Task 5: AssistantRouter — reject sender not in allowlist

**Files:**
- Modify: `src/email_agent/domain/router.py`
- Modify: `tests/unit/test_assistant_router.py`

- [ ] **Step 1: Add failing test**

Append to `tests/unit/test_assistant_router.py`:

```python
async def test_router_rejects_sender_not_in_allowlist(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    async with sqlite_session_factory() as session:
        await _seed_assistant(session, allowed_senders=["mum@example.com"])

    router = AssistantRouter(sqlite_session_factory)
    outcome = await router.resolve(_inbound(sender="randoms@example.com"))

    assert isinstance(outcome, RouteRejection)
    assert outcome.reason is RouteRejectionReason.SENDER_NOT_ALLOWED


async def test_router_accepts_allowlisted_sender_case_insensitively(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    async with sqlite_session_factory() as session:
        await _seed_assistant(session, allowed_senders=["Mum@Example.com"])

    router = AssistantRouter(sqlite_session_factory)
    outcome = await router.resolve(_inbound(sender="MUM@example.COM"))

    assert isinstance(outcome, Routed)
```

- [ ] **Step 2: Run to verify failure**

Run:

```bash
uv run pytest tests/unit/test_assistant_router.py -v
```

Expected: the `not_in_allowlist` case fails (returns `Routed`); the case-insensitive case should already pass via existing `is_sender_allowed`.

- [ ] **Step 3: Implement the allowlist gate**

In `_resolve`, add the sender check before returning `Routed(scope)`:

```python
            if not scope.is_sender_allowed(email.from_email):
                return RouteRejection(
                    reason=RouteRejectionReason.SENDER_NOT_ALLOWED,
                    detail=f"{email.from_email} not in allowlist for {scope.assistant_id}",
                )
            return Routed(scope=scope)
```

(Insert just before the existing `return Routed(scope=scope)` line.)

- [ ] **Step 4: Run to verify pass**

Run:

```bash
uv run pytest tests/unit/test_assistant_router.py -v
```

Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/email_agent/domain/router.py tests/unit/test_assistant_router.py
git commit -m "feat(domain): reject senders missing from assistant allowlist"
```

---

## Task 6: ThreadResolver — creates new thread when no headers match

**Files:**
- Create: `src/email_agent/domain/thread_resolver.py`
- Create: `tests/unit/test_thread_resolver.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_thread_resolver.py`:

```python
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import EmailThread
from email_agent.domain.thread_resolver import ThreadResolver
from email_agent.models.assistant import AssistantScope, AssistantStatus
from email_agent.models.email import NormalizedInboundEmail


def _scope(assistant_id: str = "a-1", end_user_id: str = "u-1") -> AssistantScope:
    return AssistantScope(
        assistant_id=assistant_id,
        owner_id="o-1",
        end_user_id=end_user_id,
        inbound_address=f"{assistant_id}@assistants.example.com",
        status=AssistantStatus.ACTIVE,
        allowed_senders=("mum@example.com",),
        memory_namespace="mum",
        tool_allowlist=("read",),
        budget_id="b-1",
        model_name="deepseek-flash",
        system_prompt="be kind",
    )


def _inbound(
    *,
    message_id: str = "<m-new@example.com>",
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    subject: str = "hi",
) -> NormalizedInboundEmail:
    return NormalizedInboundEmail(
        provider_message_id=message_id.strip("<>"),
        message_id_header=message_id,
        in_reply_to_header=in_reply_to,
        references_headers=references or [],
        from_email="mum@example.com",
        to_emails=["a-1@assistants.example.com"],
        subject=subject,
        body_text="body",
        received_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
    )


async def test_resolver_creates_new_thread_when_no_headers_match(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    resolver = ThreadResolver(sqlite_session_factory)
    thread = await resolver.resolve(_inbound(), _scope())

    assert thread.assistant_id == "a-1"
    assert thread.subject_normalized == "hi"
    assert thread.root_message_id == "<m-new@example.com>"

    async with sqlite_session_factory() as session:
        rows = (await session.execute(select(EmailThread))).scalars().all()
        assert len(rows) == 1
        assert rows[0].id == thread.id
```

- [ ] **Step 2: Run to verify failure**

Run:

```bash
uv run pytest tests/unit/test_thread_resolver.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement create-new-thread path**

Create `src/email_agent/domain/thread_resolver.py`:

```python
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import EmailThread, MessageIndex
from email_agent.models.assistant import AssistantScope
from email_agent.models.email import NormalizedInboundEmail

_RE_PREFIX = re.compile(r"^\s*(re|fwd?):\s*", re.IGNORECASE)


def _normalize_subject(subject: str) -> str:
    """Strip leading Re:/Fwd: noise so replies group under the same subject."""
    prev = None
    current = subject.strip()
    while current != prev:
        prev = current
        current = _RE_PREFIX.sub("", current).strip()
    return current


class ThreadResolver:
    """Maps an inbound email to its `EmailThread` row.

    Resolution order, all scoped by `assistant_id`:
    1. `In-Reply-To` header → `message_index`.
    2. `References` headers → `message_index` (last-to-first).
    3. Otherwise create a new thread.

    Cross-assistant lookups never match — the unique key is
    `(assistant_id, message_id_header)`.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def resolve(
        self, email: NormalizedInboundEmail, scope: AssistantScope
    ) -> EmailThread:
        async with self._session_factory() as session:
            thread = await self._resolve(session, email, scope)
            await session.commit()
            return thread

    async def _resolve(
        self,
        session: AsyncSession,
        email: NormalizedInboundEmail,
        scope: AssistantScope,
    ) -> EmailThread:
        return await self._create_new_thread(session, email, scope)

    async def _create_new_thread(
        self,
        session: AsyncSession,
        email: NormalizedInboundEmail,
        scope: AssistantScope,
    ) -> EmailThread:
        thread = EmailThread(
            id=f"t-{uuid.uuid4().hex[:12]}",
            assistant_id=scope.assistant_id,
            end_user_id=scope.end_user_id,
            root_message_id=email.message_id_header,
            subject_normalized=_normalize_subject(email.subject),
        )
        session.add(thread)
        await session.flush()
        return thread
```

- [ ] **Step 4: Run to verify pass**

Run:

```bash
uv run pytest tests/unit/test_thread_resolver.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/email_agent/domain/thread_resolver.py tests/unit/test_thread_resolver.py
git commit -m "feat(domain): add ThreadResolver new-thread path"
```

---

## Task 7: ThreadResolver — match by In-Reply-To header

**Files:**
- Modify: `src/email_agent/domain/thread_resolver.py`
- Modify: `tests/unit/test_thread_resolver.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_thread_resolver.py`:

```python
async def _seed_indexed_message(
    session: AsyncSession,
    *,
    assistant_id: str,
    end_user_id: str,
    thread_id: str,
    message_id_header: str,
    provider_message_id: str = "prov-prev",
    subject: str = "hi",
) -> None:
    session.add(
        EmailThread(
            id=thread_id,
            assistant_id=assistant_id,
            end_user_id=end_user_id,
            root_message_id=message_id_header,
            subject_normalized=subject,
        )
    )
    session.add(
        MessageIndex(
            assistant_id=assistant_id,
            message_id_header=message_id_header,
            thread_id=thread_id,
            provider_message_id=provider_message_id,
        )
    )
    await session.commit()


async def test_resolver_matches_thread_by_in_reply_to(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    async with sqlite_session_factory() as session:
        await _seed_indexed_message(
            session,
            assistant_id="a-1",
            end_user_id="u-1",
            thread_id="t-existing",
            message_id_header="<prev@example.com>",
        )

    resolver = ThreadResolver(sqlite_session_factory)
    thread = await resolver.resolve(
        _inbound(in_reply_to="<prev@example.com>"),
        _scope(),
    )

    assert thread.id == "t-existing"
```

Add to imports at the top of the file:

```python
from email_agent.db.models import EmailThread, MessageIndex
```

- [ ] **Step 2: Run to verify failure**

Run:

```bash
uv run pytest tests/unit/test_thread_resolver.py -v
```

Expected: FAIL — resolver creates a new thread instead of returning `t-existing`.

- [ ] **Step 3: Add In-Reply-To lookup**

In `src/email_agent/domain/thread_resolver.py`, replace `_resolve`:

```python
    async def _resolve(
        self,
        session: AsyncSession,
        email: NormalizedInboundEmail,
        scope: AssistantScope,
    ) -> EmailThread:
        if email.in_reply_to_header:
            existing = await self._find_thread_by_message_id(
                session, scope.assistant_id, email.in_reply_to_header
            )
            if existing is not None:
                return existing
        return await self._create_new_thread(session, email, scope)

    async def _find_thread_by_message_id(
        self,
        session: AsyncSession,
        assistant_id: str,
        message_id_header: str,
    ) -> EmailThread | None:
        stmt = (
            select(EmailThread)
            .join(MessageIndex, MessageIndex.thread_id == EmailThread.id)
            .where(
                MessageIndex.assistant_id == assistant_id,
                MessageIndex.message_id_header == message_id_header,
            )
        )
        return (await session.execute(stmt)).scalar_one_or_none()
```

- [ ] **Step 4: Run to verify pass**

Run:

```bash
uv run pytest tests/unit/test_thread_resolver.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/email_agent/domain/thread_resolver.py tests/unit/test_thread_resolver.py
git commit -m "feat(domain): match threads by In-Reply-To header"
```

---

## Task 8: ThreadResolver — match by References fallback

**Files:**
- Modify: `src/email_agent/domain/thread_resolver.py`
- Modify: `tests/unit/test_thread_resolver.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_thread_resolver.py`:

```python
async def test_resolver_falls_back_to_references_when_in_reply_to_misses(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    async with sqlite_session_factory() as session:
        await _seed_indexed_message(
            session,
            assistant_id="a-1",
            end_user_id="u-1",
            thread_id="t-references",
            message_id_header="<root@example.com>",
        )

    resolver = ThreadResolver(sqlite_session_factory)
    thread = await resolver.resolve(
        _inbound(
            in_reply_to="<missing@example.com>",
            references=["<root@example.com>", "<missing@example.com>"],
        ),
        _scope(),
    )

    assert thread.id == "t-references"
```

- [ ] **Step 2: Run to verify failure**

Run:

```bash
uv run pytest tests/unit/test_thread_resolver.py -v
```

Expected: FAIL — resolver creates a new thread.

- [ ] **Step 3: Add References fallback**

In `_resolve`, between the In-Reply-To block and the create call, add:

```python
        for ref in reversed(email.references_headers):
            existing = await self._find_thread_by_message_id(
                session, scope.assistant_id, ref
            )
            if existing is not None:
                return existing
```

The full `_resolve` body should now be:

```python
        if email.in_reply_to_header:
            existing = await self._find_thread_by_message_id(
                session, scope.assistant_id, email.in_reply_to_header
            )
            if existing is not None:
                return existing
        for ref in reversed(email.references_headers):
            existing = await self._find_thread_by_message_id(
                session, scope.assistant_id, ref
            )
            if existing is not None:
                return existing
        return await self._create_new_thread(session, email, scope)
```

- [ ] **Step 4: Run to verify pass**

Run:

```bash
uv run pytest tests/unit/test_thread_resolver.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/email_agent/domain/thread_resolver.py tests/unit/test_thread_resolver.py
git commit -m "feat(domain): fall back to References for thread match"
```

---

## Task 9: ThreadResolver — never crosses assistant scope

**Files:**
- Modify: `tests/unit/test_thread_resolver.py`

This is a guarantee test — the existing implementation should already satisfy it because the message_index query filters by `assistant_id`, but the spec calls it out as a priority test, so we lock the behaviour in.

- [ ] **Step 1: Write the test**

Append to `tests/unit/test_thread_resolver.py`:

```python
async def test_resolver_does_not_match_threads_from_other_assistants(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    async with sqlite_session_factory() as session:
        await _seed_indexed_message(
            session,
            assistant_id="a-other",
            end_user_id="u-other",
            thread_id="t-other",
            message_id_header="<shared@example.com>",
        )

    resolver = ThreadResolver(sqlite_session_factory)
    thread = await resolver.resolve(
        _inbound(in_reply_to="<shared@example.com>"),
        _scope(assistant_id="a-1", end_user_id="u-1"),
    )

    assert thread.id != "t-other"
    assert thread.assistant_id == "a-1"
```

- [ ] **Step 2: Run to verify pass**

Run:

```bash
uv run pytest tests/unit/test_thread_resolver.py -v
```

Expected: PASS — current implementation already filters by `assistant_id`.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_thread_resolver.py
git commit -m "test(domain): lock cross-assistant thread isolation"
```

---

## Task 10: Inbound persister — store message + attachments + index

**Files:**
- Create: `src/email_agent/domain/inbound_persister.py`
- Create: `tests/unit/test_inbound_persister.py`

The persister is a function rather than a class — it has no state, just a transactional sequence of writes against an open session. The webhook flow calls it inside the same session that resolved the thread (so persistence can be made atomic later in slice 5). Attachments are written under `attachments_root/<message_id>/`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_inbound_persister.py`:

```python
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from email_agent.db.models import (
    EmailAttachmentRow,
    EmailMessage,
    EmailThread,
    MessageIndex,
)
from email_agent.domain.inbound_persister import (
    PersistedInbound,
    persist_inbound,
)
from email_agent.models.assistant import AssistantScope, AssistantStatus
from email_agent.models.email import EmailAttachment, NormalizedInboundEmail


def _scope() -> AssistantScope:
    return AssistantScope(
        assistant_id="a-1",
        owner_id="o-1",
        end_user_id="u-1",
        inbound_address="a-1@assistants.example.com",
        status=AssistantStatus.ACTIVE,
        allowed_senders=("mum@example.com",),
        memory_namespace="mum",
        tool_allowlist=("read",),
        budget_id="b-1",
        model_name="deepseek-flash",
        system_prompt="be kind",
    )


def _inbound(
    *,
    provider_message_id: str = "prov-1",
    message_id: str = "<m-1@example.com>",
    attachments: list[EmailAttachment] | None = None,
) -> NormalizedInboundEmail:
    return NormalizedInboundEmail(
        provider_message_id=provider_message_id,
        message_id_header=message_id,
        from_email="mum@example.com",
        to_emails=["a-1@assistants.example.com"],
        subject="hi",
        body_text="hello",
        attachments=attachments or [],
        received_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
    )


async def _seed_thread(
    session: AsyncSession, *, thread_id: str = "t-1"
) -> EmailThread:
    thread = EmailThread(
        id=thread_id,
        assistant_id="a-1",
        end_user_id="u-1",
        root_message_id="<m-1@example.com>",
        subject_normalized="hi",
    )
    session.add(thread)
    await session.commit()
    return thread


async def test_persist_inbound_writes_message_and_index(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    async with sqlite_session_factory() as session:
        thread = await _seed_thread(session)

    async with sqlite_session_factory() as session:
        result = await persist_inbound(
            session,
            email=_inbound(),
            scope=_scope(),
            thread=thread,
            attachments_root=tmp_path,
        )
        await session.commit()

    assert isinstance(result, PersistedInbound)
    assert result.created is True
    assert result.message.thread_id == "t-1"
    assert result.message.assistant_id == "a-1"
    assert result.message.direction == "inbound"

    async with sqlite_session_factory() as session:
        messages = (await session.execute(select(EmailMessage))).scalars().all()
        index_rows = (await session.execute(select(MessageIndex))).scalars().all()
        assert len(messages) == 1
        assert len(index_rows) == 1
        assert index_rows[0].message_id_header == "<m-1@example.com>"
        assert index_rows[0].thread_id == "t-1"
```

- [ ] **Step 2: Run to verify failure**

Run:

```bash
uv run pytest tests/unit/test_inbound_persister.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement persister (no attachments yet)**

Create `src/email_agent/domain/inbound_persister.py`:

```python
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_agent.db.models import (
    EmailAttachmentRow,
    EmailMessage,
    EmailThread,
    MessageIndex,
)
from email_agent.models.assistant import AssistantScope
from email_agent.models.email import EmailAttachment, NormalizedInboundEmail


@dataclass(frozen=True)
class PersistedInbound:
    """Result of `persist_inbound`.

    `created` is False when the inbound was a duplicate webhook delivery and
    the existing row was returned unchanged; this is what makes the webhook
    fast path idempotent on `(assistant_id, provider_message_id)`.
    """

    message: EmailMessage
    created: bool


async def persist_inbound(
    session: AsyncSession,
    *,
    email: NormalizedInboundEmail,
    scope: AssistantScope,
    thread: EmailThread,
    attachments_root: Path,
) -> PersistedInbound:
    """Persist an inbound email + its attachments + a `message_index` entry.

    Idempotent on `(assistant_id, provider_message_id)`; duplicate Mailgun
    deliveries return the existing row with `created=False`. Caller owns
    the transaction (commits on success).
    """
    existing = await _find_existing(session, scope.assistant_id, email.provider_message_id)
    if existing is not None:
        return PersistedInbound(message=existing, created=False)

    message = EmailMessage(
        id=f"m-{uuid.uuid4().hex[:12]}",
        thread_id=thread.id,
        assistant_id=scope.assistant_id,
        direction="inbound",
        provider_message_id=email.provider_message_id,
        message_id_header=email.message_id_header,
        in_reply_to_header=email.in_reply_to_header,
        references_headers=list(email.references_headers),
        from_email=email.from_email,
        to_emails=list(email.to_emails),
        subject=email.subject,
        body_text=email.body_text,
        body_html=email.body_html,
    )
    session.add(message)
    await session.flush()

    for attachment in email.attachments:
        _write_attachment_row(session, message.id, attachment, attachments_root)

    session.add(
        MessageIndex(
            assistant_id=scope.assistant_id,
            message_id_header=email.message_id_header,
            thread_id=thread.id,
            provider_message_id=email.provider_message_id,
        )
    )
    await session.flush()
    return PersistedInbound(message=message, created=True)


async def _find_existing(
    session: AsyncSession, assistant_id: str, provider_message_id: str
) -> EmailMessage | None:
    stmt = select(EmailMessage).where(
        EmailMessage.assistant_id == assistant_id,
        EmailMessage.provider_message_id == provider_message_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _write_attachment_row(
    session: AsyncSession,
    message_id: str,
    attachment: EmailAttachment,
    attachments_root: Path,
) -> None:
    target_dir = attachments_root / message_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / attachment.filename
    target_path.write_bytes(attachment.data)
    session.add(
        EmailAttachmentRow(
            id=f"att-{uuid.uuid4().hex[:12]}",
            message_id=message_id,
            filename=attachment.filename,
            content_type=attachment.content_type,
            size_bytes=attachment.size_bytes,
            storage_path=str(target_path),
        )
    )
```

- [ ] **Step 4: Run to verify pass**

Run:

```bash
uv run pytest tests/unit/test_inbound_persister.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/email_agent/domain/inbound_persister.py tests/unit/test_inbound_persister.py
git commit -m "feat(domain): persist inbound emails + message_index entries"
```

---

## Task 11: Inbound persister — attachments written to disk

**Files:**
- Modify: `tests/unit/test_inbound_persister.py`

The implementation in Task 10 already writes attachment rows + bytes; this task just adds explicit coverage so a future regression breaks the test, not the prod system.

- [ ] **Step 1: Write the test**

Append to `tests/unit/test_inbound_persister.py`:

```python
async def test_persist_inbound_writes_attachment_bytes_to_disk(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    attachment = EmailAttachment(
        filename="receipt.pdf",
        content_type="application/pdf",
        size_bytes=4,
        data=b"%PDF",
    )
    async with sqlite_session_factory() as session:
        thread = await _seed_thread(session)

    async with sqlite_session_factory() as session:
        result = await persist_inbound(
            session,
            email=_inbound(attachments=[attachment]),
            scope=_scope(),
            thread=thread,
            attachments_root=tmp_path,
        )
        await session.commit()

    async with sqlite_session_factory() as session:
        rows = (await session.execute(select(EmailAttachmentRow))).scalars().all()
        assert len(rows) == 1
        stored = Path(rows[0].storage_path)
        assert stored.exists()
        assert stored.read_bytes() == b"%PDF"
        assert stored.parent.name == result.message.id
```

- [ ] **Step 2: Run to verify pass**

Run:

```bash
uv run pytest tests/unit/test_inbound_persister.py -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_inbound_persister.py
git commit -m "test(domain): cover attachment persistence to disk"
```

---

## Task 12: Inbound persister — idempotent on duplicate provider_message_id

**Files:**
- Modify: `tests/unit/test_inbound_persister.py`

- [ ] **Step 1: Write the test**

Append to `tests/unit/test_inbound_persister.py`:

```python
async def test_persist_inbound_is_idempotent_on_duplicate_delivery(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
):
    async with sqlite_session_factory() as session:
        thread = await _seed_thread(session)

    async with sqlite_session_factory() as session:
        first = await persist_inbound(
            session,
            email=_inbound(),
            scope=_scope(),
            thread=thread,
            attachments_root=tmp_path,
        )
        await session.commit()

    async with sqlite_session_factory() as session:
        # Re-run the same provider delivery against a fresh session/thread instance.
        thread = await session.get(EmailThread, "t-1")
        assert thread is not None
        second = await persist_inbound(
            session,
            email=_inbound(),
            scope=_scope(),
            thread=thread,
            attachments_root=tmp_path,
        )
        await session.commit()

    assert first.created is True
    assert second.created is False
    assert first.message.id == second.message.id

    async with sqlite_session_factory() as session:
        message_count = len(
            (await session.execute(select(EmailMessage))).scalars().all()
        )
        index_count = len(
            (await session.execute(select(MessageIndex))).scalars().all()
        )
        assert message_count == 1
        assert index_count == 1
```

- [ ] **Step 2: Run to verify pass**

Run:

```bash
uv run pytest tests/unit/test_inbound_persister.py -v
```

Expected: PASS — Task 10's implementation already handles the idempotency branch.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_inbound_persister.py
git commit -m "test(domain): lock idempotent inbound persistence"
```

---

## Task 13: Mailgun adapter — verify_webhook (HMAC signature)

**Files:**
- Create: `src/email_agent/mail/mailgun.py`
- Create: `tests/unit/test_mailgun_provider.py`
- Modify: `src/email_agent/mail/__init__.py`

Mailgun signs each webhook with `HMAC-SHA256(signing_key, timestamp + token)`, posted alongside the form fields `timestamp`, `token`, `signature`. We verify those before parsing. (Reference: https://documentation.mailgun.com/docs/mailgun/user-manual/receive-forward-store/#webhook-validation)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_mailgun_provider.py`:

```python
import hashlib
import hmac
import json

import pytest

from email_agent.mail.mailgun import (
    MailgunEmailProvider,
    MailgunSignatureError,
)
from email_agent.models.email import WebhookRequest

SIGNING_KEY = "test-signing-key"
TIMESTAMP = "1747900000"
TOKEN = "abc123"


def _signature(signing_key: str = SIGNING_KEY) -> str:
    return hmac.new(
        signing_key.encode(),
        f"{TIMESTAMP}{TOKEN}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _form(**overrides: str) -> dict[str, str]:
    base = {
        "timestamp": TIMESTAMP,
        "token": TOKEN,
        "signature": _signature(),
        "recipient": "a-1@assistants.example.com",
        "sender": "mum@example.com",
        "from": "Mum <mum@example.com>",
        "subject": "hi",
        "body-plain": "hello",
        "Message-Id": "<m-1@example.com>",
        "message-headers": json.dumps(
            [
                ["Message-Id", "<m-1@example.com>"],
                ["From", "Mum <mum@example.com>"],
                ["To", "a-1@assistants.example.com"],
                ["Subject", "hi"],
            ]
        ),
    }
    base.update(overrides)
    return base


async def test_verify_webhook_accepts_valid_signature():
    provider = MailgunEmailProvider(signing_key=SIGNING_KEY)
    await provider.verify_webhook(
        WebhookRequest(headers={}, body=b"", form=_form()),
    )


async def test_verify_webhook_rejects_bad_signature():
    provider = MailgunEmailProvider(signing_key=SIGNING_KEY)
    with pytest.raises(MailgunSignatureError):
        await provider.verify_webhook(
            WebhookRequest(headers={}, body=b"", form=_form(signature="0" * 64)),
        )


async def test_verify_webhook_rejects_missing_signature_fields():
    provider = MailgunEmailProvider(signing_key=SIGNING_KEY)
    form = _form()
    del form["timestamp"]
    with pytest.raises(MailgunSignatureError):
        await provider.verify_webhook(
            WebhookRequest(headers={}, body=b"", form=form),
        )
```

- [ ] **Step 2: Run to verify failure**

Run:

```bash
uv run pytest tests/unit/test_mailgun_provider.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement verify_webhook**

Create `src/email_agent/mail/mailgun.py`:

```python
import hashlib
import hmac
from datetime import UTC, datetime

from email_agent.models.email import (
    NormalizedInboundEmail,
    NormalizedOutboundEmail,
    SentEmail,
    WebhookRequest,
)


class MailgunSignatureError(Exception):
    """Mailgun webhook failed signature verification — drop the request."""


class MailgunParseError(Exception):
    """Mailgun webhook payload was malformed — drop the request."""


class MailgunEmailProvider:
    """Mailgun adapter for `EmailProvider`.

    `verify_webhook` checks the HMAC-SHA256 signature Mailgun attaches to
    every webhook (`timestamp + token` signed with the signing key).
    `parse_inbound` translates Mailgun's form payload into the wire model.
    `send_reply` is a placeholder until slice 3.
    """

    def __init__(self, *, signing_key: str) -> None:
        self._signing_key = signing_key.encode()

    async def verify_webhook(self, request: WebhookRequest) -> None:
        try:
            timestamp = request.form["timestamp"]
            token = request.form["token"]
            signature = request.form["signature"]
        except KeyError as exc:
            raise MailgunSignatureError(
                f"missing signing field: {exc.args[0]}"
            ) from exc

        expected = hmac.new(
            self._signing_key,
            f"{timestamp}{token}".encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise MailgunSignatureError("invalid signature")

    async def parse_inbound(self, request: WebhookRequest) -> NormalizedInboundEmail:
        raise NotImplementedError

    async def send_reply(self, reply: NormalizedOutboundEmail) -> SentEmail:
        raise NotImplementedError("send_reply lands in slice 3")
```

Update `src/email_agent/mail/__init__.py` to export it. Read the existing file first to see what's already exported, then add the new class to that export list. If the file is empty/minimal, set its content to:

```python
from email_agent.mail.inmemory import InMemoryEmailProvider
from email_agent.mail.mailgun import (
    MailgunEmailProvider,
    MailgunParseError,
    MailgunSignatureError,
)
from email_agent.mail.port import EmailProvider

__all__ = [
    "EmailProvider",
    "InMemoryEmailProvider",
    "MailgunEmailProvider",
    "MailgunParseError",
    "MailgunSignatureError",
]
```

- [ ] **Step 4: Run to verify pass**

Run:

```bash
uv run pytest tests/unit/test_mailgun_provider.py -v
```

Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/email_agent/mail/mailgun.py src/email_agent/mail/__init__.py tests/unit/test_mailgun_provider.py
git commit -m "feat(mail): add MailgunEmailProvider verify_webhook"
```

---

## Task 14: Mailgun adapter — parse_inbound (text body, headers)

**Files:**
- Modify: `src/email_agent/mail/mailgun.py`
- Modify: `tests/unit/test_mailgun_provider.py`

Mailgun's parsed-message webhook supplies the email as form fields. We map:

| Mailgun form field | NormalizedInboundEmail field |
| --- | --- |
| `recipient` (single) | `to_emails = [recipient]` |
| `sender` | `from_email` |
| `subject` | `subject` |
| `body-plain` | `body_text` |
| `body-html` (optional) | `body_html` |
| `Message-Id` | `message_id_header` |
| `message-headers` JSON, find `In-Reply-To` | `in_reply_to_header` |
| `message-headers` JSON, find `References` (space-separated) | `references_headers` (split) |
| `timestamp` | `received_at` (UTC datetime) |

Provider message id: Mailgun's `Message-Id` form field stripped of `<>`. (Mailgun also sends an internal `event-data` id but stripped Message-Id is the cross-provider stable choice and matches the design's expectation that webhook-retries hit the same `(assistant_id, provider_message_id)` key.)

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_mailgun_provider.py`:

```python
async def test_parse_inbound_maps_basic_fields():
    provider = MailgunEmailProvider(signing_key=SIGNING_KEY)
    email = await provider.parse_inbound(
        WebhookRequest(headers={}, body=b"", form=_form()),
    )

    assert email.provider_message_id == "m-1@example.com"
    assert email.message_id_header == "<m-1@example.com>"
    assert email.from_email == "mum@example.com"
    assert email.to_emails == ["a-1@assistants.example.com"]
    assert email.subject == "hi"
    assert email.body_text == "hello"
    assert email.body_html is None
    assert email.in_reply_to_header is None
    assert email.references_headers == []


async def test_parse_inbound_extracts_in_reply_to_and_references():
    headers = json.dumps(
        [
            ["Message-Id", "<m-2@example.com>"],
            ["In-Reply-To", "<prev@example.com>"],
            ["References", "<root@example.com> <prev@example.com>"],
        ]
    )
    form = _form(**{"message-headers": headers, "Message-Id": "<m-2@example.com>"})
    provider = MailgunEmailProvider(signing_key=SIGNING_KEY)
    email = await provider.parse_inbound(
        WebhookRequest(headers={}, body=b"", form=form),
    )
    assert email.in_reply_to_header == "<prev@example.com>"
    assert email.references_headers == ["<root@example.com>", "<prev@example.com>"]


async def test_parse_inbound_includes_html_body_when_present():
    provider = MailgunEmailProvider(signing_key=SIGNING_KEY)
    email = await provider.parse_inbound(
        WebhookRequest(
            headers={},
            body=b"",
            form=_form(**{"body-html": "<p>hello</p>"}),
        ),
    )
    assert email.body_html == "<p>hello</p>"


async def test_parse_inbound_raises_on_missing_required_field():
    form = _form()
    del form["body-plain"]
    provider = MailgunEmailProvider(signing_key=SIGNING_KEY)
    with pytest.raises(MailgunParseError):
        await provider.parse_inbound(
            WebhookRequest(headers={}, body=b"", form=form),
        )
```

- [ ] **Step 2: Run to verify failure**

Run:

```bash
uv run pytest tests/unit/test_mailgun_provider.py -v
```

Expected: 4 new tests FAIL — `parse_inbound` raises `NotImplementedError`.

- [ ] **Step 3: Implement parse_inbound**

Replace the `parse_inbound` body in `src/email_agent/mail/mailgun.py`:

```python
    async def parse_inbound(self, request: WebhookRequest) -> NormalizedInboundEmail:
        form = request.form
        try:
            recipient = form["recipient"]
            sender = form["sender"]
            subject = form["subject"]
            body_text = form["body-plain"]
            message_id_header = form["Message-Id"]
            timestamp = form["timestamp"]
        except KeyError as exc:
            raise MailgunParseError(
                f"missing required field: {exc.args[0]}"
            ) from exc

        headers = _parse_headers(form.get("message-headers", "[]"))
        in_reply_to = headers.get("In-Reply-To")
        references = _split_references(headers.get("References", ""))

        return NormalizedInboundEmail(
            provider_message_id=message_id_header.strip("<>"),
            message_id_header=message_id_header,
            in_reply_to_header=in_reply_to,
            references_headers=references,
            from_email=sender,
            to_emails=[recipient],
            subject=subject,
            body_text=body_text,
            body_html=form.get("body-html") or None,
            received_at=datetime.fromtimestamp(int(timestamp), tz=UTC),
        )
```

Add helpers at module bottom:

```python
def _parse_headers(raw: str) -> dict[str, str]:
    """Convert Mailgun's `message-headers` JSON list into a dict.

    Mailgun sends headers as `[["Name", "value"], ...]`. We keep the *last*
    occurrence of each header name to mirror MTA behaviour.
    """
    import json

    try:
        pairs = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise MailgunParseError(f"bad message-headers JSON: {exc}") from exc
    return {name: value for name, value in pairs}


def _split_references(raw: str) -> list[str]:
    return [token for token in raw.split() if token]
```

- [ ] **Step 4: Run to verify pass**

Run:

```bash
uv run pytest tests/unit/test_mailgun_provider.py -v
```

Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/email_agent/mail/mailgun.py tests/unit/test_mailgun_provider.py
git commit -m "feat(mail): parse Mailgun webhooks to NormalizedInboundEmail"
```

---

## Task 15: Mailgun adapter — attachments

**Files:**
- Modify: `src/email_agent/mail/mailgun.py`
- Modify: `tests/unit/test_mailgun_provider.py`

Mailgun delivers attachments either as additional `attachment-N` files in a multipart body (parsed by FastAPI before reaching us) or, in the **route forward webhook**, as a JSON `attachments` form field describing them with absolute URLs to fetch via the API. For MVP we accept the simpler shape used by Mailgun's "Forward" routes and tests: an `attachments` JSON field where each entry has `filename`, `content-type`, `size`, and inline `content` (base64) — that's the shape Mailgun's "store and notify" route exposes when you check "Include attachments".

The adapter takes a callable `fetch_attachment_bytes` (defaults to assuming inline base64 content); a future task can swap it for an HTTP-fetching variant when we wire real Mailgun store+notify routes.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_mailgun_provider.py`:

```python
import base64


async def test_parse_inbound_includes_inline_attachments():
    encoded = base64.b64encode(b"%PDF").decode()
    attachments_field = json.dumps(
        [
            {
                "filename": "receipt.pdf",
                "content-type": "application/pdf",
                "size": 4,
                "content": encoded,
            }
        ]
    )
    provider = MailgunEmailProvider(signing_key=SIGNING_KEY)
    email = await provider.parse_inbound(
        WebhookRequest(
            headers={},
            body=b"",
            form=_form(attachments=attachments_field),
        ),
    )

    assert len(email.attachments) == 1
    att = email.attachments[0]
    assert att.filename == "receipt.pdf"
    assert att.content_type == "application/pdf"
    assert att.size_bytes == 4
    assert att.data == b"%PDF"
```

- [ ] **Step 2: Run to verify failure**

Run:

```bash
uv run pytest tests/unit/test_mailgun_provider.py -v
```

Expected: FAIL — `email.attachments` is empty.

- [ ] **Step 3: Implement attachment parsing**

In `src/email_agent/mail/mailgun.py`, after building the `references` list and before constructing `NormalizedInboundEmail`:

```python
        attachments = _parse_attachments(form.get("attachments", "[]"))
```

Add `attachments=attachments` to the `NormalizedInboundEmail(...)` call.

Add the helper at module bottom:

```python
def _parse_attachments(raw: str) -> list[EmailAttachment]:
    import base64
    import json

    try:
        items = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise MailgunParseError(f"bad attachments JSON: {exc}") from exc
    if not items:
        return []

    out: list[EmailAttachment] = []
    for item in items:
        try:
            content_b64 = item["content"]
        except KeyError as exc:
            raise MailgunParseError(
                "attachment missing inline `content` field; URL-fetch path not yet implemented"
            ) from exc
        out.append(
            EmailAttachment(
                filename=item["filename"],
                content_type=item.get("content-type", "application/octet-stream"),
                size_bytes=int(item.get("size", 0)),
                data=base64.b64decode(content_b64),
            )
        )
    return out
```

Add the import at the top of `mailgun.py`:

```python
from email_agent.models.email import (
    EmailAttachment,
    NormalizedInboundEmail,
    NormalizedOutboundEmail,
    SentEmail,
    WebhookRequest,
)
```

- [ ] **Step 4: Run to verify pass**

Run:

```bash
uv run pytest tests/unit/test_mailgun_provider.py -v
```

Expected: 8 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/email_agent/mail/mailgun.py tests/unit/test_mailgun_provider.py
git commit -m "feat(mail): parse inline attachments from Mailgun payload"
```

---

## Task 16: Integration test — round-trip pipeline against Postgres

**Files:**
- Create: `tests/integration/test_inbound_pipeline.py`

Wires Mailgun adapter → router → resolver → persister against a real Postgres session, proving the four pieces compose. Mirrors the slice 1 integration test pattern (skip when `DATABASE_URL` is not set).

- [ ] **Step 1: Write the test**

Create `tests/integration/test_inbound_pipeline.py`:

```python
import base64
import hashlib
import hmac
import json
import os
import uuid
from datetime import UTC, datetime

import pytest

from email_agent.config import Settings
from email_agent.db.models import (
    Assistant,
    AssistantScopeRow,
    Budget,
    EmailMessage,
    EndUser,
    MessageIndex,
    Owner,
)
from email_agent.db.session import make_engine, make_session_factory, session_scope
from email_agent.domain.inbound_persister import persist_inbound
from email_agent.domain.router import AssistantRouter, Routed
from email_agent.domain.thread_resolver import ThreadResolver
from email_agent.mail.mailgun import MailgunEmailProvider
from email_agent.models.email import WebhookRequest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SIGNING_KEY = "test-signing-key"


def _signed_form(*, recipient: str, sender: str) -> dict[str, str]:
    timestamp = "1747900000"
    token = "tok"
    signature = hmac.new(
        SIGNING_KEY.encode(),
        f"{timestamp}{token}".encode(),
        hashlib.sha256,
    ).hexdigest()
    message_id = f"<{uuid.uuid4().hex}@example.com>"
    return {
        "timestamp": timestamp,
        "token": token,
        "signature": signature,
        "recipient": recipient,
        "sender": sender,
        "from": sender,
        "subject": "hello there",
        "body-plain": "real body",
        "Message-Id": message_id,
        "message-headers": json.dumps([["Message-Id", message_id]]),
        "attachments": json.dumps(
            [
                {
                    "filename": "note.txt",
                    "content-type": "text/plain",
                    "size": 5,
                    "content": base64.b64encode(b"hello").decode(),
                }
            ]
        ),
    }


@pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="needs db")
async def test_round_trip_pipeline_persists_message_index_and_attachments(tmp_path):
    settings = Settings()  # ty: ignore[missing-argument]
    engine = make_engine(settings)
    factory = make_session_factory(engine)

    suffix = uuid.uuid4().hex[:8]
    inbound_address = f"a-{suffix}@assistants.example.com"
    sender = f"mum-{suffix}@example.com"

    async with session_scope(factory) as s:
        s.add(Owner(id=f"o-{suffix}", name="Larry"))
        s.add(EndUser(id=f"u-{suffix}", owner_id=f"o-{suffix}", email=sender))
        s.add(
            Budget(
                id=f"b-{suffix}",
                assistant_id=f"a-{suffix}",
                monthly_limit_cents=1000,
                period_starts_at=datetime(2026, 5, 1, tzinfo=UTC),
                period_resets_at=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )
        s.add(
            Assistant(
                id=f"a-{suffix}",
                end_user_id=f"u-{suffix}",
                inbound_address=inbound_address,
                status="active",
                allowed_senders=[sender],
                model="deepseek-flash",
                system_prompt="be kind",
            )
        )
        s.add(
            AssistantScopeRow(
                assistant_id=f"a-{suffix}",
                memory_namespace=f"ns-{suffix}",
                tool_allowlist=["read"],
                budget_id=f"b-{suffix}",
            )
        )

    provider = MailgunEmailProvider(signing_key=SIGNING_KEY)
    request = WebhookRequest(
        headers={},
        body=b"",
        form=_signed_form(recipient=inbound_address, sender=sender),
    )
    await provider.verify_webhook(request)
    email = await provider.parse_inbound(request)

    router = AssistantRouter(factory)
    outcome = await router.resolve(email)
    assert isinstance(outcome, Routed)

    resolver = ThreadResolver(factory)
    thread = await resolver.resolve(email, outcome.scope)

    async with session_scope(factory) as s:
        thread = await s.get(type(thread), thread.id)
        assert thread is not None
        result = await persist_inbound(
            s,
            email=email,
            scope=outcome.scope,
            thread=thread,
            attachments_root=tmp_path,
        )
        assert result.created is True

    async with session_scope(factory) as s:
        from sqlalchemy import select

        msg = (
            await s.execute(
                select(EmailMessage).where(
                    EmailMessage.assistant_id == f"a-{suffix}",
                )
            )
        ).scalar_one()
        idx = (
            await s.execute(
                select(MessageIndex).where(
                    MessageIndex.assistant_id == f"a-{suffix}",
                )
            )
        ).scalar_one()
        assert msg.message_id_header == email.message_id_header
        assert idx.thread_id == thread.id

    await engine.dispose()
```

- [ ] **Step 2: Run with Postgres available**

Bring up the dev database (per slice 1 docs / `docker-compose.yml`), then:

```bash
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/email_assistant \
  uv run pytest tests/integration/test_inbound_pipeline.py -v
```

Expected: PASS. If `DATABASE_URL` is unset the test is skipped — same behaviour as slice 1's integration test.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_inbound_pipeline.py
git commit -m "test(integration): round-trip Mailgun → router → thread → persist"
```

---

## Task 17: Lint, type-check, and full-suite green

**Files:** none (verification only).

- [ ] **Step 1: Run ruff**

Run:

```bash
uv run ruff check
uv run ruff format --check
```

Expected: PASS. Fix any violations inline (most likely import ordering or unused imports), re-run, and stage the fixes.

- [ ] **Step 2: Run ty**

Run:

```bash
uv run ty check
```

Expected: PASS. If a real type error surfaces, fix it; only suppress with `# ty: ignore[<rule>]` when the type system genuinely can't see what's going on.

- [ ] **Step 3: Run the full unit suite**

Run:

```bash
uv run pytest tests/unit -q
```

Expected: PASS, no skips outside the existing slice 1 ones.

- [ ] **Step 4: Commit any cleanups**

If ruff/ty surfaced fixes:

```bash
git add -p
git commit -m "chore: ruff/ty cleanup post slice-2"
```

If everything was already clean, skip the commit.

---

## Self-review (already performed)

**Spec coverage:**
- Mailgun webhook verification → Tasks 13.
- Mailgun parser → Tasks 14, 15.
- AssistantRouter (resolution + paused/disabled/unknown/sender allowlist rejections) → Tasks 3, 4, 5.
- ThreadResolver (provider thread id is N/A for Mailgun, then In-Reply-To, then References, then new thread, plus cross-assistant isolation) → Tasks 6, 7, 8, 9.
- message_index population → Task 10.
- Storing inbound message + attachments → Tasks 10, 11.
- Idempotency on duplicate provider delivery → Task 12.
- Integration round-trip → Task 16.

Slice scope explicitly excludes: agent_runs row creation, Procrastinate enqueue, FastAPI HTTP route, send_reply implementation. Each is owned by a later slice (5, 7, 5, 3 respectively).

**Placeholder scan:** every step contains the actual code or command an engineer needs; no "TBD" / "appropriate" / "as needed".

**Type consistency:** `RouteOutcome = Routed | RouteRejection`; `Routed.scope: AssistantScope`; `RouteRejection.reason: RouteRejectionReason` — all consistent across tasks 3–5. `PersistedInbound.message: EmailMessage` and `PersistedInbound.created: bool` consistent across tasks 10–12. `ThreadResolver.resolve(email, scope) -> EmailThread` consistent across tasks 6–9.
