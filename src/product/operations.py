"""Transparent work queue, one shared digest, delivery and operational metrics."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

from .review_policy import payload_review_reasons, should_surface_in_work_queue
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
                      r.decision,r.actor AS decision_actor,r.created_at AS decision_at,
                      so.name AS source_name,so.kind AS source_kind,
                      so.category AS source_category,d.published_at,
                      (SELECT event_id FROM event_links e
                       WHERE e.signal_id=s.signal_id ORDER BY e.id DESC LIMIT 1) AS event_id
               FROM signal_revisions s
               JOIN (SELECT signal_id,MAX(revision) revision FROM signal_revisions GROUP BY signal_id) latest
                 ON latest.signal_id=s.signal_id AND latest.revision=s.revision
               LEFT JOIN analysis_runs a ON a.id=s.analysis_run_id
               LEFT JOIN prepared_documents p
                 ON p.material_id=a.material_id AND p.version=a.prepared_version
               LEFT JOIN documents d ON d.id=p.raw_document_id
               LEFT JOIN sources so ON so.id=d.source_id
               LEFT JOIN review_decisions r ON r.id=(
                 SELECT id FROM review_decisions WHERE signal_id=s.signal_id ORDER BY id DESC LIMIT 1
               ) ORDER BY s.id DESC"""
        ).fetchall()
        result = []
        seen_events: set[str] = set()
        seen_npa: set[str] = set()
        for row in rows:
            payload = json.loads(row["payload"])
            if profile and payload.get("interest") not in {profile, "BOTH"}:
                continue
            if not include_resolved and row["decision"] in {"include", "exclude"}:
                continue
            if not include_resolved:
                if not should_surface_in_work_queue(payload):
                    continue
                event_id = str(row["event_id"] or "")
                npa_identifier = str(payload.get("npa_identifier") or "").strip().casefold()
                if event_id and event_id in seen_events:
                    continue
                if npa_identifier and npa_identifier in seen_npa:
                    continue
                if event_id:
                    seen_events.add(event_id)
                if npa_identifier:
                    seen_npa.add(npa_identifier)
            result.append(
                {
                    "signal": payload,
                    "created_at": row["created_at"],
                    "review_required": bool(payload_review_reasons(payload)),
                    "review_reasons": list(payload_review_reasons(payload)),
                    "revision": row["revision"],
                    "latest_decision": row["decision"],
                    "decision_actor": row["decision_actor"],
                    "decision_at": row["decision_at"],
                    "source_name": row["source_name"],
                    "source_kind": row["source_kind"],
                    "source_category": row["source_category"],
                    "published_at": row["published_at"],
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
        title: str = "",
        header: str = "",
        footer: str = "",
    ) -> dict[str, Any]:
        if not signal_ids:
            raise ValueError("digest requires at least one confirmed signal")
        try:
            start = date.fromisoformat(period_from)
            end = date.fromisoformat(period_to)
        except ValueError as exc:
            raise ValueError("digest period must use YYYY-MM-DD dates") from exc
        if start > end:
            raise ValueError("digest period start must not be after its end")
        items = []
        for signal_id in dict.fromkeys(signal_ids):
            row = self.store.conn.execute(
                "SELECT revision,payload FROM signal_revisions WHERE signal_id=? ORDER BY revision DESC LIMIT 1",
                (signal_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"unknown signal: {signal_id}")
            decision = self.store.conn.execute(
                "SELECT decision,actor FROM review_decisions WHERE signal_id=? ORDER BY id DESC LIMIT 1",
                (signal_id,),
            ).fetchone()
            if not decision or decision["decision"] != "include":
                raise ValueError(f"signal is not confirmed for inclusion: {signal_id}")
            signal = json.loads(row["payload"])
            if not signal.get("claims"):
                raise ValueError(f"signal has no source-grounded claims: {signal_id}")
            section = self._section(signal, decision["actor"])
            items.append({"signal_id": signal_id, "signal_revision": row["revision"], "section": section, "signal": signal})
        # Python's stable sort keeps the order chosen by the operator inside
        # each block while placing PR before GR in the shared digest.
        items.sort(key=lambda item: {"pr": 0, "gr": 1}[item["section"]])
        version = self._next_version(digest_id)
        payload = {
            "id": digest_id,
            "version": version,
            "period": {"from": period_from, "to": period_to},
            "items": items,
            "recipients": recipients or [],
            "title": title.strip() or "Информационная повестка GS Labs",
            "header": header.strip(),
            "footer": footer.strip(),
            "readiness": {"PR": None, "GR": None},
            "role_content": {
                "PR": {"status": "not_generated", "text": "", "signal_ids": []},
                "GR": {"status": "not_generated", "text": "", "signal_ids": []},
            },
        }
        self.store.conn.execute(
            "INSERT INTO digests(id,version,period_from,period_to,status,payload,created_at,actor) VALUES(?,?,?,?,?,?,?,?)",
            (digest_id, version, period_from, period_to, "draft", _json(payload), _now(), actor),
        )
        self.store.audit("digest.draft_created", "digest", digest_id, actor, {"version": version}, commit=False)
        self.store.conn.commit()
        return payload

    def reset_demo(self, *, actor: str) -> dict[str, Any]:
        """Reset the demo workspace without deleting its audit history."""
        latest = self.store.conn.execute(
            """SELECT id,version,status,period_from,period_to,payload
               FROM digests ORDER BY created_at DESC,version DESC LIMIT 1"""
        ).fetchone()
        reset_version = int(latest["version"]) if latest else None
        if latest and latest["status"] != "reset":
            previous = json.loads(latest["payload"])
            reset_version = self._next_version(str(latest["id"]))
            marker = {
                "id": latest["id"],
                "version": reset_version,
                "period": {"from": latest["period_from"], "to": latest["period_to"]},
                "items": [],
                "recipients": [],
                "title": previous.get("title") or "Информационная повестка GS Labs",
                "header": previous.get("header") or "",
                "footer": previous.get("footer") or "",
                "readiness": {"PR": None, "GR": None},
                "role_content": {
                    "PR": {"status": "not_generated", "text": "", "signal_ids": []},
                    "GR": {"status": "not_generated", "text": "", "signal_ids": []},
                },
                "reset_of": {"id": latest["id"], "version": latest["version"], "status": latest["status"]},
            }
            self.store.conn.execute(
                """INSERT INTO digests(
                       id,version,period_from,period_to,status,payload,created_at,actor
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    latest["id"],
                    reset_version,
                    latest["period_from"],
                    latest["period_to"],
                    "reset",
                    _json(marker),
                    _now(),
                    actor,
                ),
            )
        returned = 0
        for item in WorkQueue(self.store).list(include_resolved=True):
            if item.get("latest_decision") != "include":
                continue
            self.store.conn.execute(
                """INSERT INTO review_decisions(
                       signal_id,signal_revision,decision,payload,actor,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    item["signal"]["signal_id"],
                    item["revision"],
                    "restore",
                    _json({"reason": "demo_reset"}),
                    actor,
                    _now(),
                ),
            )
            returned += 1
        self.store.audit(
            "demo.digest_reset",
            "digest",
            latest["id"] if latest else "workspace",
            actor,
            {"version": reset_version, "signals_returned": returned},
            commit=False,
        )
        self.store.conn.commit()
        return {"status": "reset", "signals_returned": returned}

    def role_items(self, role: str, signal_ids: list[str]) -> list[dict[str, Any]]:
        if role not in {"PR", "GR"}:
            raise ValueError("role must be PR or GR")
        items: list[dict[str, Any]] = []
        for signal_id in dict.fromkeys(signal_ids):
            row = self.store.conn.execute(
                "SELECT revision,payload FROM signal_revisions WHERE signal_id=? ORDER BY revision DESC LIMIT 1",
                (signal_id,),
            ).fetchone()
            decision = self.store.conn.execute(
                "SELECT decision,actor FROM review_decisions WHERE signal_id=? ORDER BY id DESC LIMIT 1",
                (signal_id,),
            ).fetchone()
            if row is None or not decision or decision["decision"] != "include":
                raise ValueError(f"signal is not confirmed for inclusion: {signal_id}")
            signal = json.loads(row["payload"])
            if self._section(signal, decision["actor"]).upper() != role:
                raise ValueError(f"signal belongs to another role: {signal_id}")
            source = self.store.conn.execute(
                """SELECT d.url FROM prepared_documents p
                   JOIN documents d ON d.id=p.raw_document_id
                   WHERE p.material_id=? ORDER BY p.version DESC LIMIT 1""",
                (signal.get("material_id"),),
            ).fetchone()
            items.append(
                {
                    "signal_id": signal_id,
                    "signal_revision": row["revision"],
                    "section": role.casefold(),
                    "signal": signal,
                    "source_url": source["url"] if source else "",
                }
            )
        return items

    def update_role_content(
        self,
        digest_id: str,
        version: int,
        *,
        role: str,
        signal_ids: list[str],
        text: str,
        actor: str,
        model: str,
    ) -> dict[str, Any]:
        if not text.strip():
            raise ValueError("generated digest block must not be empty")
        role_items = self.role_items(role, signal_ids)
        row = self.store.conn.execute(
            "SELECT status,payload FROM digests WHERE id=? AND version=?", (digest_id, version)
        ).fetchone()
        if row is None:
            raise LookupError("digest version not found")
        if row["status"] != "draft":
            raise ValueError("only a draft can be edited")
        payload = json.loads(row["payload"])
        payload["items"] = [
            item for item in payload.get("items", []) if item.get("section") != role.casefold()
        ] + role_items
        payload["items"].sort(key=lambda item: {"pr": 0, "gr": 1}[item["section"]])
        role_content = payload.setdefault("role_content", {})
        previous = role_content.get(role, {})
        now = datetime.now(UTC).isoformat(timespec="seconds")
        role_content[role] = {
            "status": "generated",
            "text": text.strip(),
            "signal_ids": [item["signal_id"] for item in role_items],
            "generated_at": previous.get("generated_at") or now,
            "updated_at": now,
            "actor": actor,
            "model": model,
        }
        payload.setdefault("readiness", {"PR": None, "GR": None})[role] = None
        self.store.conn.execute(
            "UPDATE digests SET payload=? WHERE id=? AND version=?",
            (_json(payload), digest_id, version),
        )
        self.store.audit(
            "digest.role_content_updated",
            "digest",
            digest_id,
            actor,
            {"version": version, "role": role, "model": model},
            commit=False,
        )
        self.store.conn.commit()
        return payload

    def mark_ready(
        self,
        digest_id: str,
        version: int,
        *,
        role: str,
        actor: str,
    ) -> dict[str, Any]:
        if role not in {"PR", "GR"}:
            raise ValueError("role must be PR or GR")
        row = self.store.conn.execute(
            "SELECT status,payload FROM digests WHERE id=? AND version=?", (digest_id, version)
        ).fetchone()
        if row is None:
            raise LookupError("digest version not found")
        if row["status"] != "draft":
            raise ValueError("only a draft can be marked ready")
        payload = json.loads(row["payload"])
        role_content = payload.get("role_content", {}).get(role, {})
        if role_content.get("status") != "generated" or not role_content.get("text"):
            raise ValueError(f"{role} digest block must be generated before readiness")
        expected_ids = {
            item["signal"]["signal_id"]
            for item in WorkQueue(self.store).list(include_resolved=True)
            if item.get("latest_decision") == "include"
            and self._section(item["signal"], item.get("decision_actor") or "").upper() == role
        }
        if set(role_content.get("signal_ids") or []) != expected_ids:
            raise ValueError(f"{role} digest block is stale and must be regenerated")
        readiness = payload.setdefault("readiness", {"PR": None, "GR": None})
        readiness[role] = {
            "actor": actor,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        if readiness.get("PR") and readiness.get("GR"):
            queue = WorkQueue(self.store).list(include_resolved=True)
            stale_roles = []
            for candidate in ("PR", "GR"):
                candidate_expected = {
                    item["signal"]["signal_id"]
                    for item in queue
                    if item.get("latest_decision") == "include"
                    and self._section(
                        item["signal"], item.get("decision_actor") or ""
                    ).upper()
                    == candidate
                }
                candidate_content = payload.get("role_content", {}).get(candidate, {})
                if (
                    candidate_content.get("status") != "generated"
                    or set(candidate_content.get("signal_ids") or []) != candidate_expected
                ):
                    stale_roles.append(candidate)
            if stale_roles:
                for stale_role in stale_roles:
                    readiness[stale_role] = None
                self.store.conn.execute(
                    "UPDATE digests SET payload=? WHERE id=? AND version=?",
                    (_json(payload), digest_id, version),
                )
                self.store.audit(
                    "digest.readiness_invalidated",
                    "digest",
                    digest_id,
                    "system",
                    {"version": version, "roles": stale_roles},
                    commit=False,
                )
                self.store.conn.commit()
                raise ValueError(
                    f"digest changed; regenerate blocks: {', '.join(stale_roles)}"
                )
        self.store.conn.execute(
            "UPDATE digests SET payload=? WHERE id=? AND version=?",
            (_json(payload), digest_id, version),
        )
        self.store.audit(
            "digest.role_ready",
            "digest",
            digest_id,
            actor,
            {"version": version, "role": role},
            commit=False,
        )
        self.store.conn.commit()
        return {
            "digest": payload,
            "all_ready": bool(readiness.get("PR") and readiness.get("GR")),
        }

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
    def _section(signal: dict[str, Any], actor: str = "") -> str:
        normalized_actor = actor.casefold()
        if normalized_actor.startswith("demo-gr") or normalized_actor.startswith("gr-"):
            return "gr"
        if normalized_actor.startswith("demo-pr") or normalized_actor.startswith("pr-"):
            return "pr"
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
