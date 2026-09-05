"""Очередь ИИ как история прогонов: `processing_runs`, `ProcessingService.run/enqueue`,
`run_in_background` и маршруты `/processing`.

Модель — `FakeLLM`, часы — `frozen_clock`; ни одного вызова наружу. Прогон
записывается всегда, кроме `dry_run`: дашборд и CLI смотрят на одну и ту же таблицу.
"""

from __future__ import annotations

import logging

import pytest
from support import NEWS_TEXT, FakeLLM, news_answer

from src.exceptions import ItemError, ProcessingError
from src.models import RawDocument, Source
from src.repositories import Database
from src.services import processing_service as processing_mod
from src.services.processing_service import ProcessingService, run_in_background

NOW = "2026-09-02T12:00:00+00:00"
PROBLEM = "application/problem+json"
DEFAULT_PARAMS = {"limit": None, "source_id": None, "since": None, "profile_id": None, "force": False}
FEED_URL = "https://a.ru/rss"


def _queue_document(db: Database, external_id: str = "n1") -> int:
    """Один собранный документ без карточки — то, что прогон должен превратить в карточку."""
    source = db.sources.get_by_fetch_url(FEED_URL) or db.sources.add(
        Source(name="Лента", url="https://a.ru/", kind="rss", fetch_url=FEED_URL)
    )
    with db.transaction():
        return db.documents.insert(
            RawDocument(
                source_id=source.id,
                external_id=external_id,
                url=f"https://a.ru/{external_id}",
                title="Оператор платного ТВ запустил рекомендательный сервис",
                text=NEWS_TEXT,
                published_at=NOW,
                fetched_at=NOW,
            )
        )


def _boom(self, **kwargs):
    raise RuntimeError("модель недоступна")


@pytest.fixture
def service(config, db, frozen_clock) -> ProcessingService:
    return ProcessingService(config, db, provider=FakeLLM(news_answer()), embedder=None)


# ── репозиторий ────────────────────────────────────────────────────────────


def test_start_opens_a_running_row_stamped_with_the_frozen_clock(db, frozen_clock):
    run = db.processing_runs.start({"limit": 5, "force": False}, trigger="api")

    stored = db.processing_runs.get(run.id)

    assert run.id == 1
    assert (stored.status, stored.trigger) == ("running", "api")
    assert (stored.started_at, stored.finished_at) == (NOW, None)
    assert stored.params == {"limit": 5, "force": False}
    assert (stored.documents, stored.items_new, stored.calls, stored.elapsed_s) == (0, 0, 0, 0.0)
    assert stored.error == ""


def test_start_defaults_to_the_cli_trigger(db):
    assert db.processing_runs.start({}).trigger == "cli"


def test_params_round_trip_through_json_with_none_booleans_and_cyrillic(db):
    params = {"limit": None, "since": "2026-09-01", "force": True, "note": "разбор утра"}

    run = db.processing_runs.start(params)

    assert db.processing_runs.get(run.id).params == params


def test_the_stored_params_are_a_copy_not_the_callers_dict(db):
    params = {"limit": 3}
    run = db.processing_runs.start(params)
    params["limit"] = 99

    assert db.processing_runs.get(run.id).params == {"limit": 3}


def test_broken_params_json_reads_as_an_empty_dict(db):
    run = db.processing_runs.start({"limit": 3})
    db.conn.execute("UPDATE processing_runs SET params='не json' WHERE id=?", (run.id,))

    assert db.processing_runs.get(run.id).params == {}


def test_finish_marks_the_row_done_with_the_counters(db, frozen_clock):
    run = db.processing_runs.start({})

    db.processing_runs.finish(
        run.id,
        {
            "documents": 3, "clusters": 2, "items_new": 2, "items_joined": 1, "items_updated": 0,
            "degraded": 1, "needs_review": 1, "calls": 4, "failed": 0, "elapsed_s": 1.25,
        },
    )

    stored = db.processing_runs.get(run.id)
    assert (stored.status, stored.finished_at, stored.error) == ("done", NOW, "")
    assert (stored.documents, stored.clusters, stored.items_new, stored.items_joined) == (3, 2, 2, 1)
    assert (stored.items_updated, stored.degraded, stored.needs_review) == (0, 1, 1)
    assert (stored.calls, stored.failed) == (4, 0)
    assert stored.elapsed_s == pytest.approx(1.25)


