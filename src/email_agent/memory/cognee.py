"""Cognee-backed MemoryPort adapter.

Cognee's `data_root_directory` and `system_root_directory` are module-global,
so per-assistant isolation is achieved by switching them under a process-wide
`asyncio.Lock` around every cognee call. `curate_memory` jobs and admin reads
share the same lock.

The agent never gets a write-side memory tool — durable memory writes happen
out-of-band after the run completes (curate_memory + cognee session traces),
where the curation step can see the whole turn.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any

import cognee
from cognee.modules.search.types import SearchType

from email_agent.models.memory import Memory, MemoryContext


class CogneeMemoryAdapter:
    """`MemoryPort` adapter backed by cognee.remember / cognee.recall.

    Per-assistant data is rooted at `<data_root>/<assistant_id>/data` and
    `<data_root>/<assistant_id>/system`. The adapter holds a process-wide
    `asyncio.Lock` and switches cognee's module-global config under that
    lock around every public call.
    """

    def __init__(self, *, data_root: Path) -> None:
        self._data_root = Path(data_root)
        self._lock = asyncio.Lock()
        self._counter = count(1)

    async def record_turn(self, assistant_id: str, thread_id: str, role: str, content: str) -> None:
        text = f"[{role}] {content}"
        async with self._scope(assistant_id):
            await cognee.remember(text, session_id=thread_id)

    async def recall(self, assistant_id: str, thread_id: str, query: str) -> MemoryContext:
        # We want raw retrieved chunks injected into the agent's prompt — NOT
        # cognee's LLM-synthesized answer. `query_type=CHUNKS` returns the
        # actual ingested text segments; `only_context=True` is redundant for
        # CHUNKS but kept as a safety belt against future API drift.
        async with self._scope(assistant_id):
            results = await cognee.recall(
                query,
                session_id=thread_id,
                query_type=SearchType.CHUNKS,
                only_context=True,
            )
        return MemoryContext(
            memories=[self._to_memory(r) for r in results],
            retrieved_at=datetime.now(UTC),
        )

    async def search(self, assistant_id: str, query: str) -> list[Memory]:
        async with self._scope(assistant_id):
            results = await cognee.recall(
                query,
                query_type=SearchType.CHUNKS,
                only_context=True,
            )
        return [self._to_memory(r) for r in results]

    async def seed_durable(self, assistant_id: str, content: str) -> None:
        """Seed a fact into the assistant's durable graph (no session id).

        Not part of `MemoryPort` — this is an ops affordance for the
        `seed-memory` CLI. Stores via `cognee.remember(content)` (no
        session_id), so the fact lands in the per-assistant graph and is
        recallable from any thread.
        """
        async with self._scope(assistant_id):
            await cognee.remember(content)

    async def delete_assistant(self, assistant_id: str) -> None:
        target = self._assistant_root(assistant_id)
        async with self._lock:
            if target.exists():
                shutil.rmtree(target)

    def _scope(self, assistant_id: str) -> _AssistantScope:
        return _AssistantScope(self._lock, self._assistant_root(assistant_id))

    def _assistant_root(self, assistant_id: str) -> Path:
        return self._data_root / assistant_id

    def _to_memory(self, raw: dict[str, Any]) -> Memory:
        # cognee.recall(query_type=CHUNKS, only_context=True) returns dicts of
        # shape {dataset_id, dataset_name, dataset_tenant_id, search_result, _source}.
        # `search_result` is the raw chunk text we want to inject into the prompt.
        return Memory(id=f"mem-{next(self._counter)}", content=raw["search_result"])


class _AssistantScope:
    """Context manager: hold the lock and point cognee.config at this assistant."""

    def __init__(self, lock: asyncio.Lock, assistant_root: Path) -> None:
        self._lock = lock
        self._assistant_root = assistant_root

    async def __aenter__(self) -> None:
        await self._lock.acquire()
        # Cognee's data ingestion builds file:// URIs from data_root_directory,
        # which fails on relative paths. Resolve to absolute before the swap.
        data_dir = (self._assistant_root / "data").resolve()
        system_dir = (self._assistant_root / "system").resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        system_dir.mkdir(parents=True, exist_ok=True)
        cognee.config.data_root_directory(str(data_dir))
        cognee.config.system_root_directory(str(system_dir))

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._lock.release()
