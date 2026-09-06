"""Читающие маршруты этапа 1.3: фасеты, справочники, документы, дайджест, состояние.

Роутер подключается **до** `items`, чтобы `/items/facets` не попал в обработчик
`/items/{item_id}` и не получил 422 на попытке привести «facets» к `int`
(research.md, R-09).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from ...feed.query import DocumentQuery, FeedQuery
from ...feed.service import FeedService
from ..schemas import DigestRequest
from ._common import get_feed

router = APIRouter(prefix="/api/v1", tags=["feed"])

Service = Annotated[FeedService, Depends(get_feed)]


def feed_query(request: Request, svc: FeedService) -> FeedQuery:
    """Собрать фильтр из строки запроса — одна форма на CLI и HTTP."""
    params = request.query_params
    return FeedQuery.build(
        q=params.get("q"),
        type=params.get("type"),
        npa_status=params.get("npa_status"),
        priority=params.getlist("priority"),
        tags=params.getlist("tag"),
        source_ids=params.getlist("source_id"),
        date_from=params.get("from"),
        date_to=params.get("to"),
        order=params.get("order", "published"),
        limit=params.get("limit"),
        cursor=params.get("cursor"),
        include_hidden=params.get("include_hidden", "").lower() in ("1", "true", "yes"),
        timezone_name=svc.timezone_name,
    )


@router.get("/items/facets", summary="Счётчики по тому же срезу, что и лента")
def facets(request: Request, svc: Service) -> dict:
    return svc.facets(feed_query(request, svc))


@router.get("/filters", summary="Справочники для панели фильтров")
def filters(svc: Service) -> dict:
    return svc.filters()


@router.get("/documents", summary="Собрано, но ещё не обработано")
def documents(
    request: Request,
    svc: Service,
    unprocessed: bool = Query(default=True, description="сейчас поддерживается только true"),
) -> dict:
    params = request.query_params
    query = DocumentQuery.build(
        unsupported={
            "priority": params.getlist("priority"),
            "type": params.get("type"),
            "tag": params.getlist("tag"),
            "npa_status": params.get("npa_status"),
        },
        q=params.get("q"),
        source_ids=params.getlist("source_id"),
        date_from=params.get("from"),
        date_to=params.get("to"),
        limit=params.get("limit"),
        timezone_name=svc.timezone_name,
    )
    return svc.documents(query)


@router.post("/digest", summary="Выгрузка среза для отправки руководителю")
def digest(payload: DigestRequest, svc: Service) -> dict:
    filters = payload.filters or {}
    query = FeedQuery.build(
        q=filters.get("q"),
        type=filters.get("type"),
        npa_status=filters.get("npa_status"),
        priority=filters.get("priority") or [],
        tags=filters.get("tag") or filters.get("tags") or [],
        source_ids=filters.get("source_id") or filters.get("source_ids") or [],
        date_from=filters.get("from"),
        date_to=filters.get("to"),
        order=filters.get("order", "priority"),
        limit=200,
        timezone_name=svc.timezone_name,
    )
    return svc.digest(
        query, fmt=payload.format, title=payload.title, include_notes=payload.include_notes
    )


@router.get("/status", summary="Состояние сбора и обработки")
def status(svc: Service) -> dict:
    return svc.status()
