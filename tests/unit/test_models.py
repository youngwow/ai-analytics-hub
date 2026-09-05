"""src/models.py — dataclass (de)serialisation and derived properties."""

from __future__ import annotations

import json

import pytest

from src.models import (
    POLL_INTERVALS,
    SOURCE_STATUSES,
    VISIBILITIES,
    CollectReport,
    FetchResult,
    FetchState,
    Item,
    ItemNote,
    ItemRevision,
    ItemTag,
    RawDocument,
    Source,
    SourceRun,
)
from src.utils import sha256_text


def test_raw_document_hash_uses_title_and_text():
    doc = RawDocument(source_id=1, external_id="x", url="https://a.ru/x", title="T", text="body")
    assert doc.compute_hash() == sha256_text("T", "body")
    assert doc.content_hash == sha256_text("T", "body")


def test_raw_document_hash_falls_back_to_summary_without_text():
    doc = RawDocument(source_id=1, external_id="x", url="https://a.ru/x", title="T", summary="S")
    assert doc.compute_hash() == sha256_text("T", "S")


def test_raw_document_row_round_trip_keeps_attachments_and_dates():
    doc = RawDocument(
        source_id=2,
        external_id="news-123",
        url="https://a.ru/news/123",
        title="Заголовок",
        summary="Анонс",
        text="Текст",
        author="Иван Петров",
        attachments=["https://a.ru/f.pdf"],
        published_at="2026-09-02T07:00:00+00:00",
        fetched_at="2026-09-02T12:00:00+00:00",
        content_hash="abc",
    )
    row = doc.to_row()
    assert row["attachments"] == '["https://a.ru/f.pdf"]'
    assert json.loads(row["attachments"]) == ["https://a.ru/f.pdf"]
    restored = RawDocument.from_row({**row, "raw_html": None})
    assert restored == RawDocument(**{**doc.__dict__, "needs_fulltext": False})


def test_raw_document_from_row_tolerates_nulls_and_broken_attachments():
    row = {
        "source_id": 1,
        "external_id": "x",
        "url": "https://a.ru/x",
        "title": None,
        "summary": None,
        "text": None,
        "raw_html": None,
        "author": None,
        "attachments": "{not json",
        "published_at": None,
        "fetched_at": None,
        "content_hash": None,
    }
    doc = RawDocument.from_row(row)
    assert (doc.title, doc.summary, doc.text, doc.author) == ("", "", "", "")
    assert doc.attachments == []
    assert doc.fetched_at == ""


def test_fetch_state_first_run_is_true_until_a_success():
    state = FetchState(source_id=1)
    assert state.first_run is True
    state.last_success_at = "2026-09-02T12:00:00+00:00"
    assert state.first_run is False


def test_fetch_state_from_row_parses_cursor_json():
    row = {
        "source_id": 5,
        "etag": 'W/"1"',
        "last_modified": "Tue, 02 Sep 2026 10:00:00 GMT",
        "last_fetch_at": None,
        "last_success_at": None,
        "last_error": None,
        "consecutive_failures": None,
        "last_doc_count": None,
        "cursor": '{"last_post_id": 1485}',
    }
    state = FetchState.from_row(row)
    assert state.cursor == {"last_post_id": 1485}
    assert state.consecutive_failures == 0
    assert state.last_doc_count == 0


def test_fetch_state_from_row_ignores_bad_or_non_dict_cursor():
    base = {k: None for k in ("etag", "last_modified", "last_fetch_at", "last_success_at",
                              "last_error", "consecutive_failures", "last_doc_count")}
    assert FetchState.from_row({**base, "source_id": 1, "cursor": "{oops"}).cursor == {}
    assert FetchState.from_row({**base, "source_id": 1, "cursor": "[1, 2]"}).cursor == {}
    assert FetchState.from_row({**base, "source_id": 1, "cursor": None}).cursor == {}


def test_fetch_result_ok_means_no_error():
    assert FetchResult().ok is True
    assert FetchResult(error="HTTP 500").ok is False


def test_collect_report_summary_line():
    report = CollectReport(sources_ok=3, sources_fail=1, sources_not_modified=2, docs_new=7)
    assert report.summary_line() == "7 new documents; sources ok=3 not_modified=2 failed=1"


def _source_row(**overrides) -> dict:
    base = {
        "id": 9,
        "name": "Ведомости",
        "url": "https://www.vedomosti.ru",
        "kind": "rss",
        "category": "media",
        "fetch_url": "https://www.vedomosti.ru/rss/news",
        "status": "active",
        "normalized_url": "vedomosti.ru/rss/news",
        "poll_interval": "6h",
        "next_run_at": "2026-09-02T13:00:00+00:00",
        "category_hint": None,
        "created_at": "2026-09-02T12:00:00+00:00",
        "notes": None,
        "deleted_at": None,
        "created_by": None,
    }
    return {**base, **overrides}


