"""src/models/responses.py — `from_domain()` переводит dataclass в проводную форму.

Поля повторяют dataclass'ы один к одному, чтобы форма ответа не разъехалась с
тем, что видит CLI. Словари `FeedService` обязаны проходить свои схемы без потерь.
"""

from __future__ import annotations

from dataclasses import asdict

import pytest
from pydantic import ValidationError

from src.models import (
    CollectReport,
    EntitySpan,
    Item,
    ItemNote,
    ItemRevision,
    ItemTag,
    NpaEvent,
    ProcessingRun,
    Source,
    SourceRun,
)
from src.models.queries import DocumentQuery, FeedQuery
from src.models.responses import (
    ArchiveResponse,
    CollectionStatusResponse,
    DigestResponse,
    DocumentsResponse,
    EntityResponse,
    FacetsResponse,
    FeedResponse,
    FiltersResponse,
    HealthResponse,
    ItemCardResponse,
    ItemEditResponse,
    ItemResponse,
    NoteResponse,
    NpaEventResponse,
    ProbeResponse,
    ProcessingRunListResponse,
    ProcessingRunResponse,
    ProcessingStatusResponse,
    RevisionResponse,
    SourceResponse,
    SourceRunResponse,
    StatusResponse,
    TagResponse,
    VisibilityResponse,
)
from src.services.collection_service import CollectionService, CollectionWatcher
from src.services.item_service import ItemService
from src.services.processing_service import ProcessingService
from src.services.source_service import ProbeResult

NOW = "2026-09-05T09:00:00+00:00"


def _run(**overrides) -> ProcessingRun:
    base = dict(
        started_at=NOW, finished_at=NOW, status="done", trigger="api",
        params={"limit": 5, "source_id": None, "since": "2026-09-01", "profile_id": None,
                "force": False, "only_failed": False},
        documents=3, processed=2, clusters=2, items_new=2, items_joined=1, items_updated=0,
        degraded=1, needs_review=1, calls=4, failed=0, elapsed_s=1.5, error="",
        heartbeat_at=NOW, id=8,
    )
    return ProcessingRun(**{**base, **overrides})


def _wire(run: ProcessingRun) -> dict:
    """Прогон в проводной форме: поля dataclass плюс вычисляемый `progress`."""
    share = round(run.processed / run.documents, 3) if run.documents else None
    return {**asdict(run), "progress": min(share, 1.0) if share is not None else None}


# Поля ответа, которых нет в dataclass: считаются на границе, а не хранятся.
COMPUTED = {ProcessingRunResponse: {"progress"}}


ROUND_TRIPS = [
    (ProcessingRunResponse, _run()),
    (ProcessingRunResponse, _run(status="failed", finished_at=None, error="RuntimeError: x", id=9)),
    (
        SourceResponse,
        Source(
            id=3, name="Ведомости", url="https://vedomosti.ru/", kind="rss", category="media",
            fetch_url="https://vedomosti.ru/rss", status="paused",
            normalized_url="vedomosti.ru/rss", poll_interval="6h", next_run_at=NOW,
            category_hint="news", created_at=NOW, notes="платный доступ", deleted_at=None,
            created_by="young",
        ),
    ),
    (
        ItemResponse,
        Item(
            cluster_id=1, id=9, type="npa", npa_status="внесён", npa_key="112233-8",
            title="Минцифры внесло законопроект", summary="Операторы ИИ обязаны пройти аккредитацию.",
            priority="high", relevance_score=0.86, reasoning="Затрагивает реестр ПО.",
            confidence=0.81, tags=["регуляторика"], analyst_note="", visibility="hidden_digest",
            hidden_reason="не для инфраструктуры", origin="collected", is_archived=False,
            degraded=False, needs_review=True, date_estimated=False, model_name="fake-glm:test",
            prompt_version=2, profile_version=1, manual_overrides=["summary"], processed_at=NOW,
            published_at=NOW,
        ),
    ),
    (
        SourceRunResponse,
        SourceRun(
            source_id=1, started_at=NOW, finished_at=NOW, http_status=200, items_found=7,
            items_new=2, error_code="timeout", error_message="read timed out", id=4,
        ),
    ),
    (
        RevisionResponse,
        ItemRevision(
            item_id=1, field="summary", old_value="было", new_value="стало", actor="user",
            source_of_change="human", edit_reason="wrong_focus", created_at=NOW, id=2,
        ),
    ),
    (NoteResponse, ItemNote(item_id=1, body="вынести на совещание", author="young", created_at=NOW, id=5)),
    (TagResponse, ItemTag(item_id=1, tag="тренды", is_manual=True)),
    (
        EntityResponse,
        EntitySpan(
            role="who", value="Минцифры", normalized_value="минцифры", evidence_start=0,
            evidence_end=8, id=6,
        ),
    ),
    (
        NpaEventResponse,
        NpaEvent(
            item_id=1, status="внесён", occurred_at=NOW, source_url="https://sozd.duma.gov.ru/1",
            note="", created_by="user", created_at=NOW, id=7,
        ),
    ),
]


