"""src/feed/service.py::documents — собрано, но карточки ещё нет (US-12).

Это не лента: у документа нет ни приоритета, ни типа, ни тегов, поэтому фильтры
карточки к нему неприменимы и отклоняются, а не игнорируются молча.
"""

from __future__ import annotations

import pytest

from src.exceptions import QueryError
from src.models.queries import DOCUMENT_ORDERS, DocumentQuery, FeedQuery

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
    assert row["fetched_at"] == "2026-09-05T09:00:00+00:00"
    assert row["last_error"] == ""
    assert row["chars"] == len("Полный текст на сорок символов ровно.")
    assert row["url"].endswith("free-1")


def test_the_row_shows_why_a_document_is_still_here(feed, document_factory, corpus_sources):
    """Без `last_error` «упал в прошлом прогоне» и «ещё не брали» выглядят одинаково."""
    document_factory(
        corpus_sources["media"], title="Упал в прошлом прогоне", last_error="RuntimeError: боль"
    )

    row = feed.documents(DocumentQuery.build(limit=50))["documents"][0]

    assert row["last_error"] == "RuntimeError: боль"


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


# ── поиск по подстроке: шаблонные символы ищутся как символы ───────────────


@pytest.fixture
def wildcards(document_factory, corpus_sources) -> dict[str, int]:
    """Заголовки, в которых живут `%`, `_` и `\\` — то, на чём ломался `LIKE` без ESCAPE."""
    media = corpus_sources["media"]
    return {
        "percent": document_factory(media, title="Скидка 100% на подписку"),
        "underscore": document_factory(media, title="Файл отчёт_2026 подготовлен"),
        "backslash": document_factory(media, title="Путь C:\\отчёты готов"),
        "plain": document_factory(media, title="Обычный документ без знаков"),
    }


@pytest.mark.parametrize(
    ("query", "key"),
    [
        ("100%", "percent"),
        ("%", "percent"),
        ("отчёт_2026", "underscore"),
        ("_", "underscore"),
        ("C:\\отчёты", "backslash"),
        ("\\", "backslash"),
    ],
    ids=["percent-in-context", "bare-percent", "underscore-in-context", "bare-underscore",
         "backslash-in-context", "bare-backslash"],
)
def test_a_like_wildcard_in_the_query_is_matched_literally(feed, wildcards, query, key):
    result = feed.documents(DocumentQuery.build(q=query, limit=50))

    assert _ids(result) == [wildcards[key]]
    assert result["total"] == 1


def test_an_underscore_does_not_stand_for_any_character(feed, wildcards):
    """До ESCAPE «отчёт_2026» нашло бы и «отчёт 2026», и «отчётX2026»."""
    assert _titles(feed.documents(DocumentQuery.build(q="отчёт_", limit=50))) == [
        "Файл отчёт_2026 подготовлен"
    ]


def test_a_percent_does_not_match_everything(feed, wildcards):
    result = feed.documents(DocumentQuery.build(q="100%%", limit=50))

    assert result["total"] == 0


def test_a_plain_substring_still_matches(feed, wildcards):
    assert _ids(feed.documents(DocumentQuery.build(q="подписку", limit=50))) == [
        wildcards["percent"]
    ]


# ── порядок: по публикации или по времени сбора ────────────────────────────


@pytest.fixture
def by_fetch(document_factory, corpus_sources) -> dict[str, int]:
    """Свежая публикация, забранная позже всех, и старая, забранная первой."""
    media = corpus_sources["media"]
    return {
        "published_first": document_factory(
            media,
            title="Опубликован раньше, забран позже",
            published_at="2026-09-01T09:00:00+00:00",
            fetched_at="2026-09-05T12:00:00+00:00",
        ),
        "fetched_first": document_factory(
            media,
            title="Опубликован позже, забран раньше",
            published_at="2026-09-04T09:00:00+00:00",
            fetched_at="2026-09-05T06:00:00+00:00",
        ),
    }


def test_the_default_order_is_still_by_publication(feed, by_fetch):
    assert _ids(feed.documents(DocumentQuery.build(limit=50))) == [
        by_fetch["fetched_first"], by_fetch["published_first"]
    ]


def test_the_fetch_order_lists_what_arrived_last_first(feed, by_fetch):
    """Очередь дежурного — это «что приехало», а не «что напечатали»."""
    result = feed.documents(DocumentQuery.build(order="fetched", limit=50))

    assert _ids(result) == [by_fetch["published_first"], by_fetch["fetched_first"]]


