"""HTTP entry point for the GS Labs pilot MVP."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import replace
from http.server import ThreadingHTTPServer
from urllib.parse import unquote

from src.common import load_env_secret
from src.processing.llm import LlmError
from src.product.contracts import EvidenceClaim, SignalDraft
from src.product.operations import DigestService
from src.product.store import ProductStore
from src.product.web import ApiError, DashboardApplication, make_handler
from src.storage import Database

from .delivery import TelegramBotDeliveryAdapter
from .service import DEFAULT_DB, MVP_ROOT, JobBusyError, PilotService

FRONTEND_ROOT = MVP_ROOT / "frontend"


class PreviewDeliveryAdapter:
    """Demo delivery: records a real attempt without contacting an external system."""

    def send(self, recipient: str, payload: dict) -> str:
        return f"preview://{recipient}/{payload['id']}/v{payload['version']}"


def _signal_from_payload(payload: dict) -> SignalDraft:
    return SignalDraft(
        signal_id=str(payload["signal_id"]),
        material_id=str(payload["material_id"]),
        summary=str(payload.get("summary") or ""),
        claims=tuple(
            EvidenceClaim(str(row.get("text") or ""), str(row.get("evidence_quote") or ""))
            for row in payload.get("claims", [])
            if isinstance(row, dict)
        ),
        relevance=str(payload.get("relevance") or "unknown"),  # type: ignore[arg-type]
        importance=str(payload.get("importance") or "medium"),  # type: ignore[arg-type]
        interest=str(payload.get("interest") or "IRRELEVANT"),  # type: ignore[arg-type]
        impact=str(payload.get("impact") or ""),
        urgency=str(payload.get("urgency") or "routine"),
        confidence=float(payload.get("confidence") or 0),
        unknowns=tuple(str(x) for x in payload.get("unknowns", [])),
        research_questions=tuple(str(x) for x in payload.get("research_questions", [])),
        npa_identifier=payload.get("npa_identifier"),
        npa_stage=payload.get("npa_stage"),
        npa_version=payload.get("npa_version"),
        npa_effective_from=payload.get("npa_effective_from"),
        npa_change_summary=payload.get("npa_change_summary"),
        reasoning=str(payload.get("reasoning") or ""),
        kind=str(payload.get("kind") or "news"),  # type: ignore[arg-type]
        recipient_roles=tuple(payload.get("recipient_roles") or ()),  # type: ignore[arg-type]
        source_title=str(payload.get("source_title") or ""),
    )


class PilotApplication(DashboardApplication):
    def __init__(self, service: PilotService):
        super().__init__(service.db_path)
        self.service = service

    def _delivery_adapters(self) -> dict:
        return {
            "preview": PreviewDeliveryAdapter(),
            "telegram": TelegramBotDeliveryAdapter(
                load_env_secret("TELEGRAM_BOT_TOKEN", self.service.paths.env_path),
                load_env_secret("TELEGRAM_CHAT_ID", self.service.paths.env_path),
            ),
        }

    def dispatch(self, method: str, path: str, query: dict[str, list[str]], body: dict):
        if method == "GET" and path == "/api/pilot/status":
            return 200, self.service.status()
        if method == "POST" and path == "/api/pilot/jobs":
            try:
                return 202, self.service.start_job(
                    str(body.get("kind") or "cycle"),
                    limit=max(1, min(200, int(body.get("limit") or 30))),
                    backfill=bool(body.get("backfill", False)),
                )
            except JobBusyError as exc:
                raise ApiError(409, str(exc)) from exc
        if method == "POST" and path == "/api/demo/reset-digest":
            db = Database(self.service.db_path)
            try:
                return 200, DigestService(ProductStore(db.conn)).reset_demo(
                    actor=str(body.get("actor") or "demo-reset")
                )
            finally:
                db.close()
        match = re.fullmatch(r"/api/digests/([^/]+)/(\d+)/deliver", path)
        if method == "POST" and match:
            digest_id, version = unquote(match.group(1)), int(match.group(2))
            db = Database(self.service.db_path)
            try:
                delivery = DigestService(ProductStore(db.conn)).deliver(
                    digest_id,
                    version,
                    self._delivery_adapters(),
                )
                delivered = all(
                    row["status"] in {"delivered", "already_delivered"} for row in delivery
                )
                return 200, {
                    "status": "delivered" if delivered else "delivery_failed",
                    "delivery": delivery,
                }
            except (LookupError, ValueError) as exc:
                raise ApiError(400, str(exc)) from exc
            finally:
                db.close()
        match = re.fullmatch(r"/api/digests/([^/]+)/(\d+)/ready", path)
        if method == "POST" and match:
            digest_id, version = unquote(match.group(1)), int(match.group(2))
            role = str(body.get("role") or "").upper()
            actor = str(body.get("actor") or f"demo-{role.lower()}")
            db = Database(self.service.db_path)
            try:
                service = DigestService(ProductStore(db.conn))
                result = service.mark_ready(
                    digest_id,
                    version,
                    role=role,
                    actor=actor,
                )
                if result["all_ready"]:
                    service.approve(digest_id, version, actor="PR + GR")
                    result["delivery"] = service.deliver(
                        digest_id,
                        version,
                        self._delivery_adapters(),
                    )
                    result["status"] = (
                        "delivered"
                        if all(
                            row["status"] in {"delivered", "already_delivered"}
                            for row in result["delivery"]
                        )
                        else "delivery_failed"
                    )
                else:
                    result["status"] = "draft"
                return 200, result
            except (LookupError, ValueError) as exc:
                raise ApiError(400, str(exc)) from exc
            finally:
                db.close()
        match = re.fullmatch(r"/api/digests/([^/]+)/(\d+)/(generate-role|role-content)", path)
        if method == "POST" and match:
            digest_id, version, action = unquote(match.group(1)), int(match.group(2)), match.group(3)
            role = str(body.get("role") or "").upper()
            actor = str(body.get("actor") or f"demo-{role.lower()}")
            signal_ids = [str(value) for value in body.get("signal_ids") or []]
            db = Database(self.service.db_path)
            try:
                digest_service = DigestService(ProductStore(db.conn))
                items = digest_service.role_items(role, signal_ids)
                if action == "generate-role":
                    generated = self.service.compose_digest_block(role, items)
                    text = generated["text"]
                    model = generated["model"]
                else:
                    text = str(body.get("text") or "").strip()
                    model = "human-edit"
                payload = digest_service.update_role_content(
                    digest_id,
                    version,
                    role=role,
                    signal_ids=signal_ids,
                    text=text,
                    actor=actor,
                    model=model,
                )
                return 200, {"status": "generated", "digest": payload}
            except (LookupError, ValueError) as exc:
                raise ApiError(400, str(exc)) from exc
            except LlmError as exc:
                raise ApiError(503, f"AI-сборка временно недоступна: {exc}") from exc
            finally:
                db.close()
        if method == "POST" and path == "/api/sources/preview":
            try:
                return 200, self.service.preview_source(body)
            except (LookupError, ValueError) as exc:
                raise ApiError(400, str(exc)) from exc
        if method == "POST" and path == "/api/sources":
            try:
                return 201, self.service.add_source(body)
            except (LookupError, ValueError) as exc:
                raise ApiError(400, str(exc)) from exc
        match = re.fullmatch(r"/api/sources/(\d+)/(enable|disable|decommission)", path)
        if method == "POST" and match:
            try:
                return 200, self.service.update_source(int(match.group(1)), match.group(2))
            except LookupError as exc:
                raise ApiError(404, str(exc)) from exc
        if method == "POST" and path == "/api/materials/import":
            try:
                result = self.service.import_material(body)
                # The document is durably pending in the common input bank.  A
                # busy worker does not create a separate in-memory queue, so do
                # not claim that it has been scheduled more precisely than that.
                result["analysis"] = "pending"
                if result["created"]:
                    try:
                        self.service.start_job(
                            "process",
                            limit=1,
                            preferred_document_id=int(result["document_id"]),
                        )
                        result["analysis"] = "started"
                    except JobBusyError:
                        pass
                return 201, result
            except ValueError as exc:
                raise ApiError(400, str(exc)) from exc
        match = re.fullmatch(r"/api/signals/([^/]+)/revise", path)
        if method == "POST" and match:
            return 201, self._revise_signal(unquote(match.group(1)), body)
        if method == "GET" and path == "/api/materials":
            return 200, self._materials(query)
        return super().dispatch(method, path, query, body)

    def _revise_signal(self, signal_id: str, body: dict) -> dict:
        editable = {"summary", "impact", "importance", "interest", "urgency", "kind"}
        updates = {key: body[key] for key in editable if key in body}
        if not updates:
            raise ApiError(400, "no editable fields supplied")
        if "summary" in updates and not str(updates["summary"]).strip():
            raise ApiError(400, "summary must not be empty")
        allowed = {
            "importance": {"low", "medium", "high", "critical"},
            "interest": {"PR", "GR", "BOTH"},
            "urgency": {"routine", "urgent"},
            "kind": {"news", "npa", "npa_candidate"},
        }
        for field, values in allowed.items():
            if field in updates and updates[field] not in values:
                raise ApiError(400, f"invalid {field}")
        db = Database(self.db_path)
        try:
            row = db.conn.execute(
                "SELECT revision,payload FROM signal_revisions WHERE signal_id=? ORDER BY revision DESC LIMIT 1",
                (signal_id,),
            ).fetchone()
            if row is None:
                raise ApiError(404, "signal not found")
            if "revision" in body and int(body["revision"]) != int(row["revision"]):
                raise ApiError(409, "signal has a newer revision")
            original = _signal_from_payload(json.loads(row["payload"]))
            revised = replace(original, **updates)
            revision = ProductStore(db.conn).save_signal(
                revised,
                actor=str(body.get("actor") or "pilot-operator"),
                reason=str(body.get("reason") or "human edit"),
            )
            return {"status": "created", "signal_id": signal_id, "revision": revision}
        finally:
            db.close()

    def _materials(self, query: dict[str, list[str]]) -> dict:
        try:
            limit = max(1, min(300, int(self._one(query, "limit") or 100)))
        except ValueError as exc:
            raise ApiError(400, "limit must be an integer") from exc
        db = Database(self.db_path)
        try:
            rows = db.conn.execute(
                """SELECT d.id,d.title,d.url,d.summary,d.published_at,d.fetched_at,
                          length(d.text) AS text_length,s.name AS source_name,s.kind,
                          s.direction,s.category,
                          (SELECT status FROM analysis_runs a
                           JOIN prepared_documents p ON p.material_id=a.material_id
                           WHERE p.raw_document_id=d.id ORDER BY a.id DESC LIMIT 1) AS analysis_status
                   FROM documents d JOIN sources s ON s.id=d.source_id
                   WHERE d.hidden=0 ORDER BY COALESCE(d.published_at,d.fetched_at) DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return {"items": [dict(row) for row in rows]}
        finally:
            db.close()


def serve(
    host: str,
    port: int,
    db_path: str,
    *,
    live: bool = False,
    interval: int = 600,
    news_active_days: int = 30,
) -> None:
    service = PilotService(db_path, news_active_days=news_active_days)
    if live:
        service.start_watch(interval_seconds=max(60, interval))
    server = ThreadingHTTPServer(
        (host, port), make_handler(PilotApplication(service), FRONTEND_ROOT)
    )
    print(f"GS Labs Radar: http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop_watch()
        server.server_close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m MVP.backend.app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3002)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--interval", type=int, default=600)
    parser.add_argument(
        "--news-active-days",
        type=int,
        default=30,
        help="days since the last meaningful update before logical news archiving",
    )
    args = parser.parse_args(argv)
    serve(
        args.host,
        args.port,
        args.db,
        live=args.live,
        interval=args.interval,
        news_active_days=args.news_active_days,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
