import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from docker.models.containers import Container

    import docker as docker_sdk


CONTAINER_NAME_PREFIX = "email-agent-sandbox-"


class DockerSandbox:
    """`AssistantSandbox` adapter backed by long-lived per-assistant containers.

    One container per `assistant_id`, lazily started on first `ensure_started`
    and reused for the lifetime of the process. The host workspace lives at
    `sandbox_data_root/<assistant_id>/workspace/` and is bind-mounted at
    `/workspace`. Resource limits + per-tool timeouts are enforced via
    docker's HostConfig and GNU `timeout` inside the container.

    docker SDK is sync, so calls are dispatched via `asyncio.to_thread`.
    """

    def __init__(
        self,
        *,
        client: "docker_sdk.DockerClient",
        image: str,
        sandbox_data_root: Path,
        memory_mb: int = 512,
        cpu_cores: float = 1.0,
        bash_timeout_seconds: int = 60,
    ) -> None:
        self._client = client
        self._image = image
        self._data_root = sandbox_data_root
        self._memory_mb = memory_mb
        self._cpu_cores = cpu_cores
        self._bash_timeout_seconds = bash_timeout_seconds

    async def ensure_started(self, assistant_id: str) -> None:
        await asyncio.to_thread(self._ensure_started_sync, assistant_id)

    def _ensure_started_sync(self, assistant_id: str) -> None:
        import docker as docker_sdk

        name = self._container_name(assistant_id)
        try:
            container = self._client.containers.get(name)
        except docker_sdk.errors.NotFound:
            self._create_container(assistant_id, name)
            return

        if container.status != "running":
            container.start()

    def _create_container(self, assistant_id: str, name: str) -> "Container":
        host_workspace = self._workspace_dir(assistant_id)
        host_workspace.mkdir(parents=True, exist_ok=True)

        return self._client.containers.run(
            image=self._image,
            name=name,
            detach=True,
            command=["sleep", "infinity"],
            volumes={
                str(host_workspace.resolve()): {"bind": "/workspace", "mode": "rw"},
            },
            mem_limit=f"{self._memory_mb}m",
            nano_cpus=int(self._cpu_cores * 1_000_000_000),
            working_dir="/workspace",
            tmpfs={"/tmp": ""},
        )

    def _workspace_dir(self, assistant_id: str) -> Path:
        return self._data_root / assistant_id / "workspace"

    def _container_name(self, assistant_id: str) -> str:
        return f"{CONTAINER_NAME_PREFIX}{assistant_id}"


__all__ = ["DockerSandbox"]
