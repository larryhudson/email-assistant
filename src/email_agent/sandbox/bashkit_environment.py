import asyncio
import io
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from bashkit import Bash, FileSystem

from email_agent.sandbox.environment import FileStat, ShellResult
from email_agent.sandbox.workspace import AssistantWorkspace

WORKSPACE_ROOT = "/workspace"


@dataclass(frozen=True)
class BashkitImportReport:
    files_imported: int = 0
    directories_imported: int = 0
    symlinks_skipped: int = 0
    binary_files_skipped: int = 0
    other_entries_skipped: int = 0

    @property
    def skipped(self) -> int:
        return self.symlinks_skipped + self.binary_files_skipped + self.other_entries_skipped


class BashkitSnapshotStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def path_for(self, assistant_id: str) -> Path:
        return self._root / assistant_id / "snapshot.bin"

    def load(self, assistant_id: str) -> bytes | None:
        path = self.path_for(assistant_id)
        if not path.exists():
            return None
        return path.read_bytes()

    def save(self, assistant_id: str, snapshot: bytes) -> None:
        path = self.path_for(assistant_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(snapshot)


class BashkitEnvironment:
    """`SandboxEnvironment` backed by Bashkit's in-process virtual bash.

    Bashkit's Python VFS API currently accepts and returns `str`, not raw
    bytes. We keep exact bytes for adapter-written files until the next shell
    execution, then fall back to Bashkit's VFS view.
    """

    def __init__(
        self,
        *,
        bash_timeout_seconds: int = 60,
        username: str = "agent",
        hostname: str = "sandbox",
        max_commands: int | None = 1000,
        max_loop_iterations: int | None = 10000,
        python_enabled: bool = True,
        sqlite_enabled: bool = True,
        snapshot: bytes | None = None,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._bash_timeout_seconds = bash_timeout_seconds
        self._bash = (
            Bash.from_snapshot(
                snapshot,
                username=username,
                hostname=hostname,
                max_commands=max_commands,
                max_loop_iterations=max_loop_iterations,
                timeout_seconds=bash_timeout_seconds,
                python=python_enabled,
                sqlite=sqlite_enabled,  # ty: ignore[unknown-argument]
            )
            if snapshot is not None
            else Bash(
                username=username,
                hostname=hostname,
                max_commands=max_commands,
                max_loop_iterations=max_loop_iterations,
                timeout_seconds=bash_timeout_seconds,
                python=python_enabled,
                sqlite=sqlite_enabled,
            )
        )
        self._on_change = on_change
        self._lock = asyncio.Lock()
        self._binary_files: dict[str, tuple[bytes, int]] = {}
        self._readonly_host_mounts: dict[str, Path] = {}
        self._exec_generation = 0
        self._bash.mkdir(WORKSPACE_ROOT, True)
        self._bash.execute_sync(f"cd {_shell_quote(WORKSPACE_ROOT)}")

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: int | None = None,
    ) -> ShellResult:
        timeout = timeout_s or self._bash_timeout_seconds
        if cwd is not None:
            command = f"cd {_shell_quote(self._normalize(cwd))} && {command}"

        start = time.monotonic()
        async with self._lock:
            try:
                result = await asyncio.wait_for(self._bash.execute(command), timeout=timeout)
            except TimeoutError:
                self._bash.cancel()
                duration_ms = int((time.monotonic() - start) * 1000)
                return ShellResult(
                    exit_code=124,
                    stdout="",
                    stderr=f"command timed out after {timeout}s",
                    duration_ms=duration_ms,
                )
            finally:
                self._bash.clear_cancel()
                self._exec_generation += 1
                self._notify_change()

        duration_ms = int((time.monotonic() - start) * 1000)
        return ShellResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr or result.error or "",
            duration_ms=duration_ms,
        )

    async def read_text(self, path: str) -> str:
        async with self._lock:
            return self._read_text_unlocked(path)

    async def read_bytes(self, path: str) -> bytes:
        normalized = self._normalize(path)
        async with self._lock:
            cached = self._binary_files.get(normalized)
            if cached is not None:
                content, generation = cached
                if generation >= self._exec_generation:
                    return content
            return self._read_text_unlocked(normalized).encode("utf-8")

    async def write_text(self, path: str, content: str) -> None:
        normalized = self._normalize(path)
        async with self._lock:
            self._ensure_parent_dirs(normalized)
            self._bash.write_file(normalized, content)
            self._binary_files.pop(normalized, None)
            self._notify_change()

    async def write_bytes(self, path: str, content: bytes) -> None:
        normalized = self._normalize(path)
        text = content.decode("utf-8", errors="replace")
        async with self._lock:
            self._ensure_parent_dirs(normalized)
            self._bash.write_file(normalized, text)
            self._binary_files[normalized] = (content, self._exec_generation)
            self._notify_change()

    async def stat(self, path: str) -> FileStat:
        normalized = self._normalize(path)
        async with self._lock:
            try:
                stat = self._bash.stat(normalized)
            except Exception as exc:
                raise FileNotFoundError(normalized) from exc

        file_type = stat.get("file_type")
        mtime = _timestamp_to_datetime(stat.get("modified"))
        return FileStat(
            is_file=file_type == "file",
            is_dir=file_type == "directory",
            is_symlink=file_type == "symlink",
            size=int(stat.get("size", 0)),
            mtime=mtime,
        )

    async def readdir(self, path: str) -> list[str]:
        normalized = self._normalize(path)
        async with self._lock:
            try:
                entries = self._bash.read_dir(normalized)
            except Exception as exc:
                raise NotADirectoryError(normalized) from exc
        return sorted(entry["name"] for entry in entries)

    async def exists(self, path: str) -> bool:
        async with self._lock:
            return bool(self._bash.exists(self._normalize(path)))

    async def mkdir(self, path: str, *, parents: bool = False) -> None:
        normalized = self._normalize(path)
        async with self._lock:
            try:
                self._bash.mkdir(normalized, parents)
            except Exception as exc:
                raise OSError(str(exc)) from exc
            self._notify_change()

    async def rm(self, path: str, *, recursive: bool = False, force: bool = False) -> None:
        normalized = self._normalize(path)
        async with self._lock:
            if normalized in self._readonly_host_mounts:
                self._unmount_readonly_host_dir(normalized)
                self._notify_change()
                return
            if force and not self._bash.exists(normalized):
                return
            try:
                self._bash.remove(normalized, recursive)
            except Exception as exc:
                raise FileNotFoundError(normalized) from exc
            prefix = normalized.rstrip("/") + "/"
            for cached_path in list(self._binary_files):
                if cached_path == normalized or cached_path.startswith(prefix):
                    self._binary_files.pop(cached_path, None)
            self._notify_change()

    async def snapshot(self) -> bytes:
        async with self._lock:
            mounts = dict(self._readonly_host_mounts)
            for mount_path in mounts:
                self._bash.unmount(mount_path)
            try:
                return self._bash.snapshot()
            finally:
                for mount_path, host_path in mounts.items():
                    self._bash.mount(mount_path, FileSystem.real(str(host_path), writable=False))

    async def import_workspace_tar(self, archive: bytes) -> BashkitImportReport:
        files_imported = 0
        directories_imported = 0
        symlinks_skipped = 0
        binary_files_skipped = 0
        other_entries_skipped = 0

        async with self._lock:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
                for member in tar:
                    path = _tar_member_workspace_path(member.name)
                    if path is None:
                        other_entries_skipped += 1
                        continue
                    if member.isdir():
                        self._bash.mkdir(path, True)
                        directories_imported += 1
                        continue
                    if member.issym():
                        symlinks_skipped += 1
                        continue
                    if not member.isfile():
                        other_entries_skipped += 1
                        continue

                    extracted = tar.extractfile(member)
                    if extracted is None:
                        other_entries_skipped += 1
                        continue
                    content = extracted.read()
                    try:
                        text = content.decode("utf-8")
                    except UnicodeDecodeError:
                        binary_files_skipped += 1
                        continue

                    self._ensure_parent_dirs(path)
                    self._bash.write_file(path, text)
                    files_imported += 1

            self._notify_change()

        return BashkitImportReport(
            files_imported=files_imported,
            directories_imported=directories_imported,
            symlinks_skipped=symlinks_skipped,
            binary_files_skipped=binary_files_skipped,
            other_entries_skipped=other_entries_skipped,
        )

    async def export_workspace_tar(self) -> bytes:
        """Export text-like persisted Bashkit workspace files as a tar archive.

        Read-only host mounts, such as the per-run email projection, are skipped
        because they are recreated for each run and should not become durable
        workspace state.

        Bashkit's public filesystem API is text-based and its snapshots do not
        expose raw file bytes. Binary files that have survived only inside a
        Bashkit snapshot cannot be exported safely, so this exporter omits files
        that look like binary/corrupted text instead of writing damaged bytes
        into the Docker volume.
        """
        async with self._lock:
            mounts = dict(self._readonly_host_mounts)
            for mount_path in mounts:
                self._bash.unmount(mount_path)
            try:
                output = io.BytesIO()
                with tarfile.open(fileobj=output, mode="w") as tar:
                    self._add_workspace_tree_to_tar(tar, WORKSPACE_ROOT, ".")
                return output.getvalue()
            finally:
                for mount_path, host_path in mounts.items():
                    self._bash.mount(mount_path, FileSystem.real(str(host_path), writable=False))

    async def mount_readonly_host_dir(self, host_path: Path, mount_path: str) -> None:
        normalized = self._normalize(mount_path)
        source = await asyncio.to_thread(_resolve_existing_dir, host_path)

        async with self._lock:
            if normalized in self._readonly_host_mounts:
                self._unmount_readonly_host_dir(normalized)
            elif self._bash.exists(normalized):
                self._bash.remove(normalized, True)

            self._ensure_parent_dirs(normalized)
            self._bash.mount(normalized, FileSystem.real(str(source), writable=False))
            self._readonly_host_mounts[normalized] = source

            prefix = normalized.rstrip("/") + "/"
            for cached_path in list(self._binary_files):
                if cached_path == normalized or cached_path.startswith(prefix):
                    self._binary_files.pop(cached_path, None)
            self._notify_change()

    def _read_text_unlocked(self, path: str) -> str:
        normalized = self._normalize(path)
        try:
            return self._bash.read_file(normalized)
        except Exception as exc:
            raise FileNotFoundError(normalized) from exc

    def _ensure_parent_dirs(self, path: str) -> None:
        parent = str(PurePosixPath(path).parent)
        if parent and parent != ".":
            self._bash.mkdir(parent, True)

    def _unmount_readonly_host_dir(self, path: str) -> None:
        self._bash.unmount(path)
        self._readonly_host_mounts.pop(path, None)

    def _add_workspace_tree_to_tar(self, tar: tarfile.TarFile, path: str, arcname: str) -> None:
        stat = self._bash.stat(path)
        file_type = stat.get("file_type")
        if file_type == "directory":
            if arcname != ".":
                info = tarfile.TarInfo(arcname)
                info.type = tarfile.DIRTYPE
                info.mtime = int(stat.get("modified", time.time()))
                tar.addfile(info)
            for entry in self._bash.read_dir(path):
                child_name = entry["name"]
                self._add_workspace_tree_to_tar(
                    tar,
                    f"{path.rstrip('/')}/{child_name}",
                    f"{arcname.rstrip('/')}/{child_name}" if arcname != "." else f"./{child_name}",
                )
            return

        if file_type != "file":
            return

        try:
            content = self._bash.read_file(path)
        except RuntimeError as exc:
            if "Invalid UTF-8" in str(exc):
                return
            raise
        if _looks_like_unexportable_binary_text(content):
            return

        data = content.encode("utf-8")
        info = tarfile.TarInfo(arcname)
        info.size = len(data)
        info.mtime = int(stat.get("modified", time.time()))
        tar.addfile(info, io.BytesIO(data))

    @staticmethod
    def _normalize(path: str) -> str:
        if path.startswith(WORKSPACE_ROOT):
            candidate = path
        elif path.startswith("/"):
            candidate = f"{WORKSPACE_ROOT}{path}"
        else:
            candidate = f"{WORKSPACE_ROOT}/{path}"
        normalized = str(PurePosixPath(candidate))
        return WORKSPACE_ROOT if normalized == "." else normalized

    def _notify_change(self) -> None:
        if self._on_change is not None:
            self._on_change()

    def set_on_change(self, on_change: Callable[[], None] | None) -> None:
        self._on_change = on_change


