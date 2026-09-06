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
    BulkArchiveRequest,
    BulkTagsRequest,
    BulkVisibilityRequest,
    HideRequest,
    ItemCreateRequest,
    ItemUpdateRequest,
    NoteCreateRequest,
    NpaEventCreateRequest,
    RevertRequest,
)
from ...models.responses import (
    ArchiveResponse,
    BulkItemsResponse,
    BulkResponse,
    FacetsResponse,
    FeedResponse,
    ItemCardResponse,
    ItemEditResponse,
    ManualItemResponse,
    NoteResponse,
    NpaEventResponse,
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


@router.post(
    "/tags/bulk",
    response_model=BulkItemsResponse,
    summary="Массовый тегинг: добавить и снять теги у списка карточек",
)
def bulk_tags(payload: BulkTagsRequest, service: ItemServiceDep) -> BulkItemsResponse:
    return BulkItemsResponse.model_validate(
        service.bulk_tags(payload.item_ids, add=payload.add, remove=payload.remove)
    )


@router.post(
    "/archive/bulk",
    response_model=BulkItemsResponse,
    summary="Массовая архивация: после отправки дайджеста это один жест",
)
def bulk_archive(payload: BulkArchiveRequest, service: ItemServiceDep) -> BulkItemsResponse:
    return BulkItemsResponse.model_validate(
        service.bulk_archive(payload.item_ids, payload.archived)
    )


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


@router.post(
    "/{item_id}/events",
    response_model=NpaEventResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Событие в хронологии: статус НПА, слушания, срок",
)
def add_event(
    item_id: ItemId, payload: NpaEventCreateRequest, service: ItemServiceDep
) -> NpaEventResponse:
    event = service.add_npa_event(
        item_id,
        payload.status,
        occurred_at=payload.occurred_at,
        source_url=payload.source_url,
        note=payload.note,
    )
    return NpaEventResponse.from_domain(event)


@router.post("/{item_id}/archive", response_model=ArchiveResponse, summary="В архив: из ленты и дайджеста, но не из поиска")
def archive_item(item_id: ItemId, service: ItemServiceDep) -> ArchiveResponse:
    return ArchiveResponse.from_domain(service.set_archived(item_id, True))


@router.post("/{item_id}/unarchive", response_model=ArchiveResponse, summary="Вернуть из архива")
def unarchive_item(item_id: ItemId, service: ItemServiceDep) -> ArchiveResponse:
    return ArchiveResponse.from_domain(service.set_archived(item_id, False))
