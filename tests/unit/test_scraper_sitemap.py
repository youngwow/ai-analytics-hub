"""src/sources/scraper_sitemap.py — sitemap parsing and lastmod-cursor selection."""

from __future__ import annotations

import gzip
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from support import GZIP, XML, MockRoutes, raising

from src.common import MOSCOW
from src.models import FetchState, Source
from src.sources.scraper_sitemap import SitemapAdapter, SitemapEntry, has_lastmod, parse_sitemap

UTC = timezone.utc
PLUS3 = timezone(timedelta(hours=3))
SITEMAP_URL = "https://site.ru/sitemap.xml"
NEWS_GZ = "https://site.ru/sitemap-news.xml.gz"
OLD_CHILD = "https://site.ru/sitemap-2024.xml"


def _source(**overrides) -> Source:
    base = dict(id=5, name="Сайт", url="https://site.ru/", kind="sitemap", fetch_url=SITEMAP_URL)
    return Source(**{**base, **overrides})


def _adapter(config) -> SitemapAdapter:
    return SitemapAdapter(config.scraper, config.sitemap)


def _urlset(entries: list[str]) -> bytes:
    body = "".join(f"<url>{e}</url>" for e in entries)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'
    ).encode()


# ── parse_sitemap ──────────────────────────────────────────────────────────


def test_parse_sitemap_index(fixture_bytes):
    kind, entries = parse_sitemap(fixture_bytes("sitemap_index.xml"))
    assert kind == "index"
    assert entries == [
        SitemapEntry(OLD_CHILD, datetime(2024, 12, 31, tzinfo=MOSCOW)),
        SitemapEntry(NEWS_GZ, datetime(2026, 9, 2, 10, 0, tzinfo=PLUS3)),
        SitemapEntry("https://other.ru/sitemap.xml", None),
    ]


def test_parse_sitemap_urlset_reads_lastmod_and_news_publication_date(fixture_bytes):
    kind, entries = parse_sitemap(fixture_bytes("sitemap_urlset.xml"))
    assert kind == "urlset"
    assert entries == [
        SitemapEntry("https://site.ru/news/1", datetime(2026, 9, 2, 9, 0, tzinfo=PLUS3)),
        SitemapEntry("https://site.ru/news/2", datetime(2026, 9, 1, tzinfo=MOSCOW)),
        SitemapEntry("https://site.ru/news/old", datetime(2026, 1, 1, tzinfo=MOSCOW)),
        SitemapEntry("https://site.ru/about", None),
        SitemapEntry("https://other.ru/leak", datetime(2026, 9, 2, tzinfo=MOSCOW)),
    ]


def test_parse_sitemap_gzip_matches_plain(fixture_bytes):
    assert parse_sitemap(fixture_bytes("sitemap_news.xml.gz")) == parse_sitemap(
        fixture_bytes("sitemap_urlset.xml")
    )


def test_parse_sitemap_tolerates_bom_and_leading_whitespace(fixture_bytes):
    body = b"\xef\xbb\xbf\r\n  " + fixture_bytes("sitemap_urlset.xml")
    kind, entries = parse_sitemap(body)
    assert kind == "urlset"
    assert len(entries) == 5


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not xml",
        b"<?xml version='1.0'?><rss><channel/></rss>",
        b"\x1f\x8b truncated gzip",
        gzip.compress(b"<html></html>"),
    ],
    ids=["empty", "text", "rss", "bad-gzip", "gzipped-html"],
)
def test_parse_sitemap_rejects_junk(body):
    assert parse_sitemap(body) == ("", [])


def test_parse_sitemap_url_without_loc_is_dropped():
    body = _urlset(["<lastmod>2026-09-02</lastmod>", "<loc>https://site.ru/a</loc>"])
    assert parse_sitemap(body) == ("urlset", [SitemapEntry("https://site.ru/a", None)])


def test_parse_sitemap_first_parseable_lastmod_wins():
    body = _urlset(
        [
            "<loc>https://site.ru/a</loc><loc>https://site.ru/ignored</loc>"
            "<lastmod>bad</lastmod><lastmod>2026-09-02</lastmod><lastmod>2020-01-01</lastmod>"
        ]
    )
    kind, entries = parse_sitemap(body)
    assert kind == "urlset"
    assert entries == [SitemapEntry("https://site.ru/a", datetime(2026, 9, 2, tzinfo=MOSCOW))]


