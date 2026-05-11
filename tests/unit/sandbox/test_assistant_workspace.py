import pytest

from email_agent.models.sandbox import ProjectedFile
from email_agent.sandbox.inmemory_environment import InMemoryEnvironment
from email_agent.sandbox.workspace import AssistantWorkspace, WorkspacePolicyError


async def test_project_emails_writes_under_workspace_emails_and_wipes_stale_files() -> None:
    env = InMemoryEnvironment()
    workspace = AssistantWorkspace(env)
    await env.write_text("/workspace/emails/stale.md", "stale")

    await workspace.project_emails(
        [
            ProjectedFile(path="emails/t-1/thread.md", content=b"thread"),
            ProjectedFile(path="t-1/0001.md", content=b"message"),
        ]
    )

    assert not await env.exists("/workspace/emails/stale.md")
    assert await env.read_text("/workspace/emails/t-1/thread.md") == "thread"
    assert await env.read_text("/workspace/emails/t-1/0001.md") == "message"


async def test_project_attachments_writes_under_run_attachment_directory() -> None:
    env = InMemoryEnvironment()
    workspace = AssistantWorkspace(env)

    await workspace.project_attachments(
        "r-1",
        [
            ProjectedFile(path="report.pdf", content=b"%PDF"),
            ProjectedFile(path="nested/data.csv", content=b"a,b"),
        ],
    )

    assert await env.read_bytes("/workspace/attachments/r-1/report.pdf") == b"%PDF"
    assert await env.read_bytes("/workspace/attachments/r-1/nested/data.csv") == b"a,b"


async def test_read_outbound_attachment_reads_generated_workspace_file() -> None:
    env = InMemoryEnvironment()
    workspace = AssistantWorkspace(env)
    await env.write_bytes("/workspace/out/report.pdf", b"%PDF")

    assert await workspace.read_outbound_attachment("/workspace/out/report.pdf") == b"%PDF"
    assert await workspace.read_outbound_attachment("out/report.pdf") == b"%PDF"


@pytest.mark.parametrize(
    "path",
    [
        "/workspace/emails/x.md",
        "emails/x.md",
        "/workspace/emails",
        "emails",
    ],
)
async def test_agent_write_policy_rejects_emails_directory(path: str) -> None:
    workspace = AssistantWorkspace(InMemoryEnvironment())

    with pytest.raises(WorkspacePolicyError, match="read-only"):
        await workspace.assert_agent_write_allowed(path)


@pytest.mark.parametrize("path", ["/workspace/notes.md", "notes.md", "scratch/x.txt"])
async def test_agent_write_policy_allows_non_email_paths(path: str) -> None:
    workspace = AssistantWorkspace(InMemoryEnvironment())

    await workspace.assert_agent_write_allowed(path)
