from pathlib import PurePosixPath

from email_agent.models.sandbox import ProjectedFile
from email_agent.sandbox.environment import SandboxEnvironment
from email_agent.sandbox.skills import (
    Skill,
    ensure_starter_files,
    load_skills,
    read_context,
)

WORKSPACE_ROOT = "/workspace"
EMAILS_DIR = "/workspace/emails"
ATTACHMENTS_DIR = "/workspace/attachments"


class WorkspacePolicyError(Exception):
    """Raised when an agent-visible operation violates workspace policy."""


class AssistantWorkspace:
    """Email-agent workspace policy on top of a generic sandbox environment."""

    def __init__(self, env: SandboxEnvironment) -> None:
        self._env = env

    @property
    def environment(self) -> SandboxEnvironment:
        return self._env

    async def project_emails(self, files: list[ProjectedFile]) -> None:
        await self._env.rm(EMAILS_DIR, recursive=True, force=True)
        await self._env.mkdir(EMAILS_DIR, parents=True)
        for projected in files:
            path = self._email_projection_path(projected.path)
            await self._env.write_bytes(path, projected.content)

    async def project_attachments(self, run_id: str, files: list[ProjectedFile]) -> None:
        run_root = f"{ATTACHMENTS_DIR}/{run_id}"
        await self._env.rm(run_root, recursive=True, force=True)
        await self._env.mkdir(run_root, parents=True)
        for projected in files:
            path = self._join(run_root, projected.path)
            await self._env.write_bytes(path, projected.content)

    async def read_outbound_attachment(self, path: str) -> bytes:
        return await self._env.read_bytes(self._workspace_path(path))

    async def load_skills(self) -> list[Skill]:
        return await load_skills(self._env)

    async def read_context(self) -> str | None:
        return await read_context(self._env)

    async def ensure_starter_files(self) -> None:
        await ensure_starter_files(self._env)

    async def assert_agent_write_allowed(self, path: str) -> None:
        normalized = self._workspace_path(path)
        if normalized == EMAILS_DIR or normalized.startswith(f"{EMAILS_DIR}/"):
            raise WorkspacePolicyError("emails/ is read-only; refuse write")

    def _email_projection_path(self, path: str) -> str:
        normalized = self._relative_path(path)
        if normalized == "emails":
            return EMAILS_DIR
        if normalized.startswith("emails/"):
            normalized = normalized[len("emails/") :]
        return self._join(EMAILS_DIR, normalized)

    def _workspace_path(self, path: str) -> str:
        if path.startswith(WORKSPACE_ROOT):
            return str(PurePosixPath(path))
        if path.startswith("/"):
            return str(PurePosixPath(f"{WORKSPACE_ROOT}{path}"))
        return self._join(WORKSPACE_ROOT, path)

    @staticmethod
    def _relative_path(path: str) -> str:
        normalized = str(PurePosixPath(path.lstrip("/")))
        if normalized.startswith("workspace/"):
            normalized = normalized[len("workspace/") :]
        return "" if normalized == "." else normalized

    @staticmethod
    def _join(root: str, path: str) -> str:
        return str(PurePosixPath(root) / PurePosixPath(path.lstrip("/")))


__all__ = [
    "ATTACHMENTS_DIR",
    "EMAILS_DIR",
    "WORKSPACE_ROOT",
    "AssistantWorkspace",
    "WorkspacePolicyError",
]
