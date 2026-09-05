"""Миграция до `PRAGMA user_version = 4` на базе, уже полной данных v3.

Индекс `items_search` собирается из четырёх таблиц, поэтому его нельзя наполнить
одним DDL-скриптом: за это отвечает `_backfill_v4`. Проверяется, что после
обновления установки поиск работает по всем существующим карточкам, а не только
по тем, что появятся дальше.
"""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from src.storage import Database
from src.storage.db import _SCHEMA_V1, _SCHEMA_V2, _SCHEMA_V3

NOW = "2026-09-02T12:00:00+00:00"


def _v3_connection(path: str) -> sqlite3.Connection:
    """База, застывшая в форме, которую оставил этап 1.4."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    for script in (_SCHEMA_V1, _SCHEMA_V2, _SCHEMA_V3):
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    return conn


def _v3_source(conn, name: str, fetch_url: str) -> int:
    cur = conn.execute(
        "INSERT INTO sources (name, url, kind, category, fetch_url, status, normalized_url, "
        "poll_interval, created_at, notes) "
        "VALUES (?, ?, 'rss', 'media', ?, 'active', ?, '1h', ?, '')",
        (name, fetch_url, fetch_url, fetch_url.removeprefix("https://"), NOW),
    )
    return int(cur.lastrowid)


def _v3_document(conn, source_id: int, external_id: str, *, norm_text: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO documents (source_id, external_id, url, title, text, norm_text, "
        "fetched_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source_id,
            external_id,
            f"https://old.example.ru/{external_id}",
            f"Документ {external_id}",
            norm_text,
            norm_text,
            NOW,
            NOW,
        ),
    )
    return int(cur.lastrowid)


def _v3_item(conn, document_id: int, *, title="", summary="", tags=(), entities=(), **fields) -> int:
    """Кластер, карточка v3, её связь с документом, теги и сущности."""
    cluster = conn.execute(
        "INSERT INTO clusters (canonical_document_id, size, created_at) VALUES (?, 1, ?)",
        (document_id, NOW),
    )
    cur = conn.execute(
        "INSERT INTO items (cluster_id, type, title, summary, priority, tags, visibility, "
        "manual_overrides, processed_at, published_at) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', ?, ?)",
        (
            int(cluster.lastrowid),
            fields.get("type", "news"),
            title,
            summary,
            fields.get("priority", "medium"),
            json.dumps(list(tags), ensure_ascii=False),
            fields.get("visibility", "visible"),
            NOW,
            fields.get("published_at", NOW),
        ),
    )
    item_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO item_sources (item_id, document_id, is_canonical) VALUES (?, ?, 1)",
        (item_id, document_id),
    )
    conn.executemany(
        "INSERT INTO item_tags (item_id, tag, is_manual) VALUES (?, ?, 0)",
        [(item_id, tag) for tag in tags],
    )
    conn.executemany(
        "INSERT INTO entities (item_id, role, value, normalized_value) VALUES (?, ?, ?, ?)",
        [(item_id, role, value, "") for role, value in entities],
    )
    return item_id


@pytest.fixture
def v3_database(tmp_path):
    """Наполненная база v3 на диске плюс идентификаторы, на которые смотрят тесты."""
    path = str(tmp_path / "hub.db")
    conn = _v3_connection(path)
    source = _v3_source(conn, "Ведомости", "https://vedomosti.ru/rss")
    ids = {
        "rich": _v3_item(
            conn,
            _v3_document(
                conn,
                source,
                "d1",
                norm_text="Ко второму чтению подготовлены поправки о трансграничной передаче.",
            ),
            title="Комитет собрал отзывы",
            summary="Обсуждение продолжится осенью.",
            tags=("регуляторика",),
            entities=(("who", "Роскомнадзор"),),
            type="npa",
            priority="high",
        ),
        "title_only": _v3_item(
            conn,
            _v3_document(conn, source, "d2"),
            title="Карточка без оригинального текста",
        ),
        "empty": _v3_item(conn, _v3_document(conn, source, "d3")),
    }
    conn.commit()
    conn.close()
    return path, ids


def _indexed(db: Database, expression: str) -> list[int]:
    return [
        int(r[0])
        for r in db.conn.execute("SELECT rowid FROM items_search WHERE body MATCH ?", (expression,))
    ]


def _index_rowids(db: Database) -> list[int]:
    return [int(r[0]) for r in db.conn.execute("SELECT rowid FROM items_search ORDER BY rowid")]


# ── новая схема ────────────────────────────────────────────────────────────


def test_opening_a_v3_database_migrates_it_to_v4(v3_database):
    path, _ = v3_database
    db = Database(path)
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        db.close()


def test_v4_adds_the_search_table(v3_database):
    path, _ = v3_database
    db = Database(path)
    try:
        tables = {
            r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "items_search" in tables
    finally:
        db.close()


@pytest.mark.parametrize(
    "index", ["idx_item_sources_canonical", "idx_items_type_priority"], ids=lambda s: s
)
def test_v4_adds_the_indexes_the_feed_leans_on(v3_database, index):
    path, _ = v3_database
    db = Database(path)
    try:
        names = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert index in names
    finally:
        db.close()


def test_the_search_table_keeps_the_tokenizer_the_feed_expects(v3_database):
    """`unicode61 remove_diacritics 2` и префиксы 2-4: на них держится поиск по-русски."""
    path, _ = v3_database
    db = Database(path)
    try:
        ddl = db.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='items_search'"
        ).fetchone()[0]
        assert "unicode61 remove_diacritics 2" in ddl
        assert "prefix = '2 3 4'" in ddl
    finally:
        db.close()


# ── наполнение индекса ─────────────────────────────────────────────────────


def test_the_backfill_indexes_the_cards_that_already_existed(v3_database):
    path, ids = v3_database
    db = Database(path)
    try:
        assert _index_rowids(db) == sorted([ids["rich"], ids["title_only"]])
    finally:
        db.close()


@pytest.mark.parametrize(
    ("expression", "field"),
    [
        ('"Комитет"*', "заголовок"),
        ('"Обсуждение"*', "саммари"),
        ('"регуляторика"*', "тег"),
        ('"Роскомнадзор"*', "сущность"),
        ('"трансграничной"*', "текст оригинала"),
    ],
    ids=["title", "summary", "tag", "entity", "original-text"],
)
def test_the_backfilled_row_is_as_wide_as_the_live_one(v3_database, expression, field):
    path, ids = v3_database
    db = Database(path)
    try:
        assert _indexed(db, expression) == [ids["rich"]], field
    finally:
        db.close()


def test_a_card_with_no_text_at_all_neither_breaks_the_backfill_nor_enters_the_index(v3_database):
    path, ids = v3_database
    db = Database(path)
    try:
        assert db.search.body_for(ids["empty"]) == ""
        assert ids["empty"] not in _index_rowids(db)
        assert _indexed(db, '"Карточка"*') == [ids["title_only"]]
    finally:
        db.close()


def test_the_backfill_says_how_many_cards_it_walked(v3_database, caplog):
    path, _ = v3_database
    with caplog.at_level(logging.INFO, logger="db"):
        db = Database(path)
    db.close()

    assert "поисковый индекс собран по 3 карточкам" in caplog.text


def test_an_empty_v3_database_migrates_without_anything_to_backfill(tmp_path, caplog):
    path = str(tmp_path / "empty.db")
    _v3_connection(path).close()

    with caplog.at_level(logging.INFO, logger="db"):
        db = Database(path)
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert _index_rowids(db) == []
    finally:
        db.close()
    assert "поисковый индекс собран по 0 карточкам" in caplog.text


# ── идемпотентность ────────────────────────────────────────────────────────


def test_reopening_a_migrated_database_does_not_double_the_index(v3_database):
    path, ids = v3_database
    first = Database(path)
    before = _index_rowids(first)
    first.close()

    second = Database(path)
    try:
        assert second.conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert _index_rowids(second) == before
        assert _indexed(second, '"Роскомнадзор"*') == [ids["rich"]]
    finally:
        second.close()


def test_reopening_does_not_rerun_the_backfill(v3_database, caplog):
    path, _ = v3_database
    with caplog.at_level(logging.INFO, logger="db"):
        Database(path).close()
        assert "поисковый индекс собран" in caplog.text
        caplog.clear()

        Database(path).close()

    assert "поисковый индекс собран" not in caplog.text
    assert "schema migrated" not in caplog.text


def test_a_fresh_database_lands_on_the_same_v4_shape_as_a_migrated_one(tmp_path, v3_database):
    migrated_path, _ = v3_database
    migrated, fresh = Database(migrated_path), Database(str(tmp_path / "fresh.db"))
    try:
        for kind in ("table", "index"):
            migrated_names = sorted(
                r[0]
                for r in migrated.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type=? AND name NOT LIKE 'sqlite_%'",
                    (kind,),
                )
            )
            fresh_names = sorted(
                r[0]
                for r in fresh.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type=? AND name NOT LIKE 'sqlite_%'",
                    (kind,),
                )
            )
            assert migrated_names == fresh_names, kind
    finally:
        migrated.close()
        fresh.close()
