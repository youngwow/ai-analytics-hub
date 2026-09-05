"""src/processing/service.py — ручное добавление материала (US-12, US-13).

PDF-дайджест с почты, документ из закрытого чата, устная информация с
мероприятия: материал без ссылки — валидный случай, обязателен только заголовок.
"""

from __future__ import annotations

import pytest
from support import FakeLLM, npa_answer

from src.processing.service import ItemError, ProcessingService

NOW = "2026-09-02T12:00:00+00:00"
BILL_URL = "https://sozd.duma.gov.ru/bill/123456-8"
BILL_TEXT = (
    "Законопроект 123456-8 вносит изменения в требования к объектам КИИ. "
    "Правительство внесло документ в Государственную Думу. Вступление в силу с 2027 года."
)


@pytest.fixture
def service(config, db, frozen_clock) -> ProcessingService:
    return ProcessingService(config, db, provider=FakeLLM(npa_answer()), embedder=None)


@pytest.fixture
def offline(config, db, frozen_clock) -> ProcessingService:
    """Провайдера нет — материал всё равно не должен потеряться."""
    return ProcessingService(config, db, provider=None, embedder=None)


def test_a_title_alone_is_enough(offline, db):
    result = offline.add_manual(title="Обсуждение на отраслевом комитете", run_llm=False)

    assert result["item_id"] is not None
    item = db.items.get(result["item_id"])
    assert item.origin == "manual"
    assert item.title == "Обсуждение на отраслевом комитете"


def test_neither_title_nor_url_is_refused(offline):
    with pytest.raises(ItemError) as excinfo:
        offline.add_manual(title="  ", url="", run_llm=False)
    assert excinfo.value.code == "validation_error"


def test_without_the_model_the_card_is_marked_degraded(offline, db):
    result = offline.add_manual(title="Материал", text=BILL_TEXT, run_llm=False)

    item = db.items.get(result["item_id"])
    assert item.degraded is True
    assert item.priority == "medium"
    assert item.summary  # экстрактивный лид, а не пустота


def test_run_llm_false_does_not_call_the_provider(service, db):
    service.add_manual(title="Материал", text=BILL_TEXT, run_llm=False)
    assert service.provider.calls == 0


def test_the_same_url_is_reported_as_a_duplicate(offline, db):
    offline.add_manual(title="Законопроект", url=BILL_URL, text=BILL_TEXT, run_llm=False)

    with pytest.raises(ItemError) as excinfo:
        offline.add_manual(title="Он же", url=BILL_URL, run_llm=False)

    assert excinfo.value.code == "possible_duplicate"
    assert excinfo.value.details["reason"] == "url"
    assert excinfo.value.details["item_id"] is not None


def test_a_near_identical_text_is_reported_as_a_duplicate(offline, db):
    offline.add_manual(title="Законопроект 123456-8", url=BILL_URL, text=BILL_TEXT, run_llm=False)

    with pytest.raises(ItemError) as excinfo:
        offline.add_manual(
            title="Законопроект 123456-8",
            url="https://other.example/bill",
            text=BILL_TEXT + " Дополнение редакции.",
            run_llm=False,
        )

    assert excinfo.value.code == "possible_duplicate"
    assert excinfo.value.details["reason"].startswith("simhash")
    assert excinfo.value.details["similarity"] > 0.8


def test_force_creates_the_card_anyway(offline, db):
    offline.add_manual(title="Законопроект", url=BILL_URL, text=BILL_TEXT, run_llm=False)

    second = offline.add_manual(title="Он же", url=BILL_URL, run_llm=False, force=True)

    assert second["item_id"] is not None
    assert db.items.count() == 2


def test_a_manual_bill_starts_its_lifecycle(offline, db):
    result = offline.add_manual(
        title="Законопроект 123456-8",
        url=BILL_URL,
        text=BILL_TEXT,
        item_type="npa",
        npa_status="внесён",
        published_at=NOW,
        run_llm=False,
    )

    item = db.items.get(result["item_id"])
    assert (item.type, item.npa_status, item.origin) == ("npa", "внесён", "manual")
    assert [e.status for e in db.items.events(item.id)] == ["внесён"]
    assert db.items.events(item.id)[0].created_by == "user"


def test_the_manual_source_is_reused_not_duplicated(offline, db):
    offline.add_manual(title="Первый", run_llm=False)
    offline.add_manual(title="Второй", run_llm=False)

    manual = [s for s in db.sources.list() if s.kind == "manual"]
    assert len(manual) == 1


def test_with_the_model_the_card_is_not_degraded(service, db):
    result = service.add_manual(title="Законопроект", url=BILL_URL, text=BILL_TEXT)

    item = db.items.get(result["item_id"])
    assert item.degraded is False
    assert item.origin == "manual"
    assert service.provider.calls == 1
