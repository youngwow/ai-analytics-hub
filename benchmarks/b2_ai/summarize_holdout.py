#!/usr/bin/env python3
"""Summarize repeated B2 holdout runs without selecting the best run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def get(row: dict, *path: str):
    value = row
    for key in path:
        value = value[key]
    return value


METRICS = {
    "relevance_macro_f1": ("material_labels", "relevance", "macro_f1"),
    "importance_macro_f1": ("material_labels", "importance", "macro_f1"),
    "roles_exact": ("material_labels", "exact", "roles", "exact_set_accuracy"),
    "critical_accuracy": (
        "material_labels",
        "exact",
        "critical_or_escalate",
        "accuracy",
    ),
    "event_pair_f1": ("event_grouping", "pair_f1"),
    "event_b_cubed_f1": ("event_grouping", "b_cubed", "f1"),
    "npa_identity_accuracy": ("npa", "link_identity_accuracy"),
    "npa_relation_accuracy": ("npa", "relation_accuracy"),
    "npa_stage_accuracy": ("npa", "stage_accuracy"),
    "review_error_recall": ("review_policy", "label_error_recall"),
    "review_critical_recall": ("review_policy", "critical_recall"),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("reports", nargs="+", type=Path)
    args = parser.parse_args()
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in args.reports]
    metrics = {}
    for name, path in METRICS.items():
        values = [get(report, *path) for report in reports]
        present = [float(value) for value in values if value is not None]
        metrics[name] = {
            "values": values,
            "worst": min(present) if present else None,
            "best": max(present) if present else None,
        }
    safety = [
        {
            "run_id": report["run_id"],
            "contract_errors": report["contract_errors"],
            "critical_in_low": report["safety"]["critical_in_low"],
            "verbatim_evidence_rate": report["safety"]["verbatim_evidence_rate"],
            "failed_calls": (report.get("usage") or {}).get("failed_calls"),
            "wall_seconds": (report.get("usage") or {}).get("wall_seconds"),
        }
        for report in reports
    ]
    safety_pass = all(
        not row["contract_errors"]
        and not row["critical_in_low"]
        and row["verbatim_evidence_rate"] == 1.0
        and row["failed_calls"] == 0
        for row in safety
    )
    result = {
        "benchmark": "B2 v2 repeated holdout",
        "runs": len(reports),
        "verdict": "LIMITED_PASS" if safety_pass else "FAIL",
        "safety_pass_all_runs": safety_pass,
        "metrics": metrics,
        "safety": safety,
        "limitations": [
            "Single-team labels are not customer gold.",
            "Semantic quality remains pending an explicitly independent judge/B4.",
            "No user-time, usability or adoption claim is supported.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if safety_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
