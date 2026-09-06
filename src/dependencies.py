"""Связывание слоёв — только здесь (шаблон Model-Service-Repository).

Маршруты объявляют `service: SourceServiceDep` и никогда не пишут `Depends(...)`
сами и не собирают сервис руками. Новый сервис — новый провайдер и псевдоним здесь.

Соединение SQLite открывается на запрос и закрывается после ответа (research.md,
R-03): `Database` рассчитан на один поток, а синхронные обработчики FastAPI идут
в пуле потоков. Кэшируются только провайдеры без аргументов.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Iterator

from fastapi import Depends

from .config import Config, Settings, get_config, get_paths, get_settings
from .paths import ProjectPaths
from .processing.embeddings import build_embedder
from .processing.llm import EmbeddingProvider, OllamaProvider, build_provider
from .repositories import Database
from .services.collection_service import (
    CollectionService,
    CollectionWatcher,
    run_collection_cycle,
)
from .services.feed_service import FeedService
from .services.health import HealthService
from .services.item_service import ItemService
from .services.processing_service import ProcessingService
from .services.profile_service import ProfileService
from .services.source_service import SourceService
from .sources.collector import Collector
from .utils import load_env_secret

SettingsDep = Annotated[Settings, Depends(get_settings)]
ConfigDep = Annotated[Config, Depends(get_config)]
PathsDep = Annotated[ProjectPaths, Depends(get_paths)]


def get_database(paths: PathsDep) -> Iterator[Database]:
    """Одно соединение на запрос; закрывается после ответа."""
    db = Database(paths.db_path)
    try:
        yield db
    finally:
        db.close()


DatabaseDep = Annotated[Database, Depends(get_database)]


@lru_cache
def get_llm_provider() -> OllamaProvider | None:
    """Провайдер модели на весь процесс: один httpx-клиент на все рабочие потоки.

    `None`, когда ключа нет — карточки тогда заводятся деградированными, как и в
    CLI. Владелец — lifespan (закрывает при остановке); сервисы запроса его не
    закрывают.
    """
    config, paths = get_config(), get_paths()
    key = load_env_secret(config.llm.api_key_env, paths.env_path)
    return build_provider(config.llm, key) if key else None


@lru_cache
def get_embedder() -> EmbeddingProvider | None:
    """Провайдер эмбеддингов на весь процесс: локальная модель грузится один раз.

    `embeddings.provider: ollama` делит клиент с языковой моделью; `local` не
    требует ключа; `off` — `None`, и S1 работает по URL и SimHash. Закрывает
    lifespan вместе с провайдером модели.
    """
    config, paths = get_config(), get_paths()
    key = load_env_secret(config.llm.api_key_env, paths.env_path)
    shared = get_llm_provider() if config.embeddings.provider == "ollama" else None
    return build_embedder(config.embeddings, config.llm, key, shared=shared)


def get_tavily_key(config: ConfigDep, paths: PathsDep) -> str:
    return load_env_secret(config.tavily.api_key_env, paths.env_path)


@lru_cache
def get_collection_watcher() -> CollectionWatcher:
    """Наблюдатель сбора на весь процесс: один поток, состояние «запущен» — свойство процесса."""
    config, paths = get_config(), get_paths()
    key = load_env_secret(config.tavily.api_key_env, paths.env_path)
    return CollectionWatcher(
        lambda: run_collection_cycle(config, paths, tavily_key=key, due_only=True)
    )


LLMProviderDep = Annotated[OllamaProvider | None, Depends(get_llm_provider)]
EmbedderDep = Annotated[EmbeddingProvider | None, Depends(get_embedder)]
TavilyKeyDep = Annotated[str, Depends(get_tavily_key)]
CollectionWatcherDep = Annotated[CollectionWatcher, Depends(get_collection_watcher)]


def get_collector(config: ConfigDep, paths: PathsDep, db: DatabaseDep, tavily_key: TavilyKeyDep) -> Collector:
    """Коллектор на соединении запроса — для синхронного `POST /sources/{id}/refresh`."""
    return Collector(config, paths, db, tavily_key=tavily_key or None)


def get_source_service(config: ConfigDep, db: DatabaseDep) -> SourceService:
    return SourceService(config, db)


def get_item_service(config: ConfigDep, db: DatabaseDep) -> ItemService:
    return ItemService(config, db)


def get_processing_service(
    config: ConfigDep, db: DatabaseDep, provider: LLMProviderDep, embedder: EmbedderDep
) -> ProcessingService:
    return ProcessingService(config, db, provider=provider, embedder=embedder)


def get_feed_service(config: ConfigDep, db: DatabaseDep) -> FeedService:
    return FeedService(config, db)


def get_profile_service(config: ConfigDep, db: DatabaseDep) -> ProfileService:
    return ProfileService(config, db)


def get_health_service(settings: SettingsDep, db: DatabaseDep) -> HealthService:
    return HealthService(settings, db)


def get_collection_service(
    config: ConfigDep, db: DatabaseDep, watcher: CollectionWatcherDep
) -> CollectionService:
    return CollectionService(config, db, watcher)


CollectorDep = Annotated[Collector, Depends(get_collector)]
SourceServiceDep = Annotated[SourceService, Depends(get_source_service)]
ItemServiceDep = Annotated[ItemService, Depends(get_item_service)]
ProcessingServiceDep = Annotated[ProcessingService, Depends(get_processing_service)]
FeedServiceDep = Annotated[FeedService, Depends(get_feed_service)]
ProfileServiceDep = Annotated[ProfileService, Depends(get_profile_service)]
HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]
CollectionServiceDep = Annotated[CollectionService, Depends(get_collection_service)]
