"""src/sources/scraper_rss.py — feedparser entry mapping and the conditional-GET adapter."""

from __future__ import annotations

import feedparser
import httpx
import pytest
from support import HTML_UTF8, RSS, MockRoutes, raising, rss_bytes

from src.models import FetchState, Source
from src.sources.scraper_rss import (
    FULL_SUMMARY_CHARS,
    RssAdapter,
    entry_full_text,
    is_feed,
    parse_entry,
)

FEED_URL = "https://example.ru/rss.xml"


def _source(**overrides) -> Source:
    base = dict(id=7, name="Лента", url="https://example.ru/", kind="rss", fetch_url=FEED_URL)
    return Source(**{**base, **overrides})


def _entries(fixture_bytes, name):
    return feedparser.parse(fixture_bytes(name)).entries


# ── is_feed / entry_full_text ──────────────────────────────────────────────


def test_is_feed_true_for_rss_and_atom(fixture_bytes):
    assert is_feed(feedparser.parse(fixture_bytes("rss_kommersant.xml"))) is True
    assert is_feed(feedparser.parse(fixture_bytes("atom_sample.xml"))) is True


@pytest.mark.parametrize("body", [b"", b"plain text", b"<html><body>hi</body></html>"])
def test_is_feed_false_for_non_feeds(body):
    assert is_feed(feedparser.parse(body)) is False


def test_is_feed_true_for_ill_formed_feed_that_still_has_entries():
    broken = rss_bytes([{"title": "a", "link": "https://example.ru/1"}]) + b"<unclosed>"
    parsed = feedparser.parse(broken)
    assert parsed.bozo
    assert is_feed(parsed) is True


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (
            {
                "yandex_full-text": "Полный <b>текст</b>",
                "turbo_content": "<p>turbo</p>",
                "content": [{"value": "<p>content</p>"}],
            },
            "Полный текст",
        ),
        ({"turbo_content": "<p>Первый</p><p>Второй</p>", "content": [{"value": "x"}]},
         "Первый\n\nВторой"),
        ({"content": [{"value": "<p>короткий</p>"}, {"value": "<p>самый длинный вариант</p>"}]},
         "самый длинный вариант"),
        ({"yandex_full-text": "   ", "content": [{"value": "<p>из content</p>"}]}, "из content"),
        ({"content": [{"value": None}]}, ""),
        ({"summary": "только анонс"}, ""),
        ({}, ""),
    ],
    ids=["yandex-wins", "turbo-over-content", "longest-content", "blank-yandex-skipped",
         "content-without-value", "summary-is-not-fulltext", "empty"],
)
def test_entry_full_text_priority(entry, expected):
    assert entry_full_text(entry) == expected


# ── parse_entry on the fixtures ────────────────────────────────────────────


def test_parse_entry_yandex_item_has_full_text_and_pdf_attachment(fixture_bytes, now):
    entry = _entries(fixture_bytes, "rss_yandex_fulltext.xml")[0]
    doc = parse_entry(entry, _source(), now)
    assert doc.source_id == 7
    assert doc.external_id == "news-123"
    assert doc.url == "https://example.ru/news/123"
    assert doc.title == "Минцифры предложило новые правила реестра ПО"
    assert doc.summary == "Анонс: ведомство опубликовало проект постановления."
    assert doc.text == (
        "Минцифры опубликовало проект постановления о новых правилах ведения реестра "
        "российского ПО.\n\nДокумент вводит требование совместимости с доверенными "
        "операционными системами."
    )
    assert doc.author == "Иван Петров"
    assert doc.attachments == ["https://example.ru/files/proekt.pdf"]
    assert doc.published_at == "2026-09-02T07:00:00+00:00"
    assert doc.fetched_at == "2026-09-02T12:00:00+00:00"
    assert doc.needs_fulltext is False
    assert doc.content_hash == ""


def test_parse_entry_turbo_content_becomes_text(fixture_bytes, now):
    entry = _entries(fixture_bytes, "rss_yandex_fulltext.xml")[1]
    doc = parse_entry(entry, _source(), now)
    assert doc.external_id == "https://example.ru/news/124"
    assert doc.text == "Первый абзац турбо-контента.\n\nВторой абзац."
    assert doc.summary == "Короткий анонс."
    assert doc.needs_fulltext is False
    assert doc.published_at == "2026-09-02T06:00:00+00:00"


