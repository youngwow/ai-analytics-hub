"""Ожидаемые ошибки сервисов: класс задаёт HTTP-статус и машинный `code`.

Схема из шаблона Model-Service-Repository: сервис бросает подкласс `AppError`,
единственный обработчик в `create_app()` превращает его в `application/problem+json`
(RFC 7807, принцип III конституции). Здесь нет ни FastAPI, ни SQLite — иерархию
видят и CLI, и тесты сервисов.

Промежуточные базы `SourceError` / `ItemError` / `QueryError` сохранены нарочно:
CLI ловит их семействами, а тесты проверяют `pytest.raises(SourceError)`.
"""

from __future__ import annotations


class AppError(Exception):
    """Внутренняя ошибка."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, detail: str | None = None, details: dict | None = None) -> None:
        self.detail = detail or self.__class__.__doc__ or "Внутренняя ошибка"
        self.details = details or {}
        super().__init__(self.detail)

    @property
    def message(self) -> str:
        """Старое имя поля: так его печатает CLI и читают существующие тесты."""
        return self.detail


class RepositoryUnavailableError(AppError):
    """Хранилище недоступно."""

    status_code = 503
    code = "repository_unavailable"


# ── источники ──────────────────────────────────────────────────────────────


class SourceError(AppError):
    """Ошибка управления источниками."""

    status_code = 400
    code = "validation_error"


class SourceValidationError(SourceError):
    """Запрос к источнику не проходит проверку."""

    status_code = 400
    code = "validation_error"


class SourceExistsError(SourceError):
    """Источник уже существует."""

    status_code = 409
    code = "source_exists"


class SourceNotFoundError(SourceError):
    """Источник не найден."""

    status_code = 404
    code = "source_not_found"


class UnsupportedSourceError(SourceError):
    """Не удалось определить, как опрашивать адрес."""

    status_code = 422
    code = "unsupported_source"


class NetworkUnreachableError(SourceError):
    """Источник недоступен по сети."""

    status_code = 502
    code = "network_unreachable"


class TelegramPreviewUnavailableError(SourceError):
    """Веб-превью Telegram-канала недоступно."""

    status_code = 502
    code = "telegram_preview_unavailable"


# ── карточки ───────────────────────────────────────────────────────────────


class ItemError(AppError):
    """Ошибка работы с карточкой."""

    status_code = 400
    code = "validation_error"


class ItemValidationError(ItemError):
    """Запрос к карточке не проходит проверку."""

    status_code = 400
    code = "validation_error"


class ItemNotFoundError(ItemError):
    """Карточка не найдена."""

    status_code = 404
    code = "item_not_found"


class PossibleDuplicateError(ItemError):
    """Похоже на уже существующую карточку."""

    status_code = 409
    code = "possible_duplicate"


class NothingToRevertError(ItemError):
    """У карточки нет версии модели для этого поля."""

    status_code = 409
    code = "nothing_to_revert"


class NoDuplicateProposalError(ItemError):
    """Отклонять нечего: у карточки нет открытого предложения «вероятный дубль»."""

    status_code = 409
    code = "no_duplicate_proposal"


# ── профиль компании ───────────────────────────────────────────────────────


class ProfileError(AppError):
    """Ошибка работы с профилем компании."""

    status_code = 400
    code = "validation_error"


class ProfileValidationError(ProfileError):
    """Профиль не проходит проверку."""

    status_code = 400
    code = "validation_error"


class ProfileNotFoundError(ProfileError):
    """Профиль компании не найден."""

    status_code = 404
    code = "profile_not_found"


# ── обработка (очередь ИИ) ─────────────────────────────────────────────────


class ProcessingError(AppError):
    """Ошибка запуска обработки."""

    status_code = 400
    code = "validation_error"


class ProcessingValidationError(ProcessingError):
    """Параметры прогона не проходят проверку."""

    status_code = 400
    code = "validation_error"


class RunNotFoundError(ProcessingError):
    """Прогон обработки не найден."""

    status_code = 404
    code = "run_not_found"


class ProcessingBusyError(ProcessingError):
    """Обработка уже идёт — дождитесь завершения прогона."""

    status_code = 409
    code = "processing_busy"


# ── сбор (автоматический мониторинг) ───────────────────────────────────────


class CollectionError(AppError):
    """Ошибка управления сбором."""

    status_code = 400
    code = "validation_error"


class CollectionValidationError(CollectionError):
    """Параметры сбора не проходят проверку."""

    status_code = 400
    code = "validation_error"


class CollectionRunningError(CollectionError):
    """Автоматический мониторинг уже запущен."""

    status_code = 409
    code = "collection_running"


class CollectionNotRunningError(CollectionError):
    """Автоматический мониторинг не запущен."""

    status_code = 409
    code = "collection_not_running"


class CollectionBusyError(CollectionError):
    """Цикл сбора уже выполняется — дождитесь его конца."""

    status_code = 409
    code = "collection_busy"


# ── фильтр ленты ───────────────────────────────────────────────────────────


class QueryError(AppError):
    """Фильтр ленты не собрать."""

    status_code = 400
    code = "validation_error"


class QueryValidationError(QueryError):
    """Параметры фильтра не проходят проверку."""

    status_code = 400
    code = "validation_error"


class InvalidCursorError(QueryError):
    """Курсор не разбирается или относится к другому срезу."""

    status_code = 400
    code = "invalid_cursor"
