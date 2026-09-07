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
        "m1:s1",
        "m1",
        "Сигнал",
        (EvidenceClaim("Факт", "Факт"),),
        "relevant",
        "high",
        "GR",
        "Влияние",
        "routine",
        0.9,
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


def test_later_signal_revision_inherits_analysis_provenance(db):
    store = ProductStore(db.conn)
    prepared_version = store.save_prepared("m1", PreparedDocument("m1", "", "Факт"))
    draft = AnalysisDraft(
        "m1", "ok", (signal(),), "A1:one_pass", "glm-5.3-flash:cloud", "ctx-v1", 1
    )
    run_id = store.save_analysis(draft, prepared_version=prepared_version)

    assert store.save_signal(signal(), actor="ai_research", reason="refined") == 2

    revisions = db.conn.execute(
        """SELECT revision,analysis_run_id FROM signal_revisions
           WHERE signal_id=? ORDER BY revision""",
        ("m1:s1",),
    ).fetchall()
    assert [(row["revision"], row["analysis_run_id"]) for row in revisions] == [
        (1, run_id),
        (2, run_id),
    ]


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


def test_old_news_event_is_logically_archived_and_kept_for_retrieval(db):
    store = ProductStore(db.conn)
    old = "2026-01-01T09:00:00+00:00"
    event = EventRecord(
        "e-old",
        "Событие",
        "История",
        (),
        ("m1",),
        "compact",
        (0.1, 0.2),
        first_seen_at=old,
        last_seen_at=old,
        last_meaningful_update_at=old,
    )
    store.save_event(event)

    result = store.archive_due_events(as_of="2026-03-01T09:00:00+00:00")

    assert result["archived"] == ["e-old"]
    archived = store.load_events()[0]
    assert archived.lifecycle_state == "archived"
    assert archived.embedding == (0.1, 0.2)
    assert archived.version == 2
    assert store.archive_due_events(as_of="2026-04-01T09:00:00+00:00")["archived"] == []


def test_open_review_blocks_news_archive_until_operator_decides(db):
    store = ProductStore(db.conn)
    store.save_signal(signal())
    old = "2026-01-01T09:00:00+00:00"
    store.save_event(
        EventRecord(
            "e-review",
            "Событие",
            "История",
            ("m1:s1",),
            ("m1",),
            "compact",
            first_seen_at=old,
            last_seen_at=old,
            last_meaningful_update_at=old,
        )
    )

    blocked = store.archive_due_events(as_of="2026-03-01T09:00:00+00:00")
    assert blocked["blocked"] == {"e-review": "open_review"}

    store.save_review("m1:s1", 1, "include", "pr-user")
    archived = store.archive_due_events(as_of="2026-03-01T09:00:00+00:00")
    assert archived["archived"] == ["e-review"]


def test_low_background_signal_does_not_block_event_archive(db):
    store = ProductStore(db.conn)
    store.save_signal(
        SignalDraft(
            "low:s1", "m-low", "Фоновый сигнал", (EvidenceClaim("Факт", "Факт"),),
            "relevant", "low", "PR", "Фон", "routine", 0.9,
        )
    )
    old = "2026-01-01T09:00:00+00:00"
    store.save_event(
        EventRecord(
            "e-low", "Фон", "Фон", ("low:s1",), ("m-low",), "Фон",
            first_seen_at=old, last_seen_at=old, last_meaningful_update_at=old,
        )
    )

    assert store.archive_due_events(as_of="2026-03-01T09:00:00+00:00")["archived"] == ["e-low"]


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


def test_npa_source_can_be_repaired_without_creating_fake_version(db):
    store = ProductStore(db.conn)
    resolution = NpaResolution(
        object_id="NEW:RU-42",
        member_signal_ids=("m1:s1",),
        external_id="RU-42",
        current_stage="adopted",
        current_version="v1",
        change_summary="published",
        effective_from=None,
    )
    payload = {"stage": "adopted", "version": "v1"}

    assert store.save_npa_resolution(resolution, payload) == ("NEW:RU-42", 1)
    assert store.save_npa_resolution(
        resolution,
        payload,
        source_url="https://publication.pravo.gov.ru/document/RU-42",
    ) == ("NEW:RU-42", 1)

    record = db.conn.execute(
        "SELECT official_url FROM npa_records WHERE id=?",
        ("NEW:RU-42",),
    ).fetchone()
    assert record["official_url"] == "https://publication.pravo.gov.ru/document/RU-42"
    assert db.conn.execute("SELECT COUNT(*) FROM npa_versions").fetchone()[0] == 1


def test_npa_archives_only_after_effective_date_and_preserves_history(db):
    store = ProductStore(db.conn)
    for stage, version, effective_from in (
        ("public_discussion", "v1", None),
        ("revised_draft", "v2", None),
        ("adopted", "v3", "2026-12-01"),
    ):
        resolution = NpaResolution(
            object_id="NPA-LIFECYCLE",
            member_signal_ids=("m1:s1",),
            external_id="RU-LIFECYCLE-1",
            current_stage=stage,
            current_version=version,
            change_summary=f"stage={stage}",
            effective_from=effective_from,
        )
        store.save_npa_resolution(resolution, {"stage": stage, "version": version})

    before = store.npa_archive_status("NPA-LIFECYCLE", as_of="2026-11-30")
    assert before["eligible"] is False
    assert before["reason"] == "awaiting_effective_date"
    assert store.archive_npa("NPA-LIFECYCLE", as_of="2026-11-30", actor="gr") is False

    on_date = store.npa_archive_status("NPA-LIFECYCLE", as_of="2026-12-01")
    assert on_date["eligible"] is True
    assert on_date["reason"] == "effective_date_reached"
    assert store.archive_npa("NPA-LIFECYCLE", as_of="2026-12-01", actor="gr") is True
    assert store.archive_npa("NPA-LIFECYCLE", as_of="2026-12-01", actor="gr") is False

    record = db.conn.execute("SELECT tracked FROM npa_records WHERE id='NPA-LIFECYCLE'").fetchone()
    assert record["tracked"] == 0
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM npa_versions WHERE npa_id='NPA-LIFECYCLE'"
        ).fetchone()[0]
        == 3
    )
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type='npa.archived'"
        ).fetchone()[0]
        == 1
    )
