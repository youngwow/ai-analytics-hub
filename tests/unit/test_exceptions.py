"""src/exceptions.py — класс ошибки задаёт HTTP-статус и машинный `code`.

Сервис бросает подкласс `AppError`, единственный обработчик в `create_app()`
превращает его в `application/problem+json` (RFC 7807, принцип III). Здесь
проверяется и сама иерархия, и то, что доходит до клиента через реальный маршрут.
"""

from __future__ import annotations

import pytest

from src.dependencies import get_source_service
from src.exceptions import (
    AppError,
    InvalidCursorError,
    ItemError,
    ItemNotFoundError,
    ItemValidationError,
    NetworkUnreachableError,
    NothingToRevertError,
    PossibleDuplicateError,
    QueryError,
    QueryValidationError,
    RepositoryUnavailableError,
    SourceError,
    SourceExistsError,
    SourceNotFoundError,
    SourceValidationError,
    TelegramPreviewUnavailableError,
    UnsupportedSourceError,
)

PROBLEM = "application/problem+json"

LEAVES = [
    (SourceValidationError, 400, "validation_error", SourceError),
    (SourceExistsError, 409, "source_exists", SourceError),
    (SourceNotFoundError, 404, "source_not_found", SourceError),
    (UnsupportedSourceError, 422, "unsupported_source", SourceError),
    (NetworkUnreachableError, 502, "network_unreachable", SourceError),
    (TelegramPreviewUnavailableError, 502, "telegram_preview_unavailable", SourceError),
    (ItemValidationError, 400, "validation_error", ItemError),
    (ItemNotFoundError, 404, "item_not_found", ItemError),
    (PossibleDuplicateError, 409, "possible_duplicate", ItemError),
    (NothingToRevertError, 409, "nothing_to_revert", ItemError),
    (QueryValidationError, 400, "validation_error", QueryError),
    (InvalidCursorError, 400, "invalid_cursor", QueryError),
    (RepositoryUnavailableError, 503, "repository_unavailable", AppError),
]


# ── иерархия ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("cls", "status", "code", "family"), LEAVES, ids=[leaf[0].__name__ for leaf in LEAVES]
)
def test_each_leaf_maps_to_one_status_and_one_code(cls, status, code, family):
    error = cls("что-то пошло не так")

    assert (error.status_code, error.code) == (status, code)
    assert isinstance(error, family)
    assert isinstance(error, AppError)


@pytest.mark.parametrize("family", [SourceError, ItemError, QueryError], ids=lambda c: c.__name__)
def test_a_family_base_is_a_400_validation_error_by_default(family):
    error = family("текст")
    assert (error.status_code, error.code) == (400, "validation_error")


def test_a_query_error_is_no_longer_a_value_error():
    """`except ValueError` в вызывающем коде больше не глотает ошибку фильтра."""
    assert not issubclass(QueryError, ValueError)


# ── экземпляр ──────────────────────────────────────────────────────────────


def test_message_is_an_alias_of_detail():
    error = ItemNotFoundError("карточка #7 не найдена")

    assert error.message == error.detail == "карточка #7 не найдена"
    assert str(error) == "карточка #7 не найдена"


def test_details_default_to_an_empty_dict_and_are_kept_when_given():
    assert SourceNotFoundError("нет").details == {}
    assert PossibleDuplicateError("похоже", {"item_id": 3}).details == {"item_id": 3}


def test_a_missing_detail_falls_back_to_the_class_docstring():
    assert SourceNotFoundError().detail == "Источник не найден."
    assert AppError().detail == "Внутренняя ошибка."


def test_the_bare_base_is_an_internal_error():
    assert (AppError.status_code, AppError.code) == (500, "internal_error")


# ── через HTTP: единый обработчик ──────────────────────────────────────────


def test_a_raised_app_error_becomes_a_problem_document(client):
    response = client.get("/api/v1/sources/999")

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM
    assert response.json() == {
        "type": "about:blank",
        "title": "Не найдено",
        "status": 404,
        "detail": "источник #999 не найден",
        "code": "source_not_found",
    }


@pytest.mark.parametrize(
    ("error", "status", "title"),
    [
        (SourceExistsError("уже добавлен", {"source_id": 7}), 409, "Конфликт"),
        (NetworkUnreachableError("нет сети", {"host": "a.ru"}), 502, "Внешний источник недоступен"),
        (RepositoryUnavailableError("база закрыта", {"path": "hub.db"}), 503, "Сервис не готов"),
    ],
    ids=["exists", "network", "repository"],
)
def test_code_and_details_travel_with_the_problem_document(app, client, error, status, title):
    """Подробности нужны UI, чтобы ветвиться по коду, а не по тексту (R-02)."""

    class FailingSourceService:
        def get(self, source_id: int):
            raise error

    app.dependency_overrides[get_source_service] = FailingSourceService

    response = client.get("/api/v1/sources/1")

    assert response.status_code == status
    assert response.headers["content-type"] == PROBLEM
    body = response.json()
    assert (body["title"], body["status"], body["detail"]) == (title, status, error.detail)
    assert (body["code"], body["details"]) == (error.code, error.details)
