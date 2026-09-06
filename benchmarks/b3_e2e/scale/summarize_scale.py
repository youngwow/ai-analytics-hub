#!/usr/bin/env python3
"""Create a count-first B3-SCALE decision table from raw reports."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import defaultdict
from pathlib import Path


def summarize(paths: list[Path], amendments_path: Path | None = None) -> dict:
    rows = []
    metadata = []
    seen = set()
    amendments = {}
    amendment_meta = None
    if amendments_path:
        amendment_rows = json.loads(amendments_path.read_text(encoding="utf-8"))
        amendments = {row["case_id"]: row for row in amendment_rows}
        amendment_meta = {
            "path": str(amendments_path),
            "sha256": hashlib.sha256(amendments_path.read_bytes()).hexdigest(),
            "count": len(amendments),
        }
    for path in paths:
        report = json.loads(path.read_text(encoding="utf-8"))
        metadata.append(
            {
                "path": str(path),
                "embedding_model": report.get("embedding_model"),
                "embedding_dimensions": report.get("embedding_dimensions"),
                "generation_model": report.get("generation_model"),
            }
        )
        for row in report["results"]:
            row = dict(row)
            amendment = amendments.get(row["case_id"])
            if amendment:
                row["original_expected_object_id"] = row["expected_object_id"]
                row["original_expected_relation"] = row["expected_relation"]
                row["expected_object_id"] = amendment["expected_object_id"]
                row["expected_relation"] = amendment["expected_relation"]
                row["object_correct"] = row["predicted_object_id"] == row["expected_object_id"]
                row["relation_correct"] = row["predicted_relation"] == row["expected_relation"]
                row["gold_amendment_reason"] = amendment["reason"]
            key = (row["size"], row["case_id"], row["mode"])
            if key in seen:
                raise ValueError(f"duplicate result: {key}")
            seen.add(key)
            rows.append(row)
    retrieval_by_case = {
        (row["size"], row["case_id"]): row["retrieval_hit_at_20"]
        for row in rows
        if row["retrieval_hit_at_20"] is not None
    }
    for row in rows:
        if row["mode"].startswith("embedding_top20") and row["retrieval_hit_at_20"] is None:
            row["retrieval_hit_at_20"] = retrieval_by_case.get(
                (row["size"], row["case_id"])
            )
    groups = defaultdict(list)
    for row in rows:
        groups[(row["size"], row["mode"], row["kind"])].append(row)
    table = []
    for (size, mode, kind), items in sorted(groups.items()):
        linked = [item for item in items if item["expected_object_id"] is not None]
        no_link = [item for item in items if item["expected_object_id"] is None]
        retrieval = [item for item in linked if item["retrieval_hit_at_20"] is not None]
        calls = [call for item in items for call in item.get("llm_calls", [])]
        table.append(
            {
                "size": size,
                "mode": mode,
                "kind": kind,
                "cases": len(items),
                "retrieval_hits": (
                    sum(bool(item["retrieval_hit_at_20"]) for item in retrieval)
                    if retrieval
                    else None
                ),
                "retrieval_linked_cases": len(retrieval) if retrieval else None,
                "final_object_correct": sum(bool(item["object_correct"]) for item in items),
                "final_relation_correct": sum(bool(item["relation_correct"]) for item in items),
                "false_links": sum(
                    item["predicted_object_id"] is not None for item in no_link
                ),
                "missed_links": sum(
                    item["predicted_object_id"] is None for item in linked
                ),
                "provider_errors": sum(item.get("error") is not None for item in items),
                "candidate_counts": sorted({item["bank_candidates"] for item in items}),
                "input_tokens_sum": sum(int(call.get("tokens_in") or 0) for call in calls),
                "wall_ms_median": int(statistics.median(item["wall_ms"] for item in items)),
            }
        )
    return {
        "benchmark": "B3-SCALE",
        "status": "MEASURED_NOT_YET_DECIDED",
        "reports": metadata,
        "gold_amendments": amendment_meta,
        "table": table,
        "interpretation_rule": (
            "Choose A3 only after all scale points: top-20 must retain every labelled true link; "
            "the same final resolver must not add errors versus a runnable full-scan control. "
            "A full-scan provider/context failure establishes its operating boundary."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--amendments", type=Path)
    args = parser.parse_args()
    report = summarize(args.reports, args.amendments)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
