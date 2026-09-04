"""src/sources/scraper_llm.py — the Tavily HTTP wrapper and the page-dump cleaner."""

from __future__ import annotations

import json

import httpx
import pytest
from support import JSON, MockRoutes, raising

from src.sources.scraper_llm import (
    MAX_RESULTS,
    REQUEST_TIMEOUT,
    TAVILY_SEARCH_URL,
    Candidate,
    SearchResponse,
    TavilyError,
    TavilySearch,
    clean_page_text,
)

GS_GROUP_RAW_URL = (
    "https://gs-group.com/press-center/news/"
    "ddr-pyatogo-pokoleniya-lokalizovany-v-rossii-po-novym-trebovaniyam"
    "?utm_source=tavily&utm_medium=test"
)
TELESPUTNIK_URL = (
    "https://telesputnik.ru/materials/trends/news/trikolor-zavershil-testirovanie-cas-dreguard/"
)
FORUM_URL = "https://forum.example.ru/viewforum.php?f=605"
TELESPUTNIK_SNIPPET = (
    "«Триколор» совместно с технологическим партнёром GS Labs завершил период тестирования "
    "системы условного доступа CAS DREGUARD собственной разработки."
)


def _reply(body: dict) -> tuple:
    return (200, json.dumps(body, ensure_ascii=False).encode("utf-8"), JSON)


def _payload(routes: MockRoutes) -> dict:
    assert len(routes.requests) == 1
    return json.loads(routes.requests[0].content)


@pytest.fixture
def tavily(config, mock_client, fixture_bytes):
    """`tavily(reply=None)` → (TavilySearch, client, routes); default reply: tavily_news.json."""

    def factory(reply=None):
        routes = MockRoutes(
            {TAVILY_SEARCH_URL: reply or (200, fixture_bytes("tavily_news.json"), JSON)}
        )
        return TavilySearch("secret-key", config.tavily), mock_client(routes), routes

    return factory


# ── construction / request shape ───────────────────────────────────────────


def test_empty_api_key_is_rejected_at_construction(config):
    with pytest.raises(TavilyError, match="no Tavily API key: set TAVILY_API"):
        TavilySearch("", config.tavily)


def test_search_posts_default_payload_with_bearer_header_and_long_timeout(tavily):
    searcher, client, routes = tavily()
    searcher.search(client, "льготы для ИТ-компаний")
    request = routes.requests[0]
    assert request.method == "POST"
    assert str(request.url) == TAVILY_SEARCH_URL
    assert request.headers["authorization"] == "Bearer secret-key"
    assert request.headers["content-type"] == "application/json"
    assert request.extensions["timeout"]["read"] == REQUEST_TIMEOUT == 30.0
    assert _payload(routes) == {
        "query": "льготы для ИТ-компаний",
        "search_depth": "basic",
        "max_results": 10,
        "topic": "general",
        "include_domains": [],
        "include_answer": False,
        "include_raw_content": False,
    }


def test_search_forwards_domains_topic_answer_and_raw_content_flags(tavily):
    searcher, client, routes = tavily()
    searcher.search(
        client,
        "закон об ИИ",
        include_domains=["gov.ru", "cbr.ru"],
        topic="news",
        include_answer="advanced",
        include_raw_content="text",
    )
    body = _payload(routes)
    assert body["query"] == "закон об ИИ"
    assert body["include_domains"] == ["gov.ru", "cbr.ru"]
    assert body["topic"] == "news"
    assert body["include_answer"] == "advanced"
    assert body["include_raw_content"] == "text"


