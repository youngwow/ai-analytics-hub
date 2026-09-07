from __future__ import annotations

import json

from src.models import RawDocument, Source
from src.product.contracts import (
    AnalysisDraft,
    EventRecord,
    EvidenceClaim,
    PreparedDocument,
    SignalDraft,
)
from src.product.operations import DigestService
from src.product.store import ProductStore
from src.product.web import ApiError, DashboardApplication
from src.storage import Database


def seed_signal(path, signal_id="signal-1"):
    db = Database(str(path))
    try:
        ProductStore(db.conn).save_signal(
            SignalDraft(
                signal_id=signal_id,
                material_id="material-1",
                summary="Изменилось регулирование",
                claims=(EvidenceClaim("Опубликован проект", "Опубликован проект"),),
                relevance="relevant",
                importance="high",
                interest="GR",
                impact="Нужно оценить влияние",
                urgency="routine",
                confidence=0.8,
                recipient_roles=("GR", "HEAD"),
            )
        )
    finally:
        db.close()


def test_dashboard_queue_review_and_digest_flow(tmp_path):
    path = tmp_path / "hub.db"
    seed_signal(path)
    app = DashboardApplication(str(path))

    status, queue = app.dispatch("GET", "/api/queue", {"profile": ["GR"]}, {})
    assert status == 200
    assert queue["items"][0]["signal"]["signal_id"] == "signal-1"

    status, review = app.dispatch(
        "POST",
        "/api/review",
        {},
        {
            "signal_id": "signal-1",
            "revision": 1,
            "decision": "include",
            "actor": "denis",
        },
    )
    assert status == 201 and review["decision_id"] > 0

    status, digest = app.dispatch(
        "POST",
        "/api/digests",
        {},
        {
            "id": "weekly-1",
            "period_from": "2026-09-01",
            "period_to": "2026-09-07",
            "signal_ids": ["signal-1"],
            "actor": "denis",
            "recipients": [{"channel": "telegram", "recipient": "head"}],
        },
    )
    assert status == 201
    assert digest["items"][0]["section"] == "gr"

    status, approved = app.dispatch(
        "POST", "/api/digests/weekly-1/1/approve", {}, {"actor": "denis"}
    )
    assert status == 200 and approved["id"] == "weekly-1"


def test_npa_api_separates_legal_stage_from_digest_workflow(tmp_path):
    path = tmp_path / "hub.db"
    db = Database(str(path))
    try:
        store = ProductStore(db.conn)
        store.save_signal(
            SignalDraft(
                signal_id="npa-signal",
                material_id="npa-material",
                summary="Опубликован новый акт",
                claims=(EvidenceClaim("Акт опубликован", "Акт опубликован"),),
                relevance="relevant",
                importance="high",
                interest="GR",
                impact="Влияет на GS Labs",
                urgency="routine",
                confidence=0.9,
                kind="npa",
                npa_identifier="№ 42",
                npa_stage="adopted",
            )
        )
        store.save_review("npa-signal", 1, "include", "demo-gr")
        digest = DigestService(store).create_draft(
            "weekly",
            "2026-09-01",
            "2026-09-07",
            ["npa-signal"],
            actor="demo-gr",
        )
        db.conn.execute(
            "UPDATE digests SET status='delivered' WHERE id='weekly' AND version=?",
            (digest["version"],),
        )
        db.conn.execute(
            "INSERT INTO npa_records(id,jurisdiction,official_identifier,official_url,created_at) VALUES(?,?,?,?,?)",
            ("npa-42", "RU", "№ 42", "https://example.test/npa-42", "2026-09-07T00:00:00+00:00"),
        )
        db.conn.execute(
            "INSERT INTO npa_versions(npa_id,version,stage,payload,source_url,created_at) VALUES(?,?,?,?,?,?)",
            (
                "npa-42",
                1,
                "adopted",
                json.dumps({"member_ids": ["npa-material"], "summary": "Акт"}),
                "https://example.test/npa-42",
                "2026-09-07T00:00:00+00:00",
            ),
        )
        db.conn.commit()
    finally:
        db.close()

    status, response = DashboardApplication(str(path)).dispatch("GET", "/api/npa", {}, {})

    assert status == 200
    item = response["items"][0]
    assert item["stage"] == "adopted"
    assert item["workflow_status"] == "delivered"
    assert item["last_digest"] == {
        "id": "weekly",
        "version": 1,
        "status": "delivered",
        "created_at": item["last_digest"]["created_at"],
        "title": "Информационная повестка GS Labs",
    }


