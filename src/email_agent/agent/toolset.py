from pathlib import PurePosixPath
from typing import Protocol

from email_agent.models.memory import Memory
from email_agent.models.sandbox import PendingAttachment
from email_agent.sandbox.environment import SandboxEnvironment
from email_agent.sandbox.workspace import AssistantWorkspace, WorkspacePolicyError


class _MemoryLike(Protocol):
    async def search(self, assistant_id: str, query: str) -> list[Memory]: ...


class AgentToolset:
    """Model-visible email-agent tools backed by an assistant workspace."""

    def __init__(
        self,
        *,
        assistant_id: str,
        run_id: str,
        env: SandboxEnvironment,
        workspace: AssistantWorkspace,
        memory: _MemoryLike,
        pending_attachments: list[PendingAttachment],
    ) -> None:
        self._assistant_id = assistant_id
        self._run_id = run_id
        self._env = env
        self._workspace = workspace
        self._memory = memory
        self._pending_attachments = pending_attachments

    async def read(self, path: str) -> str:
        try:
            return await self._env.read_text(path)
        except FileNotFoundError:
            return _tool_error("read", f"not found: {path}", detail=path)
        except Exception as exc:
            return _tool_error("read", str(exc), detail=path)

    async def write(self, path: str, content: str) -> str:
        try:
            await self._workspace.assert_agent_write_allowed(path)
            await self._env.write_text(path, content)
        except WorkspacePolicyError as exc:
            return _tool_error("write", str(exc), detail=path)
        except Exception as exc:
            return _tool_error("write", str(exc), detail=path)
        return f"wrote {path}"

    async def edit(self, path: str, old: str, new: str) -> str:
        try:
            await self._workspace.assert_agent_write_allowed(path)
            current = await self._env.read_text(path)
            if old not in current:
                return _tool_error("edit", "old string not found", detail=path)
            await self._env.write_text(path, current.replace(old, new, 1))
        except WorkspacePolicyError as exc:
            return _tool_error("edit", str(exc), detail=path)
        except Exception as exc:
            return _tool_error("edit", str(exc), detail=path)
        return f"edited {path}"

    async def bash(self, command: str, *, timeout_s: int | None = None) -> str:
        try:
            result = await self._env.exec(command, timeout_s=timeout_s)
        except Exception as exc:
            return _tool_error("bash", str(exc), detail=command)
        return f"exit_code={result.exit_code}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    async def attach_file(self, path: str, filename: str | None = None) -> str:
        try:
            if not await self._env.exists(path):
                return _tool_error("attach_file", f"attach_file: {path} not found", detail=path)
            self._pending_attachments.append(
                PendingAttachment(
                    sandbox_path=path,
                    filename=filename or PurePosixPath(path).name,
                )
            )
        except Exception as exc:
            return _tool_error("attach_file", str(exc), detail=path)
        return f"attached {path}"

    async def memory_search(self, query: str) -> list[Memory]:
        return await self._memory.search(self._assistant_id, query)

    @property
    def run_id(self) -> str:
        return self._run_id


def _tool_error(tool_name: str, error: str, *, detail: str | None = None) -> str:
    subject = f"{tool_name}({detail})" if detail else tool_name
    return f"ERROR: {subject} failed\n{error or 'unknown error'}"


__all__ = ["AgentToolset"]
