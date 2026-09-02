"""src/storage/db.py — schema, repositories and cascades on an in-memory SQLite."""

from __future__ import annotations

import pytest

from src.models import CollectReport, FetchState, RawDocument, Source
from src.storage import Database, DuplicateSourceError
from src.storage.db import MANUAL_FETCH_URL, MANUAL_SOURCE_NAME


def _source(**overrides) -> Source:
    base = dict(
        name="Ведомости",
        url="https://www.vedomosti.ru",
        kind="rss",
        category="media",
        fetch_url="https://www.vedomosti.ru/rss/news",
    )
    return Source(**{**base, **overrides})


def _doc(source_id: int, external_id: str, **overrides) -> RawDocument:
    base = dict(
        source_id=source_id,
        external_id=external_id,
        url=f"https://example.ru/{external_id}",
        title=f"Заголовок {external_id}",
        text="Текст",
        fetched_at="2026-09-02T12:00:00+00:00",
        content_hash="h",
    )
    return RawDocument(**{**base, **overrides})


# ── connection / schema ────────────────────────────────────────────────────


def test_schema_version_and_pragmas(db):
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    tables = {
        r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"sources", "documents", "fetch_state", "seen_urls", "collect_runs"} <= tables


def test_file_database_creates_parent_dir_and_uses_wal(tmp_path):
    path = tmp_path / "nested" / "data" / "hub.db"
    database = Database(str(path))
    try:
        assert path.exists()
        assert database.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert database.conn.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        database.close()


def test_reopening_a_database_does_not_rerun_migrations(tmp_path):
    path = str(tmp_path / "hub.db")
    first = Database(path)
    first.sources.add(_source())
    first.close()
    second = Database(path)
    try:
        assert [s.name for s in second.sources.list()] == ["Ведомости"]
    finally:
        second.close()


def test_transaction_commits_on_success_and_rolls_back_on_error(db):
    source = db.sources.add(_source())
    with db.transaction():
        db.documents.insert(_doc(source.id, "a"))
    assert db.documents.count() == 1
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.documents.insert(_doc(source.id, "b"))
            raise RuntimeError("boom")
    assert db.documents.count() == 1


# ── sources ────────────────────────────────────────────────────────────────


def test_add_assigns_id_and_created_at(db):
    source = db.sources.add(_source())
    assert source.id == 1
    assert source.created_at.endswith("+00:00")
    stored = db.sources.get(1)
    assert stored == source


def test_add_rejects_duplicate_fetch_url(db):
    first = db.sources.add(_source())
    with pytest.raises(DuplicateSourceError, match="source already exists: #1") as info:
        db.sources.add(_source(name="Другое имя", url="https://other.ru"))
    assert info.value.existing.id == first.id


def test_get_by_fetch_url_and_missing_lookups(db):
    source = db.sources.add(_source())
    assert db.sources.get_by_fetch_url(source.fetch_url).id == source.id
    assert db.sources.get_by_fetch_url("https://nope.ru/rss") is None
    assert db.sources.get(999) is None


def test_list_orders_by_id_and_filters_enabled(db):
    a = db.sources.add(_source(name="A"))
    b = db.sources.add(_source(name="B", fetch_url="https://b.ru/rss", enabled=False))
    assert [s.id for s in db.sources.list()] == [a.id, b.id]
    assert [s.id for s in db.sources.list(enabled_only=True)] == [a.id]


def test_update_persists_every_editable_field(db):
    source = db.sources.add(_source())
    source.name = "Новое имя"
    source.kind = "sitemap"
    source.category = "regulator"
    source.fetch_url = "https://www.vedomosti.ru/sitemap.xml"
    source.enabled = False
    source.notes = "заметка"
    db.sources.update(source)
    stored = db.sources.get(source.id)
    assert stored == source


def test_set_enabled_reports_whether_a_row_changed(db):
    source = db.sources.add(_source())
    assert db.sources.set_enabled(source.id, False) is True
    assert db.sources.get(source.id).enabled is False
    assert db.sources.set_enabled(source.id, True) is True
    assert db.sources.set_enabled(999, False) is False


