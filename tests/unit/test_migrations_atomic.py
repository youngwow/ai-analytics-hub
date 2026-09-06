"""Механизм миграций: одна миграция целиком или никак (план 3.1).

`executescript` делал COMMIT перед запуском, поэтому упавшая на середине
миграция оставляла базу полусобранной, а `PRAGMA user_version` — прежним:
следующее открытие падало на «duplicate column» и установка застревала. Теперь
шаг идёт в `BEGIN IMMEDIATE`, версия ставится в той же транзакции, а уже
применённые ALTER'ы пропускаются — чтобы пострадавшая база доехала.
"""

from __future__ import annotations

import sqlite3

import pytest
from support import frozen_database

from src.models import Cluster, Item, RawDocument, Source
from src.repositories import Database
from src.repositories import database as database_mod
from src.repositories.database import (
    _MIGRATIONS,
    _SCHEMA_V2,
    _already_applied,
    _is_noop,
    _statements,
)

NOW = "2026-09-02T12:00:00+00:00"
# Первый оператор создаёт таблицу, второй падает: ровно та середина, на которой
# старый механизм оставлял базу.
BROKEN_MIGRATION = (
    "CREATE TABLE half_way (id INTEGER PRIMARY KEY);\n"
    "INSERT INTO no_such_table (id) VALUES (1);\n"
)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


# ── _statements: скрипт делится на операторы, а не по точкам с запятой ─────


def test_a_trigger_body_stays_one_statement():
    """Тело триггера само содержит `;` — деление по нему разорвало бы его пополам."""
    trigger = [s for s in _statements(_SCHEMA_V2) if s.startswith("CREATE TRIGGER")]

    assert len(trigger) == 3
    body = next(s for s in trigger if "items_fts_au" in s)
    assert body.endswith("END;")
    assert body.count("INSERT INTO items_fts") == 2


def test_plain_statements_are_split_one_per_element():
    script = "CREATE TABLE a (id INTEGER);\nCREATE TABLE b (id INTEGER);\n"

    assert _statements(script) == ["CREATE TABLE a (id INTEGER);", "CREATE TABLE b (id INTEGER);"]


@pytest.mark.parametrize("version", sorted(_MIGRATIONS), ids=lambda v: f"v{v}")
def test_every_migration_splits_into_complete_statements(version):
    for statement in _statements(_MIGRATIONS[version]):
        assert sqlite3.complete_statement(statement), statement


def test_a_comment_only_tail_is_not_a_statement():
    script = "CREATE TABLE a (id INTEGER);\n-- дальше только пояснение\n"

    assert _statements(script) == ["CREATE TABLE a (id INTEGER);"]


def test_an_empty_script_has_no_statements():
    assert _statements("") == []
    assert _statements("\n\n   \n") == []


@pytest.mark.parametrize(
    ("statement", "noop"),
    [
        ("-- одно пояснение", True),
        ("-- две\n-- строки пояснения", True),
        (";", True),
        ("   ", True),
        ("CREATE TABLE a (id INTEGER);", False),
        ("-- пояснение\nCREATE TABLE a (id INTEGER);", False),
    ],
    ids=["comment", "comments", "semicolon", "blank", "ddl", "commented-ddl"],
)
def test_is_noop_only_skips_what_carries_no_sql(statement, noop):
    assert _is_noop(statement) is noop


# ── _already_applied: ALTER не идемпотентен, поэтому сверяется со схемой ───


@pytest.mark.parametrize(
    ("statement", "applied"),
    [
        ("ALTER TABLE documents ADD COLUMN last_error TEXT NOT NULL DEFAULT ''", True),
        ("ALTER TABLE documents ADD COLUMN совсем_новая TEXT", False),
        ("alter table documents add column last_error TEXT", True),
        ("ALTER TABLE items DROP COLUMN is_hidden", True),
        ("ALTER TABLE items DROP COLUMN title", False),
        ("ALTER TABLE items RENAME COLUMN edited_fields TO manual_overrides", True),
        ("ALTER TABLE items RENAME COLUMN title TO heading", False),
        ("CREATE INDEX IF NOT EXISTS idx_new ON items(title)", False),
        ("UPDATE items SET analyst_note = ''", False),
    ],
    ids=[
        "add-existing-column",
        "add-new-column",
        "add-existing-lowercase",
        "drop-column-already-gone",
        "drop-column-still-there",
        "rename-already-done",
        "rename-not-done",
        "create-index",
        "update",
    ],
)
def test_already_applied_reads_the_current_schema(db, statement, applied):
    assert _already_applied(db.conn, statement) is applied


