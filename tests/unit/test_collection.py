"""src/services/collection_service.py — автоматический мониторинг из процесса API.

Наблюдатель проверяется на поддельном `run_cycle`, который умеет считать вызовы,
падать и держать цикл открытым по сигналу — так состояние «занят» наблюдается
без гонок и без `sleep`. Разовый цикл идёт через настоящий `Collector` поверх
`MockRoutes`; часы у обоих — фикстура `now`.
"""

from __future__ import annotations

import logging
import threading

import pytest
from fastapi.testclient import TestClient
from support import RSS, MockRoutes, rss_bytes

from src.api.routes import collection as collection_routes
from src.config import get_config, get_paths
from src.dependencies import get_collection_watcher
from src.exceptions import CollectionBusyError, CollectionError
from src.models import CollectReport, Source
from src.repositories import Database
from src.services import collection_service as collection_mod
from src.services.collection_service import (
    DEFAULT_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
    CollectionService,
    CollectionWatcher,
    cycle_in_progress,
    run_collection_cycle,
    run_collection_task,
)
from src.sources.collector import Collector

NOW = "2026-09-02T12:00:00+00:00"
PROBLEM = "application/problem+json"
RSS_URL = "https://feed.example.ru/rss.xml"
ITEMS = [
    {
        "title": "Минцифры внесло законопроект",
        "link": "https://feed.example.ru/news/1",
        "pubDate": "Tue, 02 Sep 2026 09:00:00 +0300",
    }
]
IDLE_STATUS = {
    "running": False,
    "busy": False,
    "interval_seconds": DEFAULT_INTERVAL_SECONDS,
    "started_at": None,
    "next_tick_at": None,
    "cycles": 0,
    "last_error": "",
}


class FakeCycle:
    """`run_cycle` под наблюдатель: считает вызовы, умеет падать и ждать сигнала.

    `entered` взводится, как только цикл начался; пока `release` сброшен, цикл
    держится открытым — так тест наблюдает состояние «занят» без гонок.
    """

    def __init__(self):
        self.calls = 0
        self.error: Exception | None = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def __call__(self) -> CollectReport:
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=2)
        if self.error is not None:
            raise self.error
        return CollectReport(started_at=NOW, finished_at=NOW, sources_ok=1, docs_new=2)


def _alive_watcher_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "collection-watcher" and t.is_alive()]


def _source(db: Database, fetch_url: str = RSS_URL, **overrides) -> Source:
    base = dict(
        name="Лента", url="https://feed.example.ru/", kind="rss", category="media",
        fetch_url=fetch_url,
    )
    return db.sources.add(Source(**{**base, **overrides}))


@pytest.fixture
def cycle() -> FakeCycle:
    return FakeCycle()


@pytest.fixture
def watcher(cycle, frozen_clock) -> CollectionWatcher:
    instance = CollectionWatcher(cycle)
    yield instance
    cycle.release.set()
    if instance.running:
        instance.stop(timeout=1.0)


@pytest.fixture
def offline_collector(monkeypatch, now) -> MockRoutes:
    """Настоящий `Collector`, но транспорт — мок, а часы — фикстура `now`."""
    routes = MockRoutes({RSS_URL: (200, rss_bytes(ITEMS, title="Отраслевые новости"), RSS)})

    def build(config, paths, db, **kwargs) -> Collector:
        return Collector(config, paths, db, transport=routes.transport(), now=lambda: now, **kwargs)

    monkeypatch.setattr(collection_mod, "Collector", build)
    return routes


# ── наблюдатель: запуск и остановка ────────────────────────────────────────


def test_a_fresh_watcher_is_stopped_and_idle(watcher):
    assert watcher.running is False
    assert watcher.status() == IDLE_STATUS


@pytest.mark.parametrize("interval", [0, MIN_INTERVAL_SECONDS - 1, -60], ids=["zero", "59", "negative"])
def test_start_below_the_minimum_interval_is_a_validation_error(watcher, interval):
    with pytest.raises(CollectionError, match=str(MIN_INTERVAL_SECONDS)) as excinfo:
        watcher.start(interval)

    assert (excinfo.value.code, excinfo.value.status_code) == ("validation_error", 400)
    assert watcher.running is False
    assert _alive_watcher_threads() == []


def test_start_at_the_minimum_interval_launches_the_thread(watcher):
    watcher.start(MIN_INTERVAL_SECONDS)

    status = watcher.status()
    assert watcher.running is True
    assert (status["running"], status["interval_seconds"], status["started_at"]) == (
        True, MIN_INTERVAL_SECONDS, NOW
    )
    assert len(_alive_watcher_threads()) == 1


