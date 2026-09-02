"""src/sources/fulltext.py — trafilatura extraction and the parallel enricher."""

from __future__ import annotations

import httpx
import pytest
from support import HTML_CP1251, HTML_UTF8, MockRoutes, html_page, raising

from src.models import RawDocument
from src.sources.fulltext import Extracted, FullTextFetcher, extract, tidy

ARTICLE_URL = "https://www.cableman.ru/content/m2m-sim"
ARTICLE_TITLE = "Минцифры предложило правила управления M2M SIM-картами"
PARAGRAPH_1 = (
    "Министерство цифрового развития опубликовало проект правил, регулирующих использование "
    "SIM-карт в межмашинных системах связи, включая требования к идентификации устройств."
)
PARAGRAPH_2 = (
    "Операторы связи должны будут вести реестр таких карт и передавать сведения в единую "
    "систему; сроки вступления в силу — 1 марта следующего года."
)


def _doc(url: str, **overrides) -> RawDocument:
    base = dict(source_id=1, external_id=url, url=url, needs_fulltext=True)
    return RawDocument(**{**base, **overrides})


# ── tidy ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("  строка  с   отступом \n\n\n\n вторая", "строка с отступом\n\nвторая"),
        ("a\xa0\xa0b\t c", "a b c"),
        ("\n\nодин\n\n\nдва\n\n", "один\n\nдва"),
        ("", ""),
        (None, ""),
    ],
    ids=["indent+blank-runs", "nbsp+tabs", "leading-trailing", "empty", "none"],
)
def test_tidy(text, expected):
    assert tidy(text) == expected


# ── extract ────────────────────────────────────────────────────────────────


def test_extract_reads_title_author_and_paragraphs_from_cp1251_article(fixture_bytes):
    result = extract(fixture_bytes("article_cp1251.html"), ARTICLE_URL, 20000)
    assert result is not None
    assert result.title == ARTICLE_TITLE
    assert result.author == "Иван Петров"
    assert PARAGRAPH_1 in result.text
    assert PARAGRAPH_2 in result.text
    assert result.text.index(PARAGRAPH_1) < result.text.index(PARAGRAPH_2)
    assert "Главная" not in result.text  # nav chrome dropped
    assert "©" not in result.text


def test_extract_converts_published_time_meta_to_utc_iso(fixture_bytes):
    # <meta property="article:published_time" content="2026-09-02T12:30:00+03:00">
    result = extract(fixture_bytes("article_cp1251.html"), ARTICLE_URL, 20000)
    assert result.date == "2026-09-02T09:30:00+00:00"


def test_extract_truncates_text_to_max_chars(fixture_bytes):
    result = extract(fixture_bytes("article_cp1251.html"), ARTICLE_URL, 40)
    assert len(result.text) == 40
    assert result.text == ARTICLE_TITLE[:40]


def test_extract_reads_date_from_page_built_by_helper():
    page = html_page("Заголовок", f"<p>{PARAGRAPH_1}</p><p>{PARAGRAPH_2}</p>",
                     published="2026-09-01T18:00:00+03:00")
    result = extract(page, "https://example.ru/content/story", 20000)
    assert result is not None
    assert result.date == "2026-09-01T15:00:00+00:00"


def test_extract_returns_none_for_undated_page_date():
    page = html_page("Заголовок", f"<p>{PARAGRAPH_1}</p><p>{PARAGRAPH_2}</p>")
    result = extract(page, "https://example.ru/content/story", 20000)
    assert result is not None
    assert result.date is None


@pytest.mark.parametrize(
    "body",
    [b"", b"\x00\x01\x02 garbage \xff", b"<html><body></body></html>", b"just words"],
    ids=["empty", "binary", "empty-html", "plain-text"],
)
def test_extract_returns_none_when_nothing_usable(body):
    assert extract(body, "https://example.ru/x", 1000) is None


# ── FullTextFetcher.fetch_one ──────────────────────────────────────────────


def test_fetch_one_returns_extracted_and_raw_html(config, mock_client, fixture_bytes):
    article = fixture_bytes("article_cp1251.html")
    routes = MockRoutes({ARTICLE_URL: (200, article, HTML_CP1251)})
    fetcher = FullTextFetcher(config.scraper)
    extracted, raw = fetcher.fetch_one(mock_client(routes), ARTICLE_URL)
    assert isinstance(extracted, Extracted)
    assert extracted.title == ARTICLE_TITLE
    assert raw == article
    assert routes.requests[0].extensions["timeout"]["read"] == 5