def test_remove_cascades_documents_fetch_state_and_seen_urls(db):
    source = db.sources.add(_source())
    with db.transaction():
        db.documents.insert(_doc(source.id, "a"))
        db.fetch_state.save(FetchState(source_id=source.id, etag='W/"1"'))
        db.seen_urls.add(source.id, ["https://example.ru/a"])
    assert db.sources.remove(source.id) is True
    assert db.sources.get(source.id) is None
    assert db.documents.count(source.id) == 0
    assert db.fetch_state.get(source.id).etag is None
    assert db.seen_urls.known(source.id, ["https://example.ru/a"]) == set()
    assert db.conn.execute("SELECT count(*) FROM seen_urls").fetchone()[0] == 0
    assert db.sources.remove(source.id) is False


def test_ensure_manual_is_idempotent(db):
    manual = db.sources.ensure_manual()
    again = db.sources.ensure_manual()
    assert manual.id == again.id
    assert (manual.kind, manual.category) == ("manual", "manual")
    assert manual.name == MANUAL_SOURCE_NAME
    assert manual.fetch_url == MANUAL_FETCH_URL
    assert len(db.sources.list()) == 1


# ── documents ──────────────────────────────────────────────────────────────


def test_insert_get_round_trip(db):
    source = db.sources.add(_source())
    doc = _doc(
        source.id,
        "news-123",
        summary="Анонс",
        author="Иван Петров",
        attachments=["https://example.ru/f.pdf"],
        published_at="2026-09-02T07:00:00+00:00",
        raw_html="<html></html>",
    )
    with db.transaction():
        doc_id = db.documents.insert(doc)
    stored = db.documents.get(doc_id)
    assert stored is not None
    assert stored.external_id == "news-123"
    assert stored.attachments == ["https://example.ru/f.pdf"]
    assert stored.published_at == "2026-09-02T07:00:00+00:00"
    assert stored.author == "Иван Петров"
    assert stored.raw_html == "<html></html>"
    assert stored.needs_fulltext is False
    assert db.documents.get(999) is None


def test_exists_and_find_by_url(db):
    source = db.sources.add(_source())
    with db.transaction():
        doc_id = db.documents.insert(_doc(source.id, "a"))
    assert db.documents.exists(source.id, "a") is True
    assert db.documents.exists(source.id, "b") is False
    assert db.documents.exists(source.id + 1, "a") is False
    assert db.documents.find_by_url("https://example.ru/a") == doc_id
    assert db.documents.find_by_url("https://example.ru/zzz") is None


def test_insert_rejects_duplicate_external_id_per_source(db):
    source = db.sources.add(_source())
    with db.transaction():
        db.documents.insert(_doc(source.id, "a"))
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction():
            db.documents.insert(_doc(source.id, "a"))


def test_list_is_newest_first_with_undated_last_and_carries_display_columns(db):
    source = db.sources.add(_source(name="Источник"))
    with db.transaction():
        db.documents.insert(_doc(source.id, "old", published_at="2026-09-01T10:00:00+00:00"))
        db.documents.insert(_doc(source.id, "undated", published_at=None, text="абв"))
        db.documents.insert(_doc(source.id, "new", published_at="2026-09-02T10:00:00+00:00"))
    rows = db.documents.list()
    assert [r["external_id"] for r in rows] == ["new", "old", "undated"]
    assert rows[0]["source_name"] == "Источник"
    assert rows[2]["text_len"] == 3
    assert set(rows[0].keys()) >= {"id", "url", "title", "summary", "author", "attachments",
                                   "fetched_at", "content_hash"}


def test_list_filters_by_source_limit_and_hidden(db):
    a = db.sources.add(_source(name="A"))
    b = db.sources.add(_source(name="B", fetch_url="https://b.ru/rss"))
    with db.transaction():
        for i in range(3):
            db.documents.insert(_doc(a.id, f"a{i}", published_at=f"2026-09-0{i + 1}T00:00:00+00:00"))
        hidden_id = db.documents.insert(_doc(b.id, "b0"))
    db.conn.execute("UPDATE documents SET hidden=1 WHERE id=?", (hidden_id,))
    assert [r["external_id"] for r in db.documents.list(source_id=a.id, limit=2)] == ["a2", "a1"]
    assert [r["external_id"] for r in db.documents.list(source_id=b.id)] == []
    assert [r["external_id"] for r in db.documents.list(source_id=b.id, include_hidden=True)] == [
        "b0"
    ]
    assert db.documents.count() == 4
    assert db.documents.count(a.id) == 3
    assert db.documents.count(999) == 0


# ── fetch_state ────────────────────────────────────────────────────────────


