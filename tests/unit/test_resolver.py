"""src/sources/resolver.py — how a user-entered URL turns into (kind, fetch_url)."""

from __future__ import annotations

import httpx
import pytest
from support import HTML_UTF8, RSS, XML, MockRoutes, raising

from src.sources.resolver import Resolver

HOME_WITH_FEED = (
    b"<!DOCTYPE html><html><head><title>  Site   News </title>"
    b'<link rel="alternate" type="application/rss+xml" title="RSS" href="/rss/">'
    b"</head><body><p>hello</p></body></html>"
)
HOME_PLAIN = (
    b"<!DOCTYPE html><html><head><title>Plain Site</title></head>"
    b"<body><p>hello</p></body></html>"
)
ROBOTS_WITH_SITEMAP = b"User-agent: *\nDisallow: /admin\nSitemap: https://site.ru/sitemap-news.xml\n"
URLSET_NO_LASTMOD = (
    b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    b"<url><loc>https://site.ru/a</loc></url><url><loc>https://site.ru/b</loc></url></urlset>"
)


def _resolver(mock_client, routes: MockRoutes) -> Resolver:
    return Resolver(mock_client(routes), probe_timeout=1.5)


# ── telegram shortcuts ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://t.me/cit_gov",
        "https://t.me/s/cit_gov",
        "https://t.me/s/cit_gov/123",
        "https://t.me/cit_gov/123",
        "http://telegram.me/cit_gov",
        "https://www.t.me/cit_gov/",
        "t.me/cit_gov",
        "https://t.me/cit_gov?utm_source=x",
    ],
)
def test_telegram_urls_resolve_without_network(mock_client, url):
    routes = MockRoutes()
    res = _resolver(mock_client, routes).resolve(url)
    assert res.kind == "telegram"
    assert res.fetch_url == "https://t.me/s/cit_gov"
    assert res.name == "cit_gov"
    assert res.note == ""
    assert routes.requests == []


@pytest.mark.parametrize(
    "url",
    ["https://t.me/+AbCdEf123", "https://t.me/joinchat/xyz", "https://t.me/c/1234567/89", "t.me/+abc"],
)
def test_private_telegram_links_are_manual(mock_client, url):
    routes = MockRoutes()
    res = _resolver(mock_client, routes).resolve(url)
    assert res.kind == "manual"
    assert res.note == "private/invite Telegram links have no web preview"
    assert res.fetch_url.startswith("https://t.me/")
    assert routes.requests == []


# ── feeds ──────────────────────────────────────────────────────────────────


def test_feed_looking_url_that_parses_is_rss(mock_client, fixture_bytes):
    routes = MockRoutes({"https://site.ru/rss/news.xml": (200, fixture_bytes("rss_kommersant.xml"), RSS)})
    res = _resolver(mock_client, routes).resolve("https://site.ru/rss/news.xml")
    assert res.kind == "rss"
    assert res.fetch_url == "https://site.ru/rss/news.xml"
    assert res.name == "Коммерсантъ. Лента новостей"
    assert routes.urls() == ["https://site.ru/rss/news.xml"]


def test_feed_url_follows_redirect_and_keeps_final_url(mock_client, fixture_bytes):
    routes = MockRoutes(
        {
            "http://site.ru/rss": (301, b"", {"location": "https://site.ru/rss/"}),
            "https://site.ru/rss/": (200, fixture_bytes("atom_sample.xml"), XML),
        }
    )
    res = _resolver(mock_client, routes).resolve("http://site.ru/rss")
    assert res.kind == "rss"
    assert res.fetch_url == "https://site.ru/rss/"
    assert res.name == "Пример Atom"


def test_homepage_advertising_a_feed_resolves_to_discovered_url(mock_client, fixture_bytes):
    routes = MockRoutes(
        {
            "https://site.ru/": (200, HOME_WITH_FEED, HTML_UTF8),
            "https://site.ru/rss/": (200, fixture_bytes("rss_telesputnik.xml"), RSS),
        }
    )
    res = _resolver(mock_client, routes).resolve("https://site.ru/")
    assert res.kind == "rss"
    assert res.fetch_url == "https://site.ru/rss/"
    assert res.name == "Телеспутник"
    assert res.note == "feed discovered at https://site.ru/rss/"
    assert routes.urls()[0] == "https://site.ru/"
    assert "https://site.ru/rss/" in routes.urls()


def test_discovered_feed_without_title_falls_back_to_page_title(mock_client):
    untitled = (
        b'<?xml version="1.0"?><rss version="2.0"><channel>'
        b"<item><title>x</title><link>https://site.ru/x</link></item></channel></rss>"
    )
    routes = MockRoutes(
        {
            "https://site.ru/": (200, HOME_WITH_FEED, HTML_UTF8),
            "https://site.ru/rss/": (200, untitled, RSS),
        }
    )
    res = _resolver(mock_client, routes).resolve("https://site.ru/")
    assert res.kind == "rss"
    assert res.name == "Site News"


