"""src/feed/search.py — пользовательский запрос → выражение FTS5, и что оно находит.

Половина файла проверяет чистую сборку строки, половина — что она действительно
достаёт карточку, у которой слово есть только в теге, только в сущности или
только в тексте оригинала (US: «поиск шире карточки»).
"""

from __future__ import annotations

import pytest

from src.feed import search
from src.feed.query import FeedQuery


def _ids(result: dict) -> list[int]:
    return [row["id"] for row in result["items"]]


# ── разбор запроса ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("КИИ и НДС", ["КИИ", "и", "НДС"]),
        ("ИИ-сервисов", ["ИИ", "сервисов"]),
        ("законопроект № 112233-8", ["законопроект", "112233", "8"]),
        ("ГОСТ_Р", ["ГОСТ", "Р"]),
        ("  ", []),
        ("!!! ??? ---", []),
        (None, []),
    ],
    ids=["cyrillic", "hyphen", "number", "underscore", "blank", "punctuation", "none"],
)
def test_terms_keeps_words_and_drops_punctuation(query, expected):
    assert search.terms(query) == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("КИИ", '"КИИ"*'),
        ("ИИ", '"ИИ"'),
        ("законопроект об ИИ", '"законопроект"* AND "об" AND "ИИ"'),
        ("Минцифры -реестр", '"Минцифры"* AND "реестр"*'),
        ('"прямая цитата"', '"прямая"* AND "цитата"*'),
        ("персданные AND реестр", '"персданные"* AND "AND"* AND "реестр"*'),
        ("персданные OR реестр", '"персданные"* AND "OR" AND "реестр"*'),
        ("персданные NEAR реестр", '"персданные"* AND "NEAR"* AND "реестр"*'),
        ("", ""),
        ("   ", ""),
        ("!!! ???", ""),
        ("№ —", ""),
    ],
    ids=[
        "three-letters-get-a-prefix",
        "two-letters-stay-exact",
        "words-joined-by-and",
        "minus-is-not-an-operator",
        "user-quotes-are-dropped",
        "and-is-a-word",
        "or-is-a-word",
        "near-is-a-word",
        "empty",
        "whitespace",
        "punctuation-only",
        "cyrillic-punctuation",
    ],
)
def test_to_match_builds_a_safe_fts5_expression(query, expected):
    assert search.to_match(query) == expected


def test_every_word_of_three_letters_or_more_gets_a_prefix_star():
    """Морфологии у unicode61 нет: без `*` «законопроект» не найдёт «законопроекта»."""
    assert search.to_match("акт три слова") == '"акт"* AND "три"* AND "слова"*'
    assert search.to_match("в и о") == '"в" AND "и" AND "о"'


# ── поиск по настоящему индексу ────────────────────────────────────────────


@pytest.fixture
def searchable(card_factory, source_factory) -> dict[str, int]:
    """Четыре карточки: у каждой ключевое слово лежит ровно в одном поле индекса."""
    media = source_factory("Ведомости", category="media")
    regulator = source_factory("Банк России", category="regulator")
    return {
        "tag_only": card_factory(
            media,
            title="Итоги недели на рынке",
            summary="Ничего примечательного.",
            original="Обзор погоды и настроений участников.",
            tags=("персданные",),
            published_at="2026-09-04T09:00:00+00:00",
        ),
        "entity_only": card_factory(
            media,
            title="Ведомство выпустило разъяснение",
            summary="Позиция изложена в письме.",
            original="Письмо направлено операторам связи.",
            entities=(("who", "Роскомнадзор"),),
            published_at="2026-09-03T09:00:00+00:00",
            priority="high",
        ),
        "original_only": card_factory(
            regulator,
            title="Короткая заметка",
            summary="Подробности в оригинале.",
            original="В документе упоминается трансграничная передача сведений.",
            published_at="2026-09-02T09:00:00+00:00",
        ),
        "npa": card_factory(
            regulator,
            title="Профильный комитет собрал отзывы",
            summary="Обсуждение продолжится осенью.",
            original="Ко второму чтению законопроекта подготовлены поправки.",
            tags=("регуляторика",),
            published_at="2026-09-01T09:00:00+00:00",
            type="npa",
            priority="high",
        ),
    }


@pytest.mark.parametrize(
    ("query", "key"),
    [
        ("персданные", "tag_only"),
        ("Роскомнадзор", "entity_only"),
        ("трансграничная", "original_only"),
        ("регуляторика", "npa"),
    ],
    ids=["tag", "entity", "original-text", "tag-of-the-npa"],
)
def test_search_reaches_past_the_card_into_tags_entities_and_the_original(
    feed, searchable, query, key
):
    result = feed.items(FeedQuery.build(q=query))

    assert _ids(result) == [searchable[key]]
    assert result["total"] == 1


