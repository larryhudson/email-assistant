import shlex
import subprocess
import time
from pathlib import PurePosixPath

from email_agent.models.sandbox import (
    BashResult,
    ProjectedFile,
    ToolCall,
    ToolResult,
)

WORKSPACE_ROOT = "/workspace"
EMAILS_PREFIX = "/workspace/emails/"


class InMemorySandbox:
    """In-process sandbox for tests. Filesystem is a per-assistant dict.
    `bash` runs on the host via subprocess — fine for tests, not for prod."""

    def __init__(self) -> None:
        self._fs: dict[str, dict[str, bytes]] = {}
        self._attachments: dict[tuple[str, str], dict[str, bytes]] = {}
        self._started: set[str] = set()

    async def ensure_started(self, assistant_id: str) -> None:
        self._started.add(assistant_id)
        self._fs.setdefault(assistant_id, {})

    async def project_emails(self, assistant_id: str, files: list[ProjectedFile]) -> None:
        self._require_started(assistant_id)
        fs = self._fs[assistant_id]
        for k in list(fs):
            if k.startswith(EMAILS_PREFIX):
                del fs[k]
        for f in files:
            full = self._normalize(f"emails/{_strip_leading(f.path, 'emails/')}")
            fs[full] = f.content

    async def project_attachments(
        self, assistant_id: str, run_id: str, files: list[ProjectedFile]
    ) -> None:
        self._require_started(assistant_id)
        bucket = self._attachments.setdefault((assistant_id, run_id), {})
        for f in files:
            bucket[f.path] = f.content

    async def run_tool(self, assistant_id: str, run_id: str, call: ToolCall) -> ToolResult:
        self._require_started(assistant_id)
        fs = self._fs[assistant_id]
        match call.kind:
            case "read":
                path = self._normalize(call.path or "")
                if path not in fs:
                    return ToolResult(ok=False, error=f"not found: {path}")
                return ToolResult(ok=True, output=fs[path].decode())
            case "write":
                path = self._normalize(call.path or "")
                if path.startswith(EMAILS_PREFIX):
                    return ToolResult(ok=False, error=f"{EMAILS_PREFIX} is read-only")
                fs[path] = (call.content or "").encode()
                return ToolResult(ok=True)
            case "edit":
                path = self._normalize(call.path or "")
                if path.startswith(EMAILS_PREFIX):
                    return ToolResult(ok=False, error=f"{EMAILS_PREFIX} is read-only")
                if path not in fs:
                    return ToolResult(ok=False, error=f"not found: {path}")
                old, new = call.old or "", call.new or ""
                content = fs[path].decode()
                if old not in content:
                    return ToolResult(ok=False, error="old string not found")
                fs[path] = content.replace(old, new, 1).encode()
                return ToolResult(ok=True)
            case "bash":
                t0 = time.monotonic()
                proc = subprocess.run(  # noqa: ASYNC221
                    shlex.split(call.command or ""),
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                dur = int((time.monotonic() - t0) * 1000)
                return ToolResult(
                    ok=proc.returncode == 0,
                    output=BashResult(
                        exit_code=proc.returncode,
                        stdout=proc.stdout,
                        stderr=proc.stderr,
                        duration_ms=dur,
                    ),
                )
            case "attach_file":
                bucket = self._attachments.setdefault((assistant_id, run_id), {})
                path = self._normalize(call.path or "")
                if path not in fs:
                    return ToolResult(ok=False, error=f"not found: {path}")
                fname = call.filename or PurePosixPath(path).name
                bucket[fname] = fs[path]
                return ToolResult(ok=True)

    async def read_attachment_out(self, assistant_id: str, run_id: str, path: str) -> bytes:
        return self._attachments[(assistant_id, run_id)][path]

    async def reset(self, assistant_id: str) -> None:
        self._fs.pop(assistant_id, None)
        for key in [k for k in self._attachments if k[0] == assistant_id]:
            self._attachments.pop(key, None)
        self._started.discard(assistant_id)

    def _require_started(self, assistant_id: str) -> None:
        if assistant_id not in self._started:
            raise RuntimeError(f"sandbox for {assistant_id} not started")

    @staticmethod
    def _normalize(path: str) -> str:
        if path.startswith(WORKSPACE_ROOT):
            return path
        return f"{WORKSPACE_ROOT}/{path.lstrip('/')}"


def _strip_leading(s: str, prefix: str) -> str:
    return s[len(prefix) :] if s.startswith(prefix) else s
