from __future__ import annotations

import json

from src.product.contracts import EventRecord, EvidenceClaim, LinkDecision, SignalDraft
from src.product.operations import DigestService, MetricsService, WorkQueue
from src.product.store import ProductStore


class Delivery:
    def __init__(self):
        self.calls = []

    def send(self, recipient, payload):
        self.calls.append((recipient, payload["id"]))
        return "ok"


def signal(signal_id="s1", interest="GR", importance="high"):
    return SignalDraft(
        signal_id, "m1", "Сигнал", (EvidenceClaim("Факт", "Факт"),),
        "relevant", importance, interest, "Влияние", "routine", 0.9,
    )


def test_queue_is_shared_but_profile_is_only_a_filter(db):
    store = ProductStore(db.conn)
    store.save_signal(signal("s-gr", "GR"))
    store.save_signal(signal("s-pr", "PR"))
    queue = WorkQueue(store)
    assert {x["signal"]["signal_id"] for x in queue.list()} == {"s-gr", "s-pr"}
    assert [x["signal"]["signal_id"] for x in queue.list(profile="PR")] == ["s-pr"]


def test_active_queue_hides_low_irrelevant_and_repeated_event_signals(db):
    store = ProductStore(db.conn)
    store.save_signal(signal("low", "PR", "low"))
    store.save_signal(
        SignalDraft(
            "irrelevant", "m2", "Шум", (EvidenceClaim("Факт", "Факт"),),
            "irrelevant", "high", "IRRELEVANT", "", "routine", 0.9,
        )
    )
    store.save_signal(signal("older", "PR", "medium"))
    store.save_signal(signal("newer", "PR", "high"))
    store.save_event(EventRecord("event-1", "Событие", "Событие", ("older", "newer"), ("m1",), "Событие"))
    store.save_link(LinkDecision("older", "event-1", "same_event", 0.9, "same"))
    store.save_link(LinkDecision("newer", "event-1", "event_update", 0.9, "update"))

    assert [item["signal"]["signal_id"] for item in WorkQueue(store).list()] == ["newer"]

    store.save_review("low", 1, "include", "pr-user")
    resolved = WorkQueue(store).list(include_resolved=True)
    assert any(item["signal"]["signal_id"] == "low" for item in resolved)


def test_digest_requires_human_include_and_recipient_then_delivery_is_idempotent(db):
    store = ProductStore(db.conn)
    store.save_signal(signal())
    service = DigestService(store)
    try:
        service.create_draft("d1", "2026-09-01", "2026-09-07", ["s1"], actor="denis")
        assert False
    except ValueError as exc:
        assert "not confirmed" in str(exc)
    store.save_review("s1", 1, "include", "denis")
    draft = service.create_draft(
        "d1", "2026-09-01", "2026-09-07", ["s1"], actor="denis",
        recipients=[{"channel": "telegram", "recipient": "head"}],
    )
    service.approve("d1", draft["version"], actor="denis")
    adapter = Delivery()
    first = service.deliver("d1", 1, {"telegram": adapter})
    second = service.deliver("d1", 1, {"telegram": adapter})
    assert first[0]["status"] == "delivered"
    assert second[0]["status"] == "already_delivered"
    assert len(adapter.calls) == 1
    assert MetricsService(store).snapshot()["signals"] == 1


def test_digest_rejects_reversed_period(db):
    store = ProductStore(db.conn)
    store.save_signal(signal())
    store.save_review("s1", 1, "include", "denis")
    try:
        DigestService(store).create_draft(
            "bad-period", "2026-09-08", "2026-09-07", ["s1"], actor="denis"
        )
    except ValueError as exc:
        assert "start must not be after" in str(exc)
    else:
        raise AssertionError("reversed digest period must be rejected")