def test_has_lastmod():
    assert has_lastmod([SitemapEntry("a"), SitemapEntry("b", datetime(2026, 1, 1, tzinfo=UTC))])
    assert not has_lastmod([SitemapEntry("a"), SitemapEntry("b")])
    assert not has_lastmod([])


# ── SitemapAdapter.fetch: index handling ───────────────────────────────────


def test_index_follows_only_fresh_same_host_children(config, mock_client, fixture_bytes, now):
    routes = MockRoutes(
        {
            SITEMAP_URL: (200, fixture_bytes("sitemap_index.xml"), XML),
            NEWS_GZ: (200, fixture_bytes("sitemap_news.xml.gz"), GZIP),
            OLD_CHILD: (200, _urlset(["<loc>https://site.ru/2024</loc>"]), XML),
        }
    )
    since = now - timedelta(hours=72)
    result = _adapter(config).fetch(
        _source(), FetchState(source_id=5), mock_client(routes), now=now, since=since
    )
    assert result.error is None
    assert routes.urls() == [SITEMAP_URL, NEWS_GZ]
    assert "https://other.ru/sitemap.xml" not in routes.urls()
    assert [d.url for d in result.documents] == [
        "https://site.ru/news/1",
        "https://site.ru/news/2",
        "https://site.ru/about",
    ]


def test_index_without_lower_bound_fetches_children_newest_first_up_to_cap(
    raw_config, mock_client, fixture_bytes, now
):
    from src.config import Config

    raw_config["sitemap"]["max_sitemaps"] = 1
    config = Config.from_dict(raw_config)
    routes = MockRoutes(
        {
            SITEMAP_URL: (200, fixture_bytes("sitemap_index.xml"), XML),
            NEWS_GZ: (200, fixture_bytes("sitemap_news.xml.gz"), GZIP),
            OLD_CHILD: (200, _urlset(["<loc>https://site.ru/2024</loc>"]), XML),
        }
    )
    result = _adapter(config).fetch(
        _source(), FetchState(source_id=5), mock_client(routes), now=now, backfill=True
    )
    assert routes.urls() == [SITEMAP_URL, NEWS_GZ]
    assert len(result.documents) == 4


def test_index_skips_nested_index_and_broken_children(config, mock_client, fixture_bytes, now):
    routes = MockRoutes(
        {
            SITEMAP_URL: (200, fixture_bytes("sitemap_index.xml"), XML),
            NEWS_GZ: (200, fixture_bytes("sitemap_index.xml"), XML),  # nested index
            OLD_CHILD: (500, b"", {}),
        }
    )
    result = _adapter(config).fetch(
        _source(), FetchState(source_id=5), mock_client(routes), now=now, backfill=True
    )
    assert result.error is None
    assert result.documents == []
    assert routes.urls() == [SITEMAP_URL, NEWS_GZ, OLD_CHILD]


# ── SitemapAdapter.fetch: selection ────────────────────────────────────────


def test_first_run_keeps_window_and_undated_drops_old_and_off_host(config, mock_client, fixture_bytes, now):
    routes = MockRoutes({SITEMAP_URL: (200, fixture_bytes("sitemap_urlset.xml"), XML)})
    since = now - timedelta(hours=72)
    result = _adapter(config).fetch(
        _source(), FetchState(source_id=5), mock_client(routes), now=now, since=since
    )
    assert result.error is None
    docs = result.documents
    assert [d.url for d in docs] == [
        "https://site.ru/news/1",
        "https://site.ru/news/2",
        "https://site.ru/about",
    ]
    assert [d.external_id for d in docs] == [d.url for d in docs]
    assert [d.published_at for d in docs] == [
        "2026-09-02T06:00:00+00:00",
        "2026-08-31T21:00:00+00:00",
        None,
    ]
    assert all(d.needs_fulltext for d in docs)
    assert all(d.source_id == 5 for d in docs)
    assert all(d.fetched_at == "2026-09-02T12:00:00+00:00" for d in docs)
    assert all(d.title == "" and d.text == "" for d in docs)
    assert result.state_update == {"cursor": {"lastmod": "2026-09-02T06:00:00+00:00"}}