def test_finish_with_missing_counters_falls_back_to_zero(db):
    run = db.processing_runs.start({})

    db.processing_runs.finish(run.id, {})

    stored = db.processing_runs.get(run.id)
    assert stored.status == "done"
    assert (stored.documents, stored.items_new, stored.calls, stored.elapsed_s) == (0, 0, 0, 0.0)


def test_fail_marks_the_row_failed_and_keeps_the_error(db, frozen_clock):
    run = db.processing_runs.start({})

    db.processing_runs.fail(run.id, "RuntimeError: модель недоступна")

    stored = db.processing_runs.get(run.id)
    assert (stored.status, stored.finished_at) == ("failed", NOW)
    assert stored.error == "RuntimeError: модель недоступна"


def test_fail_cuts_the_error_to_500_characters(db):
    run = db.processing_runs.start({})

    db.processing_runs.fail(run.id, "х" * 600)

    assert len(db.processing_runs.get(run.id).error) == 500


def test_get_of_an_unknown_id_is_none(db):
    assert db.processing_runs.get(999) is None


def test_running_is_the_open_row_and_none_once_it_is_closed(db):
    assert db.processing_runs.running() is None
    run = db.processing_runs.start({})

    assert db.processing_runs.running().id == run.id

    db.processing_runs.finish(run.id, {})
    assert db.processing_runs.running() is None


def test_latest_is_the_newest_row_whatever_its_status(db):
    first = db.processing_runs.start({})
    db.processing_runs.finish(first.id, {})
    second = db.processing_runs.start({})
    db.processing_runs.fail(second.id, "x")

    assert db.processing_runs.latest().id == second.id


def test_latest_of_an_empty_history_is_none(db):
    assert db.processing_runs.latest() is None


def test_list_is_newest_first_and_honours_the_limit(db):
    ids = [db.processing_runs.start({}).id for _ in range(3)]

    assert [r.id for r in db.processing_runs.list()] == ids[::-1]
    assert [r.id for r in db.processing_runs.list(limit=2)] == ids[::-1][:2]


def test_abandon_running_fails_every_open_row_and_leaves_the_closed_ones(db, frozen_clock):
    open_ids = [db.processing_runs.start({}).id for _ in range(2)]
    done = db.processing_runs.start({})
    db.processing_runs.finish(done.id, {"documents": 1})

    abandoned = db.processing_runs.abandon_running("процесс перезапущен")

    assert abandoned == 2
    for run_id in open_ids:
        stored = db.processing_runs.get(run_id)
        assert (stored.status, stored.error, stored.finished_at) == (
            "failed", "процесс перезапущен", NOW
        )
    assert (db.processing_runs.get(done.id).status, db.processing_runs.get(done.id).error) == (
        "done", ""
    )
    assert db.processing_runs.running() is None


def test_abandon_running_with_nothing_open_reports_zero(db):
    assert db.processing_runs.abandon_running("x") == 0


# ── ProcessingService.run: запись прогона ──────────────────────────────────


def test_run_records_a_done_row_with_the_report_counters_and_the_cli_trigger(service, db):
    _queue_document(db)

    report = service.run()

    run = db.processing_runs.latest()
    assert (run.status, run.trigger, run.finished_at) == ("done", "cli", NOW)
    assert run.params == DEFAULT_PARAMS
    assert (run.documents, run.items_new, run.calls) == (1, 1, 1)
    assert (run.documents, run.clusters, run.items_new, run.items_joined, run.items_updated) == (
        report.documents, report.clusters, report.items_new, report.items_joined,
        report.items_updated,
    )
    assert (run.degraded, run.needs_review, run.calls, run.failed) == (
        report.degraded, report.needs_review, report.calls, report.failed
    )
    assert run.elapsed_s == pytest.approx(report.elapsed_s)
    assert db.processing_runs.running() is None


