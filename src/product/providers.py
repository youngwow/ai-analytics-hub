"""Provider adapters used by the product contour, with frozen boundaries."""

from __future__ import annotations

import httpx

from ..config import TavilyConfig
from ..sources.scraper_llm import TavilySearch
from .contracts import SearchEvidence


class TavilyResearchSearch:
    """Search-only adapter. Tavily's generated answer is deliberately ignored."""

    def __init__(self, config: TavilyConfig, api_key: str):
        self.config = config
        self.searcher = TavilySearch(api_key, config)
        self.client = httpx.Client(timeout=30, follow_redirects=True)

    def search(self, query: str, *, mode: str) -> list[SearchEvidence]:
        depth = {"wide": "basic", "deep": "advanced", "mixed": "advanced"}.get(mode, "basic")
        # The depth is explicit per research plan without mutating shared config.
        config = TavilyConfig(
            api_key_env=self.config.api_key_env,
            max_results=self.config.max_results,
            search_depth=depth,
            days=self.config.days,
            country=self.config.country,
            language=self.config.language,
        )
        response = TavilySearch(self.searcher.api_key, config).search(
            self.client,
            query,
            max_results=config.max_results,
            topic="general",
            include_answer=False,
            include_raw_content="text",
            country=config.country or None,
            language=config.language or None,
        )
        return [
            SearchEvidence(
                url=row.url,
                title=row.title,
                # One search hit cannot monopolise the later reasoning prompt.
                # The full Tavily response remains available in collection
                # artifacts; research receives a bounded evidence excerpt.
                snippet=(row.raw_content or row.snippet)[:6000],
                published_at=row.published,
            )
            for row in response.results
        ]

    def close(self) -> None:
        self.client.close()
