"""src/sources/base.py — Page, HostLimiter, the fetch helper, client and adapter registry."""

from __future__ import annotations

import threading
from contextlib import ExitStack

import httpx
import pytest
from support import MockRoutes, raising

from src.sources.base import HostLimiter, Page, build_adapters, fetch, make_client

# ── Page ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        (Page(url="https://a.ru", status=200), True),
        (Page(url="https://a.ru", status=204), True),
        (Page(url="https://a.ru", status=304), False),
        (Page(url="https://a.ru", status=404), False),
        (Page(url="https://a.ru", status=200, error="timeout"), False),
        (Page(url="https://a.ru"), False),
    ],
    ids=["200", "204", "304", "404", "error-overrides-status", "unfetched"],
)
def test_page_ok(page, expected):
    assert page.ok is expected


# ── HostLimiter ────────────────────────────────────────────────────────────


def test_host_limiter_third_concurrent_slot_blocks_until_one_is_released():
    limiter = HostLimiter(per_host=2)
    held = ExitStack()
    held.enter_context(limiter.slot("https://gov.ru/a"))
    held.enter_context(limiter.slot("https://www.GOV.ru/b".replace("www.", "")))
    acquired = threading.Event()

    def worker():
        with limiter.slot("https://GOV.ru/c"):
            acquired.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert acquired.wait(0.2) is False, "third slot on the same host should block"
    held.close()
    assert acquired.wait(2.0) is True
    thread.join(2.0)


def test_host_limiter_other_host_is_not_blocked():
    limiter = HostLimiter(per_host=1)
    acquired = threading.Event()
    with limiter.slot("https://gov.ru/a"):

        def worker():
            with limiter.slot("https://cbr.ru/a"):
                acquired.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        assert acquired.wait(2.0) is True
        thread.join(2.0)


def test_host_limiter_releases_slot_on_exception():
    limiter = HostLimiter(per_host=1)
    with pytest.raises(ValueError):
        with limiter.slot("https://gov.ru/a"):
            raise ValueError("boom")
    with limiter.slot("https://gov.ru/b"):
        pass  # would deadlock if the slot leaked


def test_host_limiter_floors_per_host_at_one():
    assert HostLimiter(0).per_host == 1
    assert HostLimiter(-5).per_host == 1
    assert HostLimiter(3).per_host == 3


# ── fetch ──────────────────────────────────────────────────────────────────


def test_fetch_returns_page_with_body_status_and_headers(mock_client):
    routes = MockRoutes({"https://a.ru/x": (200, b"hello", {"etag": 'W/"1"'})})
    page = fetch(mock_client(routes), "https://a.ru/x")
    assert page.ok is True
    assert (page.status, page.body, page.url) == (200, b"hello", "https://a.ru/x")
    assert page.headers["etag"] == 'W/"1"'
    assert page.redirected_from == []
    assert page.error is None


def test_fetch_follows_redirects_and_records_the_chain(mock_client):
    routes = MockRoutes(
        {
            "https://a.ru/r1": (301, b"", {"location": "https://a.ru/r2"}),
            "https://a.ru/r2": (302, b"", {"location": "https://a.ru/final"}),
            "https://a.ru/final": (200, b"done", {}),
        }
    )
    page = fetch(mock_client(routes), "https://a.ru/r1")
    assert page.url == "https://a.ru/final"
    assert page.redirected_from == ["https://a.ru/r1", "https://a.ru/r2"]
    assert page.body == b"done"
    assert routes.urls() == ["https://a.ru/r1", "https://a.ru/r2", "https://a.ru/final"]


def test_fetch_timeout_never_raises(mock_client):
    routes = MockRoutes({"https://slow.ru/": raising(httpx.ReadTimeout("slow"))})
    page = fetch(mock_client(routes), "https://slow.ru/")
    assert page.error == "timeout"
    assert page.ok is False
    assert page.url == "https://slow.ru/"


def test_fetch_connect_error_is_reported_as_request_error(mock_client):
    routes = MockRoutes({"https://dead.ru/": raising(httpx.ConnectError("refused"))})
    page = fetch(mock_client(routes), "https://dead.ru/")
    assert page.error == "request error: ConnectError: refused"
    assert page.error.startswith("request error")


def test_fetch_too_many_redirects(mock_client):
    routes = MockRoutes({"https://a.ru/loop": (302, b"", {"location": "https://a.ru/loop"})})
    page = fetch(mock_client(routes, max_redirects=2), "https://a.ru/loop")
    assert page.error == "too many redirects"


def test_fetch_http_error_status_is_not_an_error_string(mock_client):
    routes = MockRoutes({"https://a.ru/x": (503, b"busy", {})})
    page = fetch(mock_client(routes), "https://a.ru/x")
    assert page.error is None
    assert page.status == 503
    assert page.ok is False


def test_fetch_passes_headers_and_timeout_through(mock_client):
    routes = MockRoutes({"https://a.ru/x": (200, b"", {})})
    fetch(
        mock_client(routes),
        "https://a.ru/x",
        headers={"If-None-Match": 'W/"1"', "X-Test": "yes"},
        timeout=3.5,
    )
    request = routes.requests[0]
    assert request.headers["if-none-match"] == 'W/"1"'
    assert request.headers["x-test"] == "yes"
    assert request.extensions["timeout"]["read"] == 3.5
    assert request.extensions["timeout"]["connect"] == 3.5


def test_fetch_uses_client_timeout_when_none_given(mock_client):
    routes = MockRoutes({"https://a.ru/x": (200, b"", {})})
    fetch(mock_client(routes, timeout=7.0), "https://a.ru/x")
    assert routes.requests[0].extensions["timeout"]["read"] == 7.0


def test_fetch_takes_a_limiter_slot_for_the_request_host(mock_client):
    limiter = HostLimiter(per_host=1)
    routes = MockRoutes({"https://a.ru/x": (200, b"ok", {})})
    seen_hosts: list[str] = []
    original = limiter.slot

    def spy(url):
        seen_hosts.append(url)
        return original(url)

    limiter.slot = spy  # type: ignore[method-assign]
    page = fetch(mock_client(routes), "https://a.ru/x", limiter=limiter)
    assert page.ok
    assert seen_hosts == ["https://a.ru/x"]


# ── make_client / build_adapters ───────────────────────────────────────────


def test_make_client_sets_headers_redirects_and_transport(config):
    routes = MockRoutes({"https://a.ru/": (200, b"ok", {})})
    with make_client(config, transport=routes.transport()) as client:
        assert client.follow_redirects is True
        assert client.max_redirects == 5
        assert client.timeout.read == 20
        response = client.get("https://a.ru/")
    assert response.status_code == 200
    request = routes.requests[0]
    assert request.headers["user-agent"] == "test-agent"
    assert request.headers["accept-language"] == "ru"


def test_make_client_without_transport_is_a_plain_client(config):
    with make_client(config) as client:
        assert isinstance(client, httpx.Client)
        assert client.headers["user-agent"] == "test-agent"


def test_build_adapters_registers_every_kind(config):
    limiter = HostLimiter(2)
    adapters = build_adapters(config, limiter)
    assert set(adapters) == {"rss", "telegram", "sitemap", "html"}
    for kind, adapter in adapters.items():
        assert adapter.kind == kind
        assert adapter.limiter is limiter
        assert adapter.config is config.scraper
