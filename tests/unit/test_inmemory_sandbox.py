import pytest

from email_agent.models.sandbox import BashResult, ProjectedFile, ToolCall
from email_agent.sandbox.inmemory import InMemorySandbox


@pytest.mark.asyncio
async def test_project_and_read_email_file():
    s = InMemorySandbox()
    await s.ensure_started("a-1")
    await s.project_emails(
        "a-1",
        [ProjectedFile(path="emails/t/0001.md", content=b"hi")],
    )
    result = await s.run_tool(
        "a-1",
        "r-1",
        ToolCall(kind="read", path="/workspace/emails/t/0001.md"),
    )
    assert result.ok
    assert result.output == "hi"


@pytest.mark.asyncio
async def test_write_under_emails_is_rejected():
    s = InMemorySandbox()
    await s.ensure_started("a-1")
    result = await s.run_tool(
        "a-1",
        "r-1",
        ToolCall(kind="write", path="/workspace/emails/x.md", content="x"),
    )
    assert not result.ok
    assert "read-only" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_write_then_read_round_trips():
    s = InMemorySandbox()
    await s.ensure_started("a-1")
    await s.run_tool(
        "a-1", "r-1", ToolCall(kind="write", path="/workspace/notes.md", content="hello")
    )
    out = await s.run_tool("a-1", "r-1", ToolCall(kind="read", path="/workspace/notes.md"))
    assert out.output == "hello"


@pytest.mark.asyncio
async def test_edit_replaces_substring():
    s = InMemorySandbox()
    await s.ensure_started("a-1")
    await s.run_tool(
        "a-1", "r-1", ToolCall(kind="write", path="/workspace/x.md", content="abc def")
    )
    await s.run_tool(
        "a-1", "r-1", ToolCall(kind="edit", path="/workspace/x.md", old="abc", new="ABC")
    )
    out = await s.run_tool("a-1", "r-1", ToolCall(kind="read", path="/workspace/x.md"))
    assert out.output == "ABC def"


@pytest.mark.asyncio
async def test_bash_runs_and_captures_stdout():
    s = InMemorySandbox()
    await s.ensure_started("a-1")
    out = await s.run_tool("a-1", "r-1", ToolCall(kind="bash", command="echo hello"))
    assert out.ok
    assert isinstance(out.output, BashResult)
    assert out.output.exit_code == 0
    assert "hello" in out.output.stdout


@pytest.mark.asyncio
async def test_filesystem_is_isolated_per_assistant():
    s = InMemorySandbox()
    await s.ensure_started("a-1")
    await s.ensure_started("a-2")
    await s.run_tool("a-1", "r-1", ToolCall(kind="write", path="/workspace/a.md", content="A"))
    out = await s.run_tool("a-2", "r-1", ToolCall(kind="read", path="/workspace/a.md"))
    assert not out.ok


@pytest.mark.asyncio
async def test_reset_wipes_workspace():
    s = InMemorySandbox()
    await s.ensure_started("a-1")
    await s.run_tool("a-1", "r-1", ToolCall(kind="write", path="/workspace/x.md", content="x"))
    await s.reset("a-1")
    await s.ensure_started("a-1")
    out = await s.run_tool("a-1", "r-1", ToolCall(kind="read", path="/workspace/x.md"))
    assert not out.ok


@pytest.mark.asyncio
async def test_attachments_round_trip():
    s = InMemorySandbox()
    await s.ensure_started("a-1")
    await s.project_attachments(
        "a-1", "r-1", [ProjectedFile(path="report.pdf", content=b"%PDF-data")]
    )
    data = await s.read_attachment_out("a-1", "r-1", "report.pdf")
    assert data == b"%PDF-data"
