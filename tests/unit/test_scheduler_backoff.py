"""Расписание падающего источника: backoff и автопауза (план 5.1 + 5.2).

Лежащий сайт опрашивался с той же частотой, что и живой: он копил `source_runs`
вхолостую и мешал остальным. Теперь интервал растягивается вдвое за каждую
неудачу подряд (до 16×), а источник, молчащий неделю и полсотни попыток, уходит в
`paused` с причиной в `notes` — и возвращается штатным `resume`.

Часы инжектируются в коллектор, сеть — `MockRoutes`: ни одного обращения наружу.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from support import RSS, MockRoutes, rss_bytes

from src.config import Config
from src.models import FetchState, Source
from src.paths import ProjectPaths
from src.services.source_service import SourceService
from src.sources import scheduler
from src.sources.collector import FAILURES_TO_ERROR, Collector

RSS_URL = "https://feed.example.ru/rss.xml"
GOOD_FEED = rss_bytes([{"title": "Новость", "link": "https://example.ru/1"}])


def _config(raw_config) -> Config:
    raw_config["scraper"]["fetch_fulltext"] = False
    return Config.from_dict(raw_config)


def _add(db, **overrides) -> Source:
    base = dict(name="Лента", url="https://example.ru/", kind="rss", category="media",
                fetch_url=RSS_URL)
    return db.sources.add(Source(**{**base, **overrides}))


def _collector(config, db, routes, now, tmp_path) -> Collector:
    return Collector(
        config,
        ProjectPaths.from_root(str(tmp_path)),
        db,
        transport=routes.transport(),
        now=lambda: now,
        tavily_key="secret-key",
    )


def _state(db, source: Source, **fields) -> None:
    with db.transaction():
        db.fetch_state.save(FetchState(source_id=source.id, **fields))


def _schedule(db, source: Source) -> str | None:
    return db.sources.get(source.id).next_run_at


# ── backoff_factor ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("failures", "factor"),
    [(0, 1), (1, 2), (2, 4), (3, 8), (4, 16), (5, 16), (50, 16), (-1, 1)],
    ids=["none", "one", "two", "three", "four", "five", "fifty", "negative"],
)
def test_backoff_factor_doubles_per_failure_and_stops_at_sixteen(failures, factor):
    assert scheduler.backoff_factor(failures) == factor


def test_the_cap_is_four_doublings():
    assert scheduler.MAX_BACKOFF_STEPS == 4
    assert scheduler.backoff_factor(scheduler.MAX_BACKOFF_STEPS) == 16


# ── next_run_at с множителем ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("interval", "factor", "expected"),
    [
        ("15m", 1, "2026-09-02T12:15:00+00:00"),
        ("15m", 4, "2026-09-02T13:00:00+00:00"),
        ("15m", 16, "2026-09-02T16:00:00+00:00"),
        ("1h", 2, "2026-09-02T14:00:00+00:00"),
        ("24h", 16, "2026-09-18T12:00:00+00:00"),
    ],
    ids=["quarter-plain", "quarter-x4", "quarter-x16", "hour-x2", "day-x16"],
)
def test_the_factor_stretches_the_interval(now, interval, factor, expected):
    assert scheduler.next_run_at(interval, now=now, factor=factor) == expected


@pytest.mark.parametrize("factor", [0, -3], ids=["zero", "negative"])
def test_a_factor_below_one_never_pulls_the_poll_forward(now, factor):
    assert scheduler.next_run_at("1h", now=now, factor=factor) == "2026-09-02T13:00:00+00:00"


# ── reschedule ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("failures", "expected"),
    [
        (0, "2026-09-02T13:00:00+00:00"),
        (1, "2026-09-02T14:00:00+00:00"),
        (3, "2026-09-02T20:00:00+00:00"),
        (9, "2026-09-03T04:00:00+00:00"),
    ],
    ids=["healthy", "one-failure", "three-failures", "far-gone"],
)
def test_reschedule_applies_the_backoff_of_the_failure_count(db, now, failures, expected):
    source = _add(db)

    moment = scheduler.reschedule(db, source, now=now, failures=failures)

    assert moment == expected
    assert _schedule(db, source) == expected
    assert source.next_run_at == expected


def test_reschedule_without_failures_is_the_plain_interval(db, now):
    source = _add(db, poll_interval="6h")

    assert scheduler.reschedule(db, source, now=now) == "2026-09-02T18:00:00+00:00"


# ── should_pause ───────────────────────────────────────────────────────────


def _fetch_state(**fields) -> FetchState:
    return FetchState(source_id=1, **fields)


@pytest.mark.parametrize("status", ["active", "paused", "deleted"])
def test_only_a_broken_source_can_be_paused_automatically(now, status):
    source = Source(name="A", url="https://a.ru", status=status)
    state = _fetch_state(consecutive_failures=999, last_success_at=None)

    assert scheduler.should_pause(source, state, now=now) is False


def test_a_source_that_never_succeeded_is_paused_after_enough_failures(now):
    source = Source(name="A", url="https://a.ru", status="error")
    state = _fetch_state(consecutive_failures=scheduler.PAUSE_AFTER_FAILURES)

    assert scheduler.should_pause(source, state, now=now) is True


def test_one_failure_short_of_the_threshold_keeps_the_source_in_rotation(now):
    source = Source(name="A", url="https://a.ru", status="error")
    state = _fetch_state(consecutive_failures=scheduler.PAUSE_AFTER_FAILURES - 1)

    assert scheduler.should_pause(source, state, now=now) is False


@pytest.mark.parametrize(
    ("days_ago", "paused"),
    [(0, False), (6, False), (7, True), (30, True)],
    ids=["today", "six-days", "exactly-a-week", "a-month"],
)
def test_a_recent_success_protects_the_source_from_the_pause(now, days_ago, paused):
    source = Source(name="A", url="https://a.ru", status="error")
    state = _fetch_state(
        consecutive_failures=scheduler.PAUSE_AFTER_FAILURES,
        last_success_at=(now - timedelta(days=days_ago)).isoformat(),
    )

    assert scheduler.should_pause(source, state, now=now) is paused


def test_a_state_without_a_failure_counter_is_not_paused(now):
    source = Source(name="A", url="https://a.ru", status="error")

    assert scheduler.should_pause(source, object(), now=now) is False


def test_the_thresholds_are_the_documented_ones():
    assert (scheduler.PAUSE_AFTER_FAILURES, scheduler.PAUSE_AFTER_DAYS) == (50, 7)


# ── коллектор: расписание растягивается ────────────────────────────────────


def test_every_failure_pushes_the_next_poll_further_out(raw_config, db, now, tmp_path):
    routes = MockRoutes({RSS_URL: (500, b"", {})})
    source = _add(db)
    collector = _collector(_config(raw_config), db, routes, now, tmp_path)

    schedule = []
    for _ in range(3):
        collector.run()
        schedule.append(_schedule(db, source))

    assert schedule == [
        "2026-09-02T14:00:00+00:00",  # 1 неудача — интервал ×2
        "2026-09-02T16:00:00+00:00",  # 2 неудачи — ×4
        "2026-09-02T20:00:00+00:00",  # 3 неудачи — ×8
    ]
    assert db.fetch_state.get(source.id).consecutive_failures == 3


def test_the_first_success_puts_the_source_back_on_its_own_interval(raw_config, db, now, tmp_path):
    routes = MockRoutes({RSS_URL: (500, b"", {})})
    source = _add(db, poll_interval="15m")
    collector = _collector(_config(raw_config), db, routes, now, tmp_path)
    collector.run()
    collector.run()
    assert _schedule(db, source) == "2026-09-02T13:00:00+00:00"  # 15m × 4

    routes[RSS_URL] = (200, GOOD_FEED, RSS)
    collector.run()

    assert _schedule(db, source) == "2026-09-02T12:15:00+00:00"
    assert db.fetch_state.get(source.id).consecutive_failures == 0


def test_a_healthy_source_is_never_slowed_down(raw_config, db, now, tmp_path):
    routes = MockRoutes({RSS_URL: (200, GOOD_FEED, RSS)})
    source = _add(db, poll_interval="6h")

    _collector(_config(raw_config), db, routes, now, tmp_path).run()

    assert _schedule(db, source) == "2026-09-02T18:00:00+00:00"


def test_a_broken_source_keeps_its_place_in_the_rotation_until_it_is_paused(
    raw_config, db, now, tmp_path
):
    """`error` — отметка о здоровье: источник ещё опрашивают, просто реже."""
    routes = MockRoutes({RSS_URL: (500, b"", {})})
    source = _add(db)
    collector = _collector(_config(raw_config), db, routes, now, tmp_path)

    for _ in range(FAILURES_TO_ERROR):
        collector.run()

    assert db.sources.get(source.id).status == "error"
    assert [s.id for s in scheduler.due(db, now=now + timedelta(days=1))] == [source.id]


# ── коллектор: автопауза ───────────────────────────────────────────────────


@pytest.fixture
def exhausted(db, now) -> Source:
    """Источник в `error`, который не отвечает уже полсотни раз и ни разу не отвечал."""
    source = _add(db)
    db.sources.set_status(source.id, "error")
    _state(db, source, consecutive_failures=scheduler.PAUSE_AFTER_FAILURES - 1,
           last_error="HTTP 500", last_success_at=None)
    db.sources.schedule(source.id, "2026-09-02T06:00:00+00:00")
    return db.sources.get(source.id)


def test_an_exhausted_source_is_paused_with_a_reason(raw_config, db, now, tmp_path, exhausted):
    routes = MockRoutes({RSS_URL: (500, b"", {})})

    _collector(_config(raw_config), db, routes, now, tmp_path).run()

    stored = db.sources.get(exhausted.id)
    assert stored.status == "paused"
    assert stored.notes == "автопауза 2026-09-02: 50 неудач подряд"


def test_the_reason_is_appended_to_the_notes_already_there(raw_config, db, now, tmp_path,
                                                           exhausted):
    exhausted.notes = "платный доступ"
    db.sources.update(exhausted)
    routes = MockRoutes({RSS_URL: (500, b"", {})})

    _collector(_config(raw_config), db, routes, now, tmp_path).run()

    assert db.sources.get(exhausted.id).notes == (
        "платный доступ | автопауза 2026-09-02: 50 неудач подряд"
    )


def test_a_paused_source_leaves_the_rotation(raw_config, db, now, tmp_path, exhausted):
    routes = MockRoutes({RSS_URL: (500, b"", {})})
    collector = _collector(_config(raw_config), db, routes, now, tmp_path)

    collector.run()

    assert scheduler.due(db, now=now + timedelta(days=30)) == []
    assert collector.run().per_source == []  # опрашивать больше нечего


def test_the_pause_does_not_move_the_schedule(raw_config, db, now, tmp_path, exhausted):
    """Расписание не трогаем: источник снят с опроса, а не отложен."""
    routes = MockRoutes({RSS_URL: (500, b"", {})})

    _collector(_config(raw_config), db, routes, now, tmp_path).run()

    assert _schedule(db, exhausted) == "2026-09-02T06:00:00+00:00"


def test_resume_brings_the_paused_source_back(raw_config, config, db, now, tmp_path, exhausted):
    routes = MockRoutes({RSS_URL: (500, b"", {})})
    _collector(_config(raw_config), db, routes, now, tmp_path).run()

    restored = SourceService(config, db).resume(exhausted.id)

    assert restored.status == "active"
    assert [s.id for s in scheduler.due(db, now=now)] == [exhausted.id]


def test_the_pause_is_logged_with_the_source_and_the_reason(raw_config, db, now, tmp_path,
                                                            exhausted, caplog):
    routes = MockRoutes({RSS_URL: (500, b"", {})})

    with caplog.at_level("WARNING", logger="collector"):
        _collector(_config(raw_config), db, routes, now, tmp_path).run()

    assert f"источник #{exhausted.id} «Лента»: автопауза" in caplog.text


def test_a_source_that_answered_this_week_is_slowed_down_but_not_paused(
    raw_config, db, now, tmp_path
):
    source = _add(db)
    db.sources.set_status(source.id, "error")
    _state(db, source, consecutive_failures=99,
           last_success_at=(now - timedelta(days=2)).isoformat())
    routes = MockRoutes({RSS_URL: (500, b"", {})})

    _collector(_config(raw_config), db, routes, now, tmp_path).run()

    assert db.sources.get(source.id).status == "error"
    assert _schedule(db, source) == "2026-09-03T04:00:00+00:00"  # 1h × 16


# ── ручное обновление живёт по другим правилам ─────────────────────────────


def test_a_manual_refresh_neither_backs_off_nor_pauses(raw_config, db, now, tmp_path, exhausted):
    """`collect_one` — это «проверь сейчас»: расписание и статус не его дело."""
    routes = MockRoutes({RSS_URL: (500, b"", {})})
    collector = _collector(_config(raw_config), db, routes, now, tmp_path)

    result, entry = collector.collect_one(exhausted)

    assert entry["status"] == "failed"
    assert result.error == "HTTP 500"
    stored = db.sources.get(exhausted.id)
    assert (stored.status, stored.notes) == ("error", "")
    assert stored.next_run_at == "2026-09-02T06:00:00+00:00"


def test_a_manual_refresh_of_a_healthy_source_leaves_the_schedule_alone(raw_config, db, now,
                                                                        tmp_path):
    source = _add(db, poll_interval="15m")
    db.sources.schedule(source.id, "2026-09-02T06:00:00+00:00")
    routes = MockRoutes({RSS_URL: (200, GOOD_FEED, RSS)})
    collector = _collector(_config(raw_config), db, routes, now, tmp_path)

    collector.collect_one(db.sources.get(source.id))

    assert _schedule(db, source) == "2026-09-02T06:00:00+00:00"
