from datetime import datetime
from pathlib import PurePosixPath
from typing import Protocol

from email_agent.models.memory import Memory
from email_agent.models.sandbox import PendingAttachment
from email_agent.models.scheduled import ScheduledTask
from email_agent.sandbox.environment import SandboxEnvironment
from email_agent.sandbox.workspace import AssistantWorkspace, WorkspacePolicyError


class _MemoryLike(Protocol):
    async def search(self, assistant_id: str, query: str) -> list[Memory]: ...


class _ScheduledTasksLike(Protocol):
    async def create_once(
        self,
        *,
        assistant_id: str,
        run_at: datetime,
        subject: str,
        body: str,
        created_by_run_id: str | None = None,
    ) -> ScheduledTask: ...

    async def create_cron(
        self,
        *,
        assistant_id: str,
        cron_expr: str,
        subject: str,
        body: str,
        created_by_run_id: str | None = None,
    ) -> ScheduledTask: ...

    async def list_for_assistant(self, assistant_id: str) -> list[ScheduledTask]: ...

    async def delete(self, *, assistant_id: str, task_id: str) -> bool: ...


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
        scheduled_tasks: _ScheduledTasksLike | None = None,
    ) -> None:
        self._assistant_id = assistant_id
        self._run_id = run_id
        self._env = env
        self._workspace = workspace
        self._memory = memory
        self._pending_attachments = pending_attachments
        self._scheduled_tasks = scheduled_tasks

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

    async def list_scheduled_tasks(self) -> list[ScheduledTask]:
        if self._scheduled_tasks is None:
            return []
        return await self._scheduled_tasks.list_for_assistant(self._assistant_id)

    async def create_scheduled_task(self, kind: str, when: str, subject: str, body: str) -> str:
        """Create a scheduled synthetic-inbound task for this assistant.

        `kind` is 'once' or 'cron'. For 'once', `when` is an ISO-8601
        timezone-aware datetime; for 'cron', `when` is a 5-field cron
        expression. `subject` + `body` populate the synthetic inbound.
        """
        if self._scheduled_tasks is None:
            return _tool_error("create_scheduled_task", "scheduled tasks not configured")
        if not subject:
            return _tool_error("create_scheduled_task", "subject must not be empty")
        if not body:
            return _tool_error("create_scheduled_task", "body must not be empty")

        try:
            if kind == "once":
                run_at = _parse_iso_datetime(when)
                task = await self._scheduled_tasks.create_once(
                    assistant_id=self._assistant_id,
                    run_at=run_at,
                    subject=subject,
                    body=body,
                    created_by_run_id=self._run_id,
                )
            elif kind == "cron":
                task = await self._scheduled_tasks.create_cron(
                    assistant_id=self._assistant_id,
                    cron_expr=when,
                    subject=subject,
                    body=body,
                    created_by_run_id=self._run_id,
                )
            else:
                return _tool_error(
                    "create_scheduled_task",
                    f"kind must be 'once' or 'cron', got {kind!r}",
                )
        except Exception as exc:
            return _tool_error("create_scheduled_task", str(exc), detail=kind)

        return f"created scheduled_task {task.id} (next_run_at={task.next_run_at.isoformat()})"

    async def delete_scheduled_task(self, task_id: str) -> str:
        if self._scheduled_tasks is None:
            return _tool_error("delete_scheduled_task", "scheduled tasks not configured")
        deleted = await self._scheduled_tasks.delete(
            assistant_id=self._assistant_id, task_id=task_id
        )
        if not deleted:
            return _tool_error(
                "delete_scheduled_task", f"task {task_id} not found for this assistant"
            )
        return f"deleted scheduled_task {task_id}"

    @property
    def run_id(self) -> str:
        return self._run_id


def _tool_error(tool_name: str, error: str, *, detail: str | None = None) -> str:
    subject = f"{tool_name}({detail})" if detail else tool_name
    return f"ERROR: {subject} failed\n{error or 'unknown error'}"


def _parse_iso_datetime(value: str) -> datetime:
    """Parse an ISO-8601 string into a timezone-aware datetime.

    Naive inputs are rejected: the agent should always pass an explicit
    timezone so scheduling intent is unambiguous.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"datetime {value!r} must include a timezone")
    return parsed


__all__ = ["AgentToolset"]