def test_paging_in_fetch_order_repeats_nothing_and_keeps_the_order(feed, queue, by_fetch):
    paged = _walk(feed, limit=2, order="fetched")

    assert len(set(paged)) == len(paged) == 9
    assert paged[:2] == _ids(feed.documents(DocumentQuery.build(order="fetched", limit=2)))


def test_the_sort_field_follows_the_order(feed):
    assert DocumentQuery.build().sort_field == "published_at"
    assert DocumentQuery.build(order="published").sort_field == "published_at"
    assert DocumentQuery.build(order="fetched").sort_field == "fetched_at"


def test_the_orders_the_queue_knows(feed):
    assert DOCUMENT_ORDERS == ("published", "fetched")


@pytest.mark.parametrize("order", ["priority", "processed", "FETCHED", "дата"],
                         ids=["feed-order", "feed-order-2", "upper", "cyrillic"])
def test_an_unknown_order_is_a_validation_error(order):
    with pytest.raises(QueryError, match="order") as excinfo:
        DocumentQuery.build(order=order, limit=50)

    assert excinfo.value.code == "validation_error"


@pytest.mark.parametrize("order", [None, ""], ids=["absent", "empty"])
def test_no_order_at_all_means_the_default_one(order):
    """`?order=` из формы — это «не задано», а не ошибка."""
    assert DocumentQuery.build(order=order, limit=50).order == "published"


def test_the_order_is_part_of_the_cursor_fingerprint(feed, queue):
    token = feed.documents(DocumentQuery.build(limit=3))["next_cursor"]

    with pytest.raises(QueryError, match="другому набору фильтров") as excinfo:
        feed.documents(DocumentQuery.build(limit=3, cursor=token, order="fetched"))

    assert excinfo.value.code == "invalid_cursor"


def test_a_fetch_order_cursor_is_refused_by_the_default_order(feed, queue):
    token = feed.documents(DocumentQuery.build(limit=3, order="fetched"))["next_cursor"]

    with pytest.raises(QueryError, match="другому набору фильтров"):
        feed.documents(DocumentQuery.build(limit=3, cursor=token))


def test_the_fingerprint_of_the_two_orders_differs():
    assert DocumentQuery.build().fingerprint() != DocumentQuery.build(order="fetched").fingerprint()


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


def test_the_fetch_order_is_available_over_http(client, by_fetch):
    body = client.get("/api/v1/documents", params={"order": "fetched", "limit": 50}).json()

    assert [row["id"] for row in body["documents"]] == [
        by_fetch["published_first"], by_fetch["fetched_first"]
    ]


@pytest.mark.parametrize("order", ["priority", "дата"], ids=["feed-order", "cyrillic"])
def test_an_unknown_order_is_a_400_problem(client, queue, order):
    response = client.get("/api/v1/documents", params={"order": order})

    assert response.status_code == 400
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "validation_error"


def test_an_empty_order_in_the_query_string_falls_back_to_the_default(client, by_fetch):
    body = client.get("/api/v1/documents", params={"order": "", "limit": 50}).json()

    assert [row["id"] for row in body["documents"]] == [
        by_fetch["fetched_first"], by_fetch["published_first"]
    ]


def test_a_cursor_from_the_other_order_is_refused_over_http(client, queue):
    token = client.get("/api/v1/documents", params={"limit": 3}).json()["next_cursor"]

    response = client.get(
        "/api/v1/documents", params={"limit": 3, "cursor": token, "order": "fetched"}
    )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_cursor"


def test_the_document_row_carries_the_fetch_time_and_the_last_error_over_http(
    client, document_factory, corpus_sources
):
    document_factory(
        corpus_sources["media"],
        title="Упал в прошлом прогоне",
        fetched_at="2026-09-05T06:00:00+00:00",
        last_error="RuntimeError: боль",
    )

    row = client.get("/api/v1/documents").json()["documents"][0]

    assert row["fetched_at"] == "2026-09-05T06:00:00+00:00"
    assert row["last_error"] == "RuntimeError: боль"


def test_the_query_escapes_a_wildcard_over_http(client, document_factory, corpus_sources):
    wanted = document_factory(corpus_sources["media"], title="Скидка 100% на подписку")
    document_factory(corpus_sources["media"], title="Обычный документ")

    body = client.get("/api/v1/documents", params={"q": "100%"}).json()

    assert [row["id"] for row in body["documents"]] == [wanted]
    assert body["total"] == 1
