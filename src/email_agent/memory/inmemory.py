from datetime import UTC, datetime
from itertools import count

from email_agent.models.memory import Memory, MemoryContext


class InMemoryMemoryAdapter:
    """Per-assistant scoped in-memory store. Recall/search are simple substring
    matches — good enough for tests and to enforce the isolation contract."""

    def __init__(self) -> None:
        self._by_assistant: dict[str, list[Memory]] = {}
        self._counter = count(1)

    async def record_turn(self, assistant_id: str, thread_id: str, role: str, content: str) -> None:
        bucket = self._by_assistant.setdefault(assistant_id, [])
        bucket.append(
            Memory(
                id=f"mem-{next(self._counter)}",
                content=f"[{thread_id}/{role}] {content}",
            )
        )

    async def recall(self, assistant_id: str, thread_id: str, query: str) -> MemoryContext:
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
