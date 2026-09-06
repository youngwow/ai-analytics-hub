#!/usr/bin/env python3
"""Aggregate repeated deterministic B2 reports without hiding worst-case safety."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

METRICS = {
    "relevance_accuracy": ("material_labels", "exact", "relevance", "accuracy"),
    "importance_accuracy": ("material_labels", "exact", "importance", "accuracy"),
    "critical_gate_accuracy": (
        "material_labels",
        "exact",
        "critical_or_escalate",
        "accuracy",
    ),
    "roles_exact_accuracy": ("material_labels", "exact", "roles", "exact_set_accuracy"),
    "event_pair_f1": ("event_grouping", "pair_f1"),
    "npa_identity_accuracy": ("npa", "link_identity_accuracy"),
    "npa_relation_accuracy": ("npa", "relation_accuracy"),
    "npa_stage_accuracy": ("npa", "stage_accuracy"),
    "review_workload_rate": ("review_policy", "workload_rate"),
    "review_label_error_recall": ("review_policy", "label_error_recall"),
    "review_critical_recall": ("review_policy", "critical_recall"),
    "verbatim_evidence_rate": ("safety", "verbatim_evidence_rate"),
    "wall_seconds": ("usage", "wall_seconds"),
    "attempted_calls": ("usage", "attempted_calls"),
    "failed_calls": ("usage", "failed_calls"),
}


def get(document: dict, path: tuple[str, ...]):
    value = document
    for key in path:
        value = value[key]
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    parser.add_argument("reports", nargs="+", type=Path)
    args = parser.parse_args()

    reports = [json.loads(path.read_text(encoding="utf-8")) for path in args.reports]
    runs = []
    for path, report in zip(args.reports, reports, strict=True):
        row = {name: get(report, metric_path) for name, metric_path in METRICS.items()}
        row["report"] = str(path)
        row["contract_errors"] = len(report.get("contract_errors", []))
        runs.append(row)

    aggregates = {}
    for metric in METRICS:
        values = [row[metric] for row in runs if row[metric] is not None]
        aggregates[metric] = {
            "mean": statistics.fmean(values),
            "min": min(values),
            "max": max(values),
        }

    blocking_reasons = []
    if any(row["contract_errors"] for row in runs):
        blocking_reasons.append("at least one repeat has prediction-contract errors")
    if aggregates["review_critical_recall"]["min"] < 1.0:
        blocking_reasons.append("review policy missed a critical item in at least one repeat")
    if aggregates["verbatim_evidence_rate"]["min"] < 1.0:
        blocking_reasons.append("submitted claims were not fully grounded in verbatim evidence")
    if aggregates["failed_calls"]["max"] > 0:
        blocking_reasons.append("provider failures occurred in every frozen end-to-end result")
    if aggregates["npa_identity_accuracy"]["min"] < 0.8:
        blocking_reasons.append("NPA identity linking is below the acceptance orientation")
    if aggregates["npa_stage_accuracy"]["min"] < 0.8:
        blocking_reasons.append("NPA stage tracking is below the acceptance orientation")

    output = {
        "benchmark": "B2 AI repeated holdout",
        "repeats": len(runs),
        "verdict": "FAIL" if blocking_reasons else "PASS",
        "blocking_reasons": blocking_reasons,
        "runs": runs,
        "aggregates": aggregates,
        "aggregation_rule": (
            "Report mean/min/max separately. Contract, safety, provider failure and NPA "
            "blocking conditions cannot be averaged away."
        ),
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    lines = [
        "# B2 holdout — сводка повторов",
        "",
        f"**Вердикт: {output['verdict']}**. Повторов: {len(runs)}.",
        "",
        "| Метрика | Среднее | Минимум | Максимум |",
        "|---|---:|---:|---:|",
    ]
    for name, values in aggregates.items():
        lines.append(
            f"| `{name}` | {values['mean']:.4f} | {values['min']:.4f} | {values['max']:.4f} |"
        )
    lines.extend(["", "## Блокирующие причины", ""])
    lines.extend(f"- {reason}" for reason in blocking_reasons)
    lines.extend(
        [
            "",
            "Safety и отказоустойчивость оцениваются по худшему повтору, а не по среднему.",
        ]
    )
    args.output_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
