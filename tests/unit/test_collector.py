"""src/sources/collector.py — polling, persistence and dedupe rules over MockTransport."""

from __future__ import annotations

from datetime import timedelta

import pytest
from support import (
    EMPTY_CHANNEL_PAGE,
    GZIP,
    HTML_CP1251,
    HTML_UTF8,
    RSS,
    XML,
    MockRoutes,
    html_page,
    rss_bytes,
)

from src.common import parse_datetime
from src.config import Config
from src.models import FetchResult, FetchState, Source
from src.paths import ProjectPaths
from src.sources.collector import Collector

RSS_URL = "https://feed.example.ru/rss.xml"
TG_URL = "https://t.me/s/cit_gov"
SITEMAP_URL = "https://site.ru/sitemap.xml"
CABLEMAN = "https://www.cableman.ru/"
CHANNEL_TITLE = "Цифровые индустриальные технологии"

PARAGRAPH = (
    "Министерство цифрового развития опубликовало проект правил, регулирующих использование "
    "SIM-карт в межмашинных системах связи, включая требования к идентификации устройств. "
    "Операторы связи должны будут вести реестр таких карт и передавать сведения в единую систему."
)


def _list_page(links: list[tuple[str, str]]) -> bytes:
    anchors = "".join(f'<a href="{href}">{text}</a>' for href, text in links)
    return (
        "<!DOCTYPE html><html><head><title>Тестовый сайт</title></head>"
        f"<body><main>{anchors}</main></body></html>"
    ).encode()


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
    )


def _config(raw_config, **scraper) -> Config:
    raw_config["scraper"].update(scraper)
    return Config.from_dict(raw_config)


def _stored_ids(db, source_id) -> list[str]:
    return [r["external_id"] for r in db.documents.list(source_id=source_id, limit=1000)]


# ── the whole pipeline ─────────────────────────────────────────────────────


