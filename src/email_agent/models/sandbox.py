from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProjectedFile(_Frozen):
    path: str
    content: bytes


class BashResult(_Frozen):
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


class PendingAttachment(_Frozen):
    sandbox_path: str
    filename: str


class ToolCall(_Frozen):
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
    ok: bool
    output: BashResult | str | None = None
    error: str | None = None
