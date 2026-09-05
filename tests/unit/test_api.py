"""src/api — HTTP-слой поверх тех же сервисов, что и CLI.

Сервер не поднимается: всё идёт через `TestClient`, то есть по ASGI напрямую.
Отдельно проверяется формат ошибок — принцип III конституции требует RFC 7807.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from support import FakeLLM, news_answer

from src.api.app import create_app
from src.models import RawDocument, Source
from src.processing import service as service_mod

NOW = "2026-09-02T12:00:00+00:00"
PROBLEM = "application/problem+json"


@pytest.fixture
def client(config, hub_paths, file_db, monkeypatch) -> TestClient:
    """Приложение на временном HUB_ROOT; модель всегда фейковая."""
    monkeypatch.setattr(
        service_mod.ProcessingService,
        "__init__",
        _fake_provider_init(service_mod.ProcessingService.__init__),
    )
    return TestClient(create_app(config, hub_paths))


def _fake_provider_init(original):
    def patched(self, config, db, *, provider=None, embedder=None):
        original(self, config, db, provider=provider or FakeLLM(news_answer()), embedder=embedder)

    return patched


@pytest.fixture
def source(file_db) -> Source:
    return file_db.sources.add(
        Source(name="Лента", url="https://a.ru/", kind="rss", fetch_url="https://a.ru/rss")
    )


@pytest.fixture
def item_id(file_db, source, item_factory) -> int:
    with file_db.transaction():
        document_id = file_db.documents.insert(
            RawDocument(
                source_id=source.id,
                external_id="a",
                url="https://a.ru/a",
                title="Аккредитация ИИ-сервисов",
                text="Минцифры внесло законопроект. Второе предложение. Третье предложение.",
                published_at=NOW,
                fetched_at=NOW,
            )
        )
    return item_factory(file_db, document_id, title="Аккредитация ИИ-сервисов")


# ── формат ошибок ──────────────────────────────────────────────────────────


def test_health_answers(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_a_missing_source_is_a_problem_document(client):
    response = client.get("/api/v1/sources/999")

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM
    body = response.json()
    assert body["status"] == 404
    assert body["code"] == "source_not_found"
    assert set(body) >= {"type", "title", "status", "detail"}


def test_a_missing_card_is_a_problem_document(client):
    response = client.get("/api/v1/items/999")
    assert (response.status_code, response.json()["code"]) == (404, "item_not_found")


def test_a_malformed_body_is_a_problem_document(client):
    response = client.post("/api/v1/sources", json={"title": "без ссылки"})

    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "validation_error"


# ── источники ──────────────────────────────────────────────────────────────


def test_sources_are_listed_without_the_deleted_ones(client, file_db, source):
    file_db.sources.remove(source.id)

    body = client.get("/api/v1/sources").json()

    assert body["sources"] == []
    assert client.get("/api/v1/sources", params={"status": "deleted"}).json()["sources"]


def test_a_source_can_be_paused_and_resumed(client, source):
    paused = client.patch(f"/api/v1/sources/{source.id}", json={"status": "paused"})
    assert paused.json()["status"] == "paused"

    resumed = client.patch(f"/api/v1/sources/{source.id}", json={"status": "active"})
    assert resumed.json()["status"] == "active"


def test_changing_the_interval_moves_the_next_run(client, source):
    before = client.get(f"/api/v1/sources/{source.id}").json()["next_run_at"]

    after = client.patch(f"/api/v1/sources/{source.id}", json={"poll_interval": "15m"}).json()

    assert after["poll_interval"] == "15m"
    assert after["next_run_at"] != before


def test_an_unknown_interval_is_refused(client, source):
    response = client.patch(f"/api/v1/sources/{source.id}", json={"poll_interval": "30m"})
    assert (response.status_code, response.json()["code"]) == (400, "validation_error")


def test_deleting_a_source_keeps_its_documents(client, file_db, source, item_id):
    response = client.delete(f"/api/v1/sources/{source.id}")

    assert response.status_code == 200
    assert response.json()["documents_kept"] == 1
    assert file_db.sources.get(source.id).status == "deleted"
    assert file_db.documents.count(source.id) == 1


def test_purge_items_hides_the_cards_without_deleting_them(client, file_db, source, item_id):
    client.delete(f"/api/v1/sources/{source.id}", params={"purge_items": True})

    assert file_db.items.get(item_id).visibility == "hidden_feed"
    assert file_db.items.count() == 1


def test_a_deleted_source_can_be_restored(client, file_db, source):
    client.delete(f"/api/v1/sources/{source.id}")

    restored = client.post(f"/api/v1/sources/{source.id}/restore")

    assert restored.json()["status"] == "active"


def test_health_reports_the_poll_history(client, file_db, source):
    run_id = file_db.source_runs.start(source.id, NOW)
    file_db.source_runs.finish(run_id, items_found=7, items_new=2, error_code="timeout")

    body = client.get(f"/api/v1/sources/{source.id}/health").json()

    assert body["source"]["id"] == source.id
    assert [(r["items_found"], r["items_new"], r["error_code"]) for r in body["runs"]] == [
        (7, 2, "timeout")
    ]


# ── карточки ───────────────────────────────────────────────────────────────


def test_the_feed_hides_what_was_hidden(client, file_db, item_id):
    client.post(f"/api/v1/items/{item_id}/hide", json={"scope": "digest", "reason": "не тем"})

    assert client.get("/api/v1/items").json()["items"] == []
    assert client.get("/api/v1/items", params={"include_hidden": True}).json()["items"]

    client.post(f"/api/v1/items/{item_id}/unhide")
    assert len(client.get("/api/v1/items").json()["items"]) == 1


def test_bulk_hiding_answers_with_the_number_changed(client, item_id):
    response = client.post(
        "/api/v1/items/bulk", json={"item_ids": [item_id], "scope": "digest"}
    )
    assert response.json() == {"changed": 1, "scope": "digest"}


def test_patching_a_card_marks_the_field_as_human(client, file_db, item_id):
    response = client.patch(
        f"/api/v1/items/{item_id}",
        json={"summary": "Своими словами.", "edit_reason": "wrong_focus"},
    )

    assert response.status_code == 200
    assert response.json()["manual_overrides"] == ["summary"]
    revisions = file_db.items.revisions(item_id)
    assert revisions[-1].edit_reason == "wrong_focus"
    assert revisions[-1].source_of_change == "human"


def test_revert_without_a_model_version_is_a_conflict(client, item_id):
    response = client.post(f"/api/v1/items/{item_id}/revert", json={"field": "summary"})

    assert response.status_code == 409
    assert response.json()["code"] == "nothing_to_revert"


def test_a_note_is_stored_against_the_card(client, file_db, item_id):
    response = client.post(
        f"/api/v1/items/{item_id}/notes",
        json={"body": "вынести на совещание 14-го", "author": "young"},
    )

    assert response.status_code == 201
    assert [n.body for n in file_db.notes.list(item_id)] == ["вынести на совещание 14-го"]


def test_the_card_view_carries_everything_the_ui_needs(client, item_id):
    body = client.get(f"/api/v1/items/{item_id}").json()

    assert set(body) >= {
        "item",
        "entities",
        "sources",
        "events",
        "revisions",
        "notes",
        "tags",
        "model_proposals",
    }


def test_a_manual_card_can_be_added_over_http(client, file_db):
    response = client.post(
        "/api/v1/items",
        json={"title": "Материал с мероприятия", "run_llm": False},
    )

    assert response.status_code == 201
    assert response.json()["origin"] == "manual"
    assert file_db.items.get(response.json()["id"]).origin == "manual"


def test_a_duplicate_manual_card_is_a_conflict(client):
    payload = {"title": "Законопроект", "url": "https://sozd.duma.gov.ru/bill/1-8", "run_llm": False}
    client.post("/api/v1/items", json=payload)

    response = client.post("/api/v1/items", json=payload)

    assert response.status_code == 409
    assert response.json()["code"] == "possible_duplicate"
    assert response.json()["details"]["item_id"]
