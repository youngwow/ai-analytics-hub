"""src/sources/collector.py — polling, persistence and dedupe rules over MockTransport."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from support import (
    EMPTY_CHANNEL_PAGE,
    GZIP,
    HTML_CP1251,
    HTML_UTF8,
    JSON,
    RSS,
    XML,
    MockRoutes,
    html_page,
    rss_bytes,
)

from src.common import parse_datetime
from src.config import Config
from src.models import FetchResult, FetchState, RawDocument, Source
from src.paths import ProjectPaths
from src.sources.collector import Collector
from src.sources.scraper_llm import TAVILY_SEARCH_URL
from src.sources.scraper_search import SearchQuery
from src.sources.telegram_mtproto import MtChannel, MtPost

RSS_URL = "https://feed.example.ru/rss.xml"
TG_URL = "https://t.me/s/cit_gov"
SITEMAP_URL = "https://site.ru/sitemap.xml"
CABLEMAN = "https://www.cableman.ru/"
CHANNEL_TITLE = "Цифровые индустриальные технологии"
SEARCH_URL = SearchQuery("GS Labs Триколор").to_url()
GS_GROUP_URL = (
    "https://gs-group.com/press-center/news/"
    "ddr-pyatogo-pokoleniya-lokalizovany-v-rossii-po-novym-trebovaniyam"
)
TELESPUTNIK_URL = (
    "https://telesputnik.ru/materials/trends/news/trikolor-zavershil-testirovanie-cas-dreguard/"
)
FORUM_URL = "https://forum.example.ru/viewforum.php?f=605"
HIT_IDS = [GS_GROUP_URL, TELESPUTNIK_URL, FORUM_URL]
DIGEST_ID = "summary:2026-09-02"

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


def _collector(config, db, routes, now, tmp_path, tavily_key: str = "secret-key",
               **kwargs) -> Collector:
    """A collector over `routes` with a frozen clock; the Tavily key is always explicit."""
    return Collector(
        config,
        ProjectPaths.from_root(str(tmp_path)),
        db,
        transport=routes.transport(),
        now=lambda: now,
        tavily_key=tavily_key,
        **kwargs,
    )


def _search_source(db, **overrides) -> Source:
    base = dict(name="GS Labs Триколор", url=SEARCH_URL, kind="search", fetch_url=SEARCH_URL)
    return _add(db, **{**base, **overrides})


def _tavily_routes(fixture_bytes, extra: dict | None = None) -> MockRoutes:
    return MockRoutes({TAVILY_SEARCH_URL: (200, fixture_bytes("tavily_news.json"), JSON),
                       **(extra or {})})


def _tavily_reply(results: list[dict], answer: str | None = None) -> tuple:
    body = {"results": results, **({"answer": answer} if answer is not None else {})}
    return (200, json.dumps(body, ensure_ascii=False).encode("utf-8"), JSON)


def _tavily_payloads(routes: MockRoutes) -> list[dict]:
    return [json.loads(r.content) for r in routes.requests_to(TAVILY_SEARCH_URL)]


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
    assert db.fetch_state.get(tg.id).cursor == {"last_post_id": 1485, "transport": "web"}
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
    assert db.fetch_state.get(tg.id).cursor == {"last_post_id": 1485, "transport": "web"}
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


def test_paused_sources_are_polled_only_when_named(raw_config, db, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    routes = MockRoutes({RSS_URL: (200, rss_bytes([{"title": "a", "link": "https://example.ru/1"}]), RSS)})
    source = _add(db, status="paused")
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


def test_ok_entries_list_the_inserted_external_ids(raw_config, db, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    feed = rss_bytes([{"title": "a", "link": "https://example.ru/1", "guid": "one"},
                      {"title": "b", "link": "https://example.ru/2", "guid": "two"}])
    routes = MockRoutes({RSS_URL: (200, feed, RSS)})
    _add(db)
    collector = _collector(config, db, routes, now, tmp_path)
    assert collector.run().per_source[0]["new_external_ids"] == ["one", "two"]
    rerun = collector.run().per_source[0]
    assert (rerun["seen"], rerun["new"], rerun["new_external_ids"]) == (2, 0, [])


# ── search sources ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "expected_new"),
    [("search", 1), ("rss", 0), ("sitemap", 0), ("telegram", 0)],
    ids=["search-bypasses-window", "rss-filtered", "sitemap-filtered", "telegram-filtered"],
)
def test_window_filter_is_skipped_only_for_search_sources(raw_config, db, now, tmp_path, kind,
                                                          expected_new):
    config = _config(raw_config, fetch_fulltext=False)
    source = _add(db, kind=kind)
    collector = _collector(config, db, MockRoutes(), now, tmp_path)
    old = RawDocument(source.id, "old", "https://example.ru/old", title="Старая",
                      published_at="2026-08-01T00:00:00+00:00")
    entry = collector._persist(
        source, FetchState(source_id=source.id), FetchResult(documents=[old]), None, now,
        now - timedelta(hours=72), False, 0.0,
    )
    assert (entry["status"], entry["seen"], entry["new"]) == ("ok", 1, expected_new)
    assert db.documents.exists(source.id, "old") is (expected_new == 1)


def test_search_run_stores_hits_older_than_the_collect_window(raw_config, db, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)  # 72h window: since = Aug 30 12:00Z
    old_hit = {"url": "https://example.ru/old-story", "title": "Старая, но по запросу",
               "content": "Текст.", "score": 0.5, "published_date": "Sat, 01 Aug 2026 10:00:00 GMT"}
    routes = MockRoutes({TAVILY_SEARCH_URL: _tavily_reply([old_hit], answer="")})
    source = _search_source(db)
    report = _collector(config, db, routes, now, tmp_path).run()
    assert (report.sources_ok, report.docs_new) == (1, 1)
    assert db.documents.list(source_id=source.id)[0]["published_at"] == "2026-08-01T10:00:00+00:00"
    assert db.fetch_state.get(source.id).cursor == {"since": "2026-09-02", "days": 7}


def test_search_digest_is_stored_once_per_utc_day(raw_config, db, fixture_bytes, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    routes = _tavily_routes(fixture_bytes)
    source = _search_source(db)
    collector = _collector(config, db, routes, now, tmp_path)

    assert collector.run().docs_new == 4
    assert db.documents.exists(source.id, DIGEST_ID)

    collector.now = lambda: now + timedelta(hours=1)
    same_day = collector.run()
    assert (same_day.docs_new, same_day.per_source[0]["seen"]) == (0, 4)
    assert db.documents.count(source.id) == 4

    collector.now = lambda: now + timedelta(days=1)
    next_day = collector.run()
    assert next_day.docs_new == 1
    assert next_day.per_source[0]["new_external_ids"] == ["summary:2026-09-03"]
    assert db.documents.count(source.id) == 5
    rows = db.documents.list(source_id=source.id)
    digests = [r for r in rows if r["external_id"].startswith("summary:")]
    assert {r["external_id"] for r in digests} == {DIGEST_ID, "summary:2026-09-03"}
    assert all(r["url"] == "" for r in digests)
    assert db.fetch_state.get(source.id).cursor == {"since": "2026-09-03", "days": 7}


def test_per_source_cap_keeps_the_digest_of_a_search_batch(raw_config, db, fixture_bytes, now,
                                                            tmp_path):
    config = _config(raw_config, fetch_fulltext=False, max_new_per_source=2)
    routes = _tavily_routes(fixture_bytes)  # 3 hits + digest
    source = _search_source(db)
    _, entry = _collector(config, db, routes, now, tmp_path).collect_one(source)
    assert (entry["seen"], entry["new"]) == (4, 3)
    assert entry["new_external_ids"] == [GS_GROUP_URL, TELESPUTNIK_URL, DIGEST_ID]
    assert set(_stored_ids(db, source.id)) == {GS_GROUP_URL, TELESPUTNIK_URL, DIGEST_ID}
    assert db.documents.exists(source.id, FORUM_URL) is False


def test_per_source_cap_still_cuts_url_bearing_documents(raw_config, db, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False, max_new_per_source=2)
    feed = rss_bytes([{"title": f"Новость {i}", "link": f"https://example.ru/{i}"} for i in range(3)])
    routes = MockRoutes({RSS_URL: (200, feed, RSS)})
    source = _add(db)
    entry = _collector(config, db, routes, now, tmp_path).run().per_source[0]
    assert (entry["seen"], entry["new"]) == (3, 2)
    assert entry["new_external_ids"] == ["https://example.ru/0", "https://example.ru/1"]
    assert db.documents.count(source.id) == 2


def test_search_source_is_never_renamed(raw_config, db, fixture_bytes, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    routes = _tavily_routes(fixture_bytes)
    placeholder = _search_source(db, name=SEARCH_URL)  # would be replaced for html/telegram
    _collector(config, db, routes, now, tmp_path).run()
    assert db.sources.get(placeholder.id).name == SEARCH_URL


def test_search_thin_hits_get_their_text_from_the_page(raw_config, db, fixture_bytes, now,
                                                        tmp_path):
    config = _config(raw_config)  # fetch_fulltext on
    page = html_page("Триколор", f"<p>{PARAGRAPH}</p>", published="2026-08-31T12:30:00+03:00")
    routes = _tavily_routes(fixture_bytes, {TELESPUTNIK_URL: (200, page, HTML_UTF8)})
    source = _search_source(db)
    _collector(config, db, routes, now, tmp_path).collect_one(source)

    rows = {r["external_id"]: r for r in db.documents.list(source_id=source.id)}
    thin = db.documents.get(rows[TELESPUTNIK_URL]["id"])
    assert PARAGRAPH[:60] in thin.text
    assert thin.title == "«Триколор» завершил тестирование CAS DREGUARD — Телеспутник"  # Tavily's
    assert thin.published_at == "2026-08-31T09:30:00+00:00"  # Tavily's date kept
    rich = db.documents.get(rows[GS_GROUP_URL]["id"])
    assert rich.text.startswith("Российский инвестиционно-промышленный холдинг")
    forum = db.documents.get(rows[FORUM_URL]["id"])
    assert forum.text == "Ветка форума о приставках и спутниковом ТВ."  # 404 page: chunks stay
    fetched = routes.urls()
    assert TELESPUTNIK_URL in fetched and FORUM_URL in fetched
    assert GS_GROUP_URL not in fetched


# ── collect_one ────────────────────────────────────────────────────────────


def test_collect_one_polls_a_paused_source_and_records_the_run(raw_config, db, fixture_bytes,
                                                                now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    routes = _tavily_routes(fixture_bytes)
    source = _search_source(db, status="paused")
    result, entry = _collector(config, db, routes, now, tmp_path).collect_one(source)

    assert result.error is None
    assert [d.external_id for d in result.documents] == [*HIT_IDS, DIGEST_ID]
    assert (entry["id"], entry["kind"], entry["status"]) == (source.id, "search", "ok")
    assert (entry["seen"], entry["new"]) == (4, 4)
    assert entry["new_external_ids"] == [*HIT_IDS, DIGEST_ID]
    assert set(_stored_ids(db, source.id)) == {*HIT_IDS, DIGEST_ID}
    assert _tavily_payloads(routes)[0]["days"] == 7
    state = db.fetch_state.get(source.id)
    assert state.cursor == {"since": "2026-09-02", "days": 7}
    assert state.last_success_at == "2026-09-02T12:00:00+00:00"
    assert state.last_doc_count == 4
    run = db.runs.latest()
    assert (run["sources_ok"], run["sources_fail"], run["docs_new"]) == (1, 0, 4)
    assert run["started_at"] == run["finished_at"] == "2026-09-02T12:00:00+00:00"
    assert db.conn.execute("SELECT count(*) FROM collect_runs").fetchone()[0] == 1
    assert db.sources.get(source.id).status == "paused"  # an ad-hoc poll never resumes a source


def test_collect_one_force_drops_the_cursor_and_re_asks_the_full_window(raw_config, db,
                                                                         fixture_bytes, now,
                                                                         tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    routes = _tavily_routes(fixture_bytes)
    source = _search_source(db)
    with db.transaction():
        db.fetch_state.save(FetchState(source_id=source.id, cursor={"since": "2026-09-01", "days": 7},
                                       last_success_at="2026-09-01T12:00:00+00:00"))
    collector = _collector(config, db, routes, now, tmp_path)
    collector.collect_one(source)
    collector.collect_one(source, force=True)
    incremental, forced = _tavily_payloads(routes)
    assert incremental["start_date"] == "2026-08-31"
    assert "days" not in incremental
    assert forced["days"] == 7
    assert "start_date" not in forced
    assert db.fetch_state.get(source.id).cursor == {"since": "2026-09-02", "days": 7}


def test_collect_one_returns_known_hits_but_lists_only_new_ids(raw_config, db, fixture_bytes, now,
                                                                tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    routes = _tavily_routes(fixture_bytes)
    source = _search_source(db)
    collector = _collector(config, db, routes, now, tmp_path)
    collector.collect_one(source)

    collector.now = lambda: now + timedelta(hours=1)
    result, entry = collector.collect_one(source)
    assert [d.external_id for d in result.documents] == [*HIT_IDS, DIGEST_ID]
    assert (entry["status"], entry["seen"], entry["new"]) == ("ok", 4, 0)
    assert entry["new_external_ids"] == []
    assert db.documents.count(source.id) == 4
    assert db.runs.latest()["docs_new"] == 0
    assert db.conn.execute("SELECT count(*) FROM collect_runs").fetchone()[0] == 2


def test_collect_one_rejects_a_source_that_is_not_stored(raw_config, db, now, tmp_path):
    config = _config(raw_config)
    collector = _collector(config, db, MockRoutes(), now, tmp_path)
    with pytest.raises(ValueError, match="must be stored"):
        collector.collect_one(Source(name="x", url=SEARCH_URL, kind="search", fetch_url=SEARCH_URL))
    assert db.runs.latest() is None


def test_collect_one_without_an_adapter_records_a_failure(raw_config, db, now, tmp_path):
    config = _config(raw_config)
    manual = db.sources.ensure_manual()
    routes = MockRoutes()
    result, entry = _collector(config, db, routes, now, tmp_path).collect_one(manual)
    assert result.error == entry["error"] == "no adapter for kind 'manual'"
    assert entry["status"] == "failed"
    assert db.runs.latest()["sources_fail"] == 1
    assert db.fetch_state.get(manual.id).consecutive_failures == 1
    assert routes.requests == []


def test_collect_one_without_tavily_key_fails_per_run_without_http(raw_config, db, fixture_bytes,
                                                                    now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    routes = _tavily_routes(fixture_bytes)
    source = _search_source(db)
    result, entry = _collector(config, db, routes, now, tmp_path, tavily_key="").collect_one(source)
    assert entry["status"] == "failed"
    assert "TAVILY_API" in entry["error"]
    assert result.documents == []
    assert routes.requests == []
    assert db.fetch_state.get(source.id).last_error == entry["error"]
    assert db.runs.latest()["sources_fail"] == 1


# ── telegram over MTProto ──────────────────────────────────────────────────

TG_POST_DATE = datetime(2026, 9, 2, 10, 0, 0, tzinfo=timezone.utc)


class FakeMtprotoReader:
    """The collector's view of `MtprotoReader`: canned posts, and it must be closed."""

    def __init__(self, channel: MtChannel):
        self.channel = channel
        self.calls: list[dict] = []
        self.closed = 0

    def channel_posts(self, channel, *, min_id=0, limit=100, offset_date=None) -> MtChannel:
        self.calls.append(
            {"channel": channel, "min_id": min_id, "limit": limit, "offset_date": offset_date}
        )
        return self.channel

    def close(self) -> None:
        self.closed += 1


