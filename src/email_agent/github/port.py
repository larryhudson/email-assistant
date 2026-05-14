from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class GitHubRepository:
    name: str
    full_name: str
    clone_url: str
    private: bool
    description: str | None = None


class GitHubPort(Protocol):
    @property
    def username(self) -> str: ...

    async def list_owned_repositories(self) -> list[GitHubRepository]: ...

    async def get_owned_repository(self, name: str) -> GitHubRepository | None: ...


__all__ = ["GitHubPort", "GitHubRepository"]
