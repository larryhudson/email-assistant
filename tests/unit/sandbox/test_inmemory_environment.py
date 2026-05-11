from email_agent.sandbox.inmemory_environment import InMemoryEnvironment


async def test_text_and_bytes_round_trip_under_workspace() -> None:
    env = InMemoryEnvironment()

    await env.write_text("notes/draft.md", "hello")
    await env.write_bytes("/workspace/bin/data", b"\x00\x01")

    assert await env.read_text("/workspace/notes/draft.md") == "hello"
    assert await env.read_bytes("bin/data") == b"\x00\x01"
    assert await env.exists("notes/draft.md")
    assert await env.exists("/workspace/bin/data")


async def test_directory_operations() -> None:
    env = InMemoryEnvironment()

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


async def test_exec_returns_shell_result() -> None:
    env = InMemoryEnvironment()

    result = await env.exec("printf hello")

    assert result.exit_code == 0
    assert result.stdout == "hello"
    assert result.stderr == ""
    assert result.duration_ms >= 0