def test_run_records_the_parameters_it_was_given(service, db):
    service.run(limit=5, source_id=7, since="2026-09-01", force=True)

    assert db.processing_runs.latest().params == {
        "limit": 5, "source_id": 7, "since": "2026-09-01", "profile_id": None, "force": True,
    }


@pytest.mark.parametrize("trigger", ["cli", "api", "watch"], ids=lambda s: s)
def test_run_records_the_trigger_it_was_started_with(service, db, trigger):
    service.run(trigger=trigger)

    assert db.processing_runs.latest().trigger == trigger


def test_an_empty_queue_still_leaves_a_done_row_with_zero_counters(service, db):
    report = service.run()

    run = db.processing_runs.latest()
    assert (report.documents, run.status, run.documents, run.items_new) == (0, "done", 0, 0)
    assert service.provider.calls == 0


def test_a_raising_run_leaves_a_failed_row_with_the_error_and_re_raises(service, db, monkeypatch):
    monkeypatch.setattr(ProcessingService, "_run", _boom)

    with pytest.raises(RuntimeError, match="модель недоступна"):
        service.run()

    run = db.processing_runs.latest()
    assert (run.status, run.error, run.finished_at) == (
        "failed", "RuntimeError: модель недоступна", NOW
    )
    assert db.processing_runs.running() is None


def test_a_dry_run_records_nothing_and_never_calls_the_model(service, db):
    _queue_document(db)

    report = service.run(dry_run=True)

    assert report.documents == 1
    assert db.processing_runs.list() == []
    assert service.provider.calls == 0


def test_run_with_a_run_id_continues_the_enqueued_record_instead_of_opening_another(service, db):
    _queue_document(db)
    run = service.enqueue(limit=3)

    service.run(run_id=run.id, trigger="api", **run.params)

    assert [r.id for r in db.processing_runs.list()] == [run.id]
    stored = db.processing_runs.get(run.id)
    assert (stored.status, stored.trigger, stored.items_new) == ("done", "api", 1)


def test_a_repeated_run_adds_no_card_but_is_still_recorded(service, db):
    _queue_document(db)
    service.run()

    second = service.run()

    assert (second.documents, second.items_new) == (0, 0)
    assert [r.status for r in db.processing_runs.list()] == ["done", "done"]
    assert db.items.count() == 1


# ── ProcessingService.enqueue / get_run / list_runs / queue_status ─────────


def test_enqueue_opens_a_running_row_with_the_api_trigger(service, db):
    run = service.enqueue(limit=5, source_id=2, since="2026-09-01", force=True)

    assert (run.status, run.trigger, run.started_at) == ("running", "api", NOW)
    assert run.params == {
        "limit": 5, "source_id": 2, "since": "2026-09-01", "profile_id": None, "force": True,
    }
    assert db.processing_runs.running().id == run.id


def test_enqueue_refuses_while_another_run_is_open(service, db):
    first = service.enqueue()

    with pytest.raises(ProcessingError) as excinfo:
        service.enqueue()

    assert (excinfo.value.code, excinfo.value.status_code) == ("processing_busy", 409)
    assert excinfo.value.details == {"run_id": first.id}
    assert f"#{first.id}" in excinfo.value.message
    assert len(db.processing_runs.list()) == 1


@pytest.mark.parametrize("closer", ["finish", "fail"], ids=lambda s: s)
def test_enqueue_is_allowed_again_once_the_open_run_is_closed(service, db, closer):
    first = service.enqueue()
    if closer == "finish":
        db.processing_runs.finish(first.id, {})
    else:
        db.processing_runs.fail(first.id, "x")

    second = service.enqueue()

    assert second.id != first.id
    assert db.processing_runs.running().id == second.id


@pytest.mark.parametrize("limit", [0, -1], ids=["zero", "negative"])
def test_enqueue_rejects_a_limit_below_one_and_records_nothing(service, db, limit):
    with pytest.raises(ItemError, match="limit") as excinfo:
        service.enqueue(limit=limit)

    assert excinfo.value.code == "validation_error"
    assert db.processing_runs.list() == []


def test_get_run_returns_the_record(service):
    run = service.enqueue()

    assert service.get_run(run.id).id == run.id