def test_probed_feed_path_is_found_without_a_link_tag(mock_client, fixture_bytes):
    routes = MockRoutes(
        {
            "https://site.ru/": (200, HOME_PLAIN, HTML_UTF8),
            "https://site.ru/rss": (200, fixture_bytes("rss_government.xml"), RSS),
        }
    )
    res = _resolver(mock_client, routes).resolve("https://site.ru/")
    assert res.kind == "rss"
    assert res.fetch_url == "https://site.ru/rss"
    assert res.name == "Материалы из всех разделов"
    assert res.note == "feed discovered at https://site.ru/rss"


def test_generic_probe_path_wins_over_news_flavoured_one(mock_client, fixture_bytes):
    routes = MockRoutes(
        {
            "https://site.ru/": (200, HOME_PLAIN, HTML_UTF8),
            "https://site.ru/rss": (200, fixture_bytes("rss_kommersant.xml"), RSS),
            "https://site.ru/rss/news": (200, fixture_bytes("rss_government.xml"), RSS),
        }
    )
    res = _resolver(mock_client, routes).resolve("https://site.ru/")
    assert res.kind == "rss"
    assert res.fetch_url == "https://site.ru/rss"
    assert res.name == "Коммерсантъ. Лента новостей"
    assert routes.urls() == ["https://site.ru/", "https://site.ru/rss"]


def test_advertised_feed_beats_every_probe(mock_client, fixture_bytes):
    home = (
        b"<!DOCTYPE html><html><head><title>Site</title>"
        b'<link rel="alternate" type="application/rss+xml" href="/podcast/rss.xml">'
        b"</head><body><p>hello</p></body></html>"
    )
    routes = MockRoutes(
        {
            "https://site.ru/": (200, home, HTML_UTF8),
            "https://site.ru/podcast/rss.xml": (200, fixture_bytes("rss_telesputnik.xml"), RSS),
            "https://site.ru/rss": (200, fixture_bytes("rss_kommersant.xml"), RSS),
        }
    )
    res = _resolver(mock_client, routes).resolve("https://site.ru/")
    assert res.fetch_url == "https://site.ru/podcast/rss.xml"
    assert res.note == "feed discovered at https://site.ru/podcast/rss.xml"
    assert routes.urls() == ["https://site.ru/", "https://site.ru/podcast/rss.xml"]


def test_news_flavoured_advertised_feed_is_tried_before_other_advertised_feeds(
    mock_client, fixture_bytes
):
    home = (
        b"<!DOCTYPE html><html><head><title>Site</title>"
        b'<link rel="alternate" type="application/rss+xml" href="/podcast/rss.xml">'
        b'<link rel="alternate" type="application/rss+xml" href="/news/rss.xml">'
        b"</head><body><p>hello</p></body></html>"
    )
    routes = MockRoutes(
        {
            "https://site.ru/": (200, home, HTML_UTF8),
            "https://site.ru/podcast/rss.xml": (200, fixture_bytes("rss_telesputnik.xml"), RSS),
            "https://site.ru/news/rss.xml": (200, fixture_bytes("rss_government.xml"), RSS),
            "https://site.ru/rss": (200, fixture_bytes("rss_kommersant.xml"), RSS),
        }
    )
    res = _resolver(mock_client, routes).resolve("https://site.ru/")
    assert res.fetch_url == "https://site.ru/news/rss.xml"
    assert res.name == "Материалы из всех разделов"
    assert routes.urls() == ["https://site.ru/", "https://site.ru/news/rss.xml"]


def test_every_probe_path_is_tried_in_generic_first_order(mock_client, fixture_bytes):
    routes = MockRoutes(
        {
            "https://site.ru/": (200, HOME_PLAIN, HTML_UTF8),
            "https://site.ru/feed/": (200, fixture_bytes("rss_government.xml"), RSS),
        }
    )
    res = _resolver(mock_client, routes).resolve("https://site.ru/")
    assert res.kind == "rss"
    assert res.fetch_url == "https://site.ru/feed/"
    assert routes.urls() == [
        "https://site.ru/",
        "https://site.ru/rss",
        "https://site.ru/rss/",
        "https://site.ru/feed",
        "https://site.ru/feed/",
    ]

    everything_404 = MockRoutes({"https://site.ru/": (200, HOME_PLAIN, HTML_UTF8)})
    _resolver(mock_client, everything_404).resolve("https://site.ru/")
    feed_probes = [u for u in everything_404.urls()[1:] if "robots" not in u and "sitemap" not in u]
    assert feed_probes == [
        "https://site.ru/rss",
        "https://site.ru/rss/",
        "https://site.ru/feed",
        "https://site.ru/feed/",
        "https://site.ru/rss.xml",
        "https://site.ru/rss/news",
        "https://site.ru/rss/news/",
        "https://site.ru/news/rss",
        "https://site.ru/news/rss/",
        "https://site.ru/rss/all",
    ]