def test_the_feed_finds_the_inflected_form_of_the_query_word(feed, searchable):
    result = feed.items(FeedQuery.build(q="законопроект"))

    assert _ids(result) == [searchable["npa"]]


@pytest.mark.parametrize(
    "query", ["РОСКОМНАДЗОР", "роскомнадзор", "РоСкОмНаДзОр"], ids=["upper", "lower", "mixed"]
)
def test_case_does_not_matter(feed, searchable, query):
    assert _ids(feed.items(FeedQuery.build(q=query))) == [searchable["entity_only"]]


def test_a_found_row_carries_a_snippet_with_the_match_highlighted(feed, searchable):
    row = feed.items(FeedQuery.build(q="трансграничная"))["items"][0]

    assert row["snippet"]
    assert "<b>" in row["snippet"] and "</b>" in row["snippet"]
    assert "трансграничная" in row["snippet"].lower()


def test_without_a_query_there_is_no_snippet_field_to_read(feed, searchable):
    assert all(row["snippet"] is None for row in feed.items(FeedQuery.build())["items"])


@pytest.mark.parametrize(
    "query", ["!!!", "   ", "№ — ©"], ids=["punctuation", "whitespace", "symbols"]
)
def test_a_query_that_cleans_down_to_nothing_returns_the_whole_feed(feed, searchable, query):
    result = feed.items(FeedQuery.build(q=query))

    assert result["total"] == len(searchable)
    assert all(row["snippet"] is None for row in result["items"])


@pytest.mark.parametrize(
    "query",
    ['"незакрытая кавычка', "персданные OR трансграничная", "персданные NEAR трансграничная"],
    ids=["unbalanced-quote", "or-as-a-word", "near-as-a-word"],
)
def test_operators_and_quotes_from_the_user_never_reach_fts5_as_syntax(feed, searchable, query):
    """Незакрытая кавычка не должна ронять выборку, а `OR` — расширять её."""
    result = feed.items(FeedQuery.build(q=query))

    assert result["total"] == 0
    assert result["items"] == []


def test_two_words_are_joined_by_and_not_or(feed, searchable):
    both = feed.items(FeedQuery.build(q="трансграничная передача"))
    only_one = feed.items(FeedQuery.build(q="трансграничная персданные"))

    assert _ids(both) == [searchable["original_only"]]
    assert _ids(only_one) == []


def test_search_combines_with_the_other_filters(feed, searchable):
    by_type = feed.items(FeedQuery.build(q="комитет", type="npa"))
    wrong_type = feed.items(FeedQuery.build(q="комитет", type="news"))

    assert _ids(by_type) == [searchable["npa"]]
    assert (wrong_type["items"], wrong_type["total"]) == ([], 0)


def test_search_narrows_the_date_range_too(feed, searchable):
    inside = feed.items(FeedQuery.build(q="Роскомнадзор", date_from="2026-09-03", date_to="2026-09-03"))
    outside = feed.items(FeedQuery.build(q="Роскомнадзор", date_from="2026-09-04"))

    assert _ids(inside) == [searchable["entity_only"]]
    assert outside["total"] == 0


@pytest.mark.parametrize("query", ["GS Labs", "gs labs", "Labs"], ids=["as-written", "lower",
                                                                       "one-word"])
def test_a_latin_name_next_to_cyrillic_is_found_either_way(
    feed, searchable, card_factory, source_factory, query
):
    item_id = card_factory(
        source_factory("Телеспутник"),
        title="GS Labs представила ИИ-рекомендации",
        original="Разработчик GS Labs показал сервис на приставке.",
        published_at="2026-08-31T09:00:00+00:00",
    )

    assert _ids(feed.items(FeedQuery.build(q=query))) == [item_id]


def test_a_very_long_original_is_indexed_whole(feed, card_factory, source_factory):
    """Иголка в конце стотысячезначного документа должна находиться."""
    body = ("Ничем не примечательный абзац про отраслевые совещания. " * 4000
            + "Заключение: постановление подписано в Новосибирске.")
    item_id = card_factory(
        source_factory("Регулятор"), title="Длинный документ", original=body
    )

    assert len(body) > 200_000
    assert _ids(feed.items(FeedQuery.build(q="Новосибирске"))) == [item_id]


def test_a_hidden_card_stays_out_of_the_search_results(feed, file_db, searchable):
    file_db.items.set_visibility(searchable["entity_only"], "hidden_feed", "нерелевантно")

    assert feed.items(FeedQuery.build(q="Роскомнадзор"))["total"] == 0
    assert feed.items(FeedQuery.build(q="Роскомнадзор", include_hidden=True))["total"] == 1
