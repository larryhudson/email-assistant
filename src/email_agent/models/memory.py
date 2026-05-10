from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Memory(BaseModel):
    """A single durable memory retrieved from the memory adapter (Cognee in prod).

    `content` is the human-readable snippet. `source_run_id` links back to the
    run that produced it, when known. `score` is adapter-provided relevance.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    content: str
    source_run_id: str | None = None
    score: float | None = None


class MemoryContext(BaseModel):
    """Bundle of memories the runtime injects into the agent's prompt.

    Built by `MemoryPort.recall` once per run before the agent starts, so the
    model has prior context without needing to call `memory_search` first.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    memories: list[Memory] = Field(default_factory=list)
    retrieved_at: datetime
