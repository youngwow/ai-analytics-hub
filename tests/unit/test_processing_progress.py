"""Regression checks for incremental AI processing and failure reporting."""
from dataclasses import replace
from threading import Event

import pytest
from support import default_raw_config

from src.config import Config
from src.models import CompanyProfile, RawDocument, Source
from src.processing.llm import LlmConfigError
from src.processing.pipeline import Draft
from src.repositories import Database
from src.services.processing_service import ProcessingService, _Unit

NOW = "2026-09-02T12:00:00+00:00"


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "hub.db"))
    yield database
    database.close()


def service(db):
    config = Config.from_dict(default_raw_config())
    config = replace(config, processing=replace(config.processing, concurrency=2))
    return ProcessingService(config, db)


def unit(index):
    return _Unit(document_id=index, document=RawDocument(source_id=1, external_id=str(index), url='', title=str(index)), norm_text=str(index), simhash='0000000000000000', members=[index])


def test_progress_is_visible_before_completion_and_survives_failure(db):
    run = db.processing_runs.start({})
    db.processing_runs.progress(run.id, {"documents": 10, "clusters": 9, "items_new": 2})
    assert db.processing_runs.get(run.id).items_new == 2
    assert db.processing_runs.get(run.id).status == 'running'
    assert db.processing_runs.get(run.id).finished_at is None
    db.processing_runs.fail(run.id, 'provider error')
    db.processing_runs.progress(run.id, {"items_new": 9})
    assert db.processing_runs.get(run.id).items_new == 2
    assert db.processing_runs.get(run.id).status == 'failed'


def test_fast_result_is_delivered_while_first_model_call_is_still_waiting(db, monkeypatch):
    svc = service(db)
    release = Event()
    delivered = []

    def model(text, **kwargs):
        if text == '1':
            assert release.wait(2), 'fast result was held behind the slow first call'
        return Draft(title=text)

    def ready(result):
        delivered.append(result.document_id)
        if result.document_id == 2:
            release.set()

    monkeypatch.setattr(svc.pipeline, 'process', model)
    try:
        svc._draft_all([unit(1), unit(2)], CompanyProfile(name='Test'), on_ready=ready)
    finally:
        release.set()
    assert delivered == [2, 1]


def test_configuration_error_stops_batch_without_waiting_for_slow_call(db, monkeypatch):
    svc = service(db)
    release = Event()
    started = Event()
    finished = Event()
    calls = []

    def model(text, **kwargs):
        calls.append(text)
        if text == '1':
            started.set()
            try:
                release.wait(2)
            finally:
                finished.set()
        else:
            assert started.wait(1)
            raise LlmConfigError('chat: HTTP 401')
        return Draft()

    monkeypatch.setattr(svc.pipeline, 'process', model)
    try:
        with pytest.raises(LlmConfigError):
            svc._draft_all([unit(n) for n in range(1, 50)], CompanyProfile(name='Test'))
        assert not finished.is_set(), 'configuration error waited for unrelated in-flight request'
        assert sorted(calls) == ['1', '2']
    finally:
        release.set()
        assert finished.wait(2)


def test_run_commits_cards_and_counters_before_later_drafts(db, monkeypatch):
    svc = service(db)
    src = db.sources.add(Source(name='Local', url='manual://progress', fetch_url='manual://progress', kind='manual'))
    units = []
    for n in range(1, 3):
        doc = RawDocument(source_id=src.id, external_id=str(n), url=f'manual://{n}', title=str(n), text=f'Text {n}')
        with db.transaction():
            doc_id = db.documents.insert(doc)
        units.append(_Unit(document_id=doc_id, document=doc, norm_text=f'Text {n}', simhash='0000000000000000', members=[doc_id]))
    monkeypatch.setattr(svc, '_prepare', lambda rows, embed: units)

    def drafts(values, company, on_ready):
        for index, value in enumerate(values):
            value.draft = Draft(title=value.document.title, summary=['Stored summary.'])
            on_ready(value)
            progress = db.processing_runs.running()
            assert progress.documents == 2
            assert progress.items_new == index + 1
            assert db.items.item_for_document(value.document_id) is not None

    monkeypatch.setattr(svc, '_draft_all', drafts)
    result = svc.run(limit=2)
    assert result.items_new == 2
    assert db.processing_runs.latest().status == 'done'


def _queued_units(db, count: int) -> list[_Unit]:
    """`count` собранных документов и готовые к записи единицы поверх них."""
    source = db.sources.add(
        Source(name='Local', url='manual://progress', fetch_url='manual://progress', kind='manual')
    )
    units = []
    for n in range(1, count + 1):
        doc = RawDocument(
            source_id=source.id, external_id=str(n), url=f'manual://{n}', title=str(n),
            text=f'Текст {n}', published_at=NOW, fetched_at=NOW,
        )
        with db.transaction():
            doc_id = db.documents.insert(doc)
        units.append(
            _Unit(document_id=doc_id, document=doc, norm_text=f'Текст {n}',
                  simhash=f'{n:016x}', members=[doc_id])
        )
    return units


def test_progress_publishes_the_processed_count_and_a_heartbeat(db, frozen_clock):
    run = db.processing_runs.start({})

    db.processing_runs.progress(run.id, {"documents": 10, "processed": 4, "items_new": 3})

    stored = db.processing_runs.get(run.id)
    assert (stored.documents, stored.processed) == (10, 4)
    assert stored.heartbeat_at == NOW


def test_a_closed_run_gets_no_heartbeat(db, frozen_clock):
    """Биение застрявшего прогона — единственный признак жизни, врать им нельзя."""
    run = db.processing_runs.start({})
    db.processing_runs.finish(run.id, {"documents": 1, "processed": 1})

    db.processing_runs.progress(run.id, {"documents": 9, "processed": 9})

    stored = db.processing_runs.get(run.id)
    assert (stored.documents, stored.processed) == (1, 1)
    assert stored.heartbeat_at is None


def test_the_run_publishes_progress_after_every_unit_not_only_at_the_end(
    db, monkeypatch, frozen_clock
):
    svc = service(db)
    units = _queued_units(db, 2)
    seen: list[tuple[int, int, str | None]] = []

    def drafts(values, company, on_ready):
        for value in values:
            value.draft = Draft(title=value.document.title, summary=['Итог.'])
            on_ready(value)
            row = db.processing_runs.running()
            seen.append((row.documents, row.processed, row.heartbeat_at))

    monkeypatch.setattr(svc, '_prepare', lambda rows, embed: units)
    monkeypatch.setattr(svc, '_draft_all', drafts)

    report = svc.run(limit=2)

    assert seen == [(2, 1, NOW), (2, 2, NOW)]
    assert report.processed == 2
    assert db.processing_runs.latest().processed == 2
