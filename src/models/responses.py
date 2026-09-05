"""Исходящие схемы HTTP-границы (pydantic).

`from_domain()` — единственное место, где dataclass становится проводной формой.
Поля повторяют dataclass'ы один к одному, чтобы форма ответа не разъехалась с
тем, что видит CLI. Строки SQLite до этого модуля не доходят: сервисы отдают
словари.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from pydantic import BaseModel, Field

from .domain import EntitySpan, Item, ItemNote, ItemRevision, ItemTag, NpaEvent, Source, SourceRun


class ProblemResponse(BaseModel):
    """RFC 7807 плюс расширения `code` и `details`, по которым ветвится UI."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str
    code: str | None = None
    details: dict | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    app: str
    version: str
    environment: str
    checks: dict[str, str] = Field(default_factory=dict)


# ── источники ──────────────────────────────────────────────────────────────


class SourceResponse(BaseModel):
    id: int | None
    name: str
    url: str
    kind: str
    category: str
    fetch_url: str
    status: str
    normalized_url: str
    poll_interval: str
    next_run_at: str | None
    category_hint: str | None
    created_at: str
    notes: str
    deleted_at: str | None
    created_by: str

    @classmethod
    def from_domain(cls, source: Source) -> SourceResponse:
        return cls(**asdict(source))


class SourceListResponse(BaseModel):
    sources: list[SourceResponse]


class PreviewEntry(BaseModel):
    title: str
    url: str
    published_at: str | None


class ProbeResponse(BaseModel):
    resolved_type: str
    feed_url: str
    title: str
    detection_method: str
    suggested_poll_interval: str
    already_exists: bool
    already_exists_source_id: int | None
    preview: list[PreviewEntry]
    warnings: list[str]
    note: str

    @classmethod
    def from_domain(cls, probe) -> ProbeResponse:
        return cls(**asdict(probe))


class SourceDeleteResponse(BaseModel):
    source_id: int
    name: str
    documents_kept: int
    items_hidden: int
    tracked_npa: int


class SourceRunResponse(BaseModel):
    source_id: int
    started_at: str
    finished_at: str | None
    http_status: int | None
    items_found: int
    items_new: int
    error_code: str
    error_message: str
    id: int | None

    @classmethod
    def from_domain(cls, run: SourceRun) -> SourceRunResponse:
        return cls(**asdict(run))


class SourceHealthResponse(BaseModel):
    source: SourceResponse
    documents: int
    consecutive_failures: int
    last_success_at: str | None
    last_error: str | None
    runs: list[SourceRunResponse]


# ── карточки ───────────────────────────────────────────────────────────────


class ItemResponse(BaseModel):
    cluster_id: int
    type: str
    npa_status: str | None
    npa_key: str | None
    title: str
    summary: str
    priority: str
    relevance_score: float
    reasoning: str
    confidence: float
    tags: list[str]
    analyst_note: str
    visibility: str
    hidden_reason: str
    origin: str
    is_archived: bool
    degraded: bool
    needs_review: bool
    date_estimated: bool
    model_name: str
    prompt_version: int | None
    profile_version: int | None
    manual_overrides: list[str]
    processed_at: str
    published_at: str | None
    id: int | None

    @classmethod
    def from_domain(cls, item: Item) -> ItemResponse:
        return cls(**asdict(item))


class EntityResponse(BaseModel):
    role: str
    value: str
    normalized_value: str
    evidence_start: int | None
    evidence_end: int | None
    id: int | None

    @classmethod
    def from_domain(cls, span: EntitySpan) -> EntityResponse:
        return cls(**asdict(span))


class ItemSourceResponse(BaseModel):
    """Документ, стоящий за карточкой: `is_canonical` — 0/1, как в базе."""

    id: int
    url: str
    title: str | None
    published_at: str | None
    is_canonical: int
    source_name: str | None


class NpaEventResponse(BaseModel):
    item_id: int
    status: str
    occurred_at: str | None
    source_url: str
    note: str
    created_by: str
    created_at: str
    id: int | None

    @classmethod
    def from_domain(cls, event: NpaEvent) -> NpaEventResponse:
        return cls(**asdict(event))


class RevisionResponse(BaseModel):
    item_id: int
    field: str
    old_value: str | None
    new_value: str | None
    actor: str
    source_of_change: str
    edit_reason: str
    created_at: str
    id: int | None

    @classmethod
    def from_domain(cls, revision: ItemRevision) -> RevisionResponse:
        return cls(**asdict(revision))


class NoteResponse(BaseModel):
    item_id: int
    body: str
    author: str
    created_at: str
    id: int | None

    @classmethod
    def from_domain(cls, note: ItemNote) -> NoteResponse:
        return cls(**asdict(note))


