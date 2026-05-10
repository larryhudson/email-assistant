import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


def test_docker_daemon_is_reachable():
    import docker

    client = docker.from_env()
    assert client.ping() is True


@pytest.fixture
def assistant_id() -> str:
    return f"a-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def bash_timeout_seconds(request) -> int:
    """Default bash timeout. Override with `@pytest.mark.parametrize(..., indirect=True)`."""
    return getattr(request, "param", 10)


@pytest.fixture
async def sandbox(
    tmp_path: Path, assistant_id: str, bash_timeout_seconds: int
) -> AsyncIterator["object"]:  # type: ignore[name-defined]
    import contextlib

    import docker.errors

    import docker
    from email_agent.sandbox.docker import DockerSandbox

    client = docker.from_env()
    sb = DockerSandbox(
        client=client,
        image="email-agent-sandbox:slice4",
        sandbox_data_root=tmp_path / "sandboxes",
        memory_mb=512,
        cpu_cores=1.0,
        bash_timeout_seconds=bash_timeout_seconds,
    )
    try:
        yield sb
    finally:
        with contextlib.suppress(docker.errors.NotFound):
            client.containers.get(f"email-agent-sandbox-{assistant_id}").remove(force=True)


async def test_ensure_started_creates_running_container(sandbox, assistant_id, tmp_path):
    import docker

    await sandbox.ensure_started(assistant_id)

    client = docker.from_env()
    container = client.containers.get(f"email-agent-sandbox-{assistant_id}")
    assert container.status == "running"

    workspace_dir = tmp_path / "sandboxes" / assistant_id / "workspace"
    assert workspace_dir.is_dir()

    host_config = container.attrs["HostConfig"]
    assert host_config["NanoCpus"] == 1_000_000_000
    assert host_config["Memory"] == 512 * 1024 * 1024

    exit_code, _ = container.exec_run(["test", "-d", "/workspace"])
    assert exit_code == 0


async def test_ensure_started_is_idempotent(sandbox, assistant_id):
    import docker

    await sandbox.ensure_started(assistant_id)
    client = docker.from_env()
    container_id = client.containers.get(f"email-agent-sandbox-{assistant_id}").id

    await sandbox.ensure_started(assistant_id)

    same_container = client.containers.get(f"email-agent-sandbox-{assistant_id}")
    assert same_container.id == container_id
    assert same_container.status == "running"


async def test_project_emails_writes_files_into_workspace(sandbox, assistant_id):
    from email_agent.models.sandbox import ProjectedFile

    await sandbox.ensure_started(assistant_id)
    await sandbox.project_emails(
        assistant_id,
        [
            ProjectedFile(path="emails/t-1/thread.md", content=b"# thread"),
            ProjectedFile(path="emails/t-1/0001-msg.md", content=b"hello"),
        ],
    )

    import docker

    client = docker.from_env()
    container = client.containers.get(f"email-agent-sandbox-{assistant_id}")
    code, out = container.exec_run(["cat", "/workspace/emails/t-1/thread.md"])
    assert code == 0
    assert out == b"# thread"
    code, out = container.exec_run(["cat", "/workspace/emails/t-1/0001-msg.md"])
    assert code == 0
    assert out == b"hello"


async def test_project_emails_wipes_previous_projection(sandbox, assistant_id):
    from email_agent.models.sandbox import ProjectedFile

    await sandbox.ensure_started(assistant_id)
    await sandbox.project_emails(
        assistant_id,
        [ProjectedFile(path="emails/t-1/old.md", content=b"old")],
    )
    await sandbox.project_emails(
        assistant_id,
        [ProjectedFile(path="emails/t-2/new.md", content=b"new")],
    )

    import docker

    client = docker.from_env()
    container = client.containers.get(f"email-agent-sandbox-{assistant_id}")
    code, _ = container.exec_run(["test", "-f", "/workspace/emails/t-1/old.md"])
    assert code != 0
    code, out = container.exec_run(["cat", "/workspace/emails/t-2/new.md"])
    assert code == 0
    assert out == b"new"


async def test_project_attachments_lands_under_run_dir(sandbox, assistant_id):
    from email_agent.models.sandbox import ProjectedFile

    await sandbox.ensure_started(assistant_id)
    await sandbox.project_attachments(
        assistant_id,
        "r-1",
        [ProjectedFile(path="report.pdf", content=b"%PDF-1.7")],
    )

    import docker

    client = docker.from_env()
    container = client.containers.get(f"email-agent-sandbox-{assistant_id}")
    code, out = container.exec_run(["cat", "/workspace/attachments/r-1/report.pdf"])
    assert code == 0
    assert out == b"%PDF-1.7"


async def test_run_tool_read_returns_file_contents(sandbox, assistant_id):
    from email_agent.models.sandbox import ProjectedFile, ToolCall

    await sandbox.ensure_started(assistant_id)
    await sandbox.project_emails(
        assistant_id,
        [ProjectedFile(path="emails/t-1/thread.md", content=b"# greetings\n")],
    )

    result = await sandbox.run_tool(
        assistant_id,
        "r-1",
        ToolCall(kind="read", path="emails/t-1/thread.md"),
    )

    assert result.ok is True
    assert result.output == "# greetings\n"


async def test_run_tool_read_missing_file_returns_error(sandbox, assistant_id):
    from email_agent.models.sandbox import ToolCall

    await sandbox.ensure_started(assistant_id)
    result = await sandbox.run_tool(assistant_id, "r-1", ToolCall(kind="read", path="nope.md"))

    assert result.ok is False
    assert result.error is not None


