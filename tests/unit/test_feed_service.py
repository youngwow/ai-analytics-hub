"""src/feed/service.py — лента: форма строки, срез, счётчик и видимость.

`FeedService` — единственная точка входа читающего слоя: и CLI, и HTTP зовут
ровно эти методы, поэтому фильтры проверяются здесь, а не по разу на поверхность.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from src.config import Config
from src.models import ITEM_TAGS, ITEM_TYPES, NPA_STATUSES, PRIORITIES
from src.models.queries import DocumentQuery, FeedQuery
from src.services.feed_service import FeedService, local_today


def _ids(result: dict) -> list[int]:
    return [row["id"] for row in result["items"]]


def _names(corpus: dict[str, int], ids: list[int]) -> list[str]:
    by_id = {item_id: name for name, item_id in corpus.items()}
    return [by_id[i] for i in ids]


# ── форма строки ───────────────────────────────────────────────────────────


def test_a_feed_row_carries_everything_the_dashboard_shows(feed, card_factory, source_factory):
    source = source_factory("Ведомости")
    item_id = card_factory(
        source,
        title="Минцифры внесло законопроект",
        summary="Операторы ИИ обязаны пройти аккредитацию.",
        original="Текст оригинала.",
        tags=("регуляторика",),
        url="https://vedomosti.ru/news/2026/09/04/ai",
        published_at="2026-09-04T09:00:00+00:00",
        type="npa",
        priority="high",
        npa_status="внесён",
        npa_key="112233-8",
        relevance_score=0.86,
        confidence=0.81,
        reasoning="Затрагивает реестр отечественного ПО.",
    )

    row = feed.items(FeedQuery.build())["items"][0]

    assert row == {
        "id": item_id,
        "type": "npa",
        "npa_status": "внесён",
        "npa_key": "112233-8",
        "priority": "high",
        "title": "Минцифры внесло законопроект",
        "summary": "Операторы ИИ обязаны пройти аккредитацию.",
        "tags": ["регуляторика"],
        "published_at": "2026-09-04T09:00:00+00:00",
        "sources_count": 1,
        "canonical_url": "https://vedomosti.ru/news/2026/09/04/ai",
        "source_name": "Ведомости",
        "visibility": "visible",
        "hidden_reason": "",
        "origin": "collected",
        "confidence": pytest.approx(0.81),
        "relevance_score": pytest.approx(0.86),
        "reasoning": "Затрагивает реестр отечественного ПО.",
        "snippet": None,
        "flags": {
            "degraded": False,
            "needs_review": False,
            "date_estimated": False,
            "edited": False,
            "duplicate": False,
        },
    }


@pytest.mark.parametrize(
    ("field", "flag"),
    [
        ("degraded", "degraded"),
        ("needs_review", "needs_review"),
        ("date_estimated", "date_estimated"),
    ],
    ids=["degraded", "needs_review", "date_estimated"],
)
def test_the_flags_block_mirrors_the_stored_columns(feed, card_factory, source_factory, field, flag):
    card_factory(source_factory("Лента"), title="Карточка", **{field: True})

    assert feed.items(FeedQuery.build())["items"][0]["flags"][flag] is True


def test_a_hand_edited_card_is_flagged_as_edited(feed, card_factory, source_factory):
    card_factory(source_factory("Лента"), title="Карточка", manual_overrides=["summary"])

    assert feed.items(FeedQuery.build())["items"][0]["flags"]["edited"] is True


def test_sources_count_reports_how_many_publications_joined_the_card(
    feed, file_db, card_factory, source_factory
):
    item_id = card_factory(source_factory("Ведомости"), title="Одна и та же новость")
    with file_db.transaction():
        file_db.clusters.grow(file_db.items.get(item_id).cluster_id, 2)

    assert feed.items(FeedQuery.build())["items"][0]["sources_count"] == 3


def test_a_card_without_a_canonical_document_still_renders(feed, file_db, card_factory,
                                                            source_factory):
    item_id = card_factory(source_factory("Лента"), title="Карточка")
    with file_db.transaction():
        file_db.conn.execute("UPDATE item_sources SET is_canonical=0 WHERE item_id=?", (item_id,))

    row = feed.items(FeedQuery.build())["items"][0]

    assert (row["canonical_url"], row["source_name"]) == (None, None)


def test_an_empty_slice_is_an_empty_list_and_a_zero_total(feed, corpus):
    result = feed.items(FeedQuery.build(tags=["такого-тега-нет"]))

    assert result["items"] == []
    assert result["total"] == 0
    assert result["next_cursor"] is None


def test_every_answer_reports_how_long_it_took(feed, corpus):
    result = feed.items(FeedQuery.build())

    assert isinstance(result["took_ms"], int)
    assert result["took_ms"] >= 0


# ── total считается по срезу, а не по всей базе ────────────────────────────


def test_total_counts_the_slice_not_the_whole_base(feed, corpus):
    """Дефект, который чинит 1.3: раньше `total` был общим числом карточек."""
    whole_feed = feed.items(FeedQuery.build(limit=1))
    only_npa = feed.items(FeedQuery.build(type="npa", limit=1))

    assert (whole_feed["total"], len(whole_feed["items"])) == (6, 1)
    assert (only_npa["total"], len(only_npa["items"])) == (2, 1)


# ── фильтры ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"type": "npa"}, ["npa_high", "npa_medium"]),
        ({"type": "news"}, ["hidden_digest", "news_medium", "news_low", "undated"]),
        ({"npa_status": "внесён"}, ["npa_high"]),
        ({"type": "npa", "npa_status": "рассмотрение"}, ["npa_medium"]),
        ({"priority": ["low"]}, ["news_low", "undated"]),
        ({"priority": ["high", "medium"]},
         ["hidden_digest", "npa_high", "news_medium", "npa_medium"]),
        ({"tags": ["регуляторика"]}, ["npa_high", "news_medium", "npa_medium"]),
        ({"tags": ["регуляторика", "конкуренты"]}, ["news_medium"]),
        ({"tags": ["конкуренты"]}, ["hidden_digest", "news_medium"]),
        ({"date_from": "2026-09-02", "date_to": "2026-09-03"}, ["news_medium", "news_low"]),
        ({"date_from": "2026-09-04"}, ["hidden_digest", "npa_high"]),
        ({"date_to": "2026-09-01"}, ["npa_medium"]),
    ],
    ids=[
        "type-npa",
        "type-news",
        "npa-status-alone",
        "npa-status-with-type",
        "one-priority",
        "two-priorities-are-or",
        "one-tag",
        "two-tags-are-and",
        "tag-across-visibility",
        "date-range",
        "date-from",
        "date-to",
    ],
)
def test_the_filters_cut_the_slice_the_way_the_contract_says(feed, corpus, kwargs, expected):
    result = feed.items(FeedQuery.build(limit=50, **kwargs))

    assert _names(corpus, _ids(result)) == expected
    assert result["total"] == len(expected)


def test_source_ids_are_an_or_over_sources(feed, corpus, corpus_sources):
    media = corpus_sources["media"].id
    channel = corpus_sources["channel"].id

    one = feed.items(FeedQuery.build(source_ids=[media], limit=50))
    two = feed.items(FeedQuery.build(source_ids=[media, channel], limit=50))

    assert _names(corpus, _ids(one)) == ["hidden_digest", "npa_high", "news_low"]
    assert _names(corpus, _ids(two)) == ["hidden_digest", "npa_high", "news_low", "undated"]


def test_a_card_is_matched_by_any_of_its_sources_not_only_the_canonical_one(
    feed, file_db, corpus, corpus_sources, document_factory
):
    reprint = document_factory(corpus_sources["channel"], title="Перепечатка")
    with file_db.transaction():
        file_db.items.link_sources(corpus["news_medium"], [reprint])

    result = feed.items(FeedQuery.build(source_ids=[corpus_sources["channel"].id], limit=50))

    assert _names(corpus, _ids(result)) == ["news_medium", "undated"]


def test_undated_material_is_excluded_by_a_date_filter_but_kept_in_the_open_feed(feed, corpus):
    assert corpus["undated"] in _ids(feed.items(FeedQuery.build(limit=50)))
    assert corpus["undated"] not in _ids(feed.items(FeedQuery.build(date_from="2026-01-01",
                                                                   limit=50)))


# ── видимость ──────────────────────────────────────────────────────────────


def test_a_card_hidden_from_the_digest_stays_in_the_feed(feed, corpus):
    """Решение владельца от 2026-09-05: подготовка выжимки не чистит ленту всем сразу."""
    assert corpus["hidden_digest"] in _ids(feed.items(FeedQuery.build(limit=50)))


@pytest.mark.parametrize("key", ["hidden_feed", "deleted"], ids=lambda s: s)
def test_a_card_hidden_from_the_feed_or_deleted_is_gone(feed, corpus, key):
    assert corpus[key] not in _ids(feed.items(FeedQuery.build(limit=50)))


def test_include_hidden_brings_every_state_back(feed, corpus):
    result = feed.items(FeedQuery.build(include_hidden=True, limit=50))

    assert sorted(_ids(result)) == sorted(corpus.values())
    assert result["total"] == 8


def test_the_visibility_of_each_row_is_reported_so_the_ui_can_mark_it(feed, corpus):
    rows = {row["id"]: row for row in feed.items(FeedQuery.build(include_hidden=True,
                                                                 limit=50))["items"]}

    assert rows[corpus["hidden_feed"]]["visibility"] == "hidden_feed"
    assert rows[corpus["hidden_feed"]]["hidden_reason"] == "нерелевантно"
    assert rows[corpus["npa_high"]]["visibility"] == "visible"


# ── справочники ────────────────────────────────────────────────────────────


def test_filters_lists_the_sources_the_panel_offers(feed, corpus_sources):
    sources = feed.filters()["sources"]

    assert [s["name"] for s in sources] == ["Ведомости", "Банк России", "Канал ЦИТ"]
    assert sources[2]["kind"] == "telegram"
    assert {s["status"] for s in sources} == {"active"}


def test_filters_hides_a_deleted_source(feed, file_db, corpus_sources):
    file_db.sources.remove(corpus_sources["channel"].id)

    assert [s["name"] for s in feed.filters()["sources"]] == ["Ведомости", "Банк России"]


def test_filters_offers_the_tags_actually_in_use_most_common_first(feed, corpus):
    assert feed.filters()["tags"][0] == "регуляторика"
    assert set(feed.filters()["tags"]) == {"регуляторика", "конкуренты", "тренды"}


def test_filters_falls_back_to_the_vocabulary_when_nothing_is_tagged_yet(feed):
    assert feed.filters()["tags"] == list(ITEM_TAGS)


def test_filters_carries_the_vocabularies_and_the_configured_timezone(feed, config):
    payload = feed.filters()

    assert payload["priorities"] == list(PRIORITIES)
    assert payload["types"] == list(ITEM_TYPES)
    assert payload["npa_statuses"] == list(NPA_STATUSES)
    assert payload["orders"] == ["published", "priority", "processed"]
    assert payload["timezone"] == config.api.timezone == "Europe/Moscow"


def test_the_service_reads_its_timezone_from_the_config(raw_config, file_db):
    raw_config["api"] = {"timezone": "Asia/Novosibirsk"}

    service = FeedService(Config.from_dict(raw_config), file_db)

    assert service.timezone_name == "Asia/Novosibirsk"
    assert service.filters()["timezone"] == "Asia/Novosibirsk"


def test_local_today_is_the_date_in_the_configured_zone(config, monkeypatch):
    import src.services.feed_service as service_mod

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 4, 22, 30, tzinfo=tz)

    monkeypatch.setattr(service_mod, "datetime", FrozenDatetime)

    assert local_today(config) == "2026-09-05"  # 01:30 по Москве — это уже пятое


# ── читающий слой ничего не пишет ──────────────────────────────────────────


def test_a_series_of_reads_leaves_the_database_untouched(feed, file_db, hub_paths, corpus):
    """Приёмка 1.3: `PRAGMA data_version` не двигается, пока слой только читает."""
    observer = sqlite3.connect(hub_paths.db_path)
    try:
        before = observer.execute("PRAGMA data_version").fetchone()[0]

        feed.items(FeedQuery.build(q="законопроект", limit=50))
        feed.items(FeedQuery.build(order="priority", include_hidden=True, limit=50))
        feed.facets(FeedQuery.build())
        feed.filters()
        feed.documents(DocumentQuery.build())
        feed.digest(FeedQuery.build(order="priority"), include_notes=True)
        feed.digest(FeedQuery.build(), fmt="json")
        feed.status()

        after = observer.execute("PRAGMA data_version").fetchone()[0]
        assert after == before
        assert file_db.conn.in_transaction is False

        # А теперь настоящая запись — иначе тест был бы зелёным при сломанной проверке.
        file_db.items.set_visibility(corpus["npa_high"], "hidden_feed")
        assert observer.execute("PRAGMA data_version").fetchone()[0] != before
    finally:
        observer.close()
