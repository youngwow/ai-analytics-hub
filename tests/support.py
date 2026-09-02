"""Shared, deliberately boring helpers for the ingestion tests.

`MockRoutes` is a URL → canned-reply table behind `httpx.MockTransport` that also
records every request, so tests can assert on headers, bodies and call order
without touching the network. The small builders at the bottom produce inline
feeds / pages for cases the on-disk fixtures do not cover.
"""

from __future__ import annotations

import os
from typing import Callable

import httpx

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

Reply = tuple[int, bytes, dict[str, str]]
Handler = Callable[[httpx.Request], Reply]

HTML_UTF8 = {"content-type": "text/html; charset=utf-8"}
HTML_CP1251 = {"content-type": "text/html; charset=windows-1251"}
XML = {"content-type": "application/xml"}
RSS = {"content-type": "application/rss+xml"}
GZIP = {"content-type": "application/x-gzip"}
JSON = {"content-type": "application/json"}

# A `t.me/s/<channel>` page with the channel header but no posts — what Telegram
# serves for `?after=<newest id>` when nothing newer exists.
EMPTY_CHANNEL_PAGE = (
    '<!DOCTYPE html><html><body class="tgme_page_body">'
    '<div class="tgme_channel_info"><div class="tgme_channel_info_header">'
    '<div class="tgme_channel_info_header_title"><span dir="auto">'
    "Цифровые индустриальные технологии"
    "</span></div></div></div></body></html>"
).encode("utf-8")


def read_fixture(name: str) -> bytes:
    with open(os.path.join(FIXTURES_DIR, name), "rb") as f:
        return f.read()


def default_raw_config() -> dict:
    """A fresh dict mirroring config.yaml (every key `Config.from_dict` requires)."""
    return {
        "scraper": {
            "date_window_hours": 72,
            "request_timeout": 20,
            "max_redirects": 5,
            "user_agent": "test-agent",
            "accept_language": "ru",
            "concurrency": 4,
            "per_host_concurrency": 2,
            "fetch_fulltext": True,
            "fulltext_timeout": 5,
            "fulltext_max_chars": 20000,
            "max_new_per_source": 50,
            "store_raw_html": False,
        },
        "telegram": {"backfill_pages": 1},
        "sitemap": {"max_sitemaps": 5, "max_urls": 200},
        "tavily": {"api_key_env": "TAVILY_API", "max_results": 10, "search_depth": "basic"},
    }


def raising(exc: Exception) -> Handler:
    """A route value that makes the transport raise `exc` (timeouts, connection errors)."""

    def handler(request: httpx.Request) -> Reply:
        raise exc

    return handler


class MockRoutes:
    """URL table for `httpx.MockTransport`.

    Keys are matched in this order: the full URL, `path?query`, bare `path`, then
    any key ending in `*` as a prefix of the full URL. Values are
    `(status, body, headers)` or a callable taking the request and returning that
    tuple. Unmatched URLs get `default` (404). Every request is appended to
    `requests` in call order.
    """

    def __init__(
        self,
        routes: dict[str, Reply | Handler] | None = None,
        default: Reply = (404, b"not found", {}),
    ):
        self.routes: dict[str, Reply | Handler] = dict(routes or {})
        self.default = default
        self.requests: list[httpx.Request] = []

    def __setitem__(self, key: str, value: Reply | Handler) -> None:
        self.routes[key] = value

    def _lookup(self, request: httpx.Request) -> Reply | Handler:
        url = request.url
        full = str(url)
        query = url.query.decode()
        for key in (full, url.path + (f"?{query}" if query else ""), url.path):
            if key in self.routes:
                return self.routes[key]
        for key, value in self.routes.items():
            if key.endswith("*") and full.startswith(key[:-1]):
                return value
        return self.default

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self._lookup(request)
        if callable(reply):
            reply = reply(request)
        status, body, headers = reply
        return httpx.Response(status, content=body, headers=headers, request=request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def client(self, **kwargs) -> httpx.Client:
        return httpx.Client(transport=self.transport(), **kwargs)

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]

    def requests_to(self, url: str) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url) == url]


def rss_bytes(items: list[dict], title: str = "Тестовая лента") -> bytes:
    """Build a minimal RSS 2.0 feed. Item keys: title, link, guid, pubDate, description, text."""
    parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<rss version="2.0" xmlns:yandex="http://news.yandex.ru"><channel>',
        f"<title>{title}</title><link>https://example.ru/</link>",
    ]
    for item in items:
        parts.append("<item>")
        parts.append(f"<title>{item.get('title', 'Заголовок')}</title>")
        if "link" in item:
            parts.append(f"<link>{item['link']}</link>")
        if "guid" in item:
            parts.append(f'<guid isPermaLink="false">{item["guid"]}</guid>')
        if "pubDate" in item:
            parts.append(f"<pubDate>{item['pubDate']}</pubDate>")
        if "description" in item:
            parts.append(f"<description>{item['description']}</description>")
        if "text" in item:
            parts.append(f"<yandex:full-text>{item['text']}</yandex:full-text>")
        parts.append("</item>")
    parts.append("</channel></rss>")
    return "\n".join(parts).encode("utf-8")


def html_page(title: str, body: str, *, published: str | None = None) -> bytes:
    """A small article page; `published` fills `article:published_time` when given."""
    meta = f'<meta property="article:published_time" content="{published}">' if published else ""
    return (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{title}</title>{meta}</head>"
        f"<body><main><article><h1>{title}</h1>{body}</article></main></body></html>"
    ).encode("utf-8")
