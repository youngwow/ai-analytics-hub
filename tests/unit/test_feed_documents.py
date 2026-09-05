"""src/feed/service.py::documents — собрано, но карточки ещё нет (US-12).

Это не лента: у документа нет ни приоритета, ни типа, ни тегов, поэтому фильтры
карточки к нему неприменимы и отклоняются, а не игнорируются молча.
"""

from __future__ import annotations

import pytest

from src.exceptions import QueryError
from src.models.queries import DocumentQuery, FeedQuery

PROBLEM = "application/problem+json"


def _titles(result: dict) -> list[str]:
    return [row["title"] for row in result["documents"]]


def _ids(result: dict) -> list[int]:
    return [row["id"] for row in result["documents"]]


def _walk(feed, *, limit: int, pages: int = 20, start: str | None = None, **kwargs) -> list[int]:
    """Пройти очередь постранично от `start` и вернуть идентификаторы в порядке выдачи."""
    collected: list[int] = []
    cursor = start
    for _ in range(pages):
        result = feed.documents(DocumentQuery.build(limit=limit, cursor=cursor, **kwargs))
        collected.extend(_ids(result))
        cursor = result["next_cursor"]
        if not cursor:
            return collected
    raise AssertionError(f"очередь не кончилась за {pages} страниц: {collected}")


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


# ── курсор ─────────────────────────────────────────────────────────────────


@pytest.fixture
def queue(document_factory, corpus_sources, corpus) -> list[int]:
    """Семь документов без карточки: два с одной датой, один без даты — в порядке выдачи."""
    media, regulator, channel = (
        corpus_sources["media"], corpus_sources["regulator"], corpus_sources["channel"]
    )
    seventh = document_factory(media, title="Седьмой", published_at="2026-09-04T18:00:00+00:00")
    sixth = document_factory(regulator, title="Шестой", published_at="2026-09-04T15:00:00+00:00")
    fifth_a = document_factory(media, title="Пятый-а", published_at="2026-09-03T12:00:00+00:00")
    fifth_b = document_factory(media, title="Пятый-б", published_at="2026-09-03T12:00:00+00:00")
    third = document_factory(regulator, title="Третий", published_at="2026-09-02T09:00:00+00:00")
    second = document_factory(media, title="Второй", published_at="2026-09-01T09:00:00+00:00")
    undated = document_factory(channel, title="Без даты", published_at=None)
    # При равной дате новее тот, у кого больше id — так же, как в ленте.
    return [seventh, sixth, fifth_b, fifth_a, third, second, undated]


def test_the_single_page_order_is_newest_first_ties_by_id_and_the_undated_last(feed, queue):
    assert _ids(feed.documents(DocumentQuery.build(limit=50))) == queue


def test_paging_the_queue_repeats_nothing_skips_nothing_and_keeps_the_order(feed, queue):
    paged = _walk(feed, limit=3)

    assert paged == queue
    assert len(set(paged)) == len(paged) == 7


def test_the_first_page_offers_a_cursor_and_the_last_page_none(feed, queue):
    first = feed.documents(DocumentQuery.build(limit=3))
    whole = feed.documents(DocumentQuery.build(limit=50))

    assert first["next_cursor"] is not None
    assert len(first["documents"]) == 3
    assert whole["next_cursor"] is None
    assert len(whole["documents"]) == 7


def test_a_page_that_exactly_fills_the_limit_still_ends_the_walk(feed, unprocessed):
    """Три документа по три: страница полная, но следующей нет."""
    result = feed.documents(DocumentQuery.build(limit=3))

    assert len(result["documents"]) == 3
    assert result["next_cursor"] is None


def test_each_page_reports_the_total_of_the_whole_queue(feed, queue):
    first = feed.documents(DocumentQuery.build(limit=3))
    second = feed.documents(DocumentQuery.build(limit=3, cursor=first["next_cursor"]))
    third = feed.documents(DocumentQuery.build(limit=3, cursor=second["next_cursor"]))

    assert (first["total"], second["total"], third["total"]) == (7, 7, 7)
    assert (len(first["documents"]), len(second["documents"]), len(third["documents"])) == (3, 3, 1)
    assert third["next_cursor"] is None


def test_a_cursor_past_the_last_row_gives_an_empty_page_with_the_full_total(feed, queue):
    beyond = DocumentQuery.build(limit=3).encode_cursor(None, queue[-1])

    result = feed.documents(DocumentQuery.build(limit=3, cursor=beyond))

    assert (result["documents"], result["next_cursor"], result["total"]) == ([], None, 7)


def test_two_documents_with_the_same_date_are_not_split_by_a_page_of_one(feed, queue):
    paged = _walk(feed, limit=1)

    assert paged == queue
    assert paged.index(queue[2]) + 1 == paged.index(queue[3])  # Пятый-б, потом Пятый-а


def test_the_undated_document_is_reached_last_by_the_cursor(feed, queue):
    assert _walk(feed, limit=2)[-1] == queue[-1]


def test_paging_with_a_filter_stays_inside_the_slice(feed, queue, corpus_sources):
    paged = _walk(feed, limit=1, source_ids=[corpus_sources["regulator"].id])

    assert paged == [queue[1], queue[4]]


def test_a_document_inserted_between_pages_neither_duplicates_nor_hides_its_neighbours(
    feed, queue, document_factory, corpus_sources
):
    first = feed.documents(DocumentQuery.build(limit=3))
    latecomer = document_factory(
        corpus_sources["media"],
        title="Пришёл, пока читали первую страницу",
        published_at="2026-09-02T12:00:00+00:00",  # между «Третьим» и «Пятыми»
    )

    rest = _walk(feed, limit=3, start=first["next_cursor"])

    assert set(rest) & set(_ids(first)) == set()
    assert latecomer in rest
    assert len(set(rest)) == len(rest)


