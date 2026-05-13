import contextlib
import uuid
from collections.abc import AsyncIterator

import pytest

from email_agent.models.sandbox import ProjectedFile

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


def test_docker_daemon_is_reachable():
    import docker

    client = docker.from_env()
    assert client.ping() is True


@pytest.fixture
def assistant_id() -> str:
    return f"a-{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def workspace_provider(tmp_path, assistant_id: str) -> AsyncIterator["object"]:
    import docker.errors

    import docker
    from email_agent.sandbox.docker_environment import (
        CONTAINER_NAME_PREFIX,
        WORKSPACE_VOLUME_PREFIX,
        DockerWorkspaceProvider,
    )

    client = docker.from_env()
    provider = DockerWorkspaceProvider(
        client=client,
        image="email-agent-sandbox:slice4",
        sandbox_data_root=tmp_path / "sandboxes",
        memory_mb=512,
        cpu_cores=1.0,
        bash_timeout_seconds=10,
    )
    try:
        yield provider
    finally:
        with contextlib.suppress(docker.errors.NotFound):
            client.containers.get(f"{CONTAINER_NAME_PREFIX}{assistant_id}").remove(force=True)
        with contextlib.suppress(docker.errors.NotFound, docker.errors.APIError):
            client.volumes.get(f"{WORKSPACE_VOLUME_PREFIX}{assistant_id}").remove(force=True)


async def test_workspace_provider_uses_named_volume_not_host_bind(
    workspace_provider, assistant_id: str
):
    import docker
    from email_agent.sandbox.docker_environment import (
        CONTAINER_NAME_PREFIX,
        WORKSPACE_ROOT,
        WORKSPACE_VOLUME_PREFIX,
    )

    await workspace_provider.get_workspace(assistant_id)

    client = docker.from_env()
    container = client.containers.get(f"{CONTAINER_NAME_PREFIX}{assistant_id}")
    mount = next(m for m in container.attrs["Mounts"] if m["Destination"] == WORKSPACE_ROOT)

    assert mount["Type"] == "volume"
    assert mount["Name"] == f"{WORKSPACE_VOLUME_PREFIX}{assistant_id}"
    assert "/home/larry" not in mount.get("Source", "")


async def test_workspace_provider_overrides_dns_without_disabling_network(
    workspace_provider, assistant_id: str
):
    import docker
    from email_agent.sandbox.docker_environment import (
        CONTAINER_NAME_PREFIX,
        SANDBOX_DNS_SEARCH,
        SANDBOX_DNS_SERVERS,
    )

    await workspace_provider.get_workspace(assistant_id)

    client = docker.from_env()
    container = client.containers.get(f"{CONTAINER_NAME_PREFIX}{assistant_id}")
    host_config = container.attrs["HostConfig"]

    assert host_config["Dns"] == SANDBOX_DNS_SERVERS
    assert host_config["DnsSearch"] == SANDBOX_DNS_SEARCH
    assert host_config["DnsOptions"] in (None, [])

    code, output = container.exec_run(["cat", "/etc/resolv.conf"])
    assert code == 0
    resolv_conf = output.decode("utf-8", errors="replace")
    assert "100.100.100.100" not in resolv_conf
    assert "tail" not in resolv_conf.lower()

    code, _ = container.exec_run(["getent", "hosts", "pypi.org"])
    assert code == 0


async def test_workspace_remains_writable_and_rootfs_allows_tool_install_shape(
    workspace_provider, assistant_id: str
):
    workspace = await workspace_provider.get_workspace(assistant_id)

    await workspace.project_emails([ProjectedFile(path="emails/t-1/0001.md", content=b"hi")])
    assert await workspace.environment.read_text("emails/t-1/0001.md") == "hi"

    result = await workspace.environment.exec("touch /usr/local/bin/email-agent-install-check")
    assert result.exit_code == 0


async def test_stale_bind_mount_container_is_recreated_with_named_volume(
    tmp_path, assistant_id: str
):
    import docker.errors

    import docker
    from email_agent.sandbox.docker_environment import (
        CONTAINER_NAME_PREFIX,
        WORKSPACE_ROOT,
        WORKSPACE_VOLUME_PREFIX,
        DockerWorkspaceProvider,
    )

    client = docker.from_env()
    name = f"{CONTAINER_NAME_PREFIX}{assistant_id}"
    volume_name = f"{WORKSPACE_VOLUME_PREFIX}{assistant_id}"
    host_workspace = tmp_path / "old-host-workspace"
    host_workspace.mkdir()
    (host_workspace / "notes").mkdir()
    (host_workspace / "notes" / "keep.md").write_text("preserve me\n")
    container = client.containers.run(
        image="email-agent-sandbox:slice4",
        name=name,
        detach=True,
        command=["sleep", "infinity"],
        volumes={str(host_workspace): {"bind": WORKSPACE_ROOT, "mode": "rw"}},
        working_dir=WORKSPACE_ROOT,
    )

    try:
        old_id = container.id
        provider = DockerWorkspaceProvider(
            client=client,
            image="email-agent-sandbox:slice4",
            sandbox_data_root=tmp_path / "sandboxes",
            memory_mb=512,
            cpu_cores=1.0,
            bash_timeout_seconds=10,
        )

        await provider.get_workspace(assistant_id)

        recreated = client.containers.get(name)
        mount = next(m for m in recreated.attrs["Mounts"] if m["Destination"] == WORKSPACE_ROOT)
        assert recreated.id != old_id
        assert mount["Type"] == "volume"
        assert mount["Name"] == volume_name

        code, output = recreated.exec_run(["cat", "/workspace/notes/keep.md"])
        assert code == 0
        assert output == b"preserve me\n"
    finally:
        with contextlib.suppress(docker.errors.NotFound):
            client.containers.get(name).remove(force=True)
        with contextlib.suppress(docker.errors.NotFound, docker.errors.APIError):
            client.volumes.get(volume_name).remove(force=True)