async def test_run_tool_write_creates_file(sandbox, assistant_id):
    from email_agent.models.sandbox import ToolCall

    await sandbox.ensure_started(assistant_id)
    write_result = await sandbox.run_tool(
        assistant_id,
        "r-1",
        ToolCall(kind="write", path="notes/draft.md", content="hello\n"),
    )
    assert write_result.ok is True

    read_result = await sandbox.run_tool(
        assistant_id, "r-1", ToolCall(kind="read", path="notes/draft.md")
    )
    assert read_result.ok is True
    assert read_result.output == "hello\n"


async def test_run_tool_write_refuses_under_emails(sandbox, assistant_id):
    from email_agent.models.sandbox import ProjectedFile, ToolCall

    await sandbox.ensure_started(assistant_id)
    await sandbox.project_emails(
        assistant_id,
        [ProjectedFile(path="emails/t-1/thread.md", content=b"original\n")],
    )

    result = await sandbox.run_tool(
        assistant_id,
        "r-1",
        ToolCall(kind="write", path="emails/t-1/thread.md", content="tampered\n"),
    )
    assert result.ok is False
    assert result.error is not None
    assert "read-only" in result.error.lower() or "refuse" in result.error.lower()

    # Original unchanged
    read_result = await sandbox.run_tool(
        assistant_id, "r-1", ToolCall(kind="read", path="emails/t-1/thread.md")
    )
    assert read_result.output == "original\n"


async def test_run_tool_edit_applies_replacement(sandbox, assistant_id):
    from email_agent.models.sandbox import ToolCall

    await sandbox.ensure_started(assistant_id)
    await sandbox.run_tool(
        assistant_id,
        "r-1",
        ToolCall(kind="write", path="notes/plan.md", content="hello world\n"),
    )

    edit_result = await sandbox.run_tool(
        assistant_id,
        "r-1",
        ToolCall(kind="edit", path="notes/plan.md", old="world", new="planet"),
    )
    assert edit_result.ok is True

    read_result = await sandbox.run_tool(
        assistant_id, "r-1", ToolCall(kind="read", path="notes/plan.md")
    )
    assert read_result.output == "hello planet\n"


async def test_run_tool_edit_old_not_found_returns_error(sandbox, assistant_id):
    from email_agent.models.sandbox import ToolCall

    await sandbox.ensure_started(assistant_id)
    await sandbox.run_tool(
        assistant_id,
        "r-1",
        ToolCall(kind="write", path="notes/plan.md", content="hello\n"),
    )

    edit_result = await sandbox.run_tool(
        assistant_id,
        "r-1",
        ToolCall(kind="edit", path="notes/plan.md", old="missing", new="x"),
    )
    assert edit_result.ok is False
    assert edit_result.error is not None
    assert "not found" in edit_result.error.lower()

    read_result = await sandbox.run_tool(
        assistant_id, "r-1", ToolCall(kind="read", path="notes/plan.md")
    )
    assert read_result.output == "hello\n"


async def test_run_tool_bash_captures_stdout_exit(sandbox, assistant_id):
    from email_agent.models.sandbox import BashResult, ToolCall

    await sandbox.ensure_started(assistant_id)
    result = await sandbox.run_tool(
        assistant_id, "r-1", ToolCall(kind="bash", command="echo hello")
    )
    assert result.ok is True
    assert isinstance(result.output, BashResult)
    assert result.output.exit_code == 0
    assert "hello" in result.output.stdout


@pytest.mark.parametrize("bash_timeout_seconds", [2], indirect=True)
async def test_run_tool_bash_times_out(sandbox, assistant_id):
    from email_agent.models.sandbox import ToolCall

    await sandbox.ensure_started(assistant_id)
    result = await sandbox.run_tool(assistant_id, "r-1", ToolCall(kind="bash", command="sleep 30"))
    assert result.ok is False
    assert result.error is not None
    assert "timeout" in result.error.lower() or "timed out" in result.error.lower()


async def test_attach_file_validates_existence_and_read_attachment_out(sandbox, assistant_id):
    from email_agent.models.sandbox import ToolCall

    await sandbox.ensure_started(assistant_id)
    await sandbox.run_tool(
        assistant_id,
        "r-1",
        ToolCall(kind="write", path="notes/report.pdf", content="%PDF-1.7"),
    )

    attach_result = await sandbox.run_tool(
        assistant_id, "r-1", ToolCall(kind="attach_file", path="notes/report.pdf")
    )
    assert attach_result.ok is True

    bytes_out = await sandbox.read_attachment_out(assistant_id, "r-1", "notes/report.pdf")
    assert bytes_out == b"%PDF-1.7"


async def test_attach_file_missing_file_fails(sandbox, assistant_id):
    from email_agent.models.sandbox import ToolCall

    await sandbox.ensure_started(assistant_id)
    result = await sandbox.run_tool(
        assistant_id, "r-1", ToolCall(kind="attach_file", path="nope.pdf")
    )
    assert result.ok is False
    assert result.error is not None


async def test_reset_wipes_workspace_and_recreates_container(sandbox, assistant_id):
    import docker
    from email_agent.models.sandbox import ToolCall

    await sandbox.ensure_started(assistant_id)
    await sandbox.run_tool(
        assistant_id,
        "r-1",
        ToolCall(kind="write", path="notes/persistent.md", content="hi\n"),
    )

    client = docker.from_env()
    original_id = client.containers.get(f"email-agent-sandbox-{assistant_id}").id

    await sandbox.reset(assistant_id)
    await sandbox.ensure_started(assistant_id)

    new_id = client.containers.get(f"email-agent-sandbox-{assistant_id}").id
    assert new_id != original_id

    read_result = await sandbox.run_tool(
        assistant_id, "r-1", ToolCall(kind="read", path="notes/persistent.md")
    )
    assert read_result.ok is False
