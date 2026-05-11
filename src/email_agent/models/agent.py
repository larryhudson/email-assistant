from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from email_agent.models.memory import Memory
from email_agent.models.sandbox import PendingAttachment


class _ToolsetLike(Protocol):
    async def read(self, path: str) -> str: ...

    async def write(self, path: str, content: str) -> str: ...

    async def edit(self, path: str, old: str, new: str) -> str: ...

    async def bash(self, command: str, *, timeout_s: int | None = None) -> str: ...

    async def attach_file(self, path: str, filename: str | None = None) -> str: ...

    async def memory_search(self, query: str) -> list[Memory]: ...


@dataclass
class AgentDeps:
    """Per-run state the PydanticAI agent's tool callbacks see via RunContext.

    Mutable on purpose: `pending_attachments` is appended by the `attach_file`
    tool during the run, then read by the runtime after `agent.run()` returns
    to pull bytes out of the sandbox and stitch them into the outbound email.
    """

    assistant_id: str
    run_id: str
    thread_id: str
    toolset: _ToolsetLike
    pending_attachments: list[PendingAttachment] = field(default_factory=list)
    skills_block: str = ""
    context_block: str = ""


@dataclass(frozen=True)
class RunUsage:
    """Token counts + estimated cost for one agent run.

    Sourced from `pydantic_ai.RunResult.usage()`. Cost is computed on top by
    the runtime since PydanticAI doesn't price per-model.
    """

    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


@dataclass(frozen=True)
class RunStepRecord:
    """One persisted step (tool call / model response) for the admin trace."""

    kind: str
    input_summary: str
    output_summary: str
    cost_usd: Decimal = Decimal("0")


@dataclass(frozen=True)
class AgentResult:
    """What `AssistantAgent.run` returns to the runtime."""

    body: str
    usage: RunUsage
    steps: list["RunStepRecord"] = field(default_factory=list)


__all__ = [
    "AgentDeps",
    "AgentResult",
    "RunStepRecord",
    "RunUsage",
]
