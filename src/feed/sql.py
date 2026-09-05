"""Сборка запросов ленты: условия, порядок, курсор и счётчики.

Порядок задаётся тройкой ключей, все по убыванию, поэтому условие следующей
страницы — это одно сравнение кортежей, а не разбор частных случаев с NULL.
"""

from __future__ import annotations

from ..common import to_utc_iso
from . import search
from .query import FeedQuery

# Приоритет — текст, поэтому порядок задаётся явно: алфавит здесь бессмыслен.
_PRIORITY_RANK = "CASE i.priority WHEN 'high' THEN 2 WHEN 'medium' THEN 1 ELSE 0 END"
# Материал без даты не выпадает из ленты, а уходит в конец: пустая строка меньше
# любой даты, а сортировка идёт по убыванию.
_PUBLISHED = "COALESCE(i.published_at, '')"
_PROCESSED = "COALESCE(i.processed_at, '')"

FEED_COLUMNS = """
    i.id, i.type, i.npa_status, i.npa_key, i.priority, i.title, i.summary, i.tags,
    i.published_at, i.visibility, i.hidden_reason, i.origin, i.degraded, i.needs_review,
    i.date_estimated, i.manual_overrides, i.confidence, i.relevance_score, i.reasoning,
    c.size AS sources_count, d.url AS canonical_url, sr.name AS source_name
"""
FEED_FROM = """
    FROM items i
    JOIN clusters c ON c.id = i.cluster_id
    LEFT JOIN item_sources isrc ON isrc.item_id = i.id AND isrc.is_canonical = 1
    LEFT JOIN documents d ON d.id = isrc.document_id
    LEFT JOIN sources sr ON sr.id = d.source_id
"""
# Без псевдонима: `snippet()` принимает только имя таблицы, а `f MATCH` — не синтаксис.
SEARCH_JOIN = " JOIN items_search ON items_search.rowid = i.id "
SNIPPET = "snippet(items_search, 0, '<b>', '</b>', '…', 12) AS snippet"


def sort_keys(order: str) -> list[str]:
    """Ключи сортировки, всегда по убыванию — на них же строится курсор."""
    if order == "priority":
        return [_PRIORITY_RANK, _PUBLISHED, "i.id"]
    if order == "processed":
        return [_PROCESSED, "i.id"]
    return [_PUBLISHED, "i.id"]


def where(query: FeedQuery) -> tuple[list[str], list]:
    """Условия среза и их параметры."""
    clauses: list[str] = []
    params: list = []

    if not query.include_hidden:
        clauses.append("i.visibility NOT IN ('hidden_feed', 'deleted')")
    if query.type:
        clauses.append("i.type = ?")
        params.append(query.type)
    if query.npa_status:
        clauses.append("i.npa_status = ?")
        params.append(query.npa_status)
    if query.priority:
        marks = ",".join("?" * len(query.priority))
        clauses.append(f"i.priority IN ({marks})")
        params.extend(query.priority)
    for tag in query.tags:
        clauses.append("EXISTS (SELECT 1 FROM item_tags t WHERE t.item_id = i.id AND t.tag = ?)")
        params.append(tag)
    if query.source_ids:
        marks = ",".join("?" * len(query.source_ids))
        clauses.append(
            "EXISTS (SELECT 1 FROM item_sources s2 JOIN documents d2 ON d2.id = s2.document_id "
            f"WHERE s2.item_id = i.id AND d2.source_id IN ({marks}))"
        )
        params.extend(query.source_ids)
    if query.date_from:
        clauses.append("i.published_at >= ?")
        params.append(to_utc_iso(query.date_from))
    if query.date_to:
        clauses.append("i.published_at <= ?")
        params.append(to_utc_iso(query.date_to))
    return clauses, params


def match_expression(query: FeedQuery) -> str:
    return search.to_match(query.q)


