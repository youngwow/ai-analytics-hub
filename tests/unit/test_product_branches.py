from __future__ import annotations

from support import FakeLLM

from src.processing.llm import LlmTemporaryError
from src.product.contracts import (
    ContextBlock,
    EventRecord,
    EvidenceClaim,
    GsLabsContext,
    PreparedDocument,
    SearchEvidence,
    SignalDraft,
)
from src.product.critic import SignalCritic
from src.product.events import EventLinker
from src.product.research import TargetedResearcher


class FakeSearch:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    def search(self, query, *, mode):
        self.calls.append((query, mode))
        return self.rows


def signal(**overrides):
    values = dict(
        signal_id="m1:s1",
        material_id="m1",
        summary="Опубликован проект закона.",
        claims=(EvidenceClaim("Опубликован проект.", "Опубликован проект закона."),),
        relevance="relevant",
        importance="high",
        interest="GR",
        impact="Может затронуть ПО.",
        urgency="routine",
        confidence=0.7,
        unknowns=("Неясна дата вступления",),
        research_questions=("Когда вступит в силу?",),
    )
    values.update(overrides)
    return SignalDraft(**values)


def context():
    return GsLabsContext("v1", ContextBlock("GS Labs"), ContextBlock("PR"), ContextBlock("GR"))


def test_a2_skips_low_value_signal_without_calling_any_provider():
    llm, search = FakeLLM({}), FakeSearch()
    result = TargetedResearcher(llm, search, "glm-5.3-flash:cloud").research(
        signal(importance="low") , context()
    )
    assert result.status == "not_needed"
    assert llm.calls == 0 and search.calls == []


def test_a2_keeps_only_search_grounded_claims():
    plan = {"needed": True, "mode": "deep", "queries": ["проект закона дата"], "reason": ""}
    synthesis = {
        "confirmed_claims": [
            {"text": "Рассмотрение назначено.", "evidence_quote": "Рассмотрение назначено на 10 сентября."},
            {"text": "Выдумка.", "evidence_quote": "нет такого"},
        ],
        "contradictions": [],
        "unknowns": [],
        "refined_impact": "Есть срок реакции.",
        "confidence": 0.9,
        "stop_reason": "первичный источник найден",
    }
    search = FakeSearch([SearchEvidence("https://example.test", "", "Рассмотрение назначено на 10 сентября.")])
    result = TargetedResearcher(FakeLLM([plan, synthesis]), search, "glm-5.3-flash:cloud").research(signal(), context())
    assert result.status == "complete"
    assert len(result.confirmed_claims) == 1
    assert result.queries == ("проект закона дата",)


def test_a2_bounds_sources_and_total_evidence_sent_to_reasoning_model():
    plan = {"needed": True, "mode": "wide", "queries": ["q"], "reason": ""}
    synthesis = {
        "confirmed_claims": [],
        "contradictions": [],
        "unknowns": [],
        "refined_impact": "impact",
        "confidence": 0.7,
        "stop_reason": "bounded",
    }
    rows = [SearchEvidence(f"https://e/{i}", str(i), "x" * 100) for i in range(10)]
    llm = FakeLLM([plan, synthesis])
    result = TargetedResearcher(
        llm, FakeSearch(rows), "glm-5.3-flash:cloud", max_sources=3, max_evidence_chars=250
    ).research(signal(), context())
    assert len(result.evidence) == 3
    assert sum(len(row.snippet) for row in result.evidence) == 250


def test_a3_embeddings_only_select_candidates_and_glm_decides():
    events = [EventRecord(f"e{i}", f"E{i}", "", (), (), f"event {i}", (1.0, 0.0) if i == 3 else (0.0, 1.0)) for i in range(25)]
    fake = FakeLLM({"event_id": "e3", "relation": "event_update", "confidence": 0.9, "evidence": "same act", "needs_human_review": False}, embedder=lambda texts: [[1.0, 0.0] for _ in texts])
    result = EventLinker(fake, fake, model="glm-5.3-flash:cloud").link(signal(), events, mode="embedding_top20")
    assert result.event_id == "e3"
    assert result.relation == "event_update"
    assert fake.calls == 1
    assert len(fake.embedded) == 1


def test_a3_rejects_event_id_outside_candidates():
    fake = FakeLLM({"event_id": "invented", "relation": "same_event", "confidence": 1, "evidence": "", "needs_human_review": False})
    result = EventLinker(fake, None, model="glm-5.3-flash:cloud").link(signal(), [EventRecord("e1", "", "", (), (), "x")], mode="full_scan")
    assert result.event_id is None and result.relation == "different"


def test_a3_temporary_failure_is_visible_and_does_not_merge_events():
    class FailingProvider:
        def complete(self, prompt, schema, *, system=""):
            raise LlmTemporaryError("timeout")

    result = EventLinker(
        FailingProvider(), None, model="glm-5.3-flash:cloud"
    ).link(signal(), [EventRecord("e1", "", "", (), (), "x")], mode="full_scan")
    assert result.event_id is None
    assert result.relation == "different"
    assert result.needs_human_review is True
    assert result.confidence == 0.0


def test_a4_preserves_original_when_corrected_evidence_is_not_grounded():
    fake = FakeLLM({"issues": ["bad evidence"], "corrections": {"claims": [{"text": "x", "evidence_quote": "invented"}]}, "needs_human_review": False})
    original = signal()
    result = SignalCritic(fake, model="glm-5.3-flash:cloud").review(
        PreparedDocument("m1", "", "Опубликован проект закона."), original
    )
    assert result.corrected_signal.claims == original.claims
    assert result.needs_human_review is True
