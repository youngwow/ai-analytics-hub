"""Выгрузка среза наружу: RSS для подписки и CSV для отчёта (план 1.3).

Канал уходит за пределы дашборда, а авторизации в этой версии нет — поэтому
выгружается ровно то же, что дайджест: только `visible`, без архива и без заметок
аналитика. Форма проверяется настоящим разбором XML и настоящим чтением CSV, а не
поиском подстрок: экранирование — это половина задачи.
"""

from __future__ import annotations

import csv
import io
from xml.etree import ElementTree as ET

import pytest

from src.models import ItemNote
from src.models.queries import FeedQuery
from src.services.export import CSV_COLUMNS, to_csv, to_rss

NOW = "2026-09-05T09:00:00+00:00"
RSS_TYPE = "application/rss+xml; charset=utf-8"
CSV_TYPE = "text/csv; charset=utf-8"
BOM = "﻿"


def _row(**overrides) -> dict:
    """Строка ленты в том виде, в каком её отдаёт `FeedService.visible_slice`."""
    base = {
        "id": 7,
        "type": "npa",
        "npa_status": "внесён",
        "priority": "high",
        "title": "Минцифры внесло законопроект об аккредитации ИИ-сервисов",
        "summary": "Операторы ИИ обязаны пройти аккредитацию.",
        "tags": ["регуляторика"],
        "published_at": "2026-09-04T09:00:00+00:00",
        "source_name": "Ведомости",
        "canonical_url": "https://vedomosti.ru/1",
    }
    return {**base, **overrides}


def _rss(rows: list[dict], **overrides) -> str:
    payload = {
        "title": "Аналитический центр: лента",
        "link": "https://hub.local/",
        "description": "Отраслевые новости и НПА",
        "generated_at": NOW,
    }
    return to_rss(rows, **{**payload, **overrides})


def _channel(xml: str) -> ET.Element:
    return ET.fromstring(xml).find("channel")


def _entries(xml: str) -> list[ET.Element]:
    return _channel(xml).findall("item")


def _csv_rows(body: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(body.lstrip(BOM)), delimiter=";"))


# ── RSS: форма канала ──────────────────────────────────────────────────────


def test_the_document_starts_with_an_xml_declaration_and_an_rss_root():
    xml = _rss([_row()])

    assert xml.startswith('<?xml version="1.0" encoding="utf-8"?>\n')
    assert ET.fromstring(xml).attrib == {"version": "2.0"}


def test_the_channel_carries_the_title_link_description_and_language():
    channel = _channel(_rss([]))

    assert channel.findtext("title") == "Аналитический центр: лента"
    assert channel.findtext("link") == "https://hub.local/"
    assert channel.findtext("description") == "Отраслевые новости и НПА"
    assert channel.findtext("language") == "ru"


def test_the_build_date_is_rfc_822_not_iso():
    assert _channel(_rss([])).findtext("lastBuildDate") == "Sat, 05 Sep 2026 09:00:00 +0000"


def test_an_unparseable_build_date_is_left_out_rather_than_faked():
    assert _channel(_rss([], generated_at="скоро")).find("lastBuildDate") is None


def test_an_empty_slice_is_still_a_valid_channel():
    xml = _rss([])

    assert _entries(xml) == []
    assert _channel(xml).findtext("title") == "Аналитический центр: лента"


# ── RSS: одна карточка — один item ─────────────────────────────────────────


def test_every_card_becomes_one_item_in_order():
    xml = _rss([_row(id=1, title="Первая"), _row(id=2, title="Вторая")])

    assert [e.findtext("title") for e in _entries(xml)] == ["Первая", "Вторая"]


def test_the_item_carries_the_link_summary_source_and_tags():
    entry = _entries(_rss([_row(tags=["регуляторика", "тренды"])]))[0]

    assert entry.findtext("link") == "https://vedomosti.ru/1"
    assert entry.findtext("description") == "Операторы ИИ обязаны пройти аккредитацию."
    assert entry.findtext("author") == "Ведомости"
    assert [c.text for c in entry.findall("category")] == ["регуляторика", "тренды"]


