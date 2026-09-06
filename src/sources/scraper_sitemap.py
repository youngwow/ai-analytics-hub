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

from ..common import get_logger, parse_datetime, to_utc_iso
from ..config import ScraperConfig, SitemapConfig
from ..models import FetchResult, FetchState, RawDocument, Source
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
        cursor_loc = str(state.cursor.get("last_loc") or "")
        lower = None if backfill else (cursor or since)

        kind, entries, error = self._load(client, source.fetch_url)
        if error:
            return FetchResult(error=error)
        urls: list[SitemapEntry] = []
        warnings: list[str] = []
        if kind == "index":
            children = [e for e in entries if _same_host(e.loc, source.fetch_url)]
            children.sort(key=lambda e: _child_priority(e, now.year), reverse=True)
            for child in children[: self.sm.max_sitemaps]:
                if lower and child.lastmod and child.lastmod < lower:
                    continue  # whole child sitemap is older than what we need
                ckind, centries, cerror = self._load(client, child.loc)
                if cerror:
                    warning = f"child sitemap {child.loc}: {cerror}"
                    warnings.append(warning)
                    log.warning("%s: %s", source.name, warning)
                    continue
                if ckind == "index":
                    warning = f"nested sitemap index unsupported: {child.loc}"
                    warnings.append(warning)
                    log.warning("%s: %s", source.name, warning)
                    continue
                urls.extend(centries)
        else:
            urls = entries

        urls = [u for u in urls if _same_host(u.loc, source.fetch_url)]
        first_run = state.first_run and not cursor
        dated: list[SitemapEntry] = []
        undated: list[SitemapEntry] = []
        for u in urls:
            if u.lastmod is None:
                if first_run or backfill or state.backlog_status == "pending":
                    undated.append(u)
                continue
            if lower and u.lastmod < lower:
                continue
            if cursor and u.lastmod == cursor and cursor_loc and u.loc <= cursor_loc:
                continue
            dated.append(u)

        # Oldest first is deliberate: when the budget is full, the next cycle
        # resumes after the last processed (lastmod, URL) pair instead of jumping
        # the cursor to the newest URL and silently losing the tail.
        dated.sort(key=lambda e: (e.lastmod or _EPOCH, e.loc))
        undated.sort(key=lambda e: e.loc)
        undated_after = str(state.continuation_cursor.get("undated_after") or "")
        if undated_after:
            undated = [u for u in undated if u.loc > undated_after]
        eligible = [*dated, *undated]
        remaining = len(eligible) > self.sm.max_urls
        if remaining:
            selected = eligible[: self.sm.max_urls]
        else:
            # Preserve the familiar newest-first output when the whole observed
            # set fits. Ordering only becomes oldest-first when continuation is
            # required to make the cursor lossless.
            selected = [*reversed(dated), *undated]

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
        cursor_update = dict(state.cursor)
        processed_dated = [u for u in selected if u.lastmod is not None]
        if processed_dated:
            last = max(processed_dated, key=lambda u: (u.lastmod or _EPOCH, u.loc))
            cursor_update["lastmod"] = to_utc_iso(last.lastmod)
            cursor_update["last_loc"] = last.loc
        processed_undated = [u for u in selected if u.lastmod is None]
        continuation = dict(state.continuation_cursor)
        if processed_undated and remaining:
            continuation["undated_after"] = processed_undated[-1].loc
        elif not remaining:
            continuation.pop("undated_after", None)
        coverage_dates = [u.lastmod for u in selected if u.lastmod]
        if remaining:
            warnings.append("sitemap URL budget reached; continuation will resume next cycle")
        log.debug("%s: %d sitemap urls, %d selected", source.name, len(urls), len(docs))
        return FetchResult(
            documents=docs,
            state_update={
                "cursor": cursor_update,
                "native_cursor": cursor_update.get("lastmod"),
                "high_watermark": cursor_update.get("lastmod"),
                "continuation_cursor": continuation,
                "backlog_status": "pending" if remaining else "clear",
                "coverage_from": to_utc_iso(min(coverage_dates)) if coverage_dates else None,
                "coverage_to": to_utc_iso(max(coverage_dates)) if coverage_dates else None,
                "coverage_status": "partial" if remaining else "complete",
            },
            warnings=warnings,
        )
