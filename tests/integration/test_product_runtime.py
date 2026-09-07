from __future__ import annotations

from support import FakeLLM

from src.product.analysis import PrimaryAnalyzer
from src.product.contracts import (
    ContextBlock,
    EventRecord,
    GsLabsContext,
    LinkDecision,
    PreparedDocument,
)
from src.product.events import EventLinker
from src.product.npa import NpaResolver
from src.product.runtime import ProductAgentRuntime
from src.product.store import ProductStore
from src.product.workflow import BranchConfiguration


def answer():
    return {
        "status": "ok",
        "reason": "",
        "signals": [
            {
                "kind": "news",
                "summary": "Оператор начал крупный пилот.",
                "claims": [
                    {
                        "text": "Оператор начал пилот.",
                        "evidence_quote": "Оператор начал крупный пилот.",
                    }
                ],
                "relevance": "relevant",
                "importance": "high",
                "interest": "PR",
                "recipient_roles": ["PR", "HEAD"],
                "impact": "Сигнал о рыночном спросе.",
                "urgency": "routine",
                "confidence": 0.9,
                "unknowns": [],
                "research_questions": [],
                "reasoning": "Связано с основным рынком.",
            }
        ],
    }


def test_runtime_returns_one_reviewable_draft_digest(db):
    llm = FakeLLM(answer())
    store = ProductStore(db.conn)
    runtime = ProductAgentRuntime(
        PrimaryAnalyzer(llm, model="model"),
        EventLinker(llm, None, model="model"),
        NpaResolver(llm, model="model"),
        store,
    )
    context = GsLabsContext(
        "ctx-v1", ContextBlock("GS Labs"), ContextBlock("PR"), ContextBlock("GR")
    )
    result = runtime.run(
        [PreparedDocument("m1", "Пилот", "Оператор начал крупный пилот.")],
        context,
        BranchConfiguration(),
        scheduled_release=True,
    )

    assert result.telemetry.configuration_id == BranchConfiguration().id
    assert result.telemetry.documents == 1
    assert result.telemetry.signals == 1
    assert len(result.draft_digest.objects) == 1
    assert {row["recipient"] for row in result.draft_digest.deliveries} == {"PR", "HEAD"}
    assert result.draft_digest.deliveries[0]["delivery_type"] == "planned_digest"


def test_runtime_rejects_selected_unwired_strategy(db):
    llm = FakeLLM(answer())
    runtime = ProductAgentRuntime(
        PrimaryAnalyzer(llm, model="model"),
        EventLinker(llm, None, model="model"),
        NpaResolver(llm, model="model"),
        ProductStore(db.conn),
    )
    context = GsLabsContext("ctx-v1", ContextBlock(), ContextBlock(), ContextBlock())

    try:
        runtime.run(
            [PreparedDocument("m1", "x", "x")],
            context,
            BranchConfiguration(a2="targeted_research"),
        )
        assert False
    except ValueError as exc:
        assert "without researcher" in str(exc)


def test_runtime_can_find_and_reactivate_archived_event(db):
    class ExistingEventLinker:
        def link(self, signal, events, *, mode):
            assert events[0].id == "known-1"
            return LinkDecision(
                signal.signal_id,
                "known-1",
                "event_update",
                0.9,
                "same event",
            )

    llm = FakeLLM(answer())
    runtime = ProductAgentRuntime(
        PrimaryAnalyzer(llm, model="model"),
        ExistingEventLinker(),
        NpaResolver(llm, model="model"),
        ProductStore(db.conn),
    )
    context = GsLabsContext(
        "ctx-v1", ContextBlock("GS Labs"), ContextBlock("PR"), ContextBlock("GR")
    )

    runtime.run(
        [PreparedDocument("m1", "Пилот", "Оператор начал крупный пилот.")],
        context,
        BranchConfiguration(),
        initial_state={
            "known_events": [
                {"object_id": "known-1", "title": "Известный пилот", "status": "archived"}
            ]
        },
    )

    assert db.conn.execute("SELECT COUNT(*) FROM product_events").fetchone()[0] == 1
    assert db.conn.execute("SELECT event_id FROM event_links").fetchone()[0] == "known-1"
    event = ProductStore(db.conn).load_events()[0]
    assert event.lifecycle_state == "active"
    assert event.last_meaningful_update_at


