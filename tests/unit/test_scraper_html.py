"""src/sources/scraper_html.py — article-link extraction from list pages."""

from __future__ import annotations

import httpx
import pytest
from support import HTML_UTF8, JSON, MockRoutes, raising

from src.models import FetchState, Source
from src.sources.scraper_html import HtmlAdapter, extract_article_links

BASE = "https://www.cableman.ru/"
FIRST_LINK = (
    "https://www.cableman.ru/content/"
    "rynok-svyazi-v-rossii-sokrashchaetsya-kolichestvo-telekom-kompanii-upalo-na-tret-za-sem-let"
)

LIST_PAGE = """<!DOCTYPE html><html><head><title>  Тестовый   сайт </title></head><body>
<header><a href="/content/header-story-title-long-enough">Заголовок в шапке страницы</a></header>
<nav><a href="/news/2026/09/01/nav-story">Ссылка из меню навигации</a></nav>
<main>
  <a href="/content/first-story-slug">Первая новость дня с длинным заголовком</a>
  <a href="/content/first-story-slug?utm_source=main#anchor">Первая новость дня (дубль)</a>
  <a href="https://www.site.ru/content/second-story-slug">Вторая новость дня с заголовком</a>
  <a href="/news/20260902/third">Третья новость с числовым идентификатором</a>
  <a href="https://other.ru/content/off-host-story-slug">Чужой сайт с длинным заголовком</a>
  <a href="mailto:info@site.ru">Написать нам письмо на почту</a>
  <a href="javascript:void(0)">Открыть всплывающее окно сайта</a>
  <a href="#comments">Перейти к комментариям к статье</a>
  <a href="/images/big-picture-of-the-day.jpg">Большая картинка дня на сайте</a>
  <a href="/files/prilozhenie-k-state.docx">Приложение к статье в формате docx</a>
  <a href="/">Главная страница сайта целиком</a>
  <a href="/content/">Все материалы</a>
  <a href="/tag">Тег</a>
  <a href="/content/fourth-story-slug">Четвёртая новость дня с заголовком</a>
</main>
<aside><a href="/content/aside-story-slug">Материал из боковой колонки сайта</a></aside>
<footer><a href="/content/footer-story-slug">Материал из подвала страницы сайта</a></footer>
</body></html>"""


def _source(**overrides) -> Source:
    base = dict(id=4, name="", url=BASE, kind="html", fetch_url=BASE)
    return Source(**{**base, **overrides})


# ── extract_article_links ──────────────────────────────────────────────────


def test_extract_article_links_on_real_homepage(fixture_bytes):
    links = extract_article_links(fixture_bytes("list_page_cableman.html"), BASE)
    assert len(links) == 38
    assert links[0] == FIRST_LINK
    assert len(set(links)) == 38
    assert all(link.startswith("https://www.cableman.ru/") for link in links)
    assert BASE not in links
    assert "https://www.cableman.ru/user" not in links
    assert "https://www.cableman.ru/news" not in links
    assert "https://www.cableman.ru/#main-content" not in links
    assert not any("/author/" in link for link in links)
    assert not any(link.endswith(".pdf") for link in links)
    assert sum("/content/" in link for link in links) == 13
    assert sum("/channel/news/" in link for link in links) == 10
    assert sum("/article/" in link for link in links) == 7


def test_extract_article_links_from_constructed_page():
    links = extract_article_links(LIST_PAGE.encode("utf-8"), "https://www.site.ru/")
    assert links == [
        "https://www.site.ru/content/first-story-slug",
        "https://www.site.ru/content/second-story-slug",
        "https://www.site.ru/news/20260902/third",
        "https://www.site.ru/content/fourth-story-slug",
    ]


def test_extract_article_links_removes_chrome_sections():
    links = extract_article_links(LIST_PAGE.encode("utf-8"), "https://www.site.ru/")
    for word in ("header", "nav", "aside", "footer"):
        assert not any(word in link for link in links), word


def test_extract_article_links_treats_www_as_same_host():
    body = (
        '<html><body><a href="https://site.ru/content/story-slug">Заголовок статьи сайта</a>'
        "</body></html>"
    ).encode("utf-8")
    assert extract_article_links(body, "https://www.site.ru/") == [
        "https://site.ru/content/story-slug"
    ]


