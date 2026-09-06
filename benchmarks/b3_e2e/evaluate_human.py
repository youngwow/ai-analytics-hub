#!/usr/bin/env python3
"""Validate and summarise timed human observations without inventing verdicts."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument(
    "observations", type=Path, help="JSONL rows matching data/v2/observation.schema.json"
)
parser.add_argument("--report", required=True, type=Path)
parser.add_argument(
    "--value-scope",
    choices=("full", "post_collection"),
    default="full",
    help="full includes collection; post_collection compares analysis and packaging only",
)
args = parser.parse_args()
rows = [
    json.loads(line)
    for line in args.observations.read_text(encoding="utf-8").splitlines()
    if line.strip()
]

required = {
    "run_id",
    "participant_id",
    "scenario_id",
    "workflow",
    "task",
    "active_seconds",
    "corrections",
    "completed",
}
numeric_nonnegative = {
    "active_seconds",
    "waiting_seconds",
    "corrections",
    "original_opens",
    "critical_missed",
    "required_facts_missed",
    "unsupported_claims",
    "relevant_items_missed",
    "irrelevant_items_kept",
}
valid_scenarios = {
    "A",
    "B",
    "D1_critical",
    "D2_event_boundary",
    "D3_new_npa",
    "D4_known_npa",
    "UI_ACCEPTANCE",
}
valid_workflows = {"manual_baseline", "product"}
valid_tasks = {
    "collection",
    "analysis",
    "packaging",
    "review_all",
    "review_risk_only",
    "flat_events",
    "grouped_events",
    "common_release",
    "role_release",
    "full_queue",
    "top_layer",
    "acceptance",
}
errors = []
seen = set()
for index, row in enumerate(rows):
    if not isinstance(row, dict):
        errors.append(f"row {index}: must be an object")
        continue
    missing = required - set(row)
    if missing:
        errors.append(f"row {index}: missing {sorted(missing)}")
    key = (row.get("run_id"), row.get("participant_id"), row.get("scenario_id"), row.get("task"))
    if key in seen:
        errors.append(f"row {index}: duplicate observation key {key}")
    seen.add(key)
    if row.get("scenario_id") not in valid_scenarios:
        errors.append(f"row {index}: invalid scenario_id")
    if row.get("workflow") not in valid_workflows:
        errors.append(f"row {index}: invalid workflow")
    if row.get("task") not in valid_tasks:
        errors.append(f"row {index}: invalid task")
    if not isinstance(row.get("completed"), bool):
        errors.append(f"row {index}: completed must be boolean")
    for field in ("run_id", "participant_id"):
        if not isinstance(row.get(field), str) or not row.get(field).strip():
            errors.append(f"row {index}: invalid {field}")
    for field in numeric_nonnegative:
        if field in row and (
            not isinstance(row[field], (int, float))
            or isinstance(row[field], bool)
            or row[field] < 0
        ):
            errors.append(f"row {index}: invalid {field}")


def summary(selected: list[dict]) -> dict:
    if not selected:
        return {
            "n": 0,
            "median_active_seconds": None,
            "completed_rate": None,
            "total_corrections": 0,
            "total_safety_errors": None,
        }
    safety_fields = ("critical_missed", "required_facts_missed", "unsupported_claims")
    safety_known = all(all(field in row for field in safety_fields) for row in selected)
    return {
        "n": len(selected),
        "median_active_seconds": statistics.median(row["active_seconds"] for row in selected),
        "completed_rate": sum(bool(row["completed"]) for row in selected) / len(selected),
        "total_corrections": sum(row.get("corrections", 0) for row in selected),
        "total_safety_errors": sum(sum(row[field] for field in safety_fields) for row in selected)
        if safety_known
        else None,
    }


clean_rows = [
    row
    for row in rows
    if isinstance(row, dict)
    and required.issubset(row)
    and isinstance(row.get("active_seconds"), (int, float))
    and not isinstance(row.get("active_seconds"), bool)
    and isinstance(row.get("completed"), bool)
]


value_tasks = (
    ["collection", "analysis", "packaging"]
    if args.value_scope == "full"
    else ["analysis", "packaging"]
)
comparisons = {
    "VALUE": {"manual_baseline": value_tasks, "product": value_tasks},
    "H2": {"review_all": ["review_all"], "review_risk_only": ["review_risk_only"]},
    "H3": {"flat": ["flat_events"], "events": ["grouped_events"]},
    "H4": {"common": ["common_release"], "role": ["role_release"]},
    "H5": {"full": ["full_queue"], "top": ["top_layer"]},
}
results = {}
for hypothesis, variants in comparisons.items():
    variant_results = {}
    for variant, tasks in variants.items():
        selected = [
            row
            for row in clean_rows
            if row.get("task") in tasks
            and (hypothesis != "VALUE" or row.get("workflow") == variant)
        ]
        if hypothesis == "VALUE":
            grouped = defaultdict(list)
            for row in selected:
                grouped[
                    (row.get("run_id"), row.get("participant_id"), row.get("scenario_id"))
                ].append(row)
            complete_groups = []
            for key, group in grouped.items():
                if {row.get("task") for row in group} != set(tasks):
                    continue
                complete_groups.append(
                    {
                        "active_seconds": sum(row["active_seconds"] for row in group),
                        "completed": all(row["completed"] for row in group),
                        "corrections": sum(row.get("corrections", 0) for row in group),
                        "critical_missed": sum(row.get("critical_missed", 0) for row in group),
                        "required_facts_missed": sum(
                            row.get("required_facts_missed", 0) for row in group
                        ),
                        "unsupported_claims": sum(
                            row.get("unsupported_claims", 0) for row in group
                        ),
                    }
                )
            selected = complete_groups
        variant_results[variant] = summary(selected)
    ready = all(value["n"] > 0 for value in variant_results.values())
    results[hypothesis] = {
        "ready_for_interpretation": ready,
        "variants": variant_results,
        "verdict": "not_computed_without_pre_registered_decision_rule" if ready else "not_tested",
    }

report = {
    "status": "VALID" if not errors else "INVALID",
    "errors": errors,
    "rows": len(rows),
    "participants": sorted(
        {row.get("participant_id") for row in clean_rows if row.get("participant_id")}
    ),
    "value_scope": args.value_scope,
    "comparisons": results,
    "warning": (
        "This report summarises observations. A post_collection value scope does not "
        "measure manual source collection. Proxy participants are not customer evidence, "
        "and no verdict is created without a frozen rule."
    ),
}
args.report.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(1 if errors else 0)
