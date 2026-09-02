"""RSS/Atom adapter: conditional GET, feedparser, full text straight from the feed when offered."""

from __future__ import annotations

from datetime import datetime

import feedparser
import httpx

from ..common import get_logger, parse_datetime, struct_time_to_datetime, to_utc_iso
from ..config import ScraperConfig
from ..models import FetchResult, FetchState, RawDocument, Source
from .base import HostLimiter, fetch
from .textutil import clean_summary, clean_url, html_to_text, looks_like_html, strip_html

log = get_logger("rss")

# A description this long is the article itself, not an announce.
FULL_SUMMARY_CHARS = 1500

# Russian media embed the whole article in these; feedparser exposes them as
# `entry["yandex_full-text"]` / `entry["turbo_content"]` and `entry.content`.
_FULLTEXT_KEYS = ("yandex_full-text", "turbo_content")


def is_feed(parsed) -> bool:
    """feedparser sets `version` to '' for anything that is not a feed."""
    return bool(parsed.get("version")) or bool(parsed.entries)


def entry_full_text(entry) -> str:
    """Best available full text from the entry itself, or ''."""
    for key in _FULLTEXT_KEYS:
        value = entry.get(key)
        if value and value.strip():
            return html_to_text(value)
    content = entry.get("content") or []
    best = ""
    for item in content:
        value = html_to_text(item.get("value") or "")
        if len(value) > len(best):
            best = value
    return best


def parse_entry(entry, source: Source, now: datetime) -> RawDocument | None:
    link = clean_url(entry.get("link") or "")
    external_id = (entry.get("id") or link).strip()
    if not external_id:
        return None
    if not link and external_id.startswith("http"):
        link = clean_url(external_id)
    raw_summary = entry.get("summary") or entry.get("description") or ""
    summary = clean_summary(html_to_text(raw_summary))
    text = entry_full_text(entry)
    if not text and len(summary) >= FULL_SUMMARY_CHARS:
        text = summary
    published = (
        struct_time_to_datetime(entry.get("published_parsed"))
        or struct_time_to_datetime(entry.get("updated_parsed"))
        or parse_datetime(entry.get("published") or entry.get("updated"))
    )
    # Enclosures are attachments (PDF digests, documents); article images are not.
    attachments = [
        e.get("href")
        for e in entry.get("enclosures") or []
        if e.get("href") and not (e.get("type") or "").startswith("image/")
    ]
    return RawDocument(
        source_id=source.id or 0,
        external_id=external_id,
        url=link or external_id,
        title=strip_html(entry.get("title") or "").strip(),
        summary=summary,
        text=text,
        author=strip_html(entry.get("author") or "").strip(),
        attachments=attachments,
        published_at=to_utc_iso(published),
        fetched_at=to_utc_iso(now) or "",
        needs_fulltext=not text and bool(link),
    )


class RssAdapter:
    kind = "rss"

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
        headers: dict[str, str] = {}
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified
        page = fetch(client, source.fetch_url, limiter=self.limiter, headers=headers)
        if page.error:
            return FetchResult(error=page.error)
        if page.status == 304:
            return FetchResult(not_modified=True)
        if page.status >= 400:
            return FetchResult(error=f"HTTP {page.status}")
        if looks_like_html(page.body):
            return FetchResult(error="got an HTML page instead of a feed")
        parsed = feedparser.parse(page.body)
        if not is_feed(parsed):
            reason = parsed.get("bozo_exception")
            return FetchResult(error=f"not a feed ({reason})" if reason else "not a feed")
        if parsed.bozo and parsed.entries:
            log.info(
                "%s: feed is ill-formed (%s) but has entries — using them",
                source.name,
                parsed.bozo_exception,
            )
        docs = [d for d in (parse_entry(e, source, now) for e in parsed.entries) if d]
        state_update = {
            "etag": page.headers.get("etag"),
            "last_modified": page.headers.get("last-modified"),
        }
        return FetchResult(documents=docs, state_update=state_update)