def test_parse_entry_content_encoded_becomes_text(fixture_bytes, now):
    entry = _entries(fixture_bytes, "rss_yandex_fulltext.xml")[2]
    doc = parse_entry(entry, _source(), now)
    assert doc.text == "Абзац из content:encoded.\nВторая строка."
    assert doc.needs_fulltext is False
    assert doc.published_at == "2026-09-02T05:00:00+00:00"


def test_parse_entry_short_announce_needs_fulltext(fixture_bytes, now):
    entry = _entries(fixture_bytes, "rss_yandex_fulltext.xml")[3]
    doc = parse_entry(entry, _source(), now)
    assert doc.title == "Только анонс — нужен полный текст"
    assert doc.text == ""
    assert doc.summary == "Совсем короткий анонс."
    assert doc.needs_fulltext is True
    assert doc.published_at == "2026-09-02T04:00:00+00:00"


def test_parse_entry_atom_content_and_id_fallbacks(fixture_bytes, now):
    first, second = _entries(fixture_bytes, "atom_sample.xml")
    doc1 = parse_entry(first, _source(), now)
    assert doc1.external_id == "urn:uuid:0001"
    assert doc1.url == "https://example.ru/atom/1"
    assert doc1.text == "Тело первой записи."
    assert doc1.author == "Автор"
    assert doc1.published_at == "2026-09-02T07:00:00+00:00"  # from updated_parsed
    assert doc1.needs_fulltext is False

    doc2 = parse_entry(second, _source(), now)
    assert doc2.external_id == "https://example.ru/atom/2"
    assert doc2.url == "https://example.ru/atom/2"
    assert doc2.summary == "Краткое содержание."
    assert doc2.needs_fulltext is True
    assert doc2.published_at == "2026-09-01T07:00:00+00:00"


def test_parse_entry_long_description_counts_as_full_text(fixture_bytes, now):
    entries = _entries(fixture_bytes, "rss_cbr_fulltext.xml")
    docs = [parse_entry(e, _source(), now) for e in entries]
    assert [d.external_id for d in docs] == ["docid_41602", "docid_41600"]
    assert docs[0].url == "https://www.cbr.ru/press/PR/?file=639238532949681178DSD.htm"
    for doc in docs:
        assert len(doc.summary) >= FULL_SUMMARY_CHARS
        assert doc.text == doc.summary
        assert doc.needs_fulltext is False
    assert docs[0].text.startswith("Совет директоров Банка России 25 августа 2026 года")
    assert "«ДОМ.РФ Ипотечный агент»" in docs[0].text
    assert docs[0].published_at == "2026-09-01T07:51:00+00:00"


@pytest.mark.parametrize(
    ("name", "expected_ids", "expected_first_published", "expected_author"),
    [
        (
            "rss_vedomosti.xml",
            [
                "https://www.vedomosti.ru/politics/news/2026/09/02/1225859-ukraina-prodolzhaet-prepyatstvovat",
                "https://www.vedomosti.ru/politics/news/2026/09/02/1225858-evrosoyuz-mozhet-priostanovit",
                "https://www.vedomosti.ru/politics/news/2026/09/02/1225856-putin-provel-vstrechu",
            ],
            "2026-09-02T20:13:43+00:00",
            "Ксения Наумчик",
        ),
        (
            "rss_kommersant.xml",
            [
                "https://www.kommersant.ru/doc/8924564",
                "https://www.kommersant.ru/doc/8924563",
                "https://www.kommersant.ru/doc/8924560",
            ],
            "2026-09-02T20:02:19+00:00",
            "",
        ),
        (
            "rss_telesputnik.xml",
            [
                "https://telesputnik.ru/materials/cifrovaya-sreda/news/mincifry-razrabotalo-pravila-vydaci-nacionalnyx-sertifikatov-dlya-podpisi-koda",
                "https://telesputnik.ru/materials/gov/news/razrabotcikam-otecestvennogo-ii-mogut-predostavit-nalogovye-vycety",
                "https://telesputnik.ru/materials/gov/news/v-rossii-vstupili-v-silu-obnovlennye-pravila-registracii-domennyx-imen",
            ],
            "2026-09-02T15:11:00+00:00",
            "Денис Чупров",
        ),
        (
            "rss_government.xml",
            [
                "http://government.ru/news/59768/",
                "http://government.ru/news/59767/",
                "http://government.ru/news/59766/",
            ],
            "2026-09-02T17:15:00+00:00",
            "",
        ),
    ],
)
def test_parse_entry_announce_only_feeds_need_fulltext(
    fixture_bytes, now, name, expected_ids, expected_first_published, expected_author
):
    docs = [parse_entry(e, _source(), now) for e in _entries(fixture_bytes, name)]
    assert [d.external_id for d in docs] == expected_ids
    assert [d.url for d in docs] == expected_ids
    assert all(d.needs_fulltext for d in docs)
    assert all(d.text == "" for d in docs)
    assert all(d.attachments == [] for d in docs)  # image enclosures dropped
    assert docs[0].published_at == expected_first_published
    assert docs[0].author == expected_author
    assert all(d.title for d in docs)