class RecordingAdapter:
    """A `close()`-able adapter that never touches the network."""

    def __init__(self, kind: str = "rss", close_error: Exception | None = None):
        self.kind = kind
        self.close_error = close_error
        self.closed = 0

    def fetch(self, source, state, client, *, now, since=None, backfill=False) -> FetchResult:
        return FetchResult()

    def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


def _tg_source(db) -> Source:
    return _add(db, name="cit_gov", url="https://t.me/cit_gov", kind="telegram",
                category="telegram", fetch_url=TG_URL)


def _tg_reader() -> FakeMtprotoReader:
    return FakeMtprotoReader(
        MtChannel(
            title=CHANNEL_TITLE,
            posts=[
                MtPost(id=1490, text="Первый пост\n\nПодробности", date=TG_POST_DATE),
                MtPost(id=1491, text="Второй пост", date=TG_POST_DATE,
                       urls=["https://gov.ru/doc"], files=["Приказ.pdf"]),
                MtPost(id=1492, date=TG_POST_DATE),  # a photo: no document, but it moves the cursor
            ],
        )
    )


def test_explicit_mtproto_factory_reaches_the_telegram_adapter(raw_config, db, now, tmp_path):
    config = _config(raw_config)
    reader = _tg_reader()

    def factory():
        return reader

    collector = _collector(config, db, MockRoutes(), now, tmp_path, mtproto_factory=factory)
    assert collector.adapters["telegram"].mtproto_factory is factory


