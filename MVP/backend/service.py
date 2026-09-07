"""Pilot orchestration over the production collection and product core.

The module contains no benchmark shortcuts and no relevance heuristics.  It
only composes the already tested source adapters, AI contracts, append-only
store and the architecture branches selected by B1--B4.
"""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from src.common import load_env_secret, sha256_text
from src.config import Config
from src.models import CollectReport, RawDocument, Source
from src.paths import ProjectPaths
from src.processing.llm import build_embedding_provider, build_llm_provider
from src.product.analysis import PrimaryAnalyzer
from src.product.contracts import GsLabsContext
from src.product.events import EventLinker
from src.product.npa import NpaResolver
from src.product.preparation import prepare_raw_document
from src.product.providers import TavilyResearchSearch
from src.product.research import TargetedResearcher
from src.product.runtime import ProductAgentRuntime
from src.product.store import ProductStore
from src.product.workflow import BranchConfiguration
from src.sources.base import HostLimiter, make_client
from src.sources.collector import Collector
from src.sources.resolver import Resolver
from src.storage import Database, DuplicateSourceError

REPO_ROOT = Path(__file__).resolve().parents[2]
MVP_ROOT = REPO_ROOT / "MVP"
DEFAULT_DB = MVP_ROOT / "data" / "pilot.db"
DEFAULT_CONTEXT = MVP_ROOT / "context.json"
SELECTED_BRANCHES = BranchConfiguration(
    a1="one_pass",
    a2="targeted_research",
    a3="embedding_top20",
    a4="without_critic",
)
DEFAULT_NEWS_ACTIVE_DAYS = 30
ORDINARY_COLLECTION_SECONDS = 600
REGULATOR_COLLECTION_SECONDS = 1800
OFFICIAL_REGULATOR_HOSTS = (
    "publication.pravo.gov.ru",
    "government.ru",
    "rkn.gov.ru",
    "duma.gov.ru",
    "nalog.gov.ru",
    "fsb.ru",
    "rfrit.ru",
    "cbr.ru",
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class JobBusyError(RuntimeError):
    pass


class PilotService:
    """Thread-safe job facade used by the HTTP layer and scheduled worker."""

    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB,
        *,
        repo_root: str | Path = REPO_ROOT,
        context_path: str | Path = DEFAULT_CONTEXT,
        news_active_days: int = DEFAULT_NEWS_ACTIVE_DAYS,
    ) -> None:
        if news_active_days < 1:
            raise ValueError("news_active_days must be >= 1")
        self.repo_root = Path(repo_root).resolve()
        self.db_path = str(Path(db_path).resolve())
        self.context_path = Path(context_path).resolve()
        self.news_active_days = news_active_days
        self.paths = ProjectPaths.from_root(str(self.repo_root))
        self.config = Config.load(self.paths.config_path)
        self._lock = threading.Lock()
        self._job: dict[str, Any] = {
            "id": None,
            "kind": None,
            "state": "idle",
            "started_at": None,
            "finished_at": None,
            "step": None,
            "progress": 0,
            "total": 0,
            "result": None,
            "error": None,
        }
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._watch_interval_seconds: int | None = None
        self._next_watch_at: datetime | None = None
        self.initialize()

    # -- bootstrap ---------------------------------------------------------

    def initialize(self) -> dict[str, Any]:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        db = Database(self.db_path)
        try:
            context = self.load_context()
            ProductStore(db.conn).ensure_context(context.version, context, actor="pilot-bootstrap")
            seeded = self._seed_sources(db)
            return {"context": context.version, "sources_seeded": seeded}
        finally:
            db.close()

    def load_context(self) -> GsLabsContext:
        payload = json.loads(self.context_path.read_text(encoding="utf-8"))
        return GsLabsContext.from_dict(payload)

    def compose_digest_block(self, role: str, items: list[dict[str, Any]]) -> dict[str, str]:
        """Turn confirmed role-owned signals into concise copy; URLs stay code-controlled."""
        if role not in {"PR", "GR"}:
            raise ValueError("role must be PR or GR")
        if not items:
            raise ValueError("at least one signal is required")
        api_key = load_env_secret(self.config.llm.api_key_env, self.paths.env_path)
        provider = build_llm_provider(self.config.llm, api_key)
        source_rows = [
            {
                "signal_id": item["signal_id"],
                "summary": item["signal"].get("summary", ""),
                "impact": item["signal"].get("impact", ""),
                "source_title": item["signal"].get("source_title", ""),
            }
            for item in items
        ]
        schema = {
            "type": "object",
            "properties": {
                "entries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "signal_id": {"type": "string"},
                            "text": {"type": "string"},
                        },
                        "required": ["signal_id", "text"],
                    },
                }
            },
            "required": ["entries"],
        }
        completion = provider.complete(
            "Подготовь элементы блока дайджеста. Не добавляй новых фактов. "
            "Каждый элемент перепиши ясно и компактно: событие, затем значение для GS Labs. "
            "Сохрани signal_id и верни по одному элементу на каждый вход.\n\n"
            + json.dumps(source_rows, ensure_ascii=False),
            schema,
            system=f"Ты редактор {role}-блока информационного дайджеста GS Labs.",
        )
        generated = {
            str(entry.get("signal_id")): str(entry.get("text") or "").strip()
            for entry in completion.data.get("entries", [])
            if isinstance(entry, dict)
        }
        blocks = []
        for item in items:
            signal = item["signal"]
            copy = generated.get(item["signal_id"]) or str(signal.get("summary") or "").strip()
            source_url = str(item.get("source_url") or "").strip()
            source_title = str(signal.get("source_title") or "Источник").strip()
            source_line = f"[Источник: {source_title}]({source_url})" if source_url else f"Источник: {source_title}"
            blocks.append(f"{copy}\n{source_line}")
        return {"text": "\n\n".join(blocks), "model": completion.model}

    def _seed_sources(self, db: Database) -> int:
        payload = json.loads(Path(self.paths.sources_path).read_text(encoding="utf-8"))
        added = 0
        for raw in payload.get("sources", []):
            fetch_url = str(raw.get("fetch_url") or raw.get("url") or "").strip()
            if not fetch_url:
                continue
            url = str(raw.get("url") or fetch_url)
            category = str(raw.get("category") or "media")
            host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
            is_official_regulator = category == "regulator" and any(
                host == official or host.endswith("." + official)
                for official in OFFICIAL_REGULATOR_HOSTS
            )
            source = Source(
                name=str(raw.get("name") or raw.get("url") or fetch_url)[:80],
                url=url,
                kind=str(raw.get("kind") or "html"),
                category=category,
                fetch_url=fetch_url,
                enabled=raw.get("enabled") is not False,
                notes=str(raw.get("notes") or ""),
                direction=str(
                    raw.get("direction") or ("gr" if category == "regulator" else "both")
                ),
                source_class=str(
                    raw.get("source_class")
                    or ("regulator" if is_official_regulator else "ordinary")
                ),
            )
            existing = db.sources.get_by_fetch_url(fetch_url)
            if existing is None:
                db.sources.add(source)
                added += 1
                continue

            # The pilot database can predate the source-policy fields. Reconcile
            # authoritative seed metadata without changing an operator's pause
            # or decommission decision and without deleting collected history.
            desired_direction = source.direction
            desired_class = source.source_class
            if (
                existing.direction != desired_direction
                or existing.source_class != desired_class
                or existing.category != category
            ):
                existing.direction = desired_direction
                existing.source_class = desired_class
                existing.category = category
                db.sources.update(existing)
        return added

    # -- state -------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            job = json.loads(json.dumps(self._job, ensure_ascii=False))
            watching = bool(self._watch_thread and self._watch_thread.is_alive())
            watch_interval = self._watch_interval_seconds
            next_watch_at = self._next_watch_at
        db = Database(self.db_path)
        try:
            latest = db.runs.latest()
            coverage = db.conn.execute(
                """SELECT MIN(published_at),MAX(published_at),COUNT(*),
                          COUNT(DISTINCT source_id) FROM documents"""
            ).fetchone()
            unprocessed = db.conn.execute(
                """SELECT COUNT(*) FROM (
                     SELECT d.id,COALESCE(MAX(r.revision),1) AS current_revision
                     FROM documents d
                     LEFT JOIN document_revisions r ON r.document_id=d.id
                     WHERE d.hidden=0 GROUP BY d.id
                   ) current
                   WHERE NOT EXISTS(
                           SELECT 1 FROM analysis_runs a
                           WHERE a.material_id=(
                             'raw-' || current.id || '-rev-' || current.current_revision
                           ) AND a.configuration_id=?
                         )
                      OR COALESCE((
                           SELECT event_type FROM audit_events
                           WHERE object_type='material'
                             AND object_id=(
                               'raw-' || current.id || '-rev-' || current.current_revision
                             )
                             AND event_type IN ('workflow.completed','workflow.failed')
                           ORDER BY id DESC LIMIT 1
                         ),'')='workflow.failed'""",
                (SELECTED_BRANCHES.id,),
            ).fetchone()[0]
            provider_keys = {
                "glm": bool(load_env_secret(self.config.llm.api_key_env, self.paths.env_path)),
                "embeddings": bool(
                    load_env_secret(self.config.embeddings.api_key_env, self.paths.env_path)
                ),
                "tavily": bool(
                    load_env_secret(self.config.tavily.api_key_env, self.paths.env_path)
                ),
            }
            now = datetime.now(UTC)
            return {
                "job": job,
                "watching": watching,
                "selected_architecture": SELECTED_BRANCHES.id,
                "model": self.config.llm.model,
                "embedding_model": self.config.embeddings.model,
                "retention": {
                    "news_active_days": self.news_active_days,
                    "archive_mode": "logical",
                    "physical_deletion": False,
                    "npa_uses_news_window": False,
                },
                "collection_policy": {
                    "ordinary_seconds": ORDINARY_COLLECTION_SECONDS,
                    "regulator_seconds": REGULATOR_COLLECTION_SECONDS,
                    "continuation_has_priority": True,
                },
                "collection_schedule": self._collection_schedule(
                    db,
                    now=now,
                    watching=watching,
                    watch_interval_seconds=watch_interval,
                    next_watch_at=next_watch_at,
                    collecting=job.get("state") == "running"
                    and job.get("step") == "collecting",
                ),
                "providers_configured": provider_keys,
                "latest_collection": dict(latest) if latest else None,
                "data": {
                    "documents": int(coverage[2]),
                    "sources_with_documents": int(coverage[3]),
                    "from": coverage[0],
                    "to": coverage[1],
                    "awaiting_ai": int(unprocessed),
                },
            }
        finally:
            db.close()

    def start_job(
        self,
        kind: str,
        *,
        limit: int = 30,
        backfill: bool = False,
        preferred_document_id: int | None = None,
    ) -> dict:
        if kind not in {"collect", "process", "cycle", "maintenance"}:
            raise ValueError("unknown job kind")
        with self._lock:
            if self._job["state"] == "running":
                raise JobBusyError("another pilot job is already running")
            job_id = f"{kind}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
            self._job = {
                "id": job_id,
                "kind": kind,
                "state": "running",
                "started_at": _now(),
                "finished_at": None,
                "step": "starting",
                "progress": 0,
                "total": 0,
                "result": None,
                "error": None,
            }
        thread = threading.Thread(
            target=self._execute_job,
            args=(kind,),
            kwargs={
                "limit": limit,
                "backfill": backfill,
                "preferred_document_id": preferred_document_id,
            },
            daemon=True,
            name=job_id,
        )
        thread.start()
        return self.status()["job"]

    def _execute_job(
        self,
        kind: str,
        *,
        limit: int,
        backfill: bool,
        preferred_document_id: int | None = None,
    ) -> None:
        try:
            result: dict[str, Any] = {}
            collected_document_ids: list[int] | None = None
            if kind in {"collect", "cycle"}:
                self._set_job(step="collecting")
                result["collection"] = self.collect(
                    backfill=backfill,
                    due_only=kind == "cycle",
                )
                if kind == "cycle":
                    collected_document_ids = self._documents_fetched_since(
                        result["collection"]["started_at"],
                        limit=limit,
                    )
            if kind in {"process", "cycle"}:
                self._set_job(step="analysing")
                if kind == "cycle" and not collected_document_ids:
                    result["processing"] = {
                        "configuration_id": SELECTED_BRANCHES.id,
                        "selection": "new_documents_from_current_collection",
                        "processed": 0,
                        "skipped": 0,
                        "failed": 0,
                        "documents": [],
                    }
                else:
                    result["processing"] = self.process(
                        limit=min(limit, len(collected_document_ids))
                        if collected_document_ids is not None
                        else limit,
                        preferred_document_id=preferred_document_id,
                        preferred_document_ids=collected_document_ids,
                    )
            if kind in {"maintenance", "cycle"}:
                self._set_job(step="maintenance")
                result["maintenance"] = self.maintain_events(due_only=kind == "cycle")
            self._set_job(state="completed", step="completed", finished_at=_now(), result=result)
        except Exception as exc:  # the error is part of the visible pilot state
            self._set_job(
                state="failed",
                step="failed",
                finished_at=_now(),
                error=f"{type(exc).__name__}: {exc}",
            )

    def _set_job(self, **values: Any) -> None:
        with self._lock:
            self._job.update(values)

    # -- live pipeline -----------------------------------------------------

    def collect(self, *, backfill: bool = False, due_only: bool = False) -> dict[str, Any]:
        db = Database(self.db_path)
        try:
            source_ids = self._due_source_ids(db) if due_only else None
            if due_only and not source_ids:
                now = _now()
                return asdict(CollectReport(started_at=now, finished_at=now))
            collector = Collector(self.config, self.paths, db)
            report = collector.run(source_ids=source_ids, backfill=backfill)
            return asdict(report)
        finally:
            db.close()

    def _documents_fetched_since(self, started_at: str, *, limit: int) -> list[int]:
        """Return documents created by the current collection, newest first."""
        db = Database(self.db_path)
        try:
            rows = db.conn.execute(
                """SELECT id FROM documents
                   WHERE hidden=0 AND fetched_at>=?
                   ORDER BY fetched_at DESC,id DESC LIMIT ?""",
                (started_at, max(1, limit)),
            ).fetchall()
            return [int(row["id"]) for row in rows]
        finally:
            db.close()

    @staticmethod
    def _due_source_ids(db: Database, *, now: datetime | None = None) -> list[int]:
        """Return sources due under the 10/30-minute pilot polling policy."""
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        rows = db.conn.execute(
            """SELECT s.id,s.category,s.kind,s.source_class,f.last_fetch_at,
                      COALESCE(f.backlog_status,'clear') AS backlog_status
               FROM sources s LEFT JOIN fetch_state f ON f.source_id=s.id
               WHERE s.enabled=1 AND s.status!='decommissioned'
               ORDER BY s.id"""
        ).fetchall()
        due = []
        for row in rows:
            if row["backlog_status"] != "clear" or not row["last_fetch_at"]:
                due.append(int(row["id"]))
                continue
            try:
                last = datetime.fromisoformat(str(row["last_fetch_at"]).replace("Z", "+00:00"))
            except ValueError:
                due.append(int(row["id"]))
                continue
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            interval = PilotService._source_interval_seconds(row)
            if (instant - last.astimezone(UTC)).total_seconds() >= interval:
                due.append(int(row["id"]))
        return due

    @staticmethod
    def _source_interval_seconds(row: Any) -> int:
        regulatory = (
            row["category"] == "regulator"
            or row["source_class"] == "regulator"
            or row["kind"] == "search"
        )
        return REGULATOR_COLLECTION_SECONDS if regulatory else ORDINARY_COLLECTION_SECONDS

    @classmethod
    def _collection_schedule(
        cls,
        db: Database,
        *,
        now: datetime,
        watching: bool,
        watch_interval_seconds: int | None,
        next_watch_at: datetime | None,
        collecting: bool,
    ) -> list[dict[str, Any]]:
        """Describe the next real scheduled attempt for every enabled collection method."""
        rows = db.conn.execute(
            """SELECT s.id,s.kind,s.category,s.source_class,f.last_fetch_at,
                      f.last_success_at,COALESCE(f.backlog_status,'clear') AS backlog_status
               FROM sources s LEFT JOIN fetch_state f ON f.source_id=s.id
               WHERE s.enabled=1 AND s.status!='decommissioned'
               ORDER BY s.kind,s.id"""
        ).fetchall()
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            interval = cls._source_interval_seconds(row)
            due_at = now
            if row["backlog_status"] == "clear" and row["last_fetch_at"]:
                try:
                    fetched_at = datetime.fromisoformat(
                        str(row["last_fetch_at"]).replace("Z", "+00:00")
                    )
                    if fetched_at.tzinfo is None:
                        fetched_at = fetched_at.replace(tzinfo=UTC)
                    due_at = fetched_at.astimezone(UTC) + timedelta(seconds=interval)
                except ValueError:
                    pass
            groups[str(row["kind"] or "other")].append(
                {
                    "due_at": due_at,
                    "interval": interval,
                    "last_success_at": row["last_success_at"],
                }
            )

        result = []
        for kind, sources in groups.items():
            source_due_at = min(item["due_at"] for item in sources)
            due_count = sum(item["due_at"] <= now for item in sources)
            scheduled_at: datetime | None = None
            if watching and next_watch_at is not None and watch_interval_seconds:
                scheduled_at = next_watch_at
                while scheduled_at < source_due_at:
                    scheduled_at += timedelta(seconds=watch_interval_seconds)
            last_successes = [item["last_success_at"] for item in sources if item["last_success_at"]]
            result.append(
                {
                    "kind": kind,
                    "source_count": len(sources),
                    "due_count": due_count,
                    "state": "running"
                    if collecting and due_count
                    else "scheduled"
                    if watching
                    else "manual",
                    "next_run_at": scheduled_at.isoformat(timespec="seconds")
                    if scheduled_at
                    else None,
                    "last_success_at": max(last_successes) if last_successes else None,
                    "intervals_seconds": sorted({item["interval"] for item in sources}),
                }
            )
        return sorted(
            result,
            key=lambda item: (
                item["next_run_at"] is None,
                item["next_run_at"] or "",
                item["kind"],
            ),
        )

    def process(
        self,
        *,
        limit: int = 30,
        preferred_document_id: int | None = None,
        preferred_document_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        context = self.load_context()
        llm = build_llm_provider(
            self.config.llm,
            load_env_secret(self.config.llm.api_key_env, self.paths.env_path),
        )
        embedder = build_embedding_provider(
            self.config.embeddings,
            load_env_secret(self.config.embeddings.api_key_env, self.paths.env_path),
        )
        search = TavilyResearchSearch(
            self.config.tavily,
            load_env_secret(self.config.tavily.api_key_env, self.paths.env_path),
        )
        db = Database(self.db_path)
        summary: dict[str, Any] = {
            "configuration_id": SELECTED_BRANCHES.id,
            "selection": "new_documents_from_current_collection"
            if preferred_document_ids
            else "preferred_manual_document"
            if preferred_document_id is not None
            else "regulator_first_round_robin_by_source",
            "processed": 0,
            "skipped": 0,
            "failed": 0,
            "documents": [],
        }
        try:
            store = ProductStore(db.conn)
            store.ensure_context(context.version, context, actor="pilot-runtime")
            runtime = ProductAgentRuntime(
                PrimaryAnalyzer(
                    llm,
                    model=self.config.llm.model,
                    max_chars=self.config.processing.max_chars,
                ),
                EventLinker(llm, embedder, model=self.config.llm.model),
                NpaResolver(
                    llm,
                    model=self.config.llm.model,
                    embedder=embedder,
                    candidate_limit=20,
                ),
                store,
                researcher=TargetedResearcher(llm, search, self.config.llm.model),
                analysis_concurrency=self.config.processing.concurrency,
            )
            pending, skipped = self._select_pending_documents(
                db,
                store,
                limit=max(1, limit),
                preferred_document_id=preferred_document_id,
                preferred_document_ids=preferred_document_ids,
            )
            summary["skipped"] = skipped
            self._set_job(total=len(pending), progress=0)
            for index, (row, material_id) in enumerate(pending, start=1):
                raw = db.documents.get(int(row["id"]))
                source = db.sources.get(int(row["source_id"]))
                if raw is None or source is None:
                    summary["failed"] += 1
                    continue
                prepared = prepare_raw_document(
                    material_id,
                    raw,
                    source,
                    max_chunk_chars=self.config.processing.max_chars,
                )
                try:
                    result = runtime.run(
                        [prepared],
                        context,
                        SELECTED_BRANCHES,
                        initial_state=self._initial_state(store),
                        raw_document_ids={material_id: int(row["id"])},
                    )
                except Exception as exc:
                    summary["failed"] += 1
                    store.audit(
                        "workflow.failed",
                        "material",
                        material_id,
                        "pilot-runtime",
                        {"error_type": type(exc).__name__, "error": str(exc)},
                    )
                    summary["documents"].append(
                        {"material_id": material_id, "status": "failed", "error": str(exc)}
                    )
                else:
                    summary["processed"] += 1
                    store.audit(
                        "workflow.completed",
                        "material",
                        material_id,
                        "pilot-runtime",
                        {
                            "status": result.analyses[0].status,
                            "signals": len(result.analyses[0].signals),
                            "objects": len(result.draft_digest.objects),
                        },
                    )
                    summary["documents"].append(
                        {
                            "material_id": material_id,
                            "status": result.analyses[0].status,
                            "signals": len(result.analyses[0].signals),
                            "objects": len(result.draft_digest.objects),
                            "model_calls": result.telemetry.model_calls,
                        }
                    )
                self._set_job(progress=index)
            return summary
        finally:
            search.close()
            if hasattr(embedder, "close"):
                embedder.close()
            llm.close()
            db.close()

    @staticmethod
    def _select_pending_documents(
        db: Database,
        store: ProductStore,
        *,
        limit: int,
        preferred_document_id: int | None = None,
        preferred_document_ids: list[int] | None = None,
    ) -> tuple[list[tuple[Any, str]], int]:
        """Fairly sample the live backlog without guessing business relevance.

        A global newest-first query lets one high-volume feed occupy the whole
        AI batch. Official regulatory sources form the first risk lane, then
        round-robin gives every source one place before taking a second item.
        Source class is an explicit operator policy, not inferred relevance.
        Within a source, the newest material still goes first.
        """
        rows = db.conn.execute(
            """SELECT d.id,d.source_id,s.source_class,
                      COALESCE(MAX(r.revision),1) AS current_revision
               FROM documents d
               JOIN sources s ON s.id=d.source_id
               LEFT JOIN document_revisions r ON r.document_id=d.id
               WHERE d.hidden=0
               GROUP BY d.id
               ORDER BY d.source_id,COALESCE(d.published_at,d.fetched_at) DESC,d.id DESC"""
        ).fetchall()
        buckets: dict[int, deque[tuple[Any, str]]] = defaultdict(deque)
        skipped = 0
        for row in rows:
            material_id = f"raw-{row['id']}-rev-{row['current_revision']}"
            latest_workflow = db.conn.execute(
                """SELECT event_type FROM audit_events
                   WHERE object_type='material' AND object_id=?
                     AND event_type IN ('workflow.completed','workflow.failed')
                   ORDER BY id DESC LIMIT 1""",
                (material_id,),
            ).fetchone()
            failed_after_analysis = (
                latest_workflow and latest_workflow["event_type"] == "workflow.failed"
            )
            if store.has_analysis(material_id, SELECTED_BRANCHES.id) and not failed_after_analysis:
                skipped += 1
                continue
            buckets[int(row["source_id"])].append((row, material_id))

        selected: list[tuple[Any, str]] = []
        preferred_ids = list(preferred_document_ids or [])
        if preferred_document_id is not None:
            preferred_ids.insert(0, preferred_document_id)
        for document_id in dict.fromkeys(preferred_ids):
            if len(selected) >= limit:
                break
            for bucket in buckets.values():
                preferred = next(
                    (item for item in bucket if int(item[0]["id"]) == document_id),
                    None,
                )
                if preferred is not None:
                    bucket.remove(preferred)
                    selected.append(preferred)
                    break
        source_class = {
            int(row["source_id"]): str(row["source_class"] or "ordinary") for row in rows
        }
        source_ids = sorted(
            buckets,
            key=lambda source_id: (
                0 if source_class.get(source_id) == "regulator" else 1,
                source_id,
            ),
        )
        while source_ids and len(selected) < limit:
            next_round: list[int] = []
            for source_id in source_ids:
                if len(selected) >= limit:
                    break
                bucket = buckets[source_id]
                if bucket:
                    selected.append(bucket.popleft())
                if bucket:
                    next_round.append(source_id)
            source_ids = next_round
        return selected, skipped

    @staticmethod
    def _initial_state(store: ProductStore) -> dict[str, Any]:
        events = [
            {
                "object_id": event.id,
                "title": event.title,
                "summary": event.summary,
                "version": event.version,
                "signal_ids": event.signal_ids,
                "material_ids": event.material_ids,
                "compact_text": event.compact_text,
                "embedding": event.embedding,
                "lifecycle_state": event.lifecycle_state,
                "first_published_at": event.first_published_at,
                "last_published_at": event.last_published_at,
                "first_seen_at": event.first_seen_at,
                "last_seen_at": event.last_seen_at,
                "last_meaningful_update_at": event.last_meaningful_update_at,
                "archived_at": event.archived_at,
            }
            for event in store.load_events()
        ]
        rows = store.conn.execute(
            """SELECT n.id,n.official_identifier,n.tracked,v.stage,v.version,
                      v.payload,v.effective_at
               FROM npa_records n LEFT JOIN npa_versions v ON v.id=(
                 SELECT id FROM npa_versions WHERE npa_id=n.id ORDER BY version DESC LIMIT 1
               ) WHERE n.tracked=1"""
        ).fetchall()
        npas = []
        for row in rows:
            payload = json.loads(row["payload"] or "{}")
            npas.append(
                {
                    "object_id": row["id"],
                    "external_id": row["official_identifier"],
                    "title": payload.get("title") or row["official_identifier"],
                    "summary": payload.get("summary") or payload.get("change_summary") or "",
                    "current_stage": row["stage"] or "unknown",
                    "current_version": str(row["version"] or "unknown"),
                    "change_summary": payload.get("change_summary") or "",
                    "effective_from": row["effective_at"],
                }
            )
        return {"known_events": events, "tracked_npas": npas}

    def maintain_events(self, *, due_only: bool = True) -> dict[str, Any]:
        """Apply the configurable news lifecycle once per calendar month."""
        now = datetime.now(UTC)
        month = now.strftime("%Y-%m")
        db = Database(self.db_path)
        try:
            store = ProductStore(db.conn)
            previous = db.conn.execute(
                """SELECT 1 FROM audit_events
                   WHERE event_type='maintenance.news_retention.completed'
                     AND json_extract(payload,'$.month')=? LIMIT 1""",
                (month,),
            ).fetchone()
            if due_only and previous:
                return {
                    "status": "not_due",
                    "month": month,
                    "active_days": self.news_active_days,
                }
            result = store.archive_due_events(
                as_of=now.isoformat(timespec="seconds"),
                active_days=self.news_active_days,
                actor="pilot-maintenance",
            )
            store.audit(
                "maintenance.news_retention.completed",
                "system",
                month,
                "pilot-maintenance",
                {
                    "month": month,
                    "active_days": self.news_active_days,
                    "archived": len(result["archived"]),
                    "blocked": len(result["blocked"]),
                },
            )
            return {"status": "completed", "month": month, **result}
        finally:
            db.close()

    # -- source operations -------------------------------------------------

    def _source_candidate(self, body: dict[str, Any]) -> Source:
        url = str(body.get("url") or "").strip()
        if not url:
            raise ValueError("url is required")
        pinned_kind = str(body.get("kind") or "").strip()
        fetch_url = str(body.get("fetch_url") or "").strip()
        if pinned_kind and fetch_url:
            resolved_kind, resolved_url, detected_name, note = (
                pinned_kind,
                fetch_url,
                "",
                "",
            )
        else:
            with make_client(self.config) as client:
                resolution = Resolver(
                    client, HostLimiter(self.config.scraper.per_host_concurrency)
                ).resolve(url)
            resolved_kind = pinned_kind or resolution.kind
            resolved_url = fetch_url or resolution.fetch_url
            detected_name = resolution.name
            note = resolution.note
        category = str(body.get("category") or "media").lower()
        if resolved_kind == "telegram" and "category" not in body:
            category = "telegram"
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        is_official_regulator = category == "regulator" and any(
            host == official or host.endswith("." + official)
            for official in OFFICIAL_REGULATOR_HOSTS
        )
        return Source(
            name=str(body.get("name") or detected_name or url)[:80],
            url=url,
            kind=resolved_kind,
            category=category,
            fetch_url=resolved_url,
            notes=str(body.get("notes") or note or ""),
            direction=str(body.get("direction") or ("gr" if category == "regulator" else "both")),
            source_class="regulator" if is_official_regulator else "ordinary",
        )

    def preview_source(self, body: dict[str, Any]) -> dict[str, Any]:
        """Resolve and sample a source without changing the source registry."""
        source = self._source_candidate(body)
        db = Database(self.db_path)
        try:
            result, latency_ms = Collector(self.config, self.paths, db).preview_source(source)
            examples = [
                {
                    "title": row.title,
                    "url": row.url,
                    "published_at": row.published_at,
                    "summary": (row.summary or row.text)[:280],
                }
                for row in result.documents
            ]
            return {
                "status": "failed" if result.error else "ready",
                "can_confirm": result.error is None,
                "source": asdict(source),
                "examples": examples,
                "observed": len(result.documents),
                "latency_ms": latency_ms,
                "warnings": result.warnings,
                "error": result.error,
            }
        finally:
            db.close()

    def add_source(self, body: dict[str, Any]) -> dict[str, Any]:
        source = self._source_candidate(body)
        db = Database(self.db_path)
        try:
            try:
                db.sources.add(source)
                status = "created"
            except DuplicateSourceError as exc:
                source, status = exc.existing, "exists"
            return {"status": status, "source": asdict(source)}
        finally:
            db.close()

    def update_source(self, source_id: int, action: str) -> dict[str, Any]:
        db = Database(self.db_path)
        try:
            source = db.sources.get(source_id)
            if source is None:
                raise LookupError("source not found")
            if action == "enable":
                db.sources.set_enabled(source_id, True)
            elif action == "disable":
                db.sources.set_enabled(source_id, False)
            elif action == "decommission":
                db.sources.remove(source_id)
            else:
                raise ValueError("unknown source action")
            updated = db.sources.get(source_id)
            return {"status": "ok", "source": asdict(updated) if updated else None}
        finally:
            db.close()

    def import_url(self, url: str) -> dict[str, Any]:
        db = Database(self.db_path)
        try:
            document_id, created = Collector(self.config, self.paths, db).import_url(url)
            return {"document_id": document_id, "created": created}
        finally:
            db.close()

    def import_material(self, body: dict[str, Any]) -> dict[str, Any]:
        """Import a URL or user-pasted text with explicit provenance."""
        url = str(body.get("url") or "").strip()
        text = str(body.get("text") or "").strip()
        title = str(body.get("title") or "").strip()
        if not url:
            raise ValueError("укажите ссылку на источник")
        if not url.startswith(("http://", "https://")):
            raise ValueError("ссылка должна начинаться с http:// или https://")
        if not text:
            return self.import_url(url)
        if len(text) > 200_000:
            raise ValueError("вставленный текст превышает 200 000 символов")
        published_at = str(body.get("published_at") or _now()).strip()
        try:
            datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("некорректная дата публикации") from exc
        db = Database(self.db_path)
        try:
            manual = db.sources.ensure_manual()
            external_id = f"manual:{sha256_text(url, title, text)}"
            existing = db.documents.row_by_external_id(manual.id or 0, external_id)
            if existing is not None:
                return {"document_id": int(existing["id"]), "created": False}
            now = _now()
            document = RawDocument(
                source_id=manual.id or 0,
                external_id=external_id,
                url=url,
                title=title or text.splitlines()[0][:160],
                summary=text[:500],
                text=text,
                published_at=published_at,
                fetched_at=now,
            )
            document.compute_hash()
            with db.transaction():
                document_id = db.documents.insert(document)
            return {"document_id": document_id, "created": True}
        finally:
            db.close()

    # -- periodic runner ---------------------------------------------------

    def start_watch(self, interval_seconds: int = 600, process_limit: int = 30) -> None:
        if self._watch_thread and self._watch_thread.is_alive():
            return
        self._watch_stop.clear()
        with self._lock:
            self._watch_interval_seconds = interval_seconds
            self._next_watch_at = datetime.now(UTC) + timedelta(seconds=interval_seconds)

        def loop() -> None:
            while not self._watch_stop.wait(interval_seconds):
                with self._lock:
                    self._next_watch_at = datetime.now(UTC) + timedelta(seconds=interval_seconds)
                try:
                    self.start_job("cycle", limit=process_limit)
                except JobBusyError:
                    continue

        self._watch_thread = threading.Thread(target=loop, daemon=True, name="pilot-live-watch")
        self._watch_thread.start()

    def stop_watch(self) -> None:
        self._watch_stop.set()
        with self._lock:
            self._next_watch_at = None


def wait_for_job(
    service: PilotService,
    *,
    timeout: float = 300,
    interval: float = 0.2,
    callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Small test/demo helper; the HTTP UI polls asynchronously instead."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = service.status()["job"]
        if callback:
            callback(state)
        if state["state"] != "running":
            return state
        time.sleep(interval)
    raise TimeoutError("pilot job did not finish in time")
