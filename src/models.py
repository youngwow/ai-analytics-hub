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
DIRECTIONS = ("pr", "gr", "both")


def _row_value(row, key: str, default=None):
    """Read sqlite.Row or a partial dict produced by older tests/importers."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _json_dict(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


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
    direction: str = "both"
    source_class: str = "ordinary"
    status: str = "active"

    def __post_init__(self) -> None:
        self.direction = self.direction.lower()
        if self.direction not in DIRECTIONS:
            raise ValueError(f"direction must be one of {DIRECTIONS}, got {self.direction!r}")

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
            direction=_row_value(row, "direction", "both") or "both",
            source_class=_row_value(row, "source_class", "ordinary") or "ordinary",
            status=_row_value(row, "status", "active") or "active",
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
    native_cursor: str | None = None
    high_watermark: str | None = None
    continuation_cursor: dict = field(default_factory=dict)
    backlog_status: str = "clear"
    coverage_from: str | None = None
    coverage_to: str | None = None
    coverage_status: str = "unknown"

    @property
    def first_run(self) -> bool:
        return self.last_success_at is None

    @classmethod
    def from_row(cls, row) -> "FetchState":
        cursor = _json_dict(row["cursor"])
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
            native_cursor=_row_value(row, "native_cursor"),
            high_watermark=_row_value(row, "high_watermark"),
            continuation_cursor=_json_dict(_row_value(row, "continuation_cursor")),
            backlog_status=_row_value(row, "backlog_status", "clear") or "clear",
            coverage_from=_row_value(row, "coverage_from"),
            coverage_to=_row_value(row, "coverage_to"),
            coverage_status=_row_value(row, "coverage_status", "unknown") or "unknown",
        )


@dataclass
class FetchResult:
    """What an adapter returns for one source poll."""

    documents: list[RawDocument] = field(default_factory=list)
    state_update: dict = field(default_factory=dict)  # etag / last_modified / cursor
    not_modified: bool = False
    error: str | None = None
    source_title: str | None = None  # title the site/channel reports about itself
    # Recoverable subrequest/parser failures. The source may still return useful
    # documents, but the collector must expose the run as partial, never as clean ok.
    warnings: list[str] = field(default_factory=list)

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
    sources_partial: int = 0
    sources_fail: int = 0
    sources_not_modified: int = 0
    docs_new: int = 0
    per_source: list[dict] = field(default_factory=list)

    def summary_line(self) -> str:
        return (
            f"{self.docs_new} new documents; sources ok={self.sources_ok} "
            f"partial={self.sources_partial} "
            f"not_modified={self.sources_not_modified} failed={self.sources_fail}"
        )


ITEM_TYPES = ("npa", "news")
PRIORITIES = ("high", "medium", "low")
NPA_STATUSES = ("анонс", "разработка", "внесён", "рассмотрение", "принят", "действует", "архив")
ENTITY_ROLES = ("who", "what", "when", "impact", "org", "act_number")
ITEM_TAGS = ("регуляторика", "репутация", "конкуренты", "тренды", "господдержка/льготы")


def _json_list(raw) -> list:
    try:
        value = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


@dataclass
class Cluster:
    """A group of publications about one event; one card is built per cluster."""

    canonical_document_id: int
    centroid_embedding: bytes | None = None
    size: int = 1
    has_divergent_opinions: bool = False
    created_at: str = ""
    id: int | None = None

    @classmethod
    def from_row(cls, row) -> "Cluster":
        return cls(
            canonical_document_id=row["canonical_document_id"],
            centroid_embedding=row["centroid_embedding"],
            size=row["size"] or 1,
            has_divergent_opinions=bool(row["has_divergent_opinions"]),
            created_at=row["created_at"] or "",
            id=row["id"],
        )


@dataclass
class EntitySpan:
    """One extracted entity plus where it is grounded in `documents.norm_text`."""

    role: str
    value: str
    normalized_value: str = ""
    evidence_start: int | None = None
    evidence_end: int | None = None
    id: int | None = None

    @classmethod
    def from_row(cls, row) -> "EntitySpan":
        return cls(
            role=row["role"],
            value=row["value"],
            normalized_value=row["normalized_value"] or "",
            evidence_start=row["evidence_start"],
            evidence_end=row["evidence_end"],
            id=row["id"],
        )


@dataclass
class Item:
    """A feed card: what the analyst reads instead of the original."""

    cluster_id: int
    type: str = "news"
    npa_status: str | None = None
    npa_key: str | None = None
    title: str = ""
    summary: str = ""
    priority: str = "medium"
    relevance_score: float = 0.0
    reasoning: str = ""
    confidence: float = 0.0
    tags: list[str] = field(default_factory=list)
    analyst_note: str = ""
    is_hidden: bool = False
    is_archived: bool = False
    degraded: bool = False
    needs_review: bool = False
    date_estimated: bool = False
    model_name: str = ""
    prompt_version: int | None = None
    profile_version: int | None = None
    edited_fields: list[str] = field(default_factory=list)
    processed_at: str = ""
    published_at: str | None = None
    id: int | None = None

    def to_row(self) -> dict:
        return {
            "cluster_id": self.cluster_id,
            "type": self.type,
            "npa_status": self.npa_status,
            "npa_key": self.npa_key or None,
            "title": self.title,
            "summary": self.summary,
            "priority": self.priority,
            "relevance_score": float(self.relevance_score),
            "reasoning": self.reasoning,
            "confidence": float(self.confidence),
            "tags": json.dumps(self.tags, ensure_ascii=False),
            "analyst_note": self.analyst_note,
            "is_hidden": int(self.is_hidden),
            "is_archived": int(self.is_archived),
            "degraded": int(self.degraded),
            "needs_review": int(self.needs_review),
            "date_estimated": int(self.date_estimated),
            "model_name": self.model_name,
            "prompt_version": self.prompt_version,
            "profile_version": self.profile_version,
            "edited_fields": json.dumps(self.edited_fields, ensure_ascii=False),
            "processed_at": self.processed_at,
            "published_at": self.published_at,
        }

    @classmethod
    def from_row(cls, row) -> "Item":
        return cls(
            cluster_id=row["cluster_id"],
            type=row["type"],
            npa_status=row["npa_status"],
            npa_key=row["npa_key"],
            title=row["title"] or "",
            summary=row["summary"] or "",
            priority=row["priority"] or "medium",
            relevance_score=row["relevance_score"] or 0.0,
            reasoning=row["reasoning"] or "",
            confidence=row["confidence"] or 0.0,
            tags=_json_list(row["tags"]),
            analyst_note=row["analyst_note"] or "",
            is_hidden=bool(row["is_hidden"]),
            is_archived=bool(row["is_archived"]),
            degraded=bool(row["degraded"]),
            needs_review=bool(row["needs_review"]),
            date_estimated=bool(row["date_estimated"]),
            model_name=row["model_name"] or "",
            prompt_version=row["prompt_version"],
            profile_version=row["profile_version"],
            edited_fields=_json_list(row["edited_fields"]),
            processed_at=row["processed_at"] or "",
            published_at=row["published_at"],
            id=row["id"],
        )


@dataclass
class NpaEvent:
    """One step of a bill's life; kept even after the card is archived."""

    item_id: int
    status: str
    occurred_at: str | None = None
    source_url: str = ""
    note: str = ""
    created_by: str = "system"
    created_at: str = ""
    id: int | None = None

    @classmethod
    def from_row(cls, row) -> "NpaEvent":
        return cls(
            item_id=row["item_id"],
            status=row["status"],
            occurred_at=row["occurred_at"],
            source_url=row["source_url"] or "",
            note=row["note"] or "",
            created_by=row["created_by"] or "system",
            created_at=row["created_at"] or "",
            id=row["id"],
        )


