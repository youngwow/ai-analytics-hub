"""URL resolver (scraper.md §0): decide how a user-entered URL should be polled.

t.me/<channel>                     → telegram
tavily://search?q=…                → search (a saved Tavily query, scraper_search.py)
other non-http scheme              → manual (nothing to fetch)
feed-looking URL that parses       → rss
page advertising a feed / probes   → rss
robots.txt Sitemap: or sitemap.xml → sitemap (only if it carries lastmod)
anything else                      → html
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

import feedparser
import httpx
from selectolax.parser import HTMLParser
from trafilatura.feeds import FeedParameters, determine_feed
from trafilatura.sitemaps import is_plausible_sitemap

from ..models import Resolution
from ..utils import get_logger
from .base import HostLimiter, Page, fetch
from .scraper_rss import is_feed
from .scraper_search import SCHEME as SEARCH_SCHEME
from .scraper_search import SearchQuery
from .scraper_sitemap import has_lastmod, parse_sitemap
from .textutil import looks_like_html

log = get_logger("resolver")

_TG_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?P<prefix>s/)?"
    r"(?P<name>[A-Za-z0-9_]{4,32})(?:/\d+)?/?(?:[?#].*)?$"
)
_TG_REJECT = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:\+|joinchat/|c/)")
_FEEDISH_PATH = re.compile(r"(?:\.(?:xml|rss|atom)|/rss|/feed|/atom)/?$", re.IGNORECASE)
# Probed in this order after whatever the page itself advertises: the generic
# paths first (WordPress/Drupal/Bitrix conventions), then Russian news-section
# variants. The `.xml` twins earn their place: Kommersant answers 403 on
# /rss/news and serves the feed at /rss/news.xml.
_LOWER_PROBE_PATHS = (
    "/rss",
    "/rss/",
    "/feed",
    "/feed/",
    "/rss.xml",
    "/rss/news",
    "/rss/news/",
    "/rss/news.xml",
    "/news/rss",
    "/news/rss/",
    "/news/rss.xml",
    "/rss/all",
)


def _upper_rss(path: str) -> str:
    """`/rss/news.xml` → `/RSS/news.xml`; paths without an `rss` segment stay as they are."""
    return re.sub(r"(?<![a-z])rss(?![a-z])", "RSS", path)


# Most servers ignore case in paths, but nginx and Apache on Linux do not, and a
# feed published as /RSS/news.xml is then invisible to the lowercase probes. Each
# path gets an uppercase twin, tried only once every lowercase one has failed —
# so a site with a feed never pays for them.
_PROBE_PATHS = tuple(
    dict.fromkeys(_LOWER_PROBE_PATHS + tuple(_upper_rss(p) for p in _LOWER_PROBE_PATHS))
)
_MAX_PROBES = len(_PROBE_PATHS)
_SITEMAP_RE = re.compile(r"^\s*sitemap:\s*(\S+)", re.IGNORECASE | re.MULTILINE)
PROBE_TIMEOUT = 8.0  # guessed paths on a slow site must not stall `sources add` for minutes


class Resolver:
    def __init__(
        self,
        client: httpx.Client,
        limiter: HostLimiter | None = None,
        probe_timeout: float = PROBE_TIMEOUT,
    ):
        self.client = client
        self.limiter = limiter
        self.probe_timeout = probe_timeout

    # ── helpers ────────────────────────────────────────────────────────────

    def _get(self, url: str) -> Page:
        return fetch(self.client, url, limiter=self.limiter)

    def _probe(self, url: str) -> Page:
        """Fetch a guessed URL (feed probe, robots, sitemap) with a short timeout."""
        return fetch(self.client, url, limiter=self.limiter, timeout=self.probe_timeout)

    @staticmethod
    def _normalise_input(url: str) -> str:
        url = url.strip()
        if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.IGNORECASE):
            url = "https://" + url
        return url

    @staticmethod
    def _page_title(body: bytes) -> str:
        node = HTMLParser(body).css_first("title")
        return " ".join(node.text(strip=True).split())[:80] if node is not None else ""

    def _as_feed(self, page: Page) -> Resolution | None:
        if not page.ok or looks_like_html(page.body):
            return None
        parsed = feedparser.parse(page.body)
        if not is_feed(parsed) or not parsed.entries:
            return None
        title = " ".join((parsed.feed.get("title") or "").split())[:80]
        return Resolution(kind="rss", fetch_url=page.url, name=title)

    def _feed_candidates(self, page: Page) -> list[str]:
        base = urlunsplit(urlsplit(page.url)._replace(path="", query="", fragment=""))
        domain = (urlsplit(page.url).hostname or "").removeprefix("www.")
        html = page.body.decode("utf-8", errors="replace")
        found: list[str] = []
        try:
            found = list(determine_feed(html, FeedParameters(base, domain, page.url)))
        except Exception as e:  # noqa: BLE001 — discovery is best-effort
            log.debug("determine_feed failed for %s: %s", page.url, e)
        # Advertised feeds first, section-wide ("news"/"all") before comments/podcast
        # feeds; then the probe list in its own order.
        found.sort(key=lambda u: 0 if re.search(r"news|all|novosti", u, re.I) else 1)
        probes = [base + p for p in _PROBE_PATHS][:_MAX_PROBES]
        return list(dict.fromkeys(found + probes))

    def _sitemap_candidates(self, page: Page) -> list[str]:
        base = urlunsplit(urlsplit(page.url)._replace(path="", query="", fragment=""))
        out: list[str] = []
        robots = self._probe(base + "/robots.txt")
        if robots.ok and not looks_like_html(robots.body):
            text = robots.body.decode("utf-8", errors="replace")
            out.extend(m.group(1).strip() for m in _SITEMAP_RE.finditer(text))
        out.append(base + "/sitemap.xml")
        return list(dict.fromkeys(out))[:4]

    # ── public ─────────────────────────────────────────────────────────────

    def resolve(self, url: str) -> Resolution:
        url = self._normalise_input(url)

        scheme = urlsplit(url).scheme.lower()
        if scheme == SEARCH_SCHEME:
            try:
                query = SearchQuery.from_url(url)
            except ValueError as e:
                return Resolution(kind="manual", fetch_url=url, note=f"bad search url: {e}")
            # Canonical form, so the same query never lands under two fetch_urls.
            return Resolution(kind="search", fetch_url=query.to_url(), name=query.query[:80])
        if scheme not in ("http", "https"):
            return Resolution(
                kind="manual", fetch_url=url, note=f"{scheme}:// is not fetched; kept as manual"
            )

        if _TG_REJECT.match(url):
            return Resolution(
                kind="manual",
                fetch_url=url,
                note="private/invite Telegram links have no web preview",
            )
        m = _TG_RE.match(url)
        if m:
            name = m.group("name")
            return Resolution(kind="telegram", fetch_url=f"https://t.me/s/{name}", name=name)

        page: Page | None = None
        if _FEEDISH_PATH.search(urlsplit(url).path):
            page = self._get(url)
            feed = self._as_feed(page)
            if feed:
                return feed

        if page is None:
            page = self._get(url)
        if not page.ok:
            reason = page.error or f"HTTP {page.status}"
            return Resolution(kind="html", fetch_url=url, note=f"unreachable now: {reason}")
        feed = self._as_feed(page)
        if feed:
            return feed

        title = self._page_title(page.body)
        for candidate in self._feed_candidates(page):
            found = self._as_feed(self._probe(candidate))
            if found:
                found.name = found.name or title
                found.note = f"feed discovered at {found.fetch_url}"
                return found

        for candidate in self._sitemap_candidates(page):
            sm = self._probe(candidate)
            if not sm.ok:
                continue
            text = sm.body.decode("utf-8", errors="replace")
            if not is_plausible_sitemap(sm.url, text):
                continue
            kind, entries = parse_sitemap(sm.body)
            if not kind:
                continue
            if kind == "index" or has_lastmod(entries):
                return Resolution(
                    kind="sitemap",
                    fetch_url=sm.url,
                    name=title,
                    note="no feed; polling sitemap by lastmod",
                )
            log.info("%s: sitemap has no lastmod, falling back to html", sm.url)
            break

        return Resolution(
            kind="html",
            fetch_url=page.url,
            name=title,
            note="no feed or dated sitemap; diffing page links",
        )