class BashkitWorkspaceProvider:
    """Creates one long-lived Bashkit-backed workspace per assistant."""

    def __init__(
        self,
        *,
        bash_timeout_seconds: int = 60,
        python_enabled: bool = True,
        sqlite_enabled: bool = True,
        snapshot_store: BashkitSnapshotStore | None = None,
    ) -> None:
        self._bash_timeout_seconds = bash_timeout_seconds
        self._python_enabled = python_enabled
        self._sqlite_enabled = sqlite_enabled
        self._snapshot_store = snapshot_store
        self._workspaces: dict[str, AssistantWorkspace] = {}
        self._pending_persist_tasks: set[asyncio.Task[None]] = set()

    async def get_workspace(self, assistant_id: str) -> AssistantWorkspace:
        workspace = self._workspaces.get(assistant_id)
        if workspace is None:
            snapshot = self._snapshot_store.load(assistant_id) if self._snapshot_store else None
            env = BashkitEnvironment(
                bash_timeout_seconds=self._bash_timeout_seconds,
                python_enabled=self._python_enabled,
                sqlite_enabled=self._sqlite_enabled,
                snapshot=snapshot,
            )
            workspace = AssistantWorkspace(env)
            if self._snapshot_store is not None:
                env.set_on_change(self._make_persist_callback(assistant_id, workspace))
            self._workspaces[assistant_id] = workspace
        return workspace

    async def persist_workspace(
        self, assistant_id: str, workspace: AssistantWorkspace | None = None
    ) -> None:
        if self._snapshot_store is None:
            return
        workspace = workspace or self._workspaces[assistant_id]
        env = workspace.environment
        if not isinstance(env, BashkitEnvironment):
            raise TypeError("BashkitWorkspaceProvider can only persist BashkitEnvironment")
        self._snapshot_store.save(assistant_id, await env.snapshot())

    def _make_persist_callback(
        self, assistant_id: str, workspace: AssistantWorkspace
    ) -> Callable[[], None]:
        def callback() -> None:
            task = asyncio.create_task(self.persist_workspace(assistant_id, workspace))
            self._pending_persist_tasks.add(task)
            task.add_done_callback(self._pending_persist_tasks.discard)

        return callback


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _timestamp_to_datetime(value: object) -> datetime | None:
    if not isinstance(value, int | float):
        return None
    return datetime.fromtimestamp(value, UTC)


def _resolve_existing_dir(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_dir():
        raise NotADirectoryError(str(resolved))
    return resolved


def _tar_member_workspace_path(name: str) -> str | None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        return None
    parts = [part for part in path.parts if part not in ("", ".")]
    if not parts:
        return WORKSPACE_ROOT
    if parts[0] == "workspace":
        parts = parts[1:]
    if not parts:
        return WORKSPACE_ROOT
    return str(PurePosixPath(WORKSPACE_ROOT).joinpath(*parts))


def _looks_like_unexportable_binary_text(content: str) -> bool:
    if "\ufffd" in content or "\x00" in content:
        return True
    if not content:
        return False

    sample = content[:4096]
    controls = sum(1 for char in sample if ord(char) < 32 and char not in "\n\r\t\f\b")
    return controls / len(sample) > 0.05


__all__ = [
    "WORKSPACE_ROOT",
    "BashkitEnvironment",
    "BashkitImportReport",
    "BashkitSnapshotStore",
    "BashkitWorkspaceProvider",
]
