from dataclasses import replace
from threading import Event

import pytest

from src.models import RawDocument
from src.models.responses import ProcessingRunResponse
from src.processing.pipeline import Draft
from src.services.collection_service import CollectionWatcher, run_collection_cycle
from src.services.processing_service import ProcessingService, _Unit


def test_card_number_search_respects_filters_and_facets(client, card_factory, source_factory):
    source = source_factory("Card search")
    item_id = card_factory(source, title="No number in the text", priority="high")
    card_factory(source, title=f"Article mentioning {item_id}", priority="low")
    query = {"q": f"#{item_id}"}
    assert [item["id"] for item in client.get("/api/v1/items", params=query).json()["items"]] == [
        item_id
    ]
    assert client.get("/api/v1/items/facets", params=query).json()["total"] == 1
    assert client.get("/api/v1/items", params={**query, "priority": "low"}).json()["total"] == 0
    client.post(f"/api/v1/items/{item_id}/hide", json={"scope": "feed"})
    assert client.get("/api/v1/items", params=query).json()["total"] == 0
    assert (
        client.get("/api/v1/items", params={**query, "include_hidden": "true"}).json()["total"] == 1
    )


def test_unlimited_run_exceeds_configured_batch_cap(
    config, file_db, source_factory, document_factory, monkeypatch
):
    source = source_factory("Unlimited")
    for index in range(205):
        document_factory(source, title=str(index))
    service = ProcessingService(config, file_db)
    monkeypatch.setattr(service, "_prepare", lambda rows, embed: [])
    assert service.run(dry_run=True).documents == 205
    assert service.run(limit=7, dry_run=True).documents == 7


@pytest.mark.parametrize("workers", [1, 2])
def test_stop_saves_inflight_cards_and_leaves_rest_for_resume(
    config, file_db, source_factory, document_factory, monkeypatch, workers
):
    source = source_factory("Stop test")
    for index in range(5):
        document_factory(source, title=str(index), text=f"Text {index}.")
    config = replace(config, processing=replace(config.processing, concurrency=workers))
    service = ProcessingService(config, file_db)

    def prepare(rows, embed):
        return [
            _Unit(
                document_id=row["id"],
                document=RawDocument.from_row(row),
                norm_text=row["text"],
                simhash="0000000000000000",
                members=[row["id"]],
            )
            for row in rows
        ]

    monkeypatch.setattr(service, "_prepare", prepare)
    entered = Event()
    release = Event()
    calls = []

    def model(text, **kwargs):
        calls.append(text)
        if workers == 2:
            if len(calls) == 1:
                assert entered.wait(2)
            else:
                entered.set()
                assert release.wait(2)
        return Draft(title=text, summary=[text])

    monkeypatch.setattr(service.pipeline, "process", model)
    run = service.enqueue()
    store = service._store

    def save(*args):
        store(*args)
        service.stop_run(run.id)
        release.set()

    monkeypatch.setattr(service, "_store", save)
    try:
        report = service.run(run_id=run.id)
    finally:
        release.set()
    assert report.items_new == workers
    assert len(calls) == workers
    assert file_db.documents.count_unprocessed() == 5 - workers
    assert file_db.documents.count_failed() == 0
    result = ProcessingRunResponse.from_domain(service.get_run(run.id))
    assert result.stopped and result.stop_requested and result.finished_at
    assert service.queue_status()["running"] is None
    monkeypatch.setattr(service, "_store", store)
    monkeypatch.setattr(
        service.pipeline, "process", lambda text, **kwargs: Draft(title=text, summary=[text])
    )
    assert service.run().items_new == 5 - workers


def test_stop_before_background_start_makes_no_model_calls(config, file_db, monkeypatch):
    service = ProcessingService(config, file_db)
    run = service.enqueue()
    service.stop_run(run.id)
    monkeypatch.setattr(
        service, "_prepare", lambda *args, **kwargs: pytest.fail("stopped run prepared documents")
    )
    service.run(run_id=run.id)
    assert ProcessingRunResponse.from_domain(service.get_run(run.id)).stopped


def test_stop_endpoint_is_idempotent_and_preserves_completed_run(client, file_db):
    run = file_db.processing_runs.start({})
    for _ in range(2):
        response = client.post(f"/api/v1/processing/runs/{run.id}/stop")
        assert response.status_code == 200
        assert response.json()["stop_requested"] is True
    file_db.processing_runs.finish(run.id, {})
    assert client.post(f"/api/v1/processing/runs/{run.id}/stop").json()["status"] == "done"
    assert client.post("/api/v1/processing/runs/999999/stop").status_code == 404


def test_collection_window_reaches_collector_without_changing_shared_config(
    config, hub_paths, monkeypatch
):
    from src.services import collection_service

    seen = []

    class Collector:
        def __init__(self, selected, *args, **kwargs):
            seen.append(selected.scraper.date_window_hours)

        def run(self, **kwargs):
            return None

    monkeypatch.setattr(collection_service, "Collector", Collector)
    original = config.scraper.date_window_hours
    run_collection_cycle(config, hub_paths, date_window_hours=24)
    assert seen == [24]
    assert config.scraper.date_window_hours == original


def test_automatic_monitoring_keeps_the_selected_window(config, file_db):
    from src.models import CollectReport
    from src.services.collection_service import CollectionService

    entered = Event()
    seen = []

    def cycle():
        seen.append(watcher.date_window_hours)
        entered.set()
        return CollectReport()

    watcher = CollectionWatcher(cycle)
    service = CollectionService(config, file_db, watcher)
    try:
        result = service.start(120, 168)
        assert entered.wait(2)
        assert result["interval_seconds"] == 120
        assert result["date_window_hours"] == 168
        assert seen == [168]
    finally:
        if watcher.running:
            watcher.stop()


@pytest.mark.parametrize("route", ["/collection/start", "/collection/runs"])
def test_collection_rejects_nonpositive_windows(client, route):
    assert client.post("/api/v1" + route, json={"date_window_hours": 0}).status_code == 422
