"""src/storage/db.py — schema, repositories and cascades on an in-memory SQLite."""

from __future__ import annotations

import sqlite3

import pytest

from src.models import (
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
from src.storage import Database, DuplicateSourceError
from src.storage.db import MANUAL_FETCH_URL, MANUAL_SOURCE_NAME

NOW = "2026-09-02T12:00:00+00:00"


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


def _card(db: Database, doc_id: int, **overrides) -> int:
    """A cluster plus its card over `doc_id`, linked as the canonical source."""
    fields = {"processed_at": NOW, **overrides}
    with db.transaction():
        cluster_id = db.clusters.add(Cluster(canonical_document_id=doc_id, created_at=NOW))
        item_id = db.items.add(Item(cluster_id=cluster_id, **fields))
        db.items.link_sources(item_id, [doc_id], doc_id)
    return item_id


def _seeded_documents(db: Database, count: int = 1, **overrides) -> list[int]:
    """`count` documents from one source, ids in insertion order."""
    source = db.sources.add(_source())
    ids = []
    with db.transaction():
        for i in range(count):
            ids.append(db.documents.insert(_doc(source.id, f"d{i}", **overrides)))
    return ids


# ── connection / schema ────────────────────────────────────────────────────


def test_schema_version_and_pragmas(db):
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 2
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
        assert database.conn.execute("PRAGMA user_version").fetchone()[0] == 2
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


# ── schema v2: the processing tables (task 1.2) ────────────────────────────


def test_migration_to_v2_creates_every_processing_table(db):
    tables = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "clusters",
        "items",
        "entities",
        "item_sources",
        "npa_events",
        "item_revisions",
        "company_profiles",
        "prompt_versions",
        "llm_calls",
        "items_fts",
    } <= tables


def test_migration_to_v2_creates_the_fts_sync_triggers(db):
    triggers = {
        r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }
    assert {"items_fts_ai", "items_fts_ad", "items_fts_au"} <= triggers


def test_migration_to_v2_adds_the_derived_columns_to_documents(db):
    columns = {r["name"] for r in db.conn.execute("PRAGMA table_info(documents)")}
    assert {"simhash", "embedding", "norm_text"} <= columns


def test_reopening_a_database_keeps_v2_data_and_does_not_remigrate(tmp_path):
    path = str(tmp_path / "hub.db")
    first = Database(path)
    doc_id = _seeded_documents(first)[0]
    item_id = _card(first, doc_id, title="Минцифры расширило реестр", priority="high")
    first.close()

    second = Database(path)
    try:
        assert second.conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert second.items.count() == 1
        assert second.items.get(item_id).title == "Минцифры расширило реестр"
        assert [r["id"] for r in second.items.list(query="реестр")] == [item_id]
    finally:
        second.close()


# ── items: identity and the npa key ────────────────────────────────────────


def test_two_live_cards_cannot_share_an_npa_key(db):
    first, second = _seeded_documents(db, 2)
    _card(db, first, npa_key="112233-8", type="npa")
    with pytest.raises(sqlite3.IntegrityError):
        _card(db, second, npa_key="112233-8", type="npa")


def test_archiving_a_card_frees_its_npa_key(db):
    first, second = _seeded_documents(db, 2)
    item_id = _card(db, first, npa_key="112233-8", type="npa")
    item = db.items.get(item_id)
    item.is_archived = True
    with db.transaction():
        db.items.update(item)
    new_id = _card(db, second, npa_key="112233-8", type="npa")
    assert db.items.by_npa_key("112233-8").id == new_id


def test_by_npa_key_ignores_archived_cards_and_unknown_keys(db):
    doc_id = _seeded_documents(db)[0]
    item_id = _card(db, doc_id, npa_key="112233-8", type="npa")
    assert db.items.by_npa_key("112233-8").id == item_id
    assert db.items.by_npa_key("999-9") is None
    item = db.items.get(item_id)
    item.is_archived = True
    with db.transaction():
        db.items.update(item)
    assert db.items.by_npa_key("112233-8") is None


def test_a_cluster_carries_at_most_one_card(db):
    doc_id = _seeded_documents(db)[0]
    with db.transaction():
        cluster_id = db.clusters.add(Cluster(canonical_document_id=doc_id, created_at=NOW))
        db.items.add(Item(cluster_id=cluster_id, processed_at=NOW))
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction():
            db.items.add(Item(cluster_id=cluster_id, processed_at=NOW))


