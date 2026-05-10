from typing import Protocol, runtime_checkable

from email_agent.models.sandbox import ProjectedFile, ToolCall, ToolResult


@runtime_checkable
class AssistantSandbox(Protocol):
    """Boundary for the per-assistant execution environment.

    `DockerSandbox` (later slice) runs tool calls inside a long-lived
    container; `InMemorySandbox` (tests) runs them in-process. Every method
    takes `assistant_id` so isolation is checkable at the seam.
    """

    async def ensure_started(self, assistant_id: str) -> None: ...

    async def project_emails(self, assistant_id: str, files: list[ProjectedFile]) -> None: ...

    async def project_attachments(
        self, assistant_id: str, run_id: str, files: list[ProjectedFile]
    ) -> None: ...

    async def run_tool(self, assistant_id: str, run_id: str, call: ToolCall) -> ToolResult: ...

    async def read_attachment_out(self, assistant_id: str, run_id: str, path: str) -> bytes: ...

    async def reset(self, assistant_id: str) -> None: ...
