"""Fixtures shared by the tests. Everything runs offline: MockTransport for HTTP,
`FakeLLM` for the model."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from support import MockRoutes, default_raw_config, read_fixture

from src.config import Config
from src.processing import service as service_mod
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
    """Freeze the clock the processing layer stamps rows with.

    `processed_at`, `created_at` and the dedup candidate window all come from
    `utc_now()`; pinning it keeps timestamps assertable and the seven-day window
    fixed relative to the documents the tests insert.
    """
    for module in (db_mod, service_mod):
        monkeypatch.setattr(module, "utc_now", lambda: now)
    return now


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
