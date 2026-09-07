"""Minimal review-first web application over the product SQLite projection."""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ..paths import DEFAULT_PATHS
from ..storage import Database
from .contracts import GsLabsContext
from .operations import DigestService, MetricsService, WorkQueue
from .store import ProductStore

WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
MAX_REQUEST_BYTES = 1_000_000


class ApiError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class DashboardApplication:
    """Request-independent API facade; every call owns a SQLite connection."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def dispatch(self, method: str, path: str, query: dict[str, list[str]], body: dict):
        db = Database(self.db_path)
        try:
            store = ProductStore(db.conn)
            if method == "GET" and path == "/api/overview":
                return 200, self._overview(db, store)
            if method == "GET" and path == "/api/queue":
                profile = self._one(query, "profile") or None
                if profile and profile not in {"PR", "GR"}:
                    raise ApiError(400, "profile must be PR or GR")
                include_resolved = self._one(query, "resolved") == "1"
                return 200, {
                    "items": WorkQueue(store).list(
                        profile=profile, include_resolved=include_resolved
                    )
                }
            if method == "GET" and path == "/api/failures":
                return 200, {"items": self._failures(db)}
            if method == "GET" and path == "/api/filtered":
                return 200, {"items": self._filtered(db)}
            if method == "GET" and path.startswith("/api/signals/"):
                return 200, self._signal_detail(db, unquote(path.removeprefix("/api/signals/")))
            if method == "POST" and path == "/api/review":
                return 201, self._review(store, body)
            if method == "GET" and path == "/api/context":
                return 200, self._context(db)
            if method == "PUT" and path == "/api/context":
                return 201, self._save_context(store, body)
            if method == "GET" and path == "/api/sources":
                return 200, {"items": self._sources(db)}
            match = re.fullmatch(r"/api/sources/(\d+)", path)
            if method == "GET" and match:
                return 200, self._source_detail(db, int(match.group(1)))
            if method == "GET" and path == "/api/events":
                lifecycle = self._one(query, "state") or "active"
                if lifecycle not in {"active", "archived", "all"}:
                    raise ApiError(400, "state must be active, archived or all")
                return 200, {"items": self._events(db, lifecycle)}
            match = re.fullmatch(r"/api/events/([^/]+)", path)
            if method == "GET" and match:
                return 200, self._event_detail(db, unquote(match.group(1)))
            if method == "GET" and path == "/api/npa":
                return 200, {"items": self._npa(db)}
            if method == "GET" and path == "/api/digests":
                return 200, {"items": self._digests(db)}
            if method == "POST" and path == "/api/digests":
                return 201, self._create_digest(store, body)
            match = re.fullmatch(r"/api/digests/([^/]+)/(\d+)/approve", path)
            if method == "POST" and match:
                digest_id, version = unquote(match.group(1)), int(match.group(2))
                return 200, DigestService(store).approve(
                    digest_id, version, actor=str(body.get("actor") or "operator")
                )
            if method == "GET" and path == "/api/history":
                return 200, {"items": self._history(db)}
            raise ApiError(404, "endpoint not found")
        finally:
            db.close()

    @staticmethod
    def _one(query: dict[str, list[str]], key: str) -> str:
        values = query.get(key, [])
        return values[0] if values else ""

    def _overview(self, db: Database, store: ProductStore) -> dict:
        metrics = MetricsService(store).snapshot()
        source_rows = self._sources(db)
        metrics.update(
            {
                "sources_total": len(source_rows),
                "sources_attention": sum(
                    row["state"] in {"attention", "coverage_gap"} for row in source_rows
                ),
                "sources_unchecked": sum(row["state"] == "not_checked" for row in source_rows),
                "review_required": sum(
                    item["review_required"]
                    for item in WorkQueue(store).list(include_resolved=False)
                ),
                "analysis_failures": db.conn.execute(
                    "SELECT COUNT(*) FROM analysis_runs WHERE status IN ('failed','unreadable')"
                ).fetchone()[0],
                "workflow_failures": len(self._workflow_failures(db)),
                "filtered_materials": len(self._filtered(db)),
            }
        )
        return metrics

    @staticmethod
    def _signal_detail(db: Database, signal_id: str) -> dict:
        row = db.conn.execute(
            """SELECT s.payload,s.revision,s.created_at,s.actor,s.reason,
                      a.status AS analysis_status,a.configuration_id,a.model,a.context_version,
                      p.payload AS prepared_payload,d.url AS source_url,d.title AS source_title,
                      d.text AS source_text,d.published_at,d.fetched_at,so.name AS source_name
               FROM signal_revisions s
               LEFT JOIN analysis_runs a ON a.id=s.analysis_run_id
               LEFT JOIN prepared_documents p
                 ON p.material_id=a.material_id AND p.version=a.prepared_version
               LEFT JOIN documents d ON d.id=p.raw_document_id
               LEFT JOIN sources so ON so.id=d.source_id
               WHERE s.signal_id=? ORDER BY s.revision DESC LIMIT 1""",
            (signal_id,),
        ).fetchone()
        if row is None:
            raise ApiError(404, "signal not found")
        decisions = [
            dict(item)
            for item in db.conn.execute(
                "SELECT decision,payload,actor,created_at FROM review_decisions WHERE signal_id=? ORDER BY id DESC",
                (signal_id,),
            )
        ]
        links = [
            dict(item)
            for item in db.conn.execute(
                "SELECT event_id,relation,confidence,payload,created_at FROM event_links WHERE signal_id=? ORDER BY id DESC",
                (signal_id,),
            )
        ]
        return {
            "signal": json.loads(row["payload"]),
            "revision": row["revision"],
            "created_at": row["created_at"],
            "actor": row["actor"],
            "revision_reason": row["reason"],
            "analysis": {
                "status": row["analysis_status"],
                "configuration_id": row["configuration_id"],
                "model": row["model"],
                "context_version": row["context_version"],
            },
            "source": {
                "name": row["source_name"],
                "title": row["source_title"],
                "url": row["source_url"],
                "text": row["source_text"],
                "published_at": row["published_at"],
                "fetched_at": row["fetched_at"],
                "prepared": json.loads(row["prepared_payload"])
                if row["prepared_payload"]
                else None,
            },
            "decisions": [DashboardApplication._decode_payload(item) for item in decisions],
            "event_links": [DashboardApplication._decode_payload(item) for item in links],
        }

    @staticmethod
    def _review(store: ProductStore, body: dict) -> dict:
        signal_id = str(body.get("signal_id") or "").strip()
        decision = str(body.get("decision") or "").strip()
        actor = str(body.get("actor") or "operator").strip()
        try:
            revision = int(body.get("revision"))
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "revision must be an integer") from exc
        if not signal_id:
            raise ApiError(400, "signal_id is required")
        try:
            decision_id = store.save_review(
                signal_id,
                revision,
                decision,
                actor,
                {"note": str(body.get("note") or "")},
            )
        except ValueError as exc:
            raise ApiError(400, str(exc)) from exc
        return {"status": "ok", "decision_id": decision_id}

    @staticmethod
    def _context(db: Database) -> dict:
        row = db.conn.execute(
            "SELECT version,payload,created_at,actor FROM context_versions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return {"version": None, "context": None, "created_at": None, "actor": None}
        return {
            "version": row["version"],
            "context": json.loads(row["payload"]),
            "created_at": row["created_at"],
            "actor": row["actor"],
        }

    @staticmethod
    def _save_context(store: ProductStore, body: dict) -> dict:
        version = str(body.get("version") or "").strip()
        actor = str(body.get("actor") or "operator").strip()
        if not version:
            raise ApiError(400, "version is required")
        try:
            context = GsLabsContext.from_dict(body.get("context") or {})
            if context.version != version:
                raise ValueError("context.version must equal version")
            store.ensure_context(version, asdict(context), actor=actor)
        except (TypeError, ValueError) as exc:
            raise ApiError(400, str(exc)) from exc
        return {"status": "created", "version": version}

    @staticmethod
    def _sources(db: Database) -> list[dict]:
        rows = db.conn.execute(
            """SELECT s.*,f.last_fetch_at,f.last_success_at,f.last_error,
                      f.consecutive_failures,f.backlog_status,f.coverage_status,
                      (SELECT COUNT(*) FROM documents d WHERE d.source_id=s.id) AS documents
               FROM sources s LEFT JOIN fetch_state f ON f.source_id=s.id ORDER BY s.id"""
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if not item["enabled"]:
                state = "disabled"
            elif item.get("coverage_status") == "gap":
                state = "coverage_gap"
            elif item.get("last_error"):
                state = "attention"
            elif item.get("last_success_at"):
                state = "working"
            else:
                state = "not_checked"
            item["state"] = state
            result.append(item)
        return result

    @staticmethod
    def _source_detail(db: Database, source_id: int) -> dict:
        source = next(
            (item for item in DashboardApplication._sources(db) if item["id"] == source_id),
            None,
        )
        if source is None:
            raise ApiError(404, "source not found")
        rows = db.conn.execute(
            """SELECT d.id,d.title,d.url,d.summary,d.published_at,d.fetched_at,
                      length(d.text) AS text_length,
                      (SELECT status FROM analysis_runs a
                       JOIN prepared_documents p ON p.material_id=a.material_id
                       WHERE p.raw_document_id=d.id ORDER BY a.id DESC LIMIT 1)
                       AS analysis_status
               FROM documents d WHERE d.source_id=? AND d.hidden=0
               ORDER BY COALESCE(d.published_at,d.fetched_at) DESC LIMIT 30""",
            (source_id,),
        ).fetchall()
        return {"source": source, "materials": [dict(row) for row in rows]}

    @staticmethod
    def _payload_rows(db: Database, table: str, order_field: str) -> list[dict]:
        rows = db.conn.execute(f"SELECT * FROM {table} ORDER BY {order_field} DESC").fetchall()
        return [DashboardApplication._decode_payload(dict(row)) for row in rows]

    @staticmethod
    def _events(db: Database, lifecycle: str) -> list[dict]:
        if lifecycle == "all":
            rows = db.conn.execute(
                "SELECT * FROM product_events ORDER BY updated_at DESC"
            ).fetchall()
        else:
            rows = db.conn.execute(
                """SELECT * FROM product_events
                   WHERE lifecycle_state=? ORDER BY updated_at DESC""",
                (lifecycle,),
            ).fetchall()
        return [DashboardApplication._decode_payload(dict(row)) for row in rows]

    @staticmethod
    def _event_detail(db: Database, event_id: str) -> dict:
        row = db.conn.execute(
            "SELECT * FROM product_events WHERE id=?", (event_id,)
        ).fetchone()
        if row is None:
            raise ApiError(404, "event not found")
        event = DashboardApplication._decode_payload(dict(row))
        material_ids = list((event.get("payload") or {}).get("material_ids") or [])
        materials: list[dict] = []
        if material_ids:
            placeholders = ",".join("?" for _ in material_ids)
            rows = db.conn.execute(
                f"""SELECT p.material_id,p.version,d.id,d.title,d.url,d.published_at,
                            d.fetched_at,s.name AS source_name,s.kind
                     FROM prepared_documents p
                     LEFT JOIN documents d ON d.id=p.raw_document_id
                     LEFT JOIN sources s ON s.id=d.source_id
                     WHERE p.material_id IN ({placeholders})
                     ORDER BY COALESCE(d.published_at,d.fetched_at) DESC""",
                material_ids,
            ).fetchall()
            materials = [dict(item) for item in rows]
        return {"event": event, "materials": materials}

    @staticmethod
    def _npa(db: Database) -> list[dict]:
        rows = db.conn.execute(
            """SELECT n.*,
                      v.version,v.stage,v.payload AS version_payload,v.source_url,
                      v.effective_at,v.created_at AS version_created_at
               FROM npa_records n
               LEFT JOIN npa_versions v ON v.id=(
                 SELECT id FROM npa_versions WHERE npa_id=n.id ORDER BY version DESC LIMIT 1
               ) ORDER BY n.created_at DESC"""
        ).fetchall()
        records = [
            DashboardApplication._decode_payload(dict(row), "version_payload")
            for row in rows
        ]

        signal_rows = db.conn.execute(
            """SELECT s.signal_id,s.payload,r.decision,r.actor,r.created_at AS decision_at
               FROM signal_revisions s
               JOIN (SELECT signal_id,MAX(revision) AS revision
                     FROM signal_revisions GROUP BY signal_id) latest
                 ON latest.signal_id=s.signal_id AND latest.revision=s.revision
               LEFT JOIN review_decisions r ON r.id=(
                 SELECT id FROM review_decisions
                 WHERE signal_id=s.signal_id ORDER BY id DESC LIMIT 1
               )"""
        ).fetchall()
        signals_by_material: dict[str, list[dict]] = {}
        for row in signal_rows:
            signal = json.loads(row["payload"])
            signal["latest_decision"] = row["decision"]
            signal["decision_actor"] = row["actor"]
            signal["decision_at"] = row["decision_at"]
            signals_by_material.setdefault(str(signal.get("material_id") or ""), []).append(signal)

        digest_rows = db.conn.execute(
            "SELECT id,version,status,payload,created_at FROM digests ORDER BY created_at DESC,version DESC"
        ).fetchall()
        current_digest = digest_rows[0] if digest_rows and digest_rows[0]["status"] != "reset" else None
        current_by_signal: dict[str, dict] = {}
        delivered_by_signal: dict[str, dict] = {}
        for digest_row in digest_rows:
            payload = json.loads(digest_row["payload"])
            reference = {
                "id": digest_row["id"],
                "version": digest_row["version"],
                "status": digest_row["status"],
                "created_at": digest_row["created_at"],
                "title": payload.get("title") or "Информационная повестка GS Labs",
            }
            for item in payload.get("items") or []:
                signal_id = str(item.get("signal_id") or "")
                if digest_row is current_digest:
                    current_by_signal[signal_id] = reference
                if digest_row["status"] == "delivered" and signal_id not in delivered_by_signal:
                    delivered_by_signal[signal_id] = reference

        rank = {"selected": 5, "approved": 5, "pending": 4, "delivered": 3, "excluded": 2, "filtered": 1}
        for record in records:
            payload = record.get("version_payload") or {}
            material_ids = [str(value) for value in payload.get("member_ids") or []]
            related = [signal for material_id in material_ids for signal in signals_by_material.get(material_id, [])]
            candidates = []
            for signal in related:
                signal_id = str(signal.get("signal_id") or "")
                current = current_by_signal.get(signal_id)
                delivered = delivered_by_signal.get(signal_id)
                if current and current["status"] == "delivered":
                    workflow_status, digest = "delivered", current
                elif current and current["status"] in {"draft", "approved"}:
                    workflow_status, digest = ("approved" if current["status"] == "approved" else "selected"), current
                elif delivered:
                    workflow_status, digest = "delivered", delivered
                elif signal.get("latest_decision") == "include":
                    workflow_status, digest = "selected", None
                elif signal.get("latest_decision") == "exclude":
                    workflow_status, digest = "excluded", None
                elif signal.get("relevance") == "irrelevant":
                    workflow_status, digest = "filtered", None
                else:
                    workflow_status, digest = "pending", None
                candidates.append((rank[workflow_status], workflow_status, digest, signal))
            chosen = max(candidates, default=(0, "pending", None, {}), key=lambda item: item[0])
            record["workflow_status"] = chosen[1]
            record["last_digest"] = chosen[2]
            record["related_signal_ids"] = [str(signal.get("signal_id") or "") for signal in related]
            record["relevance"] = "relevant" if any(signal.get("relevance") == "relevant" for signal in related) else (
                "irrelevant" if related and all(signal.get("relevance") == "irrelevant" for signal in related) else "unknown"
            )
        return records

    @staticmethod
    def _digests(db: Database) -> list[dict]:
        rows = db.conn.execute(
            "SELECT * FROM digests ORDER BY created_at DESC,version DESC"
        ).fetchall()
        return [DashboardApplication._decode_payload(dict(row)) for row in rows]

    @staticmethod
    def _history(db: Database) -> list[dict]:
        rows = db.conn.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT 1000").fetchall()
        return [DashboardApplication._decode_payload(dict(row)) for row in rows]

    @staticmethod
    def _failures(db: Database) -> list[dict]:
        rows = db.conn.execute(
            """SELECT a.id,a.material_id,a.status,a.configuration_id,a.model,
                      a.payload,a.created_at,p.payload AS prepared_payload
               FROM analysis_runs a
               LEFT JOIN prepared_documents p
                 ON p.material_id=a.material_id AND p.version=a.prepared_version
               WHERE a.status IN ('failed','unreadable')
               ORDER BY a.id DESC"""
        ).fetchall()
        result = []
        for row in rows:
            item = DashboardApplication._decode_payload(dict(row))
            item = DashboardApplication._decode_payload(item, "prepared_payload")
            item["failure_type"] = "analysis"
            result.append(item)
        result.extend(DashboardApplication._workflow_failures(db))
        result.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return result

    @staticmethod
    def _workflow_failures(db: Database) -> list[dict]:
        rows = db.conn.execute(
            """SELECT a.id,a.object_id AS material_id,a.payload,a.created_at
               FROM audit_events a
               JOIN (
                 SELECT object_id,MAX(id) AS id FROM audit_events
                 WHERE object_type='material'
                   AND event_type IN ('workflow.completed','workflow.failed')
                 GROUP BY object_id
               ) latest ON latest.id=a.id
               WHERE a.event_type='workflow.failed'
               ORDER BY a.id DESC"""
        ).fetchall()
        result = []
        for row in rows:
            item = DashboardApplication._decode_payload(dict(row))
            item["failure_type"] = "workflow"
            item["status"] = "failed"
            result.append(item)
        return result

    @staticmethod
    def _filtered(db: Database) -> list[dict]:
        """Latest explicit no-signal decisions, with their reviewable source."""
        rows = db.conn.execute(
            """SELECT a.id,a.material_id,a.status,a.configuration_id,a.model,
                      a.payload,a.created_at,p.payload AS prepared_payload,
                      d.url AS source_url,d.text AS source_text,d.published_at,
                      so.name AS source_name
               FROM analysis_runs a
               JOIN (
                 SELECT material_id,MAX(id) AS id FROM analysis_runs GROUP BY material_id
               ) latest ON latest.id=a.id
               LEFT JOIN prepared_documents p
                 ON p.material_id=a.material_id AND p.version=a.prepared_version
               LEFT JOIN documents d ON d.id=p.raw_document_id
               LEFT JOIN sources so ON so.id=d.source_id
               WHERE a.status IN ('irrelevant','no_signal')
               ORDER BY a.id DESC"""
        ).fetchall()
        result = []
        for row in rows:
            item = DashboardApplication._decode_payload(dict(row))
            item = DashboardApplication._decode_payload(item, "prepared_payload")
            result.append(item)
        return result

    @staticmethod
    def _create_digest(store: ProductStore, body: dict) -> dict:
        required = ("id", "period_from", "period_to", "signal_ids")
        if any(not body.get(key) for key in required):
            raise ApiError(400, f"required fields: {', '.join(required)}")
        try:
            return DigestService(store).create_draft(
                str(body["id"]),
                str(body["period_from"]),
                str(body["period_to"]),
                [str(value) for value in body["signal_ids"]],
                actor=str(body.get("actor") or "operator"),
                recipients=list(body.get("recipients") or []),
                title=str(body.get("title") or ""),
                header=str(body.get("header") or ""),
                footer=str(body.get("footer") or ""),
            )
        except (LookupError, ValueError) as exc:
            raise ApiError(400, str(exc)) from exc

    @staticmethod
    def _decode_payload(row: dict, key: str = "payload") -> dict:
        if key in row and row[key]:
            try:
                row[key] = json.loads(row[key])
            except (TypeError, json.JSONDecodeError):
                pass
        return row


def make_handler(app: DashboardApplication, web_root: Path = WEB_ROOT):
    class Handler(BaseHTTPRequestHandler):
        server_version = "GS-Labs-Monitor/0.1"

        def do_GET(self):  # noqa: N802
            self._handle("GET")

        def do_POST(self):  # noqa: N802
            self._handle("POST")

        def do_PUT(self):  # noqa: N802
            self._handle("PUT")

        def _handle(self, method: str):
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                try:
                    body = self._body() if method in {"POST", "PUT"} else {}
                    status, payload = app.dispatch(
                        method, parsed.path, parse_qs(parsed.query), body
                    )
                except ApiError as exc:
                    status, payload = exc.status, {"error": str(exc)}
                except Exception as exc:  # keep error visible without leaking a traceback
                    status, payload = 500, {"error": f"{type(exc).__name__}: {exc}"}
                self._json(status, payload)
                return
            self._static(parsed.path)

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ApiError(400, "invalid Content-Length") from exc
            if length > MAX_REQUEST_BYTES:
                raise ApiError(413, "request body is too large")
            try:
                value = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError as exc:
                raise ApiError(400, "invalid JSON") from exc
            if not isinstance(value, dict):
                raise ApiError(400, "JSON body must be an object")
            return value

        def _json(self, status: int, payload):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _static(self, path: str):
            requested = "index.html" if path in {"", "/"} else unquote(path.lstrip("/"))
            candidate = (web_root / requested).resolve()
            root = web_root.resolve()
            if root not in candidate.parents and candidate != root:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not candidate.is_file():
                candidate = root / "index.html"
            data = candidate.read_bytes()
            self.send_response(200)
            self.send_header(
                "Content-Type",
                mimetypes.guess_type(candidate.name)[0] or "application/octet-stream",
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):  # noqa: A002
            return

    return Handler


def serve(host: str, port: int, db_path: str, web_root: Path = WEB_ROOT):
    Database(db_path).close()
    server = ThreadingHTTPServer(
        (host, port), make_handler(DashboardApplication(db_path), web_root)
    )
    print(f"GS Labs monitor: http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.product.web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=3002, type=int)
    parser.add_argument("--db", default=DEFAULT_PATHS.db_path)
    args = parser.parse_args(argv)
    serve(args.host, args.port, args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
