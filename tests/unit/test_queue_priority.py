"""Приоритет очереди обработки: НПА от регулятора не ждёт за лентой СМИ (план 4.2).

`unprocessed` берёт не «самое свежее», а «самое важное из свежего»: сначала вес
категории источника (`ProcessingConfig.category_weights`), и только внутри веса —
дата. При очереди в тысячи документов и `max_new_per_run=200` именно это решает,
что попадёт в ленту сегодня.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from support import NEWS_TEXT, FakeLLM, news_answer

from src.config import Config
from src.models import RawDocument, Source
from src.repositories import Database
from src.repositories.documents import DEFAULT_CATEGORY_WEIGHTS
from src.services.processing_service import ProcessingService

OLD = "2026-09-01T09:00:00+00:00"
NEW = "2026-09-04T09:00:00+00:00"
FLAT_WEIGHTS = {"regulator": 1, "telegram": 1, "media": 1, "manual": 1}


@pytest.fixture
def queue(db) -> dict[str, int]:
    """Старый документ регулятора и свежий документ СМИ — очередь на выбор."""
    return {
        "old_regulator": _document(db, "regulator", "cbr", published_at=OLD),
        "new_media": _document(db, "media", "vedomosti", published_at=NEW),
    }


def _document(db: Database, category: str, host: str, *, published_at: str | None,
              external_id: str = "d1") -> int:
    fetch_url = f"https://{host}.ru/rss"
    source = db.sources.get_by_fetch_url(fetch_url) or db.sources.add(
        Source(name=host, url=f"https://{host}.ru/", kind="rss", category=category,
               fetch_url=fetch_url)
    )
    with db.transaction():
        return db.documents.insert(
            RawDocument(
                source_id=source.id,
                external_id=external_id,
                url=f"https://{host}.ru/{external_id}",
                title=f"Материал {host}",
                text=NEWS_TEXT,
                published_at=published_at,
                fetched_at=NEW,
            )
        )


def _order(db: Database, **kwargs) -> list[int]:
    return [r["id"] for r in db.documents.unprocessed(**kwargs)]


# ── вес категории идёт впереди даты ────────────────────────────────────────


def test_an_older_regulator_document_goes_before_a_newer_media_one(db, queue):
    assert _order(db) == [queue["old_regulator"], queue["new_media"]]


def test_flat_weights_give_the_plain_date_order_back(db, queue):
    assert _order(db, category_weights=FLAT_WEIGHTS) == [
        queue["new_media"], queue["old_regulator"]
    ]


def test_an_empty_table_of_weights_switches_the_priority_off(db, queue):
    """Пустой словарь — это «без приоритета»: SQL подставляет NULL, а не 0-й столбец."""
    assert _order(db, category_weights={}) == [queue["new_media"], queue["old_regulator"]]


def test_an_unknown_category_sorts_last(db, queue):
    unknown = _document(db, "форум", "forum", published_at=NEW)

    assert _order(db) == [queue["old_regulator"], queue["new_media"], unknown]


def test_telegram_sits_between_the_regulator_and_the_media(db, queue):
    channel = _document(db, "telegram", "tme", published_at=OLD)

    assert _order(db) == [queue["old_regulator"], channel, queue["new_media"]]


def test_inside_one_category_the_newest_still_comes_first(db):
    old = _document(db, "media", "vedomosti", published_at=OLD, external_id="old")
    new = _document(db, "media", "vedomosti", published_at=NEW, external_id="new")

    assert _order(db) == [new, old]


def test_a_document_without_a_date_keeps_its_place_inside_the_category(db, queue):
    undated = _document(db, "regulator", "cbr", published_at=None, external_id="undated")

    assert _order(db) == [queue["old_regulator"], undated, queue["new_media"]]


def test_the_priority_does_not_change_which_documents_are_offered(db, queue, item_factory):
    item_factory(db, queue["old_regulator"])

    assert _order(db) == [queue["new_media"]]
    assert _order(db, limit=1) == [queue["new_media"]]


def test_a_custom_table_can_put_the_media_first(db, queue):
    assert _order(db, category_weights={"media": 9, "regulator": 1}) == [
        queue["new_media"], queue["old_regulator"]
    ]


# ── что берёт прогон ───────────────────────────────────────────────────────


def _service(config: Config, db: Database, weights: dict | None = None) -> ProcessingService:
    if weights is not None:
        config = replace(config, processing=replace(config.processing, category_weights=weights))
    return ProcessingService(config, db, provider=FakeLLM(news_answer()), embedder=None)


def test_a_run_of_one_document_takes_the_regulator_not_the_freshest(config, db, queue,
                                                                    frozen_clock):
    _service(config, db).run(limit=1)

    assert db.items.item_for_document(queue["old_regulator"]) is not None
    assert db.items.item_for_document(queue["new_media"]) is None


def test_the_same_run_with_flat_weights_takes_the_freshest(config, db, queue, frozen_clock):
    _service(config, db, FLAT_WEIGHTS).run(limit=1)

    assert db.items.item_for_document(queue["new_media"]) is not None
    assert db.items.item_for_document(queue["old_regulator"]) is None


def test_the_run_hands_the_configured_table_to_the_queue(config, db, monkeypatch, frozen_clock):
    """Вес приходит из конфигурации, а не из кода репозитория."""
    seen: dict = {}
    original = db.documents.unprocessed

    def spy(**kwargs):
        seen.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(db.documents, "unprocessed", spy)

    _service(config, db, {"regulator": 5}).run(dry_run=True)

    assert seen["category_weights"] == {"regulator": 5}
    assert seen["only_failed"] is False


def test_the_repository_default_matches_the_configured_one(config):
    assert config.processing.category_weights == DEFAULT_CATEGORY_WEIGHTS
    assert DEFAULT_CATEGORY_WEIGHTS == {"regulator": 3, "telegram": 2, "media": 1, "manual": 1}
