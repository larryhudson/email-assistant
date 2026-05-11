from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class FileStat:
    is_file: bool
    is_dir: bool
    is_symlink: bool
    size: int
    mtime: datetime | None = None


@dataclass(frozen=True)
class ShellResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


@runtime_checkable
class SandboxEnvironment(Protocol):
    """Generic filesystem + shell environment for an assistant workspace."""

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: int | None = None,
    ) -> ShellResult: ...

    async def read_text(self, path: str) -> str: ...

    async def read_bytes(self, path: str) -> bytes: ...

    async def write_text(self, path: str, content: str) -> None: ...

    async def write_bytes(self, path: str, content: bytes) -> None: ...

    async def stat(self, path: str) -> FileStat: ...

    async def readdir(self, path: str) -> list[str]: ...

    async def exists(self, path: str) -> bool: ...

    async def mkdir(self, path: str, *, parents: bool = False) -> None: ...

    async def rm(self, path: str, *, recursive: bool = False, force: bool = False) -> None: ...


__all__ = ["FileStat", "SandboxEnvironment", "ShellResult"]