def test_a_skipped_statement_is_reported_in_the_log(tmp_path, caplog):
    """Пропуск — не тишина: иначе непонятно, почему миграция «ничего не сделала»."""
    path = str(tmp_path / "hub.db")
    conn = frozen_database(path, 6)
    conn.executescript("ALTER TABLE documents ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;")
    conn.commit()
    conn.close()

    with caplog.at_level("INFO", logger="db"):
        Database(path).close()

    assert "миграция v7: пропущено «ALTER TABLE documents ADD COLUMN attempts" in caplog.text


# ── атомарность: версия и DDL живут в одной транзакции ────────────────────


@pytest.fixture
def populated(tmp_path) -> str:
    """Файловая база текущей версии с одним источником — ей и предстоит уцелеть."""
    path = str(tmp_path / "hub.db")
    db = Database(path)
    db.sources.add(
        Source(name="Ведомости", url="https://vedomosti.ru/", kind="rss",
               fetch_url="https://vedomosti.ru/rss")
    )
    db.close()
    return path


def test_a_migration_that_fails_halfway_rolls_back_its_ddl_and_the_version(
    populated, monkeypatch
):
    current = max(_MIGRATIONS)
    monkeypatch.setitem(database_mod._MIGRATIONS, current + 1, BROKEN_MIGRATION)

    with pytest.raises(sqlite3.OperationalError, match="no_such_table"):
        Database(populated)

    conn = sqlite3.connect(populated)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == current
        assert "half_way" not in _tables(conn)
    finally:
        conn.close()


def test_the_database_opens_normally_once_the_broken_step_is_gone(populated, monkeypatch):
    current = max(_MIGRATIONS)
    monkeypatch.setitem(database_mod._MIGRATIONS, current + 1, BROKEN_MIGRATION)
    with pytest.raises(sqlite3.OperationalError):
        Database(populated)
    monkeypatch.delitem(database_mod._MIGRATIONS, current + 1)

    db = Database(populated)
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == current
        assert [s.name for s in db.sources.list()] == ["Ведомости"]
    finally:
        db.close()


def test_the_python_backfill_is_rolled_back_with_the_rest_of_the_step(tmp_path, monkeypatch):
    """Донаполнение из python — часть той же транзакции, а не отдельная запись."""
    path = str(tmp_path / "hub.db")
    conn = frozen_database(path, 3)
    conn.execute(
        "INSERT INTO sources (name, url, kind, category, fetch_url, status, normalized_url, "
        "poll_interval, created_at, notes) VALUES ('Ведомости', 'https://vedomosti.ru/', 'rss', "
        "'media', 'https://vedomosti.ru/rss', 'active', '', '1h', ?, '')",
        (NOW,),
    )
    conn.commit()
    conn.close()

    def boom(_conn):
        raise RuntimeError("донаполнение упало")

    monkeypatch.setitem(database_mod._AFTER_MIGRATION, 4, boom)
    with pytest.raises(RuntimeError, match="донаполнение упало"):
        Database(path)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert "items_search" not in _tables(conn)
    finally:
        conn.close()


# ── база, пострадавшая от старого механизма, доезжает ──────────────────────


@pytest.fixture
def half_migrated_v2(tmp_path) -> tuple[str, dict[str, int]]:
    """v2-база, которой старый `executescript` успел применить часть v3 и упасть."""
    path = str(tmp_path / "hub.db")
    conn = frozen_database(path, 2)
    source = int(
        conn.execute(
            "INSERT INTO sources (name, url, kind, category, fetch_url, enabled, created_at, "
            "notes) VALUES ('Ведомости', 'https://vedomosti.ru/', 'rss', 'media', "
            "'https://vedomosti.ru/rss', 1, ?, '')",
            (NOW,),
        ).lastrowid
    )
    document = int(
        conn.execute(
            "INSERT INTO documents (source_id, external_id, url, title, fetched_at, created_at) "
            "VALUES (?, 'd1', 'https://vedomosti.ru/1', 'Документ до v3', ?, ?)",
            (source, NOW, NOW),
        ).lastrowid
    )
    cluster = conn.execute(
        "INSERT INTO clusters (canonical_document_id, size, created_at) VALUES (?, 1, ?)",
        (document, NOW),
    ).lastrowid
    item = int(
        conn.execute(
            "INSERT INTO items (cluster_id, type, title, summary, priority, tags, analyst_note, "
            "is_hidden, edited_fields, processed_at) "
            "VALUES (?, 'news', 'Скрытая карточка', 'Саммари', 'medium', '[]', '', 1, ?, ?)",
            (int(cluster), '["summary"]', NOW),
        ).lastrowid
    )
    # Ровно то, что успел старый механизм: переименование и две первые колонки.
    conn.executescript(
        "ALTER TABLE items RENAME COLUMN edited_fields TO manual_overrides;\n"
        "ALTER TABLE items ADD COLUMN visibility TEXT NOT NULL DEFAULT 'visible';\n"
        "ALTER TABLE items ADD COLUMN hidden_reason TEXT NOT NULL DEFAULT '';\n"
    )
    conn.execute("PRAGMA user_version = 2")  # версия так и не поднялась
    conn.commit()
    conn.close()
    return path, {"source": source, "document": document, "item": item}


