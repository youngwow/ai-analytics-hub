"""src/sources/collector.py — история опросов и расписание.

`fetch_state` помнит только последнее состояние, поэтому «источник молчит» и
«источник сломался неделю назад» по нему неразличимы. Историю ведёт `source_runs`
(US-4), а очередь опроса — расписание источника.
"""

from __future__ import annotations

import pytest
from support import RSS, MockRoutes, raising, rss_bytes

from src.models import Source
from src.paths import ProjectPaths
from src.sources import scheduler
from src.sources.collector import Collector, _error_code

RSS_URL = "https://example.ru/rss"
ITEMS = [
    {
        "title": "Минцифры внесло законопроект",
        "link": "https://example.ru/news/1",
        "pubDate": "Tue, 02 Sep 2026 09:00:00 +0300",
    }
]


def _source(db, **overrides) -> Source:
    base = dict(name="Лента", url="https://example.ru/", kind="rss", fetch_url=RSS_URL)
    return db.sources.add(Source(**{**base, **overrides}))


def _collector(config, db, routes, now, tmp_path) -> Collector:
    return Collector(
        config,
        ProjectPaths.from_root(str(tmp_path)),
        db,
        transport=routes.transport(),
        now=lambda: now,
        tavily_key="",
    )


@pytest.mark.parametrize(
    "message, code",
    [
        ("timeout", "timeout"),
        ("read timed out", "timeout"),
        ("HTTP 503", "http_error"),
        ("HTTP 404", "http_error"),
        ("web preview unavailable for this channel", "telegram_preview_unavailable"),
        ("connect failed", "network_unreachable"),
        ("certificate verify failed", "network_unreachable"),
        ("no adapter for kind 'x'", "parse_error"),
        ("что-то неизвестное", "error"),
    ],
)
def test_error_messages_map_to_machine_codes(message, code):
    assert _error_code(message) == code


def test_a_successful_poll_is_recorded(config, db, now, tmp_path):
    source = _source(db)
    routes = MockRoutes({RSS_URL: (200, rss_bytes(ITEMS), RSS)})

    _collector(config, db, routes, now, tmp_path).run()

    runs = db.source_runs.history(source.id)
    assert len(runs) == 1
    assert (runs[0].items_found, runs[0].items_new, runs[0].error_code) == (1, 1, "")
    assert runs[0].finished_at


def test_a_failed_poll_keeps_the_reason(config, db, now, tmp_path):
    source = _source(db)
    routes = MockRoutes({RSS_URL: raising(TimeoutError("read timed out"))})

    _collector(config, db, routes, now, tmp_path).run()

    runs = db.source_runs.history(source.id)
    assert runs[0].error_code == "timeout"
    assert runs[0].error_message


def test_history_keeps_the_newest_first(config, db, now, tmp_path):
    source = _source(db)
    routes = MockRoutes({RSS_URL: (200, rss_bytes(ITEMS), RSS)})
    collector = _collector(config, db, routes, now, tmp_path)
    collector.run()
    _collector(config, db, routes, now, tmp_path).run()

    runs = db.source_runs.history(source.id, limit=5)

    assert len(runs) == 2
    assert runs[0].started_at >= runs[1].started_at


def test_a_poll_moves_the_source_to_its_next_slot(config, db, now, tmp_path):
    source = _source(db, poll_interval="15m")
    routes = MockRoutes({RSS_URL: (200, rss_bytes(ITEMS), RSS)})

    _collector(config, db, routes, now, tmp_path).run()

    assert db.sources.get(source.id).next_run_at == scheduler.next_run_at("15m", now=now)


def test_due_only_skips_a_source_whose_turn_has_not_come(config, db, now, tmp_path):
    source = _source(db, poll_interval="24h")
    db.sources.schedule(source.id, "2099-01-01T00:00:00+00:00")
    routes = MockRoutes({RSS_URL: (200, rss_bytes(ITEMS), RSS)})

    report = _collector(config, db, routes, now, tmp_path).run(due_only=True)

    assert report.sources_ok == 0
    assert db.source_runs.history(source.id) == []


def test_due_only_polls_a_source_that_is_overdue(config, db, now, tmp_path):
    source = _source(db)
    db.sources.schedule(source.id, "2020-01-01T00:00:00+00:00")
    routes = MockRoutes({RSS_URL: (200, rss_bytes(ITEMS), RSS)})

    report = _collector(config, db, routes, now, tmp_path).run(due_only=True)

    assert report.sources_ok == 1
    assert len(db.source_runs.history(source.id)) == 1


def test_a_paused_source_is_never_due(config, db, now, tmp_path):
    source = _source(db, status="paused")
    routes = MockRoutes({RSS_URL: (200, rss_bytes(ITEMS), RSS)})

    report = _collector(config, db, routes, now, tmp_path).run(due_only=True)

    assert report.sources_ok == 0
    assert db.source_runs.history(source.id) == []


def test_five_failures_in_a_row_mark_the_source_broken(config, db, now, tmp_path):
    source = _source(db)
    routes = MockRoutes({RSS_URL: raising(TimeoutError("read timed out"))})

    for _ in range(4):
        _collector(config, db, routes, now, tmp_path).run()
        assert db.sources.get(source.id).status == "active"

    _collector(config, db, routes, now, tmp_path).run()

    assert db.sources.get(source.id).status == "error"


def test_a_broken_source_heals_itself_on_the_next_success(config, db, now, tmp_path):
    source = _source(db)
    failing = MockRoutes({RSS_URL: raising(TimeoutError("read timed out"))})
    for _ in range(5):
        _collector(config, db, failing, now, tmp_path).run()
    assert db.sources.get(source.id).status == "error"

    working = MockRoutes({RSS_URL: (200, rss_bytes(ITEMS), RSS)})
    _collector(config, db, working, now, tmp_path).run()

    assert db.sources.get(source.id).status == "active"
    assert db.fetch_state.get(source.id).consecutive_failures == 0
