"""Массовый тегинг и массовая архивация (план 1.2).

Двадцать одиночных PATCH — это не работа аналитика, а её имитация: разметить
утренний срез и убрать отправленный дайджест в архив нужно одним жестом. Обе
операции атомарны: неизвестная карточка отменяет всю пачку, а не полпачки.
"""

from __future__ import annotations

import pytest

from src.exceptions import ItemError, ItemNotFoundError, ItemValidationError
from src.models import RawDocument, Source
from src.models.queries import FeedQuery
from src.repositories import Database
from src.services.item_service import ItemService

NOW = "2026-09-02T12:00:00+00:00"
PROBLEM = "application/problem+json"


def _card(db: Database, item_factory, external_id: str, *, tags=(), **fields) -> int:
    """Карточка с согласованными тегами: и JSON-колонка, и `item_tags`."""
    source = db.sources.get_by_fetch_url("https://a.ru/rss") or db.sources.add(
        Source(name="Лента", url="https://a.ru/", kind="rss", fetch_url="https://a.ru/rss")
    )
    with db.transaction():
        document_id = db.documents.insert(
            RawDocument(
                source_id=source.id,
                external_id=external_id,
                url=f"https://a.ru/{external_id}",
                title=f"Материал {external_id}",
                fetched_at=NOW,
            )
        )
    item_id = item_factory(db, document_id, title=f"Материал {external_id}", tags=list(tags),
                           **fields)
    with db.transaction():
        db.tags.set_tags(item_id, tags, is_manual=False)
    return item_id


@pytest.fixture
def service(config, db, frozen_clock) -> ItemService:
    return ItemService(config, db)


@pytest.fixture
def cards(db, item_factory) -> list[int]:
    return [_card(db, item_factory, name) for name in ("a", "b", "c")]


def _revisions(db: Database, item_id: int) -> list[tuple[str, str | None, str | None]]:
    return [(r.field, r.old_value, r.new_value) for r in db.items.revisions(item_id)]


# ── ItemTagRepo: добавить и снять, не трогая остальное ─────────────────────


def test_add_returns_how_many_tags_are_new(db, item_factory):
    item_id = _card(db, item_factory, "a", tags=("регуляторика",))

    with db.transaction():
        added = db.tags.add(item_id, ["тренды", "регуляторика"])

    assert added == 1
    assert db.tags.names(item_id) == ["тренды", "регуляторика"]


def test_add_marks_the_new_tags_as_the_analysts(db, item_factory):
    item_id = _card(db, item_factory, "a", tags=("регуляторика",))

    with db.transaction():
        db.tags.add(item_id, ["тренды"])

    assert {t.tag: t.is_manual for t in db.tags.list(item_id)} == {
        "тренды": True, "регуляторика": False
    }


@pytest.mark.parametrize(
    "tags",
    [[], [""], ["", ""], ["   "], ["", "\t", "\n"]],
    ids=["empty", "blank", "blanks", "spaces", "whitespace"],
)
def test_add_of_nothing_changes_nothing(db, item_factory, tags):
    """Пробел — не тег: в `item_tags` он попасть не должен даже мимо сервиса."""
    item_id = _card(db, item_factory, "a")

    with db.transaction():
        assert db.tags.add(item_id, tags) == 0

    assert db.tags.names(item_id) == []


def test_add_stores_the_tag_stripped(db, item_factory):
    item_id = _card(db, item_factory, "a")

    with db.transaction():
        assert db.tags.add(item_id, ["  тренды  "]) == 1

    assert db.tags.names(item_id) == ["тренды"]


def test_a_padded_tag_does_not_duplicate_the_one_already_there(db, item_factory):
    """Иначе «тренды» и «тренды » стали бы двумя разными тегами одной карточки."""
    item_id = _card(db, item_factory, "a", tags=("тренды",))

    with db.transaction():
        assert db.tags.add(item_id, ["тренды  "]) == 0

    assert db.tags.names(item_id) == ["тренды"]


