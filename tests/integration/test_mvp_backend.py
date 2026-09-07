from __future__ import annotations

import json
from datetime import UTC, datetime

from MVP.backend.app import PilotApplication
from MVP.backend.service import PilotService
from src.models import FetchResult, RawDocument, Source
from src.product.contracts import AnalysisDraft, EvidenceClaim, PreparedDocument, SignalDraft
from src.product.store import ProductStore
from src.storage import Database


def test_pilot_bootstrap_uses_operational_context_and_explicit_source_policy(tmp_path):
    service = PilotService(tmp_path / "pilot.db")
    status = service.status()
    assert status["selected_architecture"].startswith("A1=one_pass;A2=targeted_research")
    assert set(status["providers_configured"]) == {"glm", "embeddings", "tavily"}
    assert status["retention"] == {
        "news_active_days": 30,
        "archive_mode": "logical",
        "physical_deletion": False,
        "npa_uses_news_window": False,
    }
    assert status["collection_policy"] == {
        "ordinary_seconds": 600,
        "regulator_seconds": 1800,
        "continuation_has_priority": True,
    }

    db = Database(service.db_path)
    try:
        context = db.conn.execute(
            "SELECT version,payload FROM context_versions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert context["version"] == "gs-labs-operational-v1.1.0"
        assert "GS Labs" in json.loads(context["payload"])["common"]["text"]
        official = db.conn.execute(
            "SELECT direction,source_class FROM sources WHERE name LIKE 'НПА: акты Правительства%'"
        ).fetchone()
        assert dict(official) == {"direction": "gr", "source_class": "regulator"}
        secondary = db.conn.execute(
            "SELECT direction,source_class FROM sources WHERE name='Гарант'"
        ).fetchone()
        assert dict(secondary) == {"direction": "gr", "source_class": "ordinary"}
    finally:
        db.close()


def test_pilot_news_retention_is_configurable_and_validated(tmp_path):
    service = PilotService(tmp_path / "pilot.db", news_active_days=45)
    assert service.status()["retention"]["news_active_days"] == 45

    try:
        PilotService(tmp_path / "invalid.db", news_active_days=0)
    except ValueError as exc:
        assert str(exc) == "news_active_days must be >= 1"
    else:
        raise AssertionError("invalid news retention must be rejected")


def test_pilot_status_exposes_real_collection_schedule_by_method(tmp_path):
    service = PilotService(tmp_path / "pilot.db")
    without_watch = service.status()["collection_schedule"]
    assert without_watch
    assert all(item["state"] == "manual" for item in without_watch)
    assert all(item["next_run_at"] is None for item in without_watch)

    service.start_watch(interval_seconds=60)
    try:
        schedule = service.status()["collection_schedule"]
        assert {item["kind"] for item in schedule} >= {"rss", "telegram"}
        assert all(item["state"] == "scheduled" for item in schedule)
        assert all(item["source_count"] >= 1 for item in schedule)
        assert all(item["next_run_at"] for item in schedule)
        assert all(item["intervals_seconds"] for item in schedule)
    finally:
        service.stop_watch()


def test_pilot_bootstrap_repairs_old_seed_metadata_without_reenabling_source(tmp_path):
    service = PilotService(tmp_path / "pilot.db")
    db = Database(service.db_path)
    try:
        row = db.conn.execute(
            "SELECT id FROM sources WHERE name LIKE 'НПА: акты Правительства%'"
        ).fetchone()
        db.conn.execute(
            """UPDATE sources SET direction='both',source_class='ordinary',enabled=0
               WHERE id=?""",
            (row["id"],),
        )
        db.conn.commit()
    finally:
        db.close()

    service.initialize()
    db = Database(service.db_path)
    try:
        repaired = db.conn.execute(
            "SELECT direction,source_class,enabled FROM sources WHERE id=?", (row["id"],)
        ).fetchone()
        assert dict(repaired) == {
            "direction": "gr",
            "source_class": "regulator",
            "enabled": 0,
        }
    finally:
        db.close()


def test_pilot_api_can_revise_signal_and_preserve_revision(tmp_path):
    service = PilotService(tmp_path / "pilot.db")
    db = Database(service.db_path)
    try:
        ProductStore(db.conn).save_signal(
            SignalDraft(
                signal_id="s-1",
                material_id="m-1",
                summary="Черновик",
                claims=(EvidenceClaim("Факт", "Факт"),),
                relevance="relevant",
                importance="medium",
                interest="PR",
                impact="Черновое влияние",
                urgency="routine",
                confidence=0.8,
            )
        )
    finally:
        db.close()

    app = PilotApplication(service)
    status, payload = app.dispatch(
        "POST",
        "/api/signals/s-1/revise",
        {},
        {
            "revision": 1,
            "summary": "Проверенный заголовок",
            "impact": "Уточнённое влияние",
            "kind": "npa_candidate",
            "urgency": "urgent",
            "actor": "pr-user",
        },
    )
    assert status == 201
    assert payload["revision"] == 2
    _, detail = app.dispatch("GET", "/api/signals/s-1", {}, {})
    assert detail["signal"]["summary"] == "Проверенный заголовок"
    assert detail["signal"]["kind"] == "npa_candidate"
    assert detail["signal"]["urgency"] == "urgent"
    assert detail["actor"] == "pr-user"


def test_pilot_api_source_lifecycle_preserves_row(tmp_path):
    service = PilotService(tmp_path / "pilot.db")
    app = PilotApplication(service)
    db = Database(service.db_path)
    try:
        source_id = db.conn.execute("SELECT id FROM sources ORDER BY id LIMIT 1").fetchone()[0]
    finally:
        db.close()

    status, _ = app.dispatch("POST", f"/api/sources/{source_id}/decommission", {}, {})
    assert status == 200
    db = Database(service.db_path)
    try:
        row = db.conn.execute(
            "SELECT enabled,status FROM sources WHERE id=?", (source_id,)
        ).fetchone()
        assert dict(row) == {"enabled": 0, "status": "decommissioned"}
    finally:
        db.close()


def test_manual_material_preserves_provenance_and_queues_analysis(tmp_path, monkeypatch):
    service = PilotService(tmp_path / "pilot.db")
    started = []
    monkeypatch.setattr(
        service,
        "start_job",
        lambda kind, **kwargs: started.append((kind, kwargs)) or {"state": "running"},
    )
    app = PilotApplication(service)

    status, result = app.dispatch(
        "POST",
        "/api/materials/import",
        {},
        {
            "url": "https://example.test/source",
            "title": "Ручной материал",
            "text": "Факт из материала, который нужно проверить для GS Labs.",
            "published_at": "2026-09-07",
        },
    )

    assert status == 201
    assert result["created"] is True
    assert result["analysis"] == "started"
    assert started == [
        (
            "process",
            {"limit": 1, "preferred_document_id": result["document_id"]},
        )
    ]
    db = Database(service.db_path)
    try:
        row = db.conn.execute(
            "SELECT d.url,d.title,d.text,s.kind FROM documents d "
            "JOIN sources s ON s.id=d.source_id WHERE d.id=?",
            (result["document_id"],),
        ).fetchone()
        assert dict(row) == {
            "url": "https://example.test/source",
            "title": "Ручной материал",
            "text": "Факт из материала, который нужно проверить для GS Labs.",
            "kind": "manual",
        }
    finally:
        db.close()


def test_cycle_processes_only_documents_from_current_collection(tmp_path, monkeypatch):
    service = PilotService(tmp_path / "pilot.db")
    processed = []
    monkeypatch.setattr(
        service,
        "collect",
        lambda **_kwargs: {"started_at": "2026-09-07T10:00:00+00:00", "docs_new": 2},
    )
    monkeypatch.setattr(service, "_documents_fetched_since", lambda *_args, **_kwargs: [9, 7])
    monkeypatch.setattr(
        service,
        "process",
        lambda **kwargs: processed.append(kwargs) or {"processed": 2},
    )
    monkeypatch.setattr(service, "maintain_events", lambda **_kwargs: {"checked": 0})

    service._execute_job("cycle", limit=30, backfill=False)

    assert processed == [
        {
            "limit": 2,
            "preferred_document_id": None,
            "preferred_document_ids": [9, 7],
        }
    ]
    assert service.status()["job"]["state"] == "completed"


def test_cycle_does_not_chew_old_backlog_when_collection_is_empty(tmp_path, monkeypatch):
    service = PilotService(tmp_path / "pilot.db")
    monkeypatch.setattr(
        service,
        "collect",
        lambda **_kwargs: {"started_at": "2026-09-07T10:00:00+00:00", "docs_new": 0},
    )
    monkeypatch.setattr(service, "_documents_fetched_since", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        service,
        "process",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("old backlog was processed")),
    )
    monkeypatch.setattr(service, "maintain_events", lambda **_kwargs: {"checked": 0})

    service._execute_job("cycle", limit=30, backfill=False)

    job = service.status()["job"]
    assert job["state"] == "completed"
    assert job["result"]["processing"]["processed"] == 0


