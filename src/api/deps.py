"""Зависимости запроса: соединение с БД и сервисы.

SQLite-соединение открывается на запрос и закрывается после ответа: `Database`
из 1.1 рассчитан на один поток, а синхронные обработчики FastAPI выполняются в
пуле потоков (research.md, R-03). Открытие стоит микросекунды, WAL включён с 1.1.
"""

from __future__ import annotations

from typing import Iterator

from ..common import load_env_secret
from ..config import Config
from ..paths import DEFAULT_PATHS, ProjectPaths
from ..processing.llm import build_provider
from ..processing.service import ProcessingService
from ..sources.manage import SourceService
from ..storage import Database


class Context:
    """Конфигурация и пути живут столько же, сколько приложение."""

    def __init__(self, config: Config | None = None, paths: ProjectPaths | None = None):
        self.paths = paths or DEFAULT_PATHS
        self.config = config or Config.load(self.paths.config_path)

    def database(self) -> Iterator[Database]:
        db = Database(self.paths.db_path)
        try:
            yield db
        finally:
            db.close()

    def sources(self, db: Database) -> SourceService:
        return SourceService(self.config, db)

    def processing(self, db: Database) -> ProcessingService:
        key = load_env_secret(self.config.llm.api_key_env, self.paths.env_path)
        provider = build_provider(self.config.llm, key) if key else None
        return ProcessingService(self.config, db, provider=provider, embedder=provider)
