#!/usr/bin/env python3
"""Targeted B1 v3 audit over the already captured live corpus.

This does not create new traffic. It replays the frozen proof, executes the
state/fault tests that cover the missing questions, and emits the empirical
shape of real normalized documents for B2 dataset design.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNS = Path(__file__).resolve().parent / "runs"

TARGETED_TESTS = [
    "tests/unit/test_collector.py::test_first_and_second_run_over_every_adapter",
    "tests/unit/test_collector.py::test_backfill_ignores_the_window",
    "tests/unit/test_collector.py::test_collector_never_discards_a_returned_tail",
    "tests/unit/test_collector.py::test_success_after_failure_clears_error_and_counter",
    "tests/unit/test_collector.py::test_failed_source_is_recorded_and_the_run_continues",
    "tests/unit/test_collector.py::test_recoverable_warning_is_reported_as_partial",
    "tests/unit/test_collector.py::test_rerun_does_not_duplicate_documents_by_external_id",
    "tests/unit/test_collector.py::test_legacy_per_source_cap_never_cuts_url_documents",
    "tests/unit/test_scraper_tg.py::test_incremental_run_stops_after_max_pages",
    "tests/unit/test_scraper_tg.py::test_backfill_walks_before_pages",
    "tests/unit/test_scraper_sitemap.py::test_backfill_keeps_everything_on_host",
]


def percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def latest_run() -> Path:
    root = ROOT / "benchmarks" / "b1_technical" / "runs"
    return root / (root / "LATEST_RUN").read_text(encoding="utf-8").strip()


def latest_proof() -> Path:
    root = ROOT / "benchmarks" / "b1_proof" / "runs"
    return root / (root / "LATEST_PROOF").read_text(encoding="utf-8").strip()


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def document_distribution(documents: list[dict]) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in documents:
        groups[str(row.get("source_kind") or "unknown")].append(row)
    result = {}
    for kind, rows in sorted(groups.items()):
        lengths = [len(str(row.get("text") or row.get("summary") or "")) for row in rows]
        result[kind] = {
            "n": len(rows),
            "content_chars": {
                "min": min(lengths),
                "median": int(statistics.median(lengths)),
                "p95": percentile(lengths, 0.95),
                "max": max(lengths),
            },
            "with_date": sum(bool(row.get("published_at")) for row in rows),
            "with_url": sum(bool(row.get("url")) for row in rows),
            "with_title": sum(bool(row.get("title")) for row in rows),
        }
    return result


def surface_examples(documents: list[dict], captures: list[dict]) -> list[dict]:
    by_url: dict[str, list[dict]] = defaultdict(list)
    for capture in captures:
        url = str(capture.get("url") or "").rstrip("/")
        if url and capture.get("response_body_sha256"):
            by_url[url].append(capture)
    examples = []
    seen = set()
    for row in documents:
        kind = str(row.get("source_kind") or "unknown")
        if kind in seen:
            continue
        matches = by_url.get(str(row.get("url") or "").rstrip("/"), [])
        capture = max(matches, key=lambda item: int(item.get("response_bytes") or 0)) if matches else None
        examples.append(
            {
                "method": kind,
                "raw_surface": (
                    {
                        "url": capture.get("url"),
                        "sha256": capture.get("response_body_sha256"),
                        "bytes": capture.get("response_bytes"),
                    }
                    if capture
                    else {"note": "no exact document URL/body pair in the capture index"}
                ),
                "normalized": {
                    "external_id": row.get("external_id"),
                    "url": row.get("url"),
                    "title": row.get("title"),
                    "published_at": row.get("published_at"),
                    "content_chars": len(str(row.get("text") or row.get("summary") or "")),
                    "content_sha256": hashlib.sha256(
                        str(row.get("text") or row.get("summary") or "").encode()
                    ).hexdigest(),
                },
            }
        )
        seen.add(kind)
    return examples


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    run_id = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ_gap_audit"
    )
    output = args.output_dir or RUNS / run_id
    output.mkdir(parents=True, exist_ok=True)

    completed = subprocess.run(
        [str(ROOT / ".venv" / "bin" / "pytest"), "-q", *TARGETED_TESTS],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    technical = latest_run()
    proof = latest_proof()
    documents = load_jsonl(technical / "documents.jsonl")
    captures = load_jsonl(technical / "captures.jsonl")
    proof_report = json.loads((proof / "report.json").read_text(encoding="utf-8"))
    three = json.loads(
        (ROOT / "benchmarks" / "b1_technical" / "runs" / "summary_3_runs.json").read_text(
            encoding="utf-8"
        )
    )
    response_sizes = [int(row.get("response_bytes") or 0) for row in captures]
    statuses = Counter(int(row.get("status") or 0) for row in captures)
    report = {
        "benchmark": "B1 targeted gap audit",
        "version": "3.0.0",
        "run_id": run_id,
        "uses_new_external_traffic": False,
        "inputs": {
            "proof_run": proof.name,
            "technical_run": technical.name,
            "live_run_ids": three["run_ids"],
        },
        "targeted_state_fault_tests": {
            "passed": completed.returncode == 0,
            "count": len(TARGETED_TESTS),
            "tests": TARGETED_TESTS,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        },
        "prior_frozen_proof": {
            "verdict": proof_report["verdict"],
            "C1": proof_report["C1_exact_contract"]["passed"],
            "C2": proof_report["C2_state_fault"]["passed"],
            "C3": proof_report["C3_full_collector_replay"]["passed"],
            "C4": proof_report["C4_live_compatibility"]["passed"],
        },
        "three_live_runs": {
            "source_attempts": three["totals"]["source_attempts"],
            "source_failures": three["totals"]["source_failures"],
            "methods": three["methods"],
        },
        "captured_response_shape": {
            "n": len(response_sizes),
            "bytes_p50": percentile(response_sizes, 0.5),
            "bytes_p95": percentile(response_sizes, 0.95),
            "bytes_max": max(response_sizes),
            "http_status_counts": dict(sorted(statuses.items())),
        },
        "normalized_document_shape": document_distribution(documents),
        "raw_to_normalized_examples": surface_examples(documents, captures),
        "verdict": "PASS"
        if completed.returncode == 0 and proof_report["verdict"] == "PASS"
        else "FAIL",
        "limitations": [
            "No new external traffic was created; live stability is inherited from the named three runs.",
            "A raw body is paired to a normalized example only by exact document URL; absent exact pairs stay explicit.",
            "Absolute live recall and publication-to-feed SLA remain unproved.",
        ],
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (RUNS / "LATEST_GAP_AUDIT").write_text(output.name + "\n", encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], "report": str(output / "report.json")}, ensure_ascii=False))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