def test_a_document_added_above_the_cursor_waits_for_the_next_first_page(
    feed, queue, document_factory, corpus_sources
):
    first = feed.documents(DocumentQuery.build(limit=3))
    newest = document_factory(
        corpus_sources["media"], title="Свежее всего", published_at="2026-09-05T09:00:00+00:00"
    )

    rest = _walk(feed, limit=3, start=first["next_cursor"])

    assert newest not in rest
    assert _ids(feed.documents(DocumentQuery.build(limit=1))) == [newest]


@pytest.mark.parametrize(
    "cursor", ["!!!!", "не-курсор-вовсе", "eyJwIjogbnVsbH0="],
    ids=["not-base64", "cyrillic", "missing-keys"],
)
def test_a_malformed_cursor_is_an_invalid_cursor_error(feed, queue, cursor):
    with pytest.raises(QueryError, match="курсор не разбирается") as excinfo:
        feed.documents(DocumentQuery.build(limit=3, cursor=cursor))

    assert (excinfo.value.code, excinfo.value.status_code) == ("invalid_cursor", 400)


def test_a_cursor_from_another_slice_is_refused(feed, queue, corpus_sources):
    token = feed.documents(DocumentQuery.build(limit=3))["next_cursor"]

    with pytest.raises(QueryError, match="другому набору фильтров") as excinfo:
        feed.documents(
            DocumentQuery.build(limit=3, cursor=token, source_ids=[corpus_sources["regulator"].id])
        )

    assert excinfo.value.code == "invalid_cursor"


def test_a_feed_cursor_is_refused_by_the_document_list(feed, corpus, queue):
    token = feed.items(FeedQuery.build(limit=2))["next_cursor"]

    with pytest.raises(QueryError) as excinfo:
        feed.documents(DocumentQuery.build(limit=2, cursor=token))

    assert excinfo.value.code == "invalid_cursor"


# ── DocumentQuery: отпечаток и курсор ──────────────────────────────────────


def test_the_document_fingerprint_ignores_limit_and_cursor_but_not_the_filters():
    base = DocumentQuery.build(q="реестр", limit=5).fingerprint()

    assert DocumentQuery.build(q="реестр", limit=50).fingerprint() == base
    assert DocumentQuery.build(q="реестр", cursor="x").fingerprint() == base
    assert DocumentQuery.build(q="закон").fingerprint() != base
    assert DocumentQuery.build(q="реестр", source_ids=[1]).fingerprint() != base
    assert DocumentQuery.build(q="реестр", date_from="2026-09-01").fingerprint() != base
    assert DocumentQuery.build(q="реестр", date_to="2026-09-01").fingerprint() != base


def test_the_document_fingerprint_differs_from_the_feed_one_for_the_same_filters():
    assert DocumentQuery.build(q="реестр").fingerprint() != FeedQuery.build(q="реестр").fingerprint()


def test_a_document_cursor_round_trips_the_position_of_the_last_row():
    query = DocumentQuery.build(source_ids=[4, 17])
    token = query.encode_cursor("2026-09-04T09:00:00+00:00", 7)

    position = DocumentQuery.build(source_ids=[17, 4], cursor=token).decode_cursor()

    assert position == ("2026-09-04T09:00:00+00:00", 7)


def test_a_document_cursor_keeps_a_missing_date_as_none():
    token = DocumentQuery.build().encode_cursor(None, 3)

    assert DocumentQuery.build(cursor=token).decode_cursor() == (None, 3)


def test_without_a_cursor_there_is_no_position():
    assert DocumentQuery.build().decode_cursor() is None
    assert DocumentQuery.build(cursor="").decode_cursor() is None


# ── HTTP ───────────────────────────────────────────────────────────────────


def test_the_document_cursor_pages_over_http(client, queue):
    collected: list[int] = []
    cursor = None
    for _ in range(10):
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        body = client.get("/api/v1/documents", params=params).json()
        assert body["total"] == 7
        collected.extend(row["id"] for row in body["documents"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert collected == queue
    assert len(set(collected)) == 7


def test_the_documents_response_carries_next_cursor_even_for_an_empty_queue(client, corpus):
    body = client.get("/api/v1/documents").json()

    assert body == {"documents": [], "total": 0, "next_cursor": None, "took_ms": body["took_ms"]}


@pytest.mark.parametrize(
    "cursor", ["!!!!", "не-курсор-вовсе", "eyJwIjogbnVsbH0="],
    ids=["not-base64", "cyrillic", "missing-keys"],
)
def test_a_malformed_document_cursor_is_an_invalid_cursor_problem(client, queue, cursor):
    response = client.get("/api/v1/documents", params={"cursor": cursor})

    assert response.status_code == 400
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "invalid_cursor"


def test_a_document_cursor_from_another_slice_is_refused_over_http(client, queue, corpus_sources):
    token = client.get("/api/v1/documents", params={"limit": 3}).json()["next_cursor"]

    response = client.get(
        "/api/v1/documents",
        params={"limit": 3, "cursor": token, "source_id": corpus_sources["regulator"].id},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_cursor"


def test_a_feed_cursor_is_refused_by_the_documents_endpoint(client, corpus, queue):
    token = client.get("/api/v1/items", params={"limit": 2}).json()["next_cursor"]

    response = client.get("/api/v1/documents", params={"limit": 2, "cursor": token})

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_cursor"
