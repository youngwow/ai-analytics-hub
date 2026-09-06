from __future__ import annotations

from src.product.contracts import AnalysisDraft, EventRecord, EvidenceClaim, SignalDraft
from src.product.npa import NpaResolution
from src.product.release import build_deliveries, event_object, item_decision, npa_object


def signal(signal_id="m1:s1", material_id="m1", *, critical=False):
    return SignalDraft(
        signal_id=signal_id,
        material_id=material_id,
        summary="Существенное событие",
        claims=(EvidenceClaim("Подтверждён факт", "Подтверждён факт"),),
        relevance="relevant",
        importance="critical" if critical else "high",
        interest="BOTH",
        impact="Влияет на продукт",
        urgency="urgent" if critical else "routine",
        confidence=0.9,
        unknowns=("Применимость к GS Labs не подтверждена",),
        research_questions=("Используется ли компонент в продуктах?",),
        recipient_roles=("PR", "GR", "HEAD"),
    )


def test_release_projection_keeps_evidence_and_routes_critical():
    current = signal(critical=True)
    draft = AnalysisDraft("m1", "ok", (current,), "cfg", "model", "ctx", 1)
    decision = item_decision("m1", draft)
    obj = event_object(
        EventRecord("e1", "Событие", "Событие", (current.signal_id,), ("m1",), "x"),
        {current.signal_id: current},
    )

    assert decision["critical"] is True
    assert obj is not None
    assert obj["type"] == "event"
    assert obj["claims"][0]["evidence"][0]["quote"] == "Подтверждён факт"
    assert obj["unknowns"] == [
        {"source_item_id": "m1", "text": "Применимость к GS Labs не подтверждена"}
    ]
    assert obj["research_questions"] == ["Используется ли компонент в продуктах?"]
    deliveries = build_deliveries([obj])
    assert {row["delivery_type"] for row in deliveries} == {"urgent_alert"}
    assert {row["recipient"] for row in deliveries} == {"PR", "GR", "HEAD"}


def test_critical_signal_is_always_visible_in_shared_critical_core():
    current = SignalDraft(
        signal_id="m1:s1",
        material_id="m1",
        summary="Критичное событие",
        claims=(EvidenceClaim("Факт", "Факт"),),
        relevance="relevant",
        importance="critical",
        interest="GR",
        impact="Нужна срочная проверка",
        urgency="urgent",
        confidence=0.8,
        recipient_roles=("GR",),
    )

    assert current.roles == ["PR", "GR", "HEAD"]


def test_irrelevant_empty_analysis_is_not_flagged_for_review():
    draft = AnalysisDraft("m1", "irrelevant", (), "cfg", "model", "ctx", 1)
    assert item_decision("m1", draft) == {
        "id": "m1",
        "relevance": "irrelevant",
        "importance": "low",
        "critical": False,
        "roles": [],
        "risk_flag": False,
        "reason": "",
    }


def test_npa_projection_keeps_prior_and_current_history():
    current = signal()
    resolution = NpaResolution(
        object_id="n1",
        member_signal_ids=(current.signal_id,),
        external_id="RU-1",
        current_stage="adopted",
        current_version="v2",
        change_summary="changed",
        effective_from="2026-12-01",
        prior_history_ids=("old-v1",),
    )

    obj = npa_object(resolution, {current.signal_id: current})

    assert obj is not None
    assert obj["npa_state"]["history_ids"] == ["old-v1", "m1"]


def test_release_can_keep_borderline_object_outside_delivery():
    current = signal()
    obj = event_object(
        EventRecord("e1", "Событие", "Событие", (current.signal_id,), ("m1",), "x"),
        {current.signal_id: current},
    )

    assert obj is not None
    assert build_deliveries([obj], allowed_object_ids=set()) == []


def test_scheduled_release_places_routine_npa_in_digest():
    current = signal()
    resolution = NpaResolution(
        object_id="n1",
        member_signal_ids=(current.signal_id,),
        external_id="RU-1",
        current_stage="adopted",
        current_version="v2",
        change_summary="changed",
        effective_from=None,
    )
    obj = npa_object(resolution, {current.signal_id: current})

    assert obj is not None
    deliveries = build_deliveries([obj], scheduled_release=True)
    assert {row["delivery_type"] for row in deliveries} == {"planned_digest"}
