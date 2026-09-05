"""Схема OpenAPI: у каждого успешного ответа есть схема, `/docs` управляется через `DOCS`.

`response_model` стоит на каждом маршруте — иначе фронтенд не сгенерирует типы.
Порядок путей тоже контракт: `/items/facets` объявлен раньше `/items/{item_id}` (R-09).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.config import get_settings
from src.main import create_app


def _operations(schema: dict) -> dict[str, dict]:
    return {
        f"{method.upper()} {path}": operation
        for path, methods in schema["paths"].items()
        for method, operation in methods.items()
    }


def test_every_operation_has_a_success_response_with_a_named_schema(client):
    schema = client.get("/openapi.json").json()

    operations = _operations(schema)

    assert len(operations) >= 26
    for name, operation in operations.items():
        success = operation["responses"].get("200") or operation["responses"].get("201")
        assert success is not None, name
        assert "content" in success, name
        assert "$ref" in success["content"]["application/json"]["schema"], name


def test_created_resources_answer_201_and_the_rest_200(client):
    operations = _operations(client.get("/openapi.json").json())

    created = sorted(name for name, op in operations.items() if "201" in op["responses"])

    assert created == [
        "POST /api/v1/items",
        "POST /api/v1/items/{item_id}/notes",
        "POST /api/v1/sources",
    ]


def test_the_refresh_route_is_declared_with_the_run_schema(client):
    operations = _operations(client.get("/openapi.json").json())

    refresh = operations["POST /api/v1/sources/{source_id}/refresh"]

    assert refresh["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SourceRunResponse"
    }


def test_the_literal_item_paths_come_before_the_parametrised_one(client):
    paths = list(client.get("/openapi.json").json()["paths"])

    assert paths.index("/api/v1/items/facets") < paths.index("/api/v1/items/{item_id}")
    assert paths.index("/api/v1/items/bulk") < paths.index("/api/v1/items/{item_id}")


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
