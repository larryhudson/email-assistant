from dataclasses import dataclass, field
from typing import Protocol

from email_agent.models.memory import Memory
from email_agent.models.sandbox import PendingAttachment, ToolCall, ToolResult


class _SandboxLike(Protocol):
    async def run_tool(self, assistant_id: str, run_id: str, call: ToolCall) -> ToolResult: ...


class _MemoryLike(Protocol):
    async def search(self, assistant_id: str, query: str) -> list[Memory]: ...


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
    sandbox: _SandboxLike
    memory: _MemoryLike
    pending_attachments: list[PendingAttachment] = field(default_factory=list)


@dataclass(frozen=True)
class RunUsage:
    """Token counts + estimated cost for one agent run.

    Sourced from `pydantic_ai.RunResult.usage()`. Cost is computed on top by
    the runtime since PydanticAI doesn't price per-model.
    """

    input_tokens: int
    output_tokens: int
    cost_cents: int


@dataclass(frozen=True)
class RunStepRecord:
    """One persisted step (tool call / model response) for the admin trace."""

    kind: str
    input_summary: str
    output_summary: str
    cost_cents: int = 0


@dataclass(frozen=True)
class AgentResult:
    """What `AssistantAgent.run` returns to the runtime."""

    body: str
    usage: RunUsage


__all__ = [
    "AgentDeps",
    "AgentResult",
    "RunStepRecord",
    "RunUsage",
]
