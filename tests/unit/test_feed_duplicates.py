"""Фильтр ленты `duplicate`: только карточки с открытым предложением «вероятный дубль».

Порог — сходство группы из ревизии модели (`duplicate_of`); проценты и доли
читаются одинаково. Один и тот же `FeedQuery` — на CLI, HTTP и в дайджесте.
"""

from __future__ import annotations

import json

import pytest

from src import cli
from src.exceptions import QueryError
from src.models import ItemRevision, RawDocument, Source
from src.models.queries import FeedQuery
from src.services.feed_service import FeedService
from src.services.item_service import DUPLICATE_FIELD, ItemService

NOW = "2026-09-02T12:00:00+00:00"


@pytest.fixture
def source(file_db) -> Source:
    return file_db.sources.add(
        Source(name="Лента", url="https://a.ru/", kind="rss", fetch_url="https://a.ru/rss")
    )


def _card(db, source, item_factory, external_id: str) -> int:
    with db.transaction():
        document_id = db.documents.insert(
            RawDocument(
                source_id=source.id,
                external_id=external_id,
                url=f"https://a.ru/{external_id}",
                title=f"Карточка {external_id}",
                fetched_at=NOW,
                published_at=NOW,
            )
        )
    return item_factory(db, document_id, title=f"Карточка {external_id}", published_at=NOW)


def _propose(db, item_id: int, partners: list[int], similarity: float | None) -> None:
    payload = {"items": partners, "run_id": 1}
    if similarity is not None:
        payload["similarity"] = similarity
    with db.transaction():
        db.items.add_revision(
            ItemRevision(
                item_id=item_id,
                field=DUPLICATE_FIELD,
                new_value=json.dumps(payload),
                actor="model",
                source_of_change="llm",
                edit_reason="clustering",
            )
        )


@pytest.fixture
def cards(file_db, source, item_factory) -> dict[str, int]:
    """Четыре карточки: сходство 0.91, 0.82, без числа, и без предложения вовсе."""
    strong = _card(file_db, source, item_factory, "strong")
    weak = _card(file_db, source, item_factory, "weak")
    blank = _card(file_db, source, item_factory, "blank")
    plain = _card(file_db, source, item_factory, "plain")
    _propose(file_db, strong, [weak], 0.91)
    _propose(file_db, weak, [strong], 0.82)
    _propose(file_db, blank, [plain], None)
    return {"strong": strong, "weak": weak, "blank": blank, "plain": plain}


def _ids(config, file_db, **query) -> list[int]:
    feed = FeedService(config, file_db).items(FeedQuery.build(timezone_name="UTC", **query))
    return sorted(row["id"] for row in feed["items"])


# ── FeedQuery ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.8", 0.8),
        ("80", 0.8),
        (88, 0.88),
        (0, 0.0),
        ("0", 0.0),
        (1, 1.0),
        ("100", 1.0),
        ("1.5", 0.015),
        (True, 0.0),
    ],
)
def test_the_threshold_reads_fractions_and_percents_alike(raw, expected):
    assert FeedQuery.build(duplicate=raw).duplicate == pytest.approx(expected)


@pytest.mark.parametrize("raw", [None, "", False])
def test_an_empty_threshold_means_no_filter(raw):
    assert FeedQuery.build(duplicate=raw).duplicate is None


@pytest.mark.parametrize("raw", ["много", "-0.1", "101"])
def test_a_bad_threshold_is_a_validation_error(raw):
    with pytest.raises(QueryError):
        FeedQuery.build(duplicate=raw)


def test_the_threshold_is_part_of_the_cursor_fingerprint():
    assert FeedQuery.build(duplicate=0.8).fingerprint() != FeedQuery.build().fingerprint()


# ── лента ──────────────────────────────────────────────────────────────────


def test_without_the_filter_every_card_is_listed_with_its_similarity(config, file_db, cards):
    feed = FeedService(config, file_db).items(FeedQuery.build(timezone_name="UTC"))
    by_id = {row["id"]: row for row in feed["items"]}
    assert len(by_id) == 4
    assert by_id[cards["strong"]]["duplicate_similarity"] == pytest.approx(0.91)
    assert by_id[cards["strong"]]["flags"]["duplicate"] is True
    assert by_id[cards["blank"]]["duplicate_similarity"] is None
    assert by_id[cards["blank"]]["flags"]["duplicate"] is True
    assert by_id[cards["plain"]]["duplicate_similarity"] is None
    assert by_id[cards["plain"]]["flags"]["duplicate"] is False


def test_any_similarity_lists_every_open_proposal(config, file_db, cards):
    assert _ids(config, file_db, duplicate=0) == sorted(
        [cards["strong"], cards["weak"], cards["blank"]]
    )


def test_a_threshold_keeps_only_groups_at_least_that_similar(config, file_db, cards):
    assert _ids(config, file_db, duplicate="80") == sorted([cards["strong"], cards["weak"]])
    assert _ids(config, file_db, duplicate=0.88) == [cards["strong"]]
    assert _ids(config, file_db, duplicate=0.95) == []


def test_a_closed_proposal_drops_out_of_the_filter(config, file_db, cards):
    ItemService(config, file_db).dismiss_duplicate(cards["strong"])
    assert _ids(config, file_db, duplicate=0) == [cards["blank"]]


def test_the_filter_combines_with_the_other_filters(config, file_db, cards):
    assert _ids(config, file_db, duplicate=0, priority=["high"]) == []


# ── HTTP и CLI ─────────────────────────────────────────────────────────────


def test_the_http_feed_accepts_percents_and_reports_the_total(client, cards):
    body = client.get("/api/v1/items", params={"duplicate": "88"}).json()
    assert [row["id"] for row in body["items"]] == [cards["strong"]]
    assert body["total"] == 1
    assert body["items"][0]["duplicate_similarity"] == pytest.approx(0.91)
    facets = client.get("/api/v1/items/facets", params={"duplicate": "0"}).json()
    assert facets["total"] == 3


def test_a_bad_http_threshold_is_a_400(client, cards):
    response = client.get("/api/v1/items", params={"duplicate": "много"})
    assert response.status_code == 400


def test_the_digest_takes_the_same_filter(client, cards):
    body = client.post(
        "/api/v1/digest",
        json={
            "filters": {"duplicate": 0.88},
            "format": "json",
            "title": "t",
            "include_notes": False,
        },
    ).json()
    assert body["items"] == 1


def test_the_cli_lists_only_probable_duplicates(hub_paths, file_db, cards, capsys, monkeypatch):
    monkeypatch.setattr(cli, "DEFAULT_PATHS", hub_paths)
    assert cli.main(["items", "--duplicate", "80"]) == 0
    out = capsys.readouterr().out
    assert "≈91%" in out and "≈82%" in out and "Карточка plain" not in out
    assert cli.main(["items", "--duplicate"]) == 0
    assert "Карточка blank" in capsys.readouterr().out