@pytest.mark.parametrize(
    ("response_cls", "obj"),
    ROUND_TRIPS,
    ids=[f"{cls.__name__}-{i}" for i, (cls, _) in enumerate(ROUND_TRIPS)],
)
def test_from_domain_round_trips_every_dataclass_field(response_cls, obj):
    dump = response_cls.from_domain(obj).model_dump()
    computed = COMPUTED.get(response_cls, set())

    assert set(dump) == set(asdict(obj)) | computed
    assert {k: v for k, v in dump.items() if k not in computed} == asdict(obj)


def test_probe_response_round_trips_the_probe_result():
    probe = ProbeResult(
        resolved_type="rss", feed_url="https://a.ru/rss", title="Лента", detection_method="resolver",
        already_exists=True, already_exists_source_id=3,
        preview=[{"title": "Новость", "url": "https://a.ru/1", "published_at": None}],
        warnings=["paywall_suspected"], note="",
    )

    assert ProbeResponse.from_domain(probe).model_dump() == asdict(probe)


# ── карточка целиком ───────────────────────────────────────────────────────


def _card(**overrides) -> dict:
    proposal = ItemRevision(
        item_id=9, field="summary", old_value=None, new_value="Версия модели.", actor="model",
        source_of_change="llm", created_at=NOW, id=11,
    )
    base = {
        "item": Item(cluster_id=1, id=9, title="Заголовок", processed_at=NOW),
        "entities": [EntitySpan(role="who", value="Минцифры", id=1)],
        "sources": [
            {"id": 1, "url": "https://a.ru/reprint", "title": "Перепечатка", "published_at": None,
             "is_canonical": 0, "source_name": "Лента"},
            {"id": 2, "url": "https://a.ru/original", "title": "Оригинал", "published_at": NOW,
             "is_canonical": 1, "source_name": "Ведомости"},
        ],
        "events": [],
        "revisions": [proposal],
        "notes": [ItemNote(item_id=9, body="заметка", created_at=NOW, id=3)],
        "tags": [ItemTag(item_id=9, tag="регуляторика")],
        "model_proposals": {"title": None, "summary": proposal},
    }
    return {**base, **overrides}


def test_item_card_picks_the_canonical_url_from_the_canonical_source():
    card = ItemCardResponse.from_domain(_card())

    assert card.canonical_url == "https://a.ru/original"
    assert [s.source_name for s in card.sources] == ["Лента", "Ведомости"]
    assert card.item.id == 9
    assert [e.value for e in card.entities] == ["Минцифры"]
    assert [n.body for n in card.notes] == ["заметка"]
    assert [t.tag for t in card.tags] == ["регуляторика"]


