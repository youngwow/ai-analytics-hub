"""Объединение карточек-дублей: `ItemService.merge`, `dismiss_duplicate`, маршруты и CLI.

Кластер — это предложение, объединяет человек. Объединение переносит
`item_sources`, заметки и ручные теги к принимающей карточке, а поглощённые
остаются в базе как `deleted` с причиной «объединена с #N». НПА не объединяются.
"""

from __future__ import annotations

import json

import pytest

from src import cli
from src.exceptions import ItemError
from src.models import ItemRevision, RawDocument, Source
from src.models.queries import FeedQuery
from src.repositories.items import merge_reason
from src.services.feed_service import FeedService
from src.services.item_service import DUPLICATE_FIELD, MERGED_FIELD, ItemService

NOW = "2026-09-02T12:00:00+00:00"


@pytest.fixture
def source(file_db) -> Source:
    return file_db.sources.add(
        Source(name="Лента", url="https://a.ru/", kind="rss", fetch_url="https://a.ru/rss")
    )


def _card(db, source, item_factory, external_id: str, **fields) -> int:
    title = fields.pop("title", f"Карточка {external_id}")
    with db.transaction():
        document_id = db.documents.insert(
            RawDocument(
                source_id=source.id,
                external_id=external_id,
                url=f"https://a.ru/{external_id}",
                title=title,
                text=f"Текст {external_id}. Второе предложение. Третье.",
                published_at=NOW,
                fetched_at=NOW,
            )
        )
        db.documents.set_derived(document_id, norm_text=f"Текст {external_id}.")
    item_id = item_factory(
        db, document_id, title=title, summary=f"Саммари {external_id}.", published_at=NOW, **fields
    )
    with db.transaction():
        db.search.rebuild(item_id)
    return item_id


def _propose(db, item_id: int, partners: list[int], similarity: float = 0.91) -> None:
    with db.transaction():
        db.items.add_revision(
            ItemRevision(
                item_id=item_id,
                field=DUPLICATE_FIELD,
                new_value=json.dumps({"items": partners, "similarity": similarity, "run_id": 1}),
                actor="model",
                source_of_change="llm",
                edit_reason="clustering",
            )
        )


@pytest.fixture
def pair(file_db, source, item_factory) -> tuple[int, int]:
    target = _card(file_db, source, item_factory, "a", title="ТАСС коротко")
    other = _card(
        file_db, source, item_factory, "b", title="Коммерсантъ развёрнуто", priority="high"
    )
    _propose(file_db, target, [other])
    _propose(file_db, other, [target])
    return target, other


@pytest.fixture
def items(config, file_db) -> ItemService:
    return ItemService(config, file_db)


# ── merge ──────────────────────────────────────────────────────────────────


def test_merge_relinks_item_sources_to_the_surviving_card(items, file_db, pair):
    target, other = pair
    other_document = file_db.items.sources(other)[0]["id"]

    result = items.merge(target, [other])

    assert result["absorbed"] == [other] and result["sources_count"] == 2
    sources = file_db.items.sources(target)
    assert [s["id"] for s in sources] == sorted(s["id"] for s in sources)
    assert {s["id"] for s in sources} >= {other_document}
    assert [s["is_canonical"] for s in sources] == [1, 0]  # первоисточник остался у target
    assert file_db.items.sources(other) == []
    assert file_db.items.item_for_document(other_document) == target
    assert file_db.clusters.get(file_db.items.get(target).cluster_id).size == 2


def test_the_absorbed_card_is_deleted_with_a_reason_and_leaves_the_feed(
    items, config, file_db, pair
):
    target, other = pair
    items.merge(target, [other])

    absorbed = file_db.items.get(other)
    assert absorbed.visibility == "deleted"
    assert absorbed.hidden_reason == merge_reason(target)
    merged = file_db.items.last_revision(other, MERGED_FIELD)
    assert merged.new_value == str(target) and merged.source_of_change == "human"
    feed = FeedService(config, file_db).items(FeedQuery.build(timezone_name="UTC"))
    assert [row["id"] for row in feed["items"]] == [target]
    assert feed["items"][0]["sources_count"] == 2
    assert feed["items"][0]["flags"]["duplicate"] is False  # предложение закрыто


def test_merge_closes_the_proposal_and_keeps_the_history(items, file_db, pair):
    target, other = pair
    items.merge(target, [other], reason="один и тот же релиз")

    assert items.duplicate_proposal(target) is None
    closed = file_db.items.last_revision(target, DUPLICATE_FIELD)
    assert closed.source_of_change == "human" and closed.edit_reason == "один и тот же релиз"
    assert json.loads(closed.new_value) == {"merged": [other]}
    assert json.loads(closed.old_value)["items"] == [other]


def test_merge_takes_the_highest_priority_and_locks_it(items, file_db, pair):
    target, other = pair  # target medium, other high
    result = items.merge(target, [other])

    assert result["item"].priority == "high"
    stored = file_db.items.get(target)
    assert stored.priority == "high" and "priority" in stored.manual_overrides
    assert any(
        r.field == "priority" and r.new_value == "high" for r in file_db.items.revisions(target)
    )


def test_merge_moves_notes_and_manual_tags(items, file_db, pair):
    target, other = pair
    items.add_note(other, "обсудить на планёрке", author="young")
    with file_db.transaction():
        file_db.tags.add(other, ["ручной"], is_manual=True)
        file_db.tags.add(other, ["модельный"], is_manual=False)

    items.merge(target, [other])

    assert [n.body for n in file_db.notes.list(target)] == ["обсудить на планёрке"]
    assert file_db.notes.list(other) == []
    assert file_db.tags.manual_names(target) == ["ручной"]
    assert "модельный" not in file_db.tags.names(target)
    assert file_db.items.get(target).tags == ["ручной"]