def test_runtime_does_not_resave_unchanged_existing_events(db):
    class DifferentEventLinker:
        def link(self, signal, events, *, mode):
            assert events[0].id == "known-1"
            return LinkDecision(signal.signal_id, None, "different", 0.9, "new event")

    store = ProductStore(db.conn)
    store.save_event(
        EventRecord(
            "known-1",
            "Старая история",
            "Старая история",
            ("old-signal",),
            ("old-material",),
            "Старая история",
            first_seen_at="2026-09-01T00:00:00+00:00",
            last_seen_at="2026-09-01T00:00:00+00:00",
            last_meaningful_update_at="2026-09-01T00:00:00+00:00",
        )
    )
    known = store.load_events()[0]
    llm = FakeLLM(answer())
    runtime = ProductAgentRuntime(
        PrimaryAnalyzer(llm, model="model"),
        DifferentEventLinker(),
        NpaResolver(llm, model="model"),
        store,
    )
    context = GsLabsContext(
        "ctx-v1", ContextBlock("GS Labs"), ContextBlock("PR"), ContextBlock("GR")
    )

    runtime.run(
        [PreparedDocument("m-new", "Пилот", "Оператор начал крупный пилот.")],
        context,
        BranchConfiguration(),
        initial_state={
            "known_events": [
                {
                    "object_id": known.id,
                    "title": known.title,
                    "summary": known.summary,
                    "signal_ids": known.signal_ids,
                    "material_ids": known.material_ids,
                    "compact_text": known.compact_text,
                    "version": known.version,
                    "lifecycle_state": known.lifecycle_state,
                    "first_seen_at": known.first_seen_at,
                    "last_seen_at": known.last_seen_at,
                    "last_meaningful_update_at": known.last_meaningful_update_at,
                }
            ]
        },
    )

    events = store.load_events()
    assert len(events) == 2
    assert next(event for event in events if event.id == "known-1").version == 1


def test_runtime_keeps_official_regulator_url_on_npa_record(db):
    analysis = answer()
    analysis["signals"][0].update(
        {
            "kind": "npa",
            "interest": "GR",
            "recipient_roles": ["GR", "HEAD"],
            "npa_identifier": "RU-42",
            "npa_stage": "adopted",
            "npa_version": "v1",
            "npa_effective_from": None,
            "npa_change_summary": "Документ опубликован.",
        }
    )
    resolution = {
        "objects": [
            {
                "object_id": "NEW:RU-42",
                "member_signal_ids": ["m1:s1"],
                "external_id": "RU-42",
                "current_stage": "adopted",
                "current_version": "v1",
                "change_summary": "Документ опубликован.",
                "effective_from": None,
                "stale_signal_ids": [],
                "needs_human_review": False,
            }
        ]
    }
    llm = FakeLLM([analysis, resolution])
    store = ProductStore(db.conn)
    runtime = ProductAgentRuntime(
        PrimaryAnalyzer(llm, model="model"),
        EventLinker(llm, None, model="model"),
        NpaResolver(llm, model="model"),
        store,
    )

    runtime.run(
        [
            PreparedDocument(
                "m1",
                "НПА",
                "Оператор начал крупный пилот.",
                source_name="Официальное опубликование",
                source_url="https://publication.pravo.gov.ru/document/RU-42",
                published_at="2026-09-01T12:00:00+00:00",
                source_class="regulator",
            )
        ],
        GsLabsContext(
            "ctx-v1",
            ContextBlock("GS Labs"),
            ContextBlock("PR"),
            ContextBlock("GR"),
        ),
        BranchConfiguration(),
    )

    record = db.conn.execute(
        "SELECT official_url FROM npa_records WHERE official_identifier=?",
        ("RU-42",),
    ).fetchone()
    assert record["official_url"] == "https://publication.pravo.gov.ru/document/RU-42"
