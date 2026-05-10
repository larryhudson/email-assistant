"""Unit tests for CogneeMemoryAdapter.

We don't run real cognee here (no LLM/embedding key, slow). Instead we
monkeypatch the cognee module-level functions and verify the adapter:

1. Sets per-assistant data_root_directory + system_root_directory BEFORE
   calling cognee.remember / cognee.recall.
2. Holds a shared asyncio.Lock across the call so concurrent invocations
   for different assistants serialize (cognee.config is module-global).

Real round-trip + isolation are covered by the gated integration test.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from email_agent.memory.cognee import CogneeMemoryAdapter


class _FakeConfig:
    def __init__(self) -> None:
        self.data_root: str | None = None
        self.system_root: str | None = None

    def data_root_directory(self, path: str) -> None:
        self.data_root = path

    def system_root_directory(self, path: str) -> None:
        self.system_root = path


class _FakeCognee:
    """Records the config state observed at the moment of each call.

    `recall_returns` should be a list of dicts of shape
    `{"search_result": "...", "_source": "graph", ...}` matching cognee's
    `query_type=CHUNKS, only_context=True` response envelope.
    """

    def __init__(self) -> None:
        self.config = _FakeConfig()
        self.remember_calls: list[tuple[str, str | None, str | None]] = []
        self.recall_calls: list[dict] = []
        self.recall_returns: list[dict] = []

    async def remember(self, text: str, *, session_id: str | None = None) -> None:
        self.remember_calls.append((text, session_id, self.config.data_root))

    async def recall(self, query: str, **kwargs) -> list[dict]:
        self.recall_calls.append({"query": query, "data_root": self.config.data_root, **kwargs})
        return list(self.recall_returns)


@pytest.fixture
def fake_cognee(monkeypatch: pytest.MonkeyPatch) -> _FakeCognee:
    fake = _FakeCognee()
    import email_agent.memory.cognee as mod

    monkeypatch.setattr(mod, "cognee", fake)
    return fake


@pytest.mark.asyncio
async def test_record_turn_swaps_config_before_calling_remember(
    fake_cognee: _FakeCognee, tmp_path: Path
) -> None:
    adapter = CogneeMemoryAdapter(data_root=tmp_path)
    await adapter.record_turn("a-1", "t-1", "user", "I love sourdough")

    assert len(fake_cognee.remember_calls) == 1
    text, session_id, observed_root = fake_cognee.remember_calls[0]
    assert "sourdough" in text
    assert session_id == "t-1"
    # The data_root_directory must already be the per-assistant path at the
    # moment cognee.remember runs.
    assert observed_root == str((tmp_path / "a-1" / "data").resolve())


@pytest.mark.asyncio
async def test_recall_returns_memory_context_with_per_assistant_root(
    fake_cognee: _FakeCognee, tmp_path: Path
) -> None:
    from cognee.modules.search.types import SearchType

    fake_cognee.recall_returns = [
        {"search_result": "sourdough is great", "_source": "graph"},
        {"search_result": "user prefers detail", "_source": "graph"},
    ]
    adapter = CogneeMemoryAdapter(data_root=tmp_path)

    ctx = await adapter.recall("a-2", "t-9", "what about sourdough")

    assert len(fake_cognee.recall_calls) == 1
    call = fake_cognee.recall_calls[0]
    assert call["query"] == "what about sourdough"
    assert call["session_id"] == "t-9"
    assert call["data_root"] == str((tmp_path / "a-2" / "data").resolve())
    # Raw chunks, not LLM-synthesized answers — query_type=CHUNKS, only_context=True
    assert call["query_type"] == SearchType.CHUNKS
    assert call["only_context"] is True
    assert {m.content for m in ctx.memories} == {
        "sourdough is great",
        "user prefers detail",
    }


@pytest.mark.asyncio
async def test_concurrent_calls_for_different_assistants_serialize(
    fake_cognee: _FakeCognee, tmp_path: Path
) -> None:
    """cognee.config is module-global, so two calls overlapping in time
    would clobber each other's roots. The adapter's lock must serialize."""

    overlap = {"max_concurrent": 0, "current": 0}

    async def slow_recall(query: str, **kwargs) -> list[dict]:
        overlap["current"] += 1
        overlap["max_concurrent"] = max(overlap["max_concurrent"], overlap["current"])
        await asyncio.sleep(0.01)
        overlap["current"] -= 1
        return []

    fake_cognee.recall = slow_recall  # ty: ignore[invalid-assignment]

    adapter = CogneeMemoryAdapter(data_root=tmp_path)
    await asyncio.gather(
        adapter.recall("a-1", "t-1", "q1"),
        adapter.recall("a-2", "t-2", "q2"),
        adapter.recall("a-3", "t-3", "q3"),
    )

    assert overlap["max_concurrent"] == 1


@pytest.mark.asyncio
async def test_search_passes_no_session_id(fake_cognee: _FakeCognee, tmp_path: Path) -> None:
    fake_cognee.recall_returns = [{"search_result": "across-thread fact", "_source": "graph"}]
    adapter = CogneeMemoryAdapter(data_root=tmp_path)

    hits = await adapter.search("a-1", "fact")

    assert len(fake_cognee.recall_calls) == 1
    call = fake_cognee.recall_calls[0]
    assert "session_id" not in call
    assert [m.content for m in hits] == ["across-thread fact"]


@pytest.mark.asyncio
async def test_delete_assistant_wipes_only_that_assistant_root(
    fake_cognee: _FakeCognee, tmp_path: Path
) -> None:
    (tmp_path / "a-1" / "data").mkdir(parents=True)
    (tmp_path / "a-1" / "data" / "marker").write_text("x")
    (tmp_path / "a-2" / "data").mkdir(parents=True)
    (tmp_path / "a-2" / "data" / "marker").write_text("y")

    adapter = CogneeMemoryAdapter(data_root=tmp_path)
    await adapter.delete_assistant("a-1")

    assert not (tmp_path / "a-1").exists()
    assert (tmp_path / "a-2" / "data" / "marker").read_text() == "y"
