"""src/services/item_service.py — видимость карточек: ничего не удаляется физически."""

from __future__ import annotations

import pytest

from src.exceptions import ItemError
from src.models import RawDocument, Source
from src.services.item_service import ItemService

NOW = "2026-09-02T12:00:00+00:00"


@pytest.fixture
def service(config, db) -> ItemService:
    """Видимость не зовёт модель, поэтому сервис карточек строится без провайдера."""
    return ItemService(config, db)


def _source(db, name: str = "Лента") -> Source:
    return db.sources.add(
        Source(name=name, url="https://a.ru/", kind="rss", fetch_url=f"https://a.ru/{name}")
    )


def _card(db, item_factory, source: Source, external_id: str, **overrides) -> int:
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
    return item_factory(db, document_id, **overrides)


def test_a_new_card_is_visible(db, item_factory):
    item_id = _card(db, item_factory, _source(db), "a")
    assert db.items.get(item_id).visibility == "visible"


@pytest.mark.parametrize(
    "scope, expected",
    [("feed", "hidden_feed"), ("digest", "hidden_digest"), ("deleted", "deleted")],
)
def test_hiding_moves_the_card_to_the_requested_state(
    service, db, item_factory, scope, expected
):
    item_id = _card(db, item_factory, _source(db), "a")

    item = service.set_visibility(item_id, scope, reason="нерелевантно")

    assert item.visibility == expected
    assert db.items.get(item_id).visibility == expected
    assert db.items.get(item_id).hidden_reason == "нерелевантно"


def test_restore_brings_a_card_back_and_clears_the_reason(service, db, item_factory):
    item_id = _card(db, item_factory, _source(db), "a")
    service.set_visibility(item_id, "digest", reason="для инфраструктурного отдела")

    service.set_visibility(item_id, restore=True)

    restored = db.items.get(item_id)
    assert (restored.visibility, restored.hidden_reason) == ("visible", "")


def test_an_unknown_scope_is_rejected(service, db, item_factory):
    item_id = _card(db, item_factory, _source(db), "a")
    with pytest.raises(ItemError) as excinfo:
        service.set_visibility(item_id, "куда-нибудь")
    assert excinfo.value.code == "validation_error"


def test_hiding_a_missing_card_says_so(service):
    with pytest.raises(ItemError) as excinfo:
        service.set_visibility(999, "feed")
    assert excinfo.value.code == "item_not_found"


def test_hiding_from_the_digest_keeps_the_card_in_the_feed(service, db, item_factory):
    """Решение владельца от 2026-09-05: подготовка выжимки не чистит ленту всем сразу."""
    source = _source(db)
    ids = [_card(db, item_factory, source, external) for external in ("a", "b", "c")]

    service.set_visibility(ids[0], "digest")

    assert len(db.items.list(limit=10)) == 3
    assert db.items.get(ids[0]).visibility == "hidden_digest"


@pytest.mark.parametrize("scope, expected", [("feed", "hidden_feed"), ("deleted", "deleted")])
def test_hiding_from_the_feed_or_deleting_takes_the_card_out(
    service, db, item_factory, scope, expected
):
    source = _source(db)
    ids = [_card(db, item_factory, source, external) for external in ("a", "b", "c")]

    service.set_visibility(ids[0], scope)

    assert sorted(r["id"] for r in db.items.list(limit=10)) == ids[1:]
    assert len(db.items.list(limit=10, include_hidden=True)) == 3
    assert db.items.get(ids[0]).visibility == expected


def test_bulk_hiding_is_one_operation_over_many_cards(service, db, item_factory):
    source = _source(db)
    ids = [_card(db, item_factory, source, external) for external in ("a", "b", "c")]

    changed = service.bulk_visibility(ids, "digest", reason="дайджест для серверов")

    assert changed == 3
    assert [db.items.get(i).visibility for i in ids] == ["hidden_digest"] * 3
    assert len(db.items.list(limit=10)) == 3  # из ленты не пропали
    assert service.bulk_visibility(ids, "visible") == 3
    assert [db.items.get(i).visibility for i in ids] == ["visible"] * 3


def test_bulk_hiding_from_the_feed_empties_it(service, db, item_factory):
    source = _source(db)
    ids = [_card(db, item_factory, source, external) for external in ("a", "b", "c")]

    assert service.bulk_visibility(ids, "feed", reason="источник удалён") == 3

    assert db.items.list(limit=10) == []
    assert len(db.items.list(limit=10, include_hidden=True)) == 3


def test_bulk_hiding_an_empty_list_changes_nothing(service):
    assert service.bulk_visibility([], "digest") == 0


def test_deleting_a_source_can_hide_its_cards_without_deleting_them(db, item_factory):
    source = _source(db)
    ids = [_card(db, item_factory, source, external) for external in ("a", "b")]

    hidden = db.items.hide_by_source(source.id)

    assert hidden == 2
    assert [db.items.get(i).visibility for i in ids] == ["hidden_feed"] * 2
    assert db.items.count() == 2