@dataclass
class ItemRevision:
    """Audit of one field edit — and the training data for later quality work."""

    item_id: int
    field: str
    old_value: str | None = None
    new_value: str | None = None
    actor: str = "user"
    created_at: str = ""
    id: int | None = None

    @classmethod
    def from_row(cls, row) -> "ItemRevision":
        return cls(
            item_id=row["item_id"],
            field=row["field"],
            old_value=row["old_value"],
            new_value=row["new_value"],
            actor=row["actor"] or "user",
            created_at=row["created_at"] or "",
            id=row["id"],
        )


@dataclass
class CompanyProfile:
    """Whose point of view decides priority; a version equals a prompt fragment."""

    name: str
    payload: dict = field(default_factory=dict)
    version: int = 1
    is_default: bool = False
    updated_at: str = ""
    id: int | None = None

    @classmethod
    def from_row(cls, row) -> "CompanyProfile":
        try:
            payload = json.loads(row["payload"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        return cls(
            name=row["name"],
            payload=payload if isinstance(payload, dict) else {},
            version=row["version"] or 1,
            is_default=bool(row["is_default"]),
            updated_at=row["updated_at"] or "",
            id=row["id"],
        )

    def prompt_block(self) -> str:
        """The profile as it goes into the prompt: stable order, no analyst-only fields."""
        lines = [f"Компания: {self.name}"]
        labels = {
            "industry": "Отрасль",
            "products": "Продукты",
            "stack": "Стек",
            "regime": "Налоговый/аккредитационный режим",
            "regulators": "Регуляторы",
            "competitors": "Конкуренты",
            "topics": "Ключевые темы",
            "negative_facets": "НЕ относится к компании",
        }
        for key, label in labels.items():
            value = self.payload.get(key)
            if not value:
                continue
            text = ", ".join(map(str, value)) if isinstance(value, (list, tuple)) else str(value)
            lines.append(f"{label}: {text}")
        return "\n".join(lines)


@dataclass
class LlmCall:
    """Telemetry of one model call — the measurement behind the 15 s budget."""

    stage: str
    model: str
    item_id: int | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    cost: float = 0.0
    status: str = "ok"
    error: str = ""
    created_at: str = ""
    id: int | None = None
