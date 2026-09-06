"""Живость и готовность процесса."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from ...dependencies import HealthServiceDep
from ...models.responses import HealthResponse

router = APIRouter(prefix="/health", tags=["service"])


@router.get("", response_model=HealthResponse, summary="Процесс жив")
def liveness(service: HealthServiceDep) -> HealthResponse:
    return service.liveness()


@router.get("/ready", response_model=HealthResponse, summary="Зависимости отвечают")
def readiness(response: Response, service: HealthServiceDep) -> HealthResponse:
    """503 при деградации: оркестратор выводит экземпляр из ротации, тело то же."""
    result = service.readiness()
    if result.status != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result
