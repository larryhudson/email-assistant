from pathlib import Path

from email_agent.sandbox.inmemory_environment import InMemoryEnvironment
from email_agent.sandbox.source_projection import project_source


async def test_project_source_exposes_files_under_workspace_source(tmp_path: Path) -> None:
    """After projecting a source tree, files are readable at /workspace/source/<relpath>.

    This is the load-bearing claim: the agent can read its own source code as
    if it were a normal workspace file via the existing `read` tool.
    """
    (tmp_path / "CLAUDE.md").write_text("hello from CLAUDE")
    env = InMemoryEnvironment()

    await project_source(env, tmp_path)

    assert await env.read_text("/workspace/source/CLAUDE.md") == "hello from CLAUDE"


async def test_project_source_picks_up_upstream_changes_on_refresh(tmp_path: Path) -> None:
    """Re-projecting reflects whatever the source files currently contain —
    the agent's view of its own source matches the deployed code."""
    source_file = tmp_path / "README.md"
    source_file.write_text("v1")
    env = InMemoryEnvironment()

    await project_source(env, tmp_path)
    assert await env.read_text("/workspace/source/README.md") == "v1"

    source_file.write_text("v2")
    await project_source(env, tmp_path)
    assert await env.read_text("/workspace/source/README.md") == "v2"


async def test_project_source_wipes_agent_edits_under_source(tmp_path: Path) -> None:
    """The 'your edits won't persist' promise: any file the agent writes under
    /workspace/source/ is gone after the next projection. Source is ground truth."""
    (tmp_path / "CLAUDE.md").write_text("upstream content")
    env = InMemoryEnvironment()
    await project_source(env, tmp_path)

    await env.write_text("/workspace/source/CLAUDE.md", "agent scribble")
    await env.write_text("/workspace/source/agent_made_this.md", "rogue")

    await project_source(env, tmp_path)

    assert await env.read_text("/workspace/source/CLAUDE.md") == "upstream content"
    assert not await env.exists("/workspace/source/agent_made_this.md")


async def test_project_source_includes_git_excludes_noise_and_secrets(
    tmp_path: Path,
) -> None:
    """The agent gets to see .git/ (so it can `git log` for recent changes)
    but not the bulky/sensitive stuff — virtualenvs, env files, pycache, data."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("git internals")
    (tmp_path / ".gitignore").write_text("data/\n")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "lib").mkdir()
    (tmp_path / ".venv" / "lib" / "some.py").write_text("vendor noise")
    (tmp_path / ".env").write_text("SECRET=xxx")
    (tmp_path / ".env.local").write_text("LOCAL_SECRET=xxx")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "mod.cpython-313.pyc").write_bytes(b"\x00")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "secret.json").write_text("nope")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("real code")

    env = InMemoryEnvironment()
    await project_source(env, tmp_path)

    # Included: real code, git history, root dotfiles that aren't secrets.
    assert await env.read_text("/workspace/source/src/mod.py") == "real code"
    assert await env.read_text("/workspace/source/.git/config") == "git internals"
    assert await env.read_text("/workspace/source/.gitignore") == "data/\n"
    # Excluded: vendored deps, env files, build artefacts, data.
    assert not await env.exists("/workspace/source/.venv/lib/some.py")
    assert not await env.exists("/workspace/source/.env")
    assert not await env.exists("/workspace/source/.env.local")
    assert not await env.exists("/workspace/source/__pycache__/mod.cpython-313.pyc")
    assert not await env.exists("/workspace/source/data/secret.json")
