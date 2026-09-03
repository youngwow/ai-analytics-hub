"""Adapter interface, the shared HTTP fetch helper and per-host throttling.

Every adapter turns one `Source` poll into a `FetchResult`; it does HTTP and
parsing only — persistence is the collector's job on the main thread.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from ..common import get_logger
from ..config import Config
from ..models import FetchResult, FetchState, Source

log = get_logger("fetch")


@dataclass
class Page:
    """One fetched HTTP response, or the reason it could not be fetched."""

    url: str  # final URL after redirects
    status: int = 0
    body: bytes = b""
    headers: httpx.Headers = field(default_factory=httpx.Headers)
    redirected_from: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300


class HostLimiter:
    """Caps concurrent in-flight requests per host.

    gov.ru sites sit behind Qrator/Ngenix and ban bursts, and a first run
    against a sitemap can otherwise open dozens of connections to one host.
    """

    def __init__(self, per_host: int):
        self.per_host = max(1, per_host)
        self._lock = threading.Lock()
        self._sems: dict[str, threading.Semaphore] = {}

    def _sem_for(self, url: str) -> threading.Semaphore:
        host = (urlsplit(url).hostname or "").lower()
        with self._lock:
            sem = self._sems.get(host)
            if sem is None:
                sem = self._sems[host] = threading.Semaphore(self.per_host)
            return sem

    @contextmanager
    def slot(self, url: str):
        sem = self._sem_for(url)
        sem.acquire()
        try:
            yield
        finally:
            sem.release()


def fetch(
    client: httpx.Client,
    url: str,
    *,
    limiter: HostLimiter | None = None,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
) -> Page:
    """GET `url` following redirects; never raises, errors land in `Page.error`."""
    kwargs: dict = {"follow_redirects": True}
    if headers:
        kwargs["headers"] = headers
    if timeout is not None:
        kwargs["timeout"] = timeout
    try:
        with limiter.slot(url) if limiter else nullcontext():
            resp = client.get(url, **kwargs)
    except httpx.TimeoutException:
        return Page(url=url, error="timeout")
    except httpx.TooManyRedirects:
        return Page(url=url, error="too many redirects")
    except httpx.RequestError as e:
        return Page(url=url, error=f"request error: {e.__class__.__name__}: {e}")
    return Page(
        url=str(resp.url),
        status=resp.status_code,
        body=resp.content,
        headers=resp.headers,
        redirected_from=[str(r.url) for r in resp.history],
    )


def make_client(config: Config, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """The one shared client for a collect run (thread-safe, connection-pooled)."""
    kwargs: dict = {
        "headers": config.scraper.headers,
        "timeout": config.scraper.request_timeout,
        "max_redirects": config.scraper.max_redirects,
        "follow_redirects": True,
    }
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.Client(**kwargs)


class Adapter(Protocol):
    """One polling strategy (rss, telegram, sitemap, html, search)."""

    kind: str

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
        """Poll `source`. `since` bounds expensive first-run crawls; `backfill` walks history."""
        ...


def build_adapters(
    config: Config, limiter: HostLimiter, tavily_key: str | None = None
) -> dict[str, Adapter]:
    """Registry keyed by `Source.kind`; `manual` sources have no adapter.

    `tavily_key` may be empty — `search` sources then fail per run with a clear
    error instead of taking the whole collect down.
    """
    from .scraper_html import HtmlAdapter
    from .scraper_rss import RssAdapter
    from .scraper_search import SearchAdapter
    from .scraper_sitemap import SitemapAdapter
    from .scraper_tg import TelegramAdapter

    adapters: list[Adapter] = [
        RssAdapter(config.scraper, limiter),
        TelegramAdapter(config.scraper, config.telegram, limiter),
        SitemapAdapter(config.scraper, config.sitemap, limiter),
        HtmlAdapter(config.scraper, limiter),
        SearchAdapter(config.tavily, config.scraper, limiter, api_key=tavily_key),
    ]
    return {a.kind: a for a in adapters}
