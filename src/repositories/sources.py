"""SQLite-репозитории источников: пул, состояние опроса, виденные адреса, история прогонов."""

from __future__ import annotations

import json
import sqlite3
from typing import Iterable

from ..models import (
    FetchState,
    Source,
    SourceRun,
)
from ..sources.textutil import normalized_source_url
from ..utils import get_logger, to_utc_iso, utc_now
from .repository_interface import (
    DuplicateSourceError,
    SourceRepository,
)

log = get_logger("db")


MANUAL_SOURCE_NAME = "Ручной импорт"


MANUAL_FETCH_URL = "manual://import"


def _now_iso() -> str:
    return to_utc_iso(utc_now()) or ""


class SqliteSourceRepository(SourceRepository):
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, source: Source) -> Source:
        existing = self.get_by_fetch_url(source.fetch_url)
        if existing is not None:
            raise DuplicateSourceError(existing)
        source.created_at = source.created_at or _now_iso()
        source.normalized_url = source.normalized_url or normalized_source_url(
            source.fetch_url or source.url
        )
        cur = self.conn.execute(
            "INSERT INTO sources (name, url, kind, category, fetch_url, status, normalized_url, "
            "poll_interval, next_run_at, category_hint, created_at, notes, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source.name,
                source.url,
                source.kind,
                source.category,
                source.fetch_url,
                source.status,
                source.normalized_url,
                source.poll_interval,
                source.next_run_at or source.created_at,
                source.category_hint,
                source.created_at,
                source.notes,
                source.created_by,
            ),
        )
        self.conn.commit()
        source.id = cur.lastrowid
        return source

    def update(self, source: Source) -> None:
        self.conn.execute(
            "UPDATE sources SET name=?, url=?, kind=?, category=?, fetch_url=?, status=?, "
            "normalized_url=?, poll_interval=?, next_run_at=?, category_hint=?, notes=?, "
            "deleted_at=? WHERE id=?",
            (
                source.name,
                source.url,
                source.kind,
                source.category,
                source.fetch_url,
                source.status,
                source.normalized_url,
                source.poll_interval,
                source.next_run_at,
                source.category_hint,
                source.notes,
                source.deleted_at,
                source.id,
            ),
        )
        self.conn.commit()

    def get(self, source_id: int) -> Source | None:
        row = self.conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        return Source.from_row(row) if row else None

    def get_by_fetch_url(self, fetch_url: str) -> Source | None:
        """Удалённый источник не занимает адрес: его можно завести заново (US-11)."""
        row = self.conn.execute(
            "SELECT * FROM sources WHERE fetch_url=? AND status <> 'deleted'", (fetch_url,)
        ).fetchone()
        return Source.from_row(row) if row else None

    def list(
        self,
        enabled_only: bool = False,
        *,
        status: str | None = None,
        kind: str | None = None,
        include_deleted: bool = False,
    ) -> list[Source]:
        where, params = [], []
        if enabled_only:
            # `error` — флаг здоровья, а не пауза: сломанный источник продолжают
            # опрашивать, иначе он никогда не починится сам.
            where.append("status IN ('active', 'error')")
        elif status == "active":
            where.append("status = 'active'")
        elif status:
            where.append("status = ?")
            params.append(status)
        elif not include_deleted:
            where.append("status <> 'deleted'")
        if kind:
            where.append("kind = ?")
            params.append(kind)
        sql = "SELECT * FROM sources" + (" WHERE " + " AND ".join(where) if where else "")
        return [Source.from_row(r) for r in self.conn.execute(sql + " ORDER BY id", params)]

    def get_by_normalized(self, normalized_url: str) -> Source | None:
        """Duplicate check: three spellings of one address share this key."""
        if not normalized_url:
            return None
        row = self.conn.execute(
            "SELECT * FROM sources WHERE normalized_url=? AND status <> 'deleted' LIMIT 1",
            (normalized_url,),
        ).fetchone()
        return Source.from_row(row) if row else None

    def set_status(self, source_id: int, status: str) -> bool:
        deleted_at = _now_iso() if status == "deleted" else None
        cur = self.conn.execute(
            "UPDATE sources SET status=?, deleted_at=? WHERE id=?", (status, deleted_at, source_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def due(self, now: str, limit: int = 100) -> list[Source]:
        """Sources whose turn has come — the whole scheduler in one query."""
        rows = self.conn.execute(
            "SELECT * FROM sources WHERE status IN ('active', 'error') "
            "AND (next_run_at IS NULL OR next_run_at <= ?) ORDER BY next_run_at LIMIT ?",
            (now, limit),
        )
        return [Source.from_row(r) for r in rows]

    def schedule(self, source_id: int, next_run_at: str) -> None:
        self.conn.execute(
            "UPDATE sources SET next_run_at=? WHERE id=?", (next_run_at, source_id)
        )
        self.conn.commit()

    def remove(self, source_id: int) -> bool:
        """Soft delete (US-11): documents and cards stay, the source stops being polled."""
        return self.set_status(source_id, "deleted")

    def ensure_manual(self) -> Source:
        """The built-in sink for `import-url` and hand-entered items."""
        existing = self.get_by_fetch_url(MANUAL_FETCH_URL)
        if existing is not None:
            return existing
        return self.add(
            Source(
                name=MANUAL_SOURCE_NAME,
                url=MANUAL_FETCH_URL,
                kind="manual",
                category="manual",
                fetch_url=MANUAL_FETCH_URL,
                notes="Материалы, добавленные вручную или через import-url",
            )
        )


class FetchStateRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get(self, source_id: int) -> FetchState:
        row = self.conn.execute(
            "SELECT * FROM fetch_state WHERE source_id=?", (source_id,)
        ).fetchone()
        return FetchState.from_row(row) if row else FetchState(source_id=source_id)

    def save(self, state: FetchState) -> None:
        """Upsert; the caller owns the transaction."""
        self.conn.execute(
            "INSERT INTO fetch_state (source_id, etag, last_modified, last_fetch_at, "
            "last_success_at, last_error, consecutive_failures, last_doc_count, cursor) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source_id) DO UPDATE SET etag=excluded.etag, "
            "last_modified=excluded.last_modified, last_fetch_at=excluded.last_fetch_at, "
            "last_success_at=excluded.last_success_at, last_error=excluded.last_error, "
            "consecutive_failures=excluded.consecutive_failures, "
            "last_doc_count=excluded.last_doc_count, cursor=excluded.cursor",
            (
                state.source_id,
                state.etag,
                state.last_modified,
                state.last_fetch_at,
                state.last_success_at,
                state.last_error,
                state.consecutive_failures,
                state.last_doc_count,
                json.dumps(state.cursor, ensure_ascii=False),
            ),
        )

    def reset(self, source_id: int) -> None:
        """Forget validators and cursor (used by `collect --force`); keeps failure history."""
        self.conn.execute(
            "UPDATE fetch_state SET etag=NULL, last_modified=NULL, cursor='{}' WHERE source_id=?",
            (source_id,),
        )


class SeenUrlRepo:
    """URLs the html adapter has already looked at for a source (ingested or not)."""

    _CHUNK = 500

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def known(self, source_id: int, urls: Iterable[str]) -> set[str]:
        urls = list(dict.fromkeys(urls))
        found: set[str] = set()
        for i in range(0, len(urls), self._CHUNK):
            chunk = urls[i : i + self._CHUNK]
            marks = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT url FROM seen_urls WHERE source_id=? AND url IN ({marks})",
                [source_id, *chunk],
            )
            found.update(r["url"] for r in rows)
        return found

    def add(self, source_id: int, urls: Iterable[str], seen_at: str | None = None) -> None:
        """Record URLs as seen; the caller owns the transaction."""
        seen_at = seen_at or _now_iso()
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen_urls (source_id, url, first_seen_at) VALUES (?, ?, ?)",
            [(source_id, u, seen_at) for u in dict.fromkeys(urls)],
        )


class SourceRunRepo:
    """History of polls. `fetch_state` keeps the last state; this keeps the story."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def start(self, source_id: int, started_at: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO source_runs (source_id, started_at) VALUES (?, ?)",
            (source_id, started_at or _now_iso()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish(
        self,
        run_id: int,
        *,
        items_found: int = 0,
        items_new: int = 0,
        http_status: int | None = None,
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        self.conn.execute(
            "UPDATE source_runs SET finished_at=?, items_found=?, items_new=?, http_status=?, "
            "error_code=?, error_message=? WHERE id=?",
            (
                _now_iso(),
                items_found,
                items_new,
                http_status,
                error_code,
                error_message[:500],
                run_id,
            ),
        )
        self.conn.commit()

    def history(self, source_id: int, limit: int = 20) -> list[SourceRun]:
        rows = self.conn.execute(
            "SELECT * FROM source_runs WHERE source_id=? ORDER BY started_at DESC, id DESC LIMIT ?",
            (source_id, limit),
        )
        return [SourceRun.from_row(r) for r in rows]


# Старое имя класса — для кода и тестов, написанных до разделения хранилища.
SourceRepo = SqliteSourceRepository
