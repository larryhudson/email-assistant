import httpx
from pydantic import SecretStr

from email_agent.github.port import GitHubRepository


class GitHubHttpAdapter:
    """GitHub adapter constrained to repositories owned by one username."""

    def __init__(
        self,
        *,
        username: str,
        token: SecretStr | None = None,
        api_base_url: str = "https://api.github.com",
        timeout_s: float = 30.0,
    ) -> None:
        self._username = username
        self._token = token
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout_s = timeout_s

    @property
    def username(self) -> str:
        return self._username

    async def list_owned_repositories(self) -> list[GitHubRepository]:
        if self._token is None:
            repos = await self._fetch_paginated(f"/users/{self._username}/repos?type=owner")
        else:
            repos = await self._fetch_paginated("/user/repos?affiliation=owner")

        owned = [
            _repository_from_json(item)
            for item in repos
            if item.get("owner", {}).get("login", "").lower() == self._username.lower()
        ]
        return sorted(owned, key=lambda repo: repo.name.lower())

    async def get_owned_repository(self, name: str) -> GitHubRepository | None:
        repos = await self.list_owned_repositories()
        target = name.lower()
        return next((repo for repo in repos if repo.name.lower() == target), None)

    async def _fetch_paginated(self, path: str) -> list[dict]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "email-agent",
        }
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"

        items: list[dict] = []
        url: str | None = f"{self._api_base_url}{path}"
        async with httpx.AsyncClient(timeout=self._timeout_s, headers=headers) as client:
            while url is not None:
                response = await client.get(url)
                response.raise_for_status()
                page = response.json()
                if not isinstance(page, list):
                    raise ValueError("GitHub repository response was not a list")
                items.extend(item for item in page if isinstance(item, dict))
                url = response.links.get("next", {}).get("url")
        return items


def _repository_from_json(item: dict) -> GitHubRepository:
    return GitHubRepository(
        name=str(item["name"]),
        full_name=str(item["full_name"]),
        clone_url=str(item["clone_url"]),
        private=bool(item.get("private", False)),
        description=item.get("description"),
    )


__all__ = ["GitHubHttpAdapter"]
