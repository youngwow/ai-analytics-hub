"""Pydantic-модели HTTP-границы.

Дальше границы они не идут: сервисы принимают и отдают dataclass'ы (принцип II).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..models import EDIT_REASONS, ITEM_TYPES, POLL_INTERVALS, PRIORITIES


class ProbeRequest(BaseModel):
    url: str


class SourceCreate(BaseModel):
    url: str
    title: str = ""
    type: str = ""  # словарь резолвера: rss | telegram | sitemap | html | search
    poll_interval: str = ""
    category_hint: str | None = None
    backfill_limit: int = 20
    created_by: str = ""


class SourceUpdate(BaseModel):
    title: str | None = None
    poll_interval: str | None = Field(default=None, examples=list(POLL_INTERVALS))
    category_hint: str | None = None
    status: str | None = None


class ItemPatch(BaseModel):
    title: str | None = None
    summary: str | None = None
    type: str | None = Field(default=None, examples=list(ITEM_TYPES))
    npa_status: str | None = None
    priority: str | None = Field(default=None, examples=list(PRIORITIES))
    tags: list[str] | None = None
    edit_reason: str = Field(default="", examples=list(EDIT_REASONS))


class ItemCreate(BaseModel):
    url: str = ""
    title: str = ""
    raw_text: str = ""
    published_at: str | None = None
    type: str = "news"
    npa_status: str | None = None
    run_llm: bool = True
    force: bool = False


class HideRequest(BaseModel):
    scope: str = "feed"  # feed | digest
    reason: str = ""


class BulkRequest(BaseModel):
    item_ids: list[int]
    scope: str = "digest"
    reason: str = ""


class NoteRequest(BaseModel):
    body: str
    author: str = ""


class RevertRequest(BaseModel):
    field: str


class DigestRequest(BaseModel):
    filters: dict = Field(default_factory=dict)
    format: str = "markdown"  # markdown | json
    title: str = ""
    include_notes: bool = False  # заметка — черновая мысль, пока её не решили отправить
