"""Fixtures shared by the tests. Everything runs offline: MockTransport for HTTP,
`FakeLLM` for the model."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone

import pytest
from support import MockRoutes, default_raw_config, read_fixture

from src.config import Config
from src.models import Cluster, Item
from src.paths import DEFAULT_PATHS, ProjectPaths
from src.processing import service as service_mod
from src.sources import manage as manage_mod
from src.sources import scheduler as scheduler_mod
from src.storage import Database
from src.storage import db as db_mod


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
    for module in (db_mod, service_mod, scheduler_mod, manage_mod):
        monkeypatch.setattr(module, "utc_now", lambda: now)
    return now


@pytest.fixture
def hub_paths(tmp_path) -> ProjectPaths:
    """A throwaway HUB_ROOT with the repo's real config.yaml copied in.

    Everything the CLI and the API touch (`data/hub.db`, `.env`) then lands under
    `tmp_path`, so tests never see the developer's database or secrets.
    """
    shutil.copy(DEFAULT_PATHS.config_path, tmp_path / "config.yaml")
    return ProjectPaths.from_root(str(tmp_path))


@pytest.fixture
def file_db(hub_paths):
    """An on-disk database under `hub_paths` — what the API opens per request."""
    database = Database(hub_paths.db_path)
    yield database
    database.close()


@pytest.fixture(autouse=True)
def no_provider_secrets(monkeypatch):
    """No test may pick up a real API key from the developer's environment."""
    for name in ("OLLAMA_API_KEY", "TAVILY_API", "TELEGRAM_API_ID", "TELEGRAM_API_HASH"):
        monkeypatch.delenv(name, raising=False)


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
