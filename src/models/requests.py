"""Входящие схемы HTTP-границы (pydantic).

Дальше границы они не идут: сервисы принимают и отдают dataclass'ы (принцип II).
`extra="forbid"` — опечатка в имени поля даёт 422, а не молчаливый no-op.
Словари значений (интервалы, приоритеты) проверяют сервисы, чтобы контракт
`400 validation_error` был один и для CLI, и для HTTP.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .domain import EDIT_REASONS, ITEM_TYPES, KINDS, NPA_STATUSES, POLL_INTERVALS, PRIORITIES


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
    """Частичное обновление — применяются только присланные поля.

    `url` / `type` / `fetch_url` переселяют источник на другой адрес: документы
    остаются, курсор сбрасывается, дубль по адресу — 409.
    """

    title: str | None = None
    poll_interval: str | None = Field(default=None, examples=list(POLL_INTERVALS))
    category_hint: str | None = None
    status: str | None = None
    url: str | None = None
    type: str | None = Field(default=None, examples=list(KINDS))
    fetch_url: str | None = None


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


class BulkTagsRequest(_Request):
    """Массовый тегинг: добавить и/или снять теги у списка карточек."""

    item_ids: list[int]
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


class BulkArchiveRequest(_Request):
    item_ids: list[int]
    archived: bool = True


class NoteCreateRequest(_Request):
    body: str
    author: str = ""


class RevertRequest(_Request):
    field: str


class MergeRequest(_Request):
    """Объединить карточки-дубли в ту, чей адрес в пути: её партнёры — `item_ids`."""

    item_ids: list[int] = Field(min_length=1)
    reason: str = ""


class NpaEventCreateRequest(_Request):
    """Событие в хронологии карточки: статус НПА из словаря двигает карточку,
    любое другое (слушания, срок) просто ложится в историю."""

    status: str = Field(min_length=1, max_length=64, examples=list(NPA_STATUSES))
    occurred_at: str | None = None
    source_url: str = ""
    note: str = ""


class ProfileSaveRequest(_Request):
    """Профиль целиком: имя — ключ, `payload` — то, что уйдёт в промпт."""

    name: str = Field(min_length=1, max_length=200)
    payload: dict


class ProcessingRunRequest(_Request):
    """Параметры прогона обработки — те же, что у `python -m src process`."""

    limit: int | None = Field(default=None, ge=1)
    source_id: int | None = None
    since: str | None = None
    profile_id: int | None = None
    force: bool = False
    only_failed: bool = False  # взять только документы, упавшие в прошлый прогон


class CollectionStartRequest(_Request):
    interval_seconds: int = 900
    date_window_hours: int | None = Field(default=None, ge=1)


class CollectionRunRequest(_Request):
    """Разовый цикл сбора: все включённые источники или только те, чья очередь пришла."""

    source_ids: list[int] | None = None
    due_only: bool = False
    date_window_hours: int | None = Field(default=None, ge=1)
    backfill: bool = False
    force: bool = False


class DigestRequest(_Request):
    filters: dict = Field(default_factory=dict)
    format: str = "markdown"  # markdown | json
    title: str = ""
    include_notes: bool = False  # заметка — черновая мысль, пока её не решили отправить