def test_source_from_row_reads_the_schedule_and_normalises_null_text():
    source = Source.from_row(_source_row())
    assert source.id == 9
    assert source.status == "active"
    assert source.poll_interval == "6h"
    assert source.next_run_at == "2026-09-02T13:00:00+00:00"
    assert source.normalized_url == "vedomosti.ru/rss/news"
    assert (source.notes, source.created_by) == ("", "")


@pytest.mark.parametrize(
    ("status", "active"),
    [("active", True), ("paused", False), ("error", False), ("deleted", False)],
)
def test_source_active_is_true_only_for_the_active_status(status, active):
    assert Source.from_row(_source_row(status=status)).active is active


def test_source_from_row_defaults_a_pre_v3_row_to_active():
    """A row read before the v3 columns exist must still build a usable `Source`."""
    row = {
        "id": 9,
        "name": "Ведомости",
        "url": "https://www.vedomosti.ru",
        "kind": "rss",
        "category": "media",
        "fetch_url": "https://www.vedomosti.ru/rss/news",
        "created_at": "2026-09-02T12:00:00+00:00",
        "notes": None,
    }
    source = Source.from_row(row)
    assert (source.status, source.active) == ("active", True)
    assert (source.poll_interval, source.next_run_at) == ("1h", None)
    assert (source.normalized_url, source.deleted_at, source.created_by) == ("", None, "")


# ── stage 1.4 vocabularies and rows ────────────────────────────────────────


def test_the_state_vocabularies_are_the_ones_the_schema_checks():
    assert VISIBILITIES == ("visible", "hidden_feed", "hidden_digest", "deleted")
    assert SOURCE_STATUSES == ("active", "paused", "error", "deleted")
    assert POLL_INTERVALS == ("15m", "1h", "6h", "24h")


def test_item_to_row_serialises_manual_overrides_and_visibility():
    item = Item(
        cluster_id=1,
        title="Заголовок",
        manual_overrides=["summary", "priority"],
        visibility="hidden_digest",
        hidden_reason="нерелевантно",
        origin="manual",
        tags=["регуляторика"],
    )
    row = item.to_row()
    assert row["manual_overrides"] == '["summary", "priority"]'
    assert (row["visibility"], row["hidden_reason"]) == ("hidden_digest", "нерелевантно")
    assert row["origin"] == "manual"
    assert row["tags"] == '["регуляторика"]'


def test_item_from_row_tolerates_broken_manual_overrides_json():
    row = {k: v for k, v in Item(cluster_id=1).to_row().items()}
    row.update({"id": 3, "manual_overrides": "{not json", "tags": None})
    item = Item.from_row(row)
    assert item.manual_overrides == []
    assert item.tags == []
    assert item.visibility == "visible"


def test_item_revision_from_row_defaults_source_of_change_to_human():
    row = {
        "item_id": 1,
        "field": "summary",
        "old_value": "было",
        "new_value": "стало",
        "actor": None,
        "created_at": "2026-09-02T12:00:00+00:00",
        "id": 5,
    }
    revision = ItemRevision.from_row(row)
    assert (revision.actor, revision.source_of_change, revision.edit_reason) == (
        "user",
        "human",
        "",
    )


def test_item_revision_from_row_reads_the_model_origin_and_reason():
    row = {
        "item_id": 1,
        "field": "summary",
        "old_value": None,
        "new_value": "версия модели",
        "actor": "model",
        "source_of_change": "llm",
        "edit_reason": "",
        "created_at": "2026-09-02T12:00:00+00:00",
        "id": 5,
    }
    revision = ItemRevision.from_row(row)
    assert (revision.actor, revision.source_of_change) == ("model", "llm")


def test_source_run_from_row_normalises_null_counters_and_error_text():
    row = {
        "id": 1,
        "source_id": 7,
        "started_at": "2026-09-02T12:00:00+00:00",
        "finished_at": None,
        "http_status": None,
        "items_found": None,
        "items_new": None,
        "error_code": None,
        "error_message": None,
    }
    run = SourceRun.from_row(row)
    assert (run.items_found, run.items_new) == (0, 0)
    assert (run.error_code, run.error_message) == ("", "")
    assert run.finished_at is None


def test_item_note_and_tag_from_row():
    note = ItemNote.from_row(
        {"id": 2, "item_id": 7, "body": "Взяли в дайджест", "author": None, "created_at": None}
    )
    assert (note.body, note.author, note.created_at) == ("Взяли в дайджест", "", "")
    tag = ItemTag.from_row({"item_id": 7, "tag": "регуляторика", "is_manual": 1})
    assert (tag.tag, tag.is_manual) == ("регуляторика", True)
