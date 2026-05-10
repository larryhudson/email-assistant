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
