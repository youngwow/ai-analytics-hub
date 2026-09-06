"""Append-oriented persistence for the approved product workflow."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, is_dataclass
from typing import Any

from ..common import to_utc_iso, utc_now


def _now() -> str:
    return to_utc_iso(utc_now()) or ""


def _json(value: Any) -> str:
    if is_dataclass(value):
        value = asdict(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class ProductStore:
    def __init__(self, connection: sqlite3.Connection):
        self.conn = connection

    def save_context(self, version: str, payload: Any, *, actor: str = "system") -> None:
        self.conn.execute(
            "INSERT INTO context_versions(version,payload,created_at,actor) VALUES(?,?,?,?)",
            (version, _json(payload), _now(), actor),
        )
        self.audit("context.created", "context", version, actor)
        self.conn.commit()

    def ensure_context(self, version: str, payload: Any, *, actor: str = "system") -> None:
        """Persist an immutable context version once; reject silent mutation."""
        encoded = _json(payload)
        row = self.conn.execute(
            "SELECT payload FROM context_versions WHERE version=?", (version,)
        ).fetchone()
        if row is None:
            self.save_context(version, payload, actor=actor)
            return
        if row["payload"] != encoded:
            raise ValueError(f"context version already exists with different content: {version}")

    def save_prepared(self, material_id: str, payload: Any, *, raw_document_id: int | None = None) -> int:
        version = self._next("prepared_documents", "material_id", material_id)
        self.conn.execute(
            "INSERT INTO prepared_documents(material_id,version,raw_document_id,payload,created_at) VALUES(?,?,?,?,?)",
            (material_id, version, raw_document_id, _json(payload), _now()),
        )
        self.conn.commit()
        return version

    def has_analysis(self, material_id: str, configuration_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM analysis_runs WHERE material_id=? AND configuration_id=? LIMIT 1",
            (material_id, configuration_id),
        ).fetchone()
        return row is not None

    def load_events(self) -> list[Any]:
        """Load the current event projection into provider-neutral contracts."""
        from .contracts import EventRecord

        rows = self.conn.execute("SELECT payload FROM product_events ORDER BY id").fetchall()
        events = []
        for row in rows:
            payload = json.loads(row["payload"])
            payload["signal_ids"] = tuple(payload.get("signal_ids", ()))
            payload["material_ids"] = tuple(payload.get("material_ids", ()))
            payload["embedding"] = tuple(payload.get("embedding", ()))
            events.append(EventRecord(**payload))
        return events

    def save_analysis(self, draft: Any, *, prepared_version: int) -> int:
        cur = self.conn.execute(
            """INSERT INTO analysis_runs(
                material_id,prepared_version,context_version,configuration_id,model,payload,status,created_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                draft.material_id, prepared_version, draft.context_version,
                draft.configuration_id, draft.model, _json(draft), draft.status, _now(),
            ),
        )
        run_id = int(cur.lastrowid)
        for signal in draft.signals:
            self.save_signal(signal, analysis_run_id=run_id, commit=False)
        self.audit("analysis.completed", "analysis_run", str(run_id), "ai", {"status": draft.status})
        self.conn.commit()
        return run_id

    def save_signal(
        self,
        signal: Any,
        *,
        analysis_run_id: int | None = None,
        actor: str = "ai",
        reason: str = "",
        commit: bool = True,
    ) -> int:
        revision = self._next("signal_revisions", "signal_id", signal.signal_id, "revision")
        self.conn.execute(
            "INSERT INTO signal_revisions(signal_id,revision,analysis_run_id,payload,created_at,actor,reason) VALUES(?,?,?,?,?,?,?)",
            (signal.signal_id, revision, analysis_run_id, _json(signal), _now(), actor, reason),
        )
        self.audit("signal.revised", "signal", signal.signal_id, actor, {"revision": revision}, commit=False)
        if commit:
            self.conn.commit()
        return revision

    def save_research(self, report: Any) -> int:
        cur = self.conn.execute(
            "INSERT INTO research_reports(signal_id,payload,created_at) VALUES(?,?,?)",
            (report.signal_id, _json(report), _now()),
        )
        self.audit("research.completed", "signal", report.signal_id, "ai", {"status": report.status}, commit=False)
        self.conn.commit()
        return int(cur.lastrowid)

    def save_event(self, event: Any) -> None:
        existing = self.conn.execute("SELECT version FROM product_events WHERE id=?", (event.id,)).fetchone()
        if existing and event.version <= int(existing[0]):
            raise ValueError("event version must increase")
        self.conn.execute(
            """INSERT INTO product_events(id,version,payload,created_at,updated_at)
               VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
               version=excluded.version,payload=excluded.payload,updated_at=excluded.updated_at""",
            (event.id, event.version, _json(event), _now(), _now()),
        )
        self.conn.commit()

    def save_npa_resolution(
        self,
        resolution: Any,
        payload: Any,
        *,
        jurisdiction: str = "RU",
        source_url: str = "",
    ) -> tuple[str, int]:
        """Persist an NPA object and an append-only state version atomically.

        Unresolved NPA candidates still need a stable non-empty database key.
        The synthetic key is internal and must not be presented as an official
        identifier. Repeated observations reuse the existing object and an
        identical latest state does not create a fake new version.
        """
        external_id = str(resolution.external_id or "").strip()
        if external_id:
            official_identifier = external_id
        else:
            digest = hashlib.sha256(str(resolution.object_id).encode()).hexdigest()[:16]
            official_identifier = f"unresolved:{digest}"

        row = self.conn.execute(
            "SELECT id FROM npa_records WHERE id=?",
            (str(resolution.object_id),),
        ).fetchone()
        if row is None:
            row = self.conn.execute(
                "SELECT id FROM npa_records WHERE jurisdiction=? AND official_identifier=?",
                (jurisdiction, official_identifier),
            ).fetchone()
        if row is None:
            self.conn.execute(
                """INSERT INTO npa_records(
                       id,jurisdiction,official_identifier,official_url,created_at
                   ) VALUES(?,?,?,?,?)""",
                (
                    str(resolution.object_id),
                    jurisdiction,
                    official_identifier,
                    source_url,
                    _now(),
                ),
            )
            npa_id = str(resolution.object_id)
        else:
            npa_id = str(row["id"])

        encoded = _json(payload)
        latest = self.conn.execute(
            """SELECT version,stage,payload,source_url,effective_at
               FROM npa_versions WHERE npa_id=? ORDER BY version DESC LIMIT 1""",
            (npa_id,),
        ).fetchone()
        if latest is not None and (
            latest["stage"] == resolution.current_stage
            and latest["payload"] == encoded
            and latest["source_url"] == source_url
            and latest["effective_at"] == resolution.effective_from
        ):
            self.conn.commit()
            return npa_id, int(latest["version"])

        version = int(latest["version"]) + 1 if latest is not None else 1
        self.conn.execute(
            """INSERT INTO npa_versions(
                   npa_id,version,stage,payload,source_url,effective_at,created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                npa_id,
                version,
                resolution.current_stage,
                encoded,
                source_url,
                resolution.effective_from,
                _now(),
            ),
        )
        self.audit(
            "npa.version_created",
            "npa",
            npa_id,
            "ai",
            {"version": version},
            commit=False,
        )
        self.conn.commit()
        return npa_id, version

    def save_link(self, decision: Any) -> int:
        cur = self.conn.execute(
            "INSERT INTO event_links(signal_id,event_id,relation,confidence,payload,created_at) VALUES(?,?,?,?,?,?)",
            (decision.signal_id, decision.event_id, decision.relation, decision.confidence, _json(decision), _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def save_review(self, signal_id: str, revision: int, decision: str, actor: str, payload: Any | None = None) -> int:
        if decision not in {"include", "exclude", "restore", "edit", "confirm_link", "reject_link"}:
            raise ValueError("invalid review decision")
        cur = self.conn.execute(
            "INSERT INTO review_decisions(signal_id,signal_revision,decision,payload,actor,created_at) VALUES(?,?,?,?,?,?)",
            (signal_id, revision, decision, _json(payload or {}), actor, _now()),
        )
        self.audit(f"review.{decision}", "signal", signal_id, actor, commit=False)
        self.conn.commit()
        return int(cur.lastrowid)

    def audit(self, event_type: str, object_type: str, object_id: str, actor: str, payload: Any | None = None, *, commit: bool = True) -> None:
        self.conn.execute(
            "INSERT INTO audit_events(event_type,object_type,object_id,actor,payload,created_at) VALUES(?,?,?,?,?,?)",
            (event_type, object_type, object_id, actor, _json(payload or {}), _now()),
        )
        if commit:
            self.conn.commit()

    def history(self, object_type: str, object_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM audit_events WHERE object_type=? AND object_id=? ORDER BY id",
            (object_type, object_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def _next(self, table: str, field: str, value: str, version_field: str = "version") -> int:
        # Table and field are internal constants, never user-controlled.
        row = self.conn.execute(
            f"SELECT COALESCE(MAX({version_field}),0)+1 FROM {table} WHERE {field}=?", (value,)
        ).fetchone()
        return int(row[0])
