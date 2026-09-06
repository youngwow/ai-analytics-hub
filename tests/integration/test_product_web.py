from __future__ import annotations

from src.product.contracts import AnalysisDraft, EvidenceClaim, PreparedDocument, SignalDraft
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
