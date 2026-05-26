from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from pydantic_ai import ToolReturn
from pydantic_ai.messages import ModelMessage

from email_agent.google_workspace.port import (
    GoogleCalendarDeleteResult,
    GoogleCalendarEventResult,
    GoogleCalendarEventsResult,
    GoogleCalendarFreeBusyResult,
    GoogleCalendarListResult,
)
from email_agent.models.memory import Memory
from email_agent.models.sandbox import PendingAttachment
from email_agent.models.scheduled import ScheduledTask


class _ToolsetLike(Protocol):
    async def read(self, path: str) -> str: ...

    async def read_image(self, path: str) -> ToolReturn | str: ...

    async def write(self, path: str, content: str) -> str: ...

    async def edit(self, path: str, old: str, new: str) -> str: ...

    async def bash(self, command: str, *, timeout_s: int | None = None) -> str: ...

    async def attach_file(self, path: str, filename: str | None = None) -> str: ...

    async def generate_pdf(self, html_path: str, output_path: str | None = None) -> str: ...

    async def preview_pdf(
        self, pdf_path: str, page: int = 1, dpi: int = 160
    ) -> ToolReturn | str: ...

    async def pandoc(
        self,
        args: list[str],
        input_paths: list[str],
        output_paths: list[str],
        timeout_s: int | None = None,
    ) -> str: ...

    async def soffice(
        self,
        args: list[str],
        input_paths: list[str],
        output_paths: list[str],
        timeout_s: int | None = None,
    ) -> str: ...

    async def python_docx(
        self,
        path: str,
        operations: list[dict],
        output_path: str | None = None,
    ) -> str: ...

    async def memory_search(self, query: str) -> list[Memory] | str: ...

    async def web_search(self, query: str, max_results: int = 5) -> str: ...

    async def list_github_repositories(self) -> str: ...

    async def clone_github_repository(
        self, repository: str, destination_path: str | None = None
    ) -> str: ...

    async def calendar_list_calendars(self) -> GoogleCalendarListResult | str: ...

    async def calendar_list_events(
        self,
        calendar_id: str = "primary",
        time_min: datetime | None = None,
        time_max: datetime | None = None,
        query: str | None = None,
        max_results: int = 50,
    ) -> GoogleCalendarEventsResult | str: ...

    async def calendar_get_event(
        self, calendar_id: str, event_id: str
    ) -> GoogleCalendarEventResult | str: ...

    async def calendar_check_free_busy(
        self,
        calendar_ids: list[str],
        time_min: datetime,
        time_max: datetime,
    ) -> GoogleCalendarFreeBusyResult | str: ...

    async def calendar_create_event(
        self,
        calendar_id: str,
        summary: str,
        start: datetime,
        end: datetime,
        description: str | None = None,
        location: str | None = None,
        attendees: list[str] | None = None,
    ) -> GoogleCalendarEventResult | str: ...

    async def calendar_update_event(
        self,
        calendar_id: str,
        event_id: str,
        summary: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        description: str | None = None,
        location: str | None = None,
        attendees: list[str] | None = None,
    ) -> GoogleCalendarEventResult | str: ...

    async def calendar_delete_event(
        self, calendar_id: str, event_id: str
    ) -> GoogleCalendarDeleteResult | str: ...

    async def list_scheduled_tasks(self) -> list[ScheduledTask]: ...

    async def create_scheduled_task(
        self,
        kind: str,
        when: str,
        name: str,
        body: str,
        command: str | None = None,
        is_agent_enabled: bool = True,
        max_unanswered_runs: int | None = 3,
    ) -> str: ...

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
    metered_usage: list["MeteredUsage"] = field(default_factory=list)
    record_step: Callable[["RunStepRecord"], Awaitable[None]] | None = None
    skills_block: str = ""
    context_block: str = ""
    participants_block: str = ""
    identity_block: str = ""


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
class MeteredUsage:
    """Non-token billable usage attached to an agent run.

    Examples: one Brave web search request. `input_tokens` and
    `output_tokens` stay zero for request-metered tools so the ledger
    remains explicit about what kind of usage was charged.
    """

    provider: str
    model: str
    cost_usd: Decimal
    input_tokens: int = 0
    output_tokens: int = 0
    tool_name: str | None = None


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
    metered_usage: list[MeteredUsage] = field(default_factory=list)
    # `result.all_messages()` from the underlying pydantic-ai run, kept so the
    # runtime can persist it on `agent_runs.message_history` and later thread
    # it into the next same-thread run.
    message_history: list[ModelMessage] = field(default_factory=list)


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
        metered_usage: list[MeteredUsage] | None = None,
    ) -> None:
        super().__init__(str(original))
        self.original = original
        self.usage = usage
        self.steps = steps
        self.metered_usage = metered_usage or []


__all__ = [
    "AgentDeps",
    "AgentResult",
    "AgentRunError",
    "MeteredUsage",
    "RunStepRecord",
    "RunUsage",
]
