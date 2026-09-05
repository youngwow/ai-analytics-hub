"""GET /api/v1/health и /health/ready — живость и готовность процесса.

Живость не делает ввода-вывода: процесс отвечает — значит «ok». Готовность
опрашивает хранилище и при деградации отвечает 503 с тем же телом, чтобы
оркестратор вывел экземпляр из ротации, а шапка дашборда показала причину.
"""

from __future__ import annotations

import pytest

from src import __version__
from src.config import Settings
from src.models.responses import HealthResponse
from src.repositories import Database
from src.services.health import HealthService


def _raising_ping(self):
    raise RuntimeError("база закрыта")


def _false_ping(self):
    return False


# ── живость ────────────────────────────────────────────────────────────────


def test_liveness_is_ok_with_empty_checks(client):
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "app": "ai-analytics-hub",
        "version": __version__,
        "environment": "local",
        "checks": {},
    }


def test_liveness_does_not_look_at_the_repository(client, monkeypatch):
    monkeypatch.setattr(Database, "ping", _raising_ping)

    response = client.get("/api/v1/health")

    assert (response.status_code, response.json()["status"]) == (200, "ok")
    assert response.json()["checks"] == {}


# ── готовность ─────────────────────────────────────────────────────────────


def test_readiness_reports_the_repository_as_ok(client):
    response = client.get("/api/v1/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"] == {"repository": "ok"}
    assert HealthResponse.model_validate(body) == HealthResponse.model_validate(
        client.get("/api/v1/health").json()
    ).model_copy(update={"checks": {"repository": "ok"}})


@pytest.mark.parametrize(
    ("ping", "check"),
    [(_raising_ping, "error"), (_false_ping, "unavailable")],
    ids=["ping-raises", "ping-false"],
)
def test_readiness_is_503_and_degraded_when_the_repository_fails(client, monkeypatch, ping, check):
    monkeypatch.setattr(Database, "ping", ping)

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert (body["status"], body["checks"]) == ("degraded", {"repository": check})
    assert HealthResponse.model_validate(body).app == "ai-analytics-hub"


# ── сервис без HTTP ────────────────────────────────────────────────────────


def test_the_service_reads_app_name_and_environment_from_settings(db):
    settings = Settings(_env_file=None, app_name="hub-test", environment="dev")

    result = HealthService(settings, db).readiness()

    assert (result.status, result.app, result.environment) == ("ok", "hub-test", "dev")
    assert result.version == __version__


def test_the_service_never_lets_a_repository_exception_escape(db, monkeypatch):
    monkeypatch.setattr(Database, "ping", _raising_ping)

    result = HealthService(Settings(_env_file=None), db).readiness()

    assert (result.status, result.checks) == ("degraded", {"repository": "error"})
