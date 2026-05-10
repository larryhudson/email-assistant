from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProjectedFile(_Frozen):
    """A file the runtime hands to the sandbox before a run.

    Used for both the email-thread projection (`/workspace/emails/...`,
    read-only) and per-run attachments. `path` is sandbox-relative.
    """

    path: str
    content: bytes


class BashResult(_Frozen):
    """Captured output of a single `bash` tool call inside the sandbox."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


class PendingAttachment(_Frozen):
    """A file the agent has marked to attach to its outbound reply.

    Recorded by the `attach_file` tool during the run; the runtime reads the
    bytes back out of the sandbox after `agent.run()` returns and includes
    them in the outbound email envelope.
    """

    sandbox_path: str
    filename: str


class ToolCall(_Frozen):
    """A tool invocation routed from the agent into the sandbox.

    A single discriminated type instead of one class per tool, because tool
    dispatch is a thin shim and a sum-of-fields model keeps the wire format
    simple. `model_post_init` enforces which fields are required per `kind`.
    """

    kind: Literal["read", "write", "edit", "bash", "attach_file"]
    path: str | None = None
    content: str | None = None
    old: str | None = None
    new: str | None = None
    command: str | None = None
    filename: str | None = None

    def model_post_init(self, _ctx) -> None:
        required = {
            "read": ("path",),
            "write": ("path", "content"),
            "edit": ("path", "old", "new"),
            "bash": ("command",),
            "attach_file": ("path",),
        }[self.kind]
        for field in required:
            if getattr(self, field) is None:
                raise ValueError(f"{self.kind} tool call requires {field}")


class ToolResult(_Frozen):
    """Outcome of a `ToolCall`, regardless of which tool ran.

    `ok=False` means the tool refused or the operation failed; `error` carries
    the human-readable reason. For `bash`, `output` is a `BashResult`; for
    `read`, the file's text contents; otherwise `None`.
    """

    ok: bool
    output: BashResult | str | None = None
    error: str | None = None
