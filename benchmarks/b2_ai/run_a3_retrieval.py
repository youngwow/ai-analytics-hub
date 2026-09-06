#!/usr/bin/env python3
"""Isolated A3 comparison on >20 candidates, derived from frozen B2 materials."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(PROJECT_ROOT))

from src.common import load_env_secret  # noqa: E402
from src.config import Config  # noqa: E402
from src.paths import DEFAULT_PATHS  # noqa: E402
from src.processing.llm import build_embedding_provider, build_llm_provider  # noqa: E402
from src.product.contracts import EventRecord, EvidenceClaim, SignalDraft  # noqa: E402
from src.product.events import EventLinker, cosine  # noqa: E402

HERE = Path(__file__).resolve().parent


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def signal(material: dict) -> SignalDraft:
    quote = material["text"][: min(240, len(material["text"]))]
    return SignalDraft(
        signal_id=f"query:{material['id']}",
        material_id=material["id"],
        summary=material["title"],
        claims=(EvidenceClaim(material["title"], quote),),
        relevance="relevant",
        importance="medium",
        interest="BOTH",
        impact="",
        urgency="routine",
        confidence=1.0,
    )


def event(material: dict) -> EventRecord:
    return EventRecord(
        id=f"event:{material['id']}",
        title=material["title"],
        summary=material["title"],
        signal_ids=(),
        material_ids=(material["id"],),
        compact_text=f"{material['title']}\n{material['text']}",
    )


def cases(materials: dict[str, dict], event_cases: list[dict], bank_size: int) -> list[dict]:
    built = []
    all_ids = sorted(materials)
    for row in event_cases:
        for cluster_index, cluster in enumerate(row["expected_clusters"]):
            if len(cluster) < 2:
                continue
            target_id, query_id = cluster[0], cluster[1]
            excluded = set(cluster) | {query_id}
            distractors = [item_id for item_id in all_ids if item_id not in excluded]
            bank_ids = [target_id, *distractors[: bank_size - 1]]
            built.append(
                {
                    "case_id": f"{row['case_id']}-positive-{cluster_index}",
                    "query_id": query_id,
                    "target_event_id": f"event:{target_id}",
                    "bank_ids": bank_ids,
                }
            )
    negative_case = next(row for row in event_cases if row["case_id"] == "E04")
    for index, query_id in enumerate(negative_case["member_ids"]):
        bank_ids = [item_id for item_id in all_ids if item_id != query_id][:bank_size]
        built.append(
            {
                "case_id": f"E04-negative-{index}",
                "query_id": query_id,
                "target_event_id": None,
                "bank_ids": bank_ids,
            }
        )
    return built


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", default="v2")
    parser.add_argument(
        "--split", choices=["development", "validation", "holdout"], default="validation"
    )
    parser.add_argument("--bank-size", type=int, default=30)
    args = parser.parse_args()
    if args.bank_size <= 20:
        parser.error("bank-size must be greater than 20 to distinguish top-20 from full scan")

    data = HERE / "data" / args.version
    catalog = {row["id"]: row for row in load_jsonl(data / "catalog.jsonl")}
    visible = {item_id for item_id, row in catalog.items() if row["split"] == args.split}
    materials = {
        row["id"]: row
        for row in load_jsonl(data / "materials.jsonl")
        if row["id"] in visible
    }
    event_cases = [
        row for row in load_jsonl(data / "event_cases.jsonl") if row["split"] == args.split
    ]
    test_cases = cases(materials, event_cases, args.bank_size)
    config = Config.load()
    llm = build_llm_provider(
        config.llm, load_env_secret(config.llm.api_key_env, DEFAULT_PATHS.env_path)
    )
    embedder = build_embedding_provider(
        config.embeddings,
        load_env_secret(config.embeddings.api_key_env, DEFAULT_PATHS.env_path),
    )
    full_linker = EventLinker(llm, None, model=config.llm.model)
    embedded_linker = EventLinker(llm, embedder, model=config.llm.model)
    rows = []
    started = time.monotonic()
    try:
        for case in test_cases:
            query = signal(materials[case["query_id"]])
            bank = [event(materials[item_id]) for item_id in case["bank_ids"]]
            embedded_bank = embedded_linker.ensure_embeddings(bank)
            query_vector = embedder.embed([embedded_linker._signal_text(query)])[0]
            ranked = sorted(
                embedded_bank,
                key=lambda item: cosine(query_vector, item.embedding),
                reverse=True,
            )
            top20 = {item.id for item in ranked[:20]}
            full_started = time.monotonic()
            full = full_linker.link(query, bank, mode="full_scan")
            full_ms = int((time.monotonic() - full_started) * 1000)
            embedded_started = time.monotonic()
            embedded = embedded_linker.link(query, bank, mode="embedding_top20")
            embedded_ms = int((time.monotonic() - embedded_started) * 1000)
            target = case["target_event_id"]
            rows.append(
                {
                    "case_id": case["case_id"],
                    "query_id": case["query_id"],
                    "target_event_id": target,
                    "bank_size": len(bank),
                    "target_in_top20": target in top20 if target else None,
                    "full_scan": {
                        "event_id": full.event_id,
                        "relation": full.relation,
                        "evidence": full.evidence,
                        "needs_human_review": full.needs_human_review,
                        "correct": full.event_id == target,
                        "latency_ms": full_ms,
                    },
                    "embedding_top20": {
                        "event_id": embedded.event_id,
                        "relation": embedded.relation,
                        "evidence": embedded.evidence,
                        "needs_human_review": embedded.needs_human_review,
                        "correct": embedded.event_id == target,
                        "latency_ms": embedded_ms,
                    },
                }
            )
    finally:
        embedder.close()
        llm.close()

    positives = [row for row in rows if row["target_event_id"]]
    report = {
        "benchmark": "B2-A3 retrieval",
        "source_dataset": json.loads((data / "contract.json").read_text(encoding="utf-8"))[
            "dataset_version"
        ],
        "split": args.split,
        "bank_size": args.bank_size,
        "cases": len(rows),
        "positive_cases": len(positives),
        "negative_cases": len(rows) - len(positives),
        "retrieval_recall_at_20": (
            sum(bool(row["target_in_top20"]) for row in positives) / len(positives)
        ),
        "full_scan_accuracy": sum(row["full_scan"]["correct"] for row in rows) / len(rows),
        "embedding_top20_accuracy": (
            sum(row["embedding_top20"]["correct"] for row in rows) / len(rows)
        ),
        "full_scan_latency_ms": sum(row["full_scan"]["latency_ms"] for row in rows),
        "embedding_top20_latency_ms": sum(
            row["embedding_top20"]["latency_ms"] for row in rows
        ),
        "wall_seconds": time.monotonic() - started,
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "rows"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
