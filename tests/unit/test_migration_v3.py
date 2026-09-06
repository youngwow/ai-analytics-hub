"""Migration to `PRAGMA user_version = 3` on a database already full of v2 data.

The renames of stage 1.4 (`edited_fields` → `manual_overrides`, `is_hidden` →
`visibility`, `sources.enabled` → `sources.status`) and the new `item_notes` /
`item_tags` tables must carry the existing rows across, not just create empty
columns — an installation with 53 sources and 636 documents is the only thing
this migration will ever run against.
"""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from src.repositories import Database
from src.repositories.database import _MIGRATIONS, _SCHEMA_V1, _SCHEMA_V2

NOW = "2026-09-02T12:00:00+00:00"
# This file is about the v3 *data* moves; later steps ride along, so the version
# assertions say «the chain ran to the end» instead of pinning a number twice
# (`test_migration_v4.py` owns the literal for the newest step).
CURRENT_VERSION = max(_MIGRATIONS)


def _v2_connection(path: str) -> sqlite3.Connection:
    """A database frozen at the shape stage 1.2 left behind."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA_V1)
    conn.executescript(_SCHEMA_V2)
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    return conn


def _v2_source(conn, name: str, url: str, fetch_url: str, enabled: int = 1) -> int:
    cur = conn.execute(
        "INSERT INTO sources (name, url, kind, category, fetch_url, enabled, created_at, notes) "
        "VALUES (?, ?, 'rss', 'media', ?, ?, ?, '')",
        (name, url, fetch_url, enabled, NOW),
    )
    return int(cur.lastrowid)


def _v2_document(conn, source_id: int, external_id: str) -> int:
    cur = conn.execute(
        "INSERT INTO documents (source_id, external_id, url, title, text, fetched_at, created_at) "
        "VALUES (?, ?, ?, ?, 'Текст', ?, ?)",
        (source_id, external_id, f"https://example.ru/{external_id}", f"Док {external_id}",
         NOW, NOW),
    )
    return int(cur.lastrowid)


def _v2_item(conn, document_id: int, **overrides) -> int:
    """A cluster plus the v2-shaped card over it."""
    fields = {
        "type": "news",
        "title": "Заголовок",
        "summary": "Саммари",
        "priority": "medium",
        "tags": "[]",
        "analyst_note": "",
        "is_hidden": 0,
        "edited_fields": "[]",
        **overrides,
    }
    cluster = conn.execute(
        "INSERT INTO clusters (canonical_document_id, size, created_at) VALUES (?, 1, ?)",
        (document_id, NOW),
    )
    cur = conn.execute(
        "INSERT INTO items (cluster_id, type, title, summary, priority, tags, analyst_note, "
        "is_hidden, edited_fields, processed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(cluster.lastrowid),
            fields["type"],
            fields["title"],
            fields["summary"],
            fields["priority"],
            fields["tags"],
            fields["analyst_note"],
            fields["is_hidden"],
            fields["edited_fields"],
            NOW,
        ),
    )
    item_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO item_sources (item_id, document_id, is_canonical) VALUES (?, ?, 1)",
        (item_id, document_id),
    )
    return item_id


@pytest.fixture
def v2_database(tmp_path):
    """A populated v2 database on disk, plus the ids the tests assert on."""
    path = str(tmp_path / "hub.db")
    conn = _v2_connection(path)
    ids: dict[str, int] = {}
    ids["live_source"] = _v2_source(conn, "Ведомости", "https://www.vedomosti.ru",
                                    "https://www.vedomosti.ru/rss/news")
    ids["off_source"] = _v2_source(conn, "Старый сайт", "https://old.ru", "https://old.ru/feed",
                                   enabled=0)
    document = _v2_document(conn, ids["live_source"], "d1")
    hidden_document = _v2_document(conn, ids["live_source"], "d2")
    ids["visible_item"] = _v2_item(
        conn,
        document,
        title="Минцифры расширило реестр",
        tags=json.dumps(["регуляторика", "тренды"], ensure_ascii=False),
        analyst_note="Проверить у юристов",
        edited_fields=json.dumps(["summary"]),
    )
    ids["hidden_item"] = _v2_item(conn, hidden_document, title="Скрытая карточка", is_hidden=1)
    conn.execute(
        "INSERT INTO item_revisions (item_id, field, old_value, new_value, actor, created_at) "
        "VALUES (?, 'summary', 'было', 'стало', 'user', ?)",
        (ids["visible_item"], NOW),
    )
    conn.commit()
    conn.close()
    return path, ids


def _open(path: str) -> Database:
    return Database(path)


# ── the version bump itself ────────────────────────────────────────────────


def test_opening_a_v2_database_runs_the_migration_chain_to_the_end(v2_database):
    path, _ = v2_database
    db = _open(path)
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
    finally:
        db.close()


def test_v3_adds_the_source_runs_notes_and_tags_tables(v2_database):
    path, _ = v2_database
    db = _open(path)
    try:
        tables = {
            r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"source_runs", "item_notes", "item_tags"} <= tables
    finally:
        db.close()


@pytest.mark.parametrize(
    ("table", "gone", "added"),
    [
        ("items", {"is_hidden", "edited_fields"}, {"manual_overrides", "visibility",
                                                   "hidden_reason", "origin"}),
        ("sources", {"enabled"}, {"status", "normalized_url", "poll_interval", "next_run_at",
                                  "category_hint", "deleted_at", "created_by"}),
        ("item_revisions", set(), {"source_of_change", "edit_reason"}),
    ],
    ids=["items", "sources", "item_revisions"],
)
def test_v3_replaces_the_old_columns(v2_database, table, gone, added):
    path, _ = v2_database
    db = _open(path)
    try:
        columns = {r["name"] for r in db.conn.execute(f"PRAGMA table_info({table})")}
        assert columns & gone == set()
        assert added <= columns
    finally:
        db.close()


# ── the data actually moves ────────────────────────────────────────────────


def test_hidden_cards_become_hidden_feed_and_the_rest_stay_visible(v2_database):
    path, ids = v2_database
    db = _open(path)
    try:
        assert db.items.get(ids["hidden_item"]).visibility == "hidden_feed"
        assert db.items.get(ids["visible_item"]).visibility == "visible"
        assert [r["id"] for r in db.items.list()] == [ids["visible_item"]]
    finally:
        db.close()


def test_edited_fields_become_manual_overrides(v2_database):
    path, ids = v2_database
    db = _open(path)
    try:
        assert db.items.get(ids["visible_item"]).manual_overrides == ["summary"]
        assert db.items.get(ids["hidden_item"]).manual_overrides == []
    finally:
        db.close()


def test_every_migrated_card_starts_as_collected_with_no_hidden_reason(v2_database):
    path, ids = v2_database
    db = _open(path)
    try:
        item = db.items.get(ids["hidden_item"])
        assert (item.origin, item.hidden_reason) == ("collected", "")
    finally:
        db.close()


@pytest.mark.parametrize(
    ("key", "status"), [("live_source", "active"), ("off_source", "paused")]
)
def test_enabled_becomes_the_status_vocabulary(v2_database, key, status):
    path, ids = v2_database
    db = _open(path)
    try:
        assert db.sources.get(ids[key]).status == status
    finally:
        db.close()


def test_the_analyst_note_moves_into_item_notes(v2_database):
    path, ids = v2_database
    db = _open(path)
    try:
        notes = db.notes.list(ids["visible_item"])
        assert [n.body for n in notes] == ["Проверить у юристов"]
        assert notes[0].author == ""
        assert notes[0].created_at == NOW  # processed_at of the card it came from
        assert db.notes.list(ids["hidden_item"]) == []
    finally:
        db.close()


def test_the_json_tags_become_item_tags_marked_as_the_models(v2_database):
    path, ids = v2_database
    db = _open(path)
    try:
        tags = db.tags.list(ids["visible_item"])
        assert [t.tag for t in tags] == ["регуляторика", "тренды"]
        assert [t.is_manual for t in tags] == [False, False]
        assert db.tags.names(ids["hidden_item"]) == []
    finally:
        db.close()


def test_existing_revisions_are_attributed_to_the_human(v2_database):
    path, ids = v2_database
    db = _open(path)
    try:
        revision = db.items.revisions(ids["visible_item"])[0]
        assert (revision.source_of_change, revision.edit_reason) == ("human", "")
        assert (revision.old_value, revision.new_value) == ("было", "стало")
        assert db.items.last_model_value(ids["visible_item"], "summary") is None
    finally:
        db.close()


def test_documents_and_their_links_are_untouched(v2_database):
    path, ids = v2_database
    db = _open(path)
    try:
        assert db.documents.count() == 2
        assert db.documents.count(ids["live_source"]) == 2
        assert [r["title"] for r in db.items.sources(ids["visible_item"])] == ["Док d1"]
    finally:
        db.close()


# ── the backfill: normalized_url and the schedule ──────────────────────────


def test_backfill_normalises_the_existing_addresses(v2_database):
    path, ids = v2_database
    db = _open(path)
    try:
        assert db.sources.get(ids["live_source"]).normalized_url == "vedomosti.ru/rss/news"
        assert db.sources.get(ids["off_source"]).normalized_url == "old.ru/feed"
    finally:
        db.close()


def test_backfill_schedules_every_source_so_nothing_idles_after_the_update(v2_database, frozen_clock):
    path, ids = v2_database
    db = _open(path)
    try:
        assert db.sources.get(ids["live_source"]).next_run_at == "2026-09-02T12:00:00+00:00"
        assert db.sources.get(ids["off_source"]).poll_interval == "1h"
        # paused sources keep a schedule but the query never returns them
        assert [s.id for s in db.sources.due("2026-09-02T12:00:00+00:00")] == [ids["live_source"]]
    finally:
        db.close()


def test_two_spellings_of_one_address_do_not_break_the_migration(tmp_path, caplog):
    path = str(tmp_path / "hub.db")
    conn = _v2_connection(path)
    first = _v2_source(conn, "РФРИТ", "https://t.me/rfrit", "https://t.me/s/rfrit")
    second = _v2_source(conn, "РФРИТ (копия)", "https://t.me/rfrit/", "https://t.me/rfrit/")
    conn.commit()
    conn.close()

    with caplog.at_level(logging.WARNING, logger="db"):
        db = _open(path)
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
        assert db.sources.get(first).normalized_url == "t.me/rfrit"
        # the later row keeps its data but stays out of the unique index
        assert db.sources.get(second).normalized_url == ""
        assert db.sources.get_by_normalized("t.me/rfrit").id == first
    finally:
        db.close()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert f"источник #{second}" in warnings[0]
    assert f"повторяет #{first}" in warnings[0]
    assert "t.me/rfrit" in warnings[0]


def test_three_pravo_gov_feeds_keep_their_query_and_stay_distinct(tmp_path):
    """They differ only by `block=` — dropping the query would merge three sources."""
    path = str(tmp_path / "hub.db")
    conn = _v2_connection(path)
    ids = [
        _v2_source(conn, f"pravo {block}", f"http://publication.pravo.gov.ru/rss?block={block}",
                   f"http://publication.pravo.gov.ru/rss?block={block}")
        for block in ("president", "government", "ministry")
    ]
    conn.commit()
    conn.close()

    db = _open(path)
    try:
        keys = [db.sources.get(i).normalized_url for i in ids]
        assert keys == [
            "publication.pravo.gov.ru/rss?block=president",
            "publication.pravo.gov.ru/rss?block=government",
            "publication.pravo.gov.ru/rss?block=ministry",
        ]
    finally:
        db.close()


# ── idempotence ────────────────────────────────────────────────────────────


def test_reopening_a_migrated_database_changes_nothing(v2_database):
    path, ids = v2_database
    first = _open(path)
    first_seen = {
        "notes": len(first.notes.list(ids["visible_item"])),
        "tags": first.tags.names(ids["visible_item"]),
        "next_run_at": first.sources.get(ids["live_source"]).next_run_at,
    }
    first.close()

    second = _open(path)
    try:
        assert second.conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
        assert len(second.notes.list(ids["visible_item"])) == first_seen["notes"] == 1
        assert second.tags.names(ids["visible_item"]) == first_seen["tags"]
        # the backfill must not re-stamp a schedule that already exists
        assert second.sources.get(ids["live_source"]).next_run_at == first_seen["next_run_at"]
        assert second.items.get(ids["hidden_item"]).visibility == "hidden_feed"
    finally:
        second.close()


def test_a_fresh_database_has_the_same_column_shape_as_a_migrated_one(tmp_path, v2_database):
    migrated_path, _ = v2_database
    migrated, fresh = _open(migrated_path), _open(str(tmp_path / "fresh.db"))
    try:
        for table in ("sources", "items", "item_revisions", "source_runs", "item_notes",
                      "item_tags"):
            migrated_columns = [r["name"] for r in migrated.conn.execute(
                f"PRAGMA table_info({table})")]
            fresh_columns = [r["name"] for r in fresh.conn.execute(f"PRAGMA table_info({table})")]
            assert migrated_columns == fresh_columns, table
    finally:
        migrated.close()
        fresh.close()
