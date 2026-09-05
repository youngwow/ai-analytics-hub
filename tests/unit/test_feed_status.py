"""src/feed/service.py::status — сводка для шапки: почему в ленте столько, сколько есть.

Разрыв «документов / карточек / без карточки» и список просроченных источников —
единственное, что объясняет пустую ленту, поэтому считается точно.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.models import CollectReport, FetchState

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def frozen_feed_clock(monkeypatch) -> datetime:
    """`status()` сравнивает расписание с `utc_now()` — пиним её, а не читаем часы."""
    import src.feed.service as service_mod

    monkeypatch.setattr(service_mod, "utc_now", lambda: NOW)
    return NOW


def _stale(feed) -> dict[str, dict]:
    return {row["name"]: row for row in feed.status()["stale_sources"]}


# ── счётчики ───────────────────────────────────────────────────────────────


def test_documents_cards_and_the_gap_between_them_are_counted(
    feed, corpus, corpus_sources, document_factory
):
    document_factory(corpus_sources["media"], title="Ещё не обработан")
    document_factory(corpus_sources["media"], title="И этот тоже")

    data = feed.status()

    assert data["documents"] == 10
    assert data["items"] == 8
    assert data["unprocessed"] == 2


def test_hidden_and_deleted_cards_still_count_as_cards(feed, corpus):
    """`items` — это то, что лежит в базе, а не то, что видно в ленте."""
    assert feed.status()["items"] == len(corpus) == 8


def test_a_hidden_document_is_not_counted_as_waiting_for_processing(
    feed, corpus_sources, document_factory
):
    document_factory(corpus_sources["media"], title="Скрытый", hidden=1)
    document_factory(corpus_sources["media"], title="Ждёт обработки")

    assert feed.status()["unprocessed"] == 1


def test_an_empty_installation_reports_zeroes_rather_than_nothing(feed):
    data = feed.status()

    assert (data["documents"], data["items"], data["unprocessed"]) == (0, 0, 0)
    assert data["sources"] == {}
    assert data["stale_sources"] == []
    assert data["last_collect_at"] is None


def test_the_last_collect_is_the_finish_of_the_newest_run(feed, file_db):
    file_db.runs.add(
        CollectReport(started_at="2026-09-05T10:00:00+00:00",
                      finished_at="2026-09-05T10:01:00+00:00")
    )
    file_db.runs.add(
        CollectReport(started_at="2026-09-05T11:00:00+00:00",
                      finished_at="2026-09-05T11:02:00+00:00")
    )

    assert feed.status()["last_collect_at"] == "2026-09-05T11:02:00+00:00"


def test_the_configured_timezone_travels_with_the_summary(feed, config):
    assert feed.status()["timezone"] == config.api.timezone == "Europe/Moscow"


# ── источники по статусам ──────────────────────────────────────────────────


def test_sources_are_counted_by_status_including_the_deleted_ones(feed, file_db, source_factory):
    source_factory("Активный один")
    source_factory("Активный два")
    source_factory("На паузе", status="paused")
    source_factory("Со сбоями", status="error")
    file_db.sources.remove(source_factory("Удалённый").id)

    assert feed.status()["sources"] == {"active": 2, "paused": 1, "error": 1, "deleted": 1}


# ── просроченные источники ─────────────────────────────────────────────────


def test_a_source_past_its_schedule_is_reported_with_the_delay_in_minutes(feed, source_factory):
    source_factory("Просрочен на полчаса", next_run_at="2026-09-05T11:30:00+00:00")

    assert _stale(feed)["Просрочен на полчаса"]["overdue_minutes"] == 30


def test_a_source_whose_turn_has_not_come_is_not_reported(feed, source_factory):
    source_factory("Ждёт очереди", next_run_at="2026-09-05T13:00:00+00:00")

    assert feed.status()["stale_sources"] == []


def test_a_source_due_exactly_now_counts_as_due_with_no_delay(feed, source_factory):
    source_factory("Ровно сейчас", next_run_at="2026-09-05T12:00:00+00:00")

    assert _stale(feed)["Ровно сейчас"]["overdue_minutes"] == 0


def test_a_paused_source_is_never_called_stale(feed, source_factory):
    source_factory("На паузе", status="paused", next_run_at="2026-09-01T00:00:00+00:00")

    assert feed.status()["stale_sources"] == []


def test_a_broken_source_keeps_being_watched_because_error_is_not_a_pause(feed, source_factory):
    source_factory("Со сбоями", status="error", next_run_at="2026-09-05T11:00:00+00:00")

    assert _stale(feed)["Со сбоями"]["overdue_minutes"] == 60


def test_a_deleted_source_drops_out_of_the_stale_list(feed, file_db, source_factory):
    source = source_factory("Удалённый", next_run_at="2026-09-05T11:00:00+00:00")
    file_db.sources.remove(source.id)

    assert feed.status()["stale_sources"] == []


def test_the_last_error_and_the_failure_streak_travel_with_the_stale_entry(
    feed, file_db, source_factory
):
    source = source_factory("Молчит", next_run_at="2026-09-05T11:00:00+00:00")
    with file_db.transaction():
        file_db.fetch_state.save(
            FetchState(source_id=source.id, consecutive_failures=3, last_error="я" * 300)
        )

    entry = _stale(feed)["Молчит"]

    assert entry["id"] == source.id
    assert entry["consecutive_failures"] == 3
    assert entry["last_error"] == "я" * 200  # обрезано, чтобы шапка не разъехалась


def test_a_source_that_never_failed_reports_a_clean_state(feed, source_factory):
    source_factory("Просто просрочен", next_run_at="2026-09-05T11:00:00+00:00")

    entry = _stale(feed)["Просто просрочен"]

    assert (entry["consecutive_failures"], entry["last_error"]) == (0, "")


def test_the_most_overdue_source_is_listed_first(feed, source_factory):
    source_factory("Полчаса", next_run_at="2026-09-05T11:30:00+00:00")
    source_factory("Сутки", next_run_at="2026-09-04T12:00:00+00:00")
    source_factory("Три часа", next_run_at="2026-09-05T09:00:00+00:00")

    names = [row["name"] for row in feed.status()["stale_sources"]]

    assert names == ["Сутки", "Три часа", "Полчаса"]


def test_the_stale_list_is_capped_so_the_header_stays_readable(feed, source_factory):
    for number in range(25):
        source_factory(f"Источник {number:02d}", next_run_at="2026-09-05T11:00:00+00:00")

    assert len(feed.status()["stale_sources"]) == 20
