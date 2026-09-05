"""Миграция до `PRAGMA user_version = 5` на базе, застывшей в форме v4.

v5 заводит `processing_runs`: очередь ИИ видна из дашборда, а не живёт в памяти
процесса. Данных переносить нечего — проверяется, что таблица и индекс появляются
на существующей установке, что свежая база получает ту же форму, что повторное
открытие ничего не перезапускает и что старые строки переживают шаг.
"""

from __future__ import annotations

import logging
import sqlite3

import pytest

from src.repositories import Database
from src.repositories.database import (
    _MIGRATIONS,
    _SCHEMA_V1,
    _SCHEMA_V2,
    _SCHEMA_V3,
    _SCHEMA_V4,
)

NOW = "2026-09-02T12:00:00+00:00"
# v5 — новейший шаг, поэтому литерал версии живёт здесь; более старые файлы
# миграций сверяются с `max(_MIGRATIONS)`.
CURRENT_VERSION = 5

RUN_COLUMNS = [
    "id",
    "started_at",
    "finished_at",
    "status",
    "trigger",
    "params",
    "documents",
    "clusters",
    "items_new",
    "items_joined",
    "items_updated",
    "degraded",
    "needs_review",
    "calls",
    "failed",
    "elapsed_s",
    "error",
]


def _v4_connection(path: str) -> sqlite3.Connection:
    """База, застывшая в форме, которую оставил этап 1.3 (поиск уже есть)."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    for script in (_SCHEMA_V1, _SCHEMA_V2, _SCHEMA_V3, _SCHEMA_V4):
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    return conn


@pytest.fixture
def v4_database(tmp_path) -> str:
    """Наполненная база v4 на диске: один источник и один его документ."""
    path = str(tmp_path / "hub.db")
    conn = _v4_connection(path)
    cur = conn.execute(
        "INSERT INTO sources (name, url, kind, category, fetch_url, status, normalized_url, "
        "poll_interval, created_at, notes) "
        "VALUES ('Ведомости', 'https://vedomosti.ru/', 'rss', 'media', "
        "'https://vedomosti.ru/rss', 'active', 'vedomosti.ru/rss', '6h', ?, '')",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO documents (source_id, external_id, url, title, fetched_at, created_at) "
        "VALUES (?, 'd1', 'https://vedomosti.ru/1', 'Документ до v5', ?, ?)",
        (int(cur.lastrowid), NOW, NOW),
    )
    conn.commit()
    conn.close()
    return path


def _names(db: Database, kind: str) -> list[str]:
    return sorted(
        r[0]
        for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type=? AND name NOT LIKE 'sqlite_%'", (kind,)
        )
    )


def _columns(db: Database, table: str) -> list[str]:
    return [r["name"] for r in db.conn.execute(f"PRAGMA table_info({table})")]


# ── версия ─────────────────────────────────────────────────────────────────


def test_v5_is_the_newest_migration():
    assert max(_MIGRATIONS) == CURRENT_VERSION


def test_opening_a_v4_database_migrates_it_to_v5(v4_database):
    db = Database(v4_database)
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
    finally:
        db.close()


def test_an_in_memory_database_lands_on_v5_too(db):
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION


def test_only_the_v5_step_is_logged_for_a_v4_database(v4_database, caplog):
    """Индекс поиска v4 уже собран: его наполнение не должно запускаться повторно."""
    with caplog.at_level(logging.INFO, logger="db"):
        Database(v4_database).close()

    migrated = [r.getMessage() for r in caplog.records if "schema migrated" in r.getMessage()]
    assert migrated == ["schema migrated to v5"]
    assert "поисковый индекс собран" not in caplog.text


# ── новая схема ────────────────────────────────────────────────────────────


def test_v5_adds_the_processing_runs_table_with_every_column(v4_database):
    db = Database(v4_database)
    try:
        assert "processing_runs" in _names(db, "table")
        assert _columns(db, "processing_runs") == RUN_COLUMNS
    finally:
        db.close()


def test_v5_adds_the_status_index(v4_database):
    db = Database(v4_database)
    try:
        assert "idx_processing_runs_status" in _names(db, "index")
    finally:
        db.close()


def test_a_new_row_starts_as_running_with_zero_counters(v4_database):
    db = Database(v4_database)
    try:
        db.conn.execute("INSERT INTO processing_runs (started_at) VALUES (?)", (NOW,))
        row = db.conn.execute("SELECT * FROM processing_runs").fetchone()
        assert (row["status"], row["trigger"], row["params"], row["error"]) == (
            "running", "cli", "{}", ""
        )
        assert row["finished_at"] is None
        assert [row[c] for c in RUN_COLUMNS[6:15]] == [0] * 9
        assert row["elapsed_s"] == 0
    finally:
        db.close()


@pytest.mark.parametrize("status", ["queued", "ok", "RUNNING", ""], ids=lambda s: s or "empty")
def test_the_status_check_rejects_anything_outside_the_vocabulary(v4_database, status):
    db = Database(v4_database)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            db.conn.execute(
                "INSERT INTO processing_runs (started_at, status) VALUES (?, ?)", (NOW, status)
            )
    finally:
        db.close()


def test_the_repository_works_right_after_the_migration(v4_database):
    db = Database(v4_database)
    try:
        run = db.processing_runs.start({"limit": 5}, trigger="api")
        stored = db.processing_runs.get(run.id)
        assert (stored.status, stored.trigger, stored.params) == ("running", "api", {"limit": 5})
    finally:
        db.close()


# ── старые данные ──────────────────────────────────────────────────────────


def test_existing_rows_survive_the_migration(v4_database):
    db = Database(v4_database)
    try:
        assert [s.name for s in db.sources.list()] == ["Ведомости"]
        assert db.documents.count() == 1
        assert db.documents.get(1).title == "Документ до v5"
    finally:
        db.close()


# ── идемпотентность ────────────────────────────────────────────────────────


def test_reopening_a_migrated_database_does_not_run_the_migration_again(v4_database, caplog):
    with caplog.at_level(logging.INFO, logger="db"):
        first = Database(v4_database)
        run = first.processing_runs.start({"limit": 1})
        first.close()
        assert "schema migrated to v5" in caplog.text
        caplog.clear()

        second = Database(v4_database)
    try:
        assert second.conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
        assert [r.id for r in second.processing_runs.list()] == [run.id]
        assert "schema migrated" not in caplog.text
    finally:
        second.close()


def test_a_fresh_database_lands_on_the_same_v5_shape_as_a_migrated_one(tmp_path, v4_database):
    migrated, fresh = Database(v4_database), Database(str(tmp_path / "fresh.db"))
    try:
        for kind in ("table", "index"):
            assert _names(migrated, kind) == _names(fresh, kind), kind
        assert _columns(migrated, "processing_runs") == _columns(fresh, "processing_runs")
    finally:
        migrated.close()
        fresh.close()