@pytest.mark.parametrize(
    ("reply", "case"),
    [
        ((404, b"<html><body><p>nope</p></body></html>", HTML_UTF8), "http-404"),
        ((500, b"", {}), "http-500"),
        ((200, b"%PDF-1.4 binary", {"content-type": "application/pdf"}), "pdf-content-type"),
        ((200, b"{}", {"content-type": "application/json"}), "json-content-type"),
        (raising(httpx.ReadTimeout("slow")), "timeout"),
        (raising(httpx.ConnectError("down")), "connect-error"),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_fetch_one_gives_none_none_when_page_is_unusable(config, mock_client, reply, case):
    routes = MockRoutes({ARTICLE_URL: reply})
    extracted, raw = FullTextFetcher(config.scraper).fetch_one(mock_client(routes), ARTICLE_URL)
    assert (extracted, raw) == (None, None), case


def test_fetch_one_accepts_text_and_xml_content_types(config, mock_client):
    page = html_page("Заголовок", f"<p>{PARAGRAPH_1}</p>")
    for ctype in ("text/plain", "application/xhtml+xml", ""):
        routes = MockRoutes({ARTICLE_URL: (200, page, {"content-type": ctype} if ctype else {})})
        extracted, raw = FullTextFetcher(config.scraper).fetch_one(mock_client(routes), ARTICLE_URL)
        assert extracted is not None, ctype
        assert raw == page


# ── FullTextFetcher.enrich ─────────────────────────────────────────────────


def test_enrich_fills_only_documents_flagged_needs_fulltext(config, mock_client, fixture_bytes):
    article = fixture_bytes("article_cp1251.html")
    routes = MockRoutes(
        {
            "https://example.ru/a": (200, article, HTML_CP1251),
            "https://example.ru/b": (200, article, HTML_CP1251),
        }
    )
    needs = _doc("https://example.ru/a")
    already = _doc("https://example.ru/b", needs_fulltext=False, text="есть текст")
    filled = FullTextFetcher(config.scraper).enrich(mock_client(routes), [needs, already])
    assert filled == 1
    assert routes.urls() == ["https://example.ru/a"]
    assert PARAGRAPH_1 in needs.text
    assert already.text == "есть текст"
    assert needs.needs_fulltext is False
    assert already.needs_fulltext is False


def test_enrich_fills_missing_metadata_but_keeps_existing(config, mock_client, fixture_bytes):
    article = fixture_bytes("article_cp1251.html")
    routes = MockRoutes({"https://example.ru/*": (200, article, HTML_CP1251)})
    blank = _doc("https://example.ru/blank")
    preset = _doc(
        "https://example.ru/preset",
        title="Заголовок из ленты",
        author="Автор из ленты",
        published_at="2026-09-02T04:00:00+00:00",
    )
    FullTextFetcher(config.scraper).enrich(mock_client(routes), [blank, preset])

    assert blank.title == ARTICLE_TITLE
    assert blank.author == "Иван Петров"
    assert blank.published_at == "2026-09-02T09:30:00+00:00"

    assert preset.title == "Заголовок из ленты"
    assert preset.author == "Автор из ленты"
    assert preset.published_at == "2026-09-02T04:00:00+00:00"
    assert PARAGRAPH_1 in preset.text


def test_enrich_clears_needs_fulltext_even_when_the_fetch_fails(config, mock_client):
    routes = MockRoutes(
        {
            "https://example.ru/404": (404, b"", {}),
            "https://example.ru/timeout": raising(httpx.ReadTimeout("slow")),
            "https://example.ru/empty": (200, b"<html><body></body></html>", HTML_UTF8),
        }
    )
    docs = [
        _doc("https://example.ru/404", title="t1"),
        _doc("https://example.ru/timeout", title="t2"),
        _doc("https://example.ru/empty", title="t3"),
    ]
    filled = FullTextFetcher(config.scraper).enrich(mock_client(routes), docs)
    assert filled == 0
    assert [d.needs_fulltext for d in docs] == [False, False, False]
    assert [d.text for d in docs] == ["", "", ""]
    assert [d.title for d in docs] == ["t1", "t2", "t3"]


def test_enrich_skips_documents_without_url(config, mock_client):
    routes = MockRoutes()
    doc = _doc("", needs_fulltext=True)
    assert FullTextFetcher(config.scraper).enrich(mock_client(routes), [doc]) == 0
    assert routes.requests == []
    assert doc.needs_fulltext is True


def test_enrich_returns_zero_without_targets(config, mock_client):
    routes = MockRoutes()
    docs = [_doc("https://example.ru/a", needs_fulltext=False)]
    assert FullTextFetcher(config.scraper).enrich(mock_client(routes), docs) == 0
    assert routes.requests == []


@pytest.mark.parametrize("store", [False, True])
def test_enrich_stores_raw_html_only_when_configured(raw_config, mock_client, fixture_bytes, store):
    from src.config import Config

    raw_config["scraper"]["store_raw_html"] = store
    config = Config.from_dict(raw_config)
    article = fixture_bytes("article_cp1251.html")
    routes = MockRoutes(
        {
            "https://example.ru/ok": (200, article, HTML_CP1251),
            "https://example.ru/empty": (200, b"<html><body></body></html>", HTML_UTF8),
        }
    )
    ok = _doc("https://example.ru/ok")
    empty = _doc("https://example.ru/empty")
    FullTextFetcher(config.scraper).enrich(mock_client(routes), [ok, empty])
    if store:
        assert ok.raw_html == article.decode("utf-8", errors="replace")
        assert empty.raw_html == "<html><body></body></html>"
    else:
        assert ok.raw_html is None
        assert empty.raw_html is None


def test_enrich_survives_a_crashing_fetch(config, mock_client, monkeypatch):
    fetcher = FullTextFetcher(config.scraper)

    def boom(client, url):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(fetcher, "fetch_one", boom)
    doc = _doc("https://example.ru/a", title="t")
    assert fetcher.enrich(mock_client(MockRoutes()), [doc]) == 0
    assert doc.needs_fulltext is False
    assert doc.text == ""
