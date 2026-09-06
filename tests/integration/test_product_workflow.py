from __future__ import annotations

from support import FakeLLM

from src.models import RawDocument, Source
from src.product.analysis import PrimaryAnalyzer
from src.product.contracts import ContextBlock, GsLabsContext, PreparedDocument
from src.product.events import EventLinker
from src.product.store import ProductStore
from src.product.workflow import BranchConfiguration, ProductWorkflow


def context():
    return GsLabsContext("ctx-v1", ContextBlock("GS Labs"), ContextBlock("PR"), ContextBlock("GR"))


def analysis_answer(kind="news", identifier=None):
    return {
        "status": "ok",
        "reason": "",
        "signals": [{
            "kind": kind,
            "summary": "Опубликован проект №123.",
            "claims": [{"text": "Проект опубликован.", "evidence_quote": "Опубликован проект №123."}],
            "relevance": "relevant", "importance": "high", "interest": "GR",
            "impact": "Возможны новые требования.", "urgency": "routine", "confidence": 0.9,
            "unknowns": [], "research_questions": [], "npa_identifier": identifier,
            "npa_stage": "introduced", "reasoning": "Регуляторное изменение.",
        }],
    }


def test_vertical_news_run_creates_event_queue_and_trace(db):
    fake = FakeLLM(analysis_answer())
    workflow = ProductWorkflow(
        PrimaryAnalyzer(fake, model="glm-5.3-flash:cloud"),
        EventLinker(fake, fake, model="glm-5.3-flash:cloud"),
        ProductStore(db.conn),
    )
    result = workflow.run(
        PreparedDocument("m1", "Проект", "Опубликован проект №123."),
        context(), [], BranchConfiguration(),
    )
    assert result.queue_signal_ids == ("m1:s1",)
    assert db.conn.execute("SELECT COUNT(*) FROM product_events").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] >= 3


def test_vertical_keeps_raw_document_trace(db):
    source = db.sources.add(
        Source(
            name="feed",
            url="https://example.ru/rss",
            fetch_url="https://example.ru/rss",
            kind="rss",
        )
    )
    raw_id = db.documents.insert(
        RawDocument(
            source_id=source.id or 0,
            external_id="x1",
            url="https://example.ru/x1",
            title="Project",
            text="Опубликован проект №123.",
        )
    )
    db.conn.commit()
    fake = FakeLLM(analysis_answer())
    workflow = ProductWorkflow(
        PrimaryAnalyzer(fake, model="glm-5.3-flash:cloud"),
        EventLinker(fake, fake, model="glm-5.3-flash:cloud"),
        ProductStore(db.conn),
    )
    workflow.run(
        PreparedDocument("raw-1-rev-1", "Project", "Опубликован проект №123."),
        context(),
        [],
        BranchConfiguration(),
        raw_document_id=raw_id,
    )
    assert db.conn.execute("SELECT raw_document_id FROM prepared_documents").fetchone()[0] == raw_id


def test_npa_with_identifier_uses_npa_lifecycle_not_event_linking(db):
    fake = FakeLLM(analysis_answer("npa", "123"))
    workflow = ProductWorkflow(
        PrimaryAnalyzer(fake, model="glm-5.3-flash:cloud"),
        EventLinker(fake, None, model="glm-5.3-flash:cloud"),
        ProductStore(db.conn),
    )
    result = workflow.run(
        PreparedDocument(
            "m1", "Проект", "Опубликован проект №123.", source_class="npa"
        ),
        context(), [], BranchConfiguration(),
    )
    assert result.npa_signal_ids == ("m1:s1",)
    assert db.conn.execute("SELECT COUNT(*) FROM npa_records").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM npa_versions").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM product_events").fetchone()[0] == 0


def test_nonofficial_npa_claim_stays_a_candidate(db):
    fake = FakeLLM(analysis_answer("npa", "123"))
    workflow = ProductWorkflow(
        PrimaryAnalyzer(fake, model="glm-5.3-flash:cloud"),
        EventLinker(fake, None, model="glm-5.3-flash:cloud"),
        ProductStore(db.conn),
    )
    workflow.run(
        PreparedDocument("m1", "Пересказ", "Опубликован проект №123.", source_class="ordinary"),
        context(),
        [],
        BranchConfiguration(),
    )
    assert db.conn.execute("SELECT COUNT(*) FROM npa_records").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM npa_candidates").fetchone()[0] == 1


def test_npa_without_identifier_is_visible_candidate(db):
    fake = FakeLLM(analysis_answer("npa_candidate"))
    workflow = ProductWorkflow(
        PrimaryAnalyzer(fake, model="glm-5.3-flash:cloud"),
        EventLinker(fake, None, model="glm-5.3-flash:cloud"),
        ProductStore(db.conn),
    )
    workflow.run(
        PreparedDocument("m1", "Проект", "Опубликован проект №123."),
        context(), [], BranchConfiguration(),
    )
    assert db.conn.execute("SELECT status FROM npa_candidates").fetchone()[0] == "unresolved"