def test_the_service_refuses_a_whitespace_only_tag_before_the_repository(service, db,
                                                                        item_factory):
    item_id = _card(db, item_factory, "a")

    with pytest.raises(ItemValidationError, match="хотя бы один тег"):
        service.bulk_tags([item_id], add=["   "])

    assert db.tags.names(item_id) == []


def test_remove_drops_a_model_tag_too(db, item_factory):
    """Решение человека сильнее модели: снимается и то, что поставил ИИ."""
    item_id = _card(db, item_factory, "a", tags=("регуляторика", "тренды"))

    with db.transaction():
        removed = db.tags.remove(item_id, ["регуляторика", "которого-нет"])

    assert removed == 1
    assert db.tags.names(item_id) == ["тренды"]


def test_remove_of_nothing_touches_no_row(db, item_factory):
    item_id = _card(db, item_factory, "a", tags=("тренды",))

    with db.transaction():
        assert db.tags.remove(item_id, []) == 0

    assert db.tags.names(item_id) == ["тренды"]


# ── set_archived_bulk ──────────────────────────────────────────────────────


def test_set_archived_bulk_counts_only_the_rows_it_changed(db, cards):
    with db.transaction():
        assert db.items.set_archived_bulk(cards[:2], True) == 2
        assert db.items.set_archived_bulk(cards, True) == 1  # два уже в архиве

    assert [db.items.get(item_id).is_archived for item_id in cards] == [True, True, True]


def test_set_archived_bulk_collapses_repeated_ids(db, cards):
    with db.transaction():
        assert db.items.set_archived_bulk([cards[0], cards[0], cards[0]], True) == 1


def test_set_archived_bulk_of_an_empty_list_is_zero(db, cards):
    with db.transaction():
        assert db.items.set_archived_bulk([], True) == 0


# ── ItemService.bulk_tags ──────────────────────────────────────────────────


def test_bulk_tags_adds_one_tag_to_every_card(service, db, cards):
    result = service.bulk_tags(cards, add=["тренды"])

    assert result == {"changed": 3, "items": cards}
    assert [db.tags.names(item_id) for item_id in cards] == [["тренды"]] * 3
    assert [db.items.get(item_id).tags for item_id in cards] == [["тренды"]] * 3


def test_bulk_tags_writes_one_revision_per_changed_card(service, db, cards):
    service.bulk_tags(cards[:2], add=["тренды"])

    assert _revisions(db, cards[0]) == [("tags", "", "тренды")]
    assert _revisions(db, cards[1]) == [("tags", "", "тренды")]
    assert _revisions(db, cards[2]) == []


def test_bulk_tags_marks_the_field_as_touched_by_a_human(service, db, cards):
    """Иначе переобработка затрёт ручную разметку следующим же прогоном."""
    service.bulk_tags(cards[:1], add=["тренды"])

    assert db.items.get(cards[0]).manual_overrides == ["tags"]
    assert [t.is_manual for t in db.tags.list(cards[0])] == [True]


def test_repeating_the_same_bulk_tagging_changes_nothing(service, db, cards):
    service.bulk_tags(cards, add=["тренды"])

    result = service.bulk_tags(cards, add=["тренды"])

    assert result == {"changed": 0, "items": []}
    assert len(db.items.revisions(cards[0])) == 1


def test_bulk_tags_removes_a_tag_from_the_cards_that_have_it(service, db, item_factory):
    tagged = _card(db, item_factory, "a", tags=("регуляторика",))
    untouched = _card(db, item_factory, "b")

    result = service.bulk_tags([tagged, untouched], remove=["регуляторика"])

    assert result == {"changed": 1, "items": [tagged]}
    assert db.tags.names(tagged) == []


def test_bulk_tags_adds_and_removes_in_one_pass(service, db, item_factory):
    item_id = _card(db, item_factory, "a", tags=("регуляторика",))

    service.bulk_tags([item_id], add=["тренды"], remove=["регуляторика"])

    assert db.tags.names(item_id) == ["тренды"]
    assert db.items.get(item_id).tags == ["тренды"]


