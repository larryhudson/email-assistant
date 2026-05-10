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
async def sandbox(tmp_path: Path, assistant_id: str) -> AsyncIterator["object"]:  # type: ignore[name-defined]
    import docker
    from email_agent.sandbox.docker import DockerSandbox

    client = docker.from_env()
    sb = DockerSandbox(
        client=client,
        image="email-agent-sandbox:slice4",
        sandbox_data_root=tmp_path / "sandboxes",
        memory_mb=512,
        cpu_cores=1.0,
        bash_timeout_seconds=10,
    )
    try:
        yield sb
    finally:
        # Tear down container if it exists.
        try:
            container = client.containers.get(f"email-agent-sandbox-{assistant_id}")
            container.remove(force=True)
        except docker.errors.NotFound:
            pass


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
