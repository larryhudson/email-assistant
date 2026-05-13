from decimal import Decimal

from email_agent.agent.toolset import AgentToolset
from email_agent.memory.inmemory import InMemoryMemoryAdapter
from email_agent.models.agent import MeteredUsage
from email_agent.models.sandbox import PendingAttachment
from email_agent.sandbox.inmemory_environment import InMemoryEnvironment
from email_agent.sandbox.workspace import AssistantWorkspace
from email_agent.search.inmemory import InMemorySearchAdapter
from email_agent.search.port import SearchResult


def _toolset(
    env: InMemoryEnvironment,
    *,
    memory: InMemoryMemoryAdapter | None = None,
    pending: list[PendingAttachment] | None = None,
    metered: list[MeteredUsage] | None = None,
    search: InMemorySearchAdapter | None = None,
) -> AgentToolset:
    return AgentToolset(
        assistant_id="a-1",
        run_id="r-1",
        env=env,
        workspace=AssistantWorkspace(env),
        memory=memory or InMemoryMemoryAdapter(),
        pending_attachments=pending if pending is not None else [],
        metered_usage=metered if metered is not None else [],
        search=search,
    )


async def test_read_returns_file_contents() -> None:
    env = InMemoryEnvironment()
    await env.write_text("notes/draft.md", "hello")

    assert await _toolset(env).read("notes/draft.md") == "hello"


async def test_read_returns_error_text_instead_of_raising() -> None:
    result = await _toolset(InMemoryEnvironment()).read("missing.md")

    assert "ERROR: read(missing.md) failed" in result
    assert "missing.md" in result


async def test_write_rejects_emails_directory_and_writes_other_paths() -> None:
    env = InMemoryEnvironment()
    toolset = _toolset(env)

    rejected = await toolset.write("emails/x.md", "x")
    written = await toolset.write("notes/draft.md", "hello")

    assert "ERROR: write(emails/x.md) failed" in rejected
    assert "read-only" in rejected
    assert written == "wrote notes/draft.md"
    assert await env.read_text("notes/draft.md") == "hello"


async def test_edit_replaces_first_match_and_reports_missing_old_text() -> None:
    env = InMemoryEnvironment()
    await env.write_text("notes/draft.md", "hello hello")
    toolset = _toolset(env)

    edited = await toolset.edit("notes/draft.md", "hello", "hi")
    missing = await toolset.edit("notes/draft.md", "nope", "x")

    assert edited == "edited notes/draft.md"
    assert await env.read_text("notes/draft.md") == "hi hello"
    assert "ERROR: edit(notes/draft.md) failed" in missing
    assert "old string not found" in missing


async def test_bash_returns_existing_model_facing_format() -> None:
    result = await _toolset(InMemoryEnvironment()).bash("printf hello")

    assert result == "exit_code=0\nstdout:\nhello\nstderr:\n"


async def test_attach_file_appends_pending_attachment() -> None:
    env = InMemoryEnvironment()
    await env.write_text("out/report.txt", "report")
    pending: list[PendingAttachment] = []
    toolset = _toolset(env, pending=pending)

    result = await toolset.attach_file("out/report.txt")

    assert result == "attached out/report.txt"
    assert pending == [PendingAttachment(sandbox_path="out/report.txt", filename="report.txt")]


async def test_attach_file_returns_error_for_missing_file() -> None:
    result = await _toolset(InMemoryEnvironment()).attach_file("missing.txt")

    assert "ERROR: attach_file(missing.txt) failed" in result
    assert "not found" in result


async def test_memory_search_delegates_by_assistant_id() -> None:
    memory = InMemoryMemoryAdapter()
    await memory.record_turn("a-1", "t-1", "assistant", "likes short replies")
    await memory.record_turn("a-2", "t-1", "assistant", "other assistant")

    result = await _toolset(InMemoryEnvironment(), memory=memory).memory_search("short")

    assert isinstance(result, list)
    assert [m.content for m in result] == ["[t-1/assistant] likes short replies"]


async def test_web_search_runs_on_host_adapter_and_records_metered_usage() -> None:
    metered: list[MeteredUsage] = []
    search = InMemorySearchAdapter(
        results=[
            SearchResult(
                title="Result title",
                url="https://example.com/news",
                snippet="Fresh public web content",
                age="1 day ago",
            )
        ],
        cost_usd=Decimal("0.0050"),
    )

    result = await _toolset(
        InMemoryEnvironment(),
        metered=metered,
        search=search,
    ).web_search("latest thing", max_results=3)

    assert search.calls == [("latest thing", 3)]
    assert "UNTRUSTED EXTERNAL WEB SEARCH RESULTS" in result
    assert "not from the user" in result
    assert "Fresh public web content" in result
    assert metered == [
        MeteredUsage(
            provider="brave",
            model="web-search",
            cost_usd=Decimal("0.0050"),
            tool_name="web_search",
        )
    ]