def test_parse_entry_strips_tags_from_title_and_author(now):
    entry = {"title": "<b>Жирный</b> заголовок", "link": "https://example.ru/1",
             "author": "<i>Автор</i>"}
    doc = parse_entry(entry, _source(), now)
    assert doc.title == "Жирный заголовок"
    assert doc.author == "Автор"


def test_parse_entry_returns_none_without_id_or_link(now):
    assert parse_entry({"title": "без ссылки"}, _source(), now) is None
    assert parse_entry({"id": "   ", "link": ""}, _source(), now) is None


def test_parse_entry_non_url_id_without_link_does_not_need_fulltext(now):
    doc = parse_entry({"id": "tag:abc", "title": "t", "summary": "s"}, _source(), now)
    assert doc.external_id == "tag:abc"
    assert doc.url == "tag:abc"
    assert doc.needs_fulltext is False


def test_parse_entry_date_fallbacks(now):
    from_string = parse_entry(
        {"link": "https://example.ru/1", "published": "2026-09-02T10:00:00+03:00"},
        _source(),
        now,
    )
    assert from_string.published_at == "2026-09-02T07:00:00+00:00"
    from_updated = parse_entry(
        {"link": "https://example.ru/1", "updated": "Tue, 02 Sep 2026 10:00:00 +0300"},
        _source(),
        now,
    )
    assert from_updated.published_at == "2026-09-02T07:00:00+00:00"
    undated = parse_entry({"link": "https://example.ru/1", "published": "вчера"}, _source(), now)
    assert undated.published_at is None


def test_parse_entry_keeps_non_image_enclosures_only(now):
    entry = {
        "link": "https://example.ru/1",
        "enclosures": [
            {"href": "https://example.ru/a.pdf", "type": "application/pdf"},
            {"href": "https://example.ru/a.jpg", "type": "image/jpeg"},
            {"href": "https://example.ru/untyped.docx"},
            {"type": "application/pdf"},
        ],
    }
    doc = parse_entry(entry, _source(), now)
    assert doc.attachments == ["https://example.ru/a.pdf", "https://example.ru/untyped.docx"]


# ── RssAdapter.fetch ───────────────────────────────────────────────────────


def test_fetch_parses_windows_1251_feed_and_stores_validators(config, mock_client, fixture_bytes, now):
    routes = MockRoutes(
        {
            FEED_URL: (
                200,
                fixture_bytes("rss_yandex_fulltext.xml"),
                {**RSS, "etag": 'W/"abc"', "last-modified": "Tue, 02 Sep 2026 10:00:00 GMT"},
            )
        }
    )
    result = RssAdapter(config.scraper).fetch(
        _source(), FetchState(source_id=7), mock_client(routes), now=now
    )
    assert result.error is None
    assert result.not_modified is False
    assert [d.external_id for d in result.documents] == [
        "news-123",
        "https://example.ru/news/124",
        "https://example.ru/news/125",
        "https://example.ru/news/126",
    ]
    assert result.documents[0].title == "Минцифры предложило новые правила реестра ПО"
    assert result.state_update == {
        "etag": 'W/"abc"',
        "last_modified": "Tue, 02 Sep 2026 10:00:00 GMT",
    }
    assert result.source_title is None
    request = routes.requests[0]
    assert "if-none-match" not in request.headers
    assert "if-modified-since" not in request.headers


