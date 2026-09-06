"""Контракты репозиториев — то, что сервисы HTTP-слоя знают о хранилище.

Абстрактные классы объявлены только для агрегатов, которыми пользуются сервисы
(источники, документы, карточки, чтение ленты): остальные репозитории живут
внутри сбора и обработки и никогда не подменяются. Сигнатуры — синхронные, как и
весь слой (research.md, R-03).
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from typing import Iterable

from ..models import (
    EntitySpan,
    Item,
    ItemRevision,
    NpaEvent,
    RawDocument,
    Source,
)
from ..models.queries import DocumentQuery, FeedQuery


class DuplicateSourceError(Exception):
    """A source with the same fetch_url already exists."""

    def __init__(self, existing: Source):
        super().__init__(
            f"source already exists: #{existing.id} {existing.name} ({existing.fetch_url})"
        )
        self.existing = existing


class SourceRepository(ABC):
    """Источники: пул, статусы, расписание."""

    @abstractmethod
    def add(self, source: Source) -> Source:
        """См. реализацию SQLite."""

    @abstractmethod
    def update(self, source: Source) -> None:
        """См. реализацию SQLite."""

    @abstractmethod
    def get(self, source_id: int) -> Source | None:
        """См. реализацию SQLite."""

    @abstractmethod
    def get_by_fetch_url(self, fetch_url: str) -> Source | None:
        """Удалённый источник не занимает адрес: его можно завести заново (US-11)."""

    @abstractmethod
    def list(
        self,
        enabled_only: bool = False,
        *,
        status: str | None = None,
        kind: str | None = None,
        include_deleted: bool = False,
    ) -> list[Source]:
        """См. реализацию SQLite."""

    @abstractmethod
    def get_by_normalized(self, normalized_url: str) -> Source | None:
        """Duplicate check: three spellings of one address share this key."""

    @abstractmethod
    def set_status(self, source_id: int, status: str) -> bool:
        """См. реализацию SQLite."""

    @abstractmethod
    def due(self, now: str, limit: int = 100) -> list[Source]:
        """Sources whose turn has come — the whole scheduler in one query."""

    @abstractmethod
    def schedule(self, source_id: int, next_run_at: str) -> None:
        """См. реализацию SQLite."""

    @abstractmethod
    def remove(self, source_id: int) -> bool:
        """Soft delete (US-11): documents and cards stay, the source stops being polled."""

    @abstractmethod
    def ensure_manual(self) -> Source:
        """The built-in sink for `import-url` and hand-entered items."""


class DocumentRepository(ABC):
    """Собранные документы и их производные (SimHash, эмбеддинг, нормализованный текст)."""

    @abstractmethod
    def exists(self, source_id: int, external_id: str) -> bool:
        """См. реализацию SQLite."""

    @abstractmethod
    def find_by_url(self, url: str) -> int | None:
        """См. реализацию SQLite."""

    @abstractmethod
    def insert(self, doc: RawDocument) -> int:
        """Insert one document; the caller owns the transaction."""

    @abstractmethod
    def get(self, doc_id: int) -> RawDocument | None:
        """См. реализацию SQLite."""

    @abstractmethod
    def list(
        self, source_id: int | None = None, limit: int = 20, include_hidden: bool = False
    ) -> list[sqlite3.Row]:
        """Newest first (NULL dates last). Rows carry `source_name` and `text_len` for display."""

    @abstractmethod
    def count(self, source_id: int | None = None) -> int:
        """См. реализацию SQLite."""

    @abstractmethod
    def unprocessed(
        self,
        limit: int = 200,
        source_id: int | None = None,
        since: str | None = None,
        force: bool = False,
    ) -> list[sqlite3.Row]:
        """Documents that have no card yet (with --force: everything in the window)."""

    @abstractmethod
    def set_derived(
        self, doc_id: int, *, simhash: str = "", embedding: bytes | None = None, norm_text: str = ""
    ) -> None:
        """Store what S0/S1 computed; the caller owns the transaction."""

    @abstractmethod
    def clustered_candidates(self, since: str | None = None, limit: int = 2000) -> list[sqlite3.Row]:
        """Already-carded documents a new one could join — the dedup blocking pool."""

    @abstractmethod
    def count_unprocessed(self) -> int:
        """Сколько собранных документов ещё не получили карточку."""


class ItemRepository(ABC):
    """Карточки: чтение, видимость, сущности, связи с документами, история."""

    @abstractmethod
    def add(self, item: Item) -> int:
        """См. реализацию SQLite."""

    @abstractmethod
    def update(self, item: Item) -> None:
        """См. реализацию SQLite."""

    @abstractmethod
    def get(self, item_id: int) -> Item | None:
        """См. реализацию SQLite."""

    @abstractmethod
    def by_cluster(self, cluster_id: int) -> Item | None:
        """См. реализацию SQLite."""

    @abstractmethod
    def by_npa_key(self, npa_key: str) -> Item | None:
        """The live card for an act — the join point for every later publication about it."""

    @abstractmethod
    def list(
        self,
        *,
        type_: str | None = None,
        priority: str | None = None,
        tag: str | None = None,
        since: str | None = None,
        limit: int = 20,
        include_hidden: bool = False,
    ) -> list[sqlite3.Row]:
        """Feed rows, newest first; `query` goes through items_fts."""

    @abstractmethod
    def count(self) -> int:
        """См. реализацию SQLite."""

    @abstractmethod
    def set_visibility(self, item_id: int, visibility: str, reason: str = "") -> bool:
        """См. реализацию SQLite."""

    @abstractmethod
    def set_archived(self, item_id: int, archived: bool) -> bool:
        """Перевести карточку в архив или вернуть из него; `True`, если строка нашлась."""

    @abstractmethod
    def set_visibility_bulk(self, item_ids: Iterable[int], visibility: str, reason: str = "") -> int:
        """Массовая операция под дайджест: одна транзакция, отменяется целиком."""

    @abstractmethod
    def hide_by_source(self, source_id: int) -> int:
        """`--purge-items`: карточки источника уходят из ленты, но остаются в базе."""

    @abstractmethod
    def add_entities(self, item_id: int, spans: Iterable[EntitySpan]) -> None:
        """См. реализацию SQLite."""

    @abstractmethod
    def entities(self, item_id: int) -> list[EntitySpan]:
        """См. реализацию SQLite."""

    @abstractmethod
    def clear_entities(self, item_id: int) -> None:
        """См. реализацию SQLite."""

    @abstractmethod
    def link_sources(
        self, item_id: int, document_ids: Iterable[int], canonical_id: int | None = None
    ) -> int:
        """Attach documents to a card; returns how many links are new (0 on a repeat)."""

    @abstractmethod
    def item_for_document(self, document_id: int) -> int | None:
        """См. реализацию SQLite."""

    @abstractmethod
    def sources(self, item_id: int) -> list[sqlite3.Row]:
        """См. реализацию SQLite."""

    @abstractmethod
    def add_event(self, event: NpaEvent) -> int:
        """См. реализацию SQLite."""

    @abstractmethod
    def events(self, item_id: int) -> list[NpaEvent]:
        """См. реализацию SQLite."""

    @abstractmethod
    def has_event(self, item_id: int, status: str) -> bool:
        """См. реализацию SQLite."""

    @abstractmethod
    def add_revision(self, revision: ItemRevision) -> int:
        """См. реализацию SQLite."""

    @abstractmethod
    def revisions(self, item_id: int) -> list[ItemRevision]:
        """См. реализацию SQLite."""

    @abstractmethod
    def last_model_value(self, item_id: int, field: str) -> ItemRevision | None:
        """The newest value the model proposed for a field — what `revert` restores."""

    @abstractmethod
    def last_revision(self, item_id: int, field: str) -> ItemRevision | None:
        """Последняя ревизия поля любого происхождения."""

    @abstractmethod
    def clustering_pool(
        self, since: str | None, limit: int, include_ids: Iterable[int] = ()
    ) -> list[sqlite3.Row]:
        """Новости окна плюс указанные карточки — пул второй дедупликации."""

    @abstractmethod
    def move_sources(self, from_item: int, to_item: int) -> int:
        """Перевесить публикации на другую карточку; вернуть число новых связей."""

    @abstractmethod
    def mark_merged(self, item_id: int, target_id: int) -> None:
        """Поглощённая карточка: `deleted` с причиной «объединена с #N»."""

    @abstractmethod
    def edited_share(self, since: str | None = None) -> float:
        """Share of cards an analyst touched — the honest proxy for model quality."""


