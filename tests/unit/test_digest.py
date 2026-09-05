"""src/feed/digest.py и FeedService.digest — срез, который отправляют руководителю.

Дайджест ничего не сохраняет: это выгрузка среза, а не сущность. Порядок задан
задачей «утренний разбор» — читают сверху вниз и останавливаются, когда важное
кончилось, поэтому `high` обязан быть первым.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.feed import digest as digest_mod
from src.feed.query import FeedQuery
from src.models import ItemNote

GENERATED_AT = datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def frozen_generated_at(monkeypatch) -> str:
    """`generated_at` берётся из `utc_now()` — пиним её, а не читаем часы."""
    import src.feed.service as service_mod

    monkeypatch.setattr(service_mod, "utc_now", lambda: GENERATED_AT)
    return "2026-09-05T06:00:00+00:00"


def _row(**overrides) -> dict:
    base = {
        "id": 1,
        "type": "news",
        "npa_status": None,
        "priority": "medium",
        "title": "Заголовок",
        "summary": "Первое предложение.",
        "tags": [],
        "published_at": "2026-09-04T09:00:00+00:00",
        "canonical_url": "",
        "source_name": "",
    }
    return {**base, **overrides}


# ── группировка ────────────────────────────────────────────────────────────


def test_groups_come_out_high_then_medium_then_low():
    rows = [
        _row(id=1, priority="low"),
        _row(id=2, priority="high"),
        _row(id=3, priority="medium"),
        _row(id=4, priority="high"),
    ]

    grouped = digest_mod.group_by_priority(rows)

    assert [priority for priority, _ in grouped] == ["high", "medium", "low"]
    assert [[r["id"] for r in group] for _, group in grouped] == [[2, 4], [3], [1]]


def test_an_empty_priority_group_is_left_out_entirely():
    grouped = digest_mod.group_by_priority([_row(priority="high"), _row(priority="high")])

    assert [priority for priority, _ in grouped] == ["high"]


def test_grouping_nothing_gives_nothing():
    assert digest_mod.group_by_priority([]) == []


def test_the_order_inside_a_group_is_the_one_it_arrived_in():
    rows = [_row(id=7, priority="high"), _row(id=3, priority="high"), _row(id=5, priority="high")]

    assert [r["id"] for _, group in digest_mod.group_by_priority(rows) for r in group] == [7, 3, 5]


# ── markdown ───────────────────────────────────────────────────────────────


def test_markdown_opens_with_the_title_and_the_count():
    body = digest_mod.to_markdown(
        [_row()], title="Дайджест 05.09", generated_at="2026-09-05T06:00:00+00:00"
    )

    assert body.splitlines()[0] == "# Дайджест 05.09"
    assert "_Сформировано: 2026-09-05T06:00:00+00:00; материалов: 1_" in body


def test_markdown_heads_each_group_with_its_russian_name_and_size():
    body = digest_mod.to_markdown(
        [_row(priority="high"), _row(priority="high"), _row(priority="low")],
        title="Т",
        generated_at="",
    )

    assert "## Высокий приоритет (2)" in body
    assert "## Низкий приоритет (1)" in body
    assert body.index("## Высокий приоритет") < body.index("## Низкий приоритет")


def test_markdown_shows_the_kind_status_date_and_source_of_a_card():
    body = digest_mod.to_markdown(
        [
            _row(
                type="npa",
                npa_status="внесён",
                title="Аккредитация ИИ-сервисов",
                source_name="Ведомости",
            )
        ],
        title="Т",
        generated_at="",
    )

    assert "### Аккредитация ИИ-сервисов" in body
    assert "*НПА, внесён* · 2026-09-04 · Ведомости" in body


def test_markdown_carries_the_tags_and_the_link_to_the_original():
    body = digest_mod.to_markdown(
        [
            _row(
                tags=["регуляторика", "тренды"],
                canonical_url="https://vedomosti.ru/news/2026/09/04/ai",
            )
        ],
        title="Т",
        generated_at="",
    )

    assert "Теги: регуляторика, тренды" in body
    assert "[Оригинал](https://vedomosti.ru/news/2026/09/04/ai)" in body


def test_markdown_leaves_out_what_the_card_does_not_have():
    body = digest_mod.to_markdown(
        [_row(tags=[], canonical_url="", source_name="", npa_status=None)],
        title="Т",
        generated_at="",
    )

    assert "Теги:" not in body
    assert "[Оригинал]" not in body


def test_a_card_without_a_title_still_renders():
    body = digest_mod.to_markdown([_row(title="")], title="Т", generated_at="")

    assert "### Без заголовка" in body


def test_a_note_appears_only_when_it_is_handed_in():
    rows = [_row(id=42)]
    notes = {42: ["вынести на совещание 14-го"]}

    with_note = digest_mod.to_markdown(rows, title="Т", generated_at="", notes=notes)
    without_note = digest_mod.to_markdown(rows, title="Т", generated_at="")

    assert "> Заметка: вынести на совещание 14-го" in with_note
    assert "Заметка" not in without_note


# ── выгрузка среза ─────────────────────────────────────────────────────────


def test_the_digest_groups_the_slice_with_high_on_top(feed, corpus, frozen_generated_at):
    result = feed.digest(FeedQuery.build(order="priority"))

    body = result["body"]
    assert result["items"] == 5  # шесть видимых минус скрытая из дайджеста
    assert body.index("## Высокий приоритет") < body.index("## Средний приоритет")
    assert body.index("## Средний приоритет") < body.index("## Низкий приоритет")


@pytest.mark.parametrize("key", ["hidden_digest", "hidden_feed", "deleted"], ids=lambda s: s)
def test_a_hidden_or_deleted_card_never_reaches_the_digest(feed, corpus, key, frozen_generated_at):
    body = feed.digest(FeedQuery.build())["body"]

    title = {
        "hidden_digest": "Скрыто из дайджеста",
        "hidden_feed": "Скрыто из ленты",
        "deleted": "Мягко удалённая карточка",
    }[key]
    assert title not in body


def test_include_hidden_in_the_filters_cannot_smuggle_a_hidden_card_in(
    feed, corpus, frozen_generated_at
):
    """Скрытое из дайджеста не выгружается никогда, что бы ни просили фильтры."""
    result = feed.digest(FeedQuery.build(include_hidden=True))

    assert result["items"] == 5
    assert "Скрыто из дайджеста" not in result["body"]


def test_the_digest_honours_the_filters_of_the_slice(feed, corpus, frozen_generated_at):
    result = feed.digest(FeedQuery.build(priority=["high"], order="priority"))

    assert result["items"] == 1
    assert "Минцифры внесло законопроект" in result["body"]
    assert "## Средний приоритет" not in result["body"]


def test_the_digest_titles_itself_by_the_day_it_was_made(feed, corpus, frozen_generated_at):
    result = feed.digest(FeedQuery.build())

    assert result["title"] == "Дайджест 2026-09-05"
    assert result["generated_at"] == frozen_generated_at
    assert result["body"].startswith("# Дайджест 2026-09-05")


def test_a_given_title_wins_over_the_default(feed, corpus, frozen_generated_at):
    result = feed.digest(FeedQuery.build(), title="Дайджест для правления")

    assert result["title"] == "Дайджест для правления"
    assert result["body"].startswith("# Дайджест для правления")


def test_the_markdown_digest_links_the_originals_and_lists_the_tags(
    feed, corpus, frozen_generated_at
):
    body = feed.digest(FeedQuery.build(priority=["high"]))["body"]

    assert "[Оригинал](https://s1.example.ru/doc-1)" in body
    assert "Теги: регуляторика" in body
    assert "· Ведомости" in body


def test_the_json_format_carries_the_same_rows_as_the_feed(feed, corpus, frozen_generated_at):
    result = feed.digest(FeedQuery.build(priority=["high"]), fmt="json")

    payload = json.loads(result["body"])
    assert result["format"] == "json"
    assert payload["title"] == "Дайджест 2026-09-05"
    assert payload["generated_at"] == frozen_generated_at
    assert [row["title"] for row in payload["items"]] == [
        "Минцифры внесло законопроект об аккредитации ИИ-сервисов"
    ]
    assert payload["items"][0]["canonical_url"] == "https://s1.example.ru/doc-1"


def test_the_digest_of_an_empty_slice_is_a_heading_and_nothing_else(
    feed, corpus, frozen_generated_at
):
    result = feed.digest(FeedQuery.build(tags=["такого-тега-нет"]))

    assert result["items"] == 0
    assert "## " not in result["body"]


# ── заметки аналитика ──────────────────────────────────────────────────────


@pytest.fixture
def noted(file_db, corpus) -> str:
    body = "внутренняя пометка про подрядчика"
    file_db.notes.add(ItemNote(item_id=corpus["npa_high"], body=body))
    return body


@pytest.mark.parametrize("fmt", ["markdown", "json"], ids=lambda s: s)
def test_without_include_notes_the_note_text_is_nowhere_in_the_export(
    feed, noted, fmt, frozen_generated_at
):
    result = feed.digest(FeedQuery.build(priority=["high"]), fmt=fmt)

    assert noted not in result["body"]


def test_include_notes_puts_the_note_into_the_markdown(feed, noted, frozen_generated_at):
    result = feed.digest(FeedQuery.build(priority=["high"]), include_notes=True)

    assert f"> Заметка: {noted}" in result["body"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "дефект src/feed/service.py:183 — `notes` подмешиваются только в ветке markdown, "
        "а ветка json собирает тело из одних строк ленты и игнорирует include_notes"
    ),
)
def test_include_notes_puts_the_note_into_the_json_too(feed, noted, frozen_generated_at):
    result = feed.digest(FeedQuery.build(priority=["high"]), fmt="json", include_notes=True)

    assert noted in result["body"]
