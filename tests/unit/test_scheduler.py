"""src/sources/scheduler.py — whose turn it is to be polled.

There is no daemon: `collect --watch` ticks and asks the database. Every test
here injects the clock, so nothing depends on when the suite runs.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from src.models import POLL_INTERVALS, Source
from src.sources import scheduler

NOW_ISO = "2026-09-02T12:00:00+00:00"


def _source(db, **overrides) -> Source:
    base = dict(
        name="Ведомости",
        url="https://www.vedomosti.ru",
        kind="rss",
        category="media",
        fetch_url="https://www.vedomosti.ru/rss/news",
    )
    return db.sources.add(Source(**{**base, **overrides}))


# ── intervals ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("interval", "seconds"),
    [("15m", 900), ("1h", 3600), ("6h", 21600), ("24h", 86400)],
)
def test_interval_seconds_covers_every_allowed_interval(interval, seconds):
    assert scheduler.interval_seconds(interval) == seconds


def test_every_allowed_interval_has_a_length():
    assert set(POLL_INTERVALS) == set(scheduler.INTERVAL_SECONDS)


@pytest.mark.parametrize("interval", ["", "5m", "week", None], ids=["empty", "unknown", "word",
                                                                    "none"])
def test_an_unknown_interval_falls_back_to_the_default_hour(interval):
    assert scheduler.interval_seconds(interval) == 3600
    assert scheduler.DEFAULT_INTERVAL == "1h"


@pytest.mark.parametrize(
    ("category", "interval"),
    [
        ("regulator", "1h"),
        ("telegram", "1h"),
        ("media", "6h"),
        ("manual", "24h"),
        ("что-то новое", "1h"),
    ],
    ids=["regulator", "telegram", "media", "manual", "unknown-category"],
)
def test_suggest_interval_per_category(category, interval):
    assert scheduler.suggest_interval(category) == interval


# ── next_run_at ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("interval", "expected"),
    [
        ("15m", "2026-09-02T12:15:00+00:00"),
        ("1h", "2026-09-02T13:00:00+00:00"),
        ("6h", "2026-09-02T18:00:00+00:00"),
        ("24h", "2026-09-03T12:00:00+00:00"),
    ],
)
def test_next_run_at_adds_the_interval_to_the_injected_clock(now, interval, expected):
    assert scheduler.next_run_at(interval, now=now) == expected


def test_next_run_at_uses_the_frozen_module_clock_when_none_is_passed(frozen_clock):
    assert scheduler.next_run_at("1h") == "2026-09-02T13:00:00+00:00"


# ── is_due ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", ["paused", "deleted"])
def test_a_paused_or_deleted_source_is_never_due(now, status):
    source = Source(name="A", url="https://a.ru", status=status, next_run_at=None)
    assert scheduler.is_due(source, now=now) is False


def test_a_broken_source_is_still_due(now):
    """`error` — отметка о здоровье, а не пауза: иначе источник не починится сам."""
    source = Source(name="A", url="https://a.ru", status="error", next_run_at=None)
    assert scheduler.is_due(source, now=now) is True


def test_a_source_without_a_schedule_is_due_immediately(now):
    source = Source(name="A", url="https://a.ru", status="active", next_run_at=None)
    assert scheduler.is_due(source, now=now) is True


@pytest.mark.parametrize(
    ("offset_seconds", "due"),
    [(-60, True), (0, True), (60, False)],
    ids=["past", "exactly-now", "future"],
)
def test_is_due_compares_next_run_at_with_the_clock(now, offset_seconds, due):
    scheduled = (now + timedelta(seconds=offset_seconds)).isoformat()
    source = Source(name="A", url="https://a.ru", status="active", next_run_at=scheduled)
    assert scheduler.is_due(source, now=now) is due


def test_an_unparseable_schedule_is_treated_as_due(now):
    """A corrupt timestamp must not silently take a source out of the rotation."""
    source = Source(name="A", url="https://a.ru", status="active", next_run_at="скоро")
    assert scheduler.is_due(source, now=now) is True


def test_a_naive_schedule_is_read_as_moscow_time(now):
    """Moscow is UTC+3, so 14:59 local is 11:59Z — one minute before the clock."""
    source = Source(name="A", url="https://a.ru", status="active",
                    next_run_at="2026-09-02T14:59:00")
    assert scheduler.is_due(source, now=now) is True
    source.next_run_at = "2026-09-02T15:01:00"
    assert scheduler.is_due(source, now=now) is False


# ── due() over the database ────────────────────────────────────────────────


def test_due_returns_only_sources_whose_turn_has_come(db, now):
    ready = _source(db, name="Пора", fetch_url="https://a.ru/rss")
    later = _source(db, name="Позже", fetch_url="https://b.ru/rss")
    paused = _source(db, name="Пауза", fetch_url="https://c.ru/rss", status="paused")
    db.sources.schedule(ready.id, "2026-09-02T11:00:00+00:00")
    db.sources.schedule(later.id, "2026-09-02T13:00:00+00:00")
    db.sources.schedule(paused.id, "2026-09-02T11:00:00+00:00")

    assert [s.id for s in scheduler.due(db, now=now)] == [ready.id]


def test_due_orders_by_the_oldest_schedule_first(db, now):
    late = _source(db, name="Давно", fetch_url="https://a.ru/rss")
    recent = _source(db, name="Недавно", fetch_url="https://b.ru/rss")
    db.sources.schedule(recent.id, "2026-09-02T11:30:00+00:00")
    db.sources.schedule(late.id, "2026-09-02T06:00:00+00:00")

    assert [s.id for s in scheduler.due(db, now=now)] == [late.id, recent.id]


def test_due_honours_the_limit(db, now):
    for i in range(3):
        source = _source(db, name=f"#{i}", fetch_url=f"https://{i}.ru/rss")
        db.sources.schedule(source.id, "2026-09-02T06:00:00+00:00")
    assert len(scheduler.due(db, now=now, limit=2)) == 2


def test_a_deleted_source_leaves_the_rotation(db, now):
    source = _source(db)
    db.sources.schedule(source.id, "2026-09-02T06:00:00+00:00")
    assert [s.id for s in scheduler.due(db, now=now)] == [source.id]
    db.sources.remove(source.id)
    assert scheduler.due(db, now=now) == []


def test_due_uses_the_frozen_module_clock_when_none_is_passed(db, frozen_clock):
    source = _source(db)
    db.sources.schedule(source.id, "2026-09-02T11:59:59+00:00")
    assert [s.id for s in scheduler.due(db)] == [source.id]
    db.sources.schedule(source.id, "2026-09-02T12:00:01+00:00")
    assert scheduler.due(db) == []


# ── reschedule ─────────────────────────────────────────────────────────────


def test_reschedule_moves_the_source_forward_by_its_own_interval(db, now):
    source = _source(db, poll_interval="6h")
    db.sources.schedule(source.id, "2026-09-02T06:00:00+00:00")

    moment = scheduler.reschedule(db, source, now=now)

    assert moment == "2026-09-02T18:00:00+00:00"
    assert source.next_run_at == moment  # the in-memory object is updated too
    assert db.sources.get(source.id).next_run_at == moment
    assert scheduler.due(db, now=now) == []


def test_reschedule_of_a_quarter_hourly_source_keeps_it_in_the_next_tick(db, now):
    source = _source(db, poll_interval="15m")
    scheduler.reschedule(db, source, now=now)
    assert db.sources.get(source.id).next_run_at == "2026-09-02T12:15:00+00:00"
    assert scheduler.due(db, now=now + timedelta(minutes=16))[0].id == source.id
