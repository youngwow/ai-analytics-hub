"""Очередь ИИ: запустить прогон обработки и следить за ним."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Path, Query, status

from ...dependencies import ConfigDep, LLMProviderDep, PathsDep, ProcessingServiceDep
from ...models.requests import ProcessingRunRequest
from ...models.responses import (
    ProcessingRunListResponse,
    ProcessingRunResponse,
    ProcessingStatusResponse,
    QualityResponse,
)
from ...services.processing_service import run_in_background

router = APIRouter(prefix="/processing", tags=["processing"])

RunId = Annotated[int, Path(description="Идентификатор прогона")]


@router.get("", response_model=ProcessingStatusResponse, summary="Идёт ли обработка и что в очереди")
def processing_status(service: ProcessingServiceDep) -> ProcessingStatusResponse:
    return ProcessingStatusResponse.from_domain(service.queue_status())


@router.get("/runs", response_model=ProcessingRunListResponse, summary="История прогонов")
def list_runs(
    service: ProcessingServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> ProcessingRunListResponse:
    return ProcessingRunListResponse(
        runs=[ProcessingRunResponse.from_domain(r) for r in service.list_runs(limit)]
    )


@router.post(
    "/runs",
    response_model=ProcessingRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Обработать очередь: прогон уходит в фон, ответ — его запись",
)
def start_run(
    payload: ProcessingRunRequest,
    service: ProcessingServiceDep,
    background: BackgroundTasks,
    config: ConfigDep,
    paths: PathsDep,
    provider: LLMProviderDep,
) -> ProcessingRunResponse:
    run = service.enqueue(
        limit=payload.limit,
        source_id=payload.source_id,
        since=payload.since,
        profile_id=payload.profile_id,
        force=payload.force,
        only_failed=payload.only_failed,
        trigger="api",
    )
    # Прогон идёт после ответа на своём соединении; провайдер модели — общий на процесс.
    background.add_task(run_in_background, config, paths, run.id, run.params, provider)
    return ProcessingRunResponse.from_domain(run)


@router.get(
    "/quality",
    response_model=QualityResponse,
    summary="Сводка качества: карточки, вызовы модели по этапам и суткам",
)
def quality(
    service: ProcessingServiceDep,
    since: Annotated[str | None, Query(description="ISO-дата начала окна")] = None,
    until: Annotated[str | None, Query(description="ISO-дата конца окна")] = None,
) -> QualityResponse:
    return QualityResponse.model_validate(service.quality_summary(since=since, until=until))


@router.get("/runs/{run_id}", response_model=ProcessingRunResponse, summary="Один прогон")
def get_run(run_id: RunId, service: ProcessingServiceDep) -> ProcessingRunResponse:
    return ProcessingRunResponse.from_domain(service.get_run(run_id))


@router.post("/runs/{run_id}/stop", response_model=ProcessingRunResponse,
             summary="Остановить прогон после завершения текущих запросов модели")
def stop_run(run_id: RunId, service: ProcessingServiceDep) -> ProcessingRunResponse:
    return ProcessingRunResponse.from_domain(service.stop_run(run_id))
