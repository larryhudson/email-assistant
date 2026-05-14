import io
import tarfile

from email_agent.sandbox.bashkit_environment import (
    BashkitEnvironment,
    BashkitSnapshotStore,
    BashkitWorkspaceProvider,
)


async def test_text_and_bytes_round_trip_under_workspace() -> None:
    env = BashkitEnvironment()

    await env.write_text("notes/draft.md", "hello")
    await env.write_bytes("/workspace/bin/data", b"\x00\x01")

    assert await env.read_text("/workspace/notes/draft.md") == "hello"
    assert await env.read_bytes("bin/data") == b"\x00\x01"
    assert await env.exists("notes/draft.md")
    assert await env.exists("/workspace/bin/data")


async def test_directory_operations() -> None:
    env = BashkitEnvironment()

    await env.mkdir("notes/archive", parents=True)
    await env.write_text("notes/today.md", "today")
    await env.write_text("notes/archive/old.md", "old")

    assert await env.readdir("notes") == ["archive", "today.md"]

    notes_stat = await env.stat("notes")
    file_stat = await env.stat("notes/today.md")
    assert notes_stat.is_dir
    assert not notes_stat.is_file
    assert file_stat.is_file
    assert not file_stat.is_dir
    assert file_stat.size == len("today")

    await env.rm("notes/archive", recursive=True)
    assert not await env.exists("notes/archive/old.md")

    await env.rm("missing", force=True)


async def test_exec_runs_in_workspace_and_persists_vfs_changes() -> None:
    env = BashkitEnvironment()

    result = await env.exec("printf hello > greeting.txt && pwd")

    assert result.exit_code == 0
    assert result.stdout == "/workspace\n"
    assert result.stderr == ""
    assert result.duration_ms >= 0
    assert await env.read_text("greeting.txt") == "hello"


async def test_exec_accepts_cwd() -> None:
    env = BashkitEnvironment()
    await env.mkdir("subdir", parents=True)

    result = await env.exec("pwd", cwd="subdir")

    assert result.exit_code == 0
    assert result.stdout == "/workspace/subdir\n"


async def test_exec_supports_embedded_python() -> None:
    env = BashkitEnvironment()

    result = await env.exec("python3 -c 'print(sum(i*i for i in range(1, 101)))'")

    assert result.exit_code == 0
    assert result.stdout == "338350\n"


async def test_exec_supports_embedded_sqlite() -> None:
    env = BashkitEnvironment()

    result = await env.exec(
        "sqlite3 data.db 'create table t(x); insert into t values (42); select x from t;'"
    )

    assert result.exit_code == 0
    assert result.stdout == "42\n"


async def test_embedded_sqlite_can_be_disabled() -> None:
    env = BashkitEnvironment(sqlite_enabled=False)

    result = await env.exec("sqlite3 data.db 'select 1;'")

    assert result.exit_code == 127
    assert "sqlite3: command not found" in result.stderr


async def test_provider_reuses_workspace_per_assistant() -> None:
    provider = BashkitWorkspaceProvider()

    first = await provider.get_workspace("a-1")
    second = await provider.get_workspace("a-1")
    other = await provider.get_workspace("a-2")

    await first.environment.write_text("notes.md", "persist")

    assert second is first
    assert other is not first
    assert await second.environment.read_text("notes.md") == "persist"
    assert not await other.environment.exists("notes.md")


async def test_provider_loads_and_saves_snapshot(tmp_path) -> None:
    store = BashkitSnapshotStore(tmp_path / "bashkit")
    provider = BashkitWorkspaceProvider(snapshot_store=store)

    first = await provider.get_workspace("a-1")
    await first.environment.write_text("notes.md", "persist")
    await provider.persist_workspace("a-1")

    restarted = BashkitWorkspaceProvider(snapshot_store=store)
    second = await restarted.get_workspace("a-1")

    assert await second.environment.read_text("notes.md") == "persist"


async def test_import_workspace_tar_imports_text_and_reports_skipped_binary() -> None:
    env = BashkitEnvironment()
    archive = _tar_bytes(
        [
            ("notes", None),
            ("notes/a.txt", b"hello"),
            ("bin/blob.dat", b"\xff\x00"),
        ]
    )

    report = await env.import_workspace_tar(archive)

    assert report.directories_imported == 1
    assert report.files_imported == 1
    assert report.binary_files_skipped == 1
    assert await env.read_text("notes/a.txt") == "hello"
    assert not await env.exists("bin/blob.dat")


def _tar_bytes(entries: list[tuple[str, bytes | None]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in entries:
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()