@pytest.mark.integration
def test_telegram_run_over_mtproto_stores_documents_and_the_cursor(raw_config, db, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    reader = _tg_reader()
    routes = MockRoutes()
    source = _tg_source(db)
    collector = _collector(config, db, routes, now, tmp_path, mtproto_factory=lambda: reader)

    report = collector.run()
    assert (report.sources_ok, report.sources_fail, report.docs_new) == (1, 0, 2)
    assert routes.requests == []  # nothing fell back to the t.me/s/ preview
    assert set(_stored_ids(db, source.id)) == {"cit_gov/1490", "cit_gov/1491"}
    assert db.fetch_state.get(source.id).cursor == {"last_post_id": 1492, "transport": "mtproto"}
    assert db.sources.get(source.id).name == CHANNEL_TITLE  # placeholder replaced
    rows = {r["external_id"]: r for r in db.documents.list(source_id=source.id)}
    assert rows["cit_gov/1490"]["title"] == "Первый пост"
    assert rows["cit_gov/1490"]["url"] == "https://t.me/cit_gov/1490"
    assert rows["cit_gov/1490"]["published_at"] == "2026-09-02T10:00:00+00:00"
    assert rows["cit_gov/1491"]["attachments"] == '["file:Приказ.pdf", "https://gov.ru/doc"]'
    assert reader.closed == 1
    assert reader.calls[0] == {"channel": "cit_gov", "min_id": 0, "limit": 50,
                               "offset_date": now - timedelta(hours=72)}


def test_the_stored_cursor_bounds_the_next_mtproto_run(raw_config, db, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    reader = _tg_reader()
    _tg_source(db)
    collector = _collector(config, db, MockRoutes(), now, tmp_path, mtproto_factory=lambda: reader)
    collector.run()

    collector.now = lambda: now + timedelta(hours=1)
    report = collector.run()
    assert report.docs_new == 0
    assert reader.calls[1]["min_id"] == 1492  # the cursor survived the JSON round-trip
    assert reader.calls[1]["offset_date"] is None
    assert reader.closed == 2  # one connection per run


def test_run_closes_every_adapter_at_the_end(raw_config, db, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    _add(db)
    collector = _collector(config, db, MockRoutes(), now, tmp_path)
    adapter = RecordingAdapter()
    collector.adapters["rss"] = adapter
    report = collector.run()
    assert report.sources_ok == 1
    assert adapter.closed == 1


def test_collect_one_closes_every_adapter_at_the_end(raw_config, db, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    source = _add(db)
    collector = _collector(config, db, MockRoutes(), now, tmp_path)
    adapter = RecordingAdapter()
    collector.adapters["rss"] = adapter
    _, entry = collector.collect_one(source)
    assert entry["status"] == "ok"
    assert adapter.closed == 1


def test_a_failing_close_does_not_fail_the_run(raw_config, db, now, tmp_path):
    config = _config(raw_config, fetch_fulltext=False)
    _add(db)
    collector = _collector(config, db, MockRoutes(), now, tmp_path)
    collector.adapters["rss"] = RecordingAdapter(close_error=RuntimeError("socket already gone"))
    report = collector.run()
    assert (report.sources_ok, report.sources_fail) == (1, 0)
    assert db.runs.latest()["sources_ok"] == 1


# The suite must never build a real Telethon reader, even on a machine that has
# Telegram credentials exported: a tmp project root has no session file.


def test_autoload_builds_no_factory_without_a_session_file(raw_config, db, now, tmp_path,
                                                           monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "1234567")
    monkeypatch.setenv("TELEGRAM_API_HASH", "0123456789abcdef")
    config = _config(raw_config)
    assert config.telegram.mtproto == "auto"  # the autoload branch really is taken
    collector = _collector(config, db, MockRoutes(), now, tmp_path)
    assert collector.adapters["telegram"].mtproto_factory is None


def test_autoload_builds_a_factory_when_a_session_file_exists(raw_config, db, now, tmp_path,
                                                              monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "1234567")
    monkeypatch.setenv("TELEGRAM_API_HASH", "0123456789abcdef")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "telegram.session").write_bytes(b"")
    config = _config(raw_config)
    collector = _collector(config, db, MockRoutes(), now, tmp_path)
    assert collector.adapters["telegram"].mtproto_factory is not None  # never called here


@pytest.mark.parametrize(
    "env", [{}, {"TELEGRAM_API_ID": "1234567"}, {"TELEGRAM_API_HASH": "hash"}],
    ids=["no-credentials", "id-only", "hash-only"],
)
def test_autoload_builds_no_factory_without_both_credentials(raw_config, db, now, tmp_path,
                                                             monkeypatch, env):
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "telegram.session").write_bytes(b"")
    config = _config(raw_config)
    collector = _collector(config, db, MockRoutes(), now, tmp_path)
    assert collector.adapters["telegram"].mtproto_factory is None


def test_autoload_is_skipped_when_mtproto_is_off(raw_config, db, now, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "1234567")
    monkeypatch.setenv("TELEGRAM_API_HASH", "0123456789abcdef")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "telegram.session").write_bytes(b"")
    raw_config["telegram"]["mtproto"] = "off"
    config = _config(raw_config)
    collector = _collector(config, db, MockRoutes(), now, tmp_path)
    assert collector.adapters["telegram"].mtproto_factory is None
