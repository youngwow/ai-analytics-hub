"""Читающие маршруты этапа 1.3: справочники, документы без карточки, дайджест, состояние."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ...api.query import digest_query, document_query
from ...dependencies import FeedServiceDep
from ...models.requests import DigestRequest
from ...models.responses import DigestResponse, DocumentsResponse, FiltersResponse, StatusResponse

router = APIRouter(tags=["feed"])


@router.get("/filters", response_model=FiltersResponse, summary="Справочники для панели фильтров")
def filters(service: FeedServiceDep) -> FiltersResponse:
    return FiltersResponse.model_validate(service.filters())


@router.get("/documents", response_model=DocumentsResponse, summary="Собрано, но ещё не обработано")
def documents(request: Request, service: FeedServiceDep) -> DocumentsResponse:
    query = document_query(request, service.timezone_name)
    return DocumentsResponse.model_validate(service.documents(query))


@router.post("/digest", response_model=DigestResponse, summary="Выгрузка среза для отправки руководителю")
def digest(payload: DigestRequest, service: FeedServiceDep) -> DigestResponse:
    query = digest_query(payload.filters or {}, service.timezone_name)
    result = service.digest(
        query, fmt=payload.format, title=payload.title, include_notes=payload.include_notes
    )
    return DigestResponse.model_validate(result)


@router.get("/status", response_model=StatusResponse, summary="Состояние сбора и обработки")
def status(service: FeedServiceDep) -> StatusResponse:
    return StatusResponse.model_validate(service.status())
