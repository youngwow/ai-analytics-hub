"""src/dependencies.py — связывание слоёв: соединение на запрос, провайдер на процесс.

Подменяются функции-провайдеры (`app.dependency_overrides[get_llm_provider]`),
никогда — псевдонимы `Annotated`. Соединение SQLite открывается на запрос и
закрывается после ответа (R-03), провайдер модели живёт весь процесс.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient
from support import FAKE_MODEL, NEWS_TEXT, news_answer

from src import dependencies
from src.config import get_paths
from src.dependencies import get_database, get_llm_provider
from src.main import create_app
from src.repositories import Database

# ── соединение на запрос ───────────────────────────────────────────────────


def test_get_database_yields_a_connection_to_the_hub_root_database(hub_paths):
    generator = get_database(get_paths())

    db = next(generator)

    assert isinstance(db, Database)
    assert db.path == hub_paths.db_path
    assert db.ping() is True


def test_get_database_closes_the_connection_when_the_request_is_over(hub_paths):
    generator = get_database(get_paths())
    db = next(generator)

    with pytest.raises(StopIteration):
        next(generator)

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.conn.execute("SELECT 1")


def test_get_database_closes_the_connection_even_when_the_handler_raised(hub_paths):
    generator = get_database(get_paths())
    db = next(generator)

    with pytest.raises(RuntimeError, match="обработчик упал"):
        generator.throw(RuntimeError("обработчик упал"))

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.conn.execute("SELECT 1")


def test_every_request_gets_its_own_connection(hub_paths):
    first = next(get_database(get_paths()))
    second = next(get_database(get_paths()))

    assert first is not second
    assert first.conn is not second.conn
    first.close()
    second.close()


# ── провайдер модели на процесс ────────────────────────────────────────────


def test_the_llm_provider_is_none_without_a_key(hub_paths):
    assert get_llm_provider() is None
    assert get_llm_provider() is None  # и второй раз — из кэша


def test_the_llm_provider_is_built_once_from_the_env_file_under_hub_root(
    tmp_path, hub_paths, monkeypatch
):
    (tmp_path / ".env").write_text("OLLAMA_API_KEY=secret-from-tmp\n", encoding="utf-8")
    built = []

    class Provider:
        pass

    def build(llm_config, key):
        built.append((llm_config.model, key))
        return Provider()

    monkeypatch.setattr(dependencies, "build_provider", build)
    get_llm_provider.cache_clear()

    first = get_llm_provider()
    second = get_llm_provider()

    assert isinstance(first, Provider)
    assert first is second
    assert built == [("glm-5.3-flash", "secret-from-tmp")]


# ── подмена доходит до сервиса ─────────────────────────────────────────────


def test_a_dependency_override_reaches_the_processing_service(client, file_db, fake_llm):
    response = client.post(
        "/api/v1/items",
        json={"title": "Материал с мероприятия", "raw_text": NEWS_TEXT, "run_llm": True},
    )

    assert response.status_code == 201
    assert response.json()["processing_status"] == "done"
    assert fake_llm.calls == 1
    assert "рекомендательного сервиса" in fake_llm.prompts[0]
    item = file_db.items.get(response.json()["id"])
    assert (item.model_name, item.degraded) == (FAKE_MODEL, False)
    assert item.reasoning == news_answer()["reasoning"]


def test_without_an_override_and_a_key_the_card_is_built_without_the_model(hub_paths):
    """Ключа в tmp-`.env` нет → провайдер `None` → карточка деградированная, но не потерянная."""
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/items",
            json={"title": "Материал с мероприятия", "raw_text": NEWS_TEXT, "run_llm": True},
        )

        assert response.status_code == 201
        item = client.get(f"/api/v1/items/{response.json()['id']}").json()["item"]

    assert item["degraded"] is True
    assert item["model_name"] == ""
    assert item["reasoning"] == "Карточка заведена вручную, модель не вызывалась."
