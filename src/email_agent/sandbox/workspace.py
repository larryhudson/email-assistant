from pathlib import Path, PurePosixPath

from email_agent.models.sandbox import ProjectedFile
from email_agent.sandbox.environment import ReadOnlyHostMountEnvironment, SandboxEnvironment
from email_agent.sandbox.skills import (
    Skill,
    ensure_starter_files,
    load_skills,
    read_context,
    read_identity,
)
from email_agent.sandbox.source_projection import project_source

WORKSPACE_ROOT = "/workspace"
EMAILS_DIR = "/workspace/emails"
ATTACHMENTS_DIR = "/workspace/attachments"
PLATFORM_ENV_DIR = "/workspace/.assistant"
PLATFORM_ENV_PATH = f"{PLATFORM_ENV_DIR}/env"


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

    async def project_email_directory(self, source_dir: Path) -> bool:
        """Expose an existing host email projection if the environment supports it."""
        if not isinstance(self._env, ReadOnlyHostMountEnvironment):
            return False
        await self._env.mount_readonly_host_dir(source_dir, EMAILS_DIR)
        return True

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

    async def read_identity(self) -> str | None:
        return await read_identity(self._env)

    async def ensure_starter_files(self) -> None:
        await ensure_starter_files(self._env)

    async def project_source(self, source_root: Path) -> None:
        await project_source(self._env, source_root)

    async def write_platform_environment(
        self,
        *,
        assistant_id: str,
        assistant_tools_base_url: str,
        assistant_surface_base_url: str,
        assistant_tools_token: str | None = None,
    ) -> None:
        await self._env.mkdir(PLATFORM_ENV_DIR, parents=True)
        lines = [
            f"ASSISTANT_ID={_shell_env_value(assistant_id)}",
            f"ASSISTANT_TOOLS_BASE_URL={_shell_env_value(assistant_tools_base_url)}",
            f"ASSISTANT_SURFACE_BASE_URL={_shell_env_value(assistant_surface_base_url)}",
        ]
        if assistant_tools_token is not None:
            lines.append(f"ASSISTANT_TOOLS_TOKEN={_shell_env_value(assistant_tools_token)}")
        content = "\n".join([*lines, ""])
        await self._env.write_text(PLATFORM_ENV_PATH, content)

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


def _shell_env_value(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


__all__ = [
    "ATTACHMENTS_DIR",
    "EMAILS_DIR",
    "PLATFORM_ENV_PATH",
    "WORKSPACE_ROOT",
    "AssistantWorkspace",
    "WorkspacePolicyError",
]