def test_get_run_of_an_unknown_id_is_a_named_404(service):
    with pytest.raises(ProcessingError, match="#999") as excinfo:
        service.get_run(999)

    assert (excinfo.value.code, excinfo.value.status_code) == ("run_not_found", 404)


def test_list_runs_is_newest_first_and_honours_the_limit(service, db):
    ids = []
    for _ in range(3):
        run = service.enqueue()
        db.processing_runs.finish(run.id, {})
        ids.append(run.id)

    assert [r.id for r in service.list_runs()] == ids[::-1]
    assert [r.id for r in service.list_runs(limit=1)] == [ids[-1]]


def test_queue_status_of_an_idle_service_with_one_document_waiting(service, db):
    _queue_document(db)

    assert service.queue_status() == {
        "running": None, "last": None, "unprocessed": 1, "llm_available": True,
    }


def test_queue_status_reports_the_open_run_as_both_running_and_last(service, db):
    run = service.enqueue()

    status = service.queue_status()

    assert status["running"].id == run.id
    assert status["last"].id == run.id


def test_queue_status_after_a_run_shows_no_open_run_and_an_empty_queue(service, db):
    _queue_document(db)
    service.run()

    status = service.queue_status()

    assert status["running"] is None
    assert (status["last"].status, status["last"].items_new) == ("done", 1)
    assert status["unprocessed"] == 0


def test_queue_status_without_a_provider_says_the_model_is_unavailable(config, db):
    offline = ProcessingService(config, db, provider=None, embedder=None)

    assert offline.queue_status()["llm_available"] is False


# ── run_in_background ──────────────────────────────────────────────────────


@pytest.fixture
def recording_database(monkeypatch) -> dict[str, list[str]]:
    """`Database` в модуле сервиса записывает, какой файл открыла и закрыла."""
    log: dict[str, list[str]] = {"opened": [], "closed": []}

    class RecordingDatabase(Database):
        def __init__(self, path: str):
            log["opened"].append(path)
            super().__init__(path)

        def close(self) -> None:
            log["closed"].append(self.path)
            super().close()

    monkeypatch.setattr(processing_mod, "Database", RecordingDatabase)
    return log


def test_run_in_background_continues_the_record_on_its_own_connection(
    config, hub_paths, file_db, frozen_clock, recording_database
):
    _queue_document(file_db)
    run = file_db.processing_runs.start(DEFAULT_PARAMS, trigger="api")
    llm = FakeLLM(news_answer())

    run_in_background(config, hub_paths, run.id, run.params, llm)

    assert recording_database == {"opened": [hub_paths.db_path], "closed": [hub_paths.db_path]}
    stored = file_db.processing_runs.get(run.id)
    assert (stored.status, stored.trigger, stored.items_new) == ("done", "api", 1)
    assert [r.id for r in file_db.processing_runs.list()] == [run.id]
    assert llm.calls == 1
    assert file_db.items.count() == 1


def test_run_in_background_swallows_a_failure_and_leaves_the_failed_row(
    config, hub_paths, file_db, frozen_clock, recording_database, monkeypatch, caplog
):
    monkeypatch.setattr(ProcessingService, "_run", _boom)
    run = file_db.processing_runs.start(DEFAULT_PARAMS, trigger="api")

    with caplog.at_level(logging.ERROR, logger="processing"):
        result = run_in_background(config, hub_paths, run.id, run.params, None)

    assert result is None
    stored = file_db.processing_runs.get(run.id)
    assert (stored.status, stored.error) == ("failed", "RuntimeError: модель недоступна")
    assert f"прогон обработки #{run.id} завершился ошибкой: модель недоступна" in caplog.text
    assert recording_database["closed"] == [hub_paths.db_path]


def test_run_in_background_without_a_provider_builds_degraded_cards(
    config, hub_paths, file_db, frozen_clock
):
    _queue_document(file_db)
    run = file_db.processing_runs.start(DEFAULT_PARAMS, trigger="api")

    run_in_background(config, hub_paths, run.id, run.params, None)

    stored = file_db.processing_runs.get(run.id)
    assert (stored.status, stored.items_new, stored.degraded) == ("done", 1, 1)