def test_start_runs_the_first_cycle_right_away_and_reports_busy_meanwhile(watcher, cycle):
    cycle.release.clear()

    watcher.start(60)

    assert cycle.entered.wait(1) is True
    status = watcher.status()
    assert (status["busy"], status["cycles"], status["last_error"]) == (True, 0, "")
    # Первый тик назначен на момент запуска и ещё не сдвинут: цикл не кончился.
    assert status["next_tick_at"] == NOW
    cycle.release.set()
    watcher.stop()
    assert (cycle.calls, watcher.cycles, watcher.busy) == (1, 1, False)


def test_the_watcher_thread_is_a_daemon(watcher):
    watcher.start(60)

    assert _alive_watcher_threads()[0].daemon is True


def test_stop_ends_the_thread_within_a_second(watcher, cycle):
    watcher.start(60)
    assert cycle.entered.wait(1) is True

    watcher.stop(timeout=1.0)

    assert watcher.running is False
    assert _alive_watcher_threads() == []
    status = watcher.status()
    assert (status["running"], status["started_at"], status["next_tick_at"]) == (False, None, None)


def test_a_second_start_while_running_is_a_conflict(watcher):
    watcher.start(60)

    with pytest.raises(CollectionError) as excinfo:
        watcher.start(120)

    assert (excinfo.value.code, excinfo.value.status_code) == ("collection_running", 409)
    assert watcher.status()["interval_seconds"] == 60
    assert len(_alive_watcher_threads()) == 1


def test_stop_when_not_running_is_a_conflict(watcher):
    with pytest.raises(CollectionError) as excinfo:
        watcher.stop()

    assert (excinfo.value.code, excinfo.value.status_code) == ("collection_not_running", 409)


def test_the_watcher_can_be_restarted_with_another_interval(watcher, cycle):
    watcher.start(60)
    assert cycle.entered.wait(1) is True
    watcher.stop()

    watcher.start(120)

    assert watcher.running is True
    assert watcher.status()["interval_seconds"] == 120
    assert len(_alive_watcher_threads()) == 1


def test_the_error_of_the_previous_run_is_cleared_on_start(watcher):
    watcher.last_error = "RuntimeError: нет сети"

    watcher.start(60)

    assert watcher.status()["last_error"] == ""


# ── наблюдатель: один тик ──────────────────────────────────────────────────


def test_tick_counts_a_successful_cycle_and_clears_the_last_error(watcher, cycle):
    watcher.last_error = "старая ошибка"

    report = watcher.tick()

    assert (report.sources_ok, report.docs_new) == (1, 2)
    assert (watcher.cycles, watcher.last_error, watcher.busy) == (1, "", False)
    assert cycle.calls == 1


def test_tick_keeps_the_error_of_a_failed_cycle_and_does_not_count_it(watcher, cycle, caplog):
    cycle.error = RuntimeError("нет сети")

    with caplog.at_level(logging.WARNING, logger="collection"):
        report = watcher.tick()

    assert report is None
    assert (watcher.cycles, watcher.last_error, watcher.busy) == (0, "RuntimeError: нет сети", False)
    assert "цикл сбора не удался: RuntimeError: нет сети" in caplog.text


def test_tick_skips_silently_when_a_one_off_cycle_holds_the_lock(watcher, cycle, caplog):
    cycle.error = CollectionBusyError()

    with caplog.at_level(logging.WARNING, logger="collection"):
        report = watcher.tick()

    assert report is None
    assert (watcher.cycles, watcher.last_error, watcher.busy) == (0, "", False)
    assert caplog.records == []


def test_a_long_error_is_cut_to_500_characters(watcher, cycle):
    cycle.error = RuntimeError("х" * 600)

    watcher.tick()

    assert len(watcher.last_error) == 500


def test_a_successful_tick_after_a_failure_clears_the_error(watcher, cycle):
    cycle.error = RuntimeError("нет сети")
    watcher.tick()
    cycle.error = None

    watcher.tick()

    assert (watcher.cycles, watcher.last_error) == (1, "")


# ── run_collection_cycle поверх MockRoutes ─────────────────────────────────


