"""POST /api/v1/sources/{id}/refresh и `run_backfill` — опрос источника вне расписания.

Сеть подменена `MockRoutes`: коллектор запроса собирается через
`dependency_overrides[get_collector]` (переопределение само объявляет
зависимости), фоновый сбор — подменой `Collector` в модуле сервиса.
"""

from __future__ import annotations

import logging

import pytest
from support import RSS, MockRoutes, raising, rss_bytes

from src.api.routes import sources as sources_routes
from src.config import get_config, get_paths
from src.dependencies import ConfigDep, DatabaseDep, PathsDep, get_collector
from src.exceptions import SourceNotFoundError, SourceValidationError
from src.models import Source, SourceRun
from src.paths import ProjectPaths
from src.repositories import Database
from src.services import source_service
from src.services.source_service import SourceService, run_backfill
from src.sources.base import make_client
from src.sources.collector import Collector

SITE_URL = "https://feed.example.ru/"
RSS_URL = "https://feed.example.ru/rss.xml"
ITEMS = [
    {
        "title": "Минцифры внесло законопроект",
        "link": "https://feed.example.ru/news/1",
        "pubDate": "Tue, 02 Sep 2026 09:00:00 +0300",
    }
]


@pytest.fixture
def routes() -> MockRoutes:
    return MockRoutes({RSS_URL: (200, rss_bytes(ITEMS, title="Отраслевые новости"), RSS)})


@pytest.fixture
def source(file_db) -> Source:
    return file_db.sources.add(Source(name="Лента", url=SITE_URL, kind="rss", fetch_url=RSS_URL))


@pytest.fixture
def offline_collector(app, routes, now) -> MockRoutes:
    """`get_collector` подменён: тот же `Collector`, но транспорт — мок, часы — фикстура."""

    def fake_collector(config: ConfigDep, paths: PathsDep, db: DatabaseDep) -> Collector:
        return Collector(
            config, paths, db, transport=routes.transport(), now=lambda: now, tavily_key=""
        )

    app.dependency_overrides[get_collector] = fake_collector
    return routes


class NeverCollector:
    def collect_one(self, source, *, force=True):
        raise AssertionError("коллектор не должен вызываться")


# ── HTTP ───────────────────────────────────────────────────────────────────


def test_refresh_polls_the_source_once_and_answers_with_the_run(
    client, file_db, source, offline_collector
):
    response = client.post(f"/api/v1/sources/{source.id}/refresh")

    assert response.status_code == 200
    body = response.json()
    assert (body["source_id"], body["items_found"], body["items_new"], body["error_code"]) == (
        source.id, 1, 1, "",
    )
    assert body["started_at"] == "2026-09-02T12:00:00+00:00"  # часы коллектора — фикстура `now`
    assert body["finished_at"] is not None  # прогон закрыт; штамп ставит репозиторий своими часами
    assert offline_collector.urls().count(RSS_URL) == 1


def test_refresh_writes_the_documents_the_feed_carried(client, file_db, source, offline_collector):
    client.post(f"/api/v1/sources/{source.id}/refresh")

    rows = file_db.documents.list(source.id)

    assert [(row["title"], row["url"]) for row in rows] == [
        ("Минцифры внесло законопроект", "https://feed.example.ru/news/1")
    ]
    assert file_db.fetch_state.get(source.id).consecutive_failures == 0


def test_a_second_refresh_finds_the_item_again_but_stores_nothing_new(
    client, file_db, source, offline_collector
):
    client.post(f"/api/v1/sources/{source.id}/refresh")

    second = client.post(f"/api/v1/sources/{source.id}/refresh").json()

    assert (second["items_found"], second["items_new"]) == (1, 0)
    assert file_db.documents.count(source.id) == 1
    assert len(file_db.source_runs.history(source.id, 10)) == 2


def test_a_failed_poll_is_still_a_run_record_not_an_http_error(client, source, offline_collector):
    offline_collector[RSS_URL] = raising(TimeoutError("read timed out"))

    response = client.post(f"/api/v1/sources/{source.id}/refresh")

    assert response.status_code == 200
    body = response.json()
    assert (body["items_found"], body["items_new"], body["error_code"]) == (0, 0, "timeout")
    assert "read timed out" in body["error_message"]


def test_refresh_of_an_unknown_source_is_404(client, offline_collector):
    response = client.post("/api/v1/sources/999/refresh")

    assert (response.status_code, response.json()["code"]) == (404, "source_not_found")
    assert offline_collector.requests == []


def test_refresh_of_a_deleted_source_is_refused(client, file_db, source, offline_collector):
    file_db.sources.remove(source.id)

    response = client.post(f"/api/v1/sources/{source.id}/refresh")

    assert (response.status_code, response.json()["code"]) == (400, "validation_error")
    assert "restore" in response.json()["detail"]
    assert offline_collector.requests == []


# ── сервис ─────────────────────────────────────────────────────────────────


