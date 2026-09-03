"""Saved Tavily queries as sources (kind `search`).

A query lives in `Source.fetch_url` as
`tavily://search?q=…&domains=a,b&days=7&topic=news&summary=1`, so `collect`
polls it like a feed: every run asks Tavily for the last `days` (or for what
appeared since the previous run), each hit becomes a document, and — when the
query asks for one — Tavily's answer becomes a digest document «Сводка: …»,
one per query per day.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from ..common import get_logger, parse_datetime, to_utc_iso
from ..config import ScraperConfig, TavilyConfig
from ..models import FetchResult, FetchState, RawDocument, Source
from .base import HostLimiter
from .fulltext import tidy
from .scraper_llm import TAVILY_SEARCH_URL, Candidate, TavilyError, TavilySearch, clean_page_text
from .textutil import clean_url

log = get_logger("search")

SCHEME = "tavily"
TOPICS = ("news", "general")
DEFAULT_DAYS = 7
# Below this the page dump had no article in it (paywall, JS-only page): Tavily's
# relevance chunks become the text and trafilatura still gets a go at the page.
MIN_PAGE_TEXT = 400
SEARCH_MAX_RESULTS = 20  # API cap; relevance-ranked, so a wide window crowds out new items
SUMMARY_PREFIX = "summary:"


def is_search_url(url: str) -> bool:
    return urlsplit((url or "").strip()).scheme.lower() == SCHEME


@dataclass
class SearchQuery:
    """What one `search` source asks Tavily. Canonical, so equal queries share a fetch_url."""

    query: str
    domains: list[str] = field(default_factory=list)
    days: int = DEFAULT_DAYS
    topic: str = "news"
    summary: bool = True

    def __post_init__(self) -> None:
        self.query = " ".join((self.query or "").split())
        if not self.query:
            raise ValueError("search query is empty")
        self.domains = sorted(
            {d.strip().lower().removeprefix("www.") for d in self.domains if d and d.strip()}
        )
        try:
            self.days = int(self.days)
        except (TypeError, ValueError) as e:
            raise ValueError(f"days must be a number, got {self.days!r}") from e
        if self.days < 1:
            raise ValueError("days must be >= 1")
        if self.topic not in TOPICS:
            raise ValueError(f"topic must be one of {TOPICS}, got {self.topic!r}")

    def to_url(self) -> str:
        params = [
            ("q", self.query),
            ("domains", ",".join(self.domains)),
            ("days", str(self.days)),
            ("topic", self.topic),
            ("summary", "1" if self.summary else "0"),
        ]
        return f"{SCHEME}://search?{urlencode(params)}"

    @classmethod
    def from_url(cls, url: str) -> "SearchQuery":
        parts = urlsplit((url or "").strip())
        if parts.scheme.lower() != SCHEME:
            raise ValueError(f"not a {SCHEME}:// url: {url}")
        qs = parse_qs(parts.query, keep_blank_values=True)

        def first(key: str, default: str = "") -> str:
            values = qs.get(key)
            return values[0] if values else default

        return cls(
            query=first("q"),
            domains=first("domains").split(","),
            days=first("days", str(DEFAULT_DAYS)) or DEFAULT_DAYS,
            topic=first("topic", "news") or "news",
            summary=first("summary", "1").strip().lower() not in ("0", "false", "no", "off"),
        )

    def describe(self) -> str:
        """Short human line for notes and logs: «news, 7 дн., домены: a.ru, b.ru»."""
        parts = [self.topic, f"{self.days} дн."]
        if self.domains:
            parts.append("домены: " + ", ".join(self.domains))
        if not self.summary:
            parts.append("без сводки")
        return ", ".join(parts)


def candidate_to_document(
    c: Candidate, source_id: int, now: datetime, max_chars: int
) -> RawDocument:
    """One hit → RawDocument: the page dump's prose as text, Tavily's chunks as summary."""
    url = clean_url(c.url) or c.url
    page_text = clean_page_text(c.raw_content)
    thin = len(page_text) < MIN_PAGE_TEXT
    text = tidy(c.snippet) if thin else page_text
    return RawDocument(
        source_id=source_id,
        external_id=url,
        url=url,
        title=" ".join(c.title.split()),
        summary=tidy(c.snippet),
        text=text[:max_chars],
        published_at=to_utc_iso(parse_datetime(c.published)),
        fetched_at=to_utc_iso(now) or "",
        needs_fulltext=thin,  # trafilatura gets a go at the page; the chunks stay if it fails
    )


def summary_document(query: SearchQuery, answer: str, source_id: int, now: datetime) -> RawDocument:
    """Tavily's answer as a digest document: one per query per UTC day, no origin URL."""
    stamp = to_utc_iso(now) or ""
    return RawDocument(
        source_id=source_id,
        external_id=f"{SUMMARY_PREFIX}{stamp[:10]}",
        url="",
        title=f"Сводка: {query.query}"[:200],
        summary=answer,
        text=answer,
        author="Tavily",
        published_at=stamp,
        fetched_at=stamp,
    )


