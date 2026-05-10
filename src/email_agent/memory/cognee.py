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
        async with self._scope(assistant_id):
            results = await cognee.recall(query, session_id=thread_id)
        return MemoryContext(
            memories=[self._to_memory(r) for r in results],
            retrieved_at=datetime.now(UTC),
        )

    async def search(self, assistant_id: str, query: str) -> list[Memory]:
        async with self._scope(assistant_id):
            results = await cognee.recall(query)
        return [self._to_memory(r) for r in results]

    async def delete_assistant(self, assistant_id: str) -> None:
        target = self._assistant_root(assistant_id)
        async with self._lock:
            if target.exists():
                shutil.rmtree(target)

    def _scope(self, assistant_id: str) -> _AssistantScope:
        return _AssistantScope(self._lock, self._assistant_root(assistant_id))

    def _assistant_root(self, assistant_id: str) -> Path:
        return self._data_root / assistant_id

    def _to_memory(self, raw: Any) -> Memory:
        content = raw if isinstance(raw, str) else getattr(raw, "content", str(raw))
        return Memory(id=f"mem-{next(self._counter)}", content=content)


class _AssistantScope:
    """Context manager: hold the lock and point cognee.config at this assistant."""

    def __init__(self, lock: asyncio.Lock, assistant_root: Path) -> None:
        self._lock = lock
        self._assistant_root = assistant_root

    async def __aenter__(self) -> None:
        await self._lock.acquire()
        data_dir = self._assistant_root / "data"
        system_dir = self._assistant_root / "system"
        data_dir.mkdir(parents=True, exist_ok=True)
        system_dir.mkdir(parents=True, exist_ok=True)
        cognee.config.data_root_directory(str(data_dir))
        cognee.config.system_root_directory(str(system_dir))

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._lock.release()
