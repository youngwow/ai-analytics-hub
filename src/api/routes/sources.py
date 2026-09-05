"""Источники: probe, жизненный цикл, расписание, внеочередной опрос, здоровье."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Path, Query, status

from ...dependencies import CollectorDep, ConfigDep, PathsDep, SourceServiceDep, TavilyKeyDep
from ...models.requests import ProbeRequest, SourceCreateRequest, SourceUpdateRequest
from ...models.responses import (
    ProbeResponse,
    SourceDeleteResponse,
    SourceHealthResponse,
    SourceListResponse,
    SourceResponse,
    SourceRunResponse,
)
from ...services.source_service import run_backfill

router = APIRouter(prefix="/sources", tags=["sources"])

SourceId = Annotated[int, Path(description="Идентификатор источника")]


@router.post("/probe", response_model=ProbeResponse, summary="Распознать ссылку, ничего не сохраняя")
def probe(payload: ProbeRequest, service: SourceServiceDep) -> ProbeResponse:
    return ProbeResponse.from_domain(service.probe(payload.url))


@router.post(
    "",
    response_model=SourceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Создать источник",
)
def create_source(
    payload: SourceCreateRequest,
    service: SourceServiceDep,
    background: BackgroundTasks,
    config: ConfigDep,
    paths: PathsDep,
    tavily_key: TavilyKeyDep,
) -> SourceResponse:
    source = service.create(
        payload.url,
        title=payload.title,
        kind=payload.type,
        poll_interval=payload.poll_interval,
        category_hint=payload.category_hint,
        created_by=payload.created_by,
    )
    if payload.backfill_limit and source.id is not None:
        # Первичный сбор после ответа: своё соединение, соединение запроса закрыто.
        background.add_task(run_backfill, config, paths, source.id, tavily_key=tavily_key)
    return SourceResponse.from_domain(source)


@router.get("", response_model=SourceListResponse, summary="Список источников")
def list_sources(
    service: SourceServiceDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    kind: Annotated[str | None, Query()] = None,
) -> SourceListResponse:
    sources = service.list(status=status_filter, kind=kind)
    return SourceListResponse(sources=[SourceResponse.from_domain(s) for s in sources])


@router.get("/{source_id}", response_model=SourceResponse, summary="Один источник")
def get_source(source_id: SourceId, service: SourceServiceDep) -> SourceResponse:
    return SourceResponse.from_domain(service.get(source_id))


@router.patch(
    "/{source_id}",
    response_model=SourceResponse,
    summary="Переименовать, сменить частоту, поставить на паузу",
)
def update_source(
    source_id: SourceId, payload: SourceUpdateRequest, service: SourceServiceDep
) -> SourceResponse:
    source = service.update(
        source_id,
        name=payload.title,
        poll_interval=payload.poll_interval,
        category_hint=payload.category_hint,
        status=payload.status,
        url=payload.url,
        kind=payload.type,
        fetch_url=payload.fetch_url,
    )
    return SourceResponse.from_domain(source)


@router.delete(
    "/{source_id}", response_model=SourceDeleteResponse, summary="Мягкое удаление: материалы остаются"
)
def delete_source(
    source_id: SourceId,
    service: SourceServiceDep,
    purge_items: Annotated[bool, Query()] = False,
) -> SourceDeleteResponse:
    return SourceDeleteResponse(**service.soft_delete(source_id, purge_items=purge_items))


@router.post("/{source_id}/restore", response_model=SourceResponse, summary="Вернуть удалённый источник")
def restore_source(source_id: SourceId, service: SourceServiceDep) -> SourceResponse:
    return SourceResponse.from_domain(service.restore(source_id))


@router.post(
    "/{source_id}/refresh", response_model=SourceRunResponse, summary="Опросить источник прямо сейчас"
)
def refresh_source(
    source_id: SourceId, service: SourceServiceDep, collector: CollectorDep
) -> SourceRunResponse:
    return SourceRunResponse.from_domain(service.refresh(source_id, collector))


@router.get(
    "/{source_id}/health", response_model=SourceHealthResponse, summary="История опросов и последняя ошибка"
)
def source_health(
    source_id: SourceId,
    service: SourceServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> SourceHealthResponse:
    data = service.health(source_id, limit)
    return SourceHealthResponse(
        source=SourceResponse.from_domain(data["source"]),
        documents=data["documents"],
        consecutive_failures=data["consecutive_failures"],
        last_success_at=data["last_success_at"],
        last_error=data["last_error"],
        runs=[SourceRunResponse.from_domain(r) for r in data["runs"]],
    )