class TagResponse(BaseModel):
    item_id: int
    tag: str
    is_manual: bool

    @classmethod
    def from_domain(cls, tag: ItemTag) -> TagResponse:
        return cls(**asdict(tag))


class ItemCardResponse(BaseModel):
    """Карточка целиком — всё, что нужно экрану карточки за один запрос."""

    canonical_url: str | None
    item: ItemResponse
    entities: list[EntityResponse]
    sources: list[ItemSourceResponse]
    events: list[NpaEventResponse]
    revisions: list[RevisionResponse]
    notes: list[NoteResponse]
    tags: list[TagResponse]
    model_proposals: dict[str, RevisionResponse | None]

    @classmethod
    def from_domain(cls, card: dict) -> ItemCardResponse:
        canonical = next((s for s in card["sources"] if s["is_canonical"]), None)
        return cls(
            canonical_url=(canonical["url"] if canonical else None) or None,
            item=ItemResponse.from_domain(card["item"]),
            entities=[EntityResponse.from_domain(e) for e in card["entities"]],
            sources=[ItemSourceResponse(**s) for s in card["sources"]],
            events=[NpaEventResponse.from_domain(e) for e in card["events"]],
            revisions=[RevisionResponse.from_domain(r) for r in card["revisions"]],
            notes=[NoteResponse.from_domain(n) for n in card["notes"]],
            tags=[TagResponse.from_domain(t) for t in card["tags"]],
            model_proposals={
                name: (RevisionResponse.from_domain(rev) if rev else None)
                for name, rev in card["model_proposals"].items()
            },
        )


class ItemEditResponse(BaseModel):
    item: ItemResponse
    manual_overrides: list[str]

    @classmethod
    def from_domain(cls, item: Item) -> ItemEditResponse:
        return cls(item=ItemResponse.from_domain(item), manual_overrides=list(item.manual_overrides))


class VisibilityResponse(BaseModel):
    id: int | None
    visibility: str

    @classmethod
    def from_domain(cls, item: Item) -> VisibilityResponse:
        return cls(id=item.id, visibility=item.visibility)


class BulkResponse(BaseModel):
    changed: int
    scope: str


class RevisionListResponse(BaseModel):
    revisions: list[RevisionResponse]


class ManualItemResponse(BaseModel):
    id: int | None
    document_id: int
    origin: str = "manual"
    processing_status: str


# ── лента ──────────────────────────────────────────────────────────────────


class FeedFlags(BaseModel):
    degraded: bool
    needs_review: bool
    date_estimated: bool
    edited: bool


class FeedItemResponse(BaseModel):
    id: int
    type: str
    npa_status: str | None
    npa_key: str | None
    priority: str
    title: str | None
    summary: str | None
    tags: list[str]
    published_at: str | None
    sources_count: int | None
    canonical_url: str | None
    source_name: str | None
    visibility: str
    hidden_reason: str
    origin: str
    confidence: float | None
    relevance_score: float | None
    reasoning: str | None
    snippet: str | None
    flags: FeedFlags


class FeedResponse(BaseModel):
    items: list[FeedItemResponse]
    total: int
    next_cursor: str | None
    took_ms: int


class FacetSourceEntry(BaseModel):
    source_id: int
    name: str
    count: int


class FacetTagEntry(BaseModel):
    tag: str
    count: int


class FacetsResponse(BaseModel):
    total: int
    by_priority: dict[str, int]
    by_type: dict[str, int]
    by_source: list[FacetSourceEntry]
    top_tags: list[FacetTagEntry]
    took_ms: int


class FilterSourceEntry(BaseModel):
    id: int | None
    name: str
    kind: str
    category: str
    status: str


class FiltersResponse(BaseModel):
    sources: list[FilterSourceEntry]
    tags: list[str]
    npa_statuses: list[str]
    priorities: list[str]
    types: list[str]
    orders: list[str]
    timezone: str


class DocumentEntry(BaseModel):
    id: int
    title: str | None
    url: str
    source_id: int
    source_name: str | None
    published_at: str | None
    chars: int | None


class DocumentsResponse(BaseModel):
    documents: list[DocumentEntry]
    total: int
    took_ms: int


class DigestResponse(BaseModel):
    title: str
    generated_at: str
    items: int
    format: str
    body: str


class StaleSourceEntry(BaseModel):
    id: int | None
    name: str
    overdue_minutes: int
    consecutive_failures: int
    last_error: str


class StatusResponse(BaseModel):
    last_collect_at: str | None
    documents: int
    items: int
    unprocessed: int
    sources: dict[str, int]
    stale_sources: list[StaleSourceEntry]
    timezone: str