def test_dashboard_context_is_immutable_and_overview_is_real(tmp_path):
    path = tmp_path / "hub.db"
    seed_signal(path)
    app = DashboardApplication(str(path))
    context = {
        "version": "context-v1",
        "common": {"text": "GS Labs", "important_examples": [], "unimportant_examples": []},
        "pr": {"text": "PR", "important_examples": [], "unimportant_examples": []},
        "gr": {"text": "GR", "important_examples": [], "unimportant_examples": []},
    }

    status, created = app.dispatch(
        "PUT", "/api/context", {}, {"version": "context-v1", "context": context, "actor": "admin"}
    )
    assert status == 201 and created["version"] == "context-v1"
    status, repeated = app.dispatch(
        "PUT", "/api/context", {}, {"version": "context-v1", "context": context, "actor": "admin"}
    )
    assert status == 201 and repeated["version"] == "context-v1"
    status, loaded = app.dispatch("GET", "/api/context", {}, {})
    assert loaded["context"]["common"]["text"] == "GS Labs"
    status, overview = app.dispatch("GET", "/api/overview", {}, {})
    assert status == 200
    assert overview["signals"] == 1
    assert overview["pending_queue"] == 1


def test_dashboard_rejects_invalid_profile_and_unknown_signal(tmp_path):
    app = DashboardApplication(str(tmp_path / "hub.db"))
    try:
        app.dispatch("GET", "/api/queue", {"profile": ["HEAD"]}, {})
        assert False
    except ApiError as exc:
        assert exc.status == 400
    try:
        app.dispatch("GET", "/api/signals/missing", {}, {})
        assert False
    except ApiError as exc:
        assert exc.status == 404


def test_dashboard_events_default_to_active_and_archive_is_explicit(tmp_path):
    path = tmp_path / "hub.db"
    db = Database(str(path))
    try:
        store = ProductStore(db.conn)
        store.save_event(EventRecord("active", "A", "A", (), (), "A"))
        store.save_event(
            EventRecord(
                "archived",
                "B",
                "B",
                (),
                (),
                "B",
                lifecycle_state="archived",
            )
        )
    finally:
        db.close()

    app = DashboardApplication(str(path))
    _, active = app.dispatch("GET", "/api/events", {}, {})
    _, archived = app.dispatch("GET", "/api/events", {"state": ["archived"]}, {})
    _, all_events = app.dispatch("GET", "/api/events", {"state": ["all"]}, {})
    assert [row["id"] for row in active["items"]] == ["active"]
    assert [row["id"] for row in archived["items"]] == ["archived"]
    assert {row["id"] for row in all_events["items"]} == {"active", "archived"}


