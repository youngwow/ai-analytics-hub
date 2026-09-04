"""src/sources/scraper_search.py — saved Tavily queries as `search` sources."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from support import JSON, MockRoutes, raising

from src.config import Config
from src.models import FetchState, Source
from src.sources.base import HostLimiter
from src.sources.scraper_llm import TAVILY_SEARCH_URL, Candidate, TavilySearch
from src.sources.scraper_search import (
    DEFAULT_DAYS,
    MIN_PAGE_TEXT,
    SUMMARY_PREFIX,
    SearchAdapter,
    SearchQuery,
    candidate_to_document,
    is_search_url,
    summary_document,
)

GS_GROUP_URL = (
    "https://gs-group.com/press-center/news/"
    "ddr-pyatogo-pokoleniya-lokalizovany-v-rossii-po-novym-trebovaniyam"
)
TELESPUTNIK_URL = (
    "https://telesputnik.ru/materials/trends/news/trikolor-zavershil-testirovanie-cas-dreguard/"
)
FORUM_URL = "https://forum.example.ru/viewforum.php?f=605"
TELESPUTNIK_SNIPPET = (
    "«Триколор» совместно с технологическим партнёром GS Labs завершил период тестирования "
    "системы условного доступа CAS DREGUARD собственной разработки."
)
NEWS_ANSWER = (
    "«Триколор» совместно с технологическим партнёром GS Labs завершил тестирование системы "
    "условного доступа DREGUARD собственной разработки; GS Group локализовала в России "
    "производство памяти DDR5 для приставок и серверов."
)
QUERY = SearchQuery("GS Labs Триколор", ["gs-group.com", "telesputnik.ru"])


def _reply(body: dict) -> tuple:
    return (200, json.dumps(body, ensure_ascii=False).encode("utf-8"), JSON)


def _payload(routes: MockRoutes) -> dict:
    assert len(routes.requests_to(TAVILY_SEARCH_URL)) == 1
    return json.loads(routes.requests_to(TAVILY_SEARCH_URL)[0].content)


def _bound(payload: dict) -> dict:
    return {k: payload[k] for k in ("start_date", "days", "time_range") if k in payload}


def _source(fetch_url: str = QUERY.to_url(), source_id: int | None = 3) -> Source:
    return Source(
        id=source_id,
        name="GS Labs Триколор",
        url=fetch_url,
        kind="search",
        category="media",
        fetch_url=fetch_url,
    )


def _fetch(adapter: SearchAdapter, client, now, fetch_url: str = QUERY.to_url(), **kwargs):
    """First-run poll of the default source unless `state`/`backfill`/`since` say otherwise."""
    state = kwargs.pop("state", None) or FetchState(source_id=3)
    return adapter.fetch(_source(fetch_url), state, client, now=now, **kwargs)


@pytest.fixture
def news_reply(fixture_bytes) -> tuple:
    return (200, fixture_bytes("tavily_news.json"), JSON)


@pytest.fixture
def tavily_routes(news_reply) -> MockRoutes:
    return MockRoutes({TAVILY_SEARCH_URL: news_reply})


@pytest.fixture
def news_hits(config, mock_client, tavily_routes) -> list[Candidate]:
    """The three usable hits of tavily_news.json, mapped by the real client wrapper."""
    return TavilySearch("secret-key", config.tavily).search(mock_client(tavily_routes), "q").results


@pytest.fixture
def adapter(config) -> SearchAdapter:
    return SearchAdapter(config.tavily, config.scraper, HostLimiter(2), api_key="secret-key")


# ── SearchQuery ────────────────────────────────────────────────────────────


class TestSearchQuery:
    def test_defaults(self):
        query = SearchQuery("q")
        assert (query.domains, query.days, query.topic, query.summary) == ([], 7, "news", True)
        assert DEFAULT_DAYS == 7

    def test_query_whitespace_is_collapsed(self):
        assert SearchQuery("  закон \n об \t  ИИ ").query == "закон об ИИ"

    def test_domains_are_normalised_deduped_and_sorted(self):
        raw = ["  WWW.Gov.ru ", "cbr.ru", "gov.ru", "", "  ", "www.cbr.ru", None]
        assert SearchQuery("q", raw).domains == ["cbr.ru", "gov.ru"]

    def test_days_given_as_a_string_is_coerced(self):
        assert SearchQuery("q", days="3").days == 3

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"query": ""}, "search query is empty"),
            ({"query": " \t\n"}, "search query is empty"),
            ({"query": None}, "search query is empty"),
            ({"query": "q", "days": 0}, "days must be >= 1"),
            ({"query": "q", "days": -2}, "days must be >= 1"),
            ({"query": "q", "days": "неделя"}, "days must be a number"),
            ({"query": "q", "days": None}, "days must be a number"),
            ({"query": "q", "topic": "blog"}, "topic must be one of"),
            ({"query": "q", "topic": "News"}, "topic must be one of"),
        ],
        ids=[
            "empty",
            "whitespace",
            "none",
            "zero-days",
            "negative-days",
            "word-days",
            "none-days",
            "unknown-topic",
            "topic-case",
        ],
    )
    def test_invalid_values_raise(self, kwargs, message):
        with pytest.raises(ValueError, match=message):
            SearchQuery(**kwargs)

    def test_to_url_is_canonical(self):
        url = SearchQuery(" ai   law ", ["www.Gov.ru", "cbr.ru"], "3", "general", False).to_url()
        assert url == (
            "tavily://search?q=ai+law&domains=cbr.ru%2Cgov.ru&days=3&topic=general&summary=0"
        )

    def test_to_url_percent_encodes_cyrillic(self):
        url = SearchQuery("закон об ИИ").to_url()
        assert url.startswith("tavily://search?q=%D0%B7%D0%B0%D0%BA%D0%BE%D0%BD+")
        assert parse_qs(urlsplit(url).query, keep_blank_values=True) == {
            "q": ["закон об ИИ"],
            "domains": [""],
            "days": ["7"],
            "topic": ["news"],
            "summary": ["1"],
        }

    def test_equal_queries_share_one_url(self):
        a = SearchQuery("закон  об ИИ", ["gov.ru", "www.cbr.ru"])
        b = SearchQuery("закон об ИИ", ["CBR.ru", "gov.ru", ""])
        assert a == b
        assert a.to_url() == b.to_url()

    @pytest.mark.parametrize(
        "query",
        [
            SearchQuery("закон об ИИ"),
            SearchQuery(
                "GS Labs Триколор", ["gs-group.com", "telesputnik.ru"], 3, "general", False
            ),
            SearchQuery("q, with & odd=chars? #1", days=30),
        ],
        ids=["cyrillic", "every-field", "url-special-chars"],
    )
    def test_from_url_round_trips(self, query):
        assert SearchQuery.from_url(query.to_url()) == query

    def test_from_url_fills_defaults_for_missing_params(self):
        assert SearchQuery.from_url("tavily://search?q=%D0%B7%D0%B0%D0%BA%D0%BE%D0%BD") == (
            SearchQuery("закон")
        )

    def test_from_url_treats_blank_params_as_defaults(self):
        query = SearchQuery.from_url("tavily://search?q=x&domains=&days=&topic=&summary=")
        assert (query.domains, query.days, query.topic, query.summary) == ([], 7, "news", True)

    def test_from_url_accepts_literal_commas_and_an_upper_case_scheme(self):
        query = SearchQuery.from_url("  TAVILY://search?q=x&domains=Gov.ru,www.cbr.ru&summary=0 ")
        assert query.domains == ["cbr.ru", "gov.ru"]
        assert query.summary is False

    @pytest.mark.parametrize(
        ("param", "expected"),
        [
            ("", True),
            ("&summary=", True),
            ("&summary=1", True),
            ("&summary=yes", True),
            ("&summary=true", True),
            ("&summary=2", True),
            ("&summary=0", False),
            ("&summary=false", False),
            ("&summary=FALSE", False),
            ("&summary=no", False),
            ("&summary=off", False),
            ("&summary=%20Off%20", False),
        ],
        ids=[
            "missing",
            "blank",
            "1",
            "yes",
            "true",
            "2",
            "0",
            "false",
            "FALSE",
            "no",
            "off",
            "padded-Off",
        ],
    )
    def test_from_url_summary_spellings(self, param, expected):
        query = SearchQuery.from_url(f"tavily://search?q=x{param}")
        assert query.summary is expected
        assert SearchQuery.from_url(query.to_url()).summary is expected

    @pytest.mark.parametrize(
        ("url", "message"),
        [
            ("https://example.ru/?q=x", "not a tavily:// url"),
            ("", "not a tavily:// url"),
            ("tavily://search", "search query is empty"),
            ("tavily://search?q=", "search query is empty"),
            ("tavily://search?q=x&days=0", "days must be >= 1"),
            ("tavily://search?q=x&days=abc", "days must be a number"),
            ("tavily://search?q=x&topic=blog", "topic must be one of"),
        ],
        ids=["http", "empty", "no-query", "blank-query", "zero-days", "word-days", "topic"],
    )
    def test_from_url_rejects_bad_urls(self, url, message):
        with pytest.raises(ValueError, match=message):
            SearchQuery.from_url(url)

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            (SearchQuery("q"), "news, 7 дн."),
            (SearchQuery("q", ["gov.ru", "cbr.ru"], 3), "news, 3 дн., домены: cbr.ru, gov.ru"),
            (SearchQuery("q", topic="general", summary=False), "general, 7 дн., без сводки"),
            (
                SearchQuery("q", ["a.ru"], 1, "general", False),
                "general, 1 дн., домены: a.ru, без сводки",
            ),
        ],
        ids=["defaults", "domains", "general-no-summary", "everything"],
    )
    def test_describe(self, query, expected):
        assert query.describe() == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("tavily://search?q=x", True),
        ("  TAVILY://search?q=x  ", True),
        ("https://t.me/s/cit_gov", False),
        ("manual://import", False),
        ("", False),
        (None, False),
    ],
    ids=["tavily", "upper-padded", "https", "manual", "empty", "none"],
)
def test_is_search_url(url, expected):
    assert is_search_url(url) is expected


# ── candidate_to_document ──────────────────────────────────────────────────


class TestCandidateToDocument:
    def test_rich_page_dump_becomes_the_text(self, news_hits, now):
        doc = candidate_to_document(news_hits[0], 7, now, 20000)
        assert doc.source_id == 7
        assert doc.url == GS_GROUP_URL  # utm_* dropped
        assert doc.external_id == GS_GROUP_URL
        assert doc.title == "DDR пятого поколения локализованы в России по новым ..."
        assert doc.summary.startswith("GS Nanotech\nцентр разработки, корпусирования")
        assert doc.text.startswith("Российский инвестиционно-промышленный холдинг, ключевой")
        assert "О GS Group" not in doc.text  # menu rows are gone
        assert len(doc.text) >= MIN_PAGE_TEXT
        assert doc.needs_fulltext is False
        assert doc.published_at == "2026-09-01T02:00:00+00:00"
        assert doc.fetched_at == "2026-09-02T12:00:00+00:00"
        assert (doc.author, doc.attachments, doc.raw_html) == ("", [], None)

    def test_thin_page_dump_falls_back_to_the_snippet_and_asks_for_fulltext(self, news_hits, now):
        doc = candidate_to_document(news_hits[1], 7, now, 20000)
        assert doc.url == doc.external_id == TELESPUTNIK_URL
        assert doc.text == doc.summary == TELESPUTNIK_SNIPPET
        assert doc.needs_fulltext is True
        assert doc.published_at == "2026-08-31T09:30:00+00:00"

    def test_missing_raw_content_and_date(self, news_hits, now):
        doc = candidate_to_document(news_hits[2], 7, now, 20000)
        assert doc.url == FORUM_URL  # a functional query param survives cleaning
        assert doc.text == "Ветка форума о приставках и спутниковом ТВ."
        assert doc.needs_fulltext is True
        assert doc.published_at is None

    def test_text_is_capped_at_max_chars(self, news_hits, now):
        full = candidate_to_document(news_hits[0], 1, now, 20000).text
        capped = candidate_to_document(news_hits[0], 1, now, 50).text
        assert len(capped) == 50
        assert capped == full[:50]

    def test_title_whitespace_is_collapsed_and_snippet_tidied(self, now):
        c = Candidate(
            url="https://a.ru/x",
            title="  Заголовок \n с   переносом ",
            snippet="  первая строка  \n\n\n\n вторая ",
        )
        doc = candidate_to_document(c, 1, now, 100)
        assert doc.title == "Заголовок с переносом"
        assert doc.summary == "первая строка\n\nвторая"
        assert doc.text == doc.summary

    @pytest.mark.parametrize(
        ("published", "expected"),
        [
            ("Tue, 01 Sep 2026 02:00:00 GMT", "2026-09-01T02:00:00+00:00"),
            ("2026-09-01T10:00:00+03:00", "2026-09-01T07:00:00+00:00"),
            ("2026-09-01T10:00:00", "2026-09-01T07:00:00+00:00"),
            ("2026-09-01", "2026-08-31T21:00:00+00:00"),
            ("вчера", None),
            (None, None),
        ],
        ids=["rfc2822", "iso-offset", "naive-is-moscow", "date-only", "garbage", "missing"],
    )
    def test_published_at_is_canonical_utc_or_none(self, now, published, expected):
        candidate = Candidate(url="https://a.ru/x", published=published)
        assert candidate_to_document(candidate, 1, now, 100).published_at == expected


# ── summary_document ───────────────────────────────────────────────────────


class TestSummaryDocument:
    def test_digest_fields(self, now):
        query = SearchQuery("GS Labs Триколор", ["gs-group.com"])
        doc = summary_document(query, "Сводка дня.", 5, now)
        assert doc.source_id == 5
        assert doc.external_id == "summary:2026-09-02"
        assert doc.external_id.startswith(SUMMARY_PREFIX)
        assert doc.url == ""
        assert doc.title == "Сводка: GS Labs Триколор"
        assert doc.summary == doc.text == "Сводка дня."
        assert doc.author == "Tavily"
        assert doc.published_at == doc.fetched_at == "2026-09-02T12:00:00+00:00"
        assert doc.needs_fulltext is False

    @pytest.mark.parametrize(
        ("moment", "external_id"),
        [
            (datetime(2026, 9, 2, 23, 59, tzinfo=timezone.utc), "summary:2026-09-02"),
            (datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc), "summary:2026-09-03"),
            (datetime(2026, 9, 3, 1, 30), "summary:2026-09-02"),  # naive = Moscow → 22:30Z
        ],
        ids=["end-of-utc-day", "start-of-next-utc-day", "moscow-night"],
    )
    def test_external_id_follows_the_utc_day(self, moment, external_id):
        assert summary_document(SearchQuery("q"), "x", 1, moment).external_id == external_id

    def test_title_is_capped_at_200_chars(self, now):
        doc = summary_document(SearchQuery("ы" * 300), "x", 1, now)
        assert len(doc.title) == 200
        assert doc.title.startswith("Сводка: ыыы")


# ── SearchAdapter ──────────────────────────────────────────────────────────


class TestSearchAdapter:
    def test_kind_is_search(self, adapter):
        assert adapter.kind == "search"

    @pytest.mark.parametrize(
        ("fetch_url", "message"),
        [
            ("tavily://search?q=", "bad search url: search query is empty"),
            (
                "https://example.ru/rss",
                "bad search url: not a tavily:// url: https://example.ru/rss",
            ),
            ("tavily://search?q=x&days=0", "bad search url: days must be >= 1"),
        ],
        ids=["blank-query", "http-url", "zero-days"],
    )
    def test_bad_url_is_an_error_result_without_http(
        self, adapter, mock_client, tavily_routes, now, fetch_url, message
    ):
        result = _fetch(adapter, mock_client(tavily_routes), now, fetch_url)
        assert result.error == message
        assert result.documents == []
        assert tavily_routes.requests == []

    @pytest.mark.parametrize("api_key", ["", None], ids=["empty", "none"])
    def test_missing_key_is_an_error_result_without_http(
        self, config, mock_client, tavily_routes, now, api_key
    ):
        adapter = SearchAdapter(config.tavily, config.scraper, api_key=api_key)
        result = _fetch(adapter, mock_client(tavily_routes), now)
        assert result.error == "no Tavily API key: set TAVILY_API in the environment or .env"
        assert result.documents == []
        assert tavily_routes.requests == []

    def test_first_run_of_a_news_query_asks_for_the_window_in_days(
        self, adapter, mock_client, tavily_routes, now
    ):
        result = _fetch(adapter, mock_client(tavily_routes), now)
        assert result.error is None
        assert tavily_routes.requests[0].headers["authorization"] == "Bearer secret-key"
        assert _payload(tavily_routes) == {
            "query": "GS Labs Триколор",
            "search_depth": "basic",
            "max_results": 20,
            "topic": "news",
            "include_domains": ["gs-group.com", "telesputnik.ru"],
            "include_answer": "advanced",
            "include_raw_content": "text",
            "days": 7,
        }

    def test_first_run_of_a_general_query_asks_from_a_start_date(
        self, adapter, mock_client, tavily_routes, now
    ):
        url = SearchQuery("GS Labs Триколор", days=3, topic="general").to_url()
        _fetch(adapter, mock_client(tavily_routes), now, url)
        body = _payload(tavily_routes)
        assert _bound(body) == {"start_date": "2026-08-30"}
        assert body["topic"] == "general"
        assert body["include_domains"] == []

    @pytest.mark.parametrize(
        ("cursor", "backfill", "expected"),
        [
            ({"since": "2026-09-01", "days": 7}, False, {"start_date": "2026-08-31"}),
            ({"since": "2026-09-01T23:59:59+00:00"}, False, {"start_date": "2026-08-31"}),
            ({"since": "2026-08-27"}, False, {"start_date": "2026-08-26"}),
            ({"since": "2026-08-01"}, False, {"start_date": "2026-08-26"}),
            ({"since": "2026-09-01"}, True, {"days": 7}),
            ({"since": "вчера"}, False, {"days": 7}),
            ({"since": 20260901}, False, {"days": 7}),
            ({}, False, {"days": 7}),
        ],
        ids=[
            "day-overlap",
            "timestamp-cursor",
            "clamped-to-window-by-hours",
            "clamped-to-window",
            "backfill-ignores-cursor",
            "garbage-cursor",
            "non-string-cursor",
            "no-cursor",
        ],
    )
    def test_later_runs_start_a_day_before_the_previous_run_but_inside_the_window(
        self, adapter, mock_client, tavily_routes, now, cursor, backfill, expected
    ):
        state = FetchState(source_id=3, cursor=cursor, last_success_at="2026-09-01T12:00:00+00:00")
        _fetch(adapter, mock_client(tavily_routes), now, state=state, backfill=backfill)
        assert _bound(_payload(tavily_routes)) == expected

    def test_collector_since_does_not_narrow_the_query(
        self, adapter, mock_client, tavily_routes, now
    ):
        _fetch(adapter, mock_client(tavily_routes), now, since=now - timedelta(hours=1))
        assert _bound(_payload(tavily_routes)) == {"days": 7}

    def test_summary_off_disables_the_answer_and_the_digest(
        self, adapter, mock_client, tavily_routes, now
    ):
        url = SearchQuery("GS Labs Триколор", summary=False).to_url()
        result = _fetch(adapter, mock_client(tavily_routes), now, url)
        assert _payload(tavily_routes)["include_answer"] is False
        assert [d.external_id for d in result.documents] == [
            GS_GROUP_URL,
            TELESPUTNIK_URL,
            FORUM_URL,
        ]

    @pytest.mark.parametrize(
        ("topic", "expected"),
        [("news", {"language": "ru"}), ("general", {"country": "russia", "language": "ru"})],
    )
    def test_country_and_language_come_from_config(
        self, raw_config, mock_client, tavily_routes, now, topic, expected
    ):
        raw_config["tavily"].update(country="russia", language="ru")
        config = Config.from_dict(raw_config)
        adapter = SearchAdapter(config.tavily, config.scraper, api_key="secret-key")
        url = SearchQuery("GS Labs Триколор", topic=topic).to_url()
        _fetch(adapter, mock_client(tavily_routes), now, url)
        body = _payload(tavily_routes)
        assert {k: body[k] for k in ("country", "language") if k in body} == expected

    def test_hits_and_digest_become_documents(self, adapter, mock_client, tavily_routes, now):
        result = _fetch(adapter, mock_client(tavily_routes), now)
        assert result.error is None
        assert result.not_modified is False
        assert result.source_title is None
        assert [d.external_id for d in result.documents] == [
            GS_GROUP_URL,
            TELESPUTNIK_URL,
            FORUM_URL,
            "summary:2026-09-02",
        ]
        assert all(d.source_id == 3 for d in result.documents)
        assert [d.needs_fulltext for d in result.documents] == [False, True, True, False]
        digest = result.documents[-1]
        assert digest.title == "Сводка: GS Labs Триколор"
        assert digest.text == NEWS_ANSWER
        assert digest.url == ""
        assert result.state_update == {"cursor": {"since": "2026-09-02", "days": 7}}

    def test_text_is_capped_by_fulltext_max_chars(
        self, raw_config, mock_client, tavily_routes, now
    ):
        raw_config["scraper"]["fulltext_max_chars"] = 100
        config = Config.from_dict(raw_config)
        adapter = SearchAdapter(config.tavily, config.scraper, api_key="secret-key")
        result = _fetch(adapter, mock_client(tavily_routes), now)
        assert len(result.documents[0].text) == 100

    def test_unsaved_source_gets_source_id_zero(self, adapter, mock_client, tavily_routes, now):
        result = adapter.fetch(
            _source(source_id=None), FetchState(source_id=0), mock_client(tavily_routes), now=now
        )
        assert {d.source_id for d in result.documents} == {0}

    def test_empty_answer_adds_no_digest(self, adapter, mock_client, now):
        hit = {"url": "https://a.ru/x", "title": "Заголовок", "content": "Текст."}
        routes = MockRoutes({TAVILY_SEARCH_URL: _reply({"results": [hit], "answer": ""})})
        result = _fetch(adapter, mock_client(routes), now)
        assert [d.external_id for d in result.documents] == ["https://a.ru/x"]
        assert result.state_update == {"cursor": {"since": "2026-09-02", "days": 7}}

    def test_no_hits_is_still_a_success(self, adapter, mock_client, now):
        routes = MockRoutes({TAVILY_SEARCH_URL: _reply({"results": []})})
        result = _fetch(adapter, mock_client(routes), now)
        assert result.error is None
        assert result.documents == []
        assert result.state_update == {"cursor": {"since": "2026-09-02", "days": 7}}

    @pytest.mark.parametrize(
        ("reply", "message"),
        [
            ((401, b'{"detail": "bad key"}', JSON), "Tavily HTTP 401"),
            ((200, b"<html>", {}), "Tavily returned non-JSON"),
            (raising(httpx.ConnectError("down")), "Tavily request failed: ConnectError: down"),
        ],
        ids=["401", "non-json", "connect-error"],
    )
    def test_tavily_failure_is_an_error_result(self, adapter, mock_client, now, reply, message):
        routes = MockRoutes({TAVILY_SEARCH_URL: reply})
        result = _fetch(adapter, mock_client(routes), now)
        assert result.error is not None
        assert result.error.startswith(message)
        assert result.documents == []
        assert result.state_update == {}

    def test_request_takes_a_limiter_slot_for_the_tavily_host(
        self, config, mock_client, tavily_routes, now
    ):
        limiter = HostLimiter(1)
        seen: list[str] = []
        original = limiter.slot

        def spy(url):
            seen.append(url)
            return original(url)

        limiter.slot = spy  # type: ignore[method-assign]
        adapter = SearchAdapter(config.tavily, config.scraper, limiter, api_key="secret-key")
        _fetch(adapter, mock_client(tavily_routes), now)
        assert seen == [TAVILY_SEARCH_URL]