@pytest.mark.parametrize(
    ("requested", "sent"),
    [(None, 10), (3, 3), (20, 20), (50, 20)],
    ids=["config-default", "below-cap", "at-cap", "above-cap"],
)
def test_search_caps_max_results_at_the_api_limit(tavily, requested, sent):
    searcher, client, routes = tavily()
    searcher.search(client, "q", max_results=requested)
    assert _payload(routes)["max_results"] == sent
    assert MAX_RESULTS == 20


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {"start_date": "2026-08-26", "days": 7, "time_range": "week", "topic": "news"},
            {"start_date": "2026-08-26"},
        ),
        ({"days": 7, "time_range": "week", "topic": "news"}, {"days": 7}),
        ({"days": 7, "time_range": "week", "topic": "general"}, {"time_range": "week"}),
        ({"days": 7, "topic": "general"}, {}),
        ({"time_range": "month"}, {"time_range": "month"}),
        ({"start_date": "2026-08-26", "topic": "general"}, {"start_date": "2026-08-26"}),
        ({}, {}),
    ],
    ids=[
        "start-date-wins",
        "days-beats-time-range",
        "days-ignored-for-general",
        "days-alone-for-general",
        "time-range-alone",
        "start-date-any-topic",
        "no-bound",
    ],
)
def test_search_sends_at_most_one_recency_bound(tavily, kwargs, expected):
    searcher, client, routes = tavily()
    searcher.search(client, "q", **kwargs)
    body = _payload(routes)
    assert {k: body[k] for k in ("start_date", "days", "time_range") if k in body} == expected


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"country": "russia", "topic": "general"}, {"country": "russia"}),
        ({"country": "russia", "topic": "news"}, {}),
        ({"language": "ru", "topic": "news"}, {"language": "ru"}),
        (
            {"language": "ru", "country": "russia", "topic": "general"},
            {"country": "russia", "language": "ru"},
        ),
        ({"country": "", "language": ""}, {}),
    ],
    ids=["country-general", "country-dropped-for-news", "language-any-topic", "both", "blank"],
)
def test_search_sends_country_only_for_general_and_language_whenever_given(
    tavily, kwargs, expected
):
    searcher, client, routes = tavily()
    searcher.search(client, "q", **kwargs)
    body = _payload(routes)
    assert {k: body[k] for k in ("country", "language") if k in body} == expected


# ── response mapping ───────────────────────────────────────────────────────


def test_search_maps_hits_and_answer_and_drops_entries_without_url(tavily):
    searcher, client, _ = tavily()
    response = searcher.search(client, "GS Labs Триколор")
    assert isinstance(response, SearchResponse)
    assert response.answer.startswith("«Триколор» совместно с технологическим партнёром GS Labs")
    assert [c.url for c in response.results] == [GS_GROUP_RAW_URL, TELESPUTNIK_URL, FORUM_URL]

    rich, thin, undated = response.results
    assert rich.title == "DDR пятого поколения локализованы в России по новым ..."
    assert rich.snippet.startswith("GS Nanotech\nцентр разработки")
    assert rich.score == pytest.approx(0.24803796)
    assert rich.published == "Tue, 01 Sep 2026 02:00:00 GMT"
    assert rich.raw_content.startswith("GS Group\nО GS Group")
    assert rich.raw_content.endswith("Пресс-центр")  # trailing whitespace stripped
    assert thin == Candidate(
        url=TELESPUTNIK_URL,
        title="«Триколор» завершил тестирование CAS DREGUARD — Телеспутник",
        snippet=TELESPUTNIK_SNIPPET,
        score=0.71,
        published="Mon, 31 Aug 2026 09:30:00 GMT",
        raw_content=(
            "Главная\nНовости\nТренды\n\n«Триколор» завершил тестирование CAS DREGUARD\n\n"
            "Подписаться\nПоделиться"
        ),
    )
    assert undated == Candidate(
        url=FORUM_URL,
        title="Спутниковое ТВ — обсуждение",
        snippet="Ветка форума о приставках и спутниковом ТВ.",
        score=0.12,
        published=None,
        raw_content="",
    )


