import asyncio
import contextlib
import io
import tarfile
import time
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from email_agent.sandbox.environment import FileStat, ShellResult
from email_agent.sandbox.workspace import AssistantWorkspace

if TYPE_CHECKING:
    from docker.models.containers import Container

    import docker as docker_sdk


CONTAINER_NAME_PREFIX = "email-agent-sandbox-"
WORKSPACE_VOLUME_PREFIX = "email-agent-workspace-"
WORKSPACE_ROOT = "/workspace"
SANDBOX_DNS_SERVERS = ["1.1.1.1", "8.8.8.8"]
SANDBOX_DNS_SEARCH = ["."]


class DockerEnvironmentAdapter:
    """Generic `SandboxEnvironment` backed by one running Docker container."""

    def __init__(self, *, container: "Container", bash_timeout_seconds: int = 60) -> None:
        self._container = container
        self._bash_timeout_seconds = bash_timeout_seconds

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: int | None = None,
    ) -> ShellResult:
        return await asyncio.to_thread(self._exec_sync, command, cwd, timeout_s)

    def _exec_sync(self, command: str, cwd: str | None, timeout_s: int | None) -> ShellResult:
        timeout = timeout_s or self._bash_timeout_seconds
        wrapped = ["timeout", "--signal=KILL", str(timeout), "bash", "-c", command]
        start = time.monotonic()
        code, output = self._container.exec_run(wrapped, workdir=cwd or WORKSPACE_ROOT, demux=True)
        duration_ms = int((time.monotonic() - start) * 1000)
        stdout_b, stderr_b = output if output else (b"", b"")
        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        if code in (124, 137):
            stderr = stderr or f"command timed out after {timeout}s"
        return ShellResult(exit_code=code, stdout=stdout, stderr=stderr, duration_ms=duration_ms)

    async def read_text(self, path: str) -> str:
        return (await self.read_bytes(path)).decode("utf-8")

    async def read_bytes(self, path: str) -> bytes:
        return await asyncio.to_thread(self._read_bytes_sync, self._workspace_path(path))

    def _read_bytes_sync(self, path: str) -> bytes:
        bits, _ = self._container.get_archive(path)
        buf = io.BytesIO(b"".join(bits))
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r") as tar:
            for member in tar:
                if member.isfile():
                    extracted = tar.extractfile(member)
                    if extracted is not None:
                        return extracted.read()
        raise FileNotFoundError(path)

    async def write_text(self, path: str, content: str) -> None:
        await self.write_bytes(path, content.encode("utf-8"))

    async def write_bytes(self, path: str, content: bytes) -> None:
        await asyncio.to_thread(self._write_bytes_sync, self._workspace_path(path), content)

    def _write_bytes_sync(self, path: str, content: bytes) -> None:
        parent = str(PurePosixPath(path).parent)
        self._container.exec_run(["mkdir", "-p", parent])
        self._put_files(
            self._container,
            root=parent,
            files=[(PurePosixPath(path).name, content)],
        )

    async def stat(self, path: str) -> FileStat:
        quoted_path = _shell_quote(self._workspace_path(path))
        result = await self.exec(
            f"python3 - {quoted_path} <<'PY'\n"
            "import os, sys\n"
            "p=sys.argv[1]\n"
            "s=os.lstat(p)\n"
            "print('|'.join([str(int(os.path.isfile(p))), str(int(os.path.isdir(p))), "
            "str(int(os.path.islink(p))), str(s.st_size), str(int(s.st_mtime))]))\n"
            "PY"
        )
        if result.exit_code != 0:
            raise FileNotFoundError(path)
        is_file, is_dir, is_symlink, size, _mtime = result.stdout.strip().split("|")
        return FileStat(
            is_file=is_file == "1",
            is_dir=is_dir == "1",
            is_symlink=is_symlink == "1",
            size=int(size),
        )

    async def readdir(self, path: str) -> list[str]:
        result = await self.exec("ls -1A " + _shell_quote(self._workspace_path(path)))
        if result.exit_code != 0:
            raise NotADirectoryError(path)
        if not result.stdout.strip():
            return []
        return result.stdout.splitlines()

    async def exists(self, path: str) -> bool:
        result = await self.exec("test -e " + _shell_quote(self._workspace_path(path)))
        return result.exit_code == 0

    async def mkdir(self, path: str, *, parents: bool = False) -> None:
        flag = " -p" if parents else ""
        result = await self.exec(f"mkdir{flag} " + _shell_quote(self._workspace_path(path)))
        if result.exit_code != 0:
            raise OSError(result.stderr or result.stdout)

    async def rm(self, path: str, *, recursive: bool = False, force: bool = False) -> None:
        flags = ""
        if recursive:
            flags += " -r"
        if force:
            flags += " -f"
        result = await self.exec(f"rm{flags} " + _shell_quote(self._workspace_path(path)))
        if result.exit_code != 0:
            raise FileNotFoundError(path)

    @staticmethod
    def _workspace_path(path: str) -> str:
        if path.startswith(WORKSPACE_ROOT):
            return str(PurePosixPath(path))
        if path.startswith("/"):
            return str(PurePosixPath(f"{WORKSPACE_ROOT}{path}"))
        return str(PurePosixPath(WORKSPACE_ROOT) / PurePosixPath(path))

    @staticmethod
    def _put_files(container: "Container", *, root: str, files: list[tuple[str, bytes]]) -> None:
        buf = io.BytesIO()
        now = int(time.time())
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name, content in files:
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                info.mtime = now
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(content))
        buf.seek(0)
        ok = container.put_archive(path=root, data=buf.getvalue())
        if not ok:
            raise RuntimeError(f"put_archive into {root} failed")


