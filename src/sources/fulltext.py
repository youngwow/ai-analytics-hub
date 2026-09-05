"""Full-text extraction for documents whose source only gave us a link or an announce.

trafilatura pulls the main text plus title/author/date out of arbitrary HTML
(works well on Russian pages and handles windows-1251 itself — always hand it
bytes). No disk cache: the documents table is the cache.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import httpx
import trafilatura

from ..config import ScraperConfig
from ..models import RawDocument
from ..utils import get_logger, parse_datetime, to_utc_iso
from .base import HostLimiter, fetch

log = get_logger("fulltext")

_LINE_WS_RE = re.compile(r"[ \t\xa0]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def tidy(text: str) -> str:
    """Collapse the indentation and blank-line runs trafilatura keeps from sloppy markup."""
    lines = [_LINE_WS_RE.sub(" ", line).strip() for line in (text or "").splitlines()]
    return _MULTI_NL_RE.sub("\n\n", "\n".join(lines)).strip()


@dataclass
class Extracted:
    text: str = ""
    title: str = ""
    author: str = ""
    date: str | None = None  # canonical UTC ISO, or None


def extract(body: bytes | str, url: str, max_chars: int) -> Extracted | None:
    """Run trafilatura on already-fetched HTML. None when nothing usable was found."""
    try:
        doc = trafilatura.bare_extraction(
            body,
            url=url,
            with_metadata=True,
            include_comments=False,
            # Full timestamp when the page has one; day-only pages come back as
            # "YYYY-MM-DDT00:00:00" and degrade to midnight Moscow in parse_datetime.
            date_extraction_params={
                "extensive_search": True,
                "original_date": True,
                "outputformat": "%Y-%m-%dT%H:%M:%S%z",
            },
        )
    except Exception as e:  # noqa: BLE001 — lxml/trafilatura raise assorted errors on junk
        log.debug("extraction failed for %s: %s", url, e)
        return None
    text = tidy(doc.text) if doc is not None else ""
    if not text:
        return None
    return Extracted(
        text=text[:max_chars],
        title=(doc.title or "").strip(),
        author=(doc.author or "").strip(),
        date=to_utc_iso(parse_datetime(doc.date)) if doc.date else None,
    )


class FullTextFetcher:
    """Fetches article pages in parallel and fills in text/title/author/date."""

    def __init__(self, config: ScraperConfig, limiter: HostLimiter | None = None):
        self.config = config
        self.limiter = limiter

    def fetch_one(self, client: httpx.Client, url: str) -> tuple[Extracted | None, bytes | None]:
        """Returns (extracted, raw_html). raw_html is None when the fetch itself failed."""
        page = fetch(client, url, limiter=self.limiter, timeout=self.config.fulltext_timeout)
        if not page.ok:
            log.debug("fulltext fetch failed for %s: %s", url, page.error or f"HTTP {page.status}")
            return None, None
        ctype = page.headers.get("content-type", "").lower()
        if ctype and "html" not in ctype and "xml" not in ctype and "text" not in ctype:
            log.debug("fulltext skipped %s: content-type %s", url, ctype)
            return None, None
        return extract(page.body, page.url, self.config.fulltext_max_chars), page.body

    def enrich(self, client: httpx.Client, docs: list[RawDocument]) -> int:
        """Fill `text` (and missing title/author/date) in place; returns how many got text."""
        targets = [d for d in docs if d.needs_fulltext and d.url]
        if not targets:
            return 0
        filled = failed = 0
        workers = min(8, len(targets))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.fetch_one, client, d.url): d for d in targets}
            for fut in as_completed(futures):
                doc = futures[fut]
                try:
                    extracted, raw = fut.result()
                except Exception as e:  # noqa: BLE001
                    log.debug("unhandled fulltext error for %s: %s", doc.url, e)
                    extracted, raw = None, None
                doc.needs_fulltext = False
                if extracted is None:
                    failed += 1
                    if raw and self.config.store_raw_html:
                        doc.raw_html = raw.decode("utf-8", errors="replace")
                    continue
                filled += 1
                doc.text = extracted.text
                if not doc.title:
                    doc.title = extracted.title
                if not doc.author:
                    doc.author = extracted.author
                if not doc.published_at and extracted.date:
                    doc.published_at = extracted.date
                if raw and self.config.store_raw_html:
                    doc.raw_html = raw.decode("utf-8", errors="replace")
        log.info("fulltext: %d/%d pages extracted (%d failed)", filled, len(targets), failed)
        return filled
