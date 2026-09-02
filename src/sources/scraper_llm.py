"""LLM-search layer (scraper.md §4): Tavily as a *discovery* tool, not a collector.

Regular collection is the cron over known sources; search-by-query finds what
is not in the subscriptions yet (new sources, coverage of a story). Western
search APIs index gov.ru and niche Russian domains poorly, so this supplements
RSS / Telegram / sitemap sources — it does not replace them.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..common import get_logger
from ..config import TavilyConfig

log = get_logger("tavily")

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
REQUEST_TIMEOUT = 30.0


class TavilyError(Exception):
    """Search failed (network, auth, quota, malformed response)."""


@dataclass
class Candidate:
    url: str
    title: str = ""
    snippet: str = ""
    score: float = 0.0
    published: str | None = None


class TavilySearch:
    def __init__(
        self, api_key: str, config: TavilyConfig, transport: httpx.BaseTransport | None = None
    ):
        if not api_key:
            raise TavilyError(
                f"no Tavily API key: set {config.api_key_env} in the environment or .env"
            )
        self.api_key = api_key
        self.config = config
        self.transport = transport

    def search(
        self,
        query: str,
        *,
        max_results: int | None = None,
        include_domains: list[str] | None = None,
        topic: str = "general",
        time_range: str | None = None,
    ) -> list[Candidate]:
        payload: dict = {
            "query": query,
            "search_depth": self.config.search_depth,
            "max_results": max_results or self.config.max_results,
            "topic": topic,
            "include_domains": include_domains or [],
            "include_answer": False,
            "include_raw_content": False,
        }
        if time_range:
            payload["time_range"] = time_range
        kwargs: dict = {"timeout": REQUEST_TIMEOUT}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        try:
            with httpx.Client(**kwargs) as client:
                resp = client.post(
                    TAVILY_SEARCH_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.RequestError as e:
            raise TavilyError(f"Tavily request failed: {e.__class__.__name__}: {e}") from e
        if resp.status_code != 200:
            raise TavilyError(f"Tavily HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as e:
            raise TavilyError("Tavily returned non-JSON") from e
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise TavilyError("Tavily response has no results list")
        candidates = [
            Candidate(
                url=r.get("url", ""),
                title=(r.get("title") or "").strip(),
                snippet=(r.get("content") or "").strip(),
                score=float(r.get("score") or 0.0),
                published=r.get("published_date"),
            )
            for r in results
            if isinstance(r, dict) and r.get("url")
        ]
        log.info("tavily: %d results for %r", len(candidates), query)
        return candidates
