"""src/repositories/items.py::SearchRepo — одна строка индекса на карточку.

Строка шире карточки: заголовок, саммари, теги, значения сущностей и текст
канонического оригинала. Она пересобирается целиком при каждой записи, поэтому
проверяется и содержимое, и то, что запись действительно вызывает пересборку.
"""

from __future__ import annotations

import pytest
from support import FakeLLM, news_answer

from src.models import EntitySpan, ItemRevision, RawDocument
from src.services.item_service import ItemService
from src.services.processing_service import ProcessingService

NOW = "2026-09-05T09:00:00+00:00"
ORIGINAL = "Ко второму чтению подготовлены поправки о трансграничной передаче."


@pytest.fixture
def items(config, file_db) -> ItemService:
    """Правки и откат не зовут модель — сервис карточек без провайдера."""
    return ItemService(config, file_db)


def _indexed(db, expression: str) -> list[int]:
    return [
        int(r[0])
        for r in db.conn.execute("SELECT rowid FROM items_search WHERE body MATCH ?", (expression,))
    ]


def _rows(db, item_id: int) -> list[str]:
    return [
        r[0] for r in db.conn.execute("SELECT body FROM items_search WHERE rowid=?", (item_id,))
    ]


@pytest.fixture
def indexed_card(file_db, card_factory, source_factory) -> int:
    return card_factory(
        source_factory("Ведомости"),
        title="Комитет собрал отзывы",
        summary="Обсуждение продолжится осенью.",
        original=ORIGINAL,
        tags=("регуляторика",),
        entities=(("who", "Роскомнадзор"), ("act_number", "112233-8")),
    )


# ── что попадает в строку ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("word", "source_of_the_word"),
    [
        ("Комитет", "заголовок"),
        ("Обсуждение", "саммари"),
        ("регуляторика", "тег"),
        ("Роскомнадзор", "сущность"),
        ("трансграничной", "текст оригинала"),
    ],
    ids=["title", "summary", "tag", "entity", "original-text"],
)
def test_the_body_is_assembled_from_all_four_sources(
    file_db, indexed_card, word, source_of_the_word
):
    body = file_db.search.body_for(indexed_card)

    assert word in body, source_of_the_word


def test_the_normalised_value_of_an_entity_is_indexed_too(file_db, card_factory, source_factory):
    item_id = card_factory(source_factory("Дума"), title="Законопроект")
    with file_db.transaction():
        file_db.items.add_entities(
            item_id,
            [EntitySpan(role="act_number", value="№ 112233-8", normalized_value="112233-8")],
        )
        file_db.search.rebuild(item_id)

    assert _indexed(file_db, '"112233"*') == [item_id]


def test_body_for_an_unknown_card_is_empty(file_db):
    assert file_db.search.body_for(999) == ""


def test_only_the_canonical_document_lends_its_text(file_db, card_factory, source_factory):
    source = source_factory("Ведомости")
    item_id = card_factory(source, title="Первая публикация", original=ORIGINAL)
    with file_db.transaction():
        reprint = file_db.documents.insert(
            RawDocument(
                source_id=source.id,
                external_id="reprint",
                url="https://s1.example.ru/reprint",
                title="Перепечатка",
                fetched_at=NOW,
            )
        )
        file_db.documents.set_derived(reprint, norm_text="Совершенно посторонний текст.")
        file_db.items.link_sources(item_id, [reprint])
        file_db.search.rebuild(item_id)

    body = file_db.search.body_for(item_id)
    assert "трансграничной" in body
    assert "посторонний" not in body


# ── пересборка и удаление строки ───────────────────────────────────────────


def test_rebuild_leaves_exactly_one_row_however_often_it_runs(file_db, indexed_card):
    with file_db.transaction():
        file_db.search.rebuild(indexed_card)
        file_db.search.rebuild(indexed_card)

    assert len(_rows(file_db, indexed_card)) == 1


def test_remove_takes_the_card_out_of_the_index(file_db, indexed_card):
    with file_db.transaction():
        file_db.search.remove(indexed_card)

    assert _rows(file_db, indexed_card) == []
    assert _indexed(file_db, '"Роскомнадзор"*') == []


def test_a_card_without_any_text_is_not_indexed_at_all(file_db, card_factory, source_factory):
    empty = card_factory(source_factory("Пустой"), title="", summary="", original="")

    assert file_db.search.body_for(empty) == ""
    assert _rows(file_db, empty) == []