@pytest.mark.integration
def test_first_and_second_run_over_every_adapter(raw_config, db, fixture_bytes, now, tmp_path):
    config = _config(raw_config, date_window_hours=400)  # window reaches the Aug 18 TG posts
    article = fixture_bytes("article_cp1251.html")
    routes = MockRoutes(
        {
            RSS_URL: (
                200,
                fixture_bytes("rss_yandex_fulltext.xml"),
                {**RSS, "etag": 'W/"abc"', "last-modified": "Tue, 02 Sep 2026 10:00:00 GMT"},
            ),
            "https://example.ru/news/126": (200, article, HTML_CP1251),
            TG_URL: (200, fixture_bytes("tg_channel.html"), HTML_UTF8),
            SITEMAP_URL: (200, fixture_bytes("sitemap_index.xml"), XML),
            "https://site.ru/sitemap-news.xml.gz": (200, fixture_bytes("sitemap_news.xml.gz"), GZIP),
            "https://site.ru/news/1": (200, article, HTML_CP1251),
            "https://site.ru/news/2": (200, article, HTML_CP1251),
            "https://site.ru/about": (200, article, HTML_CP1251),
            CABLEMAN: (200, fixture_bytes("list_page_cableman.html"), HTML_UTF8),
            "https://www.cableman.ru/*": (200, article, HTML_CP1251),
        }
    )
    rss = _add(db, name="Отраслевые новости")
    tg = _add(db, name="cit_gov", url="https://t.me/cit_gov", kind="telegram",
              category="telegram", fetch_url=TG_URL)
    sitemap = _add(db, name="Сайт", url="https://site.ru/", kind="sitemap", fetch_url=SITEMAP_URL)
    html = _add(db, name="", url=CABLEMAN, kind="html", fetch_url=CABLEMAN)
    collector = _collector(config, db, routes, now, tmp_path)

    # ── first run ──
    report = collector.run()
    assert (report.sources_ok, report.sources_fail, report.sources_not_modified) == (4, 0, 0)
    assert report.docs_new == 49 == sum(e["new"] for e in report.per_source)
    assert report.started_at == report.finished_at == "2026-09-02T12:00:00+00:00"
    by_id = {e["id"]: e for e in report.per_source}
    assert (by_id[rss.id]["new"], by_id[rss.id]["seen"]) == (4, 4)
    assert (by_id[tg.id]["new"], by_id[tg.id]["seen"]) == (4, 4)
    assert (by_id[sitemap.id]["new"], by_id[sitemap.id]["seen"]) == (3, 3)
    assert (by_id[html.id]["new"], by_id[html.id]["seen"]) == (38, 38)
    assert all(e["status"] == "ok" for e in report.per_source)

    # rss: full text came from the feed for 3 items and from the page for the 4th
    assert _stored_ids(db, rss.id) == [
        "news-123",
        "https://example.ru/news/124",
        "https://example.ru/news/125",
        "https://example.ru/news/126",
    ]
    rows = {r["external_id"]: r for r in db.documents.list(source_id=rss.id)}
    enriched = db.documents.get(rows["https://example.ru/news/126"]["id"])
    assert enriched.title == "Только анонс — нужен полный текст"  # feed title kept
    assert enriched.author == "Иван Петров"  # filled from the page
    assert enriched.published_at == "2026-09-02T04:00:00+00:00"  # feed date kept
    assert PARAGRAPH[:60] in enriched.text
    assert enriched.raw_html is None
    assert all(len(r["content_hash"]) == 64 for r in rows.values())
    assert rows["news-123"]["attachments"] == '["https://example.ru/files/proekt.pdf"]'
    rss_state = db.fetch_state.get(rss.id)
    assert rss_state.etag == 'W/"abc"'
    assert rss_state.last_modified == "Tue, 02 Sep 2026 10:00:00 GMT"
    assert rss_state.last_success_at == "2026-09-02T12:00:00+00:00"
    assert rss_state.last_fetch_at == "2026-09-02T12:00:00+00:00"
    assert rss_state.last_doc_count == 4
    assert rss_state.consecutive_failures == 0
    assert rss_state.last_error is None
    assert routes.urls().count("https://example.ru/news/126") == 1
    assert "https://example.ru/news/123" not in routes.urls()

    # telegram: cursor stored, placeholder name replaced by the channel title
    assert _stored_ids(db, tg.id) == ["cit_gov/1485", "cit_gov/1484", "cit_gov/1483", "cit_gov/1482"]
    assert db.fetch_state.get(tg.id).cursor == {"last_post_id": 1485}
    assert db.sources.get(tg.id).name == CHANNEL_TITLE

    # sitemap: only the fresh child was fetched; old entry never touched
    assert "https://site.ru/sitemap-2024.xml" not in routes.urls()
    assert "https://other.ru/sitemap.xml" not in routes.urls()
    assert "https://site.ru/news/old" not in routes.urls()
    assert set(_stored_ids(db, sitemap.id)) == {
        "https://site.ru/news/1",
        "https://site.ru/news/2",
        "https://site.ru/about",
    }
    sm_rows = {r["external_id"]: r for r in db.documents.list(source_id=sitemap.id)}
    assert sm_rows["https://site.ru/news/1"]["published_at"] == "2026-09-02T06:00:00+00:00"
    assert sm_rows["https://site.ru/about"]["published_at"] == "2026-09-02T09:30:00+00:00"  # page
    assert sm_rows["https://site.ru/news/1"]["title"] == (
        "Минцифры предложило правила управления M2M SIM-картами"
    )
    assert db.fetch_state.get(sitemap.id).cursor == {"lastmod": "2026-09-02T06:00:00+00:00"}
    assert db.sources.get(sitemap.id).name == "Сайт"

    # html: every candidate seen, all 38 dated by their page and stored
    assert db.documents.count(html.id) == 38
    assert db.sources.get(html.id).name == "Кабельщик"
    assert len(db.seen_urls.known(html.id, [r["url"] for r in db.documents.list(html.id, 100)])) == 38

    run_row = db.runs.latest()
    assert (run_row["sources_ok"], run_row["docs_new"]) == (4, 49)
    assert run_row["started_at"] == "2026-09-02T12:00:00+00:00"

    # ── second run: validators and cursors do their job ──
    first_run_requests = len(routes.requests)

    def conditional(request):
        if request.headers.get("if-none-match") == 'W/"abc"':
            return (304, b"", {})
        return (200, fixture_bytes("rss_yandex_fulltext.xml"), RSS)

    routes[RSS_URL] = conditional
    routes[f"{TG_URL}?after=1485"] = (200, EMPTY_CHANNEL_PAGE, HTML_UTF8)
    later = now + timedelta(hours=1)
    collector.now = lambda: later
    report2 = collector.run()

    assert report2.sources_not_modified == 1
    assert report2.sources_ok == 3
    assert report2.sources_fail == 0
    assert report2.docs_new == 0
    assert db.documents.count() == 49
    second = routes.requests[first_run_requests:]
    rss_request = next(r for r in second if str(r.url) == RSS_URL)
    assert rss_request.headers["if-none-match"] == 'W/"abc"'
    assert rss_request.headers["if-modified-since"] == "Tue, 02 Sep 2026 10:00:00 GMT"
    assert [str(r.url) for r in second if "t.me" in str(r.url)] == [f"{TG_URL}?after=1485"]
    assert "https://site.ru/news/1" not in [str(r.url) for r in second]  # already stored
    assert not any("cableman.ru/content" in str(r.url) for r in second)  # all seen
    assert db.fetch_state.get(rss.id).last_success_at == "2026-09-02T13:00:00+00:00"
    assert db.fetch_state.get(rss.id).etag == 'W/"abc"'
    assert db.fetch_state.get(tg.id).cursor == {"last_post_id": 1485}
    assert db.runs.latest()["sources_not_modified"] == 1
    assert db.conn.execute("SELECT count(*) FROM collect_runs").fetchone()[0] == 2


