"""Разбор строки запроса в `FeedQuery` / `DocumentQuery` — одна форма на CLI и HTTP."""

from __future__ import annotations

from fastapi import Request

from ..models.queries import DocumentQuery, FeedQuery


def feed_query(request: Request, timezone_name: str) -> FeedQuery:
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
        archived=params.get("archived"),
        duplicate=params.get("duplicate"),
        timezone_name=timezone_name,
    )


def document_query(request: Request, timezone_name: str) -> DocumentQuery:
    """Фильтры карточки к списку документов неприменимы — отклоняются, а не игнорируются."""
    params = request.query_params
    return DocumentQuery.build(
        unsupported={
            "priority": params.getlist("priority"),
            "type": params.get("type"),
            "tag": params.getlist("tag"),
            "npa_status": params.get("npa_status"),
            "duplicate": params.get("duplicate"),
        },
        q=params.get("q"),
        source_ids=params.getlist("source_id"),
        date_from=params.get("from"),
        date_to=params.get("to"),
        limit=params.get("limit"),
        order=params.get("order"),
        cursor=params.get("cursor"),
        timezone_name=timezone_name,
    )


def digest_query(filters: dict, timezone_name: str) -> FeedQuery:
    """Фильтры дайджеста приходят в теле; форма та же, что у ленты."""
    return FeedQuery.build(
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
        archived=filters.get("archived"),
        duplicate=filters.get("duplicate"),
        timezone_name=timezone_name,
    )