def test_manual_material_rejects_missing_provenance(tmp_path):
    app = PilotApplication(PilotService(tmp_path / "pilot.db"))
    try:
        app.dispatch(
            "POST",
            "/api/materials/import",
            {},
            {"text": "Текст без ссылки"},
        )
    except Exception as exc:
        assert getattr(exc, "status", None) == 400
        assert "ссылку" in str(exc)
    else:
        raise AssertionError("manual material without provenance must be rejected")

def test_shared_digest_is_delivered_only_after_pr_and_gr_are_ready(tmp_path, monkeypatch):
    service = PilotService(tmp_path / "pilot.db")
    db = Database(service.db_path)
    try:
        store = ProductStore(db.conn)
        store.save_signal(
            SignalDraft(
                signal_id="shared-signal",
                material_id="shared-material",
                summary="Подтверждённый сигнал",
                claims=(EvidenceClaim("Проверяемый факт", "Проверяемый факт"),),
                relevance="relevant",
                importance="high",
                interest="PR",
                impact="Влияет на повестку",
                urgency="routine",
                confidence=0.95,
            )
        )
        store.save_review("shared-signal", 1, "include", "pr-user")
    finally:
        db.close()

    app = PilotApplication(service)
    status, draft = app.dispatch(
        "POST",
        "/api/digests",
        {},
        {
            "id": "shared-weekly",
            "period_from": "2026-09-01",
            "period_to": "2026-09-07",
            "signal_ids": ["shared-signal"],
            "recipients": [{"channel": "preview", "recipient": "demo-feed"}],
            "actor": "pr-user",
        },
    )
    assert status == 201

    monkeypatch.setattr(
        service,
        "compose_digest_block",
        lambda role, items: {"text": "Готовый PR-блок\n[Источник](https://example.test)", "model": "fake"},
    )
    status, generated = app.dispatch(
        "POST",
        f"/api/digests/shared-weekly/{draft['version']}/generate-role",
        {},
        {"role": "PR", "signal_ids": ["shared-signal"], "actor": "demo-pr"},
    )
    assert status == 200
    assert "[Источник]" in generated["digest"]["role_content"]["PR"]["text"]
    status, _ = app.dispatch(
        "POST",
        f"/api/digests/shared-weekly/{draft['version']}/role-content",
        {},
        {
            "role": "GR",
            "signal_ids": [],
            "text": "Значимых GR-событий нет",
            "actor": "demo-gr",
        },
    )
    assert status == 200

    status, first_ready = app.dispatch(
        "POST",
        f"/api/digests/shared-weekly/{draft['version']}/ready",
        {},
        {"role": "PR", "actor": "pr-user"},
    )
    assert status == 200
    assert first_ready["status"] == "draft"
    assert first_ready["all_ready"] is False

    status, both_ready = app.dispatch(
        "POST",
        f"/api/digests/shared-weekly/{draft['version']}/ready",
        {},
        {"role": "GR", "actor": "gr-user"},
    )
    assert status == 200
    assert both_ready["status"] == "delivered"
    assert both_ready["all_ready"] is True
    assert both_ready["delivery"][0]["status"] == "delivered"

    db = Database(service.db_path)
    try:
        stored = db.conn.execute(
            "SELECT status,payload FROM digests WHERE id='shared-weekly' AND version=?",
            (draft["version"],),
        ).fetchone()
        payload = json.loads(stored["payload"])
        assert stored["status"] == "delivered"
        assert payload["readiness"]["PR"]["actor"] == "pr-user"
        assert payload["readiness"]["GR"]["actor"] == "gr-user"
    finally:
        db.close()

    status, reset = app.dispatch(
        "POST",
        "/api/demo/reset-digest",
        {},
        {"actor": "demo-reset"},
    )
    assert status == 200
    assert reset == {"status": "reset", "signals_returned": 1}

    db = Database(service.db_path)
    try:
        assert db.conn.execute(
            "SELECT status FROM digests WHERE id='shared-weekly' AND version=?",
            (draft["version"],),
        ).fetchone()["status"] == "delivered"
        assert db.conn.execute(
            "SELECT status FROM digests WHERE id='shared-weekly' ORDER BY version DESC LIMIT 1"
        ).fetchone()["status"] == "reset"
    finally:
        db.close()

