"""Флаги, которые наконец срабатывают (план 4.4).

`needs_review` считался в S6 и никуда не записывался: в базе он был нулём всегда,
а UI показывал признак, который не загорается. `evidence_start/end` пустовали по
той же причине. Теперь пограничная релевантность и низкая уверенность поднимают
флаг, а координаты сущности указывают на настоящее место в нормализованном тексте
— или честно остаются пустыми, если модель пересказала своими словами.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from support import ARTICLE_TEXT, NEWS_TEXT, FakeLLM, news_answer, npa_answer

from src.config import Config
from src.models import RawDocument, Source
from src.models.queries import FeedQuery
from src.processing.pipeline import TRUNCATED_NOTE, Draft, Pipeline
from src.repositories import Database
from src.services.processing_service import ProcessingService

NOW = "2026-09-02T12:00:00+00:00"
ENTITIES = npa_answer()["entities"]


def _pipeline(config: Config, answer: dict | None = None, **processing) -> Pipeline:
    processing_config = replace(config.processing, **processing) if processing else config.processing
    provider = FakeLLM(answer) if answer is not None else None
    return Pipeline(processing_config, config.llm, provider)


def _draft(config: Config, **answer) -> Draft:
    return _pipeline(config, news_answer(**answer)).process(NEWS_TEXT, title="Заголовок")


def _document(db: Database, external_id: str = "d1", text: str = ARTICLE_TEXT) -> int:
    source = db.sources.get_by_fetch_url("https://a.ru/rss") or db.sources.add(
        Source(name="Лента", url="https://a.ru/", kind="rss", fetch_url="https://a.ru/rss")
    )
    with db.transaction():
        return db.documents.insert(
            RawDocument(
                source_id=source.id,
                external_id=external_id,
                url=f"https://a.ru/{external_id}",
                title="Минцифры внесло законопроект",
                text=text,
                published_at=NOW,
                fetched_at=NOW,
            )
        )


# ── needs_review загорается ────────────────────────────────────────────────


def test_a_confident_and_clearly_relevant_answer_needs_no_review(config):
    draft = _draft(config)  # relevance 0.62, confidence 0.7

    assert draft.needs_review is False
    assert draft.priority == "medium"
    assert draft.reasoning == "Конкурентная активность на рынке платного ТВ."


def test_borderline_relevance_raises_the_flag_and_the_priority(config):
    draft = _draft(config, relevance_score=0.4, confidence=0.9, priority="medium")

    assert draft.needs_review is True
    assert draft.priority == "high"
    assert "Приоритет повышен: пограничная релевантность." in draft.reasoning


def test_low_confidence_raises_the_flag_and_the_priority(config):
    draft = _draft(config, relevance_score=0.8, confidence=0.4, priority="medium")

    assert draft.needs_review is True
    assert draft.priority == "high"
    assert "Приоритет повышен: низкая уверенность модели." in draft.reasoning


def test_the_flag_fires_even_when_the_priority_cannot_be_raised(config):
    """Раньше признак терялся ровно там, где он важнее всего — на `high`."""
    draft = _draft(config, relevance_score=0.4, confidence=0.9, priority="high")

    assert draft.needs_review is True
    assert draft.priority == "high"
    assert "Приоритет повышен" not in draft.reasoning


def test_a_low_priority_is_raised_one_step_not_to_the_top(config):
    draft = _draft(config, relevance_score=0.4, confidence=0.9, priority="low")

    assert (draft.priority, draft.needs_review) == ("medium", True)


@pytest.mark.parametrize(
    ("relevance", "flagged"),
    [(0.34, False), (0.35, True), (0.42, True), (0.5, True), (0.51, False)],
    ids=["below", "lower-bound", "inside", "upper-bound", "above"],
)
def test_the_borderline_window_is_inclusive_on_both_ends(config, relevance, flagged):
    draft = _draft(config, relevance_score=relevance, confidence=0.9)

    assert draft.needs_review is flagged


@pytest.mark.parametrize(
    ("confidence", "flagged"),
    [(0.0, True), (0.49, True), (0.5, False), (0.9, False)],
    ids=["none", "just-below", "exactly-half", "confident"],
)
def test_confidence_below_a_half_is_what_unsure_means(config, confidence, flagged):
    draft = _draft(config, relevance_score=0.8, confidence=confidence)

    assert draft.needs_review is flagged


def test_a_truncated_document_can_flip_the_flag_through_the_confidence_penalty(config):
    """Обрезка снижает уверенность на 30% — и 0.7 честно становится «не уверен»."""
    long_text = NEWS_TEXT + " Хвост. " * 200

    draft = _pipeline(config, news_answer(relevance_score=0.8, confidence=0.7),
                      max_chars=200, chunk_chars=200).process(long_text)

    assert draft.truncated is True
    assert draft.confidence == pytest.approx(0.49)
    assert draft.needs_review is True
    assert TRUNCATED_NOTE in draft.reasoning


def test_the_extractive_fallback_is_degraded_but_not_flagged_for_review(config):
    """`degraded` и `needs_review` — про разное: карточка без модели не «подозрительна»."""
    draft = _pipeline(config).process(NEWS_TEXT, title="Заголовок")

    assert (draft.degraded, draft.needs_review) == (True, False)
    assert draft.reasoning.startswith("Карточка собрана без модели")


# ── флаг доезжает до карточки и до ленты ───────────────────────────────────


def test_the_flag_reaches_the_stored_card(config, db, frozen_clock):
    document_id = _document(db)
    provider = FakeLLM(news_answer(relevance_score=0.4, confidence=0.9))

    report = ProcessingService(config, db, provider=provider, embedder=None).run()

    item = db.items.get(db.items.item_for_document(document_id))
    assert item.needs_review is True
    assert report.needs_review == 1
    assert db.processing_runs.latest().needs_review == 1


def test_a_calm_card_is_not_flagged(config, db, frozen_clock):
    document_id = _document(db)

    ProcessingService(config, db, provider=FakeLLM(news_answer()), embedder=None).run()

    assert db.items.get(db.items.item_for_document(document_id)).needs_review is False


def test_the_feed_shows_the_flag(config, file_db, feed, frozen_clock):
    _document(file_db)
    provider = FakeLLM(news_answer(relevance_score=0.4, confidence=0.9))
    ProcessingService(config, file_db, provider=provider, embedder=None).run()

    rows = feed.items(FeedQuery.build())["items"]

    assert [row["flags"]["needs_review"] for row in rows] == [True]
    assert rows[0]["flags"]["degraded"] is False


# ── evidence_start / evidence_end указывают на текст ───────────────────────


@pytest.fixture
def service(config, db) -> ProcessingService:
    return ProcessingService(config, db, provider=None, embedder=None)


def _norm_text(db: Database, document_id: int) -> str:
    """Координаты сущностей считаются по `documents.norm_text` — колонке, не полю."""
    return db.conn.execute(
        "SELECT norm_text FROM documents WHERE id=?", (document_id,)
    ).fetchone()[0]


def _spans(service: ProcessingService, norm_text: str, **draft_fields) -> dict:
    draft = Draft(entities=dict(ENTITIES), **draft_fields)
    return {
        span.role: (span.value, span.evidence_start, span.evidence_end)
        for span in service._entities(draft, norm_text)
    }


def test_a_verbatim_entity_points_at_its_place_in_the_text(service):
    spans = _spans(service, ARTICLE_TEXT)

    value, start, end = spans["who"]
    assert ARTICLE_TEXT[start:end] == value == "Минцифры"
    assert start == ARTICLE_TEXT.index("Минцифры")


def test_every_found_span_really_quotes_the_text(service):
    spans = _spans(service, ARTICLE_TEXT, npa_key="112233-8")

    found = {role: v for role, v in spans.items() if v[1] is not None}
    assert set(found) == {"who", "when", "act_number"}
    for value, start, end in found.values():
        assert ARTICLE_TEXT[start:end] == value


def test_a_paraphrased_entity_keeps_both_offsets_empty(service):
    """Выдуманный диапазон хуже отсутствующего: по нему нельзя проверить карточку."""
    spans = _spans(service, ARTICLE_TEXT)

    assert spans["what"][1:] == (None, None)  # «оборота» в пересказе потерялось
    assert spans["impact"][1:] == (None, None)


def test_without_a_text_no_entity_gets_offsets(service):
    spans = _spans(service, "")

    assert {v[1] for v in spans.values()} == {None}


def test_the_act_number_gets_its_own_span(service):
    spans = _spans(service, ARTICLE_TEXT, npa_key="112233-8")

    value, start, end = spans["act_number"]
    assert (value, ARTICLE_TEXT[start:end]) == ("112233-8", "112233-8")


def test_the_first_occurrence_wins(service):
    text = "Минцифры и снова Минцифры"

    spans = _spans(service, text)

    assert spans["who"][1:] == (0, len("Минцифры"))


def test_only_the_four_narrative_roles_and_the_act_number_are_stored(service):
    draft = Draft(entities={**ENTITIES, "нечто": "лишнее"}, npa_key="112233-8")

    roles = [span.role for span in service._entities(draft, ARTICLE_TEXT)]

    assert roles == ["who", "what", "when", "impact", "act_number"]


def test_an_empty_entity_value_is_not_stored_at_all(service):
    draft = Draft(entities={"who": "Минцифры", "what": None, "when": "", "impact": None})

    assert [span.role for span in service._entities(draft, ARTICLE_TEXT)] == ["who"]


def test_a_stored_card_carries_the_offsets_into_its_normalised_text(config, db, frozen_clock):
    document_id = _document(db)
    provider = FakeLLM(npa_answer())

    ProcessingService(config, db, provider=provider, embedder=None).run()

    item_id = db.items.item_for_document(document_id)
    norm_text = _norm_text(db, document_id)
    found = [e for e in db.items.entities(item_id) if e.evidence_start is not None]
    assert found, "ни одна сущность не нашла себя в тексте"
    for entity in found:
        assert norm_text[entity.evidence_start : entity.evidence_end] == entity.value


def test_reprocessing_recomputes_the_offsets_against_its_own_text(config, db, frozen_clock):
    document_id = _document(db)
    provider = FakeLLM(npa_answer())
    service = ProcessingService(config, db, provider=provider, embedder=None)
    service.run()
    item_id = db.items.item_for_document(document_id)
    before = {(e.role, e.evidence_start, e.evidence_end) for e in db.items.entities(item_id)}

    service.reprocess(item_id)

    after = {(e.role, e.evidence_start, e.evidence_end) for e in db.items.entities(item_id)}
    assert after == before
    norm_text = _norm_text(db, document_id)
    for entity in db.items.entities(item_id):
        if entity.evidence_start is not None:
            assert norm_text[entity.evidence_start : entity.evidence_end] == entity.value
