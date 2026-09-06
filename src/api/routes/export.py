"""Выгрузка среза: RSS для подписки и CSV для отчёта.

Фильтры те же, что у ленты. Отдаётся только видимое и не архивное, без заметок
аналитика: канал уходит за пределы дашборда, а авторизации нет (вне скоупа).
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from ...api.query import feed_query
from ...dependencies import FeedServiceDep
from ...services import export
from ...utils import to_utc_iso, utc_now

router = APIRouter(prefix="/export", tags=["export"])

RSS_MEDIA_TYPE = "application/rss+xml"
CSV_MEDIA_TYPE = "text/csv"
EXPORT_LIMIT = 200
_FILE_RESPONSE = {"schema": {"type": "string"}}


@router.get(
    "/feed.xml",
    response_class=Response,
    responses={200: {"content": {RSS_MEDIA_TYPE: _FILE_RESPONSE}}},
    summary="Срез ленты как RSS-канал",
)
def feed_rss(request: Request, service: FeedServiceDep) -> Response:
    query = feed_query(request, service.timezone_name)
    rows = service.visible_slice(query, limit=min(query.limit, EXPORT_LIMIT))
    body = export.to_rss(
        rows,
        title="Аналитический центр: лента",
        link=str(request.base_url),
        description="Отраслевые новости и НПА по выбранному срезу",
        generated_at=to_utc_iso(utc_now()) or "",
    )
    return Response(content=body, media_type=f"{RSS_MEDIA_TYPE}; charset=utf-8")


@router.get(
    "/items.csv",
    response_class=Response,
    responses={200: {"content": {CSV_MEDIA_TYPE: _FILE_RESPONSE}}},
    summary="Срез ленты как CSV для отчёта",
)
def items_csv(request: Request, service: FeedServiceDep) -> Response:
    query = feed_query(request, service.timezone_name)
    rows = service.visible_slice(query, limit=min(query.limit, EXPORT_LIMIT))
    stamp = (to_utc_iso(utc_now()) or "")[:10]
    return Response(
        content=export.to_csv(rows),
        media_type=f"{CSV_MEDIA_TYPE}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="items-{stamp}.csv"'},
    )
