"""Миграция до `PRAGMA user_version = 7` на базе, застывшей в форме v6.

v7 отвечает на два вопроса, на которые схема раньше ответить не могла: почему
документ до сих пор без карточки (`attempts`, `last_error`, `last_attempt_at`) и
далеко ли зашёл идущий прогон (`processing_runs.processed`, `heartbeat_at`).
Данных переносить нечего — проверяются колонки, их значения по умолчанию,
частичный индекс сбойных документов и то, что старые строки живы.
"""

from __future__ import annotations

import logging

import pytest
from support import frozen_database

from src.repositories import Database
from src.repositories.database import _MIGRATIONS

NOW = "2026-09-02T12:00:00+00:00"
CURRENT_VERSION = max(_MIGRATIONS)
DOCUMENT_COLUMNS = ("attempts", "last_error", "last_attempt_at")
RUN_COLUMNS = ("processed", "heartbeat_at")


@pytest.fixture
def v6_database(tmp_path) -> tuple[str, dict[str, int]]:
    """База v6 с источником, документом и открытым прогоном обработки."""
    path = str(tmp_path / "hub.db")
    conn = frozen_database(path, 6)
    source = int(
        conn.execute(
            "INSERT INTO sources (name, url, kind, category, fetch_url, status, normalized_url, "
            "poll_interval, created_at, notes) VALUES ('Банк России', 'https://cbr.ru/', 'rss', "
            "'regulator', 'https://cbr.ru/rss', 'active', 'cbr.ru/rss', '1h', ?, '')",
            (NOW,),
        ).lastrowid
    )
    document = int(
        conn.execute(
            "INSERT INTO documents (source_id, external_id, url, title, fetched_at, created_at) "
            "VALUES (?, 'd1', 'https://cbr.ru/1', 'Документ до v7', ?, ?)",
            (source, NOW, NOW),
        ).lastrowid
    )
    run = int(
        conn.execute(
            "INSERT INTO processing_runs (started_at, status, documents) VALUES (?, 'running', 4)",
            (NOW,),
        ).lastrowid
    )
    conn.commit()
    conn.close()
    return path, {"source": source, "document": document, "run": run}


def _columns(db: Database, table: str) -> list[str]:
    return [r["name"] for r in db.conn.execute(f"PRAGMA table_info({table})")]


def _index_sql(db: Database, name: str) -> str:
    row = db.conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
    return row[0] if row else ""


# ── версия ─────────────────────────────────────────────────────────────────


def test_opening_a_v6_database_migrates_it_past_v7(v6_database):
    path, _ = v6_database
    db = Database(path)
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
        assert CURRENT_VERSION >= 7
    finally:
        db.close()


# ── новые колонки ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("table", "added"), [("documents", DOCUMENT_COLUMNS), ("processing_runs", RUN_COLUMNS)]
)
def test_v7_adds_its_columns(v6_database, table, added):
    path, _ = v6_database
    db = Database(path)
    try:
        assert set(added) <= set(_columns(db, table))
    finally:
        db.close()


def test_an_existing_document_starts_with_no_failure_recorded(v6_database):
    """Старые строки не становятся сбойными задним числом."""
    path, ids = v6_database
    db = Database(path)
    try:
        row = db.conn.execute(
            "SELECT * FROM documents WHERE id=?", (ids["document"],)
        ).fetchone()
        assert (row["attempts"], row["last_error"], row["last_attempt_at"]) == (0, "", None)
        assert row["title"] == "Документ до v7"
        assert db.documents.count_failed() == 0
    finally:
        db.close()


def test_an_open_run_keeps_its_counters_and_starts_without_a_heartbeat(v6_database):
    path, ids = v6_database
    db = Database(path)
    try:
        run = db.processing_runs.get(ids["run"])
        assert (run.status, run.documents) == ("running", 4)
        assert (run.processed, run.heartbeat_at) == (0, None)
    finally:
        db.close()


def test_the_failed_documents_index_is_partial(v6_database):
    """Полный индекс по `last_error` был бы индексом пустых строк на всю таблицу."""
    path, _ = v6_database
    db = Database(path)
    try:
        sql = _index_sql(db, "idx_documents_failed")
        assert "documents(last_error, published_at DESC)" in sql
        assert "WHERE last_error <> ''" in sql
    finally:
        db.close()


def test_the_repositories_work_right_after_the_migration(v6_database, frozen_clock):
    path, ids = v6_database
    db = Database(path)
    try:
        with db.transaction():
            db.documents.mark_failed(ids["document"], "RuntimeError: боль")
        db.processing_runs.progress(ids["run"], {"documents": 4, "processed": 2})

        assert db.documents.count_failed() == 1
        run = db.processing_runs.get(ids["run"])
        assert (run.processed, run.heartbeat_at) == (2, NOW)
    finally:
        db.close()


# ── форма и идемпотентность ────────────────────────────────────────────────


def test_a_fresh_database_lands_on_the_same_shape_as_a_migrated_one(tmp_path, v6_database):
    path, _ = v6_database
    migrated, fresh = Database(path), Database(str(tmp_path / "fresh.db"))
    try:
        for table in ("documents", "processing_runs"):
            assert _columns(migrated, table) == _columns(fresh, table), table
        assert _index_sql(migrated, "idx_documents_failed") == _index_sql(
            fresh, "idx_documents_failed"
        )
    finally:
        migrated.close()
        fresh.close()


def test_reopening_a_migrated_database_does_not_run_the_step_again(v6_database, caplog):
    path, ids = v6_database
    with caplog.at_level(logging.INFO, logger="db"):
        first = Database(path)
        with first.transaction():
            first.documents.mark_failed(ids["document"], "HTTP 500", at=NOW)
        first.close()
        assert "schema migrated to v7" in caplog.text
        caplog.clear()

        second = Database(path)
    try:
        assert second.conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
        assert "schema migrated" not in caplog.text
        row = second.conn.execute(
            "SELECT * FROM documents WHERE id=?", (ids["document"],)
        ).fetchone()
        assert (row["attempts"], row["last_error"]) == (1, "HTTP 500")
    finally:
        second.close()
