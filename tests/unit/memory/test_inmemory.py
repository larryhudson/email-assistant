import pytest

from email_agent.memory.inmemory import InMemoryMemoryAdapter


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
