"""Сбой обработки живёт на документе, а не в логе (план 2.1 + 4.1).

Раньше «упал в прошлом прогоне» и «ещё не брали» были неразличимы: `report.failed`
считался и терялся вместе с процессом. Теперь документ помнит попытку и ошибку,
успех её снимает, а `--only-failed` берёт только их.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from support import ARTICLE_TEXT, NEWS_TEXT, FakeLLM, news_answer

from src import cli
from src.models import RawDocument, Source
from src.repositories import Database
from src.services.processing_service import ProcessingService

NOW = "2026-09-02T12:00:00+00:00"
LATER = "2026-09-02T18:00:00+00:00"
REPRINT_TEXT = ARTICLE_TEXT.replace("внесло в правительство", "направило в правительство")
BOOM = "модель вернула мусор"


def _source(db: Database, name: str = "Лента", category: str = "media") -> Source:
    fetch_url = f"https://{name.lower()}.ru/rss"
    return db.sources.get_by_fetch_url(fetch_url) or db.sources.add(
        Source(
            name=name,
            url=f"https://{name.lower()}.ru/",
            kind="rss",
            category=category,
            fetch_url=fetch_url,
        )
    )


def _document(db: Database, external_id: str, *, text: str = NEWS_TEXT, **overrides) -> int:
    source = _source(db, overrides.pop("source_name", "Лента"))
    with db.transaction():
        return db.documents.insert(
            RawDocument(
                source_id=source.id,
                external_id=external_id,
                url=f"https://лента.ru/{external_id}",
                title="Оператор платного ТВ запустил рекомендательный сервис",
                text=text,
                published_at=overrides.pop("published_at", NOW),
                fetched_at=NOW,
            )
        )


def _row(db: Database, document_id: int):
    return db.conn.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()


@contextmanager
def _store_fails(service: ProcessingService):
    """Прогон, в котором запись карточки падает на каждом документе."""

    def boom(unit, company, prompt_version, report):
        raise RuntimeError(BOOM)

    service._store = boom
    try:
        yield
    finally:
        del service._store


@pytest.fixture
def service(config, db, frozen_clock) -> ProcessingService:
    return ProcessingService(config, db, provider=FakeLLM(news_answer()), embedder=None)


# ── репозиторий: отметка о сбое ────────────────────────────────────────────


def test_mark_failed_records_the_error_the_attempt_and_the_stamp(db, frozen_clock):
    document_id = _document(db, "d1")

    with db.transaction():
        db.documents.mark_failed(document_id, "RuntimeError: боль")

    row = _row(db, document_id)
    assert (row["attempts"], row["last_error"], row["last_attempt_at"]) == (
        1, "RuntimeError: боль", NOW
    )


def test_mark_failed_counts_every_attempt(db, frozen_clock):
    document_id = _document(db, "d1")

    with db.transaction():
        db.documents.mark_failed(document_id, "HTTP 500")
        db.documents.mark_failed(document_id, "HTTP 502")

    row = _row(db, document_id)
    assert (row["attempts"], row["last_error"]) == (2, "HTTP 502")


def test_mark_failed_takes_the_moment_it_is_given(db):
    document_id = _document(db, "d1")

    with db.transaction():
        db.documents.mark_failed(document_id, "HTTP 500", at=LATER)

    assert _row(db, document_id)["last_attempt_at"] == LATER


def test_a_long_error_is_cut_to_five_hundred_characters(db, frozen_clock):
    document_id = _document(db, "d1")

    with db.transaction():
        db.documents.mark_failed(document_id, "ы" * 600)

    assert len(_row(db, document_id)["last_error"]) == 500


@pytest.mark.parametrize("error", ["", None], ids=["empty", "none"])
def test_an_empty_error_still_counts_as_an_attempt(db, frozen_clock, error):
    document_id = _document(db, "d1")

    with db.transaction():
        db.documents.mark_failed(document_id, error)

    row = _row(db, document_id)
    assert (row["attempts"], row["last_error"]) == (1, "")


def test_clear_failure_drops_the_error_but_keeps_the_history_of_attempts(db, frozen_clock):
    document_id = _document(db, "d1")

    with db.transaction():
        db.documents.mark_failed(document_id, "HTTP 500")
        db.documents.clear_failure(document_id, at=LATER)

    row = _row(db, document_id)
    assert (row["last_error"], row["attempts"], row["last_attempt_at"]) == ("", 1, LATER)


# ── репозиторий: очередь сбойных ───────────────────────────────────────────


def test_count_failed_counts_only_documents_still_without_a_card(db, item_factory, frozen_clock):
    failed = _document(db, "d1")
    carded = _document(db, "d2")
    hidden = _document(db, "d3")
    item_factory(db, carded)
    with db.transaction():
        for document_id in (failed, carded, hidden):
            db.documents.mark_failed(document_id, "HTTP 500")
        db.conn.execute("UPDATE documents SET hidden=1 WHERE id=?", (hidden,))

    assert db.documents.count_failed() == 1


def test_count_failed_of_a_queue_without_failures_is_zero(db):
    _document(db, "d1")

    assert db.documents.count_failed() == 0


def test_only_failed_narrows_the_queue_to_documents_that_fell_over(db, frozen_clock):
    failed = _document(db, "d1")
    _document(db, "d2")
    with db.transaction():
        db.documents.mark_failed(failed, "HTTP 500")

    assert [r["id"] for r in db.documents.unprocessed(only_failed=True)] == [failed]
    assert len(db.documents.unprocessed()) == 2


def test_a_cleared_document_leaves_the_failed_queue(db, frozen_clock):
    document_id = _document(db, "d1")
    with db.transaction():
        db.documents.mark_failed(document_id, "HTTP 500")

    with db.transaction():
        db.documents.clear_failure(document_id)

    assert db.documents.unprocessed(only_failed=True) == []
    assert [r["id"] for r in db.documents.unprocessed()] == [document_id]


def test_only_failed_still_honours_the_other_filters(db, frozen_clock):
    first = _document(db, "d1")
    second = _document(db, "d2", published_at="2026-08-01T00:00:00+00:00")
    with db.transaction():
        db.documents.mark_failed(first, "HTTP 500")
        db.documents.mark_failed(second, "HTTP 500")

    inside_window = db.documents.unprocessed(only_failed=True, since="2026-09-01T00:00:00+00:00")

    assert [r["id"] for r in inside_window] == [first]
    assert len(db.documents.unprocessed(only_failed=True, limit=1)) == 1


# ── прогон: сбой остаётся на документе ─────────────────────────────────────


def test_a_unit_that_fails_leaves_its_error_on_the_document(service, db):
    document_id = _document(db, "d1")

    with _store_fails(service):
        report = service.run()

    row = _row(db, document_id)
    assert report.failed == 1
    assert (row["attempts"], row["last_error"]) == (1, f"RuntimeError: {BOOM}")
    assert db.items.count() == 0


def test_a_failed_document_is_what_only_failed_picks_up_next_time(service, db):
    document_id = _document(db, "d1", published_at=LATER)  # свежее — прогон возьмёт его первым
    _document(db, "d2")
    with _store_fails(service):
        service.run(limit=1)

    assert [r["id"] for r in db.documents.unprocessed(only_failed=True)] == [document_id]


def test_a_later_success_clears_the_failure_and_keeps_the_attempt_count(service, db):
    document_id = _document(db, "d1")
    with _store_fails(service):
        service.run()

    report = service.run()

    row = _row(db, document_id)
    assert (report.items_new, report.failed) == (1, 0)
    assert (row["last_error"], row["attempts"]) == ("", 1)
    assert db.items.item_for_document(document_id) is not None


def test_only_failed_leaves_the_untouched_documents_alone(service, db):
    failed = _document(db, "d1", published_at=LATER)  # свежее — прогон возьмёт его первым
    fresh = _document(db, "d2")
    with _store_fails(service):
        service.run(limit=1)
        assert db.documents.count_failed() == 1

    report = service.run(only_failed=True)

    assert report.documents == 1
    assert db.items.item_for_document(failed) is not None
    assert db.items.item_for_document(fresh) is None


def test_the_failed_counter_is_what_the_queue_status_shows(service, db):
    _document(db, "d1")
    with _store_fails(service):
        service.run()

    status = service.queue_status()

    assert (status["unprocessed"], status["failed"]) == (1, 1)


def test_progress_counts_documents_not_clusters(service, db):
    """Перепечатки схлопываются в один кластер, но прогресс меряется документами."""
    _document(db, "d1", text=ARTICLE_TEXT)
    _document(db, "d2", text=REPRINT_TEXT)

    report = service.run()

    assert (report.documents, report.clusters, report.processed) == (2, 1, 2)
    assert db.processing_runs.latest().processed == 2


def test_a_run_over_an_empty_queue_reports_no_progress_at_all(service, db):
    report = service.run()

    assert (report.documents, report.processed) == (0, 0)
    assert db.processing_runs.latest().processed == 0


# ── HTTP: параметр доходит до сервиса ──────────────────────────────────────


def test_the_only_failed_flag_reaches_the_run_and_narrows_it(client, file_db, fake_llm,
                                                             frozen_clock):
    failed = _document(file_db, "d1")
    fresh = _document(file_db, "d2")
    with file_db.transaction():
        file_db.documents.mark_failed(failed, "RuntimeError: прошлый прогон")

    response = client.post("/api/v1/processing/runs", json={"only_failed": True})

    assert response.status_code == 202
    assert response.json()["params"]["only_failed"] is True
    run = client.get(f"/api/v1/processing/runs/{response.json()['id']}").json()
    assert (run["status"], run["documents"], run["items_new"]) == ("done", 1, 1)
    assert file_db.items.item_for_document(failed) is not None
    assert file_db.items.item_for_document(fresh) is None


def test_a_run_without_the_flag_takes_the_whole_queue(client, file_db, fake_llm, frozen_clock):
    failed = _document(file_db, "d1")
    fresh = _document(file_db, "d2")
    with file_db.transaction():
        file_db.documents.mark_failed(failed, "RuntimeError: прошлый прогон")

    response = client.post("/api/v1/processing/runs", json={})

    assert response.json()["params"]["only_failed"] is False
    assert file_db.items.item_for_document(failed) is not None
    assert file_db.items.item_for_document(fresh) is not None


def test_the_cli_flag_narrows_the_run_to_the_failed_documents(config, hub_paths, file_db,
                                                              frozen_clock, capsys):
    failed = _document(file_db, "d1")
    fresh = _document(file_db, "d2")
    with file_db.transaction():
        file_db.documents.mark_failed(failed, "RuntimeError: прошлый прогон")
    args = cli.build_parser().parse_args(["process", "--only-failed"])

    # Без ключа модели карточка собирается деградированной, и команда честно даёт 2.
    assert cli._cmd_process(args, config, hub_paths) == 2

    assert "обработано 1 документ(ов)" in capsys.readouterr().out
    assert file_db.items.item_for_document(failed) is not None
    assert file_db.items.item_for_document(fresh) is None


def test_the_processing_status_shows_the_failed_queue_over_http(client, file_db, frozen_clock):
    failed = _document(file_db, "d1")
    _document(file_db, "d2")
    with file_db.transaction():
        file_db.documents.mark_failed(failed, "RuntimeError: прошлый прогон")

    body = client.get("/api/v1/processing").json()

    assert (body["unprocessed"], body["failed"]) == (2, 1)
