from __future__ import annotations

from src.product.contracts import EvidenceClaim, SignalDraft
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
