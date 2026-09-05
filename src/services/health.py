"""HealthService — живость и готовность процесса для оркестратора и шапки дашборда."""

from __future__ import annotations

from .. import __version__
from ..config import Settings
from ..models.responses import HealthResponse
from ..repositories import Database
from ..utils import get_logger

log = get_logger("health")


class HealthService:
    def __init__(self, settings: Settings, db: Database):
        self._settings = settings
        self._db = db

    def liveness(self) -> HealthResponse:
        """Всегда «ok» и без ввода-вывода: процесс жив и отвечает."""
        return self._response(status="ok", checks={})

    def readiness(self) -> HealthResponse:
        """Полная проверка: каждая зависимость отвечает. Иначе «degraded» и 503."""
        checks = {"repository": self._check_repository()}
        status = "ok" if all(value == "ok" for value in checks.values()) else "degraded"
        return self._response(status=status, checks=checks)

    def _check_repository(self) -> str:
        try:
            return "ok" if self._db.ping() else "unavailable"
        except Exception as e:
            log.warning("хранилище не отвечает: %s", e)
            return "error"

    def _response(self, *, status: str, checks: dict[str, str]) -> HealthResponse:
        return HealthResponse(
            status=status,
            app=self._settings.app_name,
            version=__version__,
            environment=self._settings.environment,
            checks=checks,
        )