def test_source_preview_reads_examples_without_persisting_candidate(tmp_path, monkeypatch):
    service = PilotService(tmp_path / "pilot.db")
    db = Database(service.db_path)
    try:
        before = db.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    finally:
        db.close()

    def fake_preview(_collector, source, *, limit=3):
        assert source.kind == "rss"
        assert limit == 3
        return (
            FetchResult(
                documents=[
                    RawDocument(
                        source_id=0,
                        external_id="preview-1",
                        url="https://example.test/item",
                        title="Preview item",
                        summary="Preview summary",
                        published_at="2026-09-06T00:00:00+00:00",
                    )
                ]
            ),
            42,
        )

    monkeypatch.setattr("MVP.backend.service.Collector.preview_source", fake_preview)
    app = PilotApplication(service)
    status, payload = app.dispatch(
        "POST",
        "/api/sources/preview",
        {},
        {
            "url": "https://example.test",
            "kind": "rss",
            "fetch_url": "https://example.test/feed.xml",
            "category": "media",
            "direction": "both",
        },
    )
    assert status == 200
    assert payload["can_confirm"] is True
    assert payload["latency_ms"] == 42
    assert payload["examples"][0]["title"] == "Preview item"

    db = Database(service.db_path)
    try:
        after = db.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    finally:
        db.close()
    assert after == before