# ── documents: unprocessed / derived / candidates ──────────────────────────


def test_unprocessed_skips_documents_that_already_have_a_card(db):
    carded, fresh = _seeded_documents(db, 2)
    _card(db, carded)
    assert [r["id"] for r in db.documents.unprocessed()] == [fresh]


def test_unprocessed_returns_carded_documents_with_force(db):
    carded, fresh = _seeded_documents(db, 2)
    _card(db, carded)
    assert {r["id"] for r in db.documents.unprocessed(force=True)} == {carded, fresh}


def test_unprocessed_filters_by_source_since_limit_and_hidden(db):
    a = db.sources.add(_source(name="A"))
    b = db.sources.add(_source(name="B", fetch_url="https://b.ru/rss"))
    with db.transaction():
        old = db.documents.insert(_doc(a.id, "old", published_at="2026-08-01T00:00:00+00:00"))
        new = db.documents.insert(_doc(a.id, "new", published_at="2026-09-02T00:00:00+00:00"))
        undated = db.documents.insert(_doc(a.id, "undated", published_at=None))
        other = db.documents.insert(_doc(b.id, "other", published_at="2026-09-02T00:00:00+00:00"))
        hidden = db.documents.insert(_doc(b.id, "hidden", published_at="2026-09-02T00:00:00+00:00"))
    db.conn.execute("UPDATE documents SET hidden=1 WHERE id=?", (hidden,))

    assert [r["id"] for r in db.documents.unprocessed(source_id=a.id)] == [new, old, undated]
    assert [r["id"] for r in db.documents.unprocessed(source_id=b.id)] == [other]
    assert {r["id"] for r in db.documents.unprocessed(since="2026-09-01T00:00:00+00:00")} == {
        new,
        undated,
        other,
    }
    assert len(db.documents.unprocessed(limit=1)) == 1


def test_set_derived_stores_simhash_embedding_and_norm_text(db):
    doc_id = _seeded_documents(db)[0]
    with db.transaction():
        db.documents.set_derived(
            doc_id, simhash="0f1e2d3c4b5a6978", embedding=b"\x00\x01", norm_text="Нормализовано"
        )
    row = db.conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    assert row["simhash"] == "0f1e2d3c4b5a6978"
    assert row["embedding"] == b"\x00\x01"
    assert row["norm_text"] == "Нормализовано"


def test_set_derived_keeps_a_stored_embedding_when_none_is_given(db):
    doc_id = _seeded_documents(db)[0]
    with db.transaction():
        db.documents.set_derived(doc_id, simhash="aaaa", embedding=b"\x01\x02", norm_text="раз")
        db.documents.set_derived(doc_id, simhash="bbbb", embedding=None, norm_text="два")
    row = db.conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    assert (row["simhash"], row["norm_text"]) == ("bbbb", "два")
    assert row["embedding"] == b"\x01\x02"


def test_clustered_candidates_returns_only_carded_documents_with_a_simhash(db):
    carded, unhashed, no_card = _seeded_documents(db, 3, published_at="2026-09-02T00:00:00+00:00")
    item_id = _card(db, carded, type="npa", npa_key="112233-8")
    _card(db, unhashed)
    with db.transaction():
        db.documents.set_derived(carded, simhash="00ff00ff00ff00ff", norm_text="текст")
        db.documents.set_derived(no_card, simhash="1111111111111111", norm_text="текст")
    rows = db.documents.clustered_candidates()
    assert [r["id"] for r in rows] == [carded]
    assert rows[0]["item_id"] == item_id
    assert (rows[0]["type"], rows[0]["npa_key"]) == ("npa", "112233-8")


def test_clustered_candidates_respects_the_window_but_keeps_undated_documents(db):
    old, undated = _seeded_documents(db, 2)
    with db.transaction():
        db.conn.execute(
            "UPDATE documents SET published_at=? WHERE id=?", ("2026-08-01T00:00:00+00:00", old)
        )
    _card(db, old)
    _card(db, undated)
    with db.transaction():
        db.documents.set_derived(old, simhash="a" * 16, norm_text="старое")
        db.documents.set_derived(undated, simhash="b" * 16, norm_text="без даты")
    kept = [r["id"] for r in db.documents.clustered_candidates(since="2026-09-01T00:00:00+00:00")]
    assert kept == [undated]