def test_dashboard_source_and_event_details_keep_original_links(tmp_path):
    path = tmp_path / "hub.db"
    db = Database(str(path))
    try:
        source = db.sources.add(
            Source(
                name="Test RSS",
                url="https://example.test",
                kind="rss",
                category="media",
                fetch_url="https://example.test/feed.xml",
            )
        )
        document = RawDocument(
            source_id=source.id,
            external_id="story-1",
            url="https://example.test/story-1",
            title="Original story",
            text="Original text",
            published_at="2026-09-06T10:00:00+00:00",
            fetched_at="2026-09-06T10:05:00+00:00",
        )
        document.compute_hash()
        document_id = db.documents.insert(document)
        store = ProductStore(db.conn)
        store.save_prepared(
            "raw-1-rev-1",
            PreparedDocument("raw-1-rev-1", "Original story", "Original text"),
            raw_document_id=document_id,
        )
        store.save_event(
            EventRecord(
                "event-1",
                "One event",
                "One event summary",
                (),
                ("raw-1-rev-1",),
                "One event",
            )
        )
    finally:
        db.close()

    app = DashboardApplication(str(path))
    status, source = app.dispatch("GET", f"/api/sources/{source.id}", {}, {})
    assert status == 200
    assert source["materials"][0]["url"] == "https://example.test/story-1"
    status, event = app.dispatch("GET", "/api/events/event-1", {}, {})
    assert status == 200
    assert event["materials"][0]["source_name"] == "Test RSS"
    assert event["materials"][0]["url"] == "https://example.test/story-1"


def test_dashboard_exposes_failed_analysis(tmp_path):
    path = tmp_path / "hub.db"
    db = Database(str(path))
    try:
        store = ProductStore(db.conn)
        version = store.save_prepared("broken-1", PreparedDocument("broken-1", "Broken", "x"))
        store.save_analysis(
            AnalysisDraft(
                "broken-1",
                "failed",
                (),
                "cfg",
                "glm-5.3-flash:cloud",
                "context-v1",
                0,
                reason="temporary model failure",
            ),
            prepared_version=version,
        )
    finally:
        db.close()

    app = DashboardApplication(str(path))
    status, failures = app.dispatch("GET", "/api/failures", {}, {})
    assert status == 200
    assert failures["items"][0]["material_id"] == "broken-1"
    assert failures["items"][0]["payload"]["reason"] == "temporary model failure"
    _, overview = app.dispatch("GET", "/api/overview", {}, {})
    assert overview["analysis_failures"] == 1


def test_dashboard_shows_only_unresolved_workflow_failures(tmp_path):
    path = tmp_path / "hub.db"
    db = Database(str(path))
    try:
        store = ProductStore(db.conn)
        store.audit(
            "workflow.failed",
            "material",
            "raw-1-rev-1",
            "runtime",
            {"error": "link failed"},
        )
    finally:
        db.close()

    app = DashboardApplication(str(path))
    _, failures = app.dispatch("GET", "/api/failures", {}, {})
    assert failures["items"][0]["failure_type"] == "workflow"
    _, overview = app.dispatch("GET", "/api/overview", {}, {})
    assert overview["workflow_failures"] == 1

    db = Database(str(path))
    try:
        ProductStore(db.conn).audit("workflow.completed", "material", "raw-1-rev-1", "runtime")
    finally:
        db.close()
    _, failures = app.dispatch("GET", "/api/failures", {}, {})
    assert failures["items"] == []


def test_dashboard_exposes_latest_filtered_material_with_reason(tmp_path):
    path = tmp_path / "hub.db"
    db = Database(str(path))
    try:
        store = ProductStore(db.conn)
        version = store.save_prepared(
            "noise-1",
            PreparedDocument(
                "noise-1",
                "Нерелевантный материал",
                "Исходный текст для ручной перепроверки.",
                source_name="Источник",
                source_url="https://example.test/noise-1",
            ),
        )
        store.save_analysis(
            AnalysisDraft(
                "noise-1",
                "irrelevant",
                (),
                "cfg",
                "glm-5.3-flash:cloud",
                "context-v1",
                1,
                reason="Нет связи с GS Labs",
            ),
            prepared_version=version,
        )
    finally:
        db.close()

    app = DashboardApplication(str(path))
    status, filtered = app.dispatch("GET", "/api/filtered", {}, {})
    assert status == 200
    assert filtered["items"][0]["material_id"] == "noise-1"
    assert filtered["items"][0]["payload"]["reason"] == "Нет связи с GS Labs"
    assert filtered["items"][0]["prepared_payload"]["text"].startswith("Исходный текст")
    _, overview = app.dispatch("GET", "/api/overview", {}, {})
    assert overview["filtered_materials"] == 1