def test_fetch_state_get_returns_fresh_state_when_missing(db):
    source = db.sources.add(_source())
    state = db.fetch_state.get(source.id)
    assert state == FetchState(source_id=source.id)
    assert state.first_run is True


def test_fetch_state_save_upserts_and_round_trips_cursor(db):
    source = db.sources.add(_source())
    state = FetchState(
        source_id=source.id,
        etag='W/"abc"',
        last_modified="Tue, 02 Sep 2026 10:00:00 GMT",
        last_fetch_at="2026-09-02T12:00:00+00:00",
        last_success_at="2026-09-02T12:00:00+00:00",
        last_error=None,
        consecutive_failures=0,
        last_doc_count=4,
        cursor={"last_post_id": 1485, "название": "кириллица"},
    )
    with db.transaction():
        db.fetch_state.save(state)
    assert db.fetch_state.get(source.id) == state

    state.consecutive_failures = 2
    state.last_error = "HTTP 500"
    state.cursor = {"lastmod": "2026-09-02T06:00:00+00:00"}
    with db.transaction():
        db.fetch_state.save(state)
    stored = db.fetch_state.get(source.id)
    assert stored.consecutive_failures == 2
    assert stored.last_error == "HTTP 500"
    assert stored.cursor == {"lastmod": "2026-09-02T06:00:00+00:00"}
    assert db.conn.execute("SELECT count(*) FROM fetch_state").fetchone()[0] == 1


def test_fetch_state_reset_clears_validators_but_keeps_history(db):
    source = db.sources.add(_source())
    with db.transaction():
        db.fetch_state.save(
            FetchState(
                source_id=source.id,
                etag='W/"abc"',
                last_modified="x",
                last_success_at="2026-09-01T00:00:00+00:00",
                last_error="HTTP 500",
                consecutive_failures=3,
                last_doc_count=7,
                cursor={"last_post_id": 1},
            )
        )
        db.fetch_state.reset(source.id)
    stored = db.fetch_state.get(source.id)
    assert (stored.etag, stored.last_modified, stored.cursor) == (None, None, {})
    assert stored.last_success_at == "2026-09-01T00:00:00+00:00"
    assert stored.last_error == "HTTP 500"
    assert stored.consecutive_failures == 3
    assert stored.last_doc_count == 7


# ── seen_urls ──────────────────────────────────────────────────────────────


def test_seen_urls_known_and_add_are_per_source_and_idempotent(db):
    a = db.sources.add(_source(name="A"))
    b = db.sources.add(_source(name="B", fetch_url="https://b.ru/"))
    urls = ["https://a.ru/1", "https://a.ru/2", "https://a.ru/1"]
    with db.transaction():
        db.seen_urls.add(a.id, urls, seen_at="2026-09-02T12:00:00+00:00")
        db.seen_urls.add(a.id, urls)
    assert db.seen_urls.known(a.id, ["https://a.ru/1", "https://a.ru/3"]) == {"https://a.ru/1"}
    assert db.seen_urls.known(b.id, ["https://a.ru/1"]) == set()
    assert db.seen_urls.known(a.id, []) == set()
    assert db.conn.execute("SELECT count(*) FROM seen_urls").fetchone()[0] == 2


def test_seen_urls_known_handles_more_than_one_chunk(db):
    source = db.sources.add(_source())
    urls = [f"https://a.ru/{i}" for i in range(1203)]
    with db.transaction():
        db.seen_urls.add(source.id, urls[:1000])
    assert db.seen_urls.known(source.id, urls) == set(urls[:1000])


# ── runs ───────────────────────────────────────────────────────────────────


def test_runs_add_and_latest(db):
    assert db.runs.latest() is None
    first = CollectReport(
        started_at="2026-09-02T12:00:00+00:00",
        finished_at="2026-09-02T12:00:05+00:00",
        sources_ok=2,
        sources_fail=1,
        sources_not_modified=0,
        docs_new=9,
    )
    second = CollectReport(
        started_at="2026-09-02T13:00:00+00:00",
        finished_at="2026-09-02T13:00:01+00:00",
        sources_ok=0,
        sources_fail=0,
        sources_not_modified=3,
        docs_new=0,
    )
    assert db.runs.add(first) == 1
    assert db.runs.add(second) == 2
    latest = db.runs.latest()
    assert latest["id"] == 2
    assert latest["sources_not_modified"] == 3
    assert latest["docs_new"] == 0
    assert latest["started_at"] == "2026-09-02T13:00:00+00:00"