# ── items: the feed query ──────────────────────────────────────────────────


@pytest.fixture
def feed(db) -> dict[str, int]:
    """Three cards covering both types, all priorities, tags and hidden state."""
    npa, news, hidden = _seeded_documents(db, 3, published_at="2026-09-02T00:00:00+00:00")
    ids = {
        "npa": _card(
            db,
            npa,
            type="npa",
            npa_key="112233-8",
            priority="high",
            title="Минцифры внесло законопроект об аккредитации ИИ-сервисов",
            summary="Операторы ИИ обязаны пройти аккредитацию.",
            tags=["регуляторика", "господдержка/льготы"],
            published_at="2026-09-02T00:00:00+00:00",
        ),
        "news": _card(
            db,
            news,
            type="news",
            priority="low",
            title="Оператор платного ТВ запустил рекомендательный сервис",
            summary="Рекомендации строятся на истории просмотров.",
            tags=["конкуренты"],
            published_at="2026-09-01T00:00:00+00:00",
        ),
        "hidden": _card(
            db,
            hidden,
            type="news",
            priority="medium",
            title="Скрытая карточка про правила аккредитации",
            is_hidden=True,
            published_at="2026-09-03T00:00:00+00:00",
        ),
    }
    return ids


def test_items_list_hides_hidden_cards_and_sorts_newest_first(db, feed):
    assert [r["id"] for r in db.items.list()] == [feed["npa"], feed["news"]]
    assert [r["id"] for r in db.items.list(include_hidden=True)] == [
        feed["hidden"],
        feed["npa"],
        feed["news"],
    ]


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"type_": "npa"}, ["npa"]),
        ({"type_": "news"}, ["news"]),
        ({"priority": "high"}, ["npa"]),
        ({"priority": "low"}, ["news"]),
        ({"priority": "medium"}, []),
        ({"tag": "регуляторика"}, ["npa"]),
        ({"tag": "господдержка/льготы"}, ["npa"]),
        ({"tag": "конкуренты"}, ["news"]),
        ({"tag": "тренды"}, []),
        ({"since": "2026-09-02T00:00:00+00:00"}, ["npa"]),
        ({"type_": "npa", "priority": "low"}, []),
        ({"limit": 1}, ["npa"]),
    ],
    ids=[
        "type-npa", "type-news", "priority-high", "priority-low", "priority-medium",
        "tag-regulation", "tag-with-slash", "tag-competitors", "tag-absent", "since",
        "type-and-priority", "limit",
    ],
)
def test_items_list_filters(db, feed, filters, expected):
    assert [r["id"] for r in db.items.list(**filters)] == [feed[name] for name in expected]


def test_items_list_carries_the_cluster_size_as_sources_count(db, feed):
    row = next(r for r in db.items.list() if r["id"] == feed["npa"])
    assert row["sources_count"] == 1
    with db.transaction():
        db.clusters.grow(row["cluster_id"], 2)
    assert next(r for r in db.items.list() if r["id"] == feed["npa"])["sources_count"] == 3


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("аккредитации", ["npa"]),
        ("законопроект", ["npa"]),
        ("просмотров", ["news"]),  # matches the summary, not the title
        ("рекомендательный OR законопроект", ["npa", "news"]),
        ("вообщенетакогослова", []),
    ],
    ids=["title-word", "title-word-2", "summary-word", "or-query", "no-hit"],
)
def test_items_list_full_text_query_goes_through_items_fts(db, feed, query, expected):
    assert [r["id"] for r in db.items.list(query=query)] == [feed[name] for name in expected]


def test_items_fts_follows_a_card_through_update_and_delete(db, feed):
    item = db.items.get(feed["news"])
    item.title = "Спутниковый оператор обновил телегид"
    with db.transaction():
        db.items.update(item)
    assert [r["id"] for r in db.items.list(query="телегид")] == [feed["news"]]
    assert db.items.list(query="рекомендательный") == []
    with db.transaction():
        db.conn.execute("DELETE FROM items WHERE id=?", (feed["news"],))
    assert db.items.list(query="телегид") == []


