"""Карточки: правка, скрытие, история, возврат версии модели, ручное добавление."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from ...feed.service import FeedService
from ...processing.service import ProcessingService
from ..schemas import BulkRequest, HideRequest, ItemCreate, ItemPatch, NoteRequest, RevertRequest
from ._common import get_feed, get_processing
from .feed import feed_query

router = APIRouter(prefix="/api/v1/items", tags=["items"])

Service = Annotated[ProcessingService, Depends(get_processing)]
Feed = Annotated[FeedService, Depends(get_feed)]


@router.get("", summary="Лента карточек")
def list_items(request: Request, feed: Feed) -> dict:
    """Лента целиком уехала в `FeedService`: одна реализация фильтра на CLI и API."""
    return feed.items(feed_query(request, feed))


@router.post("", status_code=201, summary="Ручное добавление материала")
def create_item(payload: ItemCreate, svc: Service) -> dict:
    result = svc.add_manual(
        title=payload.title,
        url=payload.url,
        text=payload.raw_text,
        published_at=payload.published_at,
        item_type=payload.type,
        npa_status=payload.npa_status,
        run_llm=payload.run_llm,
        force=payload.force,
    )
    return {
        "id": result["item_id"],
        "document_id": result["document_id"],
        "origin": "manual",
        "processing_status": "done" if result["item_id"] else "queued",
    }


@router.get("/{item_id}", summary="Карточка целиком")
def get_item(item_id: int, svc: Service) -> dict:
    payload = svc.get_item(item_id)
    if payload is None:
        from ...processing.service import ItemError

        raise ItemError("item_not_found", f"карточка #{item_id} не найдена")
    canonical = next((s for s in payload["sources"] if s["is_canonical"]), None)
    return {
        "canonical_url": (canonical["url"] if canonical else None) or None,
        "item": asdict(payload["item"]),
        "entities": [asdict(e) for e in payload["entities"]],
        "sources": [dict(r) for r in payload["sources"]],
        "events": [asdict(e) for e in payload["events"]],
        "revisions": [asdict(r) for r in payload["revisions"]],
        "notes": [asdict(n) for n in payload["notes"]],
        "tags": [asdict(t) for t in payload["tags"]],
        "model_proposals": {
            name: (asdict(rev) if rev else None)
            for name, rev in payload["model_proposals"].items()
        },
    }


@router.patch("/{item_id}", summary="Правка аналитика")
def patch_item(item_id: int, payload: ItemPatch, svc: Service) -> dict:
    fields = payload.model_dump(exclude_none=True)
    reason = fields.pop("edit_reason", "")
    item = svc.edit_item(item_id, fields, reason=reason)
    return {"item": asdict(item), "manual_overrides": item.manual_overrides}


@router.post("/{item_id}/hide", summary="Скрыть из ленты или из дайджеста")
def hide_item(item_id: int, payload: HideRequest, svc: Service) -> dict:
    item = svc.set_visibility(item_id, payload.scope, payload.reason)
    return {"id": item.id, "visibility": item.visibility}


@router.post("/{item_id}/unhide", summary="Вернуть в ленту")
def unhide_item(item_id: int, svc: Service) -> dict:
    item = svc.set_visibility(item_id, restore=True)
    return {"id": item.id, "visibility": item.visibility}


@router.delete("/{item_id}", summary="Мягкое удаление карточки")
def delete_item(item_id: int, svc: Service) -> dict:
    item = svc.set_visibility(item_id, "deleted")
    return {"id": item.id, "visibility": item.visibility}


@router.post("/{item_id}/restore", summary="Вернуть удалённую карточку")
def restore_item(item_id: int, svc: Service) -> dict:
    item = svc.set_visibility(item_id, restore=True)
    return {"id": item.id, "visibility": item.visibility}


@router.get("/{item_id}/revisions", summary="История правок человека и модели")
def revisions(item_id: int, svc: Service) -> dict:
    return {"revisions": [asdict(r) for r in svc.db.items.revisions(item_id)]}


@router.post("/{item_id}/revert", summary="Вернуть версию модели")
def revert(item_id: int, payload: RevertRequest, svc: Service) -> dict:
    item = svc.revert(item_id, payload.field)
    return {"item": asdict(item), "manual_overrides": item.manual_overrides}


@router.post("/{item_id}/notes", status_code=201, summary="Заметка аналитика")
def add_note(item_id: int, payload: NoteRequest, svc: Service) -> dict:
    return asdict(svc.add_note(item_id, payload.body, payload.author))


@router.post("/bulk", summary="Массовое скрытие под дайджест")
def bulk(payload: BulkRequest, svc: Service) -> dict:
    changed = svc.bulk_visibility(payload.item_ids, payload.scope, payload.reason)
    return {"changed": changed, "scope": payload.scope}
