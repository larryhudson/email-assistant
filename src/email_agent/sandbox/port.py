from typing import Protocol, runtime_checkable

from email_agent.models.sandbox import ProjectedFile, ToolCall, ToolResult


@runtime_checkable
class AssistantSandbox(Protocol):
    """Boundary for the per-assistant execution environment.

    Legacy workflow-specific sandbox surface. The active runtime path uses
    `AssistantWorkspace` plus a `SandboxEnvironment`; this protocol remains
    for older in-memory tests.
    """

    async def ensure_started(self, assistant_id: str) -> None: ...

    async def project_emails(self, assistant_id: str, files: list[ProjectedFile]) -> None: ...

    async def project_attachments(
        self, assistant_id: str, run_id: str, files: list[ProjectedFile]
    ) -> None: ...

    async def run_tool(self, assistant_id: str, run_id: str, call: ToolCall) -> ToolResult: ...

    async def read_attachment_out(self, assistant_id: str, run_id: str, path: str) -> bytes: ...

    async def reset(self, assistant_id: str) -> None: ...