def test_merge_removes_the_absorbed_card_from_the_search_index(items, config, file_db, pair):
    target, other = pair
    items.merge(target, [other])

    feed = FeedService(config, file_db)
    hits = feed.items(FeedQuery.build(q="Коммерсантъ", timezone_name="UTC", include_hidden=True))
    assert [row["id"] for row in hits["items"]] == []
    row = file_db.conn.execute(
        "SELECT count(*) FROM items_search WHERE rowid=?", (other,)
    ).fetchone()
    assert row[0] == 0


def test_merge_of_three_cards_absorbs_each(items, file_db, source, item_factory, pair):
    target, other = pair
    third = _card(file_db, source, item_factory, "c")

    result = items.merge(target, [other, third, other])

    assert result["absorbed"] == [other, third] and result["sources_count"] == 3


@pytest.mark.parametrize("make_npa", ["target", "other"])
def test_npa_cards_are_never_merged(items, file_db, pair, make_npa):
    target, other = pair
    victim = target if make_npa == "target" else other
    card = file_db.items.get(victim)
    card.type, card.npa_key = "npa", "112233-8"
    with file_db.transaction():
        file_db.items.update(card)

    with pytest.raises(ItemError, match="npa_key"):
        items.merge(target, [other])
    assert file_db.items.sources(other) != []


def test_merge_needs_another_card_and_refuses_deleted_ones(items, file_db, pair):
    target, other = pair
    with pytest.raises(ItemError, match="хотя бы одна"):
        items.merge(target, [target])
    with pytest.raises(ItemError) as excinfo:
        items.merge(target, [999])
    assert excinfo.value.code == "item_not_found"
    items.merge(target, [other])
    with pytest.raises(ItemError, match="уже объединена"):
        items.merge(target, [other])


def test_a_merged_card_cannot_be_restored(items, pair):
    target, other = pair
    items.merge(target, [other])
    with pytest.raises(ItemError, match="объединена"):
        items.set_visibility(other, restore=True)


def test_a_partner_that_disappeared_drops_out_of_the_proposal(items, file_db, pair):
    target, other = pair
    items.set_visibility(other, "deleted")
    assert items.duplicate_proposal(target) is None


# ── dismiss ────────────────────────────────────────────────────────────────


def test_dismiss_closes_the_proposal_on_the_whole_group(items, file_db, pair):
    target, other = pair
    result = items.dismiss_duplicate(target)

    assert result == {"id": target, "dismissed": [other]}
    for member in (target, other):
        revision = file_db.items.last_revision(member, DUPLICATE_FIELD)
        assert revision.source_of_change == "human" and revision.edit_reason == "not_duplicate"
        assert items.duplicate_proposal(member) is None
    with pytest.raises(ItemError) as excinfo:
        items.dismiss_duplicate(target)
    assert excinfo.value.code == "no_duplicate_proposal"


# ── HTTP ───────────────────────────────────────────────────────────────────


def test_the_card_carries_the_proposal_and_the_feed_flags_it(client, pair):
    target, other = pair
    card = client.get(f"/api/v1/items/{target}").json()
    assert card["duplicate_proposal"]["items"] == [
        {"id": other, "title": "Коммерсантъ развёрнуто", "published_at": NOW}
    ]
    assert card["duplicate_proposal"]["similarity"] == 0.91
    feed = client.get("/api/v1/items").json()
    assert {row["id"]: row["flags"]["duplicate"] for row in feed["items"]} == {
        target: True,
        other: True,
    }


def test_merge_over_http(client, file_db, pair):
    target, other = pair
    response = client.post(f"/api/v1/items/{target}/merge", json={"item_ids": [other]})

    assert response.status_code == 200
    body = response.json()
    assert body["absorbed"] == [other] and body["sources_count"] == 2
    assert body["item"]["id"] == target and body["item"]["priority"] == "high"
    assert client.get(f"/api/v1/items/{target}").json()["duplicate_proposal"] is None
    assert file_db.items.get(other).visibility == "deleted"


def test_merge_validation_over_http(client, pair):
    target, other = pair
    assert client.post(f"/api/v1/items/{target}/merge", json={"item_ids": []}).status_code == 422
    response = client.post(f"/api/v1/items/{target}/merge", json={"item_ids": [target]})
    assert (response.status_code, response.json()["code"]) == (400, "validation_error")


def test_dismiss_over_http(client, pair):
    target, other = pair
    response = client.post(f"/api/v1/items/{target}/not-duplicate")
    assert response.status_code == 200 and response.json() == {"id": target, "dismissed": [other]}
    again = client.post(f"/api/v1/items/{target}/not-duplicate")
    assert (again.status_code, again.json()["code"]) == (409, "no_duplicate_proposal")


# ── CLI ────────────────────────────────────────────────────────────────────


def test_merge_and_not_duplicate_from_the_cli(hub_paths, file_db, pair, capsys, monkeypatch):
    target, other = pair
    monkeypatch.setattr(
        cli, "DEFAULT_PATHS", hub_paths
    )  # CLI берёт корень из модуля, не из HUB_ROOT
    assert cli.main(["item", str(target)]) == 0
    assert "вероятный дубль" in capsys.readouterr().out

    assert cli.main(["merge", str(target), str(other)]) == 0
    out = capsys.readouterr().out
    assert f"#{target}: объединена с #{other}" in out and "публикаций теперь 2" in out
    assert file_db.items.get(other).visibility == "deleted"

    assert cli.main(["not-duplicate", str(target)]) != 0  # предложение уже закрыто