def test_the_publication_date_is_rfc_822():
    assert _entries(_rss([_row()]))[0].findtext("pubDate") == "Fri, 04 Sep 2026 09:00:00 +0000"


def test_a_card_without_a_date_gets_no_pubdate():
    assert _entries(_rss([_row(published_at=None)]))[0].find("pubDate") is None


def test_a_card_with_a_url_gets_a_permanent_guid():
    guid = _entries(_rss([_row()]))[0].find("guid")

    assert guid.attrib == {"isPermaLink": "true"}
    assert guid.text == "https://vedomosti.ru/1"


def test_a_card_without_a_url_gets_an_internal_guid_that_is_not_a_link():
    entry = _entries(_rss([_row(canonical_url=None)]))[0]

    assert entry.find("guid").attrib == {"isPermaLink": "false"}
    assert entry.find("guid").text == "item-7"
    assert entry.find("link") is None


def test_a_card_without_a_title_still_has_one():
    assert _entries(_rss([_row(title="")]))[0].findtext("title") == "Без заголовка"


def test_a_multiline_summary_becomes_one_line():
    entry = _entries(_rss([_row(summary="Первое.\nВторое.\n")]))[0]

    assert entry.findtext("description") == "Первое. Второе."


def test_a_card_without_tags_has_no_category_elements():
    assert _entries(_rss([_row(tags=[])]))[0].findall("category") == []


# ── RSS: экранирование ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "title",
    [
        "Ростелеком & Триколор договорились",
        "Порог <5% для операторов",
        'Проект «О связи» с "кавычками"',
        "Скидка 100% и знак > в конце",
    ],
    ids=["ampersand", "angle-bracket", "quotes", "percent-and-gt"],
)
def test_a_title_with_markup_characters_survives_the_round_trip(title):
    xml = _rss([_row(title=title)])

    assert _entries(xml)[0].findtext("title") == title


def test_the_ampersand_is_escaped_in_the_bytes_not_left_raw():
    xml = _rss([_row(title="Ростелеком & Триколор")])

    assert "&amp;" in xml
    assert "Ростелеком & Триколор" not in xml


def test_cyrillic_is_written_as_is_not_as_entities():
    assert "Минцифры" in _rss([_row()])


def test_a_url_with_a_query_string_is_escaped_and_read_back():
    row = _row(canonical_url="https://vedomosti.ru/a?b=1&c=2")

    assert _entries(_rss([row]))[0].findtext("link") == "https://vedomosti.ru/a?b=1&c=2"


# ── CSV ────────────────────────────────────────────────────────────────────


def test_the_file_starts_with_a_byte_order_mark_for_excel():
    assert to_csv([]).startswith(BOM)


def test_the_header_row_is_the_declared_column_list():
    assert _csv_rows(to_csv([]))[0] == list(CSV_COLUMNS)


def test_the_lines_end_with_crlf():
    body = to_csv([_row()])

    assert body.endswith("\r\n")
    assert len(body.split("\r\n")) == 3  # заголовок, строка, хвост


def test_one_row_per_card_in_the_given_order():
    rows = _csv_rows(to_csv([_row(id=1, title="Первая"), _row(id=2, title="Вторая")]))

    assert [r[0] for r in rows[1:]] == ["1", "2"]
    assert [r[4] for r in rows[1:]] == ["Первая", "Вторая"]


