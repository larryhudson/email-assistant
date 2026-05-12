from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from email_agent.models.memory import Memory
from email_agent.models.sandbox import PendingAttachment
from email_agent.models.scheduled import ScheduledTask


class _ToolsetLike(Protocol):
    async def read(self, path: str) -> str: ...

    async def write(self, path: str, content: str) -> str: ...

    async def edit(self, path: str, old: str, new: str) -> str: ...

    async def bash(self, command: str, *, timeout_s: int | None = None) -> str: ...

    async def attach_file(self, path: str, filename: str | None = None) -> str: ...

    async def memory_search(self, query: str) -> list[Memory] | str: ...

    async def list_scheduled_tasks(self) -> list[ScheduledTask]: ...

    async def create_scheduled_task(self, kind: str, when: str, name: str, body: str) -> str: ...

    async def delete_scheduled_task(self, task_id: str) -> str: ...


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


class AgentRunError(Exception):
    """Raised by `AssistantAgent.run` when the underlying pydantic-ai run
    raised. Carries whatever usage + step trace was accumulated before the
    failure so the recorder can persist them — otherwise an exception after
    N tool calls would silently drop the cost and the trace.
    """

    def __init__(
        self,
        original: BaseException,
        *,
        usage: RunUsage,
        steps: list[RunStepRecord],
    ) -> None:
        super().__init__(str(original))
        self.original = original
        self.usage = usage
        self.steps = steps


__all__ = [
    "AgentDeps",
    "AgentResult",
    "AgentRunError",
    "RunStepRecord",
    "RunUsage",
]
