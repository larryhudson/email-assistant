import asyncio
import contextlib
import io
import shutil
import tarfile
import time
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from email_agent.models.sandbox import BashResult, ProjectedFile, ToolCall, ToolResult

if TYPE_CHECKING:
    from docker.models.containers import Container

    import docker as docker_sdk


CONTAINER_NAME_PREFIX = "email-agent-sandbox-"
WORKSPACE_ROOT = "/workspace"
EMAILS_DIR = "/workspace/emails"


class DockerSandbox:
    """`AssistantSandbox` adapter backed by long-lived per-assistant containers.

    One container per `assistant_id`, lazily started on first `ensure_started`
    and reused for the lifetime of the process. The host workspace lives at
    `sandbox_data_root/<assistant_id>/workspace/` and is bind-mounted at
    `/workspace`. Resource limits + per-tool timeouts are enforced via
    docker's HostConfig and GNU `timeout` inside the container.

    docker SDK is sync, so calls are dispatched via `asyncio.to_thread`.
    """

    def __init__(
        self,
        *,
        client: "docker_sdk.DockerClient",
        image: str,
        sandbox_data_root: Path,
        memory_mb: int = 512,
        cpu_cores: float = 1.0,
        bash_timeout_seconds: int = 60,
    ) -> None:
        self._client = client
        self._image = image
        self._data_root = sandbox_data_root
        self._memory_mb = memory_mb
        self._cpu_cores = cpu_cores
        self._bash_timeout_seconds = bash_timeout_seconds

    async def ensure_started(self, assistant_id: str) -> None:
        await asyncio.to_thread(self._ensure_started_sync, assistant_id)

    def _ensure_started_sync(self, assistant_id: str) -> None:
        import docker.errors

        name = self._container_name(assistant_id)
        try:
            container = self._client.containers.get(name)
        except docker.errors.NotFound:
            self._create_container(assistant_id, name)
            return

        if container.status != "running":
            container.start()

    def _create_container(self, assistant_id: str, name: str) -> "Container":
        host_workspace = self._workspace_dir(assistant_id)
        host_workspace.mkdir(parents=True, exist_ok=True)

        return self._client.containers.run(
            image=self._image,
            name=name,
            detach=True,
            command=["sleep", "infinity"],
            volumes={
                str(host_workspace.resolve()): {"bind": "/workspace", "mode": "rw"},
            },
            mem_limit=f"{self._memory_mb}m",
            nano_cpus=int(self._cpu_cores * 1_000_000_000),
            working_dir="/workspace",
            tmpfs={"/tmp": ""},
        )

    def _workspace_dir(self, assistant_id: str) -> Path:
        return self._data_root / assistant_id / "workspace"

    def _container_name(self, assistant_id: str) -> str:
        return f"{CONTAINER_NAME_PREFIX}{assistant_id}"

    def _container(self, assistant_id: str) -> "Container":
        return self._client.containers.get(self._container_name(assistant_id))

    async def project_emails(self, assistant_id: str, files: list[ProjectedFile]) -> None:
        await asyncio.to_thread(self._project_emails_sync, assistant_id, files)

    def _project_emails_sync(self, assistant_id: str, files: list[ProjectedFile]) -> None:
        container = self._container(assistant_id)
        # Wipe previous projection so deleted threads don't linger.
        container.exec_run(["rm", "-rf", EMAILS_DIR])
        container.exec_run(["mkdir", "-p", EMAILS_DIR])
        if files:
            self._put_files(container, root=WORKSPACE_ROOT, files=files)

    async def project_attachments(
        self, assistant_id: str, run_id: str, files: list[ProjectedFile]
    ) -> None:
        await asyncio.to_thread(self._project_attachments_sync, assistant_id, run_id, files)

    def _project_attachments_sync(
        self, assistant_id: str, run_id: str, files: list[ProjectedFile]
    ) -> None:
        container = self._container(assistant_id)
        run_root = f"{WORKSPACE_ROOT}/attachments/{run_id}"
        container.exec_run(["rm", "-rf", run_root])
        container.exec_run(["mkdir", "-p", run_root])
        if files:
            self._put_files(container, root=run_root, files=files)

    async def run_tool(self, assistant_id: str, run_id: str, call: ToolCall) -> ToolResult:
        return await asyncio.to_thread(self._run_tool_sync, assistant_id, run_id, call)

    def _run_tool_sync(self, assistant_id: str, run_id: str, call: ToolCall) -> ToolResult:
        container = self._container(assistant_id)
        if call.kind == "read":
            assert call.path is not None
            absolute = self._resolve_workspace_path(call.path)
            code, output = container.exec_run(["cat", absolute], demux=False)
            if code != 0:
                return ToolResult(ok=False, error=output.decode("utf-8", errors="replace"))
            return ToolResult(ok=True, output=output.decode("utf-8"))
        if call.kind == "write":
            assert call.path is not None
            assert call.content is not None
            if self._is_under_emails(call.path):
                return ToolResult(ok=False, error="emails/ is read-only; refuse write")
            self._write_file(container, call.path, call.content.encode("utf-8"))
            return ToolResult(ok=True)
        if call.kind == "edit":
            assert call.path is not None
            assert call.old is not None
            assert call.new is not None
            if self._is_under_emails(call.path):
                return ToolResult(ok=False, error="emails/ is read-only; refuse edit")
            absolute = self._resolve_workspace_path(call.path)
            code, output = container.exec_run(["cat", absolute], demux=False)
            if code != 0:
                return ToolResult(ok=False, error=output.decode("utf-8", errors="replace"))
            current = output.decode("utf-8")
            if call.old not in current:
                return ToolResult(ok=False, error=f"old string not found in {call.path}")
            updated = current.replace(call.old, call.new, 1)
            self._write_file(container, call.path, updated.encode("utf-8"))
            return ToolResult(ok=True)
        if call.kind == "bash":
            assert call.command is not None
            return self._run_bash(container, call.command)
        if call.kind == "attach_file":
            assert call.path is not None
            absolute = self._resolve_workspace_path(call.path)
            code, _ = container.exec_run(["test", "-f", absolute])
            if code != 0:
                return ToolResult(ok=False, error=f"attach_file: {call.path} not found")
            return ToolResult(ok=True)
        raise NotImplementedError(f"run_tool: {call.kind} lands in a later task")

    async def reset(self, assistant_id: str) -> None:
        await asyncio.to_thread(self._reset_sync, assistant_id)

    def _reset_sync(self, assistant_id: str) -> None:
        import docker.errors

        with contextlib.suppress(docker.errors.NotFound):
            self._container(assistant_id).remove(force=True)
        host_workspace = self._workspace_dir(assistant_id)
        if host_workspace.exists():
            shutil.rmtree(host_workspace)

    async def read_attachment_out(self, assistant_id: str, run_id: str, path: str) -> bytes:
        return await asyncio.to_thread(self._read_attachment_out_sync, assistant_id, path)

    def _read_attachment_out_sync(self, assistant_id: str, path: str) -> bytes:
        container = self._container(assistant_id)
        absolute = self._resolve_workspace_path(path)
        bits, _ = container.get_archive(absolute)
        # bits is an iterator of tar chunks; reassemble and extract the single file.
        buf = io.BytesIO(b"".join(bits))
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r") as tar:
            for member in tar:
                if member.isfile():
                    extracted = tar.extractfile(member)
                    if extracted is not None:
                        return extracted.read()
        raise FileNotFoundError(f"no file found in archive for {path}")

    def _run_bash(self, container: "Container", command: str) -> ToolResult:
        timeout_s = self._bash_timeout_seconds
        # GNU `timeout` (in the base image) handles per-call wall-clock; exit
        # code 124 means the command was killed.
        wrapped = ["timeout", "--signal=KILL", str(timeout_s), "bash", "-c", command]
        start = time.monotonic()
        code, output = container.exec_run(wrapped, demux=True)
        duration_ms = int((time.monotonic() - start) * 1000)
        stdout_b, stderr_b = output if output else (b"", b"")
        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        if code == 124 or code == 137:
            return ToolResult(
                ok=False,
                error=f"command timed out after {timeout_s}s",
                output=BashResult(
                    exit_code=code, stdout=stdout, stderr=stderr, duration_ms=duration_ms
                ),
            )
        return ToolResult(
            ok=True,
            output=BashResult(
                exit_code=code, stdout=stdout, stderr=stderr, duration_ms=duration_ms
            ),
        )

    def _write_file(self, container: "Container", path: str, content: bytes) -> None:
        rel = path.lstrip("/")
        if rel.startswith("workspace/"):
            rel = rel[len("workspace/") :]
        # Ensure parent dir exists, then put_archive of the single file.
        parent = str(PurePosixPath(rel).parent)
        if parent and parent != ".":
            container.exec_run(["mkdir", "-p", f"{WORKSPACE_ROOT}/{parent}"])
        self._put_files(
            container,
            root=WORKSPACE_ROOT,
            files=[ProjectedFile(path=rel, content=content)],
        )

    @staticmethod
    def _is_under_emails(path: str) -> bool:
        normalized = path.lstrip("/")
        if normalized.startswith("workspace/"):
            normalized = normalized[len("workspace/") :]
        return normalized.startswith("emails/") or normalized == "emails"

    @staticmethod
    def _resolve_workspace_path(path: str) -> str:
        if path.startswith("/"):
            return path
        return f"{WORKSPACE_ROOT}/{path}"

    @staticmethod
    def _put_files(container: "Container", *, root: str, files: list[ProjectedFile]) -> None:
        # Build a tar stream and put_archive into <root>.
        buf = io.BytesIO()
        now = int(time.time())
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for projected in files:
                rel_path = str(PurePosixPath(projected.path)).lstrip("/")
                info = tarfile.TarInfo(name=rel_path)
                info.size = len(projected.content)
                info.mtime = now
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(projected.content))
        buf.seek(0)
        ok = container.put_archive(path=root, data=buf.getvalue())
        if not ok:
            raise RuntimeError(f"put_archive into {root} failed")


__all__ = ["DockerSandbox"]
