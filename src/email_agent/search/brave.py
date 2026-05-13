from decimal import Decimal

import httpx

from email_agent.search.port import SearchResponse, SearchResult

BRAVE_SEARCH_COST_USD_PER_REQUEST = Decimal("0.0050")


class BraveSearchAdapter:
    """Brave Web Search adapter behind the provider-neutral SearchPort."""

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = "https://api.search.brave.com/res/v1/web/search",
        timeout_s: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint
        self._timeout_s = timeout_s

    async def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        count = min(max(1, max_results), 10)
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self._api_key,
        }
        params = {"q": query, "count": count}
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            response = await client.get(self._endpoint, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()

        raw_results = data.get("web", {}).get("results", [])
        results = [
            SearchResult(
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                snippet=str(item.get("description") or item.get("snippet") or ""),
                age=item.get("age"),
            )
            for item in raw_results
            if item.get("url")
        ]
        return SearchResponse(
            query=query,
            results=results[:count],
            provider="brave",
            model="web-search",
            cost_usd=BRAVE_SEARCH_COST_USD_PER_REQUEST,
        )


__all__ = ["BRAVE_SEARCH_COST_USD_PER_REQUEST", "BraveSearchAdapter"]
