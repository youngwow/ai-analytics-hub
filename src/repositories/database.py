"""SQLite: схема, миграции и фасад `Database`, раздающий репозитории.

Соединение открывается на запрос (HTTP) или на прогон (CLI) и закрывается после;
`check_same_thread=False` нужен потому, что FastAPI выполняет зависимость с
`yield` и обработчик в разных потоках пула — соединение всё равно используется
одним запросом последовательно. WAL + busy_timeout позволяют дашборду читать,
пока `collect --watch` пишет. Схема меняется только через `_MIGRATIONS` по
`PRAGMA user_version` (принцип V конституции)."""

from __future__ import annotations

import os
import re
import sqlite3

from ..sources.textutil import normalized_source_url
from ..utils import get_logger, to_utc_iso, utc_now
from .documents import SqliteDocumentRepository
from .items import ClusterRepo, ItemNoteRepo, ItemTagRepo, SearchRepo, SqliteItemRepository
from .processing import LlmCallRepo, ProcessingRunRepo, ProfileRepo, PromptRepo, RunRepo
from .repository_interface import DuplicateSourceError
from .sources import (
    MANUAL_FETCH_URL,
    MANUAL_SOURCE_NAME,
    FetchStateRepo,
    SeenUrlRepo,
    SourceRunRepo,
    SqliteSourceRepository,
)

log = get_logger("db")

__all__ = ["Database", "DuplicateSourceError", "MANUAL_FETCH_URL", "MANUAL_SOURCE_NAME"]


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
ALTER TABLE items RENAME COLUMN edited_fields TO manual_overrides;

ALTER TABLE items ADD COLUMN visibility TEXT NOT NULL DEFAULT 'visible'
    CHECK (visibility IN ('visible','hidden_feed','hidden_digest','deleted'));
ALTER TABLE items ADD COLUMN hidden_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE items ADD COLUMN origin TEXT NOT NULL DEFAULT 'collected'
    CHECK (origin IN ('collected','manual'));
UPDATE items SET visibility = 'hidden_feed' WHERE is_hidden = 1;
ALTER TABLE items DROP COLUMN is_hidden;
CREATE INDEX IF NOT EXISTS idx_items_visibility ON items(visibility);

ALTER TABLE sources ADD COLUMN status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','paused','error','deleted'));
UPDATE sources SET status = CASE enabled WHEN 1 THEN 'active' ELSE 'paused' END;
ALTER TABLE sources DROP COLUMN enabled;
ALTER TABLE sources ADD COLUMN normalized_url TEXT NOT NULL DEFAULT '';
ALTER TABLE sources ADD COLUMN poll_interval TEXT NOT NULL DEFAULT '1h'
    CHECK (poll_interval IN ('15m','1h','6h','24h'));
ALTER TABLE sources ADD COLUMN next_run_at TEXT;
ALTER TABLE sources ADD COLUMN category_hint TEXT
    CHECK (category_hint IS NULL OR category_hint IN ('npa','news'));
ALTER TABLE sources ADD COLUMN deleted_at TEXT;
ALTER TABLE sources ADD COLUMN created_by TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_normalized ON sources(normalized_url)
    WHERE status <> 'deleted' AND normalized_url <> '';
CREATE INDEX IF NOT EXISTS idx_sources_due ON sources(next_run_at)
    WHERE status IN ('active', 'error');

