from typing import Protocol, runtime_checkable

from email_agent.models.memory import Memory, MemoryContext


@runtime_checkable
class MemoryPort(Protocol):
    """Boundary for durable memory storage (Cognee in prod; in-memory in tests).

    Every method takes `assistant_id` — adapters must enforce per-assistant
    isolation and never return memory from another assistant.
    """

    async def recall(self, assistant_id: str, thread_id: str, query: str) -> MemoryContext: ...

    async def record_turn(
        self, assistant_id: str, thread_id: str, role: str, content: str
    ) -> None: ...

    async def search(self, assistant_id: str, query: str) -> list[Memory]: ...

    async def delete_assistant(self, assistant_id: str) -> None: ...
