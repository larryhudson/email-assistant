from functools import cache

import pytest


@cache
def _docker_reachable() -> bool:
    try:
        import docker

        client = docker.from_env()
        client.ping()
    except Exception:
        return False
    return True


def pytest_collection_modifyitems(config, items) -> None:
    skip = pytest.mark.skip(reason="docker daemon not reachable")
    for item in items:
        if "requires_docker" in item.keywords and not _docker_reachable():
            item.add_marker(skip)
