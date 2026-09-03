"""Domain models for the ingestion layer.

Adapters produce `RawDocument`s (the normalised shape from scraper.md); the
collector persists them and tracks per-source `FetchState`. JSON/SQL
(de)serialisation happens only at the storage boundary via to_row/from_row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .common import sha256_text

KINDS = ("rss", "telegram", "sitemap", "html", "manual", "search")
CATEGORIES = ("media", "regulator", "telegram", "manual")


@dataclass
class Source:
    """A monitored source. `url` is what the user typed; `fetch_url` is what we poll."""

    id: int | None = None
    name: str = ""
    url: str = ""
    kind: str = "html"
    category: str = "media"
    fetch_url: str = ""
    enabled: bool = True
    created_at: str = ""
    notes: str = ""

    @classmethod
    def from_row(cls, row) -> "Source":
        return cls(
            id=row["id"],
            name=row["name"],
            url=row["url"],
            kind=row["kind"],
            category=row["category"],
            fetch_url=row["fetch_url"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            notes=row["notes"] or "",
        )


@dataclass
class RawDocument:
    """One collected publication, normalised across all adapters."""

    source_id: int
    external_id: str
    url: str
    title: str = ""
    summary: str = ""
    text: str = ""
    raw_html: str | None = None
    author: str = ""
    attachments: list[str] = field(default_factory=list)
    published_at: str | None = None
    fetched_at: str = ""
    content_hash: str = ""
    needs_fulltext: bool = False  # adapter hint; not persisted

    def compute_hash(self) -> str:
        self.content_hash = sha256_text(self.title, self.text or self.summary)
        return self.content_hash

    def to_row(self) -> dict:
        return {
            "source_id": self.source_id,
            "external_id": self.external_id,
            "url": self.url,
            "title": self.title,
            "summary": self.summary,
            "text": self.text,
            "raw_html": self.raw_html,
            "author": self.author,
            "attachments": json.dumps(self.attachments, ensure_ascii=False),
            "published_at": self.published_at,
            "fetched_at": self.fetched_at,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_row(cls, row) -> "RawDocument":
        try:
            attachments = json.loads(row["attachments"] or "[]")
        except (ValueError, TypeError):
            attachments = []
        return cls(
            source_id=row["source_id"],
            external_id=row["external_id"],
            url=row["url"],
            title=row["title"] or "",
            summary=row["summary"] or "",
            text=row["text"] or "",
            raw_html=row["raw_html"],
            author=row["author"] or "",
            attachments=attachments,
            published_at=row["published_at"],
            fetched_at=row["fetched_at"] or "",
            content_hash=row["content_hash"] or "",
        )


@dataclass
class FetchState:
    """Per-source incremental-fetch state; `cursor` is adapter-owned JSON."""

    source_id: int
    etag: str | None = None
    last_modified: str | None = None
    last_fetch_at: str | None = None
    last_success_at: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    last_doc_count: int = 0
    cursor: dict = field(default_factory=dict)

    @property
    def first_run(self) -> bool:
        return self.last_success_at is None

    @classmethod
    def from_row(cls, row) -> "FetchState":
        try:
            cursor = json.loads(row["cursor"] or "{}")
        except (ValueError, TypeError):
            cursor = {}
        return cls(
            source_id=row["source_id"],
            etag=row["etag"],
            last_modified=row["last_modified"],
            last_fetch_at=row["last_fetch_at"],
            last_success_at=row["last_success_at"],
            last_error=row["last_error"],
            consecutive_failures=row["consecutive_failures"] or 0,
            last_doc_count=row["last_doc_count"] or 0,
            cursor=cursor if isinstance(cursor, dict) else {},
        )


@dataclass
class FetchResult:
    """What an adapter returns for one source poll."""

    documents: list[RawDocument] = field(default_factory=list)
    state_update: dict = field(default_factory=dict)  # etag / last_modified / cursor
    not_modified: bool = False
    error: str | None = None
    source_title: str | None = None  # title the site/channel reports about itself

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class Resolution:
    """Outcome of the URL resolver: how a user-entered URL should be polled."""

    kind: str
    fetch_url: str
    name: str = ""
    note: str = ""


@dataclass
class CollectReport:
    started_at: str = ""
    finished_at: str = ""
    sources_ok: int = 0
    sources_fail: int = 0
    sources_not_modified: int = 0
    docs_new: int = 0
    per_source: list[dict] = field(default_factory=list)

    def summary_line(self) -> str:
        return (
            f"{self.docs_new} new documents; sources ok={self.sources_ok} "
            f"not_modified={self.sources_not_modified} failed={self.sources_fail}"
        )
