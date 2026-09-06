"""Миграция до `PRAGMA user_version = 8`: у заметки аналитика один дом.

v3 уже переносила `items.analyst_note` в `item_notes`, но CLI `edit --note`
продолжал писать в колонку, а `EDITABLE_FIELDS` её разрешал — заметка жила в двух
местах сразу. v8 забирает остаток и гасит колонку; дальше пишет только
`item_notes`. В ответе API поле осталось ради совместимости и всегда пусто.
"""

from __future__ import annotations

import argparse
import logging

import pytest
from support import frozen_database

from src import cli
from src.models import RawDocument, Source
from src.repositories import Database
from src.repositories.database import _MIGRATIONS
from src.services.item_service import EDITABLE_FIELDS, ItemService

NOW = "2026-09-02T12:00:00+00:00"
CURRENT_VERSION = max(_MIGRATIONS)
LEFTOVER = "Проверить у юристов"
ALREADY_MOVED = "Уже перенесено в item_notes"


def _v7_item(conn, document_id: int, *, title: str, analyst_note: str) -> int:
    cluster = conn.execute(
        "INSERT INTO clusters (canonical_document_id, size, created_at) VALUES (?, 1, ?)",
        (document_id, NOW),
    ).lastrowid
    item_id = int(
        conn.execute(
            "INSERT INTO items (cluster_id, type, title, summary, priority, tags, analyst_note, "
            "visibility, manual_overrides, processed_at, published_at) "
            "VALUES (?, 'news', ?, 'Саммари', 'medium', '[]', ?, 'visible', '[]', ?, ?)",
            (int(cluster), title, analyst_note, NOW, NOW),
        ).lastrowid
    )
    conn.execute(
        "INSERT INTO item_sources (item_id, document_id, is_canonical) VALUES (?, ?, 1)",
        (item_id, document_id),
    )
    return item_id


@pytest.fixture
def v7_database(tmp_path) -> tuple[str, dict[str, int]]:
    """База v7: заметка только в колонке, заметка в обоих местах и карточка без заметки."""
    path = str(tmp_path / "hub.db")
    conn = frozen_database(path, 7)
    source = int(
        conn.execute(
            "INSERT INTO sources (name, url, kind, category, fetch_url, status, normalized_url, "
            "poll_interval, created_at, notes) VALUES ('Ведомости', 'https://vedomosti.ru/', "
            "'rss', 'media', 'https://vedomosti.ru/rss', 'active', 'vedomosti.ru/rss', '6h', ?, '')",
            (NOW,),
        ).lastrowid
    )
    ids: dict[str, int] = {}
    for index, (key, note) in enumerate(
        (("leftover", LEFTOVER), ("both", ALREADY_MOVED), ("plain", "")), start=1
    ):
        document = int(
            conn.execute(
                "INSERT INTO documents (source_id, external_id, url, title, fetched_at, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (source, f"d{index}", f"https://vedomosti.ru/{index}", f"Док {index}", NOW, NOW),
            ).lastrowid
        )
        ids[key] = _v7_item(conn, document, title=f"Карточка {key}", analyst_note=note)
    conn.execute(
        "INSERT INTO item_notes (item_id, body, author, created_at) VALUES (?, ?, '', ?)",
        (ids["both"], ALREADY_MOVED, NOW),
    )
    conn.commit()
    conn.close()
    return path, ids


def _stored_card(db: Database, item_factory) -> int:
    """Документ и карточка поверх него — то же, что видит CLI на живой установке."""
    source = db.sources.get_by_fetch_url("https://a.ru/rss") or db.sources.add(
        Source(name="Лента", url="https://a.ru/", kind="rss", fetch_url="https://a.ru/rss")
    )
    with db.transaction():
        document_id = db.documents.insert(
            RawDocument(
                source_id=source.id,
                external_id="a",
                url="https://a.ru/a",
                title="Материал",
                fetched_at=NOW,
            )
        )
    return item_factory(db, document_id, title="Материал", summary="Саммари")


@pytest.fixture
def card(db, item_factory) -> int:
    return _stored_card(db, item_factory)


@pytest.fixture
def file_card(file_db, item_factory) -> int:
    return _stored_card(file_db, item_factory)


def _notes(db: Database, item_id: int) -> list[str]:
    return [n.body for n in db.notes.list(item_id)]


def _column(db: Database, item_id: int) -> str:
    return db.conn.execute(
        "SELECT analyst_note FROM items WHERE id=?", (item_id,)
    ).fetchone()[0]


# ── версия ─────────────────────────────────────────────────────────────────


