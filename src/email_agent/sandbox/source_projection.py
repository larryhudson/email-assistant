"""Project the email-agent project source into `/workspace/source/`.

Refreshed on every run so the agent sees the currently-deployed code as
its own source of truth. Read-only by convention (edits don't persist —
the next run wipes and re-projects).
"""

import asyncio
from pathlib import Path

from email_agent.sandbox.environment import SandboxEnvironment

SOURCE_DIR = "/workspace/source"


def _find_project_root(start: Path) -> Path:
    """Walk up from `start` looking for pyproject.toml — that's the project root.

    Avoids hardcoding `parents[N]`, which breaks when files move. Raises if no
    pyproject.toml is found anywhere up the tree (e.g. when the package is
    installed without source — the caller should pass an explicit path then).
    """
    for candidate in [start, *start.parents]:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError(f"no pyproject.toml found at or above {start}")


# Override at the call site (tests pass a tmp_path; Docker deploys can point
# at an explicit checkout).
DEFAULT_PROJECT_ROOT = _find_project_root(Path(__file__).resolve())

_EXCLUDED_DIR_NAMES = frozenset(
    {
        "__pycache__",
        "node_modules",
        "data",
        "dist",
        "build",
        "htmlcov",
        ".venv",
        ".ruff_cache",
        ".mypy_cache",
        ".pytest_cache",
        ".idea",
        ".vscode",
    }
)
_EXCLUDED_FILE_SUFFIXES = frozenset({".pyc", ".log"})
_EXCLUDED_FILE_NAMES = frozenset({".DS_Store"})


def _is_env_file(name: str) -> bool:
    """Block .env and .env.<anything> so secrets never reach the projection."""
    return name == ".env" or name.startswith(".env.")


def _is_excluded(rel: Path) -> bool:
    for part in rel.parts:
        if part in _EXCLUDED_DIR_NAMES:
            return True
    if rel.suffix in _EXCLUDED_FILE_SUFFIXES:
        return True
    if rel.name in _EXCLUDED_FILE_NAMES:
        return True
    return _is_env_file(rel.name)


def _read_source_files(source_root: Path) -> list[tuple[str, bytes]]:
    """Walk `source_root` and return (relpath, bytes) pairs for projected files."""
    files: list[tuple[str, bytes]] = []
    for path in sorted(source_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(source_root)
        if _is_excluded(rel):
            continue
        files.append((rel.as_posix(), path.read_bytes()))
    return files


async def project_source(env: SandboxEnvironment, source_root: Path) -> None:
    """Mirror tracked project files from `source_root` into /workspace/source/.

    Wipes any previous projection so upstream changes propagate and agent
    edits under /workspace/source/ never survive a refresh.
    """
    await env.rm(SOURCE_DIR, recursive=True, force=True)
    await env.mkdir(SOURCE_DIR, parents=True)

    files = await asyncio.to_thread(_read_source_files, source_root)

    for relpath, content in files:
        rel = Path(relpath)
        if rel.parent != Path("."):
            await env.mkdir(f"{SOURCE_DIR}/{rel.parent.as_posix()}", parents=True)
        await env.write_bytes(f"{SOURCE_DIR}/{relpath}", content)


__all__ = ["DEFAULT_PROJECT_ROOT", "SOURCE_DIR", "project_source"]
