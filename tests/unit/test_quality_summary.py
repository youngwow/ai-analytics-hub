"""Сводка качества: что база честно знает без размеченного набора (план 4.1).

`GET /processing/quality` — единственное место, где видно, где именно теряется
прогон: разбивка вызовов модели по этапу и статусу, по суткам и состояние очереди
(сколько ждёт и сколько упало).
"""

from __future__ import annotations

import pytest

from src.models import LlmCall, RawDocument, Source
from src.models.responses import QualityResponse
from src.repositories import Database
from src.services.processing_service import ProcessingService

NOW = "2026-09-02T12:00:00+00:00"


def _document(db: Database, external_id: str) -> int:
    source = db.sources.get_by_fetch_url("https://a.ru/rss") or db.sources.add(
        Source(name="Лента", url="https://a.ru/", kind="rss", fetch_url="https://a.ru/rss")
    )
    with db.transaction():
        return db.documents.insert(
            RawDocument(
                source_id=source.id,
                external_id=external_id,
                url=f"https://a.ru/{external_id}",
                title=f"Материал {external_id}",
                published_at=NOW,
                fetched_at=NOW,
            )
        )


@pytest.fixture
def service(config, db) -> ProcessingService:
    return ProcessingService(config, db, provider=None, embedder=None)


@pytest.fixture
def hub(db, item_factory) -> dict:
    """Две карточки, две ждущие обработки строки и три вызова модели за двое суток."""
    carded = _document(db, "carded")
    edited = _document(db, "edited")
    waiting = _document(db, "waiting")
    failed = _document(db, "failed")
    ids = {
        "carded": item_factory(db, carded, priority="high", degraded=True),
        "edited": item_factory(
            db, edited, priority="medium", needs_review=True, manual_overrides=["summary"]
        ),
    }
    with db.transaction():
        db.documents.mark_failed(failed, "RuntimeError: боль", at=NOW)
        db.llm_calls.add(
            LlmCall(stage="s2_s5", model="m", tokens_in=100, tokens_out=20, latency_ms=200,
                    created_at="2026-09-01T10:00:00+00:00")
        )
        db.llm_calls.add(
            LlmCall(stage="s2_s5", model="m", tokens_in=300, tokens_out=40, latency_ms=400,
                    created_at="2026-09-02T10:00:00+00:00")
        )
        db.llm_calls.add(
            LlmCall(stage="s2_s5", model="m", tokens_in=50, tokens_out=0, latency_ms=90,
                    status="failed", error="429", created_at="2026-09-02T14:00:00+00:00")
        )
    return {**ids, "waiting": waiting, "failed": failed}


# ── карточки ───────────────────────────────────────────────────────────────


def test_the_summary_of_an_empty_hub_is_all_zeros(service):
    summary = service.quality_summary()

    assert summary["items"] == 0
    assert summary["by_priority"] == {"high": 0, "medium": 0, "low": 0}
    assert (summary["degraded"], summary["hallucination_flags"]) == (0, 0)
    assert summary["edited_share"] == 0.0
    assert summary["queue"] == {"unprocessed": 0, "failed": 0}
    assert (summary["by_stage"], summary["by_day"]) == ([], [])


def test_the_cards_are_counted_by_priority_and_by_flag(service, hub):
    summary = service.quality_summary()

    assert summary["items"] == 2
    assert summary["by_priority"] == {"high": 1, "medium": 1, "low": 0}
    assert (summary["degraded"], summary["hallucination_flags"]) == (1, 1)
    assert summary["edited_share"] == 0.5


# ── очередь ────────────────────────────────────────────────────────────────


def test_the_queue_shows_what_waits_and_what_fell_over(service, hub):
    """Два документа без карточки, один из них — со следом прошлого сбоя."""
    assert service.quality_summary()["queue"] == {"unprocessed": 2, "failed": 1}


def test_a_cleared_failure_leaves_the_queue_counter(service, db, hub):
    with db.transaction():
        db.documents.clear_failure(hub["failed"], at=NOW)

    assert service.quality_summary()["queue"] == {"unprocessed": 2, "failed": 0}


# ── вызовы модели ──────────────────────────────────────────────────────────


def test_the_totals_come_from_the_call_log(service, hub):
    summary = service.quality_summary()

    assert summary["calls"] == 3
    assert (summary["tokens_in"], summary["tokens_out"]) == (450, 60)
    assert summary["avg_latency_ms"] == 230
    assert summary["failed_calls"] == 1


def test_the_breakdown_splits_the_calls_by_stage_and_status(service, hub):
    by_stage = service.quality_summary()["by_stage"]

    assert by_stage == [
        {"stage": "s2_s5", "status": "ok", "calls": 2, "avg_latency_ms": 300,
         "tokens_in": 400, "tokens_out": 60},
        {"stage": "s2_s5", "status": "failed", "calls": 1, "avg_latency_ms": 90,
         "tokens_in": 50, "tokens_out": 0},
    ]


def test_the_days_are_newest_first_with_their_own_failure_count(service, hub):
    by_day = service.quality_summary()["by_day"]

    assert [d["day"] for d in by_day] == ["2026-09-02", "2026-09-01"]
    assert by_day[0] == {"day": "2026-09-02", "calls": 2, "tokens_in": 350, "tokens_out": 40,
                         "failed": 1}
    assert by_day[1]["failed"] == 0


def test_the_window_narrows_the_call_statistics_but_not_the_cards(service, hub):
    summary = service.quality_summary(since="2026-09-02T00:00:00+00:00")

    assert summary["calls"] == 2
    assert [d["day"] for d in summary["by_day"]] == ["2026-09-02"]
    assert summary["items"] == 2


def test_the_upper_bound_of_the_window_is_honoured(service, hub):
    summary = service.quality_summary(until="2026-09-01T23:59:59+00:00")

    assert summary["calls"] == 1
    assert summary["by_stage"] == [
        {"stage": "s2_s5", "status": "ok", "calls": 1, "avg_latency_ms": 200,
         "tokens_in": 100, "tokens_out": 20}
    ]


# ── HTTP ───────────────────────────────────────────────────────────────────


def test_the_quality_payload_passes_its_schema_without_losing_a_field(service, hub):
    payload = service.quality_summary()

    assert QualityResponse.model_validate(payload).model_dump() == payload


def test_the_quality_route_answers_with_the_same_numbers(client, file_db, item_factory):
    document_id = _document(file_db, "carded")
    item_factory(file_db, document_id, priority="high")
    _document(file_db, "waiting")

    body = client.get("/api/v1/processing/quality").json()

    assert body["items"] == 1
    assert body["by_priority"]["high"] == 1
    assert body["queue"] == {"unprocessed": 1, "failed": 0}


def test_the_quality_route_takes_the_window_from_the_query_string(client, file_db):
    with file_db.transaction():
        file_db.llm_calls.add(
            LlmCall(stage="s2_s5", model="m", tokens_in=10, tokens_out=1, latency_ms=10,
                    created_at="2026-09-01T10:00:00+00:00")
        )

    inside = client.get(
        "/api/v1/processing/quality", params={"since": "2026-09-01T00:00:00+00:00"}
    ).json()
    after = client.get(
        "/api/v1/processing/quality", params={"since": "2026-09-02T00:00:00+00:00"}
    ).json()
    before = client.get(
        "/api/v1/processing/quality", params={"until": "2026-08-31T00:00:00+00:00"}
    ).json()

    assert inside["calls"] == 1
    assert (after["calls"], before["calls"]) == (0, 0)
