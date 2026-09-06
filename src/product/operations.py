"""Transparent work queue, one shared digest, delivery and operational metrics."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

from .review_policy import payload_review_reasons
from .store import ProductStore, _json, _now


class DeliveryAdapter(Protocol):
    def send(self, recipient: str, payload: dict[str, Any]) -> str: ...


@dataclass(frozen=True)
class UserIdentity:
    id: str
    functional_profile: str
    access_role: str

    def __post_init__(self):
        if self.functional_profile not in {"PR", "GR"}:
            raise ValueError("functional_profile must be PR or GR")
        if self.access_role not in {"editor", "admin"}:
            raise ValueError("access_role must be editor or admin")


class WorkQueue:
    def __init__(self, store: ProductStore):
        self.store = store

    def list(self, *, profile: str | None = None, include_resolved: bool = False) -> list[dict[str, Any]]:
        rows = self.store.conn.execute(
            """SELECT s.signal_id,s.revision,s.payload,s.created_at,
                      r.decision,r.actor AS decision_actor,r.created_at AS decision_at
               FROM signal_revisions s
               JOIN (SELECT signal_id,MAX(revision) revision FROM signal_revisions GROUP BY signal_id) latest
                 ON latest.signal_id=s.signal_id AND latest.revision=s.revision
               LEFT JOIN review_decisions r ON r.id=(
                 SELECT id FROM review_decisions WHERE signal_id=s.signal_id ORDER BY id DESC LIMIT 1
               ) ORDER BY s.id DESC"""
        ).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row["payload"])
            if profile and payload.get("interest") not in {profile, "BOTH"}:
                continue
            if not include_resolved and row["decision"] in {"include", "exclude"}:
                continue
            result.append(
                {
                    "signal": payload,
                    "review_required": bool(payload_review_reasons(payload)),
                    "review_reasons": list(payload_review_reasons(payload)),
                    "revision": row["revision"],
                    "latest_decision": row["decision"],
                    "decision_actor": row["decision_actor"],
                    "decision_at": row["decision_at"],
                }
            )
        return result


class DigestService:
    def __init__(self, store: ProductStore):
        self.store = store

    def create_draft(
        self,
        digest_id: str,
        period_from: str,
        period_to: str,
        signal_ids: list[str],
        *,
        actor: str,
        recipients: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        if not signal_ids:
            raise ValueError("digest requires at least one confirmed signal")
        items = []
        for signal_id in dict.fromkeys(signal_ids):
            row = self.store.conn.execute(
                "SELECT revision,payload FROM signal_revisions WHERE signal_id=? ORDER BY revision DESC LIMIT 1",
                (signal_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"unknown signal: {signal_id}")
            decision = self.store.conn.execute(
                "SELECT decision FROM review_decisions WHERE signal_id=? ORDER BY id DESC LIMIT 1",
                (signal_id,),
            ).fetchone()
            if not decision or decision["decision"] != "include":
                raise ValueError(f"signal is not confirmed for inclusion: {signal_id}")
            signal = json.loads(row["payload"])
            if not signal.get("claims"):
                raise ValueError(f"signal has no source-grounded claims: {signal_id}")
            section = self._section(signal)
            items.append({"signal_id": signal_id, "signal_revision": row["revision"], "section": section, "signal": signal})
        items.sort(key=lambda x: ({"critical": 0, "gr": 1, "pr": 2}[x["section"]], x["signal_id"]))
        version = self._next_version(digest_id)
        payload = {
            "id": digest_id,
            "version": version,
            "period": {"from": period_from, "to": period_to},
            "items": items,
            "recipients": recipients or [],
        }
        self.store.conn.execute(
            "INSERT INTO digests(id,version,period_from,period_to,status,payload,created_at,actor) VALUES(?,?,?,?,?,?,?,?)",
            (digest_id, version, period_from, period_to, "draft", _json(payload), _now(), actor),
        )
        self.store.audit("digest.draft_created", "digest", digest_id, actor, {"version": version}, commit=False)
        self.store.conn.commit()
        return payload

    def approve(self, digest_id: str, version: int, *, actor: str) -> dict[str, Any]:
        row = self.store.conn.execute(
            "SELECT * FROM digests WHERE id=? AND version=?", (digest_id, version)
        ).fetchone()
        if row is None:
            raise LookupError("digest version not found")
        payload = json.loads(row["payload"])
        if not payload.get("recipients"):
            raise ValueError("at least one delivery recipient is required")
        if row["status"] != "draft":
            raise ValueError("only a draft can be approved")
        self.store.conn.execute(
            "UPDATE digests SET status='approved' WHERE id=? AND version=?", (digest_id, version)
        )
        self.store.audit("digest.approved", "digest", digest_id, actor, {"version": version}, commit=False)
        self.store.conn.commit()
        return payload

    def deliver(
        self,
        digest_id: str,
        version: int,
        adapters: dict[str, DeliveryAdapter],
    ) -> list[dict[str, Any]]:
        row = self.store.conn.execute(
            "SELECT * FROM digests WHERE id=? AND version=?", (digest_id, version)
        ).fetchone()
        if row is None or row["status"] not in {"approved", "delivered", "delivery_failed"}:
            raise ValueError("only an approved digest can be delivered")
        payload = json.loads(row["payload"])
        results = []
        for recipient in payload.get("recipients", []):
            channel, target = recipient.get("channel", ""), recipient.get("recipient", "")
            key = hashlib.sha256(f"digest:{digest_id}:{version}:{channel}:{target}".encode()).hexdigest()
            previous = self.store.conn.execute(
                "SELECT status,error FROM delivery_attempts WHERE idempotency_key=?", (key,)
            ).fetchone()
            if previous and previous["status"] == "delivered":
                results.append({"channel": channel, "recipient": target, "status": "already_delivered"})
                continue
            if channel not in adapters:
                status, error = "failed", f"delivery adapter is not configured: {channel}"
            else:
                try:
                    adapters[channel].send(target, payload)
                    status, error = "delivered", ""
                except Exception as exc:
                    status, error = "failed", f"{type(exc).__name__}: {exc}"
            self.store.conn.execute(
                """INSERT INTO delivery_attempts(
                    idempotency_key,digest_id,digest_version,channel,recipient,status,error,created_at
                ) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(idempotency_key) DO UPDATE SET
                    status=excluded.status,error=excluded.error,created_at=excluded.created_at""",
                (key, digest_id, version, channel, target, status, error, _now()),
            )
            results.append({"channel": channel, "recipient": target, "status": status, "error": error})
        final_status = "delivered" if results and all(x["status"] in {"delivered", "already_delivered"} for x in results) else "delivery_failed"
        self.store.conn.execute(
            "UPDATE digests SET status=? WHERE id=? AND version=?", (final_status, digest_id, version)
        )
        self.store.audit("digest.delivery_finished", "digest", digest_id, "system", {"version": version, "status": final_status}, commit=False)
        self.store.conn.commit()
        return results

    def _next_version(self, digest_id: str) -> int:
        row = self.store.conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM digests WHERE id=?", (digest_id,)
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _section(signal: dict[str, Any]) -> str:
        if signal.get("importance") == "critical" or signal.get("urgency") == "urgent":
            return "critical"
        return "gr" if signal.get("interest") == "GR" else "pr"


class MetricsService:
    def __init__(self, store: ProductStore):
        self.store = store

    def snapshot(self) -> dict[str, Any]:
        def scalar(sql: str) -> int:
            return int(self.store.conn.execute(sql).fetchone()[0])

        return {
            "prepared_documents": scalar("SELECT COUNT(*) FROM prepared_documents"),
            "analysis_runs": scalar("SELECT COUNT(*) FROM analysis_runs"),
            "signals": scalar("SELECT COUNT(DISTINCT signal_id) FROM signal_revisions"),
            "signal_revisions": scalar("SELECT COUNT(*) FROM signal_revisions"),
            "research_reports": scalar("SELECT COUNT(*) FROM research_reports"),
            "events": scalar("SELECT COUNT(*) FROM product_events"),
            "npa_records": scalar("SELECT COUNT(*) FROM npa_records"),
            "pending_queue": len(WorkQueue(self.store).list()),
            "delivery_failures": scalar("SELECT COUNT(*) FROM delivery_attempts WHERE status='failed'"),
        }