@pytest.mark.parametrize(
    "sources",
    [
        [],
        [{"id": 1, "url": "https://a.ru/x", "title": "x", "published_at": None,
          "is_canonical": 0, "source_name": None}],
        [{"id": 1, "url": "", "title": "Без ссылки", "published_at": None,
          "is_canonical": 1, "source_name": "Ручной ввод"}],
    ],
    ids=["no-sources", "no-canonical", "canonical-without-url"],
)
def test_item_card_has_no_canonical_url_when_there_is_nothing_to_link(sources):
    assert ItemCardResponse.from_domain(_card(sources=sources)).canonical_url is None


def test_item_card_keeps_the_model_proposals_by_field():
    card = ItemCardResponse.from_domain(_card())

    assert card.model_proposals["title"] is None
    assert card.model_proposals["summary"].new_value == "Версия модели."
    assert card.model_proposals["summary"].source_of_change == "llm"


def test_item_card_accepts_what_item_service_returns(config, file_db, corpus):
    card = ItemService(config, file_db).get_item(corpus["npa_high"])

    response = ItemCardResponse.from_domain(card)

    assert response.canonical_url == "https://s1.example.ru/doc-1"
    assert response.item.title == "Минцифры внесло законопроект об аккредитации ИИ-сервисов"
    assert set(response.model_proposals) == {"title", "summary", "priority", "type", "tags"}
    assert [e.value for e in response.entities] == ["Минцифры", "112233-8"]
    assert [t.tag for t in response.tags] == ["регуляторика"]


# ── короткие ответы ────────────────────────────────────────────────────────


def test_visibility_response_carries_only_the_id_and_the_state():
    item = Item(cluster_id=1, id=4, visibility="hidden_feed", hidden_reason="нерелевантно")
    assert VisibilityResponse.from_domain(item).model_dump() == {"id": 4, "visibility": "hidden_feed"}


def test_item_edit_response_lists_the_manual_overrides_next_to_the_card():
    item = Item(cluster_id=1, id=4, manual_overrides=["summary", "tags"])

    response = ItemEditResponse.from_domain(item)

    assert response.manual_overrides == ["summary", "tags"]
    assert response.item.manual_overrides == ["summary", "tags"]
    assert response.item.id == 4


def test_health_response_only_knows_ok_and_degraded():
    with pytest.raises(ValidationError, match="status"):
        HealthResponse(status="down", app="hub", version="0.2.0", environment="local")


def test_archive_response_carries_only_the_id_and_the_flag():
    item = Item(cluster_id=1, id=4, is_archived=True, visibility="hidden_feed")

    assert ArchiveResponse.from_domain(item).model_dump() == {"id": 4, "is_archived": True}


# ── очередь ИИ ─────────────────────────────────────────────────────────────


def test_processing_run_response_only_knows_the_three_statuses():
    with pytest.raises(ValidationError, match="status"):
        ProcessingRunResponse.from_domain(_run(status="queued"))


def test_processing_run_list_response_wraps_the_history_in_order():
    runs = [_run(id=2), _run(id=1)]

    response = ProcessingRunListResponse(runs=[ProcessingRunResponse.from_domain(r) for r in runs])

    assert [r.id for r in response.runs] == [2, 1]
    assert response.model_dump() == {"runs": [_wire(r) for r in runs]}


def test_processing_status_response_wraps_the_open_and_the_last_run():
    open_run, last = _run(id=3, status="running", finished_at=None), _run(id=2)

    response = ProcessingStatusResponse.from_domain(
        {"running": open_run, "last": last, "unprocessed": 4, "failed": 1, "llm_available": False}
    )

    assert response.model_dump() == {
        "running": _wire(open_run),
        "last": _wire(last),
        "unprocessed": 4,
        "failed": 1,
        "llm_available": False,
    }


def test_processing_status_response_accepts_an_empty_queue():
    response = ProcessingStatusResponse.from_domain(
        {"running": None, "last": None, "unprocessed": 0, "failed": 0, "llm_available": True}
    )

    assert response.model_dump() == {
        "running": None, "last": None, "unprocessed": 0, "failed": 0, "llm_available": True,
    }


