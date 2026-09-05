"""src/services/item_service.py — версия модели живёт в истории, а не в колонках `*_ai`.

Правка человека не должна затираться, но и вернуться к тому, что предлагала
модель, обязано быть одним действием (US-8). Карточку создаёт `ProcessingService`
(ему нужна модель); правит и откатывает — `ItemService`, доступный как `.items`.
"""

from __future__ import annotations

import pytest
from support import FakeLLM, news_answer

from src.exceptions import ItemError
from src.models import RawDocument, Source
from src.services.item_service import ItemService
from src.services.processing_service import ProcessingService

NOW = "2026-09-02T12:00:00+00:00"
TEXT = (
    "Минцифры внесло законопроект об аккредитации ИИ-сервисов. "
    "Документ описывает требования к операторам. Вступление в силу с 2027 года."
)


@pytest.fixture
def carded(config, db, frozen_clock):
    """Карточка, созданная моделью: у неё есть ревизии `llm` для отката."""
    source = db.sources.add(
        Source(name="Лента", url="https://a.ru/", kind="rss", fetch_url="https://a.ru/rss")
    )
    with db.transaction():
        db.documents.insert(
            RawDocument(
                source_id=source.id,
                external_id="a",
                url="https://a.ru/a",
                title="Аккредитация ИИ-сервисов",
                text=TEXT,
                published_at=NOW,
                fetched_at=NOW,
            )
        )
    provider = FakeLLM(news_answer())
    processing = ProcessingService(config, db, provider=provider, embedder=None)
    processing.run(limit=5)
    item = db.items.list(limit=1)[0]
    return processing, provider, int(item["id"])


def test_creating_a_card_records_what_the_model_produced(db, carded):
    _, _, item_id = carded
    fields = {r.field for r in db.items.revisions(item_id) if r.source_of_change == "llm"}
    assert {"summary", "priority", "title", "type", "tags"} <= fields


def test_revert_restores_the_model_value_and_unlocks_the_field(db, carded):
    processing, _, item_id = carded
    items = processing.items
    model_summary = db.items.get(item_id).summary
    items.edit_item(item_id, {"summary": "Своими словами про нас."}, reason="wrong_focus")
    assert db.items.get(item_id).manual_overrides == ["summary"]

    reverted = items.revert(item_id, "summary")

    assert reverted.summary == model_summary
    assert reverted.manual_overrides == []
    assert db.items.get(item_id).summary == model_summary


def test_revert_is_itself_written_to_the_history(db, carded):
    processing, _, item_id = carded
    processing.items.edit_item(item_id, {"priority": "low"})

    processing.items.revert(item_id, "priority")

    reasons = [r.edit_reason for r in db.items.revisions(item_id)]
    assert "revert" in reasons


def test_revert_without_a_model_version_is_refused(config, db, item_factory):
    source = db.sources.add(
        Source(name="Лента", url="https://a.ru/", kind="rss", fetch_url="https://a.ru/rss")
    )
    with db.transaction():
        document_id = db.documents.insert(
            RawDocument(source_id=source.id, external_id="a", url="https://a.ru/a", fetched_at=NOW)
        )
    item_id = item_factory(db, document_id)
    service = ItemService(config, db)

    with pytest.raises(ItemError) as excinfo:
        service.revert(item_id, "summary")

    assert excinfo.value.code == "nothing_to_revert"


def test_only_editable_fields_can_be_reverted(db, carded):
    processing, _, item_id = carded
    with pytest.raises(ItemError) as excinfo:
        processing.items.revert(item_id, "confidence")
    assert excinfo.value.code == "validation_error"


def test_reprocessing_records_the_new_proposal_even_for_a_locked_field(db, carded):
    processing, provider, item_id = carded
    processing.items.edit_item(item_id, {"summary": "Ручное саммари."})
    provider.answers = [news_answer(summary=["Совсем другое.", "Второе.", "Третье."])]

    processing.reprocess(item_id)

    assert db.items.get(item_id).summary == "Ручное саммари."
    proposal = db.items.last_model_value(item_id, "summary")
    assert proposal is not None and "Совсем другое." in proposal.new_value
    assert processing.items.revert(item_id, "summary").summary.startswith("Совсем другое.")


def test_an_empty_summary_is_refused_because_hiding_has_its_own_command(db, carded):
    processing, _, item_id = carded
    before = db.items.get(item_id).summary

    with pytest.raises(ItemError) as excinfo:
        processing.items.edit_item(item_id, {"summary": "   "})

    assert excinfo.value.code == "validation_error"
    assert db.items.get(item_id).summary == before


def test_editing_a_field_to_the_same_value_writes_nothing(db, carded):
    processing, _, item_id = carded
    current = db.items.get(item_id)
    before = len(db.items.revisions(item_id))

    processing.items.edit_item(item_id, {"priority": current.priority})

    assert len(db.items.revisions(item_id)) == before
    assert db.items.get(item_id).manual_overrides == []