def test_opening_a_v7_database_migrates_it_to_v8(v7_database):
    path, _ = v7_database
    db = Database(path)
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
        assert CURRENT_VERSION >= 8
    finally:
        db.close()


# ── переезд заметок ────────────────────────────────────────────────────────


def test_the_leftover_note_moves_into_item_notes(v7_database):
    path, ids = v7_database
    db = Database(path)
    try:
        note = db.notes.list(ids["leftover"])[0]
        assert note.body == LEFTOVER
        assert (note.author, note.created_at) == ("", NOW)  # created_at карточки
    finally:
        db.close()


def test_the_column_is_cleared_for_every_card(v7_database):
    path, ids = v7_database
    db = Database(path)
    try:
        assert [_column(db, item_id) for item_id in ids.values()] == ["", "", ""]
    finally:
        db.close()


def test_a_note_that_already_lives_in_item_notes_is_not_duplicated(v7_database):
    path, ids = v7_database
    db = Database(path)
    try:
        assert _notes(db, ids["both"]) == [ALREADY_MOVED]
    finally:
        db.close()


def test_a_card_without_a_note_gets_none(v7_database):
    path, ids = v7_database
    db = Database(path)
    try:
        assert _notes(db, ids["plain"]) == []
    finally:
        db.close()


def test_the_card_itself_survives_the_step(v7_database):
    path, ids = v7_database
    db = Database(path)
    try:
        item = db.items.get(ids["leftover"])
        assert (item.title, item.analyst_note) == ("Карточка leftover", "")
        assert db.items.count() == 3
    finally:
        db.close()


def test_reopening_the_database_does_not_move_the_note_twice(v7_database, caplog):
    path, ids = v7_database
    with caplog.at_level(logging.INFO, logger="db"):
        Database(path).close()
        assert "schema migrated to v8" in caplog.text
        caplog.clear()

        second = Database(path)
    try:
        assert _notes(second, ids["leftover"]) == [LEFTOVER]
        assert _notes(second, ids["both"]) == [ALREADY_MOVED]
        assert "schema migrated" not in caplog.text
    finally:
        second.close()


# ── дальше в колонку никто не пишет ────────────────────────────────────────


def test_the_analyst_note_is_no_longer_an_editable_field():
    assert "analyst_note" not in EDITABLE_FIELDS


def test_edit_item_ignores_an_analyst_note_and_writes_nothing(config, db, card):
    item = ItemService(config, db).edit_item(card, {"analyst_note": "мимо колонки"})

    assert item.analyst_note == ""
    assert _column(db, card) == ""
    assert db.items.revisions(card) == []
    assert _notes(db, card) == []


def test_editing_a_real_field_still_leaves_the_note_column_empty(config, db, card):
    ItemService(config, db).edit_item(card, {"title": "Новый заголовок",
                                             "analyst_note": "мимо колонки"})

    assert db.items.get(card).title == "Новый заголовок"
    assert _column(db, card) == ""
    assert [r.field for r in db.items.revisions(card)] == ["title"]


# ── CLI: устаревший флаг ведёт в item_notes ────────────────────────────────


def test_the_deprecated_edit_note_flag_creates_a_note(config, hub_paths, file_db, file_card,
                                                      caplog):
    args = cli.build_parser().parse_args(["edit", str(file_card), "--note", LEFTOVER])

    with caplog.at_level(logging.WARNING, logger="cli"):
        assert cli._cmd_item_edit(args, config, hub_paths) == 0

    assert _notes(file_db, file_card) == [LEFTOVER]
    assert _column(file_db, file_card) == ""
    assert "`edit --note` устарел" in caplog.text


def test_the_deprecated_flag_and_the_note_command_land_in_the_same_place(
    config, hub_paths, file_db, file_card
):
    cli._cmd_item_edit(
        cli.build_parser().parse_args(["edit", str(file_card), "--note", "через edit"]),
        config,
        hub_paths,
    )
    cli._cmd_item_note(
        cli.build_parser().parse_args(["note", str(file_card), "--text", "через note"]),
        config,
        hub_paths,
    )

    assert _notes(file_db, file_card) == ["через edit", "через note"]


def test_the_card_view_prints_the_notes_from_item_notes(config, hub_paths, file_db, file_card,
                                                        capsys):
    cli._cmd_item_note(
        cli.build_parser().parse_args(["note", str(file_card), "--text", LEFTOVER]),
        config,
        hub_paths,
    )
    capsys.readouterr()

    cli._cmd_item(argparse.Namespace(id=file_card), config, hub_paths)

    out = capsys.readouterr().out
    assert "заметки аналитика:" in out
    assert LEFTOVER in out
