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

from ..common import get_logger, sha256_text, to_utc_iso, utc_now
from ..models import (
    Cluster,
    CollectReport,
    CompanyProfile,
    EntitySpan,
    FetchState,
    Item,
    ItemRevision,
    LlmCall,
    NpaEvent,
    RawDocument,
    Source,
)

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
_SCHEMA_V2 = """
ALTER TABLE documents ADD COLUMN simhash   TEXT NOT NULL DEFAULT '';
ALTER TABLE documents ADD COLUMN embedding BLOB;
ALTER TABLE documents ADD COLUMN norm_text TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_documents_simhash ON documents(simhash);

CREATE TABLE IF NOT EXISTS clusters (
    id                     INTEGER PRIMARY KEY,
    canonical_document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    centroid_embedding     BLOB,
    size                   INTEGER NOT NULL DEFAULT 1,
    has_divergent_opinions INTEGER NOT NULL DEFAULT 0,
    created_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prompt_versions (
    id         INTEGER PRIMARY KEY,
    stage      TEXT NOT NULL,
    template   TEXT NOT NULL,
    model      TEXT NOT NULL,
    params     TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (stage, template, model, params)
);

CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY,
    cluster_id      INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    type            TEXT NOT NULL CHECK (type IN ('npa','news')),
    npa_status      TEXT,
    npa_key         TEXT,
    title           TEXT NOT NULL DEFAULT '',
    summary         TEXT NOT NULL DEFAULT '',
    priority        TEXT NOT NULL DEFAULT 'medium' CHECK (priority IN ('high','medium','low')),
    relevance_score REAL NOT NULL DEFAULT 0,
    reasoning       TEXT NOT NULL DEFAULT '',
    confidence      REAL NOT NULL DEFAULT 0,
    tags            TEXT NOT NULL DEFAULT '[]',
    analyst_note    TEXT NOT NULL DEFAULT '',
    is_hidden       INTEGER NOT NULL DEFAULT 0,
    is_archived     INTEGER NOT NULL DEFAULT 0,
    degraded        INTEGER NOT NULL DEFAULT 0,
    needs_review    INTEGER NOT NULL DEFAULT 0,
    date_estimated  INTEGER NOT NULL DEFAULT 0,
    model_name      TEXT NOT NULL DEFAULT '',
    prompt_version  INTEGER REFERENCES prompt_versions(id),
    profile_version INTEGER,
    edited_fields   TEXT NOT NULL DEFAULT '[]',
    processed_at    TEXT NOT NULL,
    published_at    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_cluster ON items(cluster_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_npa_key
    ON items(npa_key) WHERE npa_key IS NOT NULL AND is_archived = 0;
CREATE INDEX IF NOT EXISTS idx_items_feed ON items(published_at DESC, priority);

CREATE TABLE IF NOT EXISTS entities (
    id               INTEGER PRIMARY KEY,
    item_id          INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    role             TEXT NOT NULL CHECK (role IN ('who','what','when','impact','org','act_number')),
    value            TEXT NOT NULL,
    normalized_value TEXT NOT NULL DEFAULT '',
    evidence_start   INTEGER,
    evidence_end     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_entities_item ON entities(item_id);
CREATE INDEX IF NOT EXISTS idx_entities_act
    ON entities(normalized_value) WHERE role = 'act_number';

CREATE TABLE IF NOT EXISTS item_sources (
    item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    is_canonical INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (item_id, document_id)
);
CREATE INDEX IF NOT EXISTS idx_item_sources_doc ON item_sources(document_id);

CREATE TABLE IF NOT EXISTS npa_events (
    id          INTEGER PRIMARY KEY,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    status      TEXT NOT NULL,
    occurred_at TEXT,
    source_url  TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL DEFAULT 'system' CHECK (created_by IN ('system','user')),
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_npa_events_item ON npa_events(item_id, occurred_at);

CREATE TABLE IF NOT EXISTS item_revisions (
    id         INTEGER PRIMARY KEY,
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    actor      TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_item_revisions_item ON item_revisions(item_id, created_at);

CREATE TABLE IF NOT EXISTS company_profiles (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    version    INTEGER NOT NULL DEFAULT 1,
    is_default INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id         INTEGER PRIMARY KEY,
    item_id    INTEGER REFERENCES items(id) ON DELETE SET NULL,
    stage      TEXT NOT NULL,
    model      TEXT NOT NULL,
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    cost       REAL NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'ok',
    error      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_calls(created_at);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts
    USING fts5(title, summary, content='items', content_rowid='id', tokenize='unicode61');

CREATE TRIGGER IF NOT EXISTS items_fts_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS items_fts_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, summary)
        VALUES ('delete', old.id, old.title, old.summary);
END;
CREATE TRIGGER IF NOT EXISTS items_fts_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, summary)
        VALUES ('delete', old.id, old.title, old.summary);
    INSERT INTO items_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
END;
"""

_SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS context_versions (
    id          INTEGER PRIMARY KEY,
    version     TEXT NOT NULL UNIQUE,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    actor       TEXT NOT NULL DEFAULT 'system'
);

CREATE TABLE IF NOT EXISTS prepared_documents (
    id              INTEGER PRIMARY KEY,
    material_id     TEXT NOT NULL,
    version         INTEGER NOT NULL,
    raw_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    payload         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(material_id, version)
);

CREATE TABLE IF NOT EXISTS analysis_runs (
    id                INTEGER PRIMARY KEY,
    material_id       TEXT NOT NULL,
    prepared_version  INTEGER NOT NULL,
    context_version   TEXT NOT NULL,
    configuration_id  TEXT NOT NULL,
    model             TEXT NOT NULL,
    payload           TEXT NOT NULL,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analysis_material ON analysis_runs(material_id, id DESC);

CREATE TABLE IF NOT EXISTS signal_revisions (
    id               INTEGER PRIMARY KEY,
    signal_id        TEXT NOT NULL,
    revision         INTEGER NOT NULL,
    analysis_run_id  INTEGER REFERENCES analysis_runs(id) ON DELETE SET NULL,
    payload          TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    actor            TEXT NOT NULL DEFAULT 'ai',
    reason           TEXT NOT NULL DEFAULT '',
    UNIQUE(signal_id, revision)
);

CREATE TABLE IF NOT EXISTS research_reports (
    id          INTEGER PRIMARY KEY,
    signal_id   TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_events (
    id          TEXT PRIMARY KEY,
    version     INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_links (
    id          INTEGER PRIMARY KEY,
    signal_id   TEXT NOT NULL,
    event_id    TEXT REFERENCES product_events(id) ON DELETE SET NULL,
    relation    TEXT NOT NULL,
    confidence  REAL NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS npa_records (
    id                    TEXT PRIMARY KEY,
    jurisdiction          TEXT NOT NULL,
    official_identifier   TEXT NOT NULL,
    official_url          TEXT,
    tracked               INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT NOT NULL,
    UNIQUE(jurisdiction, official_identifier)
);

CREATE TABLE IF NOT EXISTS npa_candidates (
    id          INTEGER PRIMARY KEY,
    signal_id   TEXT NOT NULL,
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'unresolved',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS npa_versions (
    id             INTEGER PRIMARY KEY,
    npa_id         TEXT NOT NULL REFERENCES npa_records(id) ON DELETE CASCADE,
    version        INTEGER NOT NULL,
    stage          TEXT,
    payload        TEXT NOT NULL,
    source_url     TEXT NOT NULL,
    effective_at   TEXT,
    created_at     TEXT NOT NULL,
    UNIQUE(npa_id, version)
);

CREATE TABLE IF NOT EXISTS review_decisions (
    id               INTEGER PRIMARY KEY,
    signal_id        TEXT NOT NULL,
    signal_revision  INTEGER NOT NULL,
    decision         TEXT NOT NULL,
    payload          TEXT NOT NULL,
    actor             TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digests (
    id          TEXT NOT NULL,
    version     INTEGER NOT NULL,
    period_from TEXT NOT NULL,
    period_to   TEXT NOT NULL,
    status      TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    actor       TEXT NOT NULL,
    PRIMARY KEY(id, version)
);

CREATE TABLE IF NOT EXISTS delivery_attempts (
    id               INTEGER PRIMARY KEY,
    idempotency_key  TEXT NOT NULL UNIQUE,
    digest_id        TEXT,
    digest_version   INTEGER,
    channel          TEXT NOT NULL,
    recipient        TEXT NOT NULL,
    status           TEXT NOT NULL,
    error            TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id          INTEGER PRIMARY KEY,
    event_type  TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id   TEXT NOT NULL,
    actor       TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_object ON audit_events(object_type, object_id, id);
"""

_SCHEMA_V4 = """
ALTER TABLE sources ADD COLUMN direction TEXT NOT NULL DEFAULT 'both';
ALTER TABLE sources ADD COLUMN source_class TEXT NOT NULL DEFAULT 'ordinary';
ALTER TABLE sources ADD COLUMN status TEXT NOT NULL DEFAULT 'active';

ALTER TABLE fetch_state ADD COLUMN native_cursor TEXT;
ALTER TABLE fetch_state ADD COLUMN high_watermark TEXT;
ALTER TABLE fetch_state ADD COLUMN continuation_cursor TEXT NOT NULL DEFAULT '{}';
ALTER TABLE fetch_state ADD COLUMN backlog_status TEXT NOT NULL DEFAULT 'clear';
ALTER TABLE fetch_state ADD COLUMN coverage_from TEXT;
ALTER TABLE fetch_state ADD COLUMN coverage_to TEXT;
ALTER TABLE fetch_state ADD COLUMN coverage_status TEXT NOT NULL DEFAULT 'unknown';

CREATE TABLE IF NOT EXISTS fetch_artifacts (
    id                INTEGER PRIMARY KEY,
    source_id         INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    request_id        TEXT NOT NULL,
    requested_at      TEXT NOT NULL,
    completed_at      TEXT NOT NULL,
    status            TEXT NOT NULL,
    content_type      TEXT,
    sanitized_headers TEXT NOT NULL DEFAULT '{}',
    bounded_body      BLOB,
    body_hash         TEXT,
    warnings          TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS document_revisions (
    id                   INTEGER PRIMARY KEY,
    document_id          INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    revision             INTEGER NOT NULL,
    content_hash         TEXT NOT NULL,
    payload              TEXT NOT NULL,
    previous_revision_id INTEGER REFERENCES document_revisions(id),
    created_at           TEXT NOT NULL,
    UNIQUE(document_id, revision)
);
"""


_SCHEMA_V5 = """
ALTER TABLE product_events ADD COLUMN lifecycle_state TEXT NOT NULL DEFAULT 'active';
ALTER TABLE product_events ADD COLUMN first_seen_at TEXT;
ALTER TABLE product_events ADD COLUMN last_seen_at TEXT;
ALTER TABLE product_events ADD COLUMN last_meaningful_update_at TEXT;
ALTER TABLE product_events ADD COLUMN archived_at TEXT;

UPDATE product_events
SET first_seen_at=COALESCE(first_seen_at, created_at),
    last_seen_at=COALESCE(last_seen_at, updated_at),
    last_meaningful_update_at=COALESCE(last_meaningful_update_at, updated_at);

CREATE INDEX IF NOT EXISTS idx_product_events_lifecycle
    ON product_events(lifecycle_state, last_meaningful_update_at);
"""


_MIGRATIONS: dict[int, str] = {
    1: _SCHEMA_V1,
    2: _SCHEMA_V2,
    3: _SCHEMA_V3,
    4: _SCHEMA_V4,
    5: _SCHEMA_V5,
}


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
        self.fetch_artifacts = FetchArtifactRepo(self.conn)
        self.document_revisions = DocumentRevisionRepo(self.conn)
        self.fetch_state = FetchStateRepo(self.conn)
        self.seen_urls = SeenUrlRepo(self.conn)
        self.runs = RunRepo(self.conn)
        self.clusters = ClusterRepo(self.conn)
        self.items = ItemRepo(self.conn)
        self.profiles = ProfileRepo(self.conn)
        self.prompts = PromptRepo(self.conn)
        self.llm_calls = LlmCallRepo(self.conn)

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
            "INSERT INTO sources (name, url, kind, category, fetch_url, enabled, created_at, notes, direction, source_class, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source.name,
                source.url,
                source.kind,
                source.category,
                source.fetch_url,
                int(source.enabled),
                source.created_at,
                source.notes,
                source.direction,
                source.source_class,
                source.status,
            ),
        )
        self.conn.commit()
        source.id = cur.lastrowid
        return source

    def update(self, source: Source) -> None:
        self.conn.execute(
            "UPDATE sources SET name=?, url=?, kind=?, category=?, fetch_url=?, enabled=?, notes=?, direction=?, source_class=?, status=? "
            "WHERE id=?",
            (
                source.name,
                source.url,
                source.kind,
                source.category,
                source.fetch_url,
                int(source.enabled),
                source.notes,
                source.direction,
                source.source_class,
                source.status,
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
            """UPDATE sources
               SET enabled=?,
                   status=CASE WHEN ?=1 THEN 'active' ELSE status END
               WHERE id=?""",
            (int(enabled), int(enabled), source_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def remove(self, source_id: int) -> bool:
        """Decommission a source without destroying collected evidence."""
        cur = self.conn.execute(
            """UPDATE sources
               SET enabled=0, status='decommissioned'
               WHERE id=? AND status!='decommissioned'""",
            (source_id,),
        )
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

    def row_by_external_id(self, source_id: int, external_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM documents WHERE source_id=? AND external_id=? LIMIT 1",
            (source_id, external_id),
        ).fetchone()

    def update(self, document_id: int, doc: RawDocument) -> None:
        """Replace the current projection; immutable snapshots live in document_revisions."""
        r = doc.to_row()
        self.conn.execute(
            "UPDATE documents SET url=:url,title=:title,summary=:summary,text=:text,"
            "raw_html=:raw_html,author=:author,attachments=:attachments,"
            "published_at=:published_at,fetched_at=:fetched_at,content_hash=:content_hash "
            "WHERE id=:document_id",
            {**r, "document_id": document_id},
        )

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

    def unprocessed(
        self,
        limit: int = 200,
        source_id: int | None = None,
        since: str | None = None,
        force: bool = False,
    ) -> list[sqlite3.Row]:
        """Documents that have no card yet (with --force: everything in the window)."""
        where = ["d.hidden = 0"]
        params: list = []
        if not force:
            where.append("s.document_id IS NULL")
        if source_id is not None:
            where.append("d.source_id = ?")
            params.append(source_id)
        if since:
            where.append("(d.published_at >= ? OR d.published_at IS NULL)")
            params.append(since)
        params.append(limit)
        return list(
            self.conn.execute(
                "SELECT d.* FROM documents d "
                "LEFT JOIN item_sources s ON s.document_id = d.id "
                "WHERE " + " AND ".join(where) + " "
                "ORDER BY d.published_at DESC, d.id DESC LIMIT ?",
                params,
            )
        )

    def set_derived(
        self, doc_id: int, *, simhash: str = "", embedding: bytes | None = None, norm_text: str = ""
    ) -> None:
        """Store what S0/S1 computed; the caller owns the transaction."""
        self.conn.execute(
            "UPDATE documents SET simhash=?, embedding=COALESCE(?, embedding), norm_text=? "
            "WHERE id=?",
            (simhash, embedding, norm_text, doc_id),
        )

    def clustered_candidates(
        self, since: str | None = None, limit: int = 2000
    ) -> list[sqlite3.Row]:
        """Already-carded documents a new one could join — the dedup blocking pool."""
        where = ["d.simhash <> ''"]
        params: list = []
        if since:
            where.append("(d.published_at >= ? OR d.published_at IS NULL)")
            params.append(since)
        params.append(limit)
        return list(
            self.conn.execute(
                "SELECT d.id, d.simhash, d.embedding, d.title, d.url, "
                "s.item_id, i.cluster_id, i.type, i.npa_key "
                "FROM documents d "
                "JOIN item_sources s ON s.document_id = d.id "
                "JOIN items i ON i.id = s.item_id "
                "WHERE " + " AND ".join(where) + " ORDER BY d.id DESC LIMIT ?",
                params,
            )
        )


class DocumentRevisionRepo:
    """Immutable snapshots behind the mutable `documents` current projection."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @staticmethod
    def _payload(doc: RawDocument) -> str:
        return json.dumps(doc.to_row(), ensure_ascii=False, sort_keys=True)

    def append(self, document_id: int, doc: RawDocument) -> int:
        previous = self.conn.execute(
            "SELECT id,revision FROM document_revisions WHERE document_id=? "
            "ORDER BY revision DESC LIMIT 1",
            (document_id,),
        ).fetchone()
        revision = int(previous["revision"]) + 1 if previous else 1
        cur = self.conn.execute(
            "INSERT INTO document_revisions "
            "(document_id,revision,content_hash,payload,previous_revision_id,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                document_id,
                revision,
                doc.content_hash,
                self._payload(doc),
                int(previous["id"]) if previous else None,
                _now_iso(),
            ),
        )
        return int(cur.lastrowid)

    def list(self, document_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM document_revisions WHERE document_id=? ORDER BY revision",
                (document_id,),
            )
        )


class FetchArtifactRepo:
    """Bounded, sanitized manifest of one source poll; secrets and raw headers never enter it."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(
        self,
        *,
        source_id: int,
        request_id: str,
        requested_at: str,
        completed_at: str,
        status: str,
        manifest: dict | None = None,
        warnings: list[str] | None = None,
    ) -> int:
        body = json.dumps(manifest or {}, ensure_ascii=False, sort_keys=True).encode()[:65536]
        cur = self.conn.execute(
            "INSERT INTO fetch_artifacts "
            "(source_id,request_id,requested_at,completed_at,status,content_type,"
            "sanitized_headers,bounded_body,body_hash,warnings) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                source_id,
                request_id,
                requested_at,
                completed_at,
                status,
                "application/json",
                "{}",
                body,
                sha256_text(body.decode(errors="replace")),
                json.dumps(warnings or [], ensure_ascii=False),
            ),
        )
        return int(cur.lastrowid)


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
            "last_success_at, last_error, consecutive_failures, last_doc_count, cursor, "
            "native_cursor,high_watermark,continuation_cursor,backlog_status,coverage_from,coverage_to,coverage_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source_id) DO UPDATE SET etag=excluded.etag, "
            "last_modified=excluded.last_modified, last_fetch_at=excluded.last_fetch_at, "
            "last_success_at=excluded.last_success_at, last_error=excluded.last_error, "
            "consecutive_failures=excluded.consecutive_failures, "
            "last_doc_count=excluded.last_doc_count, cursor=excluded.cursor, "
            "native_cursor=excluded.native_cursor,high_watermark=excluded.high_watermark, "
            "continuation_cursor=excluded.continuation_cursor,backlog_status=excluded.backlog_status, "
            "coverage_from=excluded.coverage_from,coverage_to=excluded.coverage_to,coverage_status=excluded.coverage_status",
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
                state.native_cursor,
                state.high_watermark,
                json.dumps(state.continuation_cursor, ensure_ascii=False),
                state.backlog_status,
                state.coverage_from,
                state.coverage_to,
                state.coverage_status,
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


class ClusterRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, cluster: Cluster) -> int:
        """Insert a cluster; the caller owns the transaction."""
        cur = self.conn.execute(
            "INSERT INTO clusters (canonical_document_id, centroid_embedding, size, "
            "has_divergent_opinions, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                cluster.canonical_document_id,
                cluster.centroid_embedding,
                cluster.size,
                int(cluster.has_divergent_opinions),
                cluster.created_at or _now_iso(),
            ),
        )
        return int(cur.lastrowid)

    def get(self, cluster_id: int) -> Cluster | None:
        row = self.conn.execute("SELECT * FROM clusters WHERE id=?", (cluster_id,)).fetchone()
        return Cluster.from_row(row) if row else None

    def grow(self, cluster_id: int, count: int = 1, *, divergent: bool | None = None) -> None:
        """`count` more publications joined; a repeated link passes 0 and changes nothing."""
        if count > 0:
            self.conn.execute("UPDATE clusters SET size = size + ? WHERE id=?", (count, cluster_id))
        if divergent:
            self.conn.execute(
                "UPDATE clusters SET has_divergent_opinions = 1 WHERE id=?", (cluster_id,)
            )


class ItemRepo:
    """Cards plus everything hanging off them: entities, sources, events, revisions."""

    _COLUMNS = (
        "cluster_id, type, npa_status, npa_key, title, summary, priority, relevance_score, "
        "reasoning, confidence, tags, analyst_note, is_hidden, is_archived, degraded, "
        "needs_review, date_estimated, model_name, prompt_version, profile_version, "
        "edited_fields, processed_at, published_at"
    )

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, item: Item) -> int:
        row = item.to_row()
        placeholders = ", ".join(f":{c.strip()}" for c in self._COLUMNS.split(","))
        cur = self.conn.execute(
            f"INSERT INTO items ({self._COLUMNS}) VALUES ({placeholders})",
            {**row, "processed_at": row["processed_at"] or _now_iso()},
        )
        item.id = int(cur.lastrowid)
        return item.id

    def update(self, item: Item) -> None:
        row = item.to_row()
        assignments = ", ".join(f"{c.strip()}=:{c.strip()}" for c in self._COLUMNS.split(","))
        self.conn.execute(f"UPDATE items SET {assignments} WHERE id=:id", {**row, "id": item.id})

    def get(self, item_id: int) -> Item | None:
        row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return Item.from_row(row) if row else None

    def by_cluster(self, cluster_id: int) -> Item | None:
        row = self.conn.execute("SELECT * FROM items WHERE cluster_id=?", (cluster_id,)).fetchone()
        return Item.from_row(row) if row else None

    def by_npa_key(self, npa_key: str) -> Item | None:
        """The live card for an act — the join point for every later publication about it."""
        row = self.conn.execute(
            "SELECT * FROM items WHERE npa_key=? AND is_archived=0 LIMIT 1", (npa_key,)
        ).fetchone()
        return Item.from_row(row) if row else None

    def list(
        self,
        *,
        type_: str | None = None,
        priority: str | None = None,
        tag: str | None = None,
        query: str | None = None,
        since: str | None = None,
        limit: int = 20,
        include_hidden: bool = False,
    ) -> list[sqlite3.Row]:
        """Feed rows, newest first; `query` goes through items_fts."""
        where: list[str] = [] if include_hidden else ["i.is_hidden = 0"]
        params: list = []
        if type_:
            where.append("i.type = ?")
            params.append(type_)
        if priority:
            where.append("i.priority = ?")
            params.append(priority)
        if tag:
            where.append("EXISTS (SELECT 1 FROM json_each(i.tags) WHERE json_each.value = ?)")
            params.append(tag)
        if since:
            where.append("(i.published_at >= ? OR i.published_at IS NULL)")
            params.append(since)
        join = ""
        if query:
            join = "JOIN items_fts f ON f.rowid = i.id "
            where.append("items_fts MATCH ?")
            params.append(query)
        params.append(limit)
        sql = (
            "SELECT i.*, c.size AS sources_count FROM items i "
            "JOIN clusters c ON c.id = i.cluster_id "
            + join
            + ("WHERE " + " AND ".join(where) + " " if where else "")
            + "ORDER BY i.published_at DESC, i.id DESC LIMIT ?"
        )
        return list(self.conn.execute(sql, params))

    def count(self) -> int:
        return int(self.conn.execute("SELECT count(*) FROM items").fetchone()[0])

    # -- entities --

    def add_entities(self, item_id: int, spans: Iterable[EntitySpan]) -> None:
        self.conn.executemany(
            "INSERT INTO entities (item_id, role, value, normalized_value, evidence_start, "
            "evidence_end) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (item_id, e.role, e.value, e.normalized_value, e.evidence_start, e.evidence_end)
                for e in spans
            ],
        )

    def entities(self, item_id: int) -> list[EntitySpan]:
        rows = self.conn.execute("SELECT * FROM entities WHERE item_id=? ORDER BY id", (item_id,))
        return [EntitySpan.from_row(r) for r in rows]

    def clear_entities(self, item_id: int) -> None:
        self.conn.execute("DELETE FROM entities WHERE item_id=?", (item_id,))

    # -- sources --

    def link_sources(
        self, item_id: int, document_ids: Iterable[int], canonical_id: int | None = None
    ) -> int:
        """Attach documents to a card; returns how many links are new (0 on a repeat)."""
        cur = self.conn.executemany(
            "INSERT OR IGNORE INTO item_sources (item_id, document_id, is_canonical) "
            "VALUES (?, ?, ?)",
            [(item_id, d, int(d == canonical_id)) for d in dict.fromkeys(document_ids)],
        )
        return max(cur.rowcount, 0)

    def item_for_document(self, document_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT item_id FROM item_sources WHERE document_id=? LIMIT 1", (document_id,)
        ).fetchone()
        return int(row["item_id"]) if row else None

    def sources(self, item_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT d.id, d.url, d.title, d.published_at, s.is_canonical, sr.name AS source_name "
                "FROM item_sources s JOIN documents d ON d.id = s.document_id "
                "JOIN sources sr ON sr.id = d.source_id "
                "WHERE s.item_id=? ORDER BY s.is_canonical DESC, d.id",
                (item_id,),
            )
        )

    # -- npa timeline --

    def add_event(self, event: NpaEvent) -> int:
        cur = self.conn.execute(
            "INSERT INTO npa_events (item_id, status, occurred_at, source_url, note, created_by, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event.item_id,
                event.status,
                event.occurred_at,
                event.source_url,
                event.note,
                event.created_by,
                event.created_at or _now_iso(),
            ),
        )
        return int(cur.lastrowid)

    def events(self, item_id: int) -> list[NpaEvent]:
        rows = self.conn.execute(
            "SELECT * FROM npa_events WHERE item_id=? ORDER BY occurred_at, id", (item_id,)
        )
        return [NpaEvent.from_row(r) for r in rows]

    def has_event(self, item_id: int, status: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM npa_events WHERE item_id=? AND status=? LIMIT 1", (item_id, status)
        ).fetchone()
        return row is not None

    # -- revisions --

    def add_revision(self, revision: ItemRevision) -> int:
        cur = self.conn.execute(
            "INSERT INTO item_revisions (item_id, field, old_value, new_value, actor, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                revision.item_id,
                revision.field,
                revision.old_value,
                revision.new_value,
                revision.actor,
                revision.created_at or _now_iso(),
            ),
        )
        return int(cur.lastrowid)

    def revisions(self, item_id: int) -> list[ItemRevision]:
        rows = self.conn.execute(
            "SELECT * FROM item_revisions WHERE item_id=? ORDER BY id", (item_id,)
        )
        return [ItemRevision.from_row(r) for r in rows]

    def edited_share(self, since: str | None = None) -> float:
        """Share of cards an analyst touched — the honest proxy for model quality."""
        total = self.count()
        if not total:
            return 0.0
        sql = "SELECT count(*) FROM items WHERE edited_fields <> '[]'"
        params: list = []
        if since:
            sql += " AND processed_at >= ?"
            params.append(since)
        edited = int(self.conn.execute(sql, params).fetchone()[0])
        return edited / total


class ProfileRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get(self, profile_id: int) -> CompanyProfile | None:
        row = self.conn.execute(
            "SELECT * FROM company_profiles WHERE id=?", (profile_id,)
        ).fetchone()
        return CompanyProfile.from_row(row) if row else None

    def default(self) -> CompanyProfile | None:
        row = self.conn.execute(
            "SELECT * FROM company_profiles WHERE is_default=1 ORDER BY id LIMIT 1"
        ).fetchone()
        return CompanyProfile.from_row(row) if row else None

    def list(self) -> list[CompanyProfile]:
        return [
            CompanyProfile.from_row(r)
            for r in self.conn.execute("SELECT * FROM company_profiles ORDER BY id")
        ]

    def save(self, profile: CompanyProfile) -> CompanyProfile:
        """Insert or bump the version of an existing profile; never edits in place."""
        payload = json.dumps(profile.payload, ensure_ascii=False)
        existing = self.conn.execute(
            "SELECT * FROM company_profiles WHERE name=?", (profile.name,)
        ).fetchone()
        if existing is None:
            cur = self.conn.execute(
                "INSERT INTO company_profiles (name, payload, version, is_default, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (profile.name, payload, 1, int(profile.is_default), _now_iso()),
            )
            self.conn.commit()
            profile.id, profile.version = int(cur.lastrowid), 1
            return profile
        version = (existing["version"] or 1) + 1
        self.conn.execute(
            "UPDATE company_profiles SET payload=?, version=?, updated_at=? WHERE id=?",
            (payload, version, _now_iso(), existing["id"]),
        )
        self.conn.commit()
        profile.id, profile.version = int(existing["id"]), version
        return profile

    def set_default(self, profile_id: int) -> bool:
        self.conn.execute("UPDATE company_profiles SET is_default=0")
        cur = self.conn.execute(
            "UPDATE company_profiles SET is_default=1 WHERE id=?", (profile_id,)
        )
        self.conn.commit()
        return cur.rowcount > 0


class PromptRepo:
    """Prompt templates by version: a card always says which one produced it."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def ensure(self, stage: str, template: str, model: str, params: dict | None = None) -> int:
        payload = json.dumps(params or {}, ensure_ascii=False, sort_keys=True)
        row = self.conn.execute(
            "SELECT id FROM prompt_versions WHERE stage=? AND template=? AND model=? AND params=?",
            (stage, template, model, payload),
        ).fetchone()
        if row is not None:
            return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO prompt_versions (stage, template, model, params, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (stage, template, model, payload, _now_iso()),
        )
        self.conn.commit()
        return int(cur.lastrowid)


class LlmCallRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, call: LlmCall) -> int:
        cur = self.conn.execute(
            "INSERT INTO llm_calls (item_id, stage, model, tokens_in, tokens_out, latency_ms, "
            "cost, status, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                call.item_id,
                call.stage,
                call.model,
                call.tokens_in,
                call.tokens_out,
                call.latency_ms,
                call.cost,
                call.status,
                call.error,
                call.created_at or _now_iso(),
            ),
        )
        return int(cur.lastrowid)

    def stats(self, since: str | None = None, until: str | None = None) -> sqlite3.Row:
        where, params = [], []
        if since:
            where.append("created_at >= ?")
            params.append(since)
        if until:
            where.append("created_at <= ?")
            params.append(until)
        sql = (
            "SELECT count(*) AS calls, "
            "coalesce(avg(latency_ms), 0) AS avg_latency_ms, "
            "coalesce(sum(tokens_in), 0) AS tokens_in, "
            "coalesce(sum(tokens_out), 0) AS tokens_out, "
            "coalesce(sum(cost), 0) AS cost, "
            "sum(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed "
            "FROM llm_calls" + (" WHERE " + " AND ".join(where) if where else "")
        )
        return self.conn.execute(sql, params).fetchone()