def test_the_row_carries_every_column_in_the_declared_order():
    row = _csv_rows(to_csv([_row(tags=["регуляторика", "тренды"])]))[1]

    assert dict(zip(CSV_COLUMNS, row)) == {
        "id": "7",
        "type": "npa",
        "npa_status": "внесён",
        "priority": "high",
        "title": "Минцифры внесло законопроект об аккредитации ИИ-сервисов",
        "summary": "Операторы ИИ обязаны пройти аккредитацию.",
        "tags": "регуляторика, тренды",
        "published_at": "2026-09-04T09:00:00+00:00",
        "source_name": "Ведомости",
        "canonical_url": "https://vedomosti.ru/1",
    }


def test_missing_values_become_empty_cells_not_the_word_none():
    row = _csv_rows(
        to_csv([_row(npa_status=None, published_at=None, canonical_url=None, tags=None)])
    )[1]

    assert dict(zip(CSV_COLUMNS, row))["npa_status"] == ""
    assert dict(zip(CSV_COLUMNS, row))["published_at"] == ""
    assert dict(zip(CSV_COLUMNS, row))["canonical_url"] == ""
    assert dict(zip(CSV_COLUMNS, row))["tags"] == ""


def test_a_title_with_the_delimiter_is_quoted_and_reads_back_whole():
    """Разделитель — `;`, а в заголовках он встречается: без кавычек строка поедет."""
    body = to_csv([_row(title="Связь; вещание; интернет")])

    assert '"Связь; вещание; интернет"' in body
    assert _csv_rows(body)[1][4] == "Связь; вещание; интернет"


def test_a_multiline_title_and_summary_are_flattened_into_one_cell():
    body = to_csv([_row(title="Первая\nстрока", summary="Первое.\nВторое.")])

    row = _csv_rows(body)[1]
    assert (row[4], row[5]) == ("Первая строка", "Первое. Второе.")
    assert len(_csv_rows(body)) == 2


def test_the_file_is_utf_8_with_a_real_byte_order_mark():
    """Excel открывает кириллицу без плясок с кодировками только по этим трём байтам."""
    raw = to_csv([_row()]).encode("utf-8")

    assert raw.startswith(b"\xef\xbb\xbf")
    assert _csv_rows(raw.decode("utf-8-sig"))[1][8] == "Ведомости"


# ── HTTP: тот же срез, что у ленты ─────────────────────────────────────────


def _titles_from_rss(response) -> list[str]:
    return [e.findtext("title") for e in _entries(response.text)]


def _titles_from_csv(response) -> list[str]:
    return [row[4] for row in _csv_rows(response.text)[1:]]


def test_the_rss_route_answers_with_the_rss_media_type(client, corpus):
    response = client.get("/api/v1/export/feed.xml")

    assert response.status_code == 200
    assert response.headers["content-type"] == RSS_TYPE


def test_the_csv_route_offers_the_file_as_a_download(client, corpus):
    response = client.get("/api/v1/export/items.csv")

    assert response.status_code == 200
    assert response.headers["content-type"] == CSV_TYPE
    assert response.headers["content-disposition"].startswith('attachment; filename="items-')
    assert response.headers["content-disposition"].endswith('.csv"')


@pytest.mark.parametrize(
    ("path", "titles"),
    [("/api/v1/export/feed.xml", _titles_from_rss), ("/api/v1/export/items.csv", _titles_from_csv)],
    ids=["rss", "csv"],
)
def test_only_visible_cards_are_exported(client, corpus, path, titles):
    """Скрытое, скрытое из дайджеста и удалённое наружу не уходит никогда."""
    exported = titles(client.get(path))

    assert "Скрыто из ленты" not in exported
    assert "Скрыто из дайджеста, но не из ленты" not in exported
    assert "Мягко удалённая карточка" not in exported
    assert "Минцифры внесло законопроект об аккредитации ИИ-сервисов" in exported


@pytest.mark.parametrize(
    ("path", "titles"),
    [("/api/v1/export/feed.xml", _titles_from_rss), ("/api/v1/export/items.csv", _titles_from_csv)],
    ids=["rss", "csv"],
)
def test_an_archived_card_is_not_exported(client, file_db, corpus, path, titles):
    file_db.items.set_archived(corpus["npa_high"], True)
    file_db.conn.commit()

    exported = titles(client.get(path))

    assert "Минцифры внесло законопроект об аккредитации ИИ-сервисов" not in exported
    assert "Оператор платного ТВ запустил рекомендательный сервис" in exported