# ── failures ───────────────────────────────────────────────────────────────


def test_failed_source_is_recorded_and_the_run_continues(raw_config, db, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    routes = MockRoutes(
        {
            RSS_URL: (500, b"", {}),
            "https://ok.ru/rss": (200, rss_bytes([{"title": "ок", "link": "https://ok.ru/1"}]), RSS),
        }
    )
    bad = _add(db)
    good = _add(db, name="Хорошая", url="https://ok.ru/", fetch_url="https://ok.ru/rss")
    collector = _collector(config, db, routes, now, tmp_path)

    report = collector.run()
    assert (report.sources_ok, report.sources_fail) == (1, 1)
    assert report.docs_new == 1
    failed = next(e for e in report.per_source if e["id"] == bad.id)
    assert failed["status"] == "failed"
    assert failed["error"] == "HTTP 500"
    state = db.fetch_state.get(bad.id)
    assert state.last_error == "HTTP 500"
    assert state.consecutive_failures == 1
    assert state.last_success_at is None
    assert state.last_fetch_at == "2026-09-02T12:00:00+00:00"
    assert db.documents.count(good.id) == 1

    collector.run()
    assert db.fetch_state.get(bad.id).consecutive_failures == 2
    assert db.runs.latest()["sources_fail"] == 1


def test_success_after_failure_clears_error_and_counter(raw_config, db, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    routes = MockRoutes({RSS_URL: (503, b"", {})})
    source = _add(db)
    collector = _collector(config, db, routes, now, tmp_path)
    collector.run()
    assert db.fetch_state.get(source.id).consecutive_failures == 1

    routes[RSS_URL] = (200, rss_bytes([{"title": "a", "link": "https://example.ru/1"}]), RSS)
    collector.run()
    state = db.fetch_state.get(source.id)
    assert state.consecutive_failures == 0
    assert state.last_error is None
    assert state.last_success_at == "2026-09-02T12:00:00+00:00"


def test_adapter_crash_is_a_failure_not_an_exception(raw_config, db, now, tmp_path):
    config = _config(raw_config)
    routes = MockRoutes()
    source = _add(db)
    collector = _collector(config, db, routes, now, tmp_path)

    class Boom:
        kind = "rss"

        def fetch(self, *args, **kwargs):
            raise RuntimeError("parser exploded")

    collector.adapters["rss"] = Boom()
    report = collector.run()
    assert report.sources_fail == 1
    assert report.per_source[0]["error"] == "RuntimeError: parser exploded"
    assert db.fetch_state.get(source.id).last_error == "RuntimeError: parser exploded"


# ── window / dates / caps ──────────────────────────────────────────────────


def test_window_drops_old_documents_and_keeps_undated(raw_config, db, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)  # 72h window: since = Aug 30 12:00Z
    feed = rss_bytes(
        [
            {"title": "Старая", "link": "https://example.ru/old",
             "pubDate": "Sat, 01 Aug 2026 10:00:00 +0300"},
            {"title": "Без даты", "link": "https://example.ru/undated"},
            {"title": "На границе", "link": "https://example.ru/edge",
             "pubDate": "Sun, 30 Aug 2026 15:00:00 +0300"},
            {"title": "Свежая", "link": "https://example.ru/fresh",
             "pubDate": "Wed, 02 Sep 2026 10:00:00 +0300"},
        ]
    )
    routes = MockRoutes({RSS_URL: (200, feed, RSS)})
    source = _add(db)
    report = _collector(config, db, routes, now, tmp_path).run()
    entry = report.per_source[0]
    assert (entry["seen"], entry["new"]) == (4, 3)
    assert set(_stored_ids(db, source.id)) == {
        "https://example.ru/undated",
        "https://example.ru/edge",
        "https://example.ru/fresh",
    }


def test_backfill_ignores_the_window(raw_config, db, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    feed = rss_bytes(
        [
            {"title": "Старая", "link": "https://example.ru/old",
             "pubDate": "Sat, 01 Aug 2026 10:00:00 +0300"},
            {"title": "Свежая", "link": "https://example.ru/fresh",
             "pubDate": "Wed, 02 Sep 2026 10:00:00 +0300"},
        ]
    )
    routes = MockRoutes({RSS_URL: (200, feed, RSS)})
    source = _add(db)
    report = _collector(config, db, routes, now, tmp_path).run(backfill=True)
    assert report.docs_new == 2
    assert db.documents.count(source.id) == 2


def test_future_dates_are_clamped_to_now(raw_config, db, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    feed = rss_bytes(
        [{"title": "Из будущего", "link": "https://example.ru/future",
          "pubDate": "Thu, 03 Sep 2026 10:00:00 +0300"}]
    )
    routes = MockRoutes({RSS_URL: (200, feed, RSS)})
    source = _add(db)
    _collector(config, db, routes, now, tmp_path).run()
    row = db.documents.list(source_id=source.id)[0]
    assert row["published_at"] == "2026-09-02T12:00:00+00:00"


def test_max_new_per_source_caps_inserts(raw_config, db, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False, max_new_per_source=3)
    feed = rss_bytes(
        [{"title": f"Новость {i}", "link": f"https://example.ru/{i}"} for i in range(6)]
    )
    routes = MockRoutes({RSS_URL: (200, feed, RSS)})
    source = _add(db)
    report = _collector(config, db, routes, now, tmp_path).run()
    entry = report.per_source[0]
    assert (entry["seen"], entry["new"]) == (6, 3)
    assert set(_stored_ids(db, source.id)) == {f"https://example.ru/{i}" for i in range(3)}


def test_documents_without_title_text_or_summary_are_dropped(raw_config, db, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    feed = rss_bytes([{"title": "", "link": "https://example.ru/blank"},
                      {"title": "Есть заголовок", "link": "https://example.ru/titled"}])
    routes = MockRoutes({RSS_URL: (200, feed, RSS)})
    source = _add(db)
    _collector(config, db, routes, now, tmp_path).run()
    assert _stored_ids(db, source.id) == ["https://example.ru/titled"]


# ── dedupe ─────────────────────────────────────────────────────────────────


def test_url_already_stored_under_another_source_is_skipped(raw_config, db, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    shared = "https://example.ru/news/shared"
    routes = MockRoutes(
        {
            RSS_URL: (200, rss_bytes([{"title": "A", "link": shared, "guid": "a-shared"}]), RSS),
            "https://b.ru/rss": (
                200,
                rss_bytes(
                    [
                        {"title": "B same", "link": shared, "guid": "b-shared"},
                        {"title": "B other", "link": "https://example.ru/news/other",
                         "guid": "b-other"},
                    ]
                ),
                RSS,
            ),
        }
    )
    a = _add(db, name="A")
    b = _add(db, name="B", url="https://b.ru/", fetch_url="https://b.ru/rss")
    collector = _collector(config, db, routes, now, tmp_path)
    collector.run(source_ids=[a.id])
    report = collector.run(source_ids=[b.id])
    assert report.docs_new == 1
    assert _stored_ids(db, b.id) == ["b-other"]
    assert db.documents.exists(b.id, "b-shared") is False


def test_source_landing_url_and_bare_hosts_are_not_deduped_by_url(raw_config, db, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    landing = "https://example.ru/landing"
    routes = MockRoutes(
        {
            RSS_URL: (
                200,
                rss_bytes(
                    [
                        {"title": "A landing", "link": landing, "guid": "a-landing"},
                        {"title": "A home", "link": "https://example.ru/", "guid": "a-home"},
                    ]
                ),
                RSS,
            ),
            "https://c.ru/rss": (
                200,
                rss_bytes(
                    [
                        {"title": "C landing", "link": landing, "guid": "c-landing"},
                        {"title": "C home", "link": "https://example.ru/", "guid": "c-home"},
                    ]
                ),
                RSS,
            ),
        }
    )
    a = _add(db, name="A")
    c = _add(db, name="C", url=landing, fetch_url="https://c.ru/rss")
    collector = _collector(config, db, routes, now, tmp_path)
    collector.run(source_ids=[a.id])
    collector.run(source_ids=[c.id])
    assert set(_stored_ids(db, c.id)) == {"c-landing", "c-home"}


def test_rerun_does_not_duplicate_documents_by_external_id(raw_config, db, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    feed = rss_bytes([{"title": "a", "link": "https://example.ru/1", "guid": "one"}])
    routes = MockRoutes({RSS_URL: (200, feed, RSS)})
    source = _add(db)
    collector = _collector(config, db, routes, now, tmp_path)
    collector.run()
    report = collector.run()
    assert report.docs_new == 0
    assert report.per_source[0]["seen"] == 1
    assert db.documents.count(source.id) == 1


# ── source selection / force ───────────────────────────────────────────────


def test_disabled_sources_are_polled_only_when_named(raw_config, db, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    routes = MockRoutes({RSS_URL: (200, rss_bytes([{"title": "a", "link": "https://example.ru/1"}]), RSS)})
    source = _add(db, enabled=False)
    collector = _collector(config, db, routes, now, tmp_path)

    report = collector.run()
    assert report.per_source == []
    assert routes.requests == []
    assert db.runs.latest()["sources_ok"] == 0

    report = collector.run(source_ids=[source.id])
    assert [e["id"] for e in report.per_source] == [source.id]
    assert report.docs_new == 1


def test_manual_sources_are_never_polled(raw_config, db, now, tmp_path):
    config = _config(raw_config)
    db.sources.ensure_manual()
    report = _collector(config, db, MockRoutes(), now, tmp_path).run()
    assert report.per_source == []


def test_unknown_source_ids_are_ignored(raw_config, db, now, tmp_path):
    config = _config(raw_config)
    _add(db)
    report = _collector(config, db, MockRoutes(), now, tmp_path).run(source_ids=[999])
    assert report.per_source == []


def test_force_resets_validators_before_polling(raw_config, db, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    feed = rss_bytes([{"title": "a", "link": "https://example.ru/1"}])

    def conditional(request):
        if request.headers.get("if-none-match") == 'W/"abc"':
            return (304, b"", {})
        return (200, feed, {**RSS, "etag": 'W/"abc"'})

    routes = MockRoutes({RSS_URL: conditional})
    source = _add(db)
    with db.transaction():
        db.fetch_state.save(
            FetchState(source_id=source.id, etag='W/"abc"', last_success_at="2026-09-01T00:00:00+00:00")
        )
    collector = _collector(config, db, routes, now, tmp_path)

    report = collector.run()
    assert report.sources_not_modified == 1
    assert routes.requests[-1].headers.get("if-none-match") == 'W/"abc"'

    report = collector.run(force=True)
    assert "if-none-match" not in routes.requests[-1].headers
    assert report.docs_new == 1
    assert db.fetch_state.get(source.id).etag == 'W/"abc"'


# ── html-specific rules ────────────────────────────────────────────────────


def test_html_first_run_keeps_only_pages_dated_inside_the_window(raw_config, db, now, tmp_path):
    config = _config(raw_config)
    base = "https://www.site.ru/"
    dated = "https://www.site.ru/content/dated-story-about-sim-cards"
    undated = "https://www.site.ru/content/undated-story-about-something"
    routes = MockRoutes(
        {
            base: (200, _list_page([(dated, "Датированная новость с заголовком"),
                                    (undated, "Недатированная новость с заголовком")]), HTML_UTF8),
            dated: (200, html_page("Датированная", f"<p>{PARAGRAPH}</p>",
                                   published="2026-09-01T18:00:00+03:00"), HTML_UTF8),
            undated: (200, html_page("Недатированная", f"<p>{PARAGRAPH}</p>"), HTML_UTF8),
        }
    )
    source = _add(db, name="Сайт", url=base, kind="html", fetch_url=base)
    report = _collector(config, db, routes, now, tmp_path).run()

    entry = report.per_source[0]
    assert (entry["status"], entry["seen"], entry["new"]) == ("ok", 2, 1)
    assert _stored_ids(db, source.id) == [dated]
    row = db.documents.list(source_id=source.id)[0]
    assert row["title"] == "Датированная"
    assert row["published_at"] == "2026-09-01T15:00:00+00:00"
    assert now - timedelta(hours=72) <= parse_datetime(row["published_at"]) <= now
    assert db.seen_urls.known(source.id, [dated, undated]) == {dated, undated}


def test_html_later_runs_ingest_only_unseen_links_without_date_filter(raw_config, db, now, tmp_path):
    config = _config(raw_config)
    base = "https://www.site.ru/"
    first = "https://www.site.ru/content/first-story-about-networks"
    second = "https://www.site.ru/content/second-story-about-networks"
    routes = MockRoutes(
        {
            base: (200, _list_page([(first, "Первая новость с длинным заголовком")]), HTML_UTF8),
            first: (200, html_page("Первая", f"<p>{PARAGRAPH}</p>",
                                   published="2026-09-02T09:00:00+03:00"), HTML_UTF8),
            second: (200, html_page("Вторая", f"<p>{PARAGRAPH}</p>"), HTML_UTF8),
        }
    )
    source = _add(db, name="Сайт", url=base, kind="html", fetch_url=base)
    collector = _collector(config, db, routes, now, tmp_path)
    collector.run()
    assert _stored_ids(db, source.id) == [first]

    routes[base] = (200, _list_page([(first, "Первая новость с длинным заголовком"),
                                     (second, "Вторая новость с длинным заголовком")]), HTML_UTF8)
    before = len(routes.requests)
    report = collector.run()
    assert report.docs_new == 1
    assert [str(r.url) for r in routes.requests[before:]] == [base, second]
    assert set(_stored_ids(db, source.id)) == {first, second}


def test_placeholder_source_name_is_replaced_by_page_title(raw_config, db, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    base = "https://www.site.ru/"
    routes = MockRoutes({base: (200, _list_page([]), HTML_UTF8)})
    placeholder = _add(db, name=base, url=base, kind="html", fetch_url=base)
    named = _add(db, name="Своё имя", url="https://www.site.ru/news", kind="html",
                 fetch_url="https://www.site.ru/news")
    routes["https://www.site.ru/news"] = (200, _list_page([]), HTML_UTF8)
    _collector(config, db, routes, now, tmp_path).run()
    assert db.sources.get(placeholder.id).name == "Тестовый сайт"
    assert db.sources.get(named.id).name == "Своё имя"


# ── import_url ─────────────────────────────────────────────────────────────


def test_import_url_creates_manual_source_and_dedupes(raw_config, db, fixture_bytes, now, tmp_path):
    config = _config(raw_config)
    url = "https://www.cableman.ru/content/m2m-sim"
    routes = MockRoutes({url: (200, fixture_bytes("article_cp1251.html"), HTML_CP1251)})
    collector = _collector(config, db, routes, now, tmp_path)

    doc_id, created = collector.import_url(url)
    assert created is True
    manual = db.sources.get_by_fetch_url("manual://import")
    assert manual is not None and manual.kind == "manual"
    doc = db.documents.get(doc_id)
    assert doc.source_id == manual.id
    assert doc.external_id == url
    assert doc.title == "Минцифры предложило правила управления M2M SIM-картами"
    assert doc.author == "Иван Петров"
    assert PARAGRAPH[:60] in doc.text
    assert doc.published_at == "2026-09-02T09:30:00+00:00"
    assert doc.fetched_at == "2026-09-02T12:00:00+00:00"
    assert len(doc.content_hash) == 64

    assert collector.import_url(url) == (doc_id, False)
    assert db.documents.count() == 1
    assert routes.urls() == [url]


def test_import_url_raises_when_nothing_readable(raw_config, db, now, tmp_path):
    config = _config(raw_config)
    routes = MockRoutes({"https://example.ru/404": (404, b"", {}),
                         "https://example.ru/empty": (200, b"<html><body></body></html>", HTML_UTF8)})
    collector = _collector(config, db, routes, now, tmp_path)
    for url in ("https://example.ru/404", "https://example.ru/empty"):
        with pytest.raises(ValueError, match="could not extract anything readable"):
            collector.import_url(url)
    assert db.documents.count() == 0
    assert db.sources.get_by_fetch_url("manual://import") is not None


# ── helpers exercised directly ─────────────────────────────────────────────


def test_window_filter_and_finalize_helpers(now):
    from src.models import RawDocument

    since = now - timedelta(hours=72)
    docs = [
        RawDocument(1, "old", "u", published_at="2026-08-01T00:00:00+00:00"),
        RawDocument(1, "undated", "u"),
        RawDocument(1, "fresh", "u", published_at="2026-09-02T10:00:00+00:00"),
        RawDocument(1, "junk-date", "u", published_at="вчера"),
    ]
    kept = Collector._window_filter(docs, since)
    assert [d.external_id for d in kept] == ["undated", "fresh", "junk-date"]
    assert Collector._window_filter(docs, None) == docs

    future = RawDocument(1, "f", "u", title="t", text="x", published_at="2026-09-03T00:00:00+00:00",
                         needs_fulltext=True)
    Collector._finalize(future, now)
    assert future.published_at == "2026-09-02T12:00:00+00:00"
    assert future.needs_fulltext is False
    assert len(future.content_hash) == 64


def test_fetch_result_error_short_circuits_persistence(raw_config, db, now, tmp_path):
    config = _config(raw_config)
    source = _add(db)
    collector = _collector(config, db, MockRoutes(), now, tmp_path)
    state = FetchState(source_id=source.id)
    entry = collector._persist(
        source, state, FetchResult(error="boom"), None, now, None, False, 12.6
    )
    assert entry == {
        "id": source.id, "name": "Лента", "kind": "rss", "latency_ms": 13, "new": 0, "seen": 0,
        "status": "failed", "error": "boom",
    }
    assert db.fetch_state.get(source.id).last_error == "boom"
