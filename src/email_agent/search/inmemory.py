from decimal import Decimal

from email_agent.search.port import SearchResponse, SearchResult


class InMemorySearchAdapter:
    def __init__(
        self,
        *,
        results: list[SearchResult] | None = None,
        cost_usd: Decimal = Decimal("0.0050"),
    ) -> None:
        self.results = results or []
        self.cost_usd = cost_usd
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        self.calls.append((query, max_results))
        return SearchResponse(
            query=query,
            results=self.results[:max_results],
            provider="brave",
            model="web-search",
            cost_usd=self.cost_usd,
        )


__all__ = ["InMemorySearchAdapter"]
