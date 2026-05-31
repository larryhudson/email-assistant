from pathlib import Path
from typing import Any, cast

from email_agent.sandbox.docker_environment import DockerWorkspaceProvider


class _FakeContainers:
    def __init__(self) -> None:
        self.run_kwargs = {}

    def run(self, **kwargs):
        self.run_kwargs = kwargs
        return object()


class _FakeApi:
    def __init__(self) -> None:
        self.endpoint_config = None
        self.networking_config = None

    def create_endpoint_config(self, *, aliases: list[str]):
        self.endpoint_config = {"aliases": aliases}
        return self.endpoint_config

    def create_networking_config(self, config):
        self.networking_config = config
        return config


class _FakeVolumes:
    def get(self, name: str):
        return object()


class _FakeNetwork:
    def __init__(self) -> None:
        self.connected = []
        self.disconnected = []

    def connect(self, container, *, aliases: list[str]) -> None:
        self.connected.append((container, aliases))

    def disconnect(self, container) -> None:
        self.disconnected.append(container)


class _FakeNetworks:
    def __init__(self, network: _FakeNetwork) -> None:
        self.network = network
        self.requested = []

    def get(self, name: str) -> _FakeNetwork:
        self.requested.append(name)
        return self.network


class _FakeClient:
    def __init__(self) -> None:
        self.api = _FakeApi()
        self.containers = _FakeContainers()
        self.volumes = _FakeVolumes()
        self.network = _FakeNetwork()
        self.networks = _FakeNetworks(self.network)


class _FakeContainer:
    def __init__(self, aliases: list[str] | None = None) -> None:
        self.attrs = {
            "NetworkSettings": {
                "Networks": {
                    "email-agent": {
                        "Aliases": aliases or [],
                    }
                }
            }
        }
        self.reloads = 0

    def reload(self) -> None:
        self.reloads += 1


def test_docker_workspace_provider_sets_stable_network_alias_on_create() -> None:
    client = _FakeClient()
    provider = DockerWorkspaceProvider(
        client=cast(Any, client),
        image="sandbox:test",
        sandbox_data_root=Path("unused"),
        docker_network="email-agent",
    )

    provider._create_container("a-1", "email-agent-sandbox-a-1")

    assert client.containers.run_kwargs["network"] == "email-agent"
    assert client.api.endpoint_config == {"aliases": ["email-agent-sandbox-a-1"]}
    assert client.containers.run_kwargs["networking_config"] == {
        "email-agent": {"aliases": ["email-agent-sandbox-a-1"]}
    }
    assert client.containers.run_kwargs["ports"] == {"8000/tcp": ("127.0.0.1", None)}


def test_docker_workspace_provider_repairs_missing_network_alias() -> None:
    client = _FakeClient()
    provider = DockerWorkspaceProvider(
        client=cast(Any, client),
        image="sandbox:test",
        sandbox_data_root=Path("unused"),
        docker_network="email-agent",
    )
    container = _FakeContainer(aliases=["not-the-assistant"])

    provider._ensure_network_alias(cast(Any, container), "a-1")

    assert client.networks.requested == ["email-agent"]
    assert client.network.disconnected == [container]
    assert client.network.connected == [(container, ["email-agent-sandbox-a-1"])]