CREATE TABLE IF NOT EXISTS source_runs (
    id            INTEGER PRIMARY KEY,
    source_id     INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    http_status   INTEGER,
    items_found   INTEGER NOT NULL DEFAULT 0,
    items_new     INTEGER NOT NULL DEFAULT 0,
    error_code    TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_source_runs_source ON source_runs(source_id, started_at DESC);

CREATE TABLE IF NOT EXISTS item_notes (
    id         INTEGER PRIMARY KEY,
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    body       TEXT NOT NULL,
    author     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_item_notes_item ON item_notes(item_id, created_at);
INSERT INTO item_notes (item_id, body, author, created_at)
    SELECT id, analyst_note, '', processed_at FROM items WHERE analyst_note <> ''
      AND NOT EXISTS (SELECT 1 FROM item_notes n WHERE n.item_id = items.id);

CREATE TABLE IF NOT EXISTS item_tags (
    item_id   INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tag       TEXT NOT NULL,
    is_manual INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (item_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_item_tags_tag ON item_tags(tag);
INSERT OR IGNORE INTO item_tags (item_id, tag, is_manual)
    SELECT i.id, j.value, 0 FROM items i, json_each(i.tags) j;

ALTER TABLE item_revisions ADD COLUMN source_of_change TEXT NOT NULL DEFAULT 'human'
    CHECK (source_of_change IN ('llm','human'));
ALTER TABLE item_revisions ADD COLUMN edit_reason TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_item_revisions_field
    ON item_revisions(item_id, field, created_at DESC);
"""


_SCHEMA_V4 = """
CREATE VIRTUAL TABLE IF NOT EXISTS items_search USING fts5(
    body,
    tokenize = 'unicode61 remove_diacritics 2',
    prefix = '2 3 4'
);
CREATE INDEX IF NOT EXISTS idx_item_sources_canonical ON item_sources(item_id, is_canonical);
CREATE INDEX IF NOT EXISTS idx_items_type_priority ON items(type, priority);
"""


# v5: очередь ИИ видна из дашборда — прогоны обработки хранятся, а не живут в памяти процесса.
_SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS processing_runs (
    id            INTEGER PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','done','failed')),
    trigger       TEXT NOT NULL DEFAULT 'cli',
    params        TEXT NOT NULL DEFAULT '{}',
    documents     INTEGER NOT NULL DEFAULT 0,
    clusters      INTEGER NOT NULL DEFAULT 0,
    items_new     INTEGER NOT NULL DEFAULT 0,
    items_joined  INTEGER NOT NULL DEFAULT 0,
    items_updated INTEGER NOT NULL DEFAULT 0,
    degraded      INTEGER NOT NULL DEFAULT 0,
    needs_review  INTEGER NOT NULL DEFAULT 0,
    calls         INTEGER NOT NULL DEFAULT 0,
    failed        INTEGER NOT NULL DEFAULT 0,
    elapsed_s     REAL NOT NULL DEFAULT 0,
    error         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_processing_runs_status ON processing_runs(status, id DESC);
"""


# v6: у ленты один индекс — `items_search` (v4). `items_fts` из v2 обслуживал
# только недостижимый путь `list(query=...)`, а три его триггера дорожали каждую
# запись карточки.
_SCHEMA_V6 = """
DROP TRIGGER IF EXISTS items_fts_ai;
DROP TRIGGER IF EXISTS items_fts_ad;
DROP TRIGGER IF EXISTS items_fts_au;
DROP TABLE IF EXISTS items_fts;
"""


# v7: сбой обработки перестаёт быть строчкой в логе (документ помнит свою ошибку),
# а прогон отчитывается о прогрессе, пока идёт, а не только в конце.
_SCHEMA_V7 = """
ALTER TABLE documents ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE documents ADD COLUMN last_error TEXT NOT NULL DEFAULT '';
ALTER TABLE documents ADD COLUMN last_attempt_at TEXT;
CREATE INDEX IF NOT EXISTS idx_documents_failed ON documents(last_error, published_at DESC)
    WHERE last_error <> '';
ALTER TABLE processing_runs ADD COLUMN processed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE processing_runs ADD COLUMN heartbeat_at TEXT;
"""


# v8: у заметки аналитика один дом — `item_notes`. Колонка `items.analyst_note`
# осталась с v2, v3 перенесла её содержимое, но CLI продолжал писать в колонку.
# Здесь переезжают остатки, дальше колонка не заполняется (в ответе она осталась
# ради совместимости и всегда пуста).
_SCHEMA_V8 = """
INSERT INTO item_notes (item_id, body, author, created_at)
    SELECT id, analyst_note, '', COALESCE(processed_at, '') FROM items
    WHERE analyst_note <> ''
      AND NOT EXISTS (
          SELECT 1 FROM item_notes n WHERE n.item_id = items.id AND n.body = items.analyst_note
      );
UPDATE items SET analyst_note = '' WHERE analyst_note <> '';
"""


_MIGRATIONS: dict[int, str] = {
    1: _SCHEMA_V1,
    2: _SCHEMA_V2,
    3: _SCHEMA_V3,
    4: _SCHEMA_V4,
    5: _SCHEMA_V5,
    6: _SCHEMA_V6,
    7: _SCHEMA_V7,
    8: _SCHEMA_V8,
}


_ADD_COLUMN_RE = re.compile(r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)", re.I)
_DROP_COLUMN_RE = re.compile(r"ALTER\s+TABLE\s+(\w+)\s+DROP\s+COLUMN\s+(\w+)", re.I)
_RENAME_COLUMN_RE = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+RENAME\s+COLUMN\s+(\w+)\s+TO\s+(\w+)", re.I
)


def _statements(script: str) -> list[str]:
    """Разбить скрипт миграции на отдельные операторы.

    Делить по `;` нельзя: тело триггера само содержит `;` до своего `END`.
    `sqlite3.complete_statement` знает это правило — на нём построена и
    интерактивная оболочка из документации Python.
    """
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if buffer.strip() and sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    tail = buffer.strip()
    if tail:
        statements.append(tail)
    return [s for s in statements if not _is_noop(s)]


def _is_noop(statement: str) -> bool:
    """Хвост из одних комментариев — не оператор."""
    body = "\n".join(
        line for line in statement.splitlines() if not line.strip().startswith("--")
    )
    return not body.strip().strip(";")


def _first_line(statement: str) -> str:
    return statement.splitlines()[0].strip()[:80]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _already_applied(conn: sqlite3.Connection, statement: str) -> bool:
    """`CREATE ... IF NOT EXISTS` идемпотентен сам, а ALTER — нет: сверяемся со схемой."""
    add = _ADD_COLUMN_RE.match(statement)
    if add:
        return add.group(2) in _columns(conn, add.group(1))
    drop = _DROP_COLUMN_RE.match(statement)
    if drop:
        return drop.group(2) not in _columns(conn, drop.group(1))
    rename = _RENAME_COLUMN_RE.match(statement)
    if rename:
        columns = _columns(conn, rename.group(1))
        return rename.group(3) in columns and rename.group(2) not in columns
    return False


def _now_iso() -> str:
    return to_utc_iso(utc_now()) or ""


def _backfill_v3(conn: sqlite3.Connection) -> None:
    """Fill `normalized_url` and start the schedule for sources that predate v3.

    Not part of the SQL script: normalisation must be the same function the
    resolver uses, otherwise the same channel gets two spellings and the unique
    index stops catching duplicates.
    """

    now = _now_iso()
    seen: dict[str, int] = {}
    for row in conn.execute("SELECT id, name, url, fetch_url FROM sources ORDER BY id").fetchall():
        key = normalized_source_url(row["fetch_url"] or row["url"])
        if key in seen:
            # The pool predates the unique index and already holds two spellings of
            # one address. Keep both rows, leave the later one unnormalised (the
            # index skips empty values) and say so — merging is the operator's call.
            log.warning(
                "источник #%s «%s» повторяет #%s по адресу %s — normalized_url оставлен пустым",
                row["id"],
                row["name"],
                seen[key],
                key,
            )
            key = ""
        elif key:
            seen[key] = int(row["id"])
        conn.execute(
            "UPDATE sources SET normalized_url=?, next_run_at=COALESCE(next_run_at, ?) WHERE id=?",
            (key, now, row["id"]),
        )


def _backfill_v4(conn: sqlite3.Connection) -> None:
    """Наполнить поисковый индекс по уже существующим карточкам.

    Строка индекса собирается из четырёх таблиц, поэтому её нельзя сложить
    средствами одного SQL-скрипта миграции.
    """
    repo = SearchRepo(conn)
    rebuilt = repo.rebuild_all()
    log.info("поисковый индекс собран по %d карточкам", rebuilt)


_AFTER_MIGRATION = {3: _backfill_v3, 4: _backfill_v4}


class Database:
    """Owns the connection and exposes repositories as attributes."""

    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()
        self.sources = SqliteSourceRepository(self.conn)
        self.documents = SqliteDocumentRepository(self.conn)
        self.fetch_state = FetchStateRepo(self.conn)
        self.seen_urls = SeenUrlRepo(self.conn)
        self.runs = RunRepo(self.conn)
        self.clusters = ClusterRepo(self.conn)
        self.items = SqliteItemRepository(self.conn)
        self.profiles = ProfileRepo(self.conn)
        self.prompts = PromptRepo(self.conn)
        self.llm_calls = LlmCallRepo(self.conn)
        self.processing_runs = ProcessingRunRepo(self.conn)
        self.source_runs = SourceRunRepo(self.conn)
        self.notes = ItemNoteRepo(self.conn)
        self.tags = ItemTagRepo(self.conn)
        self.search = SearchRepo(self.conn)

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for target in sorted(_MIGRATIONS):
            if version < target:
                self._apply(target)
                log.info("schema migrated to v%d", target)
                version = target

    def _apply(self, target: int) -> None:
        """Одна миграция целиком или никак.

        `executescript` здесь нельзя: он делает COMMIT перед запуском, поэтому
        упавшая на середине миграция оставляла базу полусобранной, а
        `user_version` — прежним, и следующее открытие падало на «duplicate
        column». Здесь DDL, python-донаполнение и `PRAGMA user_version` живут в
        одной транзакции: SQLite держит `user_version` в заголовке файла и
        откатывает его вместе с остальным. Уже применённые ALTER'ы
        пропускаются — чтобы база, пострадавшая от старого механизма, доехала.
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _statements(_MIGRATIONS[target]):
                if _already_applied(self.conn, statement):
                    log.info("миграция v%d: пропущено «%s»", target, _first_line(statement))
                    continue
                self.conn.execute(statement)
            after = _AFTER_MIGRATION.get(target)
            if after is not None:
                after(self.conn)
            self.conn.execute(f"PRAGMA user_version = {target}")
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()

    def transaction(self):
        """`with db.transaction():` — commit on success, roll back on exception."""
        return self.conn

    def ping(self) -> bool:
        """Дешёвая проверка живости хранилища — для `/health/ready`."""
        return self.conn.execute("SELECT 1").fetchone()[0] == 1

    def close(self) -> None:
        self.conn.close()