def test_run_collection_cycle_polls_the_due_sources_and_records_the_run(
    config, hub_paths, file_db, offline_collector
):
    source = _source(file_db)
    file_db.sources.schedule(source.id, "2026-09-02T11:00:00+00:00")

    report = run_collection_cycle(config, hub_paths, tavily_key="", due_only=True)

    assert (report.sources_ok, report.sources_fail, report.docs_new) == (1, 0, 1)
    assert report.started_at == NOW
    assert offline_collector.urls().count(RSS_URL) == 1
    latest = file_db.runs.latest()
    assert (latest["sources_ok"], latest["docs_new"], latest["started_at"]) == (1, 1, NOW)
    assert [r["title"] for r in file_db.documents.list(source.id)] == ["Минцифры внесло законопроект"]
    assert file_db.sources.get(source.id).next_run_at == "2026-09-02T13:00:00+00:00"


def test_run_collection_cycle_with_due_only_skips_a_source_scheduled_for_later(
    config, hub_paths, file_db, offline_collector
):
    source = _source(file_db)
    file_db.sources.schedule(source.id, "2026-12-31T00:00:00+00:00")

    report = run_collection_cycle(config, hub_paths, due_only=True)

    assert (report.sources_ok, report.docs_new) == (0, 0)
    assert offline_collector.requests == []
    assert file_db.runs.latest()["sources_ok"] == 0


def test_run_collection_cycle_without_due_only_polls_every_active_source(
    config, hub_paths, file_db, offline_collector
):
    source = _source(file_db)
    file_db.sources.schedule(source.id, "2026-12-31T00:00:00+00:00")

    report = run_collection_cycle(config, hub_paths, due_only=False)

    assert (report.sources_ok, report.docs_new) == (1, 1)


def test_run_collection_cycle_with_source_ids_polls_only_those(
    config, hub_paths, file_db, offline_collector
):
    chosen = _source(file_db)
    _source(file_db, fetch_url="https://other.example.ru/rss.xml", name="Другая")

    report = run_collection_cycle(config, hub_paths, source_ids=[chosen.id])

    assert report.sources_ok == 1
    assert offline_collector.urls().count(RSS_URL) == 1
    assert "https://other.example.ru/rss.xml" not in offline_collector.urls()


def test_run_collection_cycle_opens_and_closes_its_own_connection(
    config, hub_paths, offline_collector, monkeypatch
):
    opened, closed = [], []

    class RecordingDatabase(Database):
        def __init__(self, path: str):
            opened.append(path)
            super().__init__(path)

        def close(self) -> None:
            closed.append(self.path)
            super().close()

    monkeypatch.setattr(collection_mod, "Database", RecordingDatabase)

    run_collection_cycle(config, hub_paths)

    assert opened == [hub_paths.db_path] == closed
    assert cycle_in_progress() is False


def test_a_second_cycle_while_one_is_running_is_busy(config, hub_paths, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class BlockingCollector:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, **kwargs) -> CollectReport:
            entered.set()
            release.wait(timeout=2)
            return CollectReport(started_at=NOW, finished_at=NOW)

    monkeypatch.setattr(collection_mod, "Collector", BlockingCollector)
    worker = threading.Thread(target=run_collection_cycle, args=(config, hub_paths), daemon=True)
    worker.start()
    try:
        assert entered.wait(1) is True
        assert cycle_in_progress() is True

        with pytest.raises(CollectionError) as excinfo:
            run_collection_cycle(config, hub_paths)

        assert (excinfo.value.code, excinfo.value.status_code) == ("collection_busy", 409)
    finally:
        release.set()
        worker.join(1)
    assert cycle_in_progress() is False


def test_the_lock_is_released_when_the_collector_raises(config, hub_paths, monkeypatch):
    class BrokenCollector:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, **kwargs):
            raise RuntimeError("нет сети")

    monkeypatch.setattr(collection_mod, "Collector", BrokenCollector)

    with pytest.raises(RuntimeError, match="нет сети"):
        run_collection_cycle(config, hub_paths)

    assert cycle_in_progress() is False


# ── run_collection_task ────────────────────────────────────────────────────


def test_run_collection_task_logs_the_summary_of_the_cycle(
    config, hub_paths, file_db, offline_collector, caplog
):
    source = _source(file_db)
    file_db.sources.schedule(source.id, "2026-09-02T11:00:00+00:00")

    with caplog.at_level(logging.INFO, logger="collection"):
        result = run_collection_task(config, hub_paths, tavily_key="", due_only=True)

    assert result is None
    assert (
        "разовый цикл сбора: 1 new documents; sources ok=1 not_modified=0 failed=0"
        in caplog.text
    )


