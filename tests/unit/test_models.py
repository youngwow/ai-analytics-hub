"""src/models.py — dataclass (de)serialisation and derived properties."""

from __future__ import annotations

import json

from src.common import sha256_text
from src.models import CollectReport, FetchResult, FetchState, RawDocument, Source


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


def test_source_from_row_normalises_enabled_and_notes():
    row = {
        "id": 9,
        "name": "Ведомости",
        "url": "https://www.vedomosti.ru",
        "kind": "rss",
        "category": "media",
        "fetch_url": "https://www.vedomosti.ru/rss/news",
        "enabled": 0,
        "created_at": "2026-09-02T12:00:00+00:00",
        "notes": None,
    }
    source = Source.from_row(row)
    assert source.enabled is False
    assert source.notes == ""
    assert source.id == 9
