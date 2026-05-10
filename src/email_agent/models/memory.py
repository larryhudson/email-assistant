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
