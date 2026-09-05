"""SQLite-репозиторий ленты: сборка запросов и их выполнение. Только чтение.

Порядок задаётся тройкой ключей, все по убыванию, поэтому условие следующей
страницы — это одно сравнение кортежей, а не разбор частных случаев с NULL.

Подготовка пользовательского запроса к FTS5: пользователь ищет слова, а не пишет
запрос на языке FTS5, поэтому `AND`, `OR`, `NEAR`, минус и кавычки из его текста
не должны становиться операторами. Морфологии у `unicode61` нет, поэтому к словам
добавляется `*`: по `законопроект` FTS5 не находит «законопроекта», а по
`законопроект*` — находит.
"""

from __future__ import annotations

import re
import sqlite3

from ..models.queries import DocumentQuery, FeedQuery
from ..utils import to_utc_iso
from .repository_interface import FeedRepository

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
PREFIX_FROM = 3  # слова короче трёх букв префиксом не расширяем: «в*» найдёт всё


def terms(query: str) -> list[str]:
    """Слова запроса без пунктуации и операторов."""
    return [w for w in _WORD_RE.findall(query or "") if w]


def to_match(query: str) -> str:
    """Строка для `MATCH`: каждое слово в кавычках, длинные — с префиксом, всё через AND.

    Пустой после чистки запрос даёт пустую строку — вызывающий просто не
    применяет фильтр, а не получает ошибку.
    """
    parts = []
    for word in terms(query):
        quoted = '"' + word.replace('"', '""') + '"'
        parts.append(f"{quoted}*" if len(word) >= PREFIX_FROM else quoted)
    return " AND ".join(parts)



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
    return to_match(query.q)


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


class SqliteFeedRepository(FeedRepository):
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def page(self, query: FeedQuery, position: tuple | None = None) -> list[dict]:
        statement, params = feed_sql(query, position)
        return [dict(r) for r in self.conn.execute(statement, params)]

    def count(self, query: FeedQuery) -> int:
        statement, params = count_sql(query)
        return int(self.conn.execute(statement, params).fetchone()[0])

    def facet(self, query: FeedQuery, column: str) -> dict[str, int]:
        statement, params = facet_sql(query, column)
        return {r["key"]: int(r["n"]) for r in self.conn.execute(statement, params)}

    def facet_sources(self, query: FeedQuery, limit: int = 20) -> list[dict]:
        statement, params = facet_sources_sql(query, limit)
        return [
            {"source_id": r["source_id"], "name": r["name"], "count": r["n"]}
            for r in self.conn.execute(statement, params)
        ]

    def facet_tags(self, query: FeedQuery, limit: int = 15) -> list[dict]:
        statement, params = facet_tags_sql(query, limit)
        return [{"tag": r["tag"], "count": r["n"]} for r in self.conn.execute(statement, params)]

    def tags_in_use(self) -> list[str]:
        return [
            r["tag"]
            for r in self.conn.execute(
                "SELECT tag, count(*) AS n FROM item_tags GROUP BY tag ORDER BY n DESC"
            )
        ]

    def documents(self, query: DocumentQuery) -> tuple[list[dict], int]:
        """Собрано, но карточки ещё нет: у документа нет ни приоритета, ни типа, ни тегов."""
        clauses = ["d.hidden = 0", "s.document_id IS NULL"]
        params: list = []
        if query.source_ids:
            marks = ",".join("?" * len(query.source_ids))
            clauses.append(f"d.source_id IN ({marks})")
            params.extend(query.source_ids)
        if query.date_from:
            clauses.append("d.published_at >= ?")
            params.append(to_utc_iso(query.date_from))
        if query.date_to:
            clauses.append("d.published_at <= ?")
            params.append(to_utc_iso(query.date_to))
        if query.q:
            clauses.append("d.title LIKE ?")
            params.append(f"%{query.q}%")
        base = (
            "FROM documents d LEFT JOIN item_sources s ON s.document_id = d.id "
            "JOIN sources src ON src.id = d.source_id WHERE " + " AND ".join(clauses)
        )
        rows = self.conn.execute(
            "SELECT d.id, d.title, d.url, d.source_id, src.name AS source_name, "
            "d.published_at, length(d.text) AS chars " + base
            + " ORDER BY COALESCE(d.published_at, '') DESC, d.id DESC LIMIT ?",
            [*params, query.limit],
        )
        total = int(self.conn.execute("SELECT count(*) " + base, params).fetchone()[0])
        return [dict(r) for r in rows], total

    def unprocessed_count(self) -> int:
        return int(
            self.conn.execute(
                "SELECT count(*) FROM documents d LEFT JOIN item_sources s "
                "ON s.document_id = d.id WHERE s.document_id IS NULL AND d.hidden = 0"
            ).fetchone()[0]
        )

    def sources_by_status(self) -> dict[str, int]:
        return {
            r["status"]: int(r["n"])
            for r in self.conn.execute("SELECT status, count(*) AS n FROM sources GROUP BY status")
        }
