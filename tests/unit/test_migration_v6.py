"""Миграция до `PRAGMA user_version = 6` на базе, застывшей в форме v5.

v6 снимает балласт: `items_fts` из v2 и три его триггера обслуживали единственный
недостижимый путь `SqliteItemRepository.list(query=...)`, но дорожали каждую
запись карточки. Рабочий индекс ленты — `items_search` (v4), и он вместе со
своими строками обязан пережить шаг.
"""

from __future__ import annotations

import logging

import pytest
from support import frozen_database

from src.repositories import Database
from src.repositories.database import _MIGRATIONS

NOW = "2026-09-02T12:00:00+00:00"
CURRENT_VERSION = max(_MIGRATIONS)
FTS_TRIGGERS = {"items_fts_ai", "items_fts_ad", "items_fts_au"}
INDEXED_BODY = "Комитет собрал отзывы о трансграничной передаче"


@pytest.fixture
def v5_database(tmp_path) -> tuple[str, int]:
    """База v5 с одной карточкой и её строкой в `items_search`."""
    path = str(tmp_path / "hub.db")
    conn = frozen_database(path, 5)
    source = conn.execute(
        "INSERT INTO sources (name, url, kind, category, fetch_url, status, normalized_url, "
        "poll_interval, created_at, notes) VALUES ('Ведомости', 'https://vedomosti.ru/', 'rss', "
        "'media', 'https://vedomosti.ru/rss', 'active', 'vedomosti.ru/rss', '6h', ?, '')",
        (NOW,),
    ).lastrowid
    document = conn.execute(
        "INSERT INTO documents (source_id, external_id, url, title, fetched_at, created_at) "
        "VALUES (?, 'd1', 'https://vedomosti.ru/1', 'Документ до v6', ?, ?)",
        (int(source), NOW, NOW),
    ).lastrowid
    cluster = conn.execute(
        "INSERT INTO clusters (canonical_document_id, size, created_at) VALUES (?, 1, ?)",
        (int(document), NOW),
    ).lastrowid
    item_id = int(
        conn.execute(
            "INSERT INTO items (cluster_id, type, title, summary, priority, tags, visibility, "
            "manual_overrides, processed_at, published_at) "
            "VALUES (?, 'npa', 'Комитет собрал отзывы', 'Обсуждение осенью.', 'high', '[]', "
            "'visible', '[]', ?, ?)",
            (int(cluster), NOW, NOW),
        ).lastrowid
    )
    conn.execute("INSERT INTO items_search(rowid, body) VALUES (?, ?)", (item_id, INDEXED_BODY))
    conn.commit()
    conn.close()
    return path, item_id


def _names(db: Database, kind: str) -> list[str]:
    return sorted(
        r[0]
        for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type=? AND name NOT LIKE 'sqlite_%'", (kind,)
        )
    )


def _searched(db: Database, expression: str) -> list[int]:
    return [
        int(r[0])
        for r in db.conn.execute(
            "SELECT rowid FROM items_search WHERE body MATCH ?", (expression,)
        )
    ]


# ── версия ─────────────────────────────────────────────────────────────────


def test_opening_a_v5_database_migrates_it_past_v6(v5_database):
    path, _ = v5_database
    db = Database(path)
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
        assert CURRENT_VERSION >= 6
    finally:
        db.close()


# ── балласт снят ───────────────────────────────────────────────────────────


def test_v6_drops_the_second_index_table(v5_database):
    path, _ = v5_database
    db = Database(path)
    try:
        assert "items_fts" not in _names(db, "table")
    finally:
        db.close()


def test_v6_drops_all_three_write_triggers(v5_database):
    """Каждая запись карточки шла через три триггера впустую — их больше нет."""
    path, _ = v5_database
    db = Database(path)
    try:
        assert set(_names(db, "trigger")) & FTS_TRIGGERS == set()
        assert _names(db, "trigger") == []
    finally:
        db.close()


def test_a_v5_database_really_had_them_before_the_migration(tmp_path):
    """Иначе предыдущие два теста прошли бы и на пустой схеме."""
    conn = frozen_database(str(tmp_path / "before.db"), 5)
    try:
        names = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master")
        }
        assert "items_fts" in names
        assert FTS_TRIGGERS <= names
    finally:
        conn.close()


# ── рабочий индекс цел ─────────────────────────────────────────────────────


def test_the_feed_index_survives_with_its_rows(v5_database):
    path, item_id = v5_database
    db = Database(path)
    try:
        assert "items_search" in _names(db, "table")
        assert _searched(db, '"трансграничной"*') == [item_id]
    finally:
        db.close()


def test_the_card_itself_is_untouched(v5_database):
    path, item_id = v5_database
    db = Database(path)
    try:
        item = db.items.get(item_id)
        assert (item.title, item.type, item.priority) == ("Комитет собрал отзывы", "npa", "high")
        assert db.documents.count() == 1
    finally:
        db.close()


def test_writing_a_card_after_the_migration_still_updates_the_feed_index(v5_database):
    """Строку индекса собирает `SearchRepo`, а не триггер: запись должна работать."""
    path, item_id = v5_database
    db = Database(path)
    try:
        item = db.items.get(item_id)
        item.title = "Спутниковый оператор обновил телегид"
        with db.transaction():
            db.items.update(item)
            db.search.rebuild(item_id)
        assert _searched(db, '"телегид"*') == [item_id]
        assert _searched(db, '"трансграничной"*') == []
    finally:
        db.close()


# ── форма и идемпотентность ────────────────────────────────────────────────


def test_a_fresh_database_lands_on_the_same_shape_as_a_migrated_one(tmp_path, v5_database):
    path, _ = v5_database
    migrated, fresh = Database(path), Database(str(tmp_path / "fresh.db"))
    try:
        for kind in ("table", "index", "trigger"):
            assert _names(migrated, kind) == _names(fresh, kind), kind
    finally:
        migrated.close()
        fresh.close()


def test_reopening_a_migrated_database_does_not_run_the_step_again(v5_database, caplog):
    path, item_id = v5_database
    with caplog.at_level(logging.INFO, logger="db"):
        Database(path).close()
        assert "schema migrated to v6" in caplog.text
        caplog.clear()

        second = Database(path)
    try:
        assert second.conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
        assert "schema migrated" not in caplog.text
        assert _searched(second, '"трансграничной"*') == [item_id]
    finally:
        second.close()