def test_incremental_run_keeps_only_entries_at_or_after_cursor(config, mock_client, fixture_bytes, now):
    routes = MockRoutes({SITEMAP_URL: (200, fixture_bytes("sitemap_urlset.xml"), XML)})
    state = FetchState(
        source_id=5,
        last_success_at="2026-09-01T00:00:00+00:00",
        cursor={"lastmod": "2026-09-01T00:00:00+00:00"},
    )
    result = _adapter(config).fetch(
        _source(), state, mock_client(routes), now=now, since=now - timedelta(days=30)
    )
    assert [d.url for d in result.documents] == ["https://site.ru/news/1"]
    assert result.state_update == {"cursor": {"lastmod": "2026-09-02T06:00:00+00:00"}}


def test_incremental_run_treats_cursor_equality_as_new(config, mock_client, fixture_bytes, now):
    routes = MockRoutes({SITEMAP_URL: (200, fixture_bytes("sitemap_urlset.xml"), XML)})
    state = FetchState(
        source_id=5, last_success_at="x", cursor={"lastmod": "2026-09-02T06:00:00+00:00"}
    )
    result = _adapter(config).fetch(_source(), state, mock_client(routes), now=now)
    assert [d.url for d in result.documents] == ["https://site.ru/news/1"]
    assert result.state_update == {"cursor": {"lastmod": "2026-09-02T06:00:00+00:00"}}


def test_cursor_only_moves_forward(config, mock_client, fixture_bytes, now):
    routes = MockRoutes({SITEMAP_URL: (200, fixture_bytes("sitemap_urlset.xml"), XML)})
    state = FetchState(
        source_id=5,
        last_success_at="x",
        cursor={"lastmod": "2026-09-03T00:00:00+00:00", "other": 1},
    )
    result = _adapter(config).fetch(_source(), state, mock_client(routes), now=now)
    assert result.documents == []
    assert result.state_update == {"cursor": {"lastmod": "2026-09-03T00:00:00+00:00", "other": 1}}


def test_backfill_keeps_everything_on_host(config, mock_client, fixture_bytes, now):
    routes = MockRoutes({SITEMAP_URL: (200, fixture_bytes("sitemap_urlset.xml"), XML)})
    state = FetchState(source_id=5, last_success_at="x", cursor={"lastmod": "2026-09-02T06:00:00+00:00"})
    result = _adapter(config).fetch(
        _source(), state, mock_client(routes), now=now, since=None, backfill=True
    )
    assert [d.url for d in result.documents] == [
        "https://site.ru/news/1",
        "https://site.ru/news/2",
        "https://site.ru/news/old",
        "https://site.ru/about",
    ]


def test_first_run_without_since_keeps_all_dated_and_undated(config, mock_client, fixture_bytes, now):
    routes = MockRoutes({SITEMAP_URL: (200, fixture_bytes("sitemap_urlset.xml"), XML)})
    result = _adapter(config).fetch(_source(), FetchState(source_id=5), mock_client(routes), now=now)
    assert len(result.documents) == 4


def test_www_prefix_does_not_break_same_host_check(config, mock_client, fixture_bytes, now):
    url = "https://www.site.ru/sitemap.xml"
    routes = MockRoutes({url: (200, fixture_bytes("sitemap_urlset.xml"), XML)})
    result = _adapter(config).fetch(
        _source(fetch_url=url), FetchState(source_id=5), mock_client(routes), now=now, backfill=True
    )
    assert len(result.documents) == 4


def test_max_urls_caps_newest_first(raw_config, mock_client, fixture_bytes, now):
    from src.config import Config

    raw_config["sitemap"]["max_urls"] = 2
    config = Config.from_dict(raw_config)
    routes = MockRoutes({SITEMAP_URL: (200, fixture_bytes("sitemap_urlset.xml"), XML)})
    result = _adapter(config).fetch(
        _source(), FetchState(source_id=5), mock_client(routes), now=now, backfill=True
    )
    assert [d.url for d in result.documents] == ["https://site.ru/news/1", "https://site.ru/news/2"]


@pytest.mark.parametrize(
    ("reply", "expected_error"),
    [
        ((404, b"", {}), "HTTP 404"),
        ((200, b"<html><body>nope</body></html>", XML), "not a sitemap"),
        (raising(httpx.ConnectError("down")), "request error: ConnectError: down"),
    ],
    ids=["404", "not-a-sitemap", "connect-error"],
)
def test_fetch_errors(config, mock_client, now, reply, expected_error):
    routes = MockRoutes({SITEMAP_URL: reply})
    result = _adapter(config).fetch(_source(), FetchState(source_id=5), mock_client(routes), now=now)
    assert result.error == expected_error
    assert result.documents == []
    assert result.state_update == {}