def test_a_model_tag_removed_by_hand_does_not_come_back_on_the_next_edit(service, db,
                                                                        item_factory):
    item_id = _card(db, item_factory, "a", tags=("регуляторика", "тренды"))
    service.bulk_tags([item_id], remove=["регуляторика"])

    service.edit_item(item_id, {"title": "Уточнённый заголовок"})

    assert db.tags.names(item_id) == ["тренды"]
    assert db.items.get(item_id).tags == ["тренды"]


@pytest.mark.parametrize(
    ("add", "remove"),
    [([], []), (None, None), ([""], []), ([], ["   "])],
    ids=["both-empty", "both-none", "blank-add", "blank-remove"],
)
def test_a_request_without_a_single_tag_is_a_validation_error(service, cards, add, remove):
    with pytest.raises(ItemValidationError, match="хотя бы один тег"):
        service.bulk_tags(cards, add=add, remove=remove)


def test_adding_and_removing_the_same_tag_is_a_validation_error(service, cards):
    with pytest.raises(ItemValidationError, match="регуляторика"):
        service.bulk_tags(cards, add=["регуляторика"], remove=["регуляторика", "тренды"])


def test_an_unknown_card_rolls_the_whole_batch_back(service, db, cards):
    with pytest.raises(ItemNotFoundError, match="#999"):
        service.bulk_tags([cards[0], 999, cards[1]], add=["тренды"])

    assert [db.tags.names(item_id) for item_id in cards] == [[], [], []]
    assert db.items.revisions(cards[0]) == []


def test_bulk_tags_without_any_card_does_nothing(service, cards):
    assert service.bulk_tags([], add=["тренды"]) == {"changed": 0, "items": []}


def test_repeated_ids_are_tagged_once(service, db, cards):
    result = service.bulk_tags([cards[0], cards[0]], add=["тренды"])

    assert result == {"changed": 1, "items": [cards[0]]}
    assert len(db.items.revisions(cards[0])) == 1


def test_the_search_index_finds_a_freshly_added_tag(config, file_db, card_factory,
                                                    source_factory, feed):
    item_id = card_factory(source_factory("Ведомости"), title="Обзор рынка")
    assert feed.items(FeedQuery.build(q="инвестиции"))["total"] == 0

    ItemService(config, file_db).bulk_tags([item_id], add=["инвестиции"])

    assert [r["id"] for r in feed.items(FeedQuery.build(q="инвестиции"))["items"]] == [item_id]


# ── ItemService.bulk_archive ───────────────────────────────────────────────


def test_bulk_archive_moves_every_card_into_the_archive(service, db, cards):
    result = service.bulk_archive(cards)

    assert result == {"changed": 3, "items": cards}
    assert [db.items.get(item_id).is_archived for item_id in cards] == [True, True, True]


def test_bulk_archive_writes_one_revision_per_card(service, db, cards):
    service.bulk_archive(cards[:1])

    assert _revisions(db, cards[0]) == [("is_archived", "0", "1")]
    assert db.items.revisions(cards[0])[0].edit_reason == "archive"


def test_archiving_what_is_already_archived_changes_nothing(service, db, cards):
    service.bulk_archive(cards)

    result = service.bulk_archive(cards)

    assert result == {"changed": 0, "items": []}
    assert len(db.items.revisions(cards[0])) == 1


def test_bulk_archive_brings_cards_back_when_asked(service, db, cards):
    service.bulk_archive(cards)

    result = service.bulk_archive(cards, archived=False)

    assert result == {"changed": 3, "items": cards}
    assert [db.items.get(item_id).is_archived for item_id in cards] == [False, False, False]
    assert db.items.revisions(cards[0])[-1].edit_reason == "unarchive"


def test_an_unknown_card_rolls_the_whole_archive_batch_back(service, db, cards):
    with pytest.raises(ItemNotFoundError, match="#999"):
        service.bulk_archive([cards[0], 999])

    assert db.items.get(cards[0]).is_archived is False
    assert db.items.revisions(cards[0]) == []


def test_bulk_archive_without_any_card_does_nothing(service, cards):
    assert service.bulk_archive([]) == {"changed": 0, "items": []}


