from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    age: str | None = None


@dataclass(frozen=True)
class SearchResponse:
    query: str
    results: list[SearchResult]
    provider: str
    model: str
    cost_usd: Decimal


class SearchPort(Protocol):
    async def search(self, query: str, *, max_results: int = 5) -> SearchResponse: ...


__all__ = ["SearchPort", "SearchResponse", "SearchResult"]