def test_processing_status_response_accepts_what_the_service_returns(config, db):
    service = ProcessingService(config, db, provider=None, embedder=None)
    run = service.enqueue(limit=2)

    response = ProcessingStatusResponse.from_domain(service.queue_status())

    assert response.running.id == response.last.id == run.id
    assert (response.unprocessed, response.failed, response.llm_available) == (0, 0, False)


# ── прогресс прогона: доля пройденного, а не «идёт» ────────────────────────


@pytest.mark.parametrize(
    ("documents", "processed", "expected"),
    [
        (0, 0, None),
        (4, 0, 0.0),
        (4, 1, 0.25),
        (3, 1, 0.333),
        (4, 4, 1.0),
        (4, 7, 1.0),
    ],
    ids=["nothing-to-do", "just-started", "quarter", "rounded", "done", "over-count"],
)
def test_progress_is_the_share_of_documents_processed(documents, processed, expected):
    run = _run(documents=documents, processed=processed)

    progress = ProcessingRunResponse.from_domain(run).progress

    assert progress == expected


def test_progress_of_an_empty_run_is_none_not_zero():
    """Ноль из нуля — не «0% сделано»: прогону нечего было брать."""
    assert ProcessingRunResponse.from_domain(_run(documents=0, processed=0)).progress is None


# ── сбор ───────────────────────────────────────────────────────────────────


def test_collection_status_response_accepts_what_the_service_returns(config, db, frozen_clock):
    service = CollectionService(config, db, CollectionWatcher(lambda: CollectReport()))
    db.runs.add(
        CollectReport(
            started_at=NOW, finished_at=NOW, sources_ok=2, sources_fail=1, sources_not_modified=0,
            docs_new=5,
        )
    )

    payload = service.status()

    response = CollectionStatusResponse.model_validate(payload)
    assert response.model_dump() == payload
    assert (response.running, response.due_sources, response.last_collect.docs_new) == (
        False, 0, 5
    )


def test_collection_status_response_accepts_an_idle_service_without_a_collect(config, db):
    payload = CollectionService(config, db, CollectionWatcher(lambda: CollectReport())).status()

    assert CollectionStatusResponse.model_validate(payload).last_collect is None


def test_collection_status_response_requires_the_whole_collect_run():
    payload = {
        "running": False, "busy": False, "interval_seconds": 900, "started_at": None,
        "next_tick_at": None, "cycles": 0, "last_error": "", "due_sources": 0,
        "last_collect": {"id": 1, "docs_new": 5},
    }

    with pytest.raises(ValidationError, match="last_collect"):
        CollectionStatusResponse.model_validate(payload)


# ── словари FeedService проходят свои схемы без потерь ─────────────────────


FEED_PAYLOADS = [
    (FeedResponse, lambda feed: feed.items(FeedQuery.build(limit=50))),
    (FacetsResponse, lambda feed: feed.facets(FeedQuery.build())),
    (FiltersResponse, lambda feed: feed.filters()),
    (DocumentsResponse, lambda feed: feed.documents(DocumentQuery.build())),
    (DigestResponse, lambda feed: feed.digest(FeedQuery.build(), fmt="json")),
    (StatusResponse, lambda feed: feed.status()),
]


@pytest.mark.parametrize(
    ("response_cls", "call"), FEED_PAYLOADS, ids=[cls.__name__ for cls, _ in FEED_PAYLOADS]
)
def test_feed_service_payloads_validate_without_losing_a_field(
    feed, corpus, corpus_sources, document_factory, response_cls, call
):
    document_factory(corpus_sources["media"], title="Ещё не обработан", text="Текст")

    payload = call(feed)

    assert response_cls.model_validate(payload).model_dump() == payload


def test_feed_response_carries_the_real_feed(feed, corpus):
    response = FeedResponse.model_validate(feed.items(FeedQuery.build(limit=50)))

    assert response.total == 6
    assert response.next_cursor is None
    assert [row.id for row in response.items][:2] == [corpus["hidden_digest"], corpus["npa_high"]]
    assert response.items[1].flags.model_dump() == {
        "degraded": False, "needs_review": False, "date_estimated": False, "edited": False,
    }
