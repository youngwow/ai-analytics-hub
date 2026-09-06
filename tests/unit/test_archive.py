"""Архив карточки: `ItemService.set_archived`, фильтр `archived` ленты и маршруты.

Архив — не удаление: карточка уходит из ленты и дайджеста, остаётся в базе и
показывается по запросу (`archived=include|only`). Каждое переключение — одна
ревизия `is_archived`; повтор без изменения ничего не пишет.
"""

from __future__ import annotations

import pytest

from src.exceptions import ItemError, QueryError
from src.models import RawDocument, Source
from src.models.queries import ARCHIVED_MODES, FeedQuery
from src.services.item_service import ItemService

NOW = "2026-09-02T12:00:00+00:00"
PROBLEM = "application/problem+json"


def _card(db, item_factory, external_id: str, **fields) -> int:
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
    return item_factory(db, document_id, **fields)


def _ids(result: dict) -> list[int]:
    return [row["id"] for row in result["items"]]


@pytest.fixture
def service(config, db, frozen_clock) -> ItemService:
    return ItemService(config, db)


@pytest.fixture
def card(db, item_factory) -> int:
    return _card(db, item_factory, "a")


# ── сервис ─────────────────────────────────────────────────────────────────


def test_archiving_marks_the_card_and_writes_one_revision(service, db, card):
    item = service.set_archived(card, True)

    assert item.is_archived is True
    assert db.items.get(card).is_archived is True
    revisions = db.items.revisions(card)
    assert [
        (r.field, r.old_value, r.new_value, r.edit_reason, r.source_of_change, r.actor)
        for r in revisions
    ] == [("is_archived", "0", "1", "archive", "human", "user")]
    assert revisions[0].created_at == NOW


def test_unarchiving_restores_the_card_and_writes_the_reverse_revision(service, db, card):
    service.set_archived(card, True)

    item = service.set_archived(card, False)

    assert item.is_archived is False
    assert db.items.get(card).is_archived is False
    assert [(r.old_value, r.new_value, r.edit_reason) for r in db.items.revisions(card)] == [
        ("0", "1", "archive"),
        ("1", "0", "unarchive"),
    ]


def test_archiving_an_archived_card_is_a_no_op_without_a_revision(service, db, card):
    service.set_archived(card, True)

    item = service.set_archived(card, True)

    assert item.is_archived is True
    assert len(db.items.revisions(card)) == 1


def test_unarchiving_a_live_card_is_a_no_op_without_a_revision(service, db, card):
    item = service.set_archived(card, False)

    assert item.is_archived is False
    assert db.items.revisions(card) == []


def test_the_actor_is_recorded_on_the_revision(service, db, card):
    service.set_archived(card, True, actor="young")

    assert db.items.revisions(card)[0].actor == "young"


def test_archiving_leaves_the_visibility_alone(service, db, card):
    service.set_archived(card, True)

    item = db.items.get(card)
    assert (item.visibility, item.hidden_reason) == ("visible", "")


def test_an_unknown_card_is_a_named_404(service):
    with pytest.raises(ItemError, match="#999") as excinfo:
        service.set_archived(999, True)

    assert (excinfo.value.code, excinfo.value.status_code) == ("item_not_found", 404)


def test_an_archived_bill_frees_its_key_for_the_next_publication(service, db, item_factory):
    tracked = _card(db, item_factory, "bill", type="npa", npa_key="1-8", npa_status="принят")

    service.set_archived(tracked, True)

    assert db.items.by_npa_key("1-8") is None
    successor = _card(db, item_factory, "bill-2", type="npa", npa_key="1-8", npa_status="действует")
    assert db.items.by_npa_key("1-8").id == successor


# ── лента ──────────────────────────────────────────────────────────────────


@pytest.fixture
def archived(config, file_db, corpus) -> int:
    """`news_low` ушла в архив; в ленте остаются пять карточек."""
    ItemService(config, file_db).set_archived(corpus["news_low"], True)
    return corpus["news_low"]


def test_the_feed_excludes_archived_cards_by_default(feed, corpus, archived):
    result = feed.items(FeedQuery.build(limit=50))

    assert archived not in _ids(result)
    assert result["total"] == len(result["items"]) == 5