# ── HTTP ───────────────────────────────────────────────────────────────────


def test_the_processing_status_of_an_empty_hub(client):
    response = client.get("/api/v1/processing")

    assert response.status_code == 200
    assert response.json() == {
        "running": None, "last": None, "unprocessed": 0, "llm_available": True,
    }


def test_starting_a_run_answers_202_and_the_run_is_done_on_the_next_read(
    client, file_db, fake_llm, frozen_clock
):
    _queue_document(file_db)

    response = client.post("/api/v1/processing/runs", json={"limit": 10})

    assert response.status_code == 202
    body = response.json()
    assert (body["status"], body["trigger"], body["started_at"]) == ("running", "api", NOW)
    assert body["params"] == {**DEFAULT_PARAMS, "limit": 10}
    assert body["finished_at"] is None

    # `TestClient` выполняет фоновую задачу до возврата ответа: прогон уже закрыт.
    run = client.get(f"/api/v1/processing/runs/{body['id']}").json()
    assert (run["status"], run["documents"], run["items_new"], run["calls"]) == ("done", 1, 1, 1)
    assert run["finished_at"] == NOW
    assert fake_llm.calls == 1

    status = client.get("/api/v1/processing").json()
    assert status["running"] is None
    assert status["last"]["id"] == body["id"]
    assert status["unprocessed"] == 0


def test_a_second_start_while_a_run_is_open_is_a_409_problem(client, file_db):
    open_run = file_db.processing_runs.start(DEFAULT_PARAMS, trigger="api")

    response = client.post("/api/v1/processing/runs", json={})

    assert response.status_code == 409
    assert response.headers["content-type"] == PROBLEM
    body = response.json()
    assert (body["code"], body["details"]) == ("processing_busy", {"run_id": open_run.id})
    assert len(file_db.processing_runs.list()) == 1


def test_an_unknown_run_is_a_404_problem(client):
    response = client.get("/api/v1/processing/runs/999")

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "run_not_found"


@pytest.mark.parametrize(
    "payload",
    [{"limit": 0}, {"limit": -5}, {"dry_run": True}, {"limit": "много"}],
    ids=["limit-zero", "limit-negative", "unknown-field", "limit-not-a-number"],
)
def test_a_bad_run_request_is_rejected_by_the_schema_before_anything_is_recorded(
    client, file_db, payload
):
    response = client.post("/api/v1/processing/runs", json=payload)

    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "validation_error"
    assert file_db.processing_runs.list() == []


def test_the_run_history_is_newest_first_and_honours_the_limit(client, file_db):
    ids = []
    for _ in range(3):
        run = file_db.processing_runs.start(DEFAULT_PARAMS)
        file_db.processing_runs.finish(run.id, {"documents": 2})
        ids.append(run.id)

    body = client.get("/api/v1/processing/runs", params={"limit": 2}).json()

    assert [r["id"] for r in body["runs"]] == ids[::-1][:2]
    assert body["runs"][0]["documents"] == 2


@pytest.mark.parametrize("limit", [0, 201], ids=["zero", "over-the-cap"])
def test_the_history_limit_is_validated(client, limit):
    response = client.get("/api/v1/processing/runs", params={"limit": limit})

    assert response.status_code == 422


@pytest.fixture
def stale_run(file_db) -> int:
    """Прогон, оставшийся «running» от прошлого процесса — заводится до старта приложения."""
    return file_db.processing_runs.start(DEFAULT_PARAMS, trigger="api").id


def test_startup_abandons_a_run_left_running_by_a_previous_process(stale_run, client):
    run = client.get(f"/api/v1/processing/runs/{stale_run}").json()

    assert run["status"] == "failed"
    assert run["error"] == "процесс API перезапущен во время прогона"
    assert client.get("/api/v1/processing").json()["running"] is None


def test_after_the_stale_run_is_abandoned_a_new_run_can_be_started(stale_run, client):
    response = client.post("/api/v1/processing/runs", json={})

    assert response.status_code == 202
    assert response.json()["id"] != stale_run
