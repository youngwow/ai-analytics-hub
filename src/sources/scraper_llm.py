"""Tavily HTTP wrapper (scraper.md §4).

Two callers: `discover` (find candidate sources for a query) and the `search`
adapter (poll a saved query, see scraper_search.py). Western search APIs index
gov.ru and niche Russian domains poorly, so search supplements RSS / Telegram /
sitemap sources — it does not replace them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx

from ..common import get_logger
from ..config import TavilyConfig

log = get_logger("tavily")

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
REQUEST_TIMEOUT = 30.0  # longer than the collect client's default: answers take a while
MAX_RESULTS = 20  # API cap for max_results

# A Tavily page dump is the whole page — logo rows, menus, phone numbers, footer.
# Lines that are markup leftovers or bare numbers never carry article text.
_JUNK_LINE_RE = re.compile(r"^(?:image\s*\d+\b|!?\[|\*\s|[\d\s\-()+]{7,}$)", re.IGNORECASE)
_SENTENCE_END = (".", "!", "?", "»", "…", ":", '"')


def clean_page_text(raw: str, min_words: int = 8) -> str:
    """Keep the prose out of a Tavily page dump.

    Menu items and captions are short and unpunctuated; article paragraphs are
    neither. Repeated lines (a headline echoed in three widgets) are kept once.
    """
    seen: set[str] = set()
    kept: list[str] = []
    for line in (raw or "").splitlines():
        line = " ".join(line.split())
        if not line or _JUNK_LINE_RE.match(line):
            continue
        if len(line.split()) < min_words or not line.endswith(_SENTENCE_END):
            continue
        if line in seen:
            continue
        seen.add(line)
        kept.append(line)
    return "\n\n".join(kept)


class TavilyError(Exception):
    """Search failed (network, auth, quota, malformed response)."""


@dataclass
class Candidate:
    url: str
    title: str = ""
    snippet: str = ""
    score: float = 0.0
    published: str | None = None  # only the news topic dates its hits
    raw_content: str = ""  # page text, only when the caller asked for it


@dataclass
class SearchResponse:
    """One /search reply: ranked hits plus Tavily's own answer when it was asked for."""

    results: list[Candidate] = field(default_factory=list)
    answer: str = ""


class TavilySearch:
    def __init__(self, api_key: str, config: TavilyConfig):
        if not api_key:
            raise TavilyError(
                f"no Tavily API key: set {config.api_key_env} in the environment or .env"
            )
        self.api_key = api_key
        self.config = config

    def search(
        self,
        client: httpx.Client,
        query: str,
        *,
        max_results: int | None = None,
        include_domains: list[str] | None = None,
        topic: str = "general",
        days: int | None = None,
        time_range: str | None = None,
        start_date: str | None = None,
        include_answer: bool | str = False,
        include_raw_content: bool | str = False,
        country: str | None = None,
        language: str | None = None,
    ) -> SearchResponse:
        """POST /search through `client` (the shared collect client during a run).

        Recency: pass at most one of `start_date` (YYYY-MM-DD, any topic), `days`
        (news topic only) or `time_range` (day|week|month|year); when several are
        given they win in that order, so the request never carries two bounds.
        `include_answer` is False / "basic" / "advanced"; `include_raw_content`
        False / "text" / "markdown". `country` is only honoured by the general topic;
        `language` (ISO 639-1) boosts hits in that language and, in practice, makes
        the answer come back in it.
        """
        payload: dict = {
            "query": query,
            "search_depth": self.config.search_depth,
            "max_results": min(max_results or self.config.max_results, MAX_RESULTS),
            "topic": topic,
            "include_domains": include_domains or [],
            "include_answer": include_answer,
            "include_raw_content": include_raw_content,
        }
        if start_date:
            payload["start_date"] = start_date
        elif days is not None and topic == "news":
            payload["days"] = days
        elif time_range:
            payload["time_range"] = time_range
        if country and topic == "general":
            payload["country"] = country
        if language:
            payload["language"] = language
        try:
            resp = client.post(
                TAVILY_SEARCH_URL,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=REQUEST_TIMEOUT,
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
                raw_content=(r.get("raw_content") or "").strip(),
            )
            for r in results
            if isinstance(r, dict) and r.get("url")
        ]
        answer = data.get("answer")
        answer = answer.strip() if isinstance(answer, str) else ""
        log.info(
            "tavily: %d results%s for %r", len(candidates), " + answer" if answer else "", query
        )
        return SearchResponse(results=candidates, answer=answer)