class FeedRepository(ABC):
    """Читающие запросы ленты. Только чтение: ни одной записи, ни одного вызова модели."""

    @abstractmethod
    def page(self, query: FeedQuery, position: tuple | None = None) -> list[dict]:
        """Строки ленты по срезу: `limit + 1` штук, чтобы понять, есть ли следующая страница."""

    @abstractmethod
    def count(self, query: FeedQuery) -> int:
        """Сколько всего карточек подходит под срез."""

    @abstractmethod
    def facet(self, query: FeedQuery, column: str) -> dict[str, int]:
        """Счётчики по колонке карточки для того же среза."""

    @abstractmethod
    def facet_sources(self, query: FeedQuery, limit: int = 20) -> list[dict]:
        """Топ источников среза: `{source_id, name, count}`."""

    @abstractmethod
    def facet_tags(self, query: FeedQuery, limit: int = 15) -> list[dict]:
        """Топ тегов среза: `{tag, count}`."""

    @abstractmethod
    def tags_in_use(self) -> list[str]:
        """Теги, которые реально стоят на карточках, по убыванию частоты."""

    @abstractmethod
    def documents(
        self, query: DocumentQuery, position: tuple | None = None
    ) -> tuple[list[dict], int]:
        """Собрано, но без карточки: страница (`limit + 1` строк от курсора) и общее число."""

    @abstractmethod
    def unprocessed_count(self) -> int:
        """Сколько документов ещё не получили карточку."""

    @abstractmethod
    def sources_by_status(self) -> dict[str, int]:
        """Число источников в каждом статусе."""