def test_items_list_full_text_query_combines_with_the_other_filters(db, feed):
    assert [r["id"] for r in db.items.list(query="аккредитации", type_="npa")] == [feed["npa"]]
    assert db.items.list(query="аккредитации", type_="news") == []
    # The hidden card also mentions «аккредитации» and must stay out by default.
    assert [r["id"] for r in db.items.list(query="аккредитации", include_hidden=True)] == [
        feed["hidden"],
        feed["npa"],
    ]


# ── items: entities, sources, events, revisions ────────────────────────────


def test_add_and_read_entities_round_trip(db):
    doc_id = _seeded_documents(db)[0]
    item_id = _card(db, doc_id)
    with db.transaction():
        db.items.add_entities(
            item_id,
            [
                EntitySpan(role="who", value="Минцифры", evidence_start=0, evidence_end=8),
                EntitySpan(
                    role="act_number", value="112233-8", normalized_value="112233-8"
                ),
            ],
        )
    stored = db.items.entities(item_id)
    assert [(e.role, e.value) for e in stored] == [("who", "Минцифры"), ("act_number", "112233-8")]
    assert (stored[0].evidence_start, stored[0].evidence_end) == (0, 8)
    assert (stored[1].evidence_start, stored[1].evidence_end) == (None, None)
    with db.transaction():
        db.items.clear_entities(item_id)
    assert db.items.entities(item_id) == []


def test_entities_reject_an_unknown_role(db):
    doc_id = _seeded_documents(db)[0]
    item_id = _card(db, doc_id)
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction():
            db.items.add_entities(item_id, [EntitySpan(role="кто-то", value="x")])


def test_link_sources_reports_new_links_only_once(db):
    first, second = _seeded_documents(db, 2)
    item_id = _card(db, first)  # already links `first` as canonical
    with db.transaction():
        assert db.items.link_sources(item_id, [first, second]) == 1
        assert db.items.link_sources(item_id, [first, second]) == 0
    rows = db.items.sources(item_id)
    assert [r["id"] for r in rows] == [first, second]
    assert [bool(r["is_canonical"]) for r in rows] == [True, False]
    assert db.items.item_for_document(second) == item_id
    assert db.items.item_for_document(999) is None


def test_events_are_ordered_and_deduplicated_by_status(db):
    doc_id = _seeded_documents(db)[0]
    item_id = _card(db, doc_id, type="npa")
    with db.transaction():
        db.items.add_event(
            NpaEvent(item_id=item_id, status="внесён", occurred_at="2026-09-02T00:00:00+00:00")
        )
        db.items.add_event(
            NpaEvent(
                item_id=item_id,
                status="анонс",
                occurred_at="2026-08-01T00:00:00+00:00",
                created_by="user",
            )
        )
    assert [e.status for e in db.items.events(item_id)] == ["анонс", "внесён"]
    assert db.items.has_event(item_id, "анонс") is True
    assert db.items.has_event(item_id, "принят") is False


def test_revisions_keep_the_before_and_after(db):
    doc_id = _seeded_documents(db)[0]
    item_id = _card(db, doc_id, priority="medium")
    with db.transaction():
        db.items.add_revision(
            ItemRevision(item_id=item_id, field="priority", old_value="medium", new_value="low")
        )
    revision = db.items.revisions(item_id)[0]
    assert (revision.field, revision.old_value, revision.new_value) == ("priority", "medium", "low")
    assert revision.actor == "user"
    assert revision.created_at.endswith("+00:00")


def test_edited_share_counts_only_cards_with_edited_fields(db):
    plain, edited = _seeded_documents(db, 2, published_at="2026-09-02T00:00:00+00:00")
    _card(db, plain)
    _card(db, edited, edited_fields=["priority"], processed_at="2026-09-02T12:00:00+00:00")
    assert db.items.edited_share() == pytest.approx(0.5)
    assert db.items.edited_share(since="2026-09-02T00:00:00+00:00") == pytest.approx(0.5)
    assert db.items.edited_share(since="2026-09-03T00:00:00+00:00") == pytest.approx(0.0)


def test_edited_share_of_an_empty_database_is_zero(db):
    assert db.items.edited_share() == 0.0


# ── clusters, profiles, prompts, llm_calls ─────────────────────────────────


