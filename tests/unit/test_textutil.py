"""src/sources/textutil.py — URL canonicalisation and feed-text cleanup."""

from __future__ import annotations

import pytest

from src.sources.textutil import (
    clean_summary,
    clean_url,
    html_to_text,
    looks_like_html,
    strip_html,
    title_from_text,
)

# ── clean_url ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://example.ru/news/123?utm_source=rss&utm_medium=feed",
            "https://example.ru/news/123",
        ),
        ("https://example.ru/news?id=5&from=main#top", "https://example.ru/news?id=5"),
        ("https://example.ru/list?page=2&ref=tg", "https://example.ru/list?page=2"),
        ("https://example.ru/a?erid=abc&x=1", "https://example.ru/a?x=1"),
        ("https://example.ru/a?yclid=1&ysclid=2&gclid=3&fbclid=4&_openstat=5", "https://example.ru/a"),
        ("https://example.ru/a?UTM_Source=x&Ref=y&keep=1", "https://example.ru/a?keep=1"),
        ("https://example.ru/a#section", "https://example.ru/a"),
        ("  https://example.ru/path/  ", "https://example.ru/path/"),
        ("https://example.ru/index.php?id=7", "https://example.ru/index.php?id=7"),
        ("http://government.ru/news/59768/", "http://government.ru/news/59768/"),
        (
            "https://www.cbr.ru/press/PR/?file=639238532949681178DSD.htm",
            "https://www.cbr.ru/press/PR/?file=639238532949681178DSD.htm",
        ),
        ("", ""),
        (None, ""),
    ],
    ids=["utm", "from+fragment", "ref", "erid", "click-ids", "case-insensitive", "fragment",
         "padded", "keeps-id", "trailing-slash-kept", "cbr-file-param", "empty", "none"],
)
def test_clean_url(url, expected):
    assert clean_url(url) == expected


def test_clean_url_tolerates_garbage_without_raising():
    result = clean_url("not a url at all")
    assert isinstance(result, str)
    assert result != ""


# ── strip_html / html_to_text ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("<b>жирный</b> &amp; <i>курсив</i>", "жирный & курсив"),
        ("&laquo;ДОМ.РФ&raquo;&nbsp;агент", "«ДОМ.РФ» агент"),
        ("без тегов", "без тегов"),
        ("", ""),
        (None, ""),
    ],
)
def test_strip_html(value, expected):
    assert strip_html(value) == expected


@pytest.mark.parametrize(
    ("fragment", "expected"),
    [
        ("<p>Первый абзац.</p><p>Второй абзац.</p>", "Первый абзац.\n\nВторой абзац."),
        ("Строка<br>Ещё строка", "Строка\nЕщё строка"),
        ("Строка<br />Ещё", "Строка\nЕщё"),
        ("<div>a</div><br><br><br><div>b</div>", "a\n\nb"),
        ("<ul><li>один</li><li>два</li></ul>", "один\n\nдва"),
        ("Текст &amp; &laquo;кавычки&raquo;", "Текст & «кавычки»"),
        ("  много   пробелов\tи nbsp ", "много пробелов и nbsp"),
        ("<h1>Заголовок</h1>текст", "Заголовок\nтекст"),
        ("", ""),
        (None, ""),
    ],
    ids=["paragraphs", "br", "self-closing-br", "collapse-blank-runs", "list", "entities",
         "spaces", "heading", "empty", "none"],
)
def test_html_to_text(fragment, expected):
    assert html_to_text(fragment) == expected


# ── clean_summary ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Анонс. Читать далее…", "Анонс."),
        ("Анонс. Читать полностью", "Анонс."),
        ("Анонс. читать дальше...", "Анонс."),
        ("Анонс Подробнее...", "Анонс"),
        ("Teaser. Read more…", "Teaser."),
        ("Teaser. Read More", "Teaser."),
        ("Текст. Сообщение Заголовок появились сначала на Сайт.", "Текст."),
        ("Text. The post Заголовок статьи appeared first on Кабельщик.", "Text."),
        ("Анонс →", "Анонс"),
        ("Анонс »", "Анонс"),
        ("  Анонс  ", "Анонс"),
        ("Читать далее в середине текста не трогаем.", "Читать далее в середине текста не трогаем."),
        ("", ""),
        (None, ""),
    ],
    ids=["chitat-dalee", "chitat-polnostyu", "chitat-dalshe-lowercase", "podrobnee", "read-more",
         "read-more-case", "wp-russian", "wp-english", "arrow", "guillemet", "padding",
         "mid-text-untouched", "empty", "none"],
)
def test_clean_summary(text, expected):
    assert clean_summary(text) == expected


# ── title_from_text ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("Заголовок\nТекст поста", 120, "Заголовок"),
        ("\n\n  Заголовок  \n", 120, "Заголовок"),
        ("Заголовок   с   пробелами", 120, "Заголовок с пробелами"),
        ("Это очень длинная строка, которая не влезает", 20, "Это очень длинная…"),
        ("Первое слово, второе слово", 14, "Первое слово…"),
        ("абвгдежзийклмнопрстуфхцч", 10, "абвгдежзий…"),
        ("a" * 120, 120, "a" * 120),
        ("", 120, ""),
        ("\n \n", 120, ""),
        (None, 120, ""),
    ],
    ids=["first-line", "skips-blank-lines", "collapses-spaces", "word-boundary",
         "strips-trailing-punctuation", "no-spaces-hard-cut", "exactly-limit", "empty",
         "only-blank", "none"],
)
def test_title_from_text(text, limit, expected):
    assert title_from_text(text, limit=limit) == expected


def test_title_from_text_default_limit_is_120():
    line = " ".join(["слово"] * 40)  # 239 chars
    title = title_from_text(line)
    assert title.endswith("…")
    assert len(title) <= 121


# ── looks_like_html ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"<!DOCTYPE html><html>", True),
        (b"\xef\xbb\xbf\n  <!doctype HTML>", True),
        (b"<html lang='ru'>", True),
        (b"   \r\n<HTML>", True),
        (b"<?xml version='1.0'?><rss>", False),
        (b'{"a": 1}', False),
        (b"", False),
        (b"<div>no root html tag</div>", False),
    ],
    ids=["doctype", "bom+ws", "html-tag", "upper", "xml", "json", "empty", "fragment"],
)
def test_looks_like_html(body, expected):
    assert looks_like_html(body) is expected
