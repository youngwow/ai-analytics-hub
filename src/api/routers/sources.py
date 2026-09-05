"""Источники: probe, жизненный цикл, расписание, здоровье."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request

from ...common import get_logger
from ...models import Source
from ...sources.collector import Collector
from ...sources.manage import SourceService
from ...storage import Database
from ..schemas import ProbeRequest, SourceCreate, SourceUpdate
from ._common import get_sources

log = get_logger("api")

router = APIRouter(prefix="/api/v1/sources", tags=["sources"])

Service = Annotated[SourceService, Depends(get_sources)]


def _source_json(source: Source) -> dict:
    return asdict(source)


@router.post("/probe", summary="Распознать ссылку, ничего не сохраняя")
def probe(payload: ProbeRequest, svc: Service) -> dict:
    return asdict(svc.probe(payload.url))


def _backfill(context, source_id: int) -> None:
    """Первичный сбор после ответа: добавление источника не должно висеть минуту.

    Своё соединение — соединение запроса к этому моменту уже закрыто.
    """
    db = Database(context.paths.db_path)
    try:
        source = db.sources.get(source_id)
        if source is not None:
            Collector(context.config, db, paths=context.paths).collect_one(source)
    except Exception as e:  # фоновая задача не должна ронять процесс
        log.warning("первичный сбор источника #%s не удался: %s", source_id, e)
    finally:
        db.close()


@router.post("", status_code=201, summary="Создать источник")
def create(
    payload: SourceCreate, svc: Service, request: Request, background: BackgroundTasks
) -> dict:
    source = svc.create(
        payload.url,
        title=payload.title,
        kind=payload.type,
        poll_interval=payload.poll_interval,
        category_hint=payload.category_hint,
        created_by=payload.created_by,
    )
    if payload.backfill_limit:
        background.add_task(_backfill, request.app.state.context, source.id)
    return _source_json(source)


@router.get("", summary="Список источников")
def list_sources(
    svc: Service,
    status: str | None = Query(default=None),
    kind: str | None = Query(default=None),
) -> dict:
    return {"sources": [_source_json(s) for s in svc.list(status=status, kind=kind)]}


@router.get("/{source_id}", summary="Один источник")
def get_source(source_id: int, svc: Service) -> dict:
    return _source_json(svc.get(source_id))


@router.patch("/{source_id}", summary="Переименовать, сменить частоту, поставить на паузу")
def update_source(source_id: int, payload: SourceUpdate, svc: Service) -> dict:
    return _source_json(
        svc.update(
            source_id,
            name=payload.title,
            poll_interval=payload.poll_interval,
            category_hint=payload.category_hint,
            status=payload.status,
        )
    )


@router.delete("/{source_id}", summary="Мягкое удаление: материалы остаются")
def delete_source(
    source_id: int, svc: Service, purge_items: bool = Query(default=False)
) -> dict:
    return svc.soft_delete(source_id, purge_items=purge_items)


@router.post("/{source_id}/restore", summary="Вернуть удалённый источник")
def restore_source(source_id: int, svc: Service) -> dict:
    return _source_json(svc.restore(source_id))


@router.get("/{source_id}/health", summary="История опросов и последняя ошибка")
def source_health(source_id: int, svc: Service, limit: int = Query(default=20, le=200)) -> dict:
    data = svc.health(source_id, limit)
    return {
        "source": _source_json(data["source"]),
        "documents": data["documents"],
        "consecutive_failures": data["consecutive_failures"],
        "last_success_at": data["last_success_at"],
        "last_error": data["last_error"],
        "runs": [asdict(r) for r in data["runs"]],
    }