def test_extract_article_links_requires_headline_or_id():
    body = (
        '<html><body><a href="/content/short">Кратко</a>'
        '<a href="/12345">Кратко</a>'
        '<a href="/p/x">Очень длинный текст ссылки на статью</a>'
        '<a href="/about-us-page">Очень длинный текст ссылки на статью</a>'
        "</body></html>"
    ).encode()
    assert extract_article_links(body, "https://site.ru/") == [
        "https://site.ru/12345",
        "https://site.ru/p/x",
        "https://site.ru/about-us-page",
    ]


def test_extract_article_links_empty_page():
    assert extract_article_links(b"", "https://site.ru/") == []
    assert extract_article_links(b"<html><body><p>no links</p></body></html>", "https://site.ru/") == []


# ── HtmlAdapter.fetch ──────────────────────────────────────────────────────


def test_fetch_returns_candidates_needing_fulltext_and_page_title(config, mock_client, fixture_bytes, now):
    routes = MockRoutes({BASE: (200, fixture_bytes("list_page_cableman.html"), HTML_UTF8)})
    result = HtmlAdapter(config.scraper).fetch(
        _source(), FetchState(source_id=4), mock_client(routes), now=now
    )
    assert result.error is None
    assert result.source_title == "Кабельщик"
    assert len(result.documents) == 38
    first = result.documents[0]
    assert first.url == FIRST_LINK
    assert first.external_id == FIRST_LINK
    assert first.source_id == 4
    assert first.needs_fulltext is True
    assert first.title == ""
    assert first.published_at is None
    assert first.fetched_at == "2026-09-02T12:00:00+00:00"
    assert result.state_update == {}


def test_fetch_normalises_title_whitespace(config, mock_client, now):
    routes = MockRoutes({"https://www.site.ru/": (200, LIST_PAGE.encode(), HTML_UTF8)})
    result = HtmlAdapter(config.scraper).fetch(
        _source(url="https://www.site.ru/", fetch_url="https://www.site.ru/"),
        FetchState(source_id=4),
        mock_client(routes),
        now=now,
    )
    assert result.source_title == "Тестовый сайт"
    assert len(result.documents) == 4


def test_fetch_uses_final_url_after_redirect_as_base(config, mock_client, now):
    routes = MockRoutes(
        {
            "https://site.ru/": (301, b"", {"location": "https://www.site.ru/"}),
            "https://www.site.ru/": (200, LIST_PAGE.encode(), HTML_UTF8),
        }
    )
    result = HtmlAdapter(config.scraper).fetch(
        _source(url="https://site.ru/", fetch_url="https://site.ru/"),
        FetchState(source_id=4),
        mock_client(routes),
        now=now,
    )
    assert result.documents[0].url == "https://www.site.ru/content/first-story-slug"


@pytest.mark.parametrize(
    ("reply", "expected_error"),
    [
        ((500, b"", {}), "HTTP 500"),
        ((403, b"<html>forbidden</html>", HTML_UTF8), "HTTP 403"),
        ((200, b'{"items": []}', JSON), "response is not an HTML page"),
        (raising(httpx.ReadTimeout("slow")), "timeout"),
    ],
    ids=["500", "403", "json", "timeout"],
)
def test_fetch_errors(config, mock_client, now, reply, expected_error):
    routes = MockRoutes({BASE: reply})
    result = HtmlAdapter(config.scraper).fetch(
        _source(), FetchState(source_id=4), mock_client(routes), now=now
    )
    assert result.error == expected_error
    assert result.documents == []


def test_fetch_accepts_html_fragment_with_anchors(config, mock_client, now):
    body = (
        '<div><a href="/content/story-with-a-long-title">Заголовок статьи на сайте</a></div>'
    ).encode("utf-8")
    routes = MockRoutes({BASE: (200, body, HTML_UTF8)})
    result = HtmlAdapter(config.scraper).fetch(
        _source(), FetchState(source_id=4), mock_client(routes), now=now
    )
    assert result.error is None
    assert [d.url for d in result.documents] == [
        "https://www.cableman.ru/content/story-with-a-long-title"
    ]
    assert result.source_title is None
