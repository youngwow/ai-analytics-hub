"""Headless CLI for the target workflow (`python -m src.product.cli ...`)."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from ..common import load_env_secret
from ..config import Config
from ..paths import DEFAULT_PATHS
from ..processing.llm import build_embedding_provider, build_llm_provider
from ..storage import Database
from .analysis import PrimaryAnalyzer
from .contracts import EventRecord, GsLabsContext, PreparedDocument
from .critic import SignalCritic
from .events import EventLinker
from .operations import MetricsService, WorkQueue
from .preparation import prepare_raw_document
from .providers import TavilyResearchSearch
from .research import TargetedResearcher
from .store import ProductStore
from .workflow import BranchConfiguration, ProductWorkflow


def _load(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _key(name: str) -> str:
    return load_env_secret(name, DEFAULT_PATHS.env_path)


def _branches(raw: str) -> BranchConfiguration:
    values = dict(part.split("=", 1) for part in raw.split(",") if "=" in part)
    return BranchConfiguration(**values)


def run(args) -> int:
    config = Config.load()
    branches = _branches(args.branches)
    llm = build_llm_provider(config.llm, _key(config.llm.api_key_env))
    embedder = None
    if branches.a3 == "embedding_top20":
        embedder = build_embedding_provider(
            config.embeddings, _key(config.embeddings.api_key_env)
        )
    search = None
    researcher = None
    if branches.a2 == "targeted_research":
        search = TavilyResearchSearch(config.tavily, _key(config.tavily.api_key_env))
        researcher = TargetedResearcher(llm, search, config.llm.model)
    db = Database(args.db or DEFAULT_PATHS.db_path)
    try:
        store = ProductStore(db.conn)
        workflow = ProductWorkflow(
            PrimaryAnalyzer(llm, model=config.llm.model, max_chars=config.processing.max_chars),
            EventLinker(llm, embedder, model=config.llm.model),
            store,
            researcher=researcher,
            critic=SignalCritic(llm, model=config.llm.model) if branches.a4 == "with_critic" else None,
        )
        document = PreparedDocument.from_dict(_load(args.input))
        context = GsLabsContext.from_dict(_load(args.context))
        events = [EventRecord(**row) for row in (_load(args.events) if args.events else [])]
        result = workflow.run(document, context, events, branches)
        rendered = json.dumps(asdict(result), ensure_ascii=False, indent=2)
        if args.output:
            Path(args.output).write_text(rendered + "\n", encoding="utf-8")
        else:
            print(rendered)
    finally:
        if search:
            search.close()
        if embedder and hasattr(embedder, "close"):
            embedder.close()
        llm.close()
        db.close()
    return 0


def run_collected(args) -> int:
    """Process current immutable document revisions into the product queue."""
    config = Config.load()
    branches = _branches(args.branches)
    context = GsLabsContext.from_dict(_load(args.context))
    llm = build_llm_provider(config.llm, _key(config.llm.api_key_env))
    embedder = None
    if branches.a3 == "embedding_top20":
        embedder = build_embedding_provider(
            config.embeddings, _key(config.embeddings.api_key_env)
        )
    search = None
    researcher = None
    if branches.a2 == "targeted_research":
        search = TavilyResearchSearch(config.tavily, _key(config.tavily.api_key_env))
        researcher = TargetedResearcher(llm, search, config.llm.model)
    db = Database(args.db or DEFAULT_PATHS.db_path)
    summary = {"configuration_id": branches.id, "processed": 0, "skipped": 0, "failed": 0, "documents": []}
    try:
        store = ProductStore(db.conn)
        store.ensure_context(context.version, context, actor="operator")
        workflow = ProductWorkflow(
            PrimaryAnalyzer(llm, model=config.llm.model, max_chars=config.processing.max_chars),
            EventLinker(llm, embedder, model=config.llm.model),
            store,
            researcher=researcher,
            critic=SignalCritic(llm, model=config.llm.model) if branches.a4 == "with_critic" else None,
        )
        rows = db.conn.execute(
            """SELECT d.id,d.source_id,d.content_hash,
                      COALESCE(MAX(r.revision),1) AS current_revision
               FROM documents d
               LEFT JOIN document_revisions r ON r.document_id=d.id
               WHERE d.hidden=0
               GROUP BY d.id
               ORDER BY d.published_at DESC,d.id DESC
               LIMIT ?""",
            (args.limit,),
        ).fetchall()
        for row in rows:
            material_id = f"raw-{row['id']}-rev-{row['current_revision']}"
            if not args.force and store.has_analysis(material_id, branches.id):
                summary["skipped"] += 1
                continue
            raw = db.documents.get(int(row["id"]))
            source = db.sources.get(int(row["source_id"]))
            if raw is None or source is None:
                summary["failed"] += 1
                summary["documents"].append(
                    {"material_id": material_id, "status": "failed", "error": "document or source missing"}
                )
                continue
            prepared = prepare_raw_document(
                material_id, raw, source, max_chunk_chars=config.processing.max_chars
            )
            try:
                result = workflow.run(
                    prepared,
                    context,
                    store.load_events(),
                    branches,
                    raw_document_id=int(row["id"]),
                )
            except Exception as exc:
                summary["failed"] += 1
                store.audit(
                    "workflow.failed",
                    "material",
                    material_id,
                    "system",
                    {"error_type": type(exc).__name__, "error": str(exc)},
                )
                summary["documents"].append(
                    {
                        "material_id": material_id,
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                if args.fail_fast:
                    break
                continue
            summary["processed"] += 1
            summary["documents"].append(
                {
                    "material_id": material_id,
                    "status": result.analysis.status,
                    "signals": len(result.analysis.signals),
                    "queue_signal_ids": list(result.queue_signal_ids),
                }
            )
    finally:
        if search:
            search.close()
        if embedder and hasattr(embedder, "close"):
            embedder.close()
        llm.close()
        db.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["failed"] else 0


def queue(args) -> int:
    db = Database(args.db or DEFAULT_PATHS.db_path)
    try:
        print(json.dumps(WorkQueue(ProductStore(db.conn)).list(profile=args.profile), ensure_ascii=False, indent=2))
    finally:
        db.close()
    return 0


def metrics(args) -> int:
    db = Database(args.db or DEFAULT_PATHS.db_path)
    try:
        print(json.dumps(MetricsService(ProductStore(db.conn)).snapshot(), ensure_ascii=False, indent=2))
    finally:
        db.close()
    return 0


def review(args) -> int:
    db = Database(args.db or DEFAULT_PATHS.db_path)
    try:
        decision_id = ProductStore(db.conn).save_review(
            args.signal, args.revision, args.decision, args.actor,
            {"note": args.note} if args.note else {},
        )
        print(json.dumps({"status": "ok", "decision_id": decision_id}, ensure_ascii=False))
    finally:
        db.close()
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="product")
    root.add_argument("--db")
    sub = root.add_subparsers(dest="command", required=True)
    command = sub.add_parser("run")
    command.add_argument("--input", required=True)
    command.add_argument("--context", required=True)
    command.add_argument("--events")
    command.add_argument("--output")
    command.add_argument(
        "--branches",
        default="a1=one_pass,a2=without_research,a3=full_scan,a4=without_critic",
    )
    command.set_defaults(func=run)
    command = sub.add_parser(
        "run-collected", help="process collected SQLite documents into the product queue"
    )
    command.add_argument("--context", required=True)
    command.add_argument("--limit", type=int, default=20)
    command.add_argument("--force", action="store_true")
    command.add_argument("--fail-fast", action="store_true")
    command.add_argument(
        "--branches",
        default="a1=one_pass,a2=without_research,a3=full_scan,a4=without_critic",
    )
    command.set_defaults(func=run_collected)
    command = sub.add_parser("queue")
    command.add_argument("--profile", choices=["PR", "GR"])
    command.set_defaults(func=queue)
    command = sub.add_parser("metrics")
    command.set_defaults(func=metrics)
    command = sub.add_parser("review")
    command.add_argument("--signal", required=True)
    command.add_argument("--revision", required=True, type=int)
    command.add_argument("--decision", required=True, choices=["include", "exclude", "restore", "edit", "confirm_link", "reject_link"])
    command.add_argument("--actor", required=True)
    command.add_argument("--note", default="")
    command.set_defaults(func=review)
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