def feed_sql(query: FeedQuery, position: tuple | None = None) -> tuple[str, list]:
    """SELECT строки ленты: условия, курсор, порядок и лимит."""
    clauses, params = where(query)
    match = match_expression(query)
    columns = FEED_COLUMNS + (f", {SNIPPET}" if match else "")
    join = SEARCH_JOIN if match else ""
    if match:
        clauses.append("items_search.body MATCH ?")
        params.append(match)

    keys = sort_keys(query.order)
    if position:
        marks = ",".join("?" * len(keys))
        clauses.append(f"({', '.join(keys)}) < ({marks})")
        params.extend(position)

    sql = (
        f"SELECT {columns} {FEED_FROM} {join}"
        + ("WHERE " + " AND ".join(clauses) + " " if clauses else "")
        + "ORDER BY " + ", ".join(f"{k} DESC" for k in keys)
        + " LIMIT ?"
    )
    params.append(query.limit + 1)  # на один больше — так видно, есть ли следующая страница
    return sql, params


def cursor_position(query: FeedQuery, row) -> list:
    """Значения ключей сортировки последней отданной строки."""
    if query.order == "priority":
        rank = {"high": 2, "medium": 1}.get(row["priority"], 0)
        return [rank, row["published_at"] or "", row["id"]]
    if query.order == "processed":
        return [row["processed_at"] or "", row["id"]]
    return [row["published_at"] or "", row["id"]]


def count_sql(query: FeedQuery) -> tuple[str, list]:
    """Сколько всего подходит под срез — по тем же условиям, что и лента."""
    clauses, params = where(query)
    match = match_expression(query)
    join = SEARCH_JOIN if match else ""
    if match:
        clauses.append("items_search.body MATCH ?")
        params.append(match)
    sql = (
        f"SELECT count(*) {FEED_FROM} {join}"
        + ("WHERE " + " AND ".join(clauses) if clauses else "")
    )
    return sql, params


def facet_sql(query: FeedQuery, column: str) -> tuple[str, list]:
    """Счётчики по колонке карточки для того же среза."""
    clauses, params = where(query)
    match = match_expression(query)
    join = SEARCH_JOIN if match else ""
    if match:
        clauses.append("items_search.body MATCH ?")
        params.append(match)
    sql = (
        f"SELECT i.{column} AS key, count(*) AS n {FEED_FROM} {join}"
        + ("WHERE " + " AND ".join(clauses) + " " if clauses else "")
        + f"GROUP BY i.{column}"
    )
    return sql, params


def facet_sources_sql(query: FeedQuery, limit: int = 20) -> tuple[str, list]:
    clauses, params = where(query)
    match = match_expression(query)
    join = SEARCH_JOIN if match else ""
    if match:
        clauses.append("items_search.body MATCH ?")
        params.append(match)
    sql = (
        "SELECT d2.source_id AS source_id, s2n.name AS name, count(DISTINCT i.id) AS n "
        + FEED_FROM
        + join
        + " JOIN item_sources s2 ON s2.item_id = i.id "
        "JOIN documents d2 ON d2.id = s2.document_id "
        "JOIN sources s2n ON s2n.id = d2.source_id "
        + ("WHERE " + " AND ".join(clauses) + " " if clauses else "")
        + "GROUP BY d2.source_id ORDER BY n DESC LIMIT ?"
    )
    params.append(limit)
    return sql, params


def facet_tags_sql(query: FeedQuery, limit: int = 15) -> tuple[str, list]:
    clauses, params = where(query)
    match = match_expression(query)
    join = SEARCH_JOIN if match else ""
    if match:
        clauses.append("items_search.body MATCH ?")
        params.append(match)
    sql = (
        "SELECT t2.tag AS tag, count(*) AS n "
        + FEED_FROM
        + join
        + " JOIN item_tags t2 ON t2.item_id = i.id "
        + ("WHERE " + " AND ".join(clauses) + " " if clauses else "")
        + "GROUP BY t2.tag ORDER BY n DESC LIMIT ?"
    )
    params.append(limit)
    return sql, params