def test_service_refresh_returns_the_newest_run(config, db, routes, now, tmp_path):
    source = db.sources.add(Source(name="Лента", url=SITE_URL, kind="rss", fetch_url=RSS_URL))
    collector = Collector(
        config, ProjectPaths.from_root(str(tmp_path)), db, transport=routes.transport(),
        now=lambda: now, tavily_key="",
    )

    run = SourceService(config, db).refresh(source.id, collector)

    assert isinstance(run, SourceRun)
    assert (run.source_id, run.items_found, run.items_new) == (source.id, 1, 1)
    assert db.source_runs.history(source.id, 1) == [run]


def test_service_refresh_of_an_unknown_source_raises(config, db):
    with pytest.raises(SourceNotFoundError, match="#42 не найден"):
        SourceService(config, db).refresh(42, NeverCollector())


def test_service_refresh_of_a_deleted_source_raises(config, db):
    source = db.sources.add(Source(name="Лента", url=SITE_URL, kind="rss", fetch_url=RSS_URL))
    db.sources.remove(source.id)

    with pytest.raises(SourceValidationError, match="удалён"):
        SourceService(config, db).refresh(source.id, NeverCollector())


# ── фоновый первичный сбор ─────────────────────────────────────────────────


@pytest.fixture
def backfill_collector(monkeypatch, routes, now) -> list[dict]:
    """`Collector` в модуле сервиса: тот же класс на мок-транспорте; возвращает его kwargs."""
    seen_kwargs: list[dict] = []

    class OfflineCollector(Collector):
        def __init__(self, config, paths, db, **kwargs):
            seen_kwargs.append(dict(kwargs))
            super().__init__(
                config, paths, db, transport=routes.transport(), now=lambda: now, **kwargs
            )

    monkeypatch.setattr(source_service, "Collector", OfflineCollector)
    return seen_kwargs


def test_run_backfill_collects_the_source_once_on_its_own_connection(
    config, hub_paths, file_db, source, backfill_collector
):
    run_backfill(config, hub_paths, source.id, tavily_key="")

    assert file_db.documents.count(source.id) == 1
    runs = file_db.source_runs.history(source.id, 10)
    assert [(r.items_found, r.items_new, r.error_code) for r in runs] == [(1, 1, "")]
    assert backfill_collector == [{"tavily_key": None}]


def test_run_backfill_hands_the_tavily_key_to_the_collector(
    config, hub_paths, file_db, source, backfill_collector
):
    run_backfill(config, hub_paths, source.id, tavily_key="tvly-secret")

    assert backfill_collector == [{"tavily_key": "tvly-secret"}]


def test_run_backfill_of_an_unknown_source_collects_nothing(
    config, hub_paths, file_db, backfill_collector
):
    run_backfill(config, hub_paths, 999)

    assert backfill_collector == []


def test_run_backfill_swallows_a_failure_into_a_warning_and_closes_the_database(
    config, hub_paths, file_db, source, monkeypatch, caplog
):
    closed: list[str] = []

    class ClosingDatabase(Database):
        def close(self) -> None:
            closed.append(self.path)
            super().close()

    class BrokenCollector:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("нет сети")

    monkeypatch.setattr(source_service, "Database", ClosingDatabase)
    monkeypatch.setattr(source_service, "Collector", BrokenCollector)

    with caplog.at_level(logging.WARNING, logger="sources"):
        run_backfill(config, hub_paths, source.id)

    assert closed == [hub_paths.db_path]
    assert f"первичный сбор источника #{source.id} не удался: нет сети" in caplog.text
    assert file_db.source_runs.history(source.id, 10) == []


# ── POST /sources ставит сбор в фон ────────────────────────────────────────


@pytest.fixture
def resolvable_feed(monkeypatch, routes) -> MockRoutes:
    """`SourceService.create` распознаёт ссылку через сеть — здесь она мок."""
    monkeypatch.setattr(
        source_service,
        "make_client",
        lambda cfg, transport=None: make_client(cfg, routes.transport()),
    )
    return routes


def test_creating_a_source_schedules_the_backfill_after_the_response(
    client, monkeypatch, resolvable_feed
):
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        sources_routes, "run_backfill", lambda *args, **kwargs: calls.append((args, kwargs))
    )

    response = client.post("/api/v1/sources", json={"url": RSS_URL, "backfill_limit": 5})

    assert response.status_code == 201
    source_id = response.json()["id"]
    assert len(calls) == 1
    (config, paths, scheduled_id), kwargs = calls[0]
    assert config is get_config()
    assert paths == get_paths()
    assert (scheduled_id, kwargs) == (source_id, {"tavily_key": ""})


def test_a_zero_backfill_limit_schedules_nothing(client, monkeypatch, resolvable_feed):
    calls: list = []
    monkeypatch.setattr(sources_routes, "run_backfill", lambda *args, **kwargs: calls.append(args))

    response = client.post("/api/v1/sources", json={"url": RSS_URL, "backfill_limit": 0})

    assert response.status_code == 201
    assert response.json()["kind"] == "rss"
    assert calls == []
