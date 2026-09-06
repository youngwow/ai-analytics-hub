from __future__ import annotations

import json

from src.product.contracts import (
    AnalysisDraft,
    EventRecord,
    EvidenceClaim,
    PreparedDocument,
    SignalDraft,
)
from src.product.npa import NpaResolution
from src.product.store import ProductStore


def signal():
    return SignalDraft(
        "m1:s1", "m1", "Сигнал", (EvidenceClaim("Факт", "Факт"),),
        "relevant", "high", "GR", "Влияние", "routine", 0.9,
    )


def test_analysis_storage_is_append_only_and_auditable(db):
    store = ProductStore(db.conn)
    prepared_version = store.save_prepared("m1", PreparedDocument("m1", "", "Факт"))
    draft = AnalysisDraft(
        "m1", "ok", (signal(),), "A1:one_pass", "glm-5.3-flash:cloud", "ctx-v1", 1
    )
    first = store.save_analysis(draft, prepared_version=prepared_version)
    second = store.save_analysis(draft, prepared_version=prepared_version)
    assert second > first
    revisions = db.conn.execute(
        "SELECT revision,payload FROM signal_revisions WHERE signal_id=? ORDER BY revision",
        ("m1:s1",),
    ).fetchall()
    assert [row["revision"] for row in revisions] == [1, 2]
    assert json.loads(revisions[0]["payload"])["summary"] == "Сигнал"
    assert len(store.history("signal", "m1:s1")) == 2


def test_review_does_not_overwrite_ai_signal(db):
    store = ProductStore(db.conn)
    store.save_signal(signal())
    store.save_review("m1:s1", 1, "exclude", "denis", {"reason": "noise"})
    assert db.conn.execute("SELECT COUNT(*) FROM signal_revisions").fetchone()[0] == 1
    assert db.conn.execute("SELECT decision FROM review_decisions").fetchone()[0] == "exclude"


def test_context_version_is_idempotent_but_not_mutable(db):
    store = ProductStore(db.conn)
    store.ensure_context("ctx-v1", {"common": "GS Labs"})
    store.ensure_context("ctx-v1", {"common": "GS Labs"})
    assert db.conn.execute("SELECT COUNT(*) FROM context_versions").fetchone()[0] == 1
    try:
        store.ensure_context("ctx-v1", {"common": "changed"})
    except ValueError as exc:
        assert "different content" in str(exc)
    else:
        raise AssertionError("context mutation must be rejected")


def test_current_event_projection_round_trips_as_contract(db):
    store = ProductStore(db.conn)
    event = EventRecord("e1", "Title", "Summary", ("s1",), ("m1",), "compact")
    store.save_event(event)
    assert store.load_events() == [event]


def test_unresolved_npa_is_stored_atomically_and_idempotently(db):
    store = ProductStore(db.conn)
    resolution = NpaResolution(
        object_id="NEW:UNKNOWN:abc",
        member_signal_ids=("m1:s1",),
        external_id=None,
        current_stage="unknown",
        current_version="unknown",
        change_summary="unknown",
        effective_from=None,
        needs_human_review=True,
    )
    payload = {"object_id": resolution.object_id, "needs_human_review": True}

    assert store.save_npa_resolution(resolution, payload) == (resolution.object_id, 1)
    assert store.save_npa_resolution(resolution, payload) == (resolution.object_id, 1)

    record = db.conn.execute(
        "SELECT official_identifier FROM npa_records WHERE id=?",
        (resolution.object_id,),
    ).fetchone()
    assert record["official_identifier"].startswith("unresolved:")
    assert db.conn.execute("SELECT COUNT(*) FROM npa_versions").fetchone()[0] == 1
