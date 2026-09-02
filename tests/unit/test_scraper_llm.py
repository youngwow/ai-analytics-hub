"""src/sources/scraper_llm.py — the Tavily search client."""

from __future__ import annotations

import json

import httpx
import pytest
from support import JSON, MockRoutes, raising

from src.sources.scraper_llm import TAVILY_SEARCH_URL, Candidate, TavilyError, TavilySearch


def _search(config, fixture_bytes, reply=None) -> tuple[TavilySearch, MockRoutes]:
    routes = MockRoutes({TAVILY_SEARCH_URL: reply or (200, fixture_bytes("tavily_search.json"), JSON)})
    return TavilySearch("secret-key", config.tavily, transport=routes.transport()), routes


def test_empty_api_key_is_rejected_at_construction(config):
    with pytest.raises(TavilyError, match="no Tavily API key: set TAVILY_API"):
        TavilySearch("", config.tavily)


def test_search_posts_expected_payload_and_bearer_header(config, fixture_bytes):
    search, routes = _search(config, fixture_bytes)
    search.search("льготы для ИТ-компаний")
    assert len(routes.requests) == 1
    request = routes.requests[0]
    assert request.method == "POST"
    assert str(request.url) == TAVILY_SEARCH_URL
    assert request.headers["authorization"] == "Bearer secret-key"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {
        "query": "льготы для ИТ-компаний",
        "search_depth": "basic",
        "max_results": 10,
        "topic": "general",
        "include_domains": [],
        "include_answer": False,
        "include_raw_content": False,
    }


def test_search_overrides_go_into_the_payload(config, fixture_bytes):
    search, routes = _search(config, fixture_bytes)
    search.search(
        "закон об ИИ",
        max_results=3,
        include_domains=["gov.ru", "cbr.ru"],
        topic="news",
        time_range="week",
    )
    body = json.loads(routes.requests[0].content)
    assert body["max_results"] == 3
    assert body["include_domains"] == ["gov.ru", "cbr.ru"]
    assert body["topic"] == "news"
    assert body["time_range"] == "week"
    assert body["search_depth"] == "basic"


def test_search_maps_results_and_drops_entries_without_url(config, fixture_bytes):
    search, _ = _search(config, fixture_bytes)
    results = search.search("льготы для ИТ-компаний")
    assert results == [
        Candidate(
            url="https://digital.gov.ru/ru/events/12345/",
            title="Льготы для ИТ-компаний в 2026 году — Минцифры",
            snippet=(
                "Минцифры разъяснило условия применения налоговых льгот для аккредитованных "
                "ИТ-компаний..."
            ),
            score=0.91,
            published="Mon, 01 Sep 2026 10:00:00 GMT",
        ),
        Candidate(
            url="http://duma.gov.ru/news/64000/",
            title="Госдума приняла закон о продлении льгот",
            snippet="Депутаты одобрили продление пониженных тарифов страховых взносов.",
            score=0.85,
            published=None,
        ),
    ]
    assert results[0].score == pytest.approx(0.91)


def test_search_tolerates_missing_optional_fields(config, fixture_bytes):
    body = json.dumps({"results": [{"url": "https://a.ru/x"}, "junk", {"url": ""}]}).encode()
    search, _ = _search(config, fixture_bytes, reply=(200, body, JSON))
    assert search.search("q") == [Candidate(url="https://a.ru/x", score=0.0)]


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        ((401, b'{"detail": "bad key"}', JSON), "Tavily HTTP 401"),
        ((429, b"quota", {}), "Tavily HTTP 429: quota"),
        ((200, b"<html>not json</html>", {}), "Tavily returned non-JSON"),
        ((200, b'{"answer": "x"}', JSON), "Tavily response has no results list"),
        ((200, b"[1, 2]", JSON), "Tavily response has no results list"),
        (raising(httpx.ConnectError("down")), "Tavily request failed: ConnectError: down"),
        (raising(httpx.ReadTimeout("slow")), "Tavily request failed: ReadTimeout"),
    ],
    ids=["401", "429", "non-json", "no-results", "list-body", "connect-error", "timeout"],
)
def test_search_failures_raise_tavily_error(config, fixture_bytes, reply, message):
    search, _ = _search(config, fixture_bytes, reply=reply)
    with pytest.raises(TavilyError, match=message):
        search.search("q")