def test_a_card_whose_only_text_is_the_original_still_gets_indexed(
    file_db, card_factory, source_factory
):
    item_id = card_factory(source_factory("Канал"), title="", summary="", original=ORIGINAL)

    assert _indexed(file_db, '"трансграничной"*') == [item_id]


def test_rebuild_all_reports_and_covers_every_card(file_db, card_factory, source_factory):
    source = source_factory("Ведомости")
    with_text = card_factory(source, title="Есть текст", original=ORIGINAL)
    without_text = card_factory(source, title="", summary="", original="")
    with file_db.transaction():
        file_db.conn.execute("DELETE FROM items_search")

    assert file_db.search.rebuild_all() == 2  # обходит обе карточки…
    assert _rows(file_db, with_text) != []  # …но строку заводит только той, у которой есть текст
    assert _rows(file_db, without_text) == []


# ── синхронизация при записи ───────────────────────────────────────────────


def test_editing_the_title_moves_the_card_in_the_index(file_db, items, indexed_card):
    items.edit_item(indexed_card, {"title": "Совершенно новый заголовок"})

    assert _indexed(file_db, '"новый"*') == [indexed_card]
    assert _indexed(file_db, '"Комитет"*') == []


def test_editing_the_summary_moves_the_card_in_the_index(file_db, items, indexed_card):
    items.edit_item(indexed_card, {"summary": "Переписано аналитиком вручную."})

    assert _indexed(file_db, '"аналитиком"*') == [indexed_card]
    assert _indexed(file_db, '"Обсуждение"*') == []


def test_a_hand_added_tag_reaches_the_index_next_to_the_models_own(
    file_db, items, indexed_card
):
    """Правка заменяет только ручные теги, поэтому в индексе оказываются оба."""
    items.edit_item(indexed_card, {"tags": ["персданные"]})

    assert _indexed(file_db, '"персданные"*') == [indexed_card]
    assert _indexed(file_db, '"регуляторика"*') == [indexed_card]
    assert file_db.tags.names(indexed_card) == ["персданные", "регуляторика"]


def test_dropping_a_model_tag_takes_it_out_of_the_index(file_db, indexed_card):
    with file_db.transaction():
        file_db.tags.set_tags(indexed_card, ["тренды"], is_manual=False)
        file_db.search.rebuild(indexed_card)

    assert _indexed(file_db, '"тренды"*') == [indexed_card]
    assert _indexed(file_db, '"регуляторика"*') == []


def test_revert_puts_the_model_version_back_into_the_index(file_db, items, indexed_card):
    file_db.items.add_revision(
        ItemRevision(
            item_id=indexed_card,
            field="title",
            old_value=None,
            new_value="Версия модели про аккредитацию",
            actor="model",
            source_of_change="llm",
        )
    )
    file_db.conn.commit()
    items.edit_item(indexed_card, {"title": "Ручной заголовок"})
    assert _indexed(file_db, '"Ручной"*') == [indexed_card]

    items.revert(indexed_card, "title")

    assert _indexed(file_db, '"аккредитацию"*') == [indexed_card]
    assert _indexed(file_db, '"Ручной"*') == []


def test_reprocessing_a_card_rebuilds_its_row(file_db, config, source_factory, card_factory):
    item_id = card_factory(
        source_factory("Ведомости"),
        title="Исходный заголовок",
        summary="Исходное саммари.",
        original="Оператор платного ТВ запустил бета-версию рекомендательного сервиса.",
    )
    provider = FakeLLM(
        news_answer(summary=["Пересобранное про приставку.", "Второе.", "Третье."])
    )
    service = ProcessingService(config, file_db, provider=provider, embedder=None)

    service.reprocess(item_id)

    assert _indexed(file_db, '"Пересобранное"*') == [item_id]
    assert _indexed(file_db, '"Исходное"*') == []


def test_a_manual_card_is_indexed_the_moment_it_is_created(file_db, config):
    service = ProcessingService(config, file_db, provider=None, embedder=None)

    result = service.add_manual(
        title="Материал с закрытого совещания",
        text="Обсуждали трансграничную передачу сведений.",
        run_llm=False,
    )

    assert _indexed(file_db, '"совещания"*') == [result["item_id"]]
    assert _indexed(file_db, '"трансграничную"*') == [result["item_id"]]
