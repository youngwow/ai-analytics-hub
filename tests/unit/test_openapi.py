"""Схема OpenAPI: у каждого успешного ответа есть схема, `/docs` управляется через `DOCS`.

`response_model` стоит на каждом маршруте — иначе фронтенд не сгенерирует типы.
Порядок путей тоже контракт: `/items/facets` объявлен раньше `/items/{item_id}` (R-09).
Фоновые задачи (прогон обработки, разовый цикл сбора) отвечают 202: запись
создана, работа идёт после ответа.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.config import get_settings
from src.main import create_app

# Успешный ответ маршрута — ровно один из этих кодов, по смыслу операции.
SUCCESS_CODES = ("200", "201", "202")

CREATED = [
    "POST /api/v1/items",
    "POST /api/v1/items/{item_id}/events",
    "POST /api/v1/items/{item_id}/notes",
    "POST /api/v1/sources",
]
ACCEPTED = [
    "POST /api/v1/collection/runs",
    "POST /api/v1/processing/runs",
]
# Маршруты, появившиеся вместе с очередью ИИ, мониторингом, архивом и хронологией.
ADDED_OPERATIONS = {
    "GET /api/v1/processing",
    "GET /api/v1/processing/runs",
    "POST /api/v1/processing/runs",
    "GET /api/v1/processing/runs/{run_id}",
    "GET /api/v1/collection",
    "POST /api/v1/collection/start",
    "POST /api/v1/collection/stop",
    "POST /api/v1/collection/runs",
    "POST /api/v1/items/{item_id}/events",
    "POST /api/v1/items/{item_id}/archive",
    "POST /api/v1/items/{item_id}/unarchive",
}


def _operations(schema: dict) -> dict[str, dict]:
    return {
        f"{method.upper()} {path}": operation
        for path, methods in schema["paths"].items()
        for method, operation in methods.items()
    }


def _success(operation: dict) -> dict | None:
    for code in SUCCESS_CODES:
        if code in operation["responses"]:
            return operation["responses"][code]
    return None


def _schema_ref(operation: dict, code: str) -> str:
    return operation["responses"][code]["content"]["application/json"]["schema"]["$ref"]


def test_every_operation_has_a_success_response_with_a_named_schema(client):
    schema = client.get("/openapi.json").json()

    operations = _operations(schema)

    assert len(operations) >= 39
    for name, operation in operations.items():
        success = _success(operation)
        assert success is not None, name
        assert "content" in success, name
        assert "$ref" in success["content"]["application/json"]["schema"], name


def test_no_operation_declares_two_success_codes(client):
    operations = _operations(client.get("/openapi.json").json())

    doubled = {
        name: [code for code in SUCCESS_CODES if code in op["responses"]]
        for name, op in operations.items()
        if sum(code in op["responses"] for code in SUCCESS_CODES) > 1
    }

    assert doubled == {}


def test_created_resources_answer_201_background_jobs_202_and_the_rest_200(client):
    operations = _operations(client.get("/openapi.json").json())

    created = sorted(name for name, op in operations.items() if "201" in op["responses"])
    accepted = sorted(name for name, op in operations.items() if "202" in op["responses"])
    rest = [name for name in operations if name not in created and name not in accepted]

    assert created == CREATED
    assert accepted == ACCEPTED
    assert [name for name in rest if "200" not in operations[name]["responses"]] == []


def test_the_new_routes_are_all_declared(client):
    operations = _operations(client.get("/openapi.json").json())

    assert ADDED_OPERATIONS <= set(operations)


@pytest.mark.parametrize(
    ("name", "code", "ref"),
    [
        ("POST /api/v1/processing/runs", "202", "#/components/schemas/ProcessingRunResponse"),
        ("GET /api/v1/processing", "200", "#/components/schemas/ProcessingStatusResponse"),
        ("GET /api/v1/processing/runs", "200", "#/components/schemas/ProcessingRunListResponse"),
        ("POST /api/v1/collection/runs", "202", "#/components/schemas/CollectionStatusResponse"),
        ("POST /api/v1/collection/start", "200", "#/components/schemas/CollectionStatusResponse"),
        ("POST /api/v1/items/{item_id}/events", "201", "#/components/schemas/NpaEventResponse"),
        ("POST /api/v1/items/{item_id}/archive", "200", "#/components/schemas/ArchiveResponse"),
        ("POST /api/v1/items/{item_id}/unarchive", "200", "#/components/schemas/ArchiveResponse"),
    ],
    ids=[
        "processing-run",
        "processing-status",
        "processing-history",
        "collection-run",
        "collection-start",
        "npa-event",
        "archive",
        "unarchive",
    ],
)
def test_the_new_routes_are_declared_with_their_response_schema(client, name, code, ref):
    operations = _operations(client.get("/openapi.json").json())

    assert _schema_ref(operations[name], code) == ref


def test_the_refresh_route_is_declared_with_the_run_schema(client):
    operations = _operations(client.get("/openapi.json").json())

    refresh = operations["POST /api/v1/sources/{source_id}/refresh"]

    assert refresh["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SourceRunResponse"
    }


def test_the_documents_schema_carries_the_cursor(client):
    schema = client.get("/openapi.json").json()

    documents = schema["components"]["schemas"]["DocumentsResponse"]["properties"]

    assert "next_cursor" in documents


def test_the_literal_item_paths_come_before_the_parametrised_one(client):
    paths = list(client.get("/openapi.json").json()["paths"])

    assert paths.index("/api/v1/items/facets") < paths.index("/api/v1/items/{item_id}")
    assert paths.index("/api/v1/items/bulk") < paths.index("/api/v1/items/{item_id}")


def test_the_literal_processing_path_comes_before_the_parametrised_one(client):
    paths = list(client.get("/openapi.json").json()["paths"])

    assert paths.index("/api/v1/processing/runs") < paths.index("/api/v1/processing/runs/{run_id}")


def test_docs_are_served_by_default(client):
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_docs_can_be_switched_off_from_the_env_file(tmp_path, hub_paths, monkeypatch):
    monkeypatch.delenv("DOCS", raising=False)
    (tmp_path / ".env").write_text("DOCS=false\n", encoding="utf-8")
    get_settings.cache_clear()

    with TestClient(create_app()) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/api/v1/health").status_code == 200
