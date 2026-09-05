"""Входящие схемы HTTP-границы (pydantic).

Дальше границы они не идут: сервисы принимают и отдают dataclass'ы (принцип II).
`extra="forbid"` — опечатка в имени поля даёт 422, а не молчаливый no-op.
Словари значений (интервалы, приоритеты) проверяют сервисы, чтобы контракт
`400 validation_error` был один и для CLI, и для HTTP.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .domain import EDIT_REASONS, ITEM_TYPES, POLL_INTERVALS, PRIORITIES


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProbeRequest(_Request):
    url: str


class SourceCreateRequest(_Request):
    url: str
    title: str = ""
    type: str = ""  # словарь резолвера: rss | telegram | sitemap | html | search
    poll_interval: str = Field(default="", examples=list(POLL_INTERVALS))
    category_hint: str | None = None
    backfill_limit: int = 20
    created_by: str = ""


class SourceUpdateRequest(_Request):
    """Частичное обновление — применяются только присланные поля."""

    title: str | None = None
    poll_interval: str | None = Field(default=None, examples=list(POLL_INTERVALS))
    category_hint: str | None = None
    status: str | None = None


class ItemCreateRequest(_Request):
    url: str = ""
    title: str = ""
    raw_text: str = ""
    published_at: str | None = None
    type: str = Field(default="news", examples=list(ITEM_TYPES))
    npa_status: str | None = None
    run_llm: bool = True
    force: bool = False


class ItemUpdateRequest(_Request):
    """Правка аналитика: только присланные поля, каждое уходит в `manual_overrides`."""

    title: str | None = None
    summary: str | None = None
    type: str | None = Field(default=None, examples=list(ITEM_TYPES))
    npa_status: str | None = None
    priority: str | None = Field(default=None, examples=list(PRIORITIES))
    tags: list[str] | None = None
    edit_reason: str = Field(default="", examples=list(EDIT_REASONS))


class HideRequest(_Request):
    scope: str = "feed"  # feed | digest
    reason: str = ""


class BulkVisibilityRequest(_Request):
    item_ids: list[int]
    scope: str = "digest"
    reason: str = ""


class NoteCreateRequest(_Request):
    body: str
    author: str = ""


class RevertRequest(_Request):
    field: str


class DigestRequest(_Request):
    filters: dict = Field(default_factory=dict)
    format: str = "markdown"  # markdown | json
    title: str = ""
    include_notes: bool = False  # заметка — черновая мысль, пока её не решили отправить
