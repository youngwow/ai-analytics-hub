"""src/feed/service.py::facets — счётчики того же среза, что и лента.

Смысл фасетов в том, что цифра рядом с фильтром совпадает с тем, что фильтр
покажет. Поэтому каждый счётчик здесь сверяется с `total` соответствующей ленты.
"""

from __future__ import annotations

import pytest

from src.feed.query import FeedQuery
from src.feed.service import FACET_SOURCES_LIMIT, FACET_TAGS_LIMIT


def _by_source(facets: dict) -> dict[str, int]:
    return {row["name"]: row["count"] for row in facets["by_source"]}


def _by_tag(facets: dict) -> dict[str, int]:
    return {row["tag"]: row["count"] for row in facets["top_tags"]}


# ── счётчики совпадают со срезами ──────────────────────────────────────────


@pytest.mark.parametrize("priority", ["high", "medium", "low"], ids=lambda s: s)
def test_by_priority_equals_the_total_of_that_priority_slice(feed, corpus, priority):
    facets = feed.facets(FeedQuery.build())

    assert facets["by_priority"][priority] == feed.items(FeedQuery.build(priority=[priority]))[
        "total"
    ]


@pytest.mark.parametrize("kind", ["npa", "news"], ids=lambda s: s)
def test_by_type_equals_the_total_of_that_type_slice(feed, corpus, kind):
    facets = feed.facets(FeedQuery.build())

    assert facets["by_type"][kind] == feed.items(FeedQuery.build(type=kind))["total"]


def test_the_facet_totals_add_up_to_the_feed_total(feed, corpus):
    facets = feed.facets(FeedQuery.build())

    assert facets["total"] == feed.items(FeedQuery.build())["total"] == 6
    assert sum(facets["by_type"].values()) == 6


def test_the_counters_have_the_shape_the_corpus_dictates(feed, corpus):
    facets = feed.facets(FeedQuery.build())

    assert facets["by_priority"] == {"high": 2, "medium": 2, "low": 2}
    assert facets["by_type"] == {"npa": 2, "news": 4}
    assert _by_source(facets) == {"Ведомости": 3, "Банк России": 2, "Канал ЦИТ": 1}
    assert _by_tag(facets) == {"регуляторика": 3, "конкуренты": 2, "тренды": 1}


def test_a_priority_absent_from_the_slice_is_reported_as_zero_not_dropped(feed, corpus):
    facets = feed.facets(FeedQuery.build(type="npa"))

    assert facets["by_priority"] == {"high": 1, "medium": 1, "low": 0}
    assert facets["by_type"] == {"npa": 2, "news": 0}


def test_facets_follow_the_same_filters_as_the_feed(feed, corpus):
    facets = feed.facets(FeedQuery.build(tags=["регуляторика"]))

    assert facets["total"] == feed.items(FeedQuery.build(tags=["регуляторика"]))["total"] == 3
    assert facets["by_priority"] == {"high": 1, "medium": 2, "low": 0}
    assert _by_source(facets) == {"Банк России": 2, "Ведомости": 1}


def test_facets_follow_the_visibility_rules_of_the_feed(feed, corpus):
    visible = feed.facets(FeedQuery.build())
    everything = feed.facets(FeedQuery.build(include_hidden=True))

    assert visible["total"] == 6
    assert everything["total"] == 8
    assert everything["by_priority"]["high"] == 3  # +hidden_feed


def test_facets_of_an_empty_slice_are_zeroes_and_empty_lists(feed, corpus):
    facets = feed.facets(FeedQuery.build(tags=["такого-тега-нет"]))

    assert facets["total"] == 0
    assert facets["by_priority"] == {"high": 0, "medium": 0, "low": 0}
    assert facets["by_type"] == {"npa": 0, "news": 0}
    assert (facets["by_source"], facets["top_tags"]) == ([], [])


def test_facets_report_how_long_they_took(feed, corpus):
    assert feed.facets(FeedQuery.build())["took_ms"] >= 0


# ── фасеты учитывают поиск ─────────────────────────────────────────────────


def test_facets_narrow_with_the_search_query(feed, corpus):
    query = FeedQuery.build(q="законопроект")

    facets = feed.facets(query)

    assert facets["total"] == feed.items(query)["total"] == 1
    assert facets["by_priority"] == {"high": 1, "medium": 0, "low": 0}
    assert facets["by_type"] == {"npa": 1, "news": 0}
    assert _by_source(facets) == {"Ведомости": 1}
    assert _by_tag(facets) == {"регуляторика": 1}


def test_a_search_that_matches_nothing_zeroes_every_facet(feed, corpus):
    facets = feed.facets(FeedQuery.build(q="криптовалюта"))

    assert facets["total"] == 0
    assert (facets["by_source"], facets["top_tags"]) == ([], [])


def test_search_and_filters_stack_in_the_facets_too(feed, corpus):
    query = FeedQuery.build(q="законопроект", type="news")

    assert feed.facets(query)["total"] == feed.items(query)["total"] == 0


# ── длина списков ──────────────────────────────────────────────────────────


def test_by_source_is_capped_and_ordered_by_count(feed, card_factory, source_factory):
    for number in range(FACET_SOURCES_LIMIT + 5):
        source = source_factory(f"Источник {number:02d}")
        for _ in range(number % 3 + 1):
            card_factory(source, title=f"Материал {number}")

    facets = feed.facets(FeedQuery.build())

    assert len(facets["by_source"]) == FACET_SOURCES_LIMIT == 20
    counts = [row["count"] for row in facets["by_source"]]
    assert counts == sorted(counts, reverse=True)


def test_top_tags_is_capped_and_ordered_by_count(feed, card_factory, source_factory):
    source = source_factory("Ведомости")
    tags = [f"тег-{number:02d}" for number in range(FACET_TAGS_LIMIT + 5)]
    card_factory(source, title="Первая", tags=tuple(tags))
    card_factory(source, title="Вторая", tags=(tags[0],))

    facets = feed.facets(FeedQuery.build())

    assert len(facets["top_tags"]) == FACET_TAGS_LIMIT == 15
    assert facets["top_tags"][0] == {"tag": tags[0], "count": 2}
    counts = [row["count"] for row in facets["top_tags"]]
    assert counts == sorted(counts, reverse=True)
