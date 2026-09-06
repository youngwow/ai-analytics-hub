"""Sitemap adapter for sites without a feed: walk sitemap.xml by `lastmod`.

Entries newer than the cursor (or, on the first run, inside the date window)
become documents that the collector sends through full-text extraction.
Sitemap indexes are followed newest-first and capped; nested indexes are not
recursed into.
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import httpx

from ..config import ScraperConfig, SitemapConfig
from ..models import FetchResult, FetchState, RawDocument, Source
from ..utils import get_logger, parse_datetime, to_utc_iso
from .base import HostLimiter, fetch

log = get_logger("sitemap")

_GZIP_MAGIC = b"\x1f\x8b"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass
class SitemapEntry:
    loc: str
    lastmod: datetime | None = None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(body: bytes) -> tuple[str, list[SitemapEntry]]:
    """Returns ("index" | "urlset" | "", entries). Handles gzip and Google News dates."""
    if body[:2] == _GZIP_MAGIC:
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError):
            return "", []
    body = body.lstrip(b"\xef\xbb\xbf \t\r\n")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return "", []
    kind = _local(root.tag)
    if kind not in ("sitemapindex", "urlset"):
        return "", []
    entries: list[SitemapEntry] = []
    for child in root:
        if _local(child.tag) not in ("sitemap", "url"):
            continue
        loc = None
        lastmod = None
        for el in child.iter():
            name = _local(el.tag)
            if name == "loc" and loc is None:
                loc = (el.text or "").strip()
            elif name == "lastmod" and lastmod is None:
                lastmod = parse_datetime(el.text)
            elif name == "publication_date" and lastmod is None:  # <news:publication_date>
                lastmod = parse_datetime(el.text)
        if loc:
            entries.append(SitemapEntry(loc=loc, lastmod=lastmod))
    return ("index" if kind == "sitemapindex" else "urlset"), entries


def has_lastmod(entries: list[SitemapEntry]) -> bool:
    return any(e.lastmod is not None for e in entries)


def _same_host(url: str, reference: str) -> bool:
    a = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    b = (urlsplit(reference).hostname or "").lower().removeprefix("www.")
    return a == b


def _child_priority(entry: SitemapEntry, year: int) -> tuple:
    """Newest lastmod first; undated children ranked by name hints (news / current year)."""
    hint = 0
    lowered = entry.loc.lower()
    if "news" in lowered or "novosti" in lowered or str(year) in lowered:
        hint = 1
    return (entry.lastmod or _EPOCH, hint)


class SitemapAdapter:
    kind = "sitemap"

    def __init__(
        self, config: ScraperConfig, sm_config: SitemapConfig, limiter: HostLimiter | None = None
    ):
        self.config = config
        self.sm = sm_config
        self.limiter = limiter

    def _load(self, client: httpx.Client, url: str) -> tuple[str, list[SitemapEntry], str | None]:
        page = fetch(client, url, limiter=self.limiter)
        if page.error:
            return "", [], page.error
        if page.status >= 400:
            return "", [], f"HTTP {page.status}"
        kind, entries = parse_sitemap(page.body)
        if not kind:
            return "", [], "not a sitemap"
        return kind, entries, None

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
        cursor = parse_datetime(state.cursor.get("lastmod"))
        lower = None if backfill else (cursor or since)

        kind, entries, error = self._load(client, source.fetch_url)
        if error:
            return FetchResult(error=error)
        urls: list[SitemapEntry] = []
        if kind == "index":
            children = [e for e in entries if _same_host(e.loc, source.fetch_url)]
            children.sort(key=lambda e: _child_priority(e, now.year), reverse=True)
            for child in children[: self.sm.max_sitemaps]:
                if lower and child.lastmod and child.lastmod < lower:
                    continue  # whole child sitemap is older than what we need
                ckind, centries, cerror = self._load(client, child.loc)
                if cerror:
                    log.warning("%s: child sitemap %s: %s", source.name, child.loc, cerror)
                    continue
                if ckind == "index":
                    log.info("%s: skipping nested sitemap index %s", source.name, child.loc)
                    continue
                urls.extend(centries)
        else:
            urls = entries

        urls = [u for u in urls if _same_host(u.loc, source.fetch_url)]
        first_run = state.first_run and not cursor
        selected: list[SitemapEntry] = []
        for u in urls:
            if u.lastmod is None:
                if first_run or backfill:
                    selected.append(u)  # can't date it; only worth it once
                continue
            if lower and u.lastmod < lower:
                continue
            selected.append(u)
        selected.sort(key=lambda e: e.lastmod or _EPOCH, reverse=True)
        selected = selected[: self.sm.max_urls]

        docs = [
            RawDocument(
                source_id=source.id or 0,
                external_id=u.loc,
                url=u.loc,
                published_at=to_utc_iso(u.lastmod),
                fetched_at=to_utc_iso(now) or "",
                needs_fulltext=True,
            )
            for u in selected
        ]
        newest = max((u.lastmod for u in urls if u.lastmod), default=None)
        cursor_update = dict(state.cursor)
        if newest and (cursor is None or newest > cursor):
            cursor_update["lastmod"] = to_utc_iso(newest)
        log.debug("%s: %d sitemap urls, %d selected", source.name, len(urls), len(docs))
        return FetchResult(documents=docs, state_update={"cursor": cursor_update})
