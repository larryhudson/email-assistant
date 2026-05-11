import subprocess
import time
from datetime import UTC, datetime
from pathlib import PurePosixPath

from email_agent.sandbox.environment import FileStat, ShellResult

WORKSPACE_ROOT = "/workspace"


class InMemoryEnvironment:
    """In-process workspace environment for tests.

    File operations use an in-memory dict. `exec` runs on the host process,
    matching the existing `InMemorySandbox` test adapter; this is not a
    production sandbox.
    """

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}
        self._dirs: set[str] = {WORKSPACE_ROOT}
        self._mtimes: dict[str, datetime] = {}

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: int | None = None,
    ) -> ShellResult:
        start = time.monotonic()
        proc = subprocess.run(  # noqa: ASYNC221
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_s or 10,
            cwd=None,
            check=False,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        return ShellResult(
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_ms=duration_ms,
        )

    async def read_text(self, path: str) -> str:
        return (await self.read_bytes(path)).decode()

    async def read_bytes(self, path: str) -> bytes:
        normalized = self._normalize(path)
        try:
            return self._files[normalized]
        except KeyError as exc:
            raise FileNotFoundError(normalized) from exc

    async def write_text(self, path: str, content: str) -> None:
        await self.write_bytes(path, content.encode())

    async def write_bytes(self, path: str, content: bytes) -> None:
        normalized = self._normalize(path)
        self._ensure_parent_dirs(normalized)
        self._files[normalized] = content
        self._mtimes[normalized] = datetime.now(UTC)

    async def stat(self, path: str) -> FileStat:
        normalized = self._normalize(path)
        if normalized in self._files:
            return FileStat(
                is_file=True,
                is_dir=False,
                is_symlink=False,
                size=len(self._files[normalized]),
                mtime=self._mtimes.get(normalized),
            )
        if normalized in self._all_dirs():
            return FileStat(
                is_file=False,
                is_dir=True,
                is_symlink=False,
                size=0,
                mtime=self._mtimes.get(normalized),
            )
        raise FileNotFoundError(normalized)

    async def readdir(self, path: str) -> list[str]:
        normalized = self._normalize(path)
        if normalized not in self._all_dirs():
            raise NotADirectoryError(normalized)

        prefix = normalized.rstrip("/") + "/"
        entries: set[str] = set()
        for candidate in [*self._all_dirs(), *self._files]:
            if candidate == normalized or not candidate.startswith(prefix):
                continue
            rest = candidate[len(prefix) :]
            name = rest.split("/", 1)[0]
            if name:
                entries.add(name)
        return sorted(entries)

    async def exists(self, path: str) -> bool:
        normalized = self._normalize(path)
        return normalized in self._files or normalized in self._all_dirs()

    async def mkdir(self, path: str, *, parents: bool = False) -> None:
        normalized = self._normalize(path)
        parent = self._parent(normalized)
        if not parents and parent not in self._all_dirs():
            raise FileNotFoundError(parent)
        self._dirs.add(normalized)
        if parents:
            self._ensure_parent_dirs(normalized)
        self._mtimes[normalized] = datetime.now(UTC)

    async def rm(self, path: str, *, recursive: bool = False, force: bool = False) -> None:
        normalized = self._normalize(path)
        exists = normalized in self._files or normalized in self._all_dirs()
        if not exists:
            if force:
                return
            raise FileNotFoundError(normalized)

        if normalized in self._files:
            self._files.pop(normalized, None)
            self._mtimes.pop(normalized, None)
            return

        prefix = normalized.rstrip("/") + "/"
        children = [
            candidate
            for candidate in [*self._files, *self._all_dirs()]
            if candidate.startswith(prefix)
        ]
        if children and not recursive:
            raise OSError(f"directory not empty: {normalized}")
        for child in children:
            self._files.pop(child, None)
            self._dirs.discard(child)
            self._mtimes.pop(child, None)
        if normalized != WORKSPACE_ROOT:
            self._dirs.discard(normalized)
            self._mtimes.pop(normalized, None)

    def _normalize(self, path: str) -> str:
        if path.startswith(WORKSPACE_ROOT):
            candidate = path
        elif path.startswith("/"):
            candidate = f"{WORKSPACE_ROOT}{path}"
        else:
            candidate = f"{WORKSPACE_ROOT}/{path}"
        normalized = str(PurePosixPath(candidate))
        if normalized == ".":
            return WORKSPACE_ROOT
        return normalized

    def _ensure_parent_dirs(self, path: str) -> None:
        parent = self._parent(path)
        while parent and parent != "/":
            self._dirs.add(parent)
            if parent == WORKSPACE_ROOT:
                break
            parent = self._parent(parent)

    @staticmethod
    def _parent(path: str) -> str:
        return str(PurePosixPath(path).parent)

    def _all_dirs(self) -> set[str]:
        dirs = set(self._dirs)
        for path in self._files:
            self._ensure_parent_dirs(path)
            dirs.add(self._parent(path))
        return dirs


__all__ = ["InMemoryEnvironment"]