def test_run_collection_task_swallows_a_failure_and_releases_the_lock(
    config, hub_paths, monkeypatch, caplog
):
    class BrokenCollector:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, **kwargs):
            raise RuntimeError("нет сети")

    monkeypatch.setattr(collection_mod, "Collector", BrokenCollector)

    with caplog.at_level(logging.WARNING, logger="collection"):
        result = run_collection_task(config, hub_paths)

    assert result is None
    assert "разовый цикл сбора не удался: нет сети" in caplog.text
    assert cycle_in_progress() is False


def test_run_collection_task_swallows_the_busy_error_too(config, hub_paths, monkeypatch, caplog):
    def busy(*args, **kwargs):
        raise CollectionBusyError()

    monkeypatch.setattr(collection_mod, "run_collection_cycle", busy)

    with caplog.at_level(logging.WARNING, logger="collection"):
        assert run_collection_task(config, hub_paths) is None

    assert "разовый цикл сбора не удался" in caplog.text


# ── CollectionService ──────────────────────────────────────────────────────


@pytest.fixture
def service(config, db, watcher) -> CollectionService:
    return CollectionService(config, db, watcher)


def test_status_of_an_idle_service_adds_zero_due_sources_and_no_last_collect(service):
    assert service.status() == {**IDLE_STATUS, "date_window_hours": 72, "due_sources": 0, "last_collect": None}


def test_status_counts_the_due_sources_and_shows_the_last_collect(service, db):
    due = _source(db)
    later = _source(db, fetch_url="https://other.example.ru/rss.xml", name="Позже")
    db.sources.schedule(due.id, "2026-09-02T11:00:00+00:00")
    db.sources.schedule(later.id, "2026-09-02T13:00:00+00:00")
    run_id = db.runs.add(
        CollectReport(
            started_at=NOW, finished_at="2026-09-02T12:00:05+00:00", sources_ok=2, sources_fail=1,
            sources_not_modified=0, docs_new=5,
        )
    )

    status = service.status()

    assert status["due_sources"] == 1
    assert status["last_collect"] == {
        "id": run_id,
        "started_at": NOW,
        "finished_at": "2026-09-02T12:00:05+00:00",
        "sources_ok": 2,
        "sources_fail": 1,
        "sources_not_modified": 0,
        "docs_new": 5,
    }


def test_start_and_stop_drive_the_watcher_and_answer_with_the_status(service, watcher, cycle):
    started = service.start(60)

    assert (started["running"], started["interval_seconds"], started["started_at"]) == (
        True, 60, NOW
    )
    assert watcher.running is True
    assert cycle.entered.wait(1) is True

    stopped = service.stop()

    assert (stopped["running"], stopped["started_at"]) == (False, None)
    assert watcher.running is False


def test_start_validates_the_interval_before_touching_the_watcher(service, watcher):
    with pytest.raises(CollectionError) as excinfo:
        service.start(10)

    assert excinfo.value.code == "validation_error"
    assert watcher.running is False


def test_ensure_idle_passes_when_nothing_is_collecting(service):
    assert service.ensure_idle() is None


def test_ensure_idle_is_a_conflict_while_the_watcher_is_inside_a_cycle(service, watcher, cycle):
    cycle.release.clear()
    watcher.start(60)
    assert cycle.entered.wait(1) is True

    with pytest.raises(CollectionError) as excinfo:
        service.ensure_idle()

    assert (excinfo.value.code, excinfo.value.status_code) == ("collection_busy", 409)
    cycle.release.set()


def test_ensure_idle_is_a_conflict_while_a_one_off_cycle_holds_the_lock(service, monkeypatch):
    monkeypatch.setattr(collection_mod, "cycle_in_progress", lambda: True)

    with pytest.raises(CollectionError) as excinfo:
        service.ensure_idle()

    assert excinfo.value.code == "collection_busy"
    assert service.status()["busy"] is True


# ── HTTP ───────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_watcher(app, cycle, frozen_clock) -> CollectionWatcher:
    """Наблюдатель процесса подменён: цикл поддельный, часы заморожены."""
    instance = CollectionWatcher(cycle)
    app.dependency_overrides[get_collection_watcher] = lambda: instance
    yield instance
    cycle.release.set()
    if instance.running:
        instance.stop(timeout=1.0)


@pytest.fixture
def recorded_task(monkeypatch) -> list[tuple[tuple, dict]]:
    """`run_collection_task` в маршруте записывает вызов вместо сбора."""
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        collection_routes, "run_collection_task", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    return calls


