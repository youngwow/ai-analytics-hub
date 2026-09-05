"""src/feed/query.py — единая форма фильтра: границы суток, проверки, курсор.

Ни одного обращения к базе: `FeedQuery` — чистая структура, и именно поэтому
CLI и HTTP не могут разойтись в том, что значит «за сегодня».
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import pytest

from src.exceptions import QueryError
from src.models.queries import DEFAULT_LIMIT, MAX_LIMIT, DocumentQuery, FeedQuery


def _utc(*parts) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)


# ── голая дата: местные сутки целиком ──────────────────────────────────────


@pytest.mark.parametrize(
    ("zone", "expected_from", "expected_to"),
    [
        ("Europe/Moscow", _utc(2026, 9, 4, 21), _utc(2026, 9, 5, 20, 59, 59, 999999)),
        ("UTC", _utc(2026, 9, 5, 0), _utc(2026, 9, 5, 23, 59, 59, 999999)),
    ],
    ids=["moscow", "utc"],
)
def test_a_bare_date_becomes_the_whole_local_day_in_utc(zone, expected_from, expected_to):
    query = FeedQuery.build(date_from="2026-09-05", date_to="2026-09-05", timezone_name=zone)

    assert query.date_from == expected_from
    assert query.date_to == expected_to


def test_moscow_midnight_and_the_previous_evening_fall_on_different_days():
    """00:30 по Москве — это уже «сегодня», 23:30 накануне — ещё нет (сценарий 6)."""
    query = FeedQuery.build(date_from="2026-09-05", timezone_name="Europe/Moscow")

    assert _utc(2026, 9, 4, 21, 30) >= query.date_from  # 00:30 MSK 5 сентября
    assert _utc(2026, 9, 4, 20, 30) < query.date_from  # 23:30 MSK 4 сентября


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-05T10:00:00+03:00", _utc(2026, 9, 5, 7)),
        ("2026-09-05T07:00:00Z", _utc(2026, 9, 5, 7)),
        ("2026-09-05T07:00:00+00:00", _utc(2026, 9, 5, 7)),
        ("2026-09-05T02:00:00-05:00", _utc(2026, 9, 5, 7)),
    ],
    ids=["msk-offset", "zulu", "utc-offset", "negative-offset"],
)
def test_a_timestamp_with_an_offset_is_taken_as_sent(value, expected):
    assert FeedQuery.build(date_from=value, timezone_name="Europe/Moscow").date_from == expected


def test_a_naive_timestamp_follows_the_projects_moscow_convention():
    """`common.parse_datetime` трактует наивное время как московское, зона фильтра тут не участвует."""
    query = FeedQuery.build(date_from="2026-09-05T10:00:00", timezone_name="UTC")

    assert query.date_from == _utc(2026, 9, 5, 7)


@pytest.mark.parametrize("value", [None, ""], ids=["none", "empty"])
def test_an_absent_date_leaves_the_boundary_open(value):
    query = FeedQuery.build(date_from=value, date_to=value)

    assert (query.date_from, query.date_to) == (None, None)


@pytest.mark.parametrize(
    "value", ["2026-13-45", "05.09.2026", "вчера", "2026-09-05T99:00:00+00:00"],
    ids=["impossible-day", "russian-format", "cyrillic", "impossible-hour"],
)
def test_an_unparseable_date_is_a_validation_error(value):
    with pytest.raises(QueryError, match="дата не разбирается") as excinfo:
        FeedQuery.build(date_from=value)
    assert excinfo.value.code == "validation_error"


# ── проверки ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("limit", [1, 20, MAX_LIMIT], ids=str)
def test_a_limit_inside_the_range_is_accepted(limit):
    assert FeedQuery.build(limit=limit).limit == limit


def test_an_absent_limit_falls_back_to_the_default():
    assert FeedQuery.build().limit == DEFAULT_LIMIT == 20


@pytest.mark.parametrize("limit", [0, -1, 201, 500], ids=str)
def test_a_limit_outside_the_range_is_refused(limit):
    with pytest.raises(QueryError, match=rf"limit должен быть в диапазоне 1\.\.{MAX_LIMIT}"):
        FeedQuery.build(limit=limit)


def test_npa_status_cannot_be_combined_with_type_news():
    with pytest.raises(QueryError, match="npa_status не применяется к type=news") as excinfo:
        FeedQuery.build(type="news", npa_status="внесён")
    assert excinfo.value.code == "validation_error"


def test_npa_status_is_fine_with_type_npa_and_on_its_own():
    assert FeedQuery.build(type="npa", npa_status="внесён").npa_status == "внесён"
    assert FeedQuery.build(npa_status="внесён").type is None


def test_from_later_than_to_is_refused():
    with pytest.raises(QueryError, match="from не может быть позже to"):
        FeedQuery.build(date_from="2026-09-05", date_to="2026-09-01")


def test_one_local_day_is_a_valid_range_even_though_from_and_to_are_equal():
    query = FeedQuery.build(date_from="2026-09-05", date_to="2026-09-05")
    assert query.date_from < query.date_to


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"priority": ["urgent"]}, "priority: ожидалось одно из"),
        ({"type": "law"}, "type: ожидалось одно из"),
        ({"order": "relevance"}, "order: ожидалось одно из"),
        ({"npa_status": "подписан"}, "npa_status: ожидалось одно из"),
        ({"priority": ["high", "HIGH"]}, "priority: ожидалось одно из"),
    ],
    ids=["priority", "type", "order", "npa_status", "case-sensitive"],
)
def test_a_value_outside_the_vocabulary_is_refused(kwargs, expected):
    with pytest.raises(QueryError, match=expected) as excinfo:
        FeedQuery.build(**kwargs)
    assert excinfo.value.code == "validation_error"


def test_repeated_and_blank_values_collapse_into_a_clean_tuple():
    query = FeedQuery.build(
        priority=["high", "high", None, ""],
        tags=["регуляторика", "регуляторика", ""],
        source_ids=["4", "4", "17"],
    )

    assert query.priority == ("high",)
    assert query.tags == ("регуляторика",)
    assert query.source_ids == (4, 17)


def test_the_search_string_is_stripped_and_the_defaults_are_the_open_feed():
    query = FeedQuery.build(q="  законопроект  ")

    assert query.q == "законопроект"
    assert (query.order, query.include_hidden, query.cursor) == ("published", False, None)


# ── отпечаток среза ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({"priority": ["high", "medium"]}, {"priority": ["medium", "high"]}),
        ({"tags": ["а", "б"]}, {"tags": ["б", "а"]}),
        ({"source_ids": [4, 17]}, {"source_ids": [17, 4]}),
    ],
    ids=["priority", "tags", "source_ids"],
)
def test_reordering_a_list_does_not_change_the_fingerprint(first, second):
    assert FeedQuery.build(**first).fingerprint() == FeedQuery.build(**second).fingerprint()


def test_the_fingerprint_ignores_limit_and_cursor_but_not_the_filters():
    base = FeedQuery.build(priority=["high"], limit=20)

    assert FeedQuery.build(priority=["high"], limit=50).fingerprint() == base.fingerprint()
    assert FeedQuery.build(priority=["high"], cursor="x").fingerprint() == base.fingerprint()
    assert FeedQuery.build(priority=["medium"]).fingerprint() != base.fingerprint()
    assert FeedQuery.build(priority=["high"], order="priority").fingerprint() != base.fingerprint()
    assert (
        FeedQuery.build(priority=["high"], include_hidden=True).fingerprint() != base.fingerprint()
    )


# ── курсор ─────────────────────────────────────────────────────────────────


def test_a_cursor_round_trips_the_position_of_the_last_row():
    query = FeedQuery.build(priority=["high"])
    token = query.encode_cursor("2026-09-04T09:00:00+00:00", 7)

    position = FeedQuery.build(priority=["high"], cursor=token).decode_cursor()

    assert position == ("2026-09-04T09:00:00+00:00", 7)


def test_a_cursor_survives_the_filter_values_arriving_in_another_order():
    first = FeedQuery.build(priority=["high", "medium"], tags=["а", "б"])
    token = first.encode_cursor("2026-09-04T09:00:00+00:00", 7)

    second = FeedQuery.build(priority=["medium", "high"], tags=["б", "а"], cursor=token)

    assert second.decode_cursor() == ("2026-09-04T09:00:00+00:00", 7)


def test_a_cursor_over_an_undated_row_keeps_the_empty_position():
    query = FeedQuery.build()
    token = query.encode_cursor(None, 3)

    assert FeedQuery.build(cursor=token).decode_cursor() == (None, 3)


@pytest.mark.parametrize("cursor", [None, ""], ids=["none", "empty"])
def test_no_cursor_means_the_first_page(cursor):
    assert FeedQuery.build(cursor=cursor).decode_cursor() is None


def test_a_cursor_from_another_slice_is_refused_instead_of_silently_paging_it():
    token = FeedQuery.build(priority=["high"]).encode_cursor("2026-09-04T09:00:00+00:00", 7)

    with pytest.raises(QueryError, match="курсор относится к другому набору фильтров") as excinfo:
        FeedQuery.build(priority=["low"], cursor=token).decode_cursor()
    assert excinfo.value.code == "invalid_cursor"


@pytest.mark.parametrize(
    "cursor",
    [
        "!!!!",
        "не-base64-строка",
        base64.urlsafe_b64encode(b"not json at all").decode("ascii"),
        base64.urlsafe_b64encode(json.dumps({"p": None}).encode()).decode("ascii"),
        base64.urlsafe_b64encode(json.dumps(["p", 1, "fp"]).encode()).decode("ascii"),
        base64.urlsafe_b64encode(
            json.dumps({"p": None, "i": "не число", "fp": "x"}).encode()
        ).decode("ascii"),
    ],
    ids=["not-base64", "cyrillic", "not-json", "missing-keys", "wrong-shape", "id-not-a-number"],
)
def test_a_malformed_cursor_is_an_invalid_cursor_error(cursor):
    with pytest.raises(QueryError, match="курсор не разбирается") as excinfo:
        FeedQuery.build(cursor=cursor).decode_cursor()
    assert excinfo.value.code == "invalid_cursor"


# ── список необработанного ─────────────────────────────────────────────────


def test_document_query_takes_the_filters_a_document_can_answer():
    query = DocumentQuery.build(
        source_ids=["4"], date_from="2026-09-05", q=" КИИ ", limit=5, timezone_name="UTC"
    )

    assert query.source_ids == (4,)
    assert query.date_from == _utc(2026, 9, 5, 0)
    assert (query.q, query.limit) == ("КИИ", 5)


@pytest.mark.parametrize(
    ("unsupported", "expected"),
    [
        ({"priority": ["high"]}, "priority"),
        ({"type": "npa"}, "type"),
        ({"tag": ["регуляторика"]}, "tag"),
        ({"npa_status": "внесён"}, "npa_status"),
        ({"type": "npa", "priority": ["high"]}, "priority, type"),
    ],
    ids=["priority", "type", "tag", "npa_status", "two-at-once"],
)
def test_a_card_filter_is_refused_for_the_document_list(unsupported, expected):
    with pytest.raises(QueryError) as excinfo:
        DocumentQuery.build(unsupported=unsupported)

    assert excinfo.value.code == "validation_error"
    assert excinfo.value.message == (
        f"к списку документов неприменимы фильтры карточки: {expected}"
    )


@pytest.mark.parametrize(
    "unsupported",
    [None, {}, {"priority": []}, {"type": None}, {"npa_status": ""}],
    ids=["none", "empty", "empty-list", "none-value", "empty-string"],
)
def test_an_absent_card_filter_does_not_trip_the_document_list(unsupported):
    assert DocumentQuery.build(unsupported=unsupported).limit == DEFAULT_LIMIT