def test_an_archived_card_leaves_the_feed(service, config, file_db, card_factory, source_factory,
                                          feed):
    item_id = card_factory(source_factory("Ведомости"), title="Обзор рынка")

    ItemService(config, file_db).bulk_archive([item_id])

    assert feed.items(FeedQuery.build())["total"] == 0
    assert [r["id"] for r in feed.items(FeedQuery.build(archived="only"))["items"]] == [item_id]


# ── HTTP ───────────────────────────────────────────────────────────────────


def test_bulk_tagging_over_http_answers_with_the_changed_cards(client, file_db, corpus):
    ids = [corpus["npa_high"], corpus["news_low"]]

    response = client.post(
        "/api/v1/items/tags/bulk", json={"item_ids": ids, "add": ["инвестиции"]}
    )

    assert response.status_code == 200
    assert response.json() == {"changed": 2, "items": ids}
    assert "инвестиции" in file_db.tags.names(corpus["npa_high"])


def test_a_card_that_already_carries_the_tag_is_not_counted_as_changed(client, file_db, corpus):
    ids = [corpus["npa_high"], corpus["news_low"]]  # у news_low «тренды» уже есть

    response = client.post("/api/v1/items/tags/bulk", json={"item_ids": ids, "add": ["тренды"]})

    assert response.json() == {"changed": 1, "items": [corpus["npa_high"]]}


def test_bulk_untagging_over_http(client, file_db, corpus):
    response = client.post(
        "/api/v1/items/tags/bulk",
        json={"item_ids": [corpus["npa_high"]], "remove": ["регуляторика"]},
    )

    assert response.json() == {"changed": 1, "items": [corpus["npa_high"]]}
    assert file_db.tags.names(corpus["npa_high"]) == []


def test_bulk_archiving_over_http(client, file_db, corpus):
    ids = [corpus["npa_high"], corpus["news_low"]]

    response = client.post("/api/v1/items/archive/bulk", json={"item_ids": ids})

    assert response.status_code == 200
    assert response.json() == {"changed": 2, "items": ids}
    assert file_db.items.get(corpus["npa_high"]).is_archived is True


def test_bulk_unarchiving_over_http(client, file_db, corpus):
    ids = [corpus["npa_high"]]
    client.post("/api/v1/items/archive/bulk", json={"item_ids": ids})

    response = client.post(
        "/api/v1/items/archive/bulk", json={"item_ids": ids, "archived": False}
    )

    assert response.json() == {"changed": 1, "items": ids}
    assert file_db.items.get(corpus["npa_high"]).is_archived is False


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/v1/items/tags/bulk", {"item_ids": [1], "add": ["тренды"]}),
        ("/api/v1/items/archive/bulk", {"item_ids": [1]}),
    ],
    ids=["tags", "archive"],
)
def test_an_unknown_card_is_a_404_problem(client, file_db, corpus, path, payload):
    unknown = max(corpus.values()) + 100

    response = client.post(path, json={**payload, "item_ids": [unknown]})

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "item_not_found"


def test_a_bulk_tagging_request_without_tags_is_a_400_problem(client, corpus):
    response = client.post(
        "/api/v1/items/tags/bulk", json={"item_ids": [corpus["npa_high"]]}
    )

    assert response.status_code == 400
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "validation_error"


def test_a_bulk_request_with_an_unknown_field_is_rejected_by_the_schema(client, corpus):
    response = client.post(
        "/api/v1/items/tags/bulk",
        json={"item_ids": [corpus["npa_high"]], "add": ["тренды"], "scope": "digest"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_the_batch_is_rolled_back_over_http_too(client, file_db, corpus):
    unknown = max(corpus.values()) + 100

    client.post(
        "/api/v1/items/tags/bulk",
        json={"item_ids": [corpus["npa_high"], unknown], "add": ["тренды"]},
    )

    assert "тренды" not in file_db.tags.names(corpus["npa_high"])


def test_the_service_error_families_stay_intact(service, cards):
    """CLI ловит `ItemError` семейством — оба отказа обязаны в него попадать."""
    with pytest.raises(ItemError):
        service.bulk_tags(cards, add=[], remove=[])
    with pytest.raises(ItemError):
        service.bulk_archive([999])
