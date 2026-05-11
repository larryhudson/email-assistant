from typing import Protocol

from email_agent.sandbox.inmemory_environment import InMemoryEnvironment
from email_agent.sandbox.workspace import AssistantWorkspace


class WorkspaceProvider(Protocol):
    async def get_workspace(self, assistant_id: str) -> AssistantWorkspace: ...


class StaticWorkspaceProvider:
    def __init__(self, workspace: AssistantWorkspace) -> None:
        self._workspace = workspace

    async def get_workspace(self, assistant_id: str) -> AssistantWorkspace:
        return self._workspace


class InMemoryWorkspaceProvider:
    def __init__(self) -> None:
        self._workspaces: dict[str, AssistantWorkspace] = {}

    async def get_workspace(self, assistant_id: str) -> AssistantWorkspace:
        workspace = self._workspaces.get(assistant_id)
        if workspace is None:
            workspace = AssistantWorkspace(InMemoryEnvironment())
            self._workspaces[assistant_id] = workspace
        return workspace


__all__ = ["InMemoryWorkspaceProvider", "StaticWorkspaceProvider", "WorkspaceProvider"]