class DockerWorkspaceProvider:
    """Creates one long-lived Docker-backed workspace per assistant.

    `/workspace` is a Docker named volume rather than a host bind mount. This
    preserves per-assistant workspace state without exposing the host project
    path through container mount metadata.
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
        # Kept in the constructor for settings/backward compatibility. The
        # active Docker path now uses named volumes instead of host paths.
        self._memory_mb = memory_mb
        self._cpu_cores = cpu_cores
        self._bash_timeout_seconds = bash_timeout_seconds

    async def get_workspace(self, assistant_id: str) -> AssistantWorkspace:
        container = await asyncio.to_thread(self._ensure_container_sync, assistant_id)
        env = DockerEnvironmentAdapter(
            container=container,
            bash_timeout_seconds=self._bash_timeout_seconds,
        )
        return AssistantWorkspace(env)

    def _ensure_container_sync(self, assistant_id: str) -> "Container":
        import docker.errors

        name = f"{CONTAINER_NAME_PREFIX}{assistant_id}"
        try:
            container = self._client.containers.get(name)
        except docker.errors.NotFound:
            return self._create_container(assistant_id, name)

        if self._container_needs_recreate(container, assistant_id):
            return self._recreate_container(assistant_id, container)

        if container.status != "running":
            container.start()
        return container

    def _create_container(self, assistant_id: str, name: str) -> "Container":
        volume_name = self._workspace_volume_name(assistant_id)
        self._ensure_workspace_volume(assistant_id)

        return self._client.containers.run(
            image=self._image,
            name=name,
            detach=True,
            command=["sleep", "infinity"],
            volumes={
                volume_name: {"bind": WORKSPACE_ROOT, "mode": "rw"},
            },
            mem_limit=f"{self._memory_mb}m",
            nano_cpus=int(self._cpu_cores * 1_000_000_000),
            working_dir=WORKSPACE_ROOT,
            tmpfs={"/tmp": ""},
            dns=SANDBOX_DNS_SERVERS,
            dns_search=SANDBOX_DNS_SEARCH,
            dns_opt=[],
            use_config_proxy=False,
            init=True,
        )

    def _recreate_container(self, assistant_id: str, container: "Container") -> "Container":
        workspace_archive = self._export_workspace(container)
        container.remove(force=True)
        self._remove_workspace_volume(assistant_id)
        recreated = self._create_container(assistant_id, self._container_name(assistant_id))
        if workspace_archive:
            ok = recreated.put_archive(WORKSPACE_ROOT, workspace_archive)
            if not ok:
                raise RuntimeError(f"put_archive into {WORKSPACE_ROOT} failed")
        return recreated

    def _export_workspace(self, container: "Container") -> bytes | None:
        code, output = container.exec_run(["tar", "-C", WORKSPACE_ROOT, "-cf", "-", "."])
        if code != 0:
            return None
        return output

    def _container_needs_recreate(self, container: "Container", assistant_id: str) -> bool:
        container.reload()
        attrs = container.attrs or {}
        mount = next(
            (m for m in attrs.get("Mounts", []) if m.get("Destination") == WORKSPACE_ROOT),
            None,
        )
        if mount is None:
            return True
        if mount.get("Type") != "volume":
            return True
        if mount.get("Name") != self._workspace_volume_name(assistant_id):
            return True

        host_config = attrs.get("HostConfig", {})
        if _none_to_empty(host_config.get("Dns")) != SANDBOX_DNS_SERVERS:
            return True
        if _none_to_empty(host_config.get("DnsSearch")) != SANDBOX_DNS_SEARCH:
            return True
        return bool(_none_to_empty(host_config.get("DnsOptions")))

    def _ensure_workspace_volume(self, assistant_id: str) -> None:
        import docker.errors

        volume_name = self._workspace_volume_name(assistant_id)
        try:
            self._client.volumes.get(volume_name)
        except docker.errors.NotFound:
            self._client.volumes.create(
                name=volume_name,
                labels={
                    "email-agent.kind": "assistant-workspace",
                    "email-agent.assistant_id": assistant_id,
                },
            )

    def _remove_workspace_volume(self, assistant_id: str) -> None:
        import docker.errors

        with contextlib.suppress(docker.errors.NotFound, docker.errors.APIError):
            self._client.volumes.get(self._workspace_volume_name(assistant_id)).remove(force=True)

    @staticmethod
    def _container_name(assistant_id: str) -> str:
        return f"{CONTAINER_NAME_PREFIX}{assistant_id}"

    @staticmethod
    def _workspace_volume_name(assistant_id: str) -> str:
        return f"{WORKSPACE_VOLUME_PREFIX}{assistant_id}"


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _none_to_empty(value: list[str] | None) -> list[str]:
    return [] if value is None else value


__all__ = [
    "SANDBOX_DNS_SEARCH",
    "SANDBOX_DNS_SERVERS",
    "WORKSPACE_ROOT",
    "WORKSPACE_VOLUME_PREFIX",
    "DockerEnvironmentAdapter",
    "DockerWorkspaceProvider",
]