def test_cluster_grow_adds_the_given_count_and_ignores_zero(db):
    doc_id = _seeded_documents(db)[0]
    with db.transaction():
        cluster_id = db.clusters.add(Cluster(canonical_document_id=doc_id, created_at=NOW))
    with db.transaction():
        db.clusters.grow(cluster_id, 2)
        db.clusters.grow(cluster_id, 0)
    assert db.clusters.get(cluster_id).size == 3
    with db.transaction():
        db.clusters.grow(cluster_id, 0, divergent=True)
    cluster = db.clusters.get(cluster_id)
    assert (cluster.size, cluster.has_divergent_opinions) == (3, True)


def test_profiles_save_inserts_then_bumps_the_version(db):
    saved = db.profiles.save(CompanyProfile(name="ООО «Цифра»", payload={"industry": "ИТ"}))
    assert (saved.id, saved.version) == (1, 1)
    again = db.profiles.save(CompanyProfile(name="ООО «Цифра»", payload={"industry": "ИТ и ИИ"}))
    assert (again.id, again.version) == (1, 2)
    assert db.profiles.get(1).payload == {"industry": "ИТ и ИИ"}
    assert len(db.profiles.list()) == 1


def test_profiles_set_default_keeps_a_single_default(db):
    first = db.profiles.save(CompanyProfile(name="A"))
    second = db.profiles.save(CompanyProfile(name="B"))
    assert db.profiles.default() is None
    assert db.profiles.set_default(first.id) is True
    assert db.profiles.default().id == first.id
    assert db.profiles.set_default(second.id) is True
    assert db.profiles.default().id == second.id
    assert [p.is_default for p in db.profiles.list()] == [False, True]
    assert db.profiles.set_default(999) is False


def test_prompts_ensure_is_idempotent_per_stage_template_model_and_params(db):
    first = db.prompts.ensure("s2_s5", "шаблон", "glm-5.3:cloud", {"temperature": 0.2})
    assert db.prompts.ensure("s2_s5", "шаблон", "glm-5.3:cloud", {"temperature": 0.2}) == first
    assert db.prompts.ensure("s2_s5", "шаблон", "glm-5.3:cloud", {"temperature": 0.5}) != first
    assert db.prompts.ensure("s2_s5", "другой шаблон", "glm-5.3:cloud", {}) != first
    assert db.conn.execute("SELECT count(*) FROM prompt_versions").fetchone()[0] == 3


def test_llm_calls_stats_aggregate_and_window(db):
    with db.transaction():
        db.llm_calls.add(
            LlmCall(
                stage="s2_s5", model="m", tokens_in=100, tokens_out=20, latency_ms=200,
                created_at="2026-09-02T10:00:00+00:00",
            )
        )
        db.llm_calls.add(
            LlmCall(
                stage="s2_s5", model="m", tokens_in=300, tokens_out=40, latency_ms=400,
                status="failed", error="429", created_at="2026-09-02T14:00:00+00:00",
            )
        )
    stats = db.llm_calls.stats()
    assert stats["calls"] == 2
    assert stats["avg_latency_ms"] == pytest.approx(300)
    assert (stats["tokens_in"], stats["tokens_out"]) == (400, 60)
    assert stats["failed"] == 1
    windowed = db.llm_calls.stats(since="2026-09-02T12:00:00+00:00")
    assert (windowed["calls"], windowed["failed"]) == (1, 1)


def test_llm_calls_stats_on_an_empty_table(db):
    stats = db.llm_calls.stats()
    assert stats["calls"] == 0
    assert stats["avg_latency_ms"] == 0
    assert stats["failed"] is None  # SUM over no rows


def test_deleting_a_card_cascades_its_children(db):
    doc_id = _seeded_documents(db)[0]
    item_id = _card(db, doc_id)
    with db.transaction():
        db.items.add_entities(item_id, [EntitySpan(role="who", value="Минцифры")])
        db.items.add_event(NpaEvent(item_id=item_id, status="внесён"))
        db.items.add_revision(ItemRevision(item_id=item_id, field="priority", new_value="low"))
    with db.transaction():
        db.conn.execute("DELETE FROM items WHERE id=?", (item_id,))
    assert db.items.entities(item_id) == []
    assert db.items.events(item_id) == []
    assert db.items.revisions(item_id) == []
    assert db.items.sources(item_id) == []
