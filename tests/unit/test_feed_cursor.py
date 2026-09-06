"""Листание ленты курсором: без повторов, без пропусков, устойчиво к вставке.

Курсор — это позиция по ключам сортировки, а не смещение, поэтому карточка,
появившаяся между страницами, не должна ни дублировать выдачу, ни выбивать из
неё соседей.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.exceptions import QueryError
from src.models.queries import FeedQuery


def _ids(result: dict) -> list[int]:
    return [row["id"] for row in result["items"]]


def _names(corpus: dict[str, int], ids: list[int]) -> list[str]:
    by_id = {item_id: name for name, item_id in corpus.items()}
    return [by_id[i] for i in ids]


def _walk(feed, *, limit: int = 2, pages: int = 20, start: str | None = None, **kwargs) -> list[int]:
    """Пройти ленту постранично от `start` и вернуть идентификаторы в порядке выдачи."""
    collected: list[int] = []
    cursor = start
    for _ in range(pages):
        result = feed.items(FeedQuery.build(limit=limit, cursor=cursor, **kwargs))
        collected.extend(_ids(result))
        cursor = result["next_cursor"]
        if not cursor:
            return collected
    raise AssertionError(f"лента не кончилась за {pages} страниц: {collected}")


# ── порядок ────────────────────────────────────────────────────────────────


def test_the_default_order_is_newest_published_first_with_the_undated_last(feed, corpus):
    result = feed.items(FeedQuery.build(limit=50))

    assert _names(corpus, _ids(result)) == [
        "hidden_digest",
        "npa_high",
        "news_medium",
        "news_low",
        "npa_medium",
        "undated",
    ]


def test_order_priority_puts_high_first_and_sorts_by_date_inside_a_group(feed, corpus):
    result = feed.items(FeedQuery.build(order="priority", limit=50))

    assert _names(corpus, _ids(result)) == [
        "hidden_digest",
        "npa_high",
        "news_medium",
        "npa_medium",
        "news_low",
        "undated",
    ]


def test_order_processed_follows_when_the_card_was_last_built(feed, corpus):
    result = feed.items(FeedQuery.build(order="processed", limit=50))

    assert _names(corpus, _ids(result)) == [
        "news_low",
        "undated",
        "hidden_digest",
        "npa_high",
        "news_medium",
        "npa_medium",
    ]


def test_material_without_a_date_never_falls_out_of_the_feed(feed, corpus):
    """`COALESCE(published_at, '')` уводит недатированное в конец, а не за борт."""
    ids = _ids(feed.items(FeedQuery.build(limit=50)))

    assert ids[-1] == corpus["undated"]
    assert len(ids) == 6


# ── листание ───────────────────────────────────────────────────────────────


def test_paging_the_whole_feed_repeats_nothing_and_skips_nothing(feed, corpus):
    single_page = _ids(feed.items(FeedQuery.build(limit=50)))

    paged = _walk(feed, limit=2)

    assert paged == single_page
    assert len(set(paged)) == len(paged)


@pytest.mark.parametrize(
    "order",
    [
        "published",
        pytest.param(
            "priority",
            marks=pytest.mark.xfail(
                strict=True,
                raises=sqlite3.ProgrammingError,
                reason=(
                    "дефект src/feed/service.py:50 — курсор кодируется как (published_at, id), "
                    "а sort_keys('priority') даёт три ключа, поэтому вторая страница падает на "
                    "числе параметров; готовый sql.cursor_position() при этом не вызывается"
                ),
            ),
        ),
        pytest.param(
            "processed",
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "дефект src/feed/service.py:50 — в курсоре published_at, а сравнивается он "
                    "с COALESCE(processed_at, ''), так что вторая страница молча обрывает ленту"
                ),
            ),
        ),
    ],
    ids=lambda s: s,
)
def test_paging_agrees_with_the_single_page_listing_in_every_order(feed, corpus, order):
    single_page = _ids(feed.items(FeedQuery.build(order=order, limit=50)))

    paged = _walk(feed, limit=2, order=order)

    assert paged == single_page


def test_paging_with_a_filter_stays_inside_the_slice(feed, corpus):
    paged = _walk(feed, limit=1, priority=["high", "medium"])

    assert _names(corpus, paged) == ["hidden_digest", "npa_high", "news_medium", "npa_medium"]


def test_the_last_page_hands_back_no_cursor(feed, corpus):
    last = feed.items(FeedQuery.build(limit=50))

    assert last["next_cursor"] is None
    assert len(last["items"]) == 6


def test_a_page_that_exactly_fills_the_limit_still_ends_the_walk(feed, corpus):
    """Шесть карточек по три: вторая страница полная, но следующей нет."""
    first = feed.items(FeedQuery.build(limit=3))
    second = feed.items(FeedQuery.build(limit=3, cursor=first["next_cursor"]))

    assert len(second["items"]) == 3
    assert second["next_cursor"] is None


def test_each_page_reports_the_total_of_the_whole_slice(feed, corpus):
    first = feed.items(FeedQuery.build(limit=2))
    second = feed.items(FeedQuery.build(limit=2, cursor=first["next_cursor"]))

    assert first["total"] == second["total"] == 6


def test_a_card_inserted_between_pages_neither_duplicates_nor_hides_its_neighbours(
    feed, corpus, card_factory, source_factory
):
    first = feed.items(FeedQuery.build(limit=2))
    latecomer = card_factory(
        source_factory("Опоздавший"),
        title="Пришло, пока читали первую страницу",
        published_at="2026-09-02T09:00:00+00:00",  # между news_low и news_medium
    )

    rest = _walk(feed, limit=2, start=first["next_cursor"])

    assert set(rest) & set(_ids(first)) == set()
    assert latecomer in rest
    assert len(set(rest)) == len(rest)


def test_a_card_added_above_the_cursor_waits_for_the_next_first_page(
    feed, corpus, card_factory, source_factory
):
    """Курсор — позиция, а не смещение: новинка не сдвигает уже прочитанное."""
    first = feed.items(FeedQuery.build(limit=2))
    newest = card_factory(
        source_factory("Опоздавший"),
        title="Свежее всего в ленте",
        published_at="2026-09-05T09:00:00+00:00",
    )

    rest = _walk(feed, limit=2, start=first["next_cursor"])

    assert newest not in rest
    assert _ids(feed.items(FeedQuery.build(limit=1)))[0] == newest


def test_paging_over_include_hidden_covers_every_state(feed, corpus):
    paged = _walk(feed, limit=3, include_hidden=True)

    assert sorted(paged) == sorted(corpus.values())


def test_a_cursor_from_another_slice_is_refused_at_the_service_boundary(feed, corpus):
    token = feed.items(FeedQuery.build(limit=2))["next_cursor"]

    with pytest.raises(QueryError) as excinfo:
        feed.items(FeedQuery.build(limit=2, cursor=token, priority=["high"]))

    assert excinfo.value.code == "invalid_cursor"


def test_an_empty_slice_never_offers_a_cursor(feed, corpus):
    result = feed.items(FeedQuery.build(limit=2, tags=["такого-тега-нет"]))

    assert (result["items"], result["next_cursor"]) == ([], None)