def test_archived_include_shows_the_card_alongside_the_live_ones(feed, corpus, archived):
    result = feed.items(FeedQuery.build(archived="include", limit=50))

    assert archived in _ids(result)
    assert result["total"] == 6
    row = next(r for r in result["items"] if r["id"] == archived)
    assert (row["title"], row["visibility"]) == ("Обзор рынка спутникового вещания", "visible")


def test_archived_only_shows_nothing_but_the_archive(feed, corpus, archived):
    result = feed.items(FeedQuery.build(archived="only", limit=50))

    assert _ids(result) == [archived]
    assert result["total"] == 1


def test_the_archive_filter_composes_with_the_others(feed, corpus, archived):
    by_priority = feed.items(FeedQuery.build(priority=["low"], archived="include", limit=50))
    by_source = feed.items(FeedQuery.build(source_ids=[99], archived="only", limit=50))

    assert sorted(_ids(by_priority)) == sorted([archived, corpus["undated"]])
    assert (by_source["items"], by_source["total"]) == ([], 0)


def test_search_finds_an_archived_card_only_when_asked_for_the_archive(feed, corpus, archived):
    """Индексная строка остаётся; слово «консолидации» есть только в саммари `news_low`."""
    live = feed.items(FeedQuery.build(q="консолидации"))
    with_archive = feed.items(FeedQuery.build(q="консолидации", archived="include"))

    assert _ids(live) == []
    assert _ids(with_archive) == [archived]


@pytest.mark.parametrize(
    ("mode", "total"), [("exclude", 5), ("include", 6), ("only", 1)], ids=lambda v: str(v)
)
def test_facets_follow_the_archive_filter(feed, corpus, archived, mode, total):
    facets = feed.facets(FeedQuery.build(archived=mode))

    assert facets["total"] == feed.items(FeedQuery.build(archived=mode))["total"] == total
    assert facets["by_priority"]["low"] == {"exclude": 1, "include": 2, "only": 1}[mode]
    assert facets["by_type"] == {"exclude": {"npa": 2, "news": 3}, "include": {"npa": 2, "news": 4},
                                 "only": {"npa": 0, "news": 1}}[mode]


def test_the_digest_leaves_out_an_archived_card(feed, corpus, archived):
    result = feed.digest(FeedQuery.build(), fmt="markdown")

    assert result["items"] == 4
    assert "Обзор рынка спутникового вещания" not in result["body"]


@pytest.mark.parametrize("mode", ["include", "only"], ids=lambda s: s)
def test_the_digest_leaves_out_an_archived_card_even_when_asked_for_the_archive(
    feed, corpus, archived, mode
):
    """Архив не выгружается никогда — как и скрытое из дайджеста, что бы ни просили фильтры."""
    result = feed.digest(FeedQuery.build(archived=mode), fmt="markdown")

    assert "Обзор рынка спутникового вещания" not in result["body"]
    assert result["items"] == 4


def test_the_digest_endpoint_ignores_an_archive_filter_in_the_body(client, corpus):
    client.post(f"/api/v1/items/{corpus['news_low']}/archive")

    body = client.post("/api/v1/digest", json={"filters": {"archived": "include"}}).json()

    assert body["items"] == 4
    assert "Обзор рынка спутникового вещания" not in body["body"]


# ── FeedQuery.archived ─────────────────────────────────────────────────────


def test_the_archive_modes_are_exactly_exclude_include_and_only():
    assert ARCHIVED_MODES == ("exclude", "include", "only")


@pytest.mark.parametrize("given", [None, ""], ids=["none", "empty"])
def test_the_default_mode_is_exclude(given):
    assert FeedQuery.build(archived=given).archived == "exclude"
    assert FeedQuery.build().archived == "exclude"


@pytest.mark.parametrize("mode", ["bogus", "all", "ONLY", "1", "true"], ids=lambda s: s)
def test_an_unknown_mode_is_a_validation_error(mode):
    with pytest.raises(QueryError, match="archived") as excinfo:
        FeedQuery.build(archived=mode)

    assert (excinfo.value.code, excinfo.value.status_code) == ("validation_error", 400)