def test_failed_workflow_remains_in_pilot_backlog(tmp_path):
    service = PilotService(tmp_path / "pilot.db")
    db = Database(service.db_path)
    try:
        source_id = db.conn.execute("SELECT id FROM sources ORDER BY id LIMIT 1").fetchone()[0]
        document = RawDocument(
            source_id=source_id,
            external_id="retry-me",
            url="https://example.test/retry-me",
            title="Retry",
            text="Retry",
            fetched_at="2026-09-06T00:00:00+00:00",
        )
        document.compute_hash()
        document_id = db.documents.insert(document)
        db.conn.commit()
        store = ProductStore(db.conn)
        material_id = f"raw-{document_id}-rev-1"
        prepared_version = store.save_prepared(
            material_id,
            PreparedDocument(material_id, "Retry", "Retry"),
            raw_document_id=document_id,
        )
        store.save_analysis(
            AnalysisDraft(
                material_id,
                "irrelevant",
                (),
                "A1=one_pass;A2=targeted_research;A3=embedding_top20;A4=without_critic",
                "model",
                "context",
                1,
            ),
            prepared_version=prepared_version,
        )
        store.audit("workflow.failed", "material", material_id, "test")
        selected, _ = service._select_pending_documents(db, store, limit=1)
        assert selected[0][1] == material_id
    finally:
        db.close()

    assert service.status()["data"]["awaiting_ai"] == 1


def test_pilot_backlog_prioritises_official_regulator_without_starving_sources(tmp_path):
    service = PilotService(tmp_path / "pilot.db")
    db = Database(service.db_path)
    try:
        ordinary = db.sources.ensure_manual()
        regulator = db.sources.add(
            Source(
                name="Official regulator test",
                url="https://publication.pravo.gov.ru/test",
                kind="manual",
                category="regulator",
                fetch_url="manual://regulator-test",
                direction="gr",
                source_class="regulator",
            )
        )
        for source, suffix in ((ordinary, "ordinary"), (regulator, "regulator")):
            document = RawDocument(
                source_id=source.id or 0,
                external_id=suffix,
                url=f"https://example.test/{suffix}",
                title=suffix,
                text=suffix,
                fetched_at="2026-09-06T00:00:00+00:00",
            )
            document.compute_hash()
            db.documents.insert(document)
        db.conn.commit()

        selected, _ = service._select_pending_documents(db, ProductStore(db.conn), limit=2)
        assert [row[0]["source_class"] for row in selected] == ["regulator", "ordinary"]
    finally:
        db.close()


def test_live_collection_uses_separate_ordinary_and_regulator_intervals(tmp_path):
    service = PilotService(tmp_path / "pilot.db")
    db = Database(service.db_path)
    try:
        ordinary = db.sources.ensure_manual()
        regulator = next(row for row in db.sources.list() if row.source_class == "regulator")
        now = datetime(2026, 9, 6, 12, 30, tzinfo=UTC)
        for source in (ordinary, regulator):
            db.conn.execute(
                """INSERT INTO fetch_state(source_id,last_fetch_at,backlog_status)
                   VALUES(?,?,?) ON CONFLICT(source_id) DO UPDATE SET
                   last_fetch_at=excluded.last_fetch_at,backlog_status=excluded.backlog_status""",
                (source.id, "2026-09-06T12:15:00+00:00", "clear"),
            )
        db.conn.commit()

        due = service._due_source_ids(db, now=now)
        assert ordinary.id in due
        assert regulator.id not in due

        db.conn.execute(
            "UPDATE fetch_state SET backlog_status='continuation' WHERE source_id=?",
            (regulator.id,),
        )
        db.conn.commit()
        assert regulator.id in service._due_source_ids(db, now=now)
    finally:
        db.close()