@pytest.mark.parametrize(
    ("path", "titles"),
    [("/api/v1/export/feed.xml", _titles_from_rss), ("/api/v1/export/items.csv", _titles_from_csv)],
    ids=["rss", "csv"],
)
def test_asking_for_the_hidden_cards_does_not_widen_the_export(client, corpus, path, titles):
    """Фильтр может просить что угодно — канал без авторизации отдаёт только видимое."""
    exported = titles(client.get(path, params={"include_hidden": "true", "archived": "include"}))

    assert "Скрыто из ленты" not in exported
    assert "Мягко удалённая карточка" not in exported


@pytest.mark.parametrize(
    "path", ["/api/v1/export/feed.xml", "/api/v1/export/items.csv"], ids=["rss", "csv"]
)
def test_an_analyst_note_never_leaves_the_dashboard(client, file_db, corpus, path):
    file_db.notes.add(ItemNote(item_id=corpus["npa_high"], body="Совещание в четверг", author="y"))

    body = client.get(path).text

    assert "Совещание в четверг" not in body


def test_the_priority_filter_narrows_the_rss(client, corpus):
    exported = _titles_from_rss(client.get("/api/v1/export/feed.xml", params={"priority": "high"}))

    assert exported == ["Минцифры внесло законопроект об аккредитации ИИ-сервисов"]


def test_the_search_filter_narrows_the_csv(client, corpus):
    exported = _titles_from_csv(
        client.get("/api/v1/export/items.csv", params={"q": "рекомендательный"})
    )

    assert exported == ["Оператор платного ТВ запустил рекомендательный сервис"]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"from": "2026-09-04"}, ["Минцифры внесло законопроект об аккредитации ИИ-сервисов"]),
        ({"to": "2026-09-01"}, ["Комитет вернулся к рассмотрению поправок"]),
        ({"from": "2026-09-05"}, []),
    ],
    ids=["from", "to", "empty-window"],
)
def test_the_date_filters_apply_to_the_export(client, corpus, params, expected):
    assert _titles_from_rss(client.get("/api/v1/export/feed.xml", params=params)) == expected


def test_a_broken_filter_is_a_problem_response_not_a_broken_file(client, corpus):
    response = client.get("/api/v1/export/feed.xml", params={"priority": "срочно"})

    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "validation_error"


def test_the_export_of_an_empty_hub_is_an_empty_channel_and_a_header_only_file(client):
    rss = client.get("/api/v1/export/feed.xml")
    table = client.get("/api/v1/export/items.csv")

    assert _entries(rss.text) == []
    assert _csv_rows(table.text) == [list(CSV_COLUMNS)]


def test_the_limit_from_the_query_string_caps_the_export(client, corpus):
    """Страница режется по `limit` до отбора видимых: больше запрошенного не уйдёт никогда.

    Скрытая карточка занимает место в странице и просто не попадает в файл — так
    же ведёт себя дайджест, у них одна основа.
    """
    exported = _titles_from_rss(client.get("/api/v1/export/feed.xml", params={"limit": 2}))

    assert exported == ["Минцифры внесло законопроект об аккредитации ИИ-сервисов"]
    assert len(exported) <= 2


def test_the_visible_slice_is_the_same_one_the_digest_uses(feed, corpus):
    """Одна основа: дайджест и выгрузка не могут разъехаться по составу."""
    rows = feed.visible_slice(FeedQuery.build(limit=50))

    assert [r["visibility"] for r in rows] == ["visible"] * len(rows)
    assert len(rows) == 5
    assert feed.digest(FeedQuery.build(limit=50))["items"] == len(rows)