def test_a_half_applied_v3_finishes_instead_of_failing_on_a_duplicate_column(half_migrated_v2):
    path, ids = half_migrated_v2

    db = Database(path)
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == max(_MIGRATIONS)
        columns = _columns(db.conn, "items")
        assert {"manual_overrides", "visibility", "hidden_reason", "origin"} <= columns
        assert "is_hidden" not in columns
        assert "edited_fields" not in columns
    finally:
        db.close()


def test_a_note_already_moved_by_a_half_run_v3_is_not_duplicated(tmp_path):
    """Старый механизм успел создать `item_notes` и перенести заметку — второй раз нельзя."""
    path = str(tmp_path / "hub.db")
    conn = frozen_database(path, 2)
    source = int(
        conn.execute(
            "INSERT INTO sources (name, url, kind, category, fetch_url, enabled, created_at, "
            "notes) VALUES ('Ведомости', 'https://vedomosti.ru/', 'rss', 'media', "
            "'https://vedomosti.ru/rss', 1, ?, '')",
            (NOW,),
        ).lastrowid
    )
    document = int(
        conn.execute(
            "INSERT INTO documents (source_id, external_id, url, title, fetched_at, created_at) "
            "VALUES (?, 'd1', 'https://vedomosti.ru/1', 'Док', ?, ?)",
            (source, NOW, NOW),
        ).lastrowid
    )
    cluster = conn.execute(
        "INSERT INTO clusters (canonical_document_id, size, created_at) VALUES (?, 1, ?)",
        (document, NOW),
    ).lastrowid
    item = int(
        conn.execute(
            "INSERT INTO items (cluster_id, type, title, summary, priority, tags, analyst_note, "
            "is_hidden, edited_fields, processed_at) "
            "VALUES (?, 'news', 'Заголовок', '', 'medium', '[]', 'Проверить у юристов', 0, '[]', ?)",
            (int(cluster), NOW),
        ).lastrowid
    )
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS item_notes (id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL "
        "REFERENCES items(id) ON DELETE CASCADE, body TEXT NOT NULL, author TEXT NOT NULL "
        "DEFAULT '', created_at TEXT NOT NULL);"
    )
    conn.execute(
        "INSERT INTO item_notes (item_id, body, author, created_at) VALUES (?, ?, '', ?)",
        (item, "Проверить у юристов", NOW),
    )
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        assert [n.body for n in db.notes.list(item)] == ["Проверить у юристов"]
    finally:
        db.close()


def test_the_data_moves_of_the_half_applied_step_still_run(half_migrated_v2):
    """Колонки уже были, а `UPDATE ... WHERE is_hidden = 1` — ещё нет: он обязан пройти."""
    path, ids = half_migrated_v2

    db = Database(path)
    try:
        item = db.items.get(ids["item"])
        assert item.visibility == "hidden_feed"
        assert item.manual_overrides == ["summary"]
        assert db.sources.get(ids["source"]).status == "active"
        assert db.documents.get(ids["document"]).title == "Документ до v3"
    finally:
        db.close()


# ── SearchRepo.rebuild_all: транзакцией владеет вызывающий ─────────────────


def test_rebuild_all_does_not_commit_the_transaction_it_runs_in(db):
    source = db.sources.add(
        Source(name="Лента", url="https://a.ru/", kind="rss", fetch_url="https://a.ru/rss")
    )
    with db.transaction():
        document_id = db.documents.insert(
            RawDocument(source_id=source.id, external_id="a", url="https://a.ru/a",
                        title="Материал", fetched_at=NOW)
        )

    db.conn.execute("BEGIN IMMEDIATE")
    cluster_id = db.clusters.add(Cluster(canonical_document_id=document_id, created_at=NOW))
    item_id = db.items.add(
        Item(cluster_id=cluster_id, title="Минцифры расширило реестр", processed_at=NOW)
    )
    db.items.link_sources(item_id, [document_id], document_id)
    assert db.search.rebuild_all() == 1
    db.conn.rollback()

    assert db.items.count() == 0
    assert db.conn.execute("SELECT count(*) FROM items_search").fetchone()[0] == 0
