from __future__ import annotations

from support import FakeLLM

from src.product.analysis import PrimaryAnalyzer
from src.product.contracts import ContextBlock, GsLabsContext, LinkDecision, PreparedDocument
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


def test_runtime_persists_event_before_link_to_initial_state(db):
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
                {"object_id": "known-1", "title": "Известный пилот", "status": "active"}
            ]
        },
    )

    assert db.conn.execute("SELECT COUNT(*) FROM product_events").fetchone()[0] == 1
    assert db.conn.execute("SELECT event_id FROM event_links").fetchone()[0] == "known-1"