def test_collection_status_of_an_idle_hub(client, fake_watcher):
    response = client.get("/api/v1/collection")

    assert response.status_code == 200
    assert response.json() == {**IDLE_STATUS, "date_window_hours": 72, "due_sources": 0, "last_collect": None}


def test_start_answers_with_the_running_status(client, fake_watcher):
    response = client.post("/api/v1/collection/start", json={"interval_seconds": 60})

    assert response.status_code == 200
    body = response.json()
    assert (body["running"], body["interval_seconds"], body["started_at"]) == (True, 60, NOW)
    assert fake_watcher.running is True


def test_start_defaults_to_fifteen_minutes(client, fake_watcher):
    body = client.post("/api/v1/collection/start", json={}).json()

    assert body["interval_seconds"] == DEFAULT_INTERVAL_SECONDS == 900


def test_a_second_start_is_a_409_problem(client, fake_watcher):
    client.post("/api/v1/collection/start", json={"interval_seconds": 60})

    response = client.post("/api/v1/collection/start", json={"interval_seconds": 120})

    assert response.status_code == 409
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "collection_running"
    assert fake_watcher.status()["interval_seconds"] == 60


def test_start_below_the_minimum_is_a_400_problem(client, fake_watcher):
    response = client.post("/api/v1/collection/start", json={"interval_seconds": 30})

    assert response.status_code == 400
    assert response.json()["code"] == "validation_error"
    assert fake_watcher.running is False


def test_stop_answers_with_the_stopped_status(client, fake_watcher):
    client.post("/api/v1/collection/start", json={"interval_seconds": 60})

    response = client.post("/api/v1/collection/stop")

    assert response.status_code == 200
    body = response.json()
    assert (body["running"], body["started_at"], body["next_tick_at"]) == (False, None, None)
    assert fake_watcher.running is False


def test_stop_when_not_running_is_a_409_problem(client, fake_watcher):
    response = client.post("/api/v1/collection/stop")

    assert response.status_code == 409
    assert response.json()["code"] == "collection_not_running"


def test_a_one_off_run_answers_202_and_schedules_the_task_after_the_response(
    client, fake_watcher, recorded_task
):
    response = client.post(
        "/api/v1/collection/runs",
        json={"source_ids": [3], "due_only": True, "backfill": True, "force": True},
    )

    assert response.status_code == 202
    assert response.json()["running"] is False
    assert len(recorded_task) == 1
    (config, paths), kwargs = recorded_task[0]
    assert config is get_config()
    assert paths == get_paths()
    assert kwargs == {
        "tavily_key": "", "due_only": True, "source_ids": [3], "backfill": True, "force": True,
    }


def test_a_one_off_run_defaults_to_every_source_without_backfill(client, fake_watcher, recorded_task):
    response = client.post("/api/v1/collection/runs", json={})

    assert response.status_code == 202
    _, kwargs = recorded_task[0]
    assert kwargs == {
        "tavily_key": "", "due_only": False, "source_ids": None, "backfill": False, "force": False,
    }


def test_a_one_off_run_while_the_watcher_is_busy_is_a_409_problem(client, fake_watcher, recorded_task):
    fake_watcher.busy = True

    response = client.post("/api/v1/collection/runs", json={})

    assert response.status_code == 409
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "collection_busy"
    assert recorded_task == []


def test_a_one_off_run_while_a_cycle_holds_the_lock_is_a_409_problem(
    client, fake_watcher, recorded_task, monkeypatch
):
    monkeypatch.setattr(collection_mod, "cycle_in_progress", lambda: True)

    response = client.post("/api/v1/collection/runs", json={})

    assert (response.status_code, response.json()["code"]) == (409, "collection_busy")
    assert recorded_task == []


def test_an_unknown_field_in_the_run_request_is_a_422(client, fake_watcher, recorded_task):
    response = client.post("/api/v1/collection/runs", json={"interval_seconds": 60})

    assert response.status_code == 422
    assert recorded_task == []


def test_the_lifespan_stops_a_running_watcher_on_shutdown(app, hub_paths):
    """Настоящий наблюдатель без источников: цикл проходит вхолостую и не ходит в сеть."""
    with TestClient(app) as client:
        response = client.post("/api/v1/collection/start", json={"interval_seconds": 60})
        assert response.status_code == 200
        watcher = get_collection_watcher()
        assert watcher.running is True

    assert watcher.running is False
    assert _alive_watcher_threads() == []
