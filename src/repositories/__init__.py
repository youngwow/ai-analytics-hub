"""Репозитории — «Repository» из Model-Service-Repository.

`Database` держит соединение SQLite и раздаёт репозитории атрибутами
(`db.sources`, `db.items`, …); контракты для сервисов — в `repository_interface`.
"""

from .database import MANUAL_FETCH_URL, MANUAL_SOURCE_NAME, Database
from .documents import SqliteDocumentRepository
from .feed import SqliteFeedRepository
from .items import ClusterRepo, ItemNoteRepo, ItemTagRepo, SearchRepo, SqliteItemRepository
from .processing import LlmCallRepo, ProcessingRunRepo, ProfileRepo, PromptRepo, RunRepo
from .repository_interface import (
    DocumentRepository,
    DuplicateSourceError,
    FeedRepository,
    ItemRepository,
    SourceRepository,
)
from .sources import FetchStateRepo, SeenUrlRepo, SourceRunRepo, SqliteSourceRepository

__all__ = [
    "MANUAL_FETCH_URL",
    "MANUAL_SOURCE_NAME",
    "ClusterRepo",
    "Database",
    "DocumentRepository",
    "DuplicateSourceError",
    "FeedRepository",
    "FetchStateRepo",
    "ItemNoteRepo",
    "ItemRepository",
    "ItemTagRepo",
    "LlmCallRepo",
    "ProcessingRunRepo",
    "ProfileRepo",
    "PromptRepo",
    "RunRepo",
    "SearchRepo",
    "SeenUrlRepo",
    "SourceRepository",
    "SourceRunRepo",
    "SqliteDocumentRepository",
    "SqliteFeedRepository",
    "SqliteItemRepository",
    "SqliteSourceRepository",
]
