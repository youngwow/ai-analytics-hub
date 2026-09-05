"""FeedService — всё, что читает дашборд.

Единственная точка входа этапа 1.3: на неё смотрят и CLI, и HTTP, поэтому фильтр
описан один раз (принцип III). Слой только читает: ни одной записи в базу и ни
одного обращения к модели — на этом держится бюджет «быстрее секунды».
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from ..common import get_logger, parse_datetime, to_utc_iso, utc_now
from ..config import Config
from ..models import ITEM_TAGS, ITEM_TYPES, NPA_STATUSES, PRIORITIES
from ..storage import Database
from . import digest as digest_mod
from . import sql
from .query import DocumentQuery, FeedQuery

log = get_logger("feed")

FACET_SOURCES_LIMIT = 20
FACET_TAGS_LIMIT = 15


class FeedService:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db

    @property
    def timezone_name(self) -> str:
        return self.config.api.timezone

    # -- лента --

    def items(self, query: FeedQuery) -> dict:
        started = time.monotonic()
        position = query.decode_cursor()
        statement, params = sql.feed_sql(query, position)
        rows = list(self.db.conn.execute(statement, params))
        has_more = len(rows) > query.limit
        rows = rows[: query.limit]

        cursor = None
        if has_more and rows:
            last = rows[-1]
            cursor = query.encode_cursor(last["published_at"], int(last["id"]))
        count_statement, count_params = sql.count_sql(query)
        total = int(self.db.conn.execute(count_statement, count_params).fetchone()[0])
        return {
            "items": [self._row(r) for r in rows],
            "total": total,
            "next_cursor": cursor,
            "took_ms": int((time.monotonic() - started) * 1000),
        }

    def facets(self, query: FeedQuery) -> dict:
        started = time.monotonic()
        by_priority = self._facet(query, "priority", PRIORITIES)
        by_type = self._facet(query, "type", ITEM_TYPES)
        sources_sql, sources_params = sql.facet_sources_sql(query, FACET_SOURCES_LIMIT)
        tags_sql, tags_params = sql.facet_tags_sql(query, FACET_TAGS_LIMIT)
        return {
            "total": sum(by_priority.values()),
            "by_priority": by_priority,
            "by_type": by_type,
            "by_source": [
                {"source_id": r["source_id"], "name": r["name"], "count": r["n"]}
                for r in self.db.conn.execute(sources_sql, sources_params)
            ],
            "top_tags": [
                {"tag": r["tag"], "count": r["n"]}
                for r in self.db.conn.execute(tags_sql, tags_params)
            ],
            "took_ms": int((time.monotonic() - started) * 1000),
        }

    def _facet(self, query: FeedQuery, column: str, known) -> dict:
        statement, params = sql.facet_sql(query, column)
        counts = {key: 0 for key in known}
        for row in self.db.conn.execute(statement, params):
            counts[row["key"]] = counts.get(row["key"], 0) + int(row["n"])
        return counts

    def filters(self) -> dict:
        """Словари для панели фильтров: дашборд не должен знать их наизусть."""
        sources = [
            {"id": s.id, "name": s.name, "kind": s.kind, "category": s.category, "status": s.status}
            for s in self.db.sources.list()
        ]
        tags = [
            r["tag"]
            for r in self.db.conn.execute(
                "SELECT tag, count(*) AS n FROM item_tags GROUP BY tag ORDER BY n DESC"
            )
        ]
        return {
            "sources": sources,
            "tags": tags or list(ITEM_TAGS),
            "npa_statuses": list(NPA_STATUSES),
            "priorities": list(PRIORITIES),
            "types": list(ITEM_TYPES),
            "orders": ["published", "priority", "processed"],
            "timezone": self.timezone_name,
        }

    # -- необработанное --

    def documents(self, query: DocumentQuery) -> dict:
        """Собрано, но карточки ещё нет: US-12.

        Это не лента — у документа нет ни приоритета, ни типа, ни тегов.
        """
        started = time.monotonic()
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
        where = " AND ".join(clauses)
        base = (
            "FROM documents d LEFT JOIN item_sources s ON s.document_id = d.id "
            "JOIN sources src ON src.id = d.source_id WHERE " + where
        )
        rows = list(
            self.db.conn.execute(
                "SELECT d.id, d.title, d.url, d.source_id, src.name AS source_name, "
                "d.published_at, length(d.text) AS chars " + base
                + " ORDER BY COALESCE(d.published_at, '') DESC, d.id DESC LIMIT ?",
                [*params, query.limit],
            )
        )
        total = int(self.db.conn.execute("SELECT count(*) " + base, params).fetchone()[0])
        return {
            "documents": [dict(r) for r in rows],
            "total": total,
            "took_ms": int((time.monotonic() - started) * 1000),
        }

    # -- дайджест --

    def digest(
        self,
        query: FeedQuery,
        *,
        fmt: str = "markdown",
        title: str = "",
        include_notes: bool = False,
        limit: int = 200,
    ) -> dict:
        """Выгрузка среза. Ничего не сохраняет: дайджест — это срез, а не сущность."""
        from dataclasses import replace

        # Скрытое из дайджеста не выгружается никогда, что бы ни просили фильтры.
        prepared = replace(query, include_hidden=False, limit=limit, cursor=None)
        statement, params = sql.feed_sql(prepared, None)
        rows = [
            self._row(r)
            for r in self.db.conn.execute(statement, params)
            if r["visibility"] == "visible"
        ]
        rows.sort(key=lambda r: ({"high": 0, "medium": 1}.get(r["priority"], 2),
                                 r["published_at"] or ""), reverse=False)
        notes = self._notes_for(rows) if include_notes else {}
        generated_at = to_utc_iso(utc_now()) or ""
        heading = title or f"Дайджест {generated_at[:10]}"
        body = (
            digest_mod.to_markdown(rows, title=heading, generated_at=generated_at, notes=notes)
            if fmt == "markdown"
            else json.dumps(
                {"title": heading, "generated_at": generated_at, "items": rows},
                ensure_ascii=False,
                indent=2,
            )
        )
        return {
            "title": heading,
            "generated_at": generated_at,
            "items": len(rows),
            "format": fmt,
            "body": body,
        }

    def _notes_for(self, rows: list[dict]) -> dict:
        notes: dict[int, list[str]] = {}
        for row in rows:
            stored = self.db.notes.list(row["id"])
            if stored:
                notes[row["id"]] = [n.body for n in stored]
        return notes

    # -- состояние --

    def status(self) -> dict:
        """Сводка для шапки: почему в ленте столько, сколько есть."""
        conn = self.db.conn
        latest = self.db.runs.latest()
        unprocessed = int(
            conn.execute(
                "SELECT count(*) FROM documents d LEFT JOIN item_sources s "
                "ON s.document_id = d.id WHERE s.document_id IS NULL AND d.hidden = 0"
            ).fetchone()[0]
        )
        by_status = {
            r["status"]: r["n"]
            for r in conn.execute("SELECT status, count(*) AS n FROM sources GROUP BY status")
        }
        now = utc_now()
        stale = []
        for source in self.db.sources.list(enabled_only=True):
            scheduled = parse_datetime(source.next_run_at) if source.next_run_at else None
            if scheduled is None or scheduled > now:
                continue
            state = self.db.fetch_state.get(source.id)
            stale.append(
                {
                    "id": source.id,
                    "name": source.name,
                    "overdue_minutes": int((now - scheduled).total_seconds() // 60),
                    "consecutive_failures": state.consecutive_failures,
                    "last_error": (state.last_error or "")[:200],
                }
            )
        stale.sort(key=lambda s: s["overdue_minutes"], reverse=True)
        return {
            "last_collect_at": latest["finished_at"] if latest else None,
            "documents": self.db.documents.count(),
            "items": self.db.items.count(),
            "unprocessed": unprocessed,
            "sources": by_status,
            "stale_sources": stale[:20],
            "timezone": self.timezone_name,
        }

    # -- общее --

    def _row(self, row) -> dict:
        keys = row.keys()
        return {
            "id": int(row["id"]),
            "type": row["type"],
            "npa_status": row["npa_status"],
            "npa_key": row["npa_key"],
            "priority": row["priority"],
            "title": row["title"],
            "summary": row["summary"],
            "tags": _json_list(row["tags"]),
            "published_at": row["published_at"],
            "sources_count": row["sources_count"],
            "canonical_url": row["canonical_url"] or None,
            "source_name": row["source_name"],
            "visibility": row["visibility"],
            "hidden_reason": row["hidden_reason"] or "",
            "origin": row["origin"],
            "confidence": row["confidence"],
            "relevance_score": row["relevance_score"],
            "reasoning": row["reasoning"],
            "snippet": row["snippet"] if "snippet" in keys else None,
            "flags": {
                "degraded": bool(row["degraded"]),
                "needs_review": bool(row["needs_review"]),
                "date_estimated": bool(row["date_estimated"]),
                "edited": bool(_json_list(row["manual_overrides"])),
            },
        }


def _json_list(raw) -> list:
    try:
        value = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


def local_today(config: Config) -> str:
    """Сегодняшняя дата в зоне конфига — для подсказок и значений по умолчанию."""
    from zoneinfo import ZoneInfo

    return datetime.now(timezone.utc).astimezone(ZoneInfo(config.api.timezone)).date().isoformat()
