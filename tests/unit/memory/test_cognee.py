"""Unit tests for CogneeMemoryAdapter.

We don't run real cognee here (no LLM/embedding key, slow). Instead we
monkeypatch the module-level functions the adapter uses (`cognee.remember`,
`cognee.recall`, `get_user_by_email`, `create_user`, etc.) and verify the
adapter:

1. Lazily creates one cognee `User` per `assistant_id` and caches it.
2. Threads `user=` and `session_id=` through every call so cognee's own
   tenant/conversation model handles isolation + per-thread sessions.
3. Translates `NoDataError` (fresh user with no chunks yet) into an
   empty `MemoryContext` rather than re-raising.

The full end-to-end (real cognee, real LLM/embedding keys) is covered by
the gated integration test under tests/integration/.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from email_agent.memory.cognee import CogneeMemoryAdapter


class _FakeUser:
    """Minimal stand-in for `cognee.modules.users.models.User` — we only
    need it to be a unique, comparable object."""

    def __init__(self, email: str) -> None:
        self.email = email
        self.id = email  # cognee passes user.id around in some places


class _FakeCognee:
    """Records remember/recall/forget calls so tests can introspect what
    the adapter forwarded."""

    def __init__(self) -> None:
        self.remember_calls: list[dict] = []
        self.recall_calls: list[dict] = []
        self.forget_calls: list[dict] = []
        self.recall_returns: list[dict] = []
        self.recall_side_effect: BaseException | None = None

    async def remember(self, text: str, **kwargs: Any) -> None:
        self.remember_calls.append({"text": text, **kwargs})

    async def recall(self, query: str, **kwargs: Any) -> list[dict]:
        self.recall_calls.append({"query": query, **kwargs})
        if self.recall_side_effect is not None:
            raise self.recall_side_effect
        return list(self.recall_returns)

    async def forget(self, **kwargs: Any) -> dict:
        self.forget_calls.append(kwargs)
        return {}


@pytest.fixture
def fake_cognee(monkeypatch: pytest.MonkeyPatch) -> _FakeCognee:
    fake = _FakeCognee()
    import email_agent.memory.cognee as mod

    monkeypatch.setattr(mod, "cognee", fake)
    return fake


class _FakeUserStore:
    """In-memory user store + audit logs the adapter is monkeypatched against."""

    def __init__(self) -> None:
        self.by_email: dict[str, _FakeUser] = {}
        self.create_log: list[str] = []
        self.delete_log: list[str] = []

    def __getitem__(self, email: str) -> _FakeUser:
        return self.by_email[email]


@pytest.fixture
def fake_users(monkeypatch: pytest.MonkeyPatch) -> _FakeUserStore:
    store = _FakeUserStore()

    async def fake_get_user_by_email(email: str) -> _FakeUser | None:
        return store.by_email.get(email)

    async def fake_create_user(*, email: str, **_: Any) -> _FakeUser:
        user = _FakeUser(email)
        store.by_email[email] = user
        store.create_log.append(email)
        return user

    async def fake_delete_user(email: str) -> None:
        store.by_email.pop(email, None)
        store.delete_log.append(email)

    import email_agent.memory.cognee as mod

    monkeypatch.setattr(mod, "get_user_by_email", fake_get_user_by_email)
    monkeypatch.setattr(mod, "create_user", fake_create_user)
    monkeypatch.setattr(mod, "delete_user", fake_delete_user)
    return store


@pytest.mark.asyncio
async def test_record_turn_creates_user_and_passes_session_id(
    fake_cognee: _FakeCognee, fake_users: _FakeUserStore
) -> None:
    adapter = CogneeMemoryAdapter()
    await adapter.record_turn("a-1", "t-99", "user", "I love sourdough")

    # User was lazily created with the synthetic email scheme.
    assert fake_users.create_log == ["assistant-a-1@email-agent.local"]
    user = fake_users["assistant-a-1@email-agent.local"]

    assert len(fake_cognee.remember_calls) == 1
    call = fake_cognee.remember_calls[0]
    assert "sourdough" in call["text"]
    assert call["text"].startswith("[user]")
    assert call["session_id"] == "t-99"
    assert call["user"] is user


@pytest.mark.asyncio
async def test_user_is_cached_across_calls(
    fake_cognee: _FakeCognee, fake_users: _FakeUserStore
) -> None:
    """A second call for the same assistant_id must NOT re-create the user."""
    adapter = CogneeMemoryAdapter()
    await adapter.record_turn("a-1", "t-1", "user", "first")
    await adapter.record_turn("a-1", "t-2", "user", "second")
    await adapter.recall("a-1", "t-2", "anything")

    assert fake_users.create_log == ["assistant-a-1@email-agent.local"]


@pytest.mark.asyncio
async def test_concurrent_first_touches_create_one_user(
    fake_cognee: _FakeCognee, fake_users: _FakeUserStore
) -> None:
    """Per-assistant first-touch lock prevents two parallel create_user calls."""
    adapter = CogneeMemoryAdapter()
    await asyncio.gather(
        adapter.record_turn("a-1", "t-1", "user", "x"),
        adapter.record_turn("a-1", "t-2", "user", "y"),
        adapter.record_turn("a-1", "t-3", "user", "z"),
    )
    assert fake_users.create_log == ["assistant-a-1@email-agent.local"]


@pytest.mark.asyncio
async def test_recall_returns_chunks_with_search_result_extracted(
    fake_cognee: _FakeCognee, fake_users: _FakeUserStore
) -> None:
    from cognee.modules.search.types import SearchType

    fake_cognee.recall_returns = [
        {"search_result": "sourdough is great", "_source": "graph"},
        {"search_result": "user prefers detail", "_source": "graph"},
    ]
    adapter = CogneeMemoryAdapter()

    ctx = await adapter.recall("a-2", "t-9", "what about sourdough")

    assert len(fake_cognee.recall_calls) == 1
    call = fake_cognee.recall_calls[0]
    assert call["query"] == "what about sourdough"
    assert call["session_id"] == "t-9"
    assert call["user"] is fake_users["assistant-a-2@email-agent.local"]
    assert call["query_type"] == SearchType.CHUNKS
    assert call["only_context"] is True
    assert {m.content for m in ctx.memories} == {"sourdough is great", "user prefers detail"}


@pytest.mark.asyncio
async def test_recall_returns_empty_on_no_data_error(
    fake_cognee: _FakeCognee, fake_users: _FakeUserStore
) -> None:
    """Fresh user → vector DB has no chunks → cognee raises NoDataError.
    The adapter should treat this as 'no memories yet', not a failure."""
    from cognee.modules.retrieval.exceptions.exceptions import NoDataError

    fake_cognee.recall_side_effect = NoDataError("No data found in the system")
    adapter = CogneeMemoryAdapter()

    ctx = await adapter.recall("a-fresh", "t-1", "anything")

    assert ctx.memories == []


@pytest.mark.asyncio
async def test_search_omits_session_id(
    fake_cognee: _FakeCognee, fake_users: _FakeUserStore
) -> None:
    """search() is the cross-thread variant — it must NOT pin to a session."""
    fake_cognee.recall_returns = [{"search_result": "across-thread fact", "_source": "graph"}]
    adapter = CogneeMemoryAdapter()

    hits = await adapter.search("a-1", "fact")

    assert len(fake_cognee.recall_calls) == 1
    assert "session_id" not in fake_cognee.recall_calls[0]
    assert [m.content for m in hits] == ["across-thread fact"]


@pytest.mark.asyncio
async def test_delete_assistant_drops_user_and_data(
    fake_cognee: _FakeCognee, fake_users: _FakeUserStore
) -> None:
    """delete_assistant must call cognee.forget(everything=True, user=...)
    AND cognee's delete_user — leaving zero residue for that assistant."""
    adapter = CogneeMemoryAdapter()
    await adapter.record_turn("a-1", "t-1", "user", "remember me")  # creates user

    await adapter.delete_assistant("a-1")

    assert len(fake_cognee.forget_calls) == 1
    forget = fake_cognee.forget_calls[0]
    assert forget["everything"] is True
    assert forget["user"].email == "assistant-a-1@email-agent.local"
    assert fake_users.delete_log == ["assistant-a-1@email-agent.local"]
    # Cache cleared so a future call re-resolves.
    fake_cognee.recall_returns = []
    await adapter.recall("a-1", "t-1", "anything")
    # The recall lookup either re-creates or reuses the existing record;
    # either way the cache shouldn't keep returning the deleted instance.
