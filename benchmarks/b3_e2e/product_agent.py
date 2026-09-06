#!/usr/bin/env python3
"""Run the real product components against a leak-free B3 packet."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import time
import uuid
from dataclasses import replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(PROJECT_ROOT))

from trafilatura import extract  # noqa: E402

from src.common import load_env_secret  # noqa: E402
from src.config import Config  # noqa: E402
from src.paths import DEFAULT_PATHS  # noqa: E402
from src.processing.llm import build_llm_provider  # noqa: E402
from src.product.analysis import PrimaryAnalyzer  # noqa: E402
from src.product.contracts import EventRecord, GsLabsContext, PreparedDocument  # noqa: E402
from src.product.events import EventLinker  # noqa: E402
from src.product.npa import NpaResolver  # noqa: E402
from src.product.release import (  # noqa: E402
    build_deliveries,
    event_object,
    item_decision,
    npa_object,
)
from src.product.store import ProductStore  # noqa: E402
from src.storage import Database  # noqa: E402


def _transport_text(payload: dict) -> tuple[str, str]:
    kind = str(payload.get("format") or "")
    if kind == "rss_item":
        return str(payload.get("title") or ""), str(
            payload.get("description") or payload.get("content") or ""
        )
    if kind == "telegram_message":
        text = str(payload.get("text") or "")
        return text.splitlines()[0] if text else "", text
    if kind == "search_result":
        return str(payload.get("title") or ""), str(payload.get("content") or "")
    if kind == "html":
        body = str(payload.get("body") or "")
        text = extract(body, include_comments=False, include_tables=False) or ""
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", body, flags=re.I | re.S)
        title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip() if title_match else ""
        return title, text
    return "", json.dumps(payload, ensure_ascii=False)


def prepared_documents(packet: dict) -> list[PreparedDocument]:
    """Collapse duplicate discovery observations before any AI call."""
    selected: dict[str, dict] = {}
    for row in packet["timeline"]:
        item_id = str(row["canonical_item_id"])
        previous = selected.get(item_id)
        # Fixed-source content is preferred in hybrid mode; search rediscovery
        # remains provenance, not a second publication.
        if previous is None or (
            previous.get("discovery_channel") == "search"
            and row.get("discovery_channel") == "fixed"
        ):
            selected[item_id] = row
    result = []
    for item_id, row in sorted(
        selected.items(), key=lambda pair: (pair[1]["available_at"], pair[0])
    ):
        if "raw_text" in row:
            title, text = str(row.get("title") or ""), str(row.get("raw_text") or "")
        else:
            title, text = _transport_text(row.get("transport_payload") or {})
        surface = str((row.get("transport_payload") or {}).get("format") or "benchmark")
        source_name = str(row.get("source_name") or "")
        source_url = str(row.get("source_url") or "")
        source_marker = f"{source_name} {source_url}".lower()
        result.append(
            PreparedDocument(
                id=item_id,
                title=title or item_id,
                text=text,
                source_name=source_name,
                source_type=surface,
                source_url=source_url,
                published_at=str(row.get("available_at") or ""),
                source_class=(
                    "regulator"
                    if "regulator" in source_marker
                    or "регулирован" in source_marker
                    or "официальн" in source_marker
                    or "реестр" in source_marker
                    else "ordinary"
                ),
            )
        )
    return result


def _initial_events(initial_state: dict) -> list[EventRecord]:
    return [
        EventRecord(
            id=str(row["object_id"]),
            title=str(row["title"]),
            summary=str(row["title"]),
            signal_ids=(),
            material_ids=(),
            compact_text=str(row["title"]),
        )
        for row in initial_state.get("known_events", [])
        if row.get("status") != "archived"
    ]


def _link_events(linker: EventLinker, signals, initial_state: dict):
    events = _initial_events(initial_state)
    for signal in signals:
        decision = linker.link(signal, events, mode="full_scan")
        if decision.event_id and decision.relation in {"same_event", "event_update"}:
            next_events = []
            for event in events:
                if event.id != decision.event_id:
                    next_events.append(event)
                    continue
                next_events.append(
                    replace(
                        event,
                        signal_ids=tuple(dict.fromkeys((*event.signal_ids, signal.signal_id))),
                        material_ids=tuple(
                            dict.fromkeys((*event.material_ids, signal.material_id))
                        ),
                        compact_text=f"{event.compact_text}\n{signal.summary}\n{signal.impact}",
                        embedding=(),
                        version=event.version + 1,
                    )
                )
            events = next_events
        else:
            events.append(
                EventRecord(
                    id="evt-" + uuid.uuid5(uuid.NAMESPACE_URL, signal.signal_id).hex[:12],
                    title=signal.summary,
                    summary=signal.summary,
                    signal_ids=(signal.signal_id,),
                    material_ids=(signal.material_id,),
                    compact_text=f"{signal.summary}\n{signal.impact}",
                )
            )
    return [event for event in events if event.material_ids]


def _persist_npas(store: ProductStore, resolutions, signal_map) -> None:
    for resolution in resolutions:
        members = [signal_map[item] for item in resolution.member_signal_ids if item in signal_map]
        if not members:
            continue
        payload = npa_object(resolution, signal_map)
        store.save_npa_resolution(resolution, payload)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", nargs="?", default=os.environ.get("B3_INPUT"))
    parser.add_argument("output", nargs="?", default=os.environ.get("B3_OUTPUT"))
    parser.add_argument("--db")
    args = parser.parse_args(argv)
    if not args.input or not args.output:
        parser.error("input/output paths or B3_INPUT/B3_OUTPUT are required")

    packet = json.loads(Path(args.input).read_text(encoding="utf-8"))
    config = Config.load()
    llm_key = load_env_secret(config.llm.api_key_env, DEFAULT_PATHS.env_path)
    provider = build_llm_provider(config.llm, llm_key)
    npa_provider = build_llm_provider(
        replace(config.llm, temperature=0.0, think=None, max_output_tokens=-1),
        llm_key,
    )
    db_path = Path(args.db) if args.db else Path(os.environ.get("B3_WORKDIR", ".")) / "product.db"
    db = Database(str(db_path))
    store = ProductStore(db.conn)
    context = GsLabsContext.from_dict(packet["company_context"])
    store.ensure_context(context.version, context, actor="benchmark")
    documents = prepared_documents(packet)
    analyzer = PrimaryAnalyzer(
        provider, model=config.llm.model, max_chars=config.processing.max_chars
    )
    linker = EventLinker(provider, None, model=config.llm.model)
    resolver = NpaResolver(npa_provider, model=config.llm.model)
    started = time.monotonic()
    try:

        def analyze(document):
            return document, analyzer.analyze(document, context, mode="one_pass")

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=config.processing.concurrency
        ) as executor:
            analyzed = list(executor.map(analyze, documents))

        decisions = []
        signal_map = {}
        document_map = {document.id: document for document in documents}
        for document, draft in analyzed:
            prepared_version = store.save_prepared(document.id, document)
            store.save_analysis(draft, prepared_version=prepared_version)
            decisions.append(item_decision(document.id, draft))
            signal_map.update({signal.signal_id: signal for signal in draft.signals})

        npa_candidates = [
            signal
            for signal in signal_map.values()
            if signal.kind in {"npa", "npa_candidate"}
        ]
        all_resolutions = resolver.resolve(
            npa_candidates,
            document_map,
            list((packet.get("initial_state") or {}).get("tracked_npas", [])),
        )
        tracked_ids = {
            str(item["object_id"])
            for item in (packet.get("initial_state") or {}).get("tracked_npas", [])
            if item.get("object_id")
        }
        resolutions = tuple(
            resolution
            for resolution in all_resolutions
            if resolution.external_id or resolution.object_id in tracked_ids
        )
        resolved_npa_signal_ids = {
            signal_id
            for resolution in resolutions
            for signal_id in resolution.member_signal_ids
        }
        event_signals = [
            signal
            for signal in signal_map.values()
            if signal.signal_id not in resolved_npa_signal_ids
            and signal.relevance != "irrelevant"
        ]
        events = _link_events(linker, event_signals, packet.get("initial_state") or {})
        for event in events:
            store.save_event(event)
        _persist_npas(store, resolutions, signal_map)

        objects = [event_object(event, signal_map) for event in events]
        objects.extend(npa_object(resolution, signal_map) for resolution in resolutions)
        objects = [item for item in objects if item is not None]
        deliverable_ids = {
            obj["object_id"]
            for obj in objects
            if any(
                signal.relevance == "relevant" or signal.critical_or_escalate
                for signal in signal_map.values()
                if signal.material_id in obj["member_ids"]
            )
        }
        scheduled_release = bool((packet.get("scenario") or {}).get("release_at"))
        output = {
            "run_id": str(uuid.uuid4()),
            "scenario_id": packet["scenario_id"],
            "mode": packet["mode"],
            "item_decisions": decisions,
            "objects": objects,
            "deliveries": build_deliveries(
                objects,
                allowed_object_ids=deliverable_ids,
                scheduled_release=scheduled_release,
            ),
            "telemetry": {
                "wall_seconds": time.monotonic() - started,
                "generation_provider": "ollama",
                "generation_model": config.llm.model,
                "architecture": "one_pass/full_scan/no_critic/review_first",
            },
        }
        Path(args.output).write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    finally:
        npa_provider.close()
        provider.close()
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
