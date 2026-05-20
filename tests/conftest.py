from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from email_agent.db import models  # noqa: F401  # registers ORM tables on Base.metadata
from email_agent.db.base import Base


@pytest.fixture(scope="session")
def stub_project_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Minimal project root for tests that trigger source projection.

    The real repo's .git/ alone is ~8 MB / 1500+ files; copying it into an
    in-memory env for every runtime test is wasted work since we're not
    testing git itself. Slice-4 unit tests in test_source_projection.py
    pass their own tmp_path and don't depend on this stub.
    """
    root = tmp_path_factory.mktemp("stub_project_root")
    (root / "pyproject.toml").write_text("[project]\nname = 'stub'\nversion = '0'\n")
    return root


@pytest.fixture(autouse=True)
def _patch_default_project_root(stub_project_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto-redirect the runtime's source projection to a minimal stub root."""
    monkeypatch.setattr(
        "email_agent.runtime.assistant_runtime.DEFAULT_PROJECT_ROOT",
        stub_project_root,
    )


@pytest.fixture
async def sqlite_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def sqlite_session_factory(
    sqlite_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(sqlite_engine, expire_on_commit=False)
