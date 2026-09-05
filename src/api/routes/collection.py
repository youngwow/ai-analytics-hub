"""Сбор: автоматический мониторинг (фоновый цикл) и разовый проход по источникам."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, status

from ...dependencies import CollectionServiceDep, ConfigDep, PathsDep, TavilyKeyDep
from ...models.requests import CollectionRunRequest, CollectionStartRequest
from ...models.responses import CollectionStatusResponse
from ...services.collection_service import run_collection_task

router = APIRouter(prefix="/collection", tags=["collection"])


@router.get("", response_model=CollectionStatusResponse, summary="Запущен ли мониторинг, когда следующий тик")
def collection_status(service: CollectionServiceDep) -> CollectionStatusResponse:
    return CollectionStatusResponse.model_validate(service.status())


@router.post("/start", response_model=CollectionStatusResponse, summary="Запустить автоматический мониторинг")
def start_collection(
    payload: CollectionStartRequest, service: CollectionServiceDep
) -> CollectionStatusResponse:
    return CollectionStatusResponse.model_validate(service.start(payload.interval_seconds))


@router.post("/stop", response_model=CollectionStatusResponse, summary="Остановить автоматический мониторинг")
def stop_collection(service: CollectionServiceDep) -> CollectionStatusResponse:
    return CollectionStatusResponse.model_validate(service.stop())


@router.post(
    "/runs",
    response_model=CollectionStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Разовый цикл сбора прямо сейчас (в фоне)",
)
def run_collection(
    payload: CollectionRunRequest,
    service: CollectionServiceDep,
    background: BackgroundTasks,
    config: ConfigDep,
    paths: PathsDep,
    tavily_key: TavilyKeyDep,
) -> CollectionStatusResponse:
    service.ensure_idle()
    background.add_task(
        run_collection_task,
        config,
        paths,
        tavily_key=tavily_key,
        due_only=payload.due_only,
        source_ids=payload.source_ids,
        backfill=payload.backfill,
        force=payload.force,
    )
    return CollectionStatusResponse.model_validate(service.status())
