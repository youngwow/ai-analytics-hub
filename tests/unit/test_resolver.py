"""src/sources/resolver.py — how a user-entered URL turns into (kind, fetch_url)."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx
import pytest
from support import HTML_UTF8, RSS, XML, MockRoutes, raising

from src.sources.resolver import _PROBE_PATHS, Resolver
from src.sources.scraper_search import SearchQuery

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
# The lowercase probe list the resolver walks, in its declared order: the generic
# WordPress/Drupal/Bitrix conventions first, then the Russian news-section
# variants. Every `.xml` twin sits right after the extensionless spelling it
# complements — Kommersant answers 403 on /rss/news and serves the feed at
# /rss/news.xml, so the extension variants have to be probed too.
LOWER_PROBE_PATHS = [
    "/rss",
    "/rss/",
    "/feed",
    "/feed/",
    "/rss.xml",
    "/rss/news",
    "/rss/news/",
    "/rss/news.xml",
    "/news/rss",
    "/news/rss/",
    "/news/rss.xml",
    "/rss/all",
]
# nginx and Apache on Linux match paths case-sensitively, so a feed published as
# /RSS/news.xml is invisible to the lowercase probes; each path carrying an `rss`
# segment gets an uppercase twin. /feed and /feed/ have no such segment, so their
# twins collapse into the lowercase entries and the twins number 10, not 12.
UPPER_PROBE_PATHS = [p.replace("rss", "RSS") for p in LOWER_PROBE_PATHS if "rss" in p]
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


# ── non-http schemes ───────────────────────────────────────────────────────


CANONICAL_SEARCH_URL = SearchQuery("закон об ИИ", ["gov.ru"]).to_url()


@pytest.mark.parametrize(
    ("url", "name", "fetch_url"),
    [
        (CANONICAL_SEARCH_URL, "закон об ИИ", CANONICAL_SEARCH_URL),
        (
            "tavily://search?q=%D0%B7%D0%B0%D0%BA%D0%BE%D0%BD",
            "закон",
            "tavily://search?q=%D0%B7%D0%B0%D0%BA%D0%BE%D0%BD&domains=&days=7&topic=news&summary=1",
        ),
        (
            "  tavily://search?q=ai+law&days=3&topic=general  ",
            "ai law",
            "tavily://search?q=ai+law&domains=&days=3&topic=general&summary=1",
        ),
        (
            "TAVILY://SEARCH?q=x",
            "x",
            "tavily://search?q=x&domains=&days=7&topic=news&summary=1",
        ),
        (
            "tavily://search?q=x&domains=WWW.Gov.ru,cbr.ru&summary=no",
            "x",
            "tavily://search?q=x&domains=cbr.ru%2Cgov.ru&days=7&topic=news&summary=0",
        ),
    ],
    ids=["canonical-unchanged", "bare-query", "padded", "upper-case-scheme", "messy-params"],
)
def test_search_urls_resolve_to_a_canonical_search_url_without_network(
    mock_client, url, name, fetch_url
):
    routes = MockRoutes()
    res = _resolver(mock_client, routes).resolve(url)
    assert res.kind == "search"
    assert res.fetch_url == fetch_url
    assert res.fetch_url == SearchQuery.from_url(url).to_url()
    assert res.name == name
    assert res.note == ""
    assert routes.requests == []


def test_equivalent_search_urls_resolve_to_one_fetch_url(mock_client):
    resolver = _resolver(mock_client, MockRoutes())
    spellings = [
        "tavily://search?q=закон+об+ИИ&domains=gov.ru",
        "TAVILY://search?q=%D0%B7%D0%B0%D0%BA%D0%BE%D0%BD+%D0%BE%D0%B1+%D0%98%D0%98&domains=WWW.GOV.RU,&summary=yes",
        " tavily://search?summary=1&topic=news&days=7&domains=gov.ru&q=закон++об+ИИ ",
    ]
    assert {resolver.resolve(u).fetch_url for u in spellings} == {CANONICAL_SEARCH_URL}


def test_search_source_name_is_capped_at_80_chars(mock_client):
    routes = MockRoutes()
    res = _resolver(mock_client, routes).resolve(SearchQuery("ы" * 100).to_url())
    assert res.kind == "search"
    assert res.name == "ы" * 80


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("tavily://search?q=", "search query is empty"),
        ("tavily://search", "search query is empty"),
        ("tavily://search?q=x&days=0", "days must be >= 1"),
        ("tavily://search?q=x&topic=blog", "topic must be one of"),
    ],
    ids=["blank-query", "no-query", "zero-days", "bad-topic"],
)
def test_malformed_search_urls_are_manual_with_a_note(mock_client, url, reason):
    routes = MockRoutes()
    res = _resolver(mock_client, routes).resolve(url)
    assert res.kind == "manual"
    assert res.fetch_url == url
    assert res.note.startswith("bad search url: " + reason)
    assert routes.requests == []


@pytest.mark.parametrize(
    "url",
    ["manual://import", "ftp://files.example.ru/npa.pdf", "file:///tmp/draft.pdf"],
    ids=["manual", "ftp", "file"],
)
def test_other_non_http_schemes_are_manual_without_network(mock_client, url):
    routes = MockRoutes()
    res = _resolver(mock_client, routes).resolve(url)
    scheme = url.split("://", 1)[0]
    assert res.kind == "manual"
    assert res.fetch_url == url
    assert res.name == ""
    assert res.note == f"{scheme}:// is not fetched; kept as manual"
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
    assert feed_probes == ["https://site.ru" + p for p in LOWER_PROBE_PATHS + UPPER_PROBE_PATHS]
    assert len(feed_probes) == 22
    assert len(set(feed_probes)) == len(feed_probes)  # no path is probed twice


def test_uppercase_probe_twins_come_after_every_lowercase_path():
    lowercase = [p for p in _PROBE_PATHS if "RSS" not in p]
    uppercase = [p for p in _PROBE_PATHS if "RSS" in p]

    assert lowercase == LOWER_PROBE_PATHS
    assert uppercase == UPPER_PROBE_PATHS
    # A site that answers on a lowercase path never pays for the twins: the walk
    # stops at the first hit, and no twin is interleaved among the lowercase ones.
    assert list(_PROBE_PATHS) == lowercase + uppercase
    assert len(set(_PROBE_PATHS)) == len(_PROBE_PATHS)


def test_feed_is_found_at_rss_news_xml_when_rss_news_is_forbidden(mock_client, fixture_bytes):
    # Kommersant: /rss/news answers 403 (Qrator), the feed lives at /rss/news.xml.
    routes = MockRoutes(
        {
            "https://kommersant.ru/": (200, HOME_PLAIN, HTML_UTF8),
            "https://kommersant.ru/rss/news": (403, b"<html>Forbidden</html>", HTML_UTF8),
            "https://kommersant.ru/rss/news.xml": (
                200,
                fixture_bytes("rss_kommersant.xml"),
                RSS,
            ),
        }
    )
    res = _resolver(mock_client, routes).resolve("https://kommersant.ru/")
    assert res.kind == "rss"
    assert res.fetch_url == "https://kommersant.ru/rss/news.xml"
    assert res.name == "Коммерсантъ. Лента новостей"
    assert res.note == "feed discovered at https://kommersant.ru/rss/news.xml"
    # The 403 neither ends the walk nor is mistaken for a feed.
    assert "https://kommersant.ru/rss/news" in routes.urls()
    assert routes.urls()[-1] == "https://kommersant.ru/rss/news.xml"


def test_feed_published_at_uppercase_path_is_found_after_all_lowercase_probes(
    mock_client, fixture_bytes
):
    # Case-sensitive nginx: only /RSS/news.xml exists, every lowercase path 404s.
    routes = MockRoutes(
        {
            "https://site.ru/": (200, HOME_PLAIN, HTML_UTF8),
            "https://site.ru/RSS/news.xml": (200, fixture_bytes("rss_government.xml"), RSS),
        }
    )
    res = _resolver(mock_client, routes).resolve("https://site.ru/")
    assert res.kind == "rss"
    assert res.fetch_url == "https://site.ru/RSS/news.xml"
    assert res.name == "Материалы из всех разделов"
    assert res.note == "feed discovered at https://site.ru/RSS/news.xml"

    probed = [urlsplit(u).path for u in routes.urls()[1:]]
    assert "/rss/news.xml" in probed  # the lowercase twin was tried and 404'd
    assert probed[: len(LOWER_PROBE_PATHS)] == LOWER_PROBE_PATHS
    assert probed[len(LOWER_PROBE_PATHS) :] == [
        "/RSS",
        "/RSS/",
        "/RSS.xml",
        "/RSS/news",
        "/RSS/news/",
        "/RSS/news.xml",
    ]
    assert "/RSS/all" not in probed  # the walk stops at the first hit


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