class SearchAdapter:
    kind = "search"

    def __init__(
        self,
        tavily: TavilyConfig,
        scraper: ScraperConfig,
        limiter: HostLimiter | None = None,
        api_key: str | None = None,
    ):
        self.tavily = tavily
        self.scraper = scraper
        self.limiter = limiter
        self.api_key = api_key or ""

    @staticmethod
    def _cursor_date(state: FetchState) -> datetime | None:
        """The UTC day of the previous successful run, kept in `cursor.since`."""
        raw = state.cursor.get("since")
        if not isinstance(raw, str):
            return None
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def fetch(
        self,
        source: Source,
        state: FetchState,
        client: httpx.Client,
        *,
        now: datetime,
        since: datetime | None = None,
        backfill: bool = False,
    ) -> FetchResult:
        try:
            query = SearchQuery.from_url(source.fetch_url)
        except ValueError as e:
            return FetchResult(error=f"bad search url: {e}")
        if not self.api_key:
            return FetchResult(
                error=f"no Tavily API key: set {self.tavily.api_key_env} in the environment or .env"
            )

        # Exactly one recency bound per request. First run / backfill: the full
        # window. Later runs: since the previous run, overlapping by a day (Tavily
        # dates are UTC days, our timestamps may be Moscow evenings), never wider
        # than the window. `--force` clears the cursor, so it re-asks the full window.
        window_start = now - timedelta(days=query.days)
        last = None if backfill else self._cursor_date(state)
        bound: dict = {}
        if last is not None:
            bound["start_date"] = max(window_start, last - timedelta(days=1)).date().isoformat()
        elif query.topic == "news":
            bound["days"] = query.days
        else:
            bound["start_date"] = window_start.date().isoformat()

        try:
            searcher = TavilySearch(self.api_key, self.tavily)
            with self.limiter.slot(TAVILY_SEARCH_URL) if self.limiter else nullcontext():
                response = searcher.search(
                    client,
                    query.query,
                    max_results=SEARCH_MAX_RESULTS,
                    include_domains=query.domains or None,
                    topic=query.topic,
                    include_answer="advanced" if query.summary else False,
                    include_raw_content="text",
                    country=self.tavily.country or None,
                    language=self.tavily.language or None,
                    **bound,
                )
        except TavilyError as e:
            return FetchResult(error=str(e))

        source_id = source.id or 0
        docs = [
            candidate_to_document(c, source_id, now, self.scraper.fulltext_max_chars)
            for c in response.results
            if c.url
        ]
        if query.summary and response.answer:
            docs.append(summary_document(query, response.answer, source_id, now))
        log.info(
            "%s: %d hits%s (%s)",
            source.name,
            len(response.results),
            " + сводка" if response.answer else "",
            ", ".join(f"{k}={v}" for k, v in bound.items()),
        )
        return FetchResult(
            documents=docs,
            state_update={"cursor": {"since": (to_utc_iso(now) or "")[:10], "days": query.days}},
        )