def test_shared_digest_tracks_pr_and_gr_readiness(db):
    store = ProductStore(db.conn)
    store.save_signal(signal("s1", "PR"))
    store.save_review("s1", 1, "include", "pr-user")
    service = DigestService(store)
    draft = service.create_draft(
        "shared",
        "2026-09-01",
        "2026-09-07",
        ["s1"],
        actor="pr-user",
        recipients=[{"channel": "preview", "recipient": "demo"}],
        title="Weekly",
        header="Intro",
        footer="Links",
    )
    service.update_role_content(
        "shared", draft["version"], role="PR", signal_ids=["s1"],
        text="Готовый PR-блок", actor="pr-user", model="test-model",
    )
    service.update_role_content(
        "shared", draft["version"], role="GR", signal_ids=[],
        text="Значимых GR-событий нет", actor="gr-user", model="test-model",
    )

    pr = service.mark_ready("shared", draft["version"], role="PR", actor="pr-user")
    assert pr["all_ready"] is False
    assert pr["digest"]["readiness"]["PR"]["actor"] == "pr-user"

    gr = service.mark_ready("shared", draft["version"], role="GR", actor="gr-user")
    assert gr["all_ready"] is True
    assert gr["digest"]["readiness"]["GR"]["actor"] == "gr-user"


def test_shared_digest_invalidates_stale_ready_block_before_release(db):
    store = ProductStore(db.conn)
    store.save_signal(signal("s1", "PR"))
    store.save_review("s1", 1, "include", "pr-user")
    service = DigestService(store)
    draft = service.create_draft(
        "stale-shared",
        "2026-09-01",
        "2026-09-07",
        ["s1"],
        actor="pr-user",
        recipients=[{"channel": "preview", "recipient": "demo"}],
    )
    service.update_role_content(
        "stale-shared", draft["version"], role="PR", signal_ids=["s1"],
        text="PR-блок", actor="pr-user", model="test",
    )
    service.update_role_content(
        "stale-shared", draft["version"], role="GR", signal_ids=[],
        text="GR-блок пуст", actor="gr-user", model="test",
    )
    service.mark_ready("stale-shared", draft["version"], role="PR", actor="pr-user")

    store.save_signal(signal("s2", "PR"))
    store.save_review("s2", 1, "include", "pr-user")

    try:
        service.mark_ready("stale-shared", draft["version"], role="GR", actor="gr-user")
    except ValueError as exc:
        assert "regenerate blocks: PR" in str(exc)
    else:
        raise AssertionError("stale PR block must prevent release")
    payload = json.loads(
        store.conn.execute(
            "SELECT payload FROM digests WHERE id=? AND version=?",
            ("stale-shared", draft["version"]),
        ).fetchone()["payload"]
    )
    assert payload["readiness"]["PR"] is None
    assert payload["readiness"]["GR"]["actor"] == "gr-user"


def test_shared_interest_signal_belongs_to_role_that_selected_it(db):
    store = ProductStore(db.conn)
    store.save_signal(signal("shared-interest", "BOTH"))
    store.save_review("shared-interest", 1, "include", "demo-gr")

    draft = DigestService(store).create_draft(
        "role-owned",
        "2026-09-01",
        "2026-09-07",
        ["shared-interest"],
        actor="demo-gr",
    )

    assert draft["items"][0]["section"] == "gr"


def test_demo_reset_returns_selected_signals_without_deleting_history(db):
    store = ProductStore(db.conn)
    store.save_signal(signal("pr-signal", "PR"))
    store.save_signal(signal("gr-signal", "GR"))
    store.save_review("pr-signal", 1, "include", "demo-pr")
    store.save_review("gr-signal", 1, "include", "demo-gr")
    service = DigestService(store)
    draft = service.create_draft(
        "demo-digest",
        "2026-09-01",
        "2026-09-07",
        ["pr-signal", "gr-signal"],
        actor="demo-pr",
    )

    result = service.reset_demo(actor="demo-reset")

    assert result == {"status": "reset", "signals_returned": 2}
    stored = db.conn.execute(
        "SELECT status FROM digests WHERE id=? AND version=?",
        ("demo-digest", draft["version"]),
    ).fetchone()
    assert stored["status"] == "draft"
    marker = db.conn.execute(
        "SELECT version,status,payload FROM digests WHERE id=? ORDER BY version DESC LIMIT 1",
        ("demo-digest",),
    ).fetchone()
    assert marker["version"] == draft["version"] + 1
    assert marker["status"] == "reset"
    assert json.loads(marker["payload"])["reset_of"]["version"] == draft["version"]
    restored = {item["signal"]["signal_id"]: item["latest_decision"] for item in WorkQueue(store).list()}
    assert restored == {"pr-signal": "restore", "gr-signal": "restore"}
    assert db.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type='demo.digest_reset'"
    ).fetchone()[0] == 1
