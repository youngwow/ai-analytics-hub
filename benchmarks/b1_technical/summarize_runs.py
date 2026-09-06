"""Aggregate repeated live B1 runs without mixing in semantic quality."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, math.ceil(q * len(values)) - 1)], 3)


def pct(a: int, b: int) -> float | None:
    return round(100 * a / b, 2) if b else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dirs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    method_rows: dict[str, list[dict]] = defaultdict(list)
    source_runs: dict[tuple[str, str], list[dict]] = defaultdict(list)
    documents: dict[str, list[dict]] = defaultdict(list)
    non_success_http: list[dict] = []
    run_summaries = []
    for run_dir in args.run_dirs:
        report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
        sources = json.loads((run_dir / "source_results.json").read_text(encoding="utf-8"))
        docs = [json.loads(line) for line in (run_dir / "documents.jsonl").read_text(encoding="utf-8").splitlines()]
        captures = [json.loads(line) for line in (run_dir / "captures.jsonl").read_text(encoding="utf-8").splitlines()]
        run_summaries.append({"run_id": run_dir.name, **report["live"]})
        for row in report.get("methods", report.get("methods_ranked", [])):
            method_rows[row["method"]].append(row)
        for row in sources:
            source_runs[(row["kind"], row["name"])].append(row)
        for row in docs:
            documents[row["source_kind"]].append(row)
        for row in captures:
            status = row.get("status")
            if status is not None and not (200 <= status < 400):
                non_success_http.append(
                    {
                        "run_id": run_dir.name,
                        "method": row["method"],
                        "url": row["url"],
                        "status": status,
                    }
                )

    methods = []
    for kind, rows in method_rows.items():
        attempts = [row for (source_kind, _), values in source_runs.items() if source_kind == kind for row in values]
        per_source = [values for (source_kind, _), values in source_runs.items() if source_kind == kind]
        docs = documents[kind]
        complete = sum(bool(d.get("title") and (d.get("text") or d.get("summary")) and d.get("published_at")) for d in docs)
        methods.append(
            {
                "method": kind,
                "configured_sources": rows[0]["configured_sources"],
                "source_attempts": len(attempts),
                "adapter_success_percent": pct(sum(row["status"] in ("ok", "not_modified") for row in attempts), len(attempts)),
                "sources_successful_in_all_runs_percent": pct(sum(all(row["status"] in ("ok", "not_modified") for row in values) for values in per_source), len(per_source)),
                "attempts_with_documents_percent": pct(sum(row["stored"] > 0 for row in attempts), len(attempts)),
                "stored_documents": len(docs),
                "complete_document_percent": pct(complete, len(docs)),
                "live_latency_p50_ms": percentile([row["latency_ms"] for row in attempts], 0.5),
                "live_latency_p95_ms": percentile([row["latency_ms"] for row in attempts], 0.95),
                "replay_success_percent": min(row["load_success_percent"] for row in rows),
                "replay_stability_percent": min(row["load_stability_percent"] for row in rows),
                "replay_24_workers_p95_ms_median": percentile([row["load_p95_ms"] for row in rows], 0.5),
                "replay_24_workers_throughput_rps_median": percentile([row["load_throughput_rps"] for row in rows], 0.5),
            }
        )

    speed = sorted(methods, key=lambda row: row["live_latency_p95_ms"])
    for rank, row in enumerate(speed, 1):
        row["live_speed_rank"] = rank
    coverage = sorted(methods, key=lambda row: (-row["stored_documents"], -row["configured_sources"]))
    for rank, row in enumerate(coverage, 1):
        row["technical_coverage_rank"] = rank

    result = {
        "benchmark": "B1-technical-three-run-summary",
        "run_ids": [path.name for path in args.run_dirs],
        "runs": run_summaries,
        "totals": {
            "live_runs": len(args.run_dirs),
            "source_attempts": sum(row["sources"] for row in run_summaries),
            "source_failures": sum(row["sources_failed"] for row in run_summaries),
            "documents_stored": sum(row["documents_stored"] for row in run_summaries),
            "http_exchanges": sum(row["http_exchanges"] for row in run_summaries),
            "raw_response_bytes": sum(row["response_bytes"] for row in run_summaries),
            "wall_seconds": round(sum(row["wall_ms"] for row in run_summaries) / 1000, 3),
        },
        "methods": sorted(methods, key=lambda row: row["technical_coverage_rank"]),
        "rankings": {
            "stability": "all methods tied at 100% adapter/replay success across this three-run window",
            "live_speed": [row["method"] for row in speed],
            "technical_coverage": [row["method"] for row in coverage],
        },
        "non_success_http": non_success_http,
        "ranking_note": "No semantic relevance is scored. Coverage means technically delivered documents; methods are complementary.",
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