def test_feed_with_no_entries_is_not_accepted(mock_client):
    empty = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Пусто</title></channel></rss>'
    ).encode("utf-8")
    routes = MockRoutes({"https://site.ru/rss.xml": (200, empty, RSS)})
    res = _resolver(mock_client, routes).resolve("https://site.ru/rss.xml")
    assert res.kind == "html"
    assert res.fetch_url == "https://site.ru/rss.xml"


# ── sitemaps / html fallback ───────────────────────────────────────────────


def test_robots_sitemap_with_lastmod_resolves_to_sitemap(mock_client, fixture_bytes):
    routes = MockRoutes(
        {
            "https://site.ru/": (200, HOME_PLAIN, HTML_UTF8),
            "https://site.ru/robots.txt": (200, ROBOTS_WITH_SITEMAP, {"content-type": "text/plain"}),
            "https://site.ru/sitemap-news.xml": (200, fixture_bytes("sitemap_urlset.xml"), XML),
        }
    )
    res = _resolver(mock_client, routes).resolve("https://site.ru/")
    assert res.kind == "sitemap"
    assert res.fetch_url == "https://site.ru/sitemap-news.xml"
    assert res.name == "Plain Site"
    assert res.note == "no feed; polling sitemap by lastmod"
    assert "https://site.ru/sitemap.xml" not in routes.urls()


def test_sitemap_index_at_default_path_resolves_to_sitemap(mock_client, fixture_bytes):
    routes = MockRoutes(
        {
            "https://site.ru/": (200, HOME_PLAIN, HTML_UTF8),
            "https://site.ru/sitemap.xml": (200, fixture_bytes("sitemap_index.xml"), XML),
        }
    )
    res = _resolver(mock_client, routes).resolve("https://site.ru/")
    assert res.kind == "sitemap"
    assert res.fetch_url == "https://site.ru/sitemap.xml"


def test_sitemap_without_lastmod_falls_back_to_html(mock_client):
    routes = MockRoutes(
        {
            "https://site.ru/": (200, HOME_PLAIN, HTML_UTF8),
            "https://site.ru/sitemap.xml": (200, URLSET_NO_LASTMOD, XML),
        }
    )
    res = _resolver(mock_client, routes).resolve("https://site.ru/")
    assert res.kind == "html"
    assert res.fetch_url == "https://site.ru/"
    assert res.name == "Plain Site"
    assert res.note == "no feed or dated sitemap; diffing page links"


def test_page_with_nothing_discoverable_is_html(mock_client):
    routes = MockRoutes({"https://site.ru/news": (200, HOME_PLAIN, HTML_UTF8)})
    res = _resolver(mock_client, routes).resolve("https://site.ru/news")
    assert res.kind == "html"
    assert res.fetch_url == "https://site.ru/news"
    probed = set(routes.urls())
    assert "https://site.ru/robots.txt" in probed
    assert "https://site.ru/sitemap.xml" in probed
    assert "https://site.ru/rss" in probed
    assert all(r.extensions["timeout"]["read"] == 1.5 for r in routes.requests[1:])


def test_probes_use_short_timeout_but_main_fetch_uses_client_timeout(mock_client):
    routes = MockRoutes({"https://site.ru/": (200, HOME_PLAIN, HTML_UTF8)})
    resolver = Resolver(mock_client(routes, timeout=20.0), probe_timeout=2.0)
    resolver.resolve("https://site.ru/")
    assert routes.requests[0].extensions["timeout"]["read"] == 20.0
    assert routes.requests[1].extensions["timeout"]["read"] == 2.0


# ── unreachable ────────────────────────────────────────────────────────────


def test_unreachable_host_is_html_with_note(mock_client):
    routes = MockRoutes({"https://dead.ru/": raising(httpx.ConnectError("refused"))})
    res = _resolver(mock_client, routes).resolve("https://dead.ru/")
    assert res.kind == "html"
    assert res.fetch_url == "https://dead.ru/"
    assert res.note.startswith("unreachable now")
    assert "ConnectError" in res.note
    assert len(routes.requests) == 1


def test_http_error_is_html_with_status_in_note(mock_client):
    routes = MockRoutes({"https://site.ru/": (503, b"", {})})
    res = _resolver(mock_client, routes).resolve("https://site.ru/")
    assert res.kind == "html"
    assert res.note == "unreachable now: HTTP 503"


def test_unreachable_feedish_url_is_fetched_only_once(mock_client):
    routes = MockRoutes({"https://site.ru/rss.xml": raising(httpx.ReadTimeout("slow"))})
    res = _resolver(mock_client, routes).resolve("https://site.ru/rss.xml")
    assert res.kind == "html"
    assert res.note == "unreachable now: timeout"
    assert routes.urls() == ["https://site.ru/rss.xml"]


def test_bare_domain_gets_https_scheme(mock_client):
    routes = MockRoutes()
    res = _resolver(mock_client, routes).resolve("  site.ru  ")
    assert res.fetch_url == "https://site.ru"
    assert routes.urls()[0] == "https://site.ru"