def test_the_cursor_fingerprint_changes_with_the_archive_mode():
    base = FeedQuery.build().fingerprint()

    assert FeedQuery.build(archived="exclude").fingerprint() == base
    assert FeedQuery.build(archived="include").fingerprint() != base
    assert FeedQuery.build(archived="only").fingerprint() != base
    assert FeedQuery.build(archived="include").fingerprint() != FeedQuery.build(archived="only").fingerprint()


def test_a_cursor_from_the_live_feed_is_refused_over_the_archive(feed, corpus, archived):
    token = feed.items(FeedQuery.build(limit=2))["next_cursor"]

    with pytest.raises(QueryError) as excinfo:
        feed.items(FeedQuery.build(limit=2, cursor=token, archived="include"))

    assert excinfo.value.code == "invalid_cursor"


def test_paging_over_the_archive_keeps_its_own_cursor(feed, corpus, archived):
    first = feed.items(FeedQuery.build(limit=4, archived="include"))

    second = feed.items(FeedQuery.build(limit=4, cursor=first["next_cursor"], archived="include"))

    assert set(_ids(first)) & set(_ids(second)) == set()
    assert sorted(_ids(first) + _ids(second)) == sorted(corpus[k] for k in (
        "hidden_digest", "npa_high", "news_medium", "news_low", "npa_medium", "undated"
    ))
    assert second["next_cursor"] is None


# ── HTTP ───────────────────────────────────────────────────────────────────


def test_archive_and_unarchive_over_http(client, file_db, corpus):
    item_id = corpus["news_low"]

    archived = client.post(f"/api/v1/items/{item_id}/archive")

    assert archived.status_code == 200
    assert archived.json() == {"id": item_id, "is_archived": True}
    assert item_id not in _ids(client.get("/api/v1/items", params={"limit": 50}).json())
    assert _ids(client.get("/api/v1/items", params={"archived": "only"}).json()) == [item_id]
    assert item_id in _ids(
        client.get("/api/v1/items", params={"archived": "include", "limit": 50}).json()
    )
    assert file_db.items.get(item_id).is_archived is True

    restored = client.post(f"/api/v1/items/{item_id}/unarchive")

    assert restored.json() == {"id": item_id, "is_archived": False}
    assert item_id in _ids(client.get("/api/v1/items", params={"limit": 50}).json())
    assert [r.edit_reason for r in file_db.items.revisions(item_id)] == ["archive", "unarchive"]


def test_archiving_over_http_twice_writes_one_revision(client, file_db, corpus):
    item_id = corpus["news_low"]
    client.post(f"/api/v1/items/{item_id}/archive")

    response = client.post(f"/api/v1/items/{item_id}/archive")

    assert response.json() == {"id": item_id, "is_archived": True}
    assert len(file_db.items.revisions(item_id)) == 1


@pytest.mark.parametrize("action", ["archive", "unarchive"], ids=lambda s: s)
def test_archiving_an_unknown_card_is_a_404_problem(client, action):
    response = client.post(f"/api/v1/items/999/{action}")

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "item_not_found"


@pytest.mark.parametrize("path", ["/api/v1/items", "/api/v1/items/facets"], ids=["feed", "facets"])
def test_an_unknown_archive_mode_is_a_400_problem(client, corpus, path):
    response = client.get(path, params={"archived": "bogus"})

    assert response.status_code == 400
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "validation_error"
    assert "archived" in response.json()["detail"]


def test_facets_over_http_follow_the_archive_mode(client, corpus):
    client.post(f"/api/v1/items/{corpus['news_low']}/archive")

    live = client.get("/api/v1/items/facets").json()
    archive = client.get("/api/v1/items/facets", params={"archived": "only"}).json()

    assert (live["total"], live["by_priority"]["low"]) == (5, 1)
    assert (archive["total"], archive["by_priority"]["low"]) == (1, 1)


def test_the_digest_endpoint_leaves_out_an_archived_card(client, corpus):
    client.post(f"/api/v1/items/{corpus['news_low']}/archive")

    body = client.post("/api/v1/digest", json={"filters": {}}).json()

    assert body["items"] == 4
    assert "Обзор рынка спутникового вещания" not in body["body"]
