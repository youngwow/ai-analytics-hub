"""Generic list-page adapter — the resolver's last resort (scraper.md §0 step 5).

The page's article-looking links are returned as candidate documents; the
collector diffs them against `seen_urls`, and only links it has never seen go
through full-text extraction. First run seeds the seen set and keeps only pages
whose extracted date falls inside the window.
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit

import httpx
from courlan import is_navigation_page, is_not_crawlable
from selectolax.parser import HTMLParser

from ..common import get_logger, to_utc_iso
from ..config import ScraperConfig
from ..models import FetchResult, FetchState, RawDocument, Source
from .base import HostLimiter, fetch
from .textutil import clean_url, looks_like_html

log = get_logger("html")

MAX_CANDIDATES = 300
MIN_ANCHOR_CHARS = 15  # headline-length anchor text
_CHROME_TAGS = ("nav", "header", "footer", "aside", "script", "style", "form", "noscript")
_SKIP_EXT = re.compile(r"\.(?:jpe?g|png|gif|svg|webp|css|js|ico|mp[34]|zip|rar|xlsx?|docx?)$", re.I)
_SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "#")
_ID_RE = re.compile(r"\d{4,}")  # numeric ids / dates in the path
_SLUG_RE = re.compile(r"[a-z0-9]+-[a-z0-9-]+", re.I)


def _same_host(url: str, reference: str) -> bool:
    a = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    b = (urlsplit(reference).hostname or "").lower().removeprefix("www.")
    return bool(a) and a == b


def _plausible_article(url: str, anchor_text: str) -> bool:
    path = urlsplit(url).path.rstrip("/")
    if not path or _SKIP_EXT.search(path):
        return False
    if is_not_crawlable(url) or is_navigation_page(url):
        return False
    depth = path.count("/")
    text_ok = len(anchor_text) >= MIN_ANCHOR_CHARS
    id_ok = bool(_ID_RE.search(path)) or bool(_SLUG_RE.search(path.rsplit("/", 1)[-1]))
    return (text_ok and depth >= 1) or (id_ok and len(anchor_text) >= 5)


def extract_article_links(body: bytes, base_url: str) -> list[str]:
    """Same-host, article-looking links in page order (chrome sections removed)."""
    tree = HTMLParser(body)
    for tag in _CHROME_TAGS:
        for node in tree.css(tag):
            node.decompose()
    out: list[str] = []
    seen: set[str] = set()
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(_SKIP_SCHEMES):
            continue
        url = clean_url(urljoin(base_url, href))
        if not url.startswith("http") or not _same_host(url, base_url) or url in seen:
            continue
        if url.rstrip("/") == base_url.rstrip("/"):
            continue
        anchor = " ".join(a.text(separator=" ", strip=True).split())
        if not _plausible_article(url, anchor):
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= MAX_CANDIDATES:
            break
    return out


class HtmlAdapter:
    kind = "html"

    def __init__(self, config: ScraperConfig, limiter: HostLimiter | None = None):
        self.config = config
        self.limiter = limiter

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
        page = fetch(client, source.fetch_url, limiter=self.limiter)
        if page.error:
            return FetchResult(error=page.error)
        if page.status >= 400:
            return FetchResult(error=f"HTTP {page.status}")
        if not looks_like_html(page.body) and b"<a" not in page.body[:200000].lower():
            return FetchResult(error="response is not an HTML page")
        links = extract_article_links(page.body, page.url)
        tree_title = HTMLParser(page.body).css_first("title")
        title = " ".join(tree_title.text(strip=True).split()) if tree_title is not None else ""
        docs = [
            RawDocument(
                source_id=source.id or 0,
                external_id=link,
                url=link,
                fetched_at=to_utc_iso(now) or "",
                needs_fulltext=True,
            )
            for link in links
        ]
        log.debug("%s: %d candidate links", source.name, len(docs))
        return FetchResult(documents=docs, source_title=title or None)
