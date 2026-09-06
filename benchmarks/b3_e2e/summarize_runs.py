#!/usr/bin/env python3
"""Build a compact, count-first B3 scorecard from completed run reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def count(row: dict) -> str:
    return f"{row.get('correct')}/{row.get('total')}"


def add_counts(rows: list[dict], field: str) -> dict[str, int]:
    correct = total = 0
    for row in rows:
        left, right = row[field].split("/")
        correct += int(left)
        total += int(right)
    return {"correct": correct, "total": total}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    reports = []
    for path in sorted(args.runs.glob("v3_final_*/deterministic_report.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        reports.append((path.parent.name, report))
    expected = {
        ("A", "fixed"),
        ("A", "search"),
        ("A", "hybrid"),
        ("B", "fixed"),
        ("B", "search"),
        ("B", "hybrid"),
        ("D1_critical", "hybrid"),
        ("D2_event_boundary", "hybrid"),
        ("D3_new_npa", "hybrid"),
        ("D4_known_npa", "hybrid"),
    }
    actual = {(row["scenario_id"], row["mode"]) for _, row in reports}
    if actual != expected:
        raise ValueError(f"run coverage mismatch: missing={sorted(expected-actual)} extra={sorted(actual-expected)}")

    rows = []
    for name, report in reports:
        labels = report["item_labels"]
        grouping = report["grouping"]
        rows.append(
            {
                "run": name,
                "scenario": report["scenario_id"],
                "mode": report["mode"],
                "visible_items": report["coverage"]["visible_items"],
                "reachable_digest_objects": len(
                    report["discovery"]["digest_objects_reachable_in_mode"]
                ),
                "all_digest_objects": len(
                    report["discovery"]["all_digest_objects_in_scenario"]
                ),
                "irrelevant_exposed": report["discovery"]["irrelevant_items_exposed_by_mode"],
                "relevance_exact": count(labels["relevance"]),
                "importance_exact": count(labels["importance"]),
                "roles_exact": count(labels["roles"]),
                "review_policy_exact": count(labels["review_policy"]),
                "critical_item_recall": report["safety"]["critical_item_recall"],
                "critical_items_in_low": report["safety"]["critical_items_in_low"],
                "all_evidence_quotes_verbatim": report["safety"][
                    "all_submitted_evidence_quotes_verbatim"
                ],
                "event_pairs": {
                    "correct": grouping["pair_true_positive"],
                    "false_merge": grouping["pair_false_positive"],
                    "missed_merge": grouping["pair_false_negative"],
                },
                "exact_objects": f"{grouping['exact_expected_objects']}/{grouping['expected_objects']}",
                "npa": report["npa_state"],
                "contract_errors": report["contract_errors"],
            }
        )

    by_key = {(row["scenario"], row["mode"]): row for row in rows}
    h1 = {}
    for scenario in ("A", "B"):
        h1[scenario] = {
            mode: {
                "reachable_digest_objects": by_key[(scenario, mode)][
                    "reachable_digest_objects"
                ],
                "all_digest_objects": by_key[(scenario, mode)]["all_digest_objects"],
                "irrelevant_exposed": by_key[(scenario, mode)]["irrelevant_exposed"],
            }
            for mode in ("fixed", "search", "hybrid")
        }

    selected = [row for row in rows if row["mode"] == "hybrid"]
    selected_contour = {
        "runs": [row["run"] for row in selected],
        "relevance": add_counts(selected, "relevance_exact"),
        "importance": add_counts(selected, "importance_exact"),
        "roles": add_counts(selected, "roles_exact"),
        "review_policy": add_counts(selected, "review_policy_exact"),
        "event_pairs": {
            key: sum(row["event_pairs"][key] for row in selected)
            for key in ("correct", "false_merge", "missed_merge")
        },
        "contract_errors": sum(len(row["contract_errors"]) for row in selected),
        "evidence_quotes_all_verbatim": all(
            row["all_evidence_quotes_verbatim"] for row in selected
        ),
    }

    result = {
        "benchmark": "B3 automated E2E",
        "runs": rows,
        "h1_discovery_comparison": h1,
        "selected_hybrid_contour": selected_contour,
        "interpretation_rules": [
            "Counts are primary; percentages may be derived but are not a separate source of truth.",
            "No dimension is averaged into a total score.",
            "Human time, usability, trust and adoption are not measured.",
            "Semantic sufficiency remains a separate B4 judgement.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"status": "COMPLETE_WITH_FINDINGS", "runs": len(rows)},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