def test_fetch_sends_validators_from_state_and_honours_304(config, mock_client, now):
    routes = MockRoutes({FEED_URL: (304, b"", {})})
    state = FetchState(source_id=7, etag='W/"abc"', last_modified="Tue, 02 Sep 2026 10:00:00 GMT")
    result = RssAdapter(config.scraper).fetch(_source(), state, mock_client(routes), now=now)
    assert result.not_modified is True
    assert result.error is None
    assert result.documents == []
    request = routes.requests[0]
    assert request.headers["if-none-match"] == 'W/"abc"'
    assert request.headers["if-modified-since"] == "Tue, 02 Sep 2026 10:00:00 GMT"


def test_fetch_missing_response_validators_are_stored_as_none(config, mock_client, now):
    routes = MockRoutes({FEED_URL: (200, rss_bytes([{"link": "https://example.ru/1"}]), RSS)})
    result = RssAdapter(config.scraper).fetch(
        _source(), FetchState(source_id=7), mock_client(routes), now=now
    )
    assert result.state_update == {"etag": None, "last_modified": None}
    assert len(result.documents) == 1


@pytest.mark.parametrize(
    ("reply", "expected_error"),
    [
        ((500, b"", {}), "HTTP 500"),
        ((404, b"not found", {}), "HTTP 404"),
        (raising(httpx.ReadTimeout("slow")), "timeout"),
    ],
    ids=["500", "404", "timeout"],
)
def test_fetch_reports_http_and_transport_errors(config, mock_client, now, reply, expected_error):
    routes = MockRoutes({FEED_URL: reply})
    result = RssAdapter(config.scraper).fetch(
        _source(), FetchState(source_id=7), mock_client(routes), now=now
    )
    assert result.error == expected_error
    assert result.documents == []
    assert result.state_update == {}


def test_fetch_html_body_is_reported_not_parsed(config, mock_client, fixture_bytes, now):
    routes = MockRoutes({FEED_URL: (200, fixture_bytes("tg_channel.html"), HTML_UTF8)})
    result = RssAdapter(config.scraper).fetch(
        _source(), FetchState(source_id=7), mock_client(routes), now=now
    )
    assert result.error == "got an HTML page instead of a feed"
    assert "HTML" in result.error


def test_fetch_non_feed_xml_is_reported(config, mock_client, now):
    routes = MockRoutes({FEED_URL: (200, b"<?xml version='1.0'?><root><x/></root>", RSS)})
    result = RssAdapter(config.scraper).fetch(
        _source(), FetchState(source_id=7), mock_client(routes), now=now
    )
    assert result.error is not None
    assert result.error.startswith("not a feed")


def test_fetch_ill_formed_feed_with_entries_still_yields_documents(config, mock_client, now):
    broken = rss_bytes([{"title": "a", "link": "https://example.ru/1"}]) + b"<unclosed>"
    routes = MockRoutes({FEED_URL: (200, broken, RSS)})
    result = RssAdapter(config.scraper).fetch(
        _source(), FetchState(source_id=7), mock_client(routes), now=now
    )
    assert result.error is None
    assert [d.url for d in result.documents] == ["https://example.ru/1"]


def test_fetch_follows_redirect_to_moved_feed(config, mock_client, fixture_bytes, now):
    routes = MockRoutes(
        {
            FEED_URL: (301, b"", {"location": "https://example.ru/feed/"}),
            "https://example.ru/feed/": (200, fixture_bytes("rss_government.xml"), RSS),
        }
    )
    result = RssAdapter(config.scraper).fetch(
        _source(), FetchState(source_id=7), mock_client(routes), now=now
    )
    assert result.error is None
    assert len(result.documents) == 3