def test_search_reply_without_answer_yields_empty_answer(tavily, fixture_bytes):
    searcher, client, _ = tavily(reply=(200, fixture_bytes("tavily_search.json"), JSON))
    response = searcher.search(client, "льготы для ИТ-компаний")
    assert response.answer == ""
    assert response.results == [
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


@pytest.mark.parametrize(
    ("answer", "expected"),
    [(None, ""), (42, ""), ({"text": "x"}, ""), ("  Сводка дня.  ", "Сводка дня.")],
    ids=["null", "number", "object", "padded-string"],
)
def test_search_answer_is_kept_only_when_it_is_a_string(tavily, answer, expected):
    searcher, client, _ = tavily(reply=_reply({"results": [], "answer": answer}))
    assert searcher.search(client, "q").answer == expected


def test_search_tolerates_missing_optional_fields_and_junk_entries(tavily):
    body = {
        "results": [
            {"url": "https://a.ru/x"},
            "junk",
            {"url": ""},
            {"url": None, "title": "без адреса"},
            {
                "url": "https://a.ru/y",
                "title": None,
                "content": None,
                "score": None,
                "published_date": None,
                "raw_content": None,
            },
        ]
    }
    searcher, client, _ = tavily(reply=_reply(body))
    assert searcher.search(client, "q") == SearchResponse(
        results=[Candidate(url="https://a.ru/x"), Candidate(url="https://a.ru/y")], answer=""
    )


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        ((401, b'{"detail": "bad key"}', JSON), "Tavily HTTP 401"),
        ((429, b"quota", {}), "Tavily HTTP 429: quota"),
        ((200, b"<html>not json</html>", {}), "Tavily returned non-JSON"),
        ((200, b'{"answer": "x"}', JSON), "Tavily response has no results list"),
        ((200, b'{"results": "none"}', JSON), "Tavily response has no results list"),
        ((200, b"[1, 2]", JSON), "Tavily response has no results list"),
        (raising(httpx.ConnectError("down")), "Tavily request failed: ConnectError: down"),
        (raising(httpx.ReadTimeout("slow")), "Tavily request failed: ReadTimeout"),
    ],
    ids=[
        "401",
        "429",
        "non-json",
        "no-results",
        "results-not-a-list",
        "list-body",
        "connect-error",
        "timeout",
    ],
)
def test_search_failures_raise_tavily_error(tavily, reply, message):
    searcher, client, _ = tavily(reply=reply)
    with pytest.raises(TavilyError, match=message):
        searcher.search(client, "q")


# ── clean_page_text ────────────────────────────────────────────────────────

SENTENCE = (
    "Минцифры разъяснило условия применения налоговых льгот для аккредитованных ИТ-компаний."
)
SECOND = "Операторы связи должны будут вести реестр таких карт и передавать сведения в систему."


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("   \n\n \t ", ""),
        (SENTENCE, SENTENCE),
        (f"Главная\nНовости\n{SENTENCE}\nПодписаться\nПоделиться", SENTENCE),
        ("один два три четыре пять шесть семь восемь девять десять", ""),
        (f"* {SENTENCE}", ""),
        (f"[Фото] {SENTENCE}", ""),
        (f"image 12 {SENTENCE}", ""),
        (f"{SENTENCE}\n{SENTENCE}\n\n{SENTENCE}", SENTENCE),
        (
            "  Минцифры   разъяснило\tусловия применения налоговых льгот для "
            "аккредитованных ИТ-компаний.  ",
            SENTENCE,
        ),
        (f"{SENTENCE}\nМеню\n{SECOND}", f"{SENTENCE}\n\n{SECOND}"),
    ],
    ids=[
        "empty",
        "whitespace-only",
        "single-paragraph",
        "menu-rows-dropped",
        "unpunctuated-dropped",
        "bullet-dropped",
        "bracket-dropped",
        "image-caption-dropped",
        "repeats-kept-once",
        "inner-whitespace-collapsed",
        "paragraphs-joined-by-blank-line",
    ],
)
def test_clean_page_text_keeps_prose_only(raw, expected):
    assert clean_page_text(raw) == expected


@pytest.mark.parametrize("end", [".", "!", "?", "»", "…", ":", '"'])
def test_clean_page_text_accepts_every_sentence_terminator(end):
    line = f"один два три четыре пять шесть семь восемь{end}"
    assert clean_page_text(line) == line


def test_clean_page_text_min_words_is_adjustable():
    line = "Короткая фраза из четырёх слов."
    assert clean_page_text(line) == ""
    assert clean_page_text(line, min_words=3) == line
