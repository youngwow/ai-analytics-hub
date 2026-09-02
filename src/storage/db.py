"""SQLite persistence: schema, connection and small repositories.

One connection per process, used from the main thread only — adapter workers
never touch the database, they return `FetchResult`s. WAL + busy_timeout let a
later dashboard read while `collect --watch` writes. Schema changes go through
`_MIGRATIONS` keyed by `PRAGMA user_version`.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Iterable

from ..common import get_logger, to_utc_iso, utc_now
from ..models import CollectReport, FetchState, RawDocument, Source

log = get_logger("db")

MANUAL_SOURCE_NAME = "Ручной импорт"
MANUAL_FETCH_URL = "manual://import"

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    url         TEXT NOT NULL,
    kind        TEXT NOT NULL,
    category    TEXT NOT NULL,
    fetch_url   TEXT NOT NULL UNIQUE,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    notes       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY,
    source_id     INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    external_id   TEXT NOT NULL,
    url           TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL DEFAULT '',
    text          TEXT NOT NULL DEFAULT '',
    raw_html      TEXT,
    author        TEXT NOT NULL DEFAULT '',
    attachments   TEXT NOT NULL DEFAULT '[]',
    published_at  TEXT,
    fetched_at    TEXT NOT NULL,
    content_hash  TEXT NOT NULL DEFAULT '',
    hidden        INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    UNIQUE (source_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_documents_url ON documents(url);
CREATE INDEX IF NOT EXISTS idx_documents_source_published ON documents(source_id, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash);

CREATE TABLE IF NOT EXISTS fetch_state (
    source_id             INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    etag                  TEXT,
    last_modified         TEXT,
    last_fetch_at         TEXT,
    last_success_at       TEXT,
    last_error            TEXT,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    last_doc_count        INTEGER NOT NULL DEFAULT 0,
    cursor                TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS seen_urls (
    source_id      INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    url            TEXT NOT NULL,
    first_seen_at  TEXT NOT NULL,
    PRIMARY KEY (source_id, url)
);

CREATE TABLE IF NOT EXISTS collect_runs (
    id                    INTEGER PRIMARY KEY,
    started_at            TEXT NOT NULL,
    finished_at           TEXT NOT NULL,
    sources_ok            INTEGER NOT NULL,
    sources_fail          INTEGER NOT NULL,
    sources_not_modified  INTEGER NOT NULL,
    docs_new              INTEGER NOT NULL
);
"""

# version -> DDL script that brings the schema from version-1 to version
_MIGRATIONS: dict[int, str] = {1: _SCHEMA_V1}


class DuplicateSourceError(Exception):
    """A source with the same fetch_url already exists."""

    def __init__(self, existing: Source):
        super().__init__(
            f"source already exists: #{existing.id} {existing.name} ({existing.fetch_url})"
        )
        self.existing = existing


def _now_iso() -> str:
    return to_utc_iso(utc_now()) or ""


class Database:
    """Owns the connection and exposes repositories as attributes."""

    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()
        self.sources = SourceRepo(self.conn)
        self.documents = DocumentRepo(self.conn)
        self.fetch_state = FetchStateRepo(self.conn)
        self.seen_urls = SeenUrlRepo(self.conn)
        self.runs = RunRepo(self.conn)

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for target in sorted(_MIGRATIONS):
            if version < target:
                self.conn.executescript(_MIGRATIONS[target])
                self.conn.execute(f"PRAGMA user_version = {target}")
                self.conn.commit()
                log.info("schema migrated to v%d", target)
                version = target

    def transaction(self):
        """`with db.transaction():` — commit on success, roll back on exception."""
        return self.conn

    def close(self) -> None:
        self.conn.close()


class SourceRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, source: Source) -> Source:
        existing = self.get_by_fetch_url(source.fetch_url)
        if existing is not None:
            raise DuplicateSourceError(existing)
        source.created_at = source.created_at or _now_iso()
        cur = self.conn.execute(
            "INSERT INTO sources (name, url, kind, category, fetch_url, enabled, created_at, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source.name,
                source.url,
                source.kind,
                source.category,
                source.fetch_url,
                int(source.enabled),
                source.created_at,
                source.notes,
            ),
        )
        self.conn.commit()
        source.id = cur.lastrowid
        return source

    def update(self, source: Source) -> None:
        self.conn.execute(
            "UPDATE sources SET name=?, url=?, kind=?, category=?, fetch_url=?, enabled=?, notes=? "
            "WHERE id=?",
            (
                source.name,
                source.url,
                source.kind,
                source.category,
                source.fetch_url,
                int(source.enabled),
                source.notes,
                source.id,
            ),
        )
        self.conn.commit()

    def get(self, source_id: int) -> Source | None:
        row = self.conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        return Source.from_row(row) if row else None

    def get_by_fetch_url(self, fetch_url: str) -> Source | None:
        row = self.conn.execute("SELECT * FROM sources WHERE fetch_url=?", (fetch_url,)).fetchone()
        return Source.from_row(row) if row else None

    def list(self, enabled_only: bool = False) -> list[Source]:
        sql = (
            "SELECT * FROM sources" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id"
        )
        return [Source.from_row(r) for r in self.conn.execute(sql)]

    def set_enabled(self, source_id: int, enabled: bool) -> bool:
        cur = self.conn.execute(
            "UPDATE sources SET enabled=? WHERE id=?", (int(enabled), source_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def remove(self, source_id: int) -> bool:
        """Hard delete; documents, fetch state and seen URLs cascade."""
        cur = self.conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
        self.conn.commit()
        return cur.rowcount > 0

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


class DocumentRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def exists(self, source_id: int, external_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM documents WHERE source_id=? AND external_id=? LIMIT 1",
            (source_id, external_id),
        ).fetchone()
        return row is not None

    def find_by_url(self, url: str) -> int | None:
        row = self.conn.execute("SELECT id FROM documents WHERE url=? LIMIT 1", (url,)).fetchone()
        return int(row["id"]) if row else None

    def insert(self, doc: RawDocument) -> int:
        """Insert one document; the caller owns the transaction."""
        r = doc.to_row()
        cur = self.conn.execute(
            "INSERT INTO documents (source_id, external_id, url, title, summary, text, raw_html, "
            "author, attachments, published_at, fetched_at, content_hash, created_at) "
            "VALUES (:source_id, :external_id, :url, :title, :summary, :text, :raw_html, "
            ":author, :attachments, :published_at, :fetched_at, :content_hash, :created_at)",
            {**r, "created_at": _now_iso()},
        )
        return int(cur.lastrowid)

    def get(self, doc_id: int) -> RawDocument | None:
        row = self.conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        return RawDocument.from_row(row) if row else None

    def list(
        self, source_id: int | None = None, limit: int = 20, include_hidden: bool = False
    ) -> list[sqlite3.Row]:
        """Newest first (NULL dates last). Rows carry `source_name` and `text_len` for display."""
        where = [] if include_hidden else ["d.hidden = 0"]
        params: list = []
        if source_id is not None:
            where.append("d.source_id = ?")
            params.append(source_id)
        sql = (
            "SELECT d.id, d.source_id, s.name AS source_name, d.external_id, d.url, d.title, "
            "d.summary, d.author, d.attachments, d.published_at, d.fetched_at, d.content_hash, "
            "length(d.text) AS text_len "
            "FROM documents d JOIN sources s ON s.id = d.source_id"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY d.published_at DESC, d.id DESC LIMIT ?"
        )
        params.append(limit)
        return list(self.conn.execute(sql, params))

    def count(self, source_id: int | None = None) -> int:
        if source_id is None:
            return int(self.conn.execute("SELECT count(*) FROM documents").fetchone()[0])
        return int(
            self.conn.execute(
                "SELECT count(*) FROM documents WHERE source_id=?", (source_id,)
            ).fetchone()[0]
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


class RunRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, report: CollectReport) -> int:
        cur = self.conn.execute(
            "INSERT INTO collect_runs (started_at, finished_at, sources_ok, sources_fail, "
            "sources_not_modified, docs_new) VALUES (?, ?, ?, ?, ?, ?)",
            (
                report.started_at,
                report.finished_at,
                report.sources_ok,
                report.sources_fail,
                report.sources_not_modified,
                report.docs_new,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def latest(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM collect_runs ORDER BY id DESC LIMIT 1").fetchone()
