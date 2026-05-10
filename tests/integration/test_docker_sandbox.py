import pytest

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


def test_docker_daemon_is_reachable():
    import docker

    client = docker.from_env()
    info = client.ping()
    assert info is True
