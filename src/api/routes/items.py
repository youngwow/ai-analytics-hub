"""Карточки: лента, счётчики, правка, скрытие, история, возврат версии модели, ручное добавление.

Литеральные пути (`/facets`, `/bulk`) объявлены раньше `/{item_id}` — иначе
«facets» уйдёт в разбор `int` и вернёт 422 (research.md, R-09).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Request, status

from ...api.query import feed_query
from ...dependencies import FeedServiceDep, ItemServiceDep, ProcessingServiceDep
from ...models.requests import (
    BulkVisibilityRequest,
    HideRequest,
    ItemCreateRequest,
    ItemUpdateRequest,
    NoteCreateRequest,
    RevertRequest,
)
from ...models.responses import (
    BulkResponse,
    FacetsResponse,
    FeedResponse,
    ItemCardResponse,
    ItemEditResponse,
    ManualItemResponse,
    NoteResponse,
    RevisionListResponse,
    RevisionResponse,
    VisibilityResponse,
)

router = APIRouter(prefix="/items", tags=["items"])

ItemId = Annotated[int, Path(description="Идентификатор карточки")]


@router.get("", response_model=FeedResponse, summary="Лента карточек")
def list_items(request: Request, service: FeedServiceDep) -> FeedResponse:
    """Одна реализация фильтра на CLI и HTTP: разбор строки запроса и вызов `FeedService`."""
    return FeedResponse.model_validate(service.items(feed_query(request, service.timezone_name)))


@router.post(
    "",
    response_model=ManualItemResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ручное добавление материала",
)
def create_item(payload: ItemCreateRequest, service: ProcessingServiceDep) -> ManualItemResponse:
    result = service.add_manual(
        title=payload.title,
        url=payload.url,
        text=payload.raw_text,
        published_at=payload.published_at,
        item_type=payload.type,
        npa_status=payload.npa_status,
        run_llm=payload.run_llm,
        force=payload.force,
    )
    return ManualItemResponse(
        id=result["item_id"],
        document_id=result["document_id"],
        origin="manual",
        processing_status="done" if result["item_id"] else "queued",
    )


@router.get(
    "/facets", response_model=FacetsResponse, tags=["feed"], summary="Счётчики по тому же срезу, что и лента"
)
def facets(request: Request, service: FeedServiceDep) -> FacetsResponse:
    return FacetsResponse.model_validate(service.facets(feed_query(request, service.timezone_name)))


@router.post("/bulk", response_model=BulkResponse, summary="Массовое скрытие под дайджест")
def bulk_visibility(payload: BulkVisibilityRequest, service: ItemServiceDep) -> BulkResponse:
    changed = service.bulk_visibility(payload.item_ids, payload.scope, payload.reason)
    return BulkResponse(changed=changed, scope=payload.scope)


@router.get("/{item_id}", response_model=ItemCardResponse, summary="Карточка целиком")
def get_item(item_id: ItemId, service: ItemServiceDep) -> ItemCardResponse:
    return ItemCardResponse.from_domain(service.get_item(item_id))


@router.patch("/{item_id}", response_model=ItemEditResponse, summary="Правка аналитика")
def update_item(item_id: ItemId, payload: ItemUpdateRequest, service: ItemServiceDep) -> ItemEditResponse:
    fields = payload.model_dump(exclude_none=True)
    reason = fields.pop("edit_reason", "")
    return ItemEditResponse.from_domain(service.edit_item(item_id, fields, reason=reason))


@router.post("/{item_id}/hide", response_model=VisibilityResponse, summary="Скрыть из ленты или из дайджеста")
def hide_item(item_id: ItemId, payload: HideRequest, service: ItemServiceDep) -> VisibilityResponse:
    return VisibilityResponse.from_domain(service.set_visibility(item_id, payload.scope, payload.reason))


@router.post("/{item_id}/unhide", response_model=VisibilityResponse, summary="Вернуть в ленту")
def unhide_item(item_id: ItemId, service: ItemServiceDep) -> VisibilityResponse:
    return VisibilityResponse.from_domain(service.set_visibility(item_id, restore=True))


@router.delete("/{item_id}", response_model=VisibilityResponse, summary="Мягкое удаление карточки")
def delete_item(item_id: ItemId, service: ItemServiceDep) -> VisibilityResponse:
    return VisibilityResponse.from_domain(service.set_visibility(item_id, "deleted"))


@router.post("/{item_id}/restore", response_model=VisibilityResponse, summary="Вернуть удалённую карточку")
def restore_item(item_id: ItemId, service: ItemServiceDep) -> VisibilityResponse:
    return VisibilityResponse.from_domain(service.set_visibility(item_id, restore=True))


@router.get(
    "/{item_id}/revisions", response_model=RevisionListResponse, summary="История правок человека и модели"
)
def revisions(item_id: ItemId, service: ItemServiceDep) -> RevisionListResponse:
    return RevisionListResponse(
        revisions=[RevisionResponse.from_domain(r) for r in service.revisions(item_id)]
    )


@router.post("/{item_id}/revert", response_model=ItemEditResponse, summary="Вернуть версию модели")
def revert(item_id: ItemId, payload: RevertRequest, service: ItemServiceDep) -> ItemEditResponse:
    return ItemEditResponse.from_domain(service.revert(item_id, payload.field))


@router.post(
    "/{item_id}/notes",
    response_model=NoteResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Заметка аналитика",
)
def add_note(item_id: ItemId, payload: NoteCreateRequest, service: ItemServiceDep) -> NoteResponse:
    return NoteResponse.from_domain(service.add_note(item_id, payload.body, payload.author))
