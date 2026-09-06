"""Fixtures shared by the tests. Everything runs offline: MockTransport for HTTP,
`FakeLLM` for the model, and a throwaway `HUB_ROOT` for the application."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import yaml
from fastapi.testclient import TestClient
from support import FakeLLM, MockRoutes, default_raw_config, news_answer, read_fixture

from src.config import Config, get_config, get_paths, get_settings
from src.dependencies import get_collection_watcher, get_embedder, get_llm_provider
from src.main import create_app
from src.models import Cluster, Item
from src.paths import ProjectPaths
from src.repositories import Database
from src.repositories import database as database_mod
from src.repositories import documents as documents_mod
from src.repositories import items as items_mod
from src.repositories import processing as processing_repo_mod
from src.repositories import sources as sources_mod
from src.services import collection_service as collection_mod
from src.services import item_service as item_mod
from src.services import processing_service as processing_mod
from src.services import source_service as source_mod
from src.sources import scheduler as scheduler_mod

# The no-argument providers behind `create_app()`; every test starts and ends
# with them empty so no test sees another test's root or config.
CACHED_PROVIDERS = (
    get_settings,
    get_paths,
    get_config,
    get_llm_provider,
    get_embedder,
    get_collection_watcher,
)

# Every module that stamps a stored row with `utc_now()` imported the name, so
# each one is pinned separately by `frozen_clock`.
CLOCK_MODULES = (
    database_mod,
    sources_mod,
    documents_mod,
    items_mod,
    processing_repo_mod,
    processing_mod,
    item_mod,
    source_mod,
    collection_mod,
    scheduler_mod,
)


def clear_provider_caches() -> None:
    for provider in CACHED_PROVIDERS:
        provider.cache_clear()


def stop_leaked_watcher() -> None:
    """A watcher thread a test started must not outlive it.

    Only the cached instance is looked at: building one just to stop it would
    read the config of a root the test may not have prepared.
    """
    if get_collection_watcher.cache_info().currsize:
        watcher = get_collection_watcher()
        if watcher.running:
            watcher.stop(timeout=1.0)


@pytest.fixture(autouse=True)
def _isolated_hub_root(tmp_path, monkeypatch):
    """`HUB_ROOT` points at `tmp_path` for the whole test.

    `get_settings()` reads the root from the environment before it opens `.env`,
    so the developer's own `.env`, database and config.yaml are never touched.
    """
    monkeypatch.setenv("HUB_ROOT", str(tmp_path))
    clear_provider_caches()
    yield
    stop_leaked_watcher()
    clear_provider_caches()


@pytest.fixture(autouse=True)
def no_provider_secrets(monkeypatch):
    """No test may pick up a real API key from the developer's environment."""
    for name in ("OLLAMA_API_KEY", "TAVILY_API", "TELEGRAM_API_ID", "TELEGRAM_API_HASH"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def raw_config() -> dict:
    """A fresh config dict per test, so tests can tweak a key before building `Config`."""
    return default_raw_config()


@pytest.fixture
def config(raw_config) -> Config:
    return Config.from_dict(raw_config)


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def now() -> datetime:
    """The injected clock: 2026-09-02T12:00:00Z, the day the fixtures were captured."""
    return datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def frozen_clock(monkeypatch, now):
    """Freeze every clock a stored row can be stamped with.

    `processed_at`, `created_at`, `next_run_at` and the dedup candidate window all
    come from `utc_now()`; each module imported the name, so each one is pinned
    separately. Nothing in the tests may read the wall clock.
    """
    for module in CLOCK_MODULES:
        monkeypatch.setattr(module, "utc_now", lambda: now)
    return now


@pytest.fixture
def hub_paths(tmp_path, raw_config) -> ProjectPaths:
    """A throwaway HUB_ROOT whose config.yaml *is* the test config.

    `create_app()` takes no arguments and loads `<HUB_ROOT>/config.yaml` itself,
    so the on-disk file must carry `raw_config`. Everything the CLI and the API
    touch (`data/hub.db`, `.env`) then lands under `tmp_path`.
    """
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(raw_config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    paths = ProjectPaths.from_root(str(tmp_path))
    assert paths == get_paths(), "the application must resolve the same root as the test"
    return paths


@pytest.fixture
def file_db(hub_paths):
    """An on-disk database under `hub_paths` — what the API opens per request."""
    database = Database(hub_paths.db_path)
    yield database
    database.close()


@pytest.fixture
def fake_llm() -> FakeLLM:
    """The model the application sees: one canned news answer, a call log."""
    return FakeLLM(news_answer())


@pytest.fixture
def app(hub_paths, fake_llm):
    """The application on the temporary root with the model provider overridden."""
    application = create_app()
    application.dependency_overrides[get_llm_provider] = lambda: fake_llm
    return application


@pytest.fixture
def client(app):
    """`TestClient` as a context manager: the lifespan runs (schema migrated once)."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def fixture_bytes():
    """`fixture_bytes("name")` → raw bytes of tests/fixtures/<name>."""
    return read_fixture


@pytest.fixture
def mock_client():
    """`mock_client(routes)` → `httpx.Client` over `MockTransport`; closed at teardown.

    `routes` is a `MockRoutes` (keeps the request log) or a plain
    {url: (status, body, headers)} dict.
    """
    clients = []

    def factory(routes, **kwargs):
        table = routes if isinstance(routes, MockRoutes) else MockRoutes(routes)
        client = table.client(**kwargs)
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.close()


@pytest.fixture
def item_factory():
    """`item_factory(db, document_id, **overrides)` → id карточки поверх документа.

    Кластер, карточка и связь `item_sources` — минимум, на котором проверяются
    видимость, мягкое удаление и правки.
    """

    def make(db, document_id: int, **overrides) -> int:
        now = overrides.pop("processed_at", "2026-09-02T12:00:00+00:00")
        cluster_id = db.clusters.add(
            Cluster(canonical_document_id=document_id, created_at=now)
        )
        item_id = db.items.add(Item(cluster_id=cluster_id, processed_at=now, **overrides))
        db.items.link_sources(item_id, [document_id], document_id)
        db.conn.commit()
        return item_id

    return make
