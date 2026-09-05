"""src/feed/service.py::documents — собрано, но карточки ещё нет (US-12).

Это не лента: у документа нет ни приоритета, ни типа, ни тегов, поэтому фильтры
карточки к нему неприменимы и отклоняются, а не игнорируются молча.
"""

from __future__ import annotations

import pytest

from src.exceptions import QueryError
from src.models.queries import DocumentQuery


def _titles(result: dict) -> list[str]:
    return [row["title"] for row in result["documents"]]


@pytest.fixture
def unprocessed(document_factory, corpus_sources, corpus) -> dict[str, int]:
    """Три документа без карточки рядом с восемью, у которых карточка есть."""
    return {
        "fresh": document_factory(
            corpus_sources["media"],
            title="Свежий документ без карточки",
            text="Полный текст на сорок символов ровно.",
            published_at="2026-09-04T15:00:00+00:00",
        ),
        "older": document_factory(
            corpus_sources["regulator"],
            title="Документ постарше",
            published_at="2026-09-02T15:00:00+00:00",
        ),
        "undated": document_factory(
            corpus_sources["channel"], title="Документ без даты", published_at=None
        ),
    }


def test_only_documents_without_a_card_are_listed(feed, unprocessed):
    result = feed.documents(DocumentQuery.build(limit=50))

    assert sorted(row["id"] for row in result["documents"]) == sorted(unprocessed.values())
    assert result["total"] == 3


def test_a_document_leaves_the_list_the_moment_it_gets_a_card(
    feed, file_db, unprocessed, item_factory
):
    item_factory(file_db, unprocessed["fresh"], title="Карточка появилась")

    result = feed.documents(DocumentQuery.build(limit=50))

    assert unprocessed["fresh"] not in [row["id"] for row in result["documents"]]
    assert result["total"] == 2


def test_a_hidden_document_is_not_offered_for_processing(feed, document_factory, corpus_sources):
    document_factory(corpus_sources["media"], title="Скрытый", hidden=1)
    document_factory(corpus_sources["media"], title="Обычный")

    assert _titles(feed.documents(DocumentQuery.build(limit=50))) == ["Обычный"]


def test_the_row_carries_what_the_unprocessed_list_shows(feed, unprocessed, corpus_sources):
    row = next(
        r
        for r in feed.documents(DocumentQuery.build(limit=50))["documents"]
        if r["id"] == unprocessed["fresh"]
    )

    assert row["title"] == "Свежий документ без карточки"
    assert row["source_id"] == corpus_sources["media"].id
    assert row["source_name"] == "Ведомости"
    assert row["published_at"] == "2026-09-04T15:00:00+00:00"
    assert row["chars"] == len("Полный текст на сорок символов ровно.")
    assert row["url"].endswith("free-1")


def test_the_newest_document_comes_first_and_the_undated_one_last(feed, unprocessed):
    assert _titles(feed.documents(DocumentQuery.build(limit=50))) == [
        "Свежий документ без карточки",
        "Документ постарше",
        "Документ без даты",
    ]


def test_the_limit_cuts_the_page_but_not_the_total(feed, unprocessed):
    result = feed.documents(DocumentQuery.build(limit=1))

    assert len(result["documents"]) == 1
    assert result["total"] == 3


def test_an_empty_list_is_a_zero_total_not_an_error(feed, corpus):
    result = feed.documents(DocumentQuery.build(limit=50))

    assert (result["documents"], result["total"]) == ([], 0)


def test_the_answer_reports_how_long_it_took(feed, unprocessed):
    assert feed.documents(DocumentQuery.build())["took_ms"] >= 0


# ── фильтры ────────────────────────────────────────────────────────────────


def test_the_source_filter_narrows_the_list(feed, unprocessed, corpus_sources):
    result = feed.documents(
        DocumentQuery.build(source_ids=[corpus_sources["regulator"].id], limit=50)
    )

    assert _titles(result) == ["Документ постарше"]
    assert result["total"] == 1


def test_several_sources_are_an_or(feed, unprocessed, corpus_sources):
    result = feed.documents(
        DocumentQuery.build(
            source_ids=[corpus_sources["regulator"].id, corpus_sources["channel"].id], limit=50
        )
    )

    assert _titles(result) == ["Документ постарше", "Документ без даты"]


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"date_from": "2026-09-03"}, ["Свежий документ без карточки"]),
        ({"date_to": "2026-09-03"}, ["Документ постарше"]),
        (
            {"date_from": "2026-09-02", "date_to": "2026-09-04"},
            ["Свежий документ без карточки", "Документ постарше"],
        ),
        ({"date_from": "2026-09-05"}, []),
    ],
    ids=["from", "to", "range", "empty-range"],
)
def test_the_date_filters_use_local_days(feed, unprocessed, kwargs, expected):
    result = feed.documents(DocumentQuery.build(limit=50, **kwargs))

    assert _titles(result) == expected
    assert result["total"] == len(expected)


def test_the_query_matches_the_document_title(feed, unprocessed):
    result = feed.documents(DocumentQuery.build(q="постарше", limit=50))

    assert _titles(result) == ["Документ постарше"]
    assert result["total"] == 1


# ── неприменимые фильтры ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("unsupported", "expected"),
    [
        ({"priority": ["high"]}, "priority"),
        ({"type": "npa"}, "type"),
        ({"tag": ["регуляторика"]}, "tag"),
        ({"npa_status": "внесён"}, "npa_status"),
    ],
    ids=["priority", "type", "tag", "npa_status"],
)
def test_a_card_filter_is_a_validation_error_not_a_silent_no_op(unsupported, expected):
    with pytest.raises(QueryError) as excinfo:
        DocumentQuery.build(unsupported=unsupported, limit=50)

    assert excinfo.value.code == "validation_error"
    assert expected in excinfo.value.message


def test_the_document_list_still_enforces_the_limit_range():
    with pytest.raises(QueryError, match="limit должен быть в диапазоне") as excinfo:
        DocumentQuery.build(limit=500)

    assert excinfo.value.code == "validation_error"
