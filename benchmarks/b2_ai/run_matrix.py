#!/usr/bin/env python3
"""Run and summarize the preregistered B2 A1 x A5 matrix."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
B2 = Path(__file__).resolve().parent
MODELS = (
    "glm-5.3-flash:cloud",
    "deepseek-v4-flash:cloud",
    "gpt-oss:120b-cloud",
)
CONTOURS = ("one_pass", "two_pass")


def metric(report: dict, *path: str) -> float | None:
    value: object = report
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return float(value) if isinstance(value, int | float) else None


def summarize(run_dir: Path) -> dict:
    report = json.loads((run_dir / "deterministic_report.json").read_text(encoding="utf-8"))
    usage = report.get("usage") or {}
    dimensions = {
        "relevance_macro_f1": metric(report, "material_labels", "relevance", "macro_f1"),
        "importance_macro_f1": metric(report, "material_labels", "importance", "macro_f1"),
        "roles_exact": metric(report, "material_labels", "exact", "roles", "exact_set_accuracy"),
        "event_b_cubed_f1": metric(report, "event_grouping", "b_cubed", "f1"),
        "npa_stage_accuracy": metric(report, "npa", "stage_accuracy"),
    }
    known = [value for value in dimensions.values() if value is not None]
    safety_pass = (
        not report.get("contract_errors")
        and not report["safety"]["critical_in_low"]
        and report["safety"]["all_submitted_evidence_quotes_verbatim"] is True
        and int(usage.get("failed_calls") or 0) == 0
    )
    return {
        "configuration_id": report["configuration_id"],
        "safety_pass": safety_pass,
        "worst_core_metric": min(known) if known else None,
        "dimensions": dimensions,
        "critical_in_low": report["safety"]["critical_in_low"],
        "verbatim_evidence_rate": report["safety"]["verbatim_evidence_rate"],
        "contract_errors": report["contract_errors"],
        "usage": usage,
        "run_dir": str(run_dir.relative_to(ROOT)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--version", default="v2")
    parser.add_argument("--split", default="validation")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    output_root = args.output_dir.resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    rows = []
    for contour in CONTOURS:
        for model in MODELS:
            slug = model.split(":", 1)[0].replace(".", "-")
            run_dir = output_root / f"{contour}__{slug}"
            report_path = run_dir / "deterministic_report.json"
            if not (args.resume and report_path.exists()):
                configuration = f"a1-{contour}__a5-{model}"
                command = [
                    sys.executable,
                    str(B2 / "run_benchmark.py"),
                    "--version",
                    args.version,
                    "--split",
                    args.split,
                    "--output-dir",
                    str(run_dir),
                    "--",
                    sys.executable,
                    str(B2 / "product_system.py"),
                    "--a1",
                    contour,
                    "--a3",
                    "full_scan",
                    "--a4",
                    "without_critic",
                    "--model",
                    model,
                    "--configuration-id",
                    configuration,
                    "{input}",
                    "{output}",
                ]
                subprocess.run(command, cwd=ROOT, check=True)
            rows.append(summarize(run_dir))
            (output_root / "matrix.partial.json").write_text(
                json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )

    eligible = [row for row in rows if row["safety_pass"]]
    # Predeclared minimax: protect the weakest measured core capability. A tie
    # is resolved by the simpler contour, then fewer successful model calls.
    selected = max(
        eligible,
        key=lambda row: (
            row["worst_core_metric"] if row["worst_core_metric"] is not None else -1,
            row["configuration_id"].startswith("a1-one_pass"),
            -int(row["usage"].get("model_calls") or 0),
        ),
        default=None,
    )
    result = {
        "benchmark": "B2 A1 x A5 validation matrix",
        "dataset_version": args.version,
        "split": args.split,
        "selection_rule": "safety gates, then maximum worst core metric; tie: one_pass, then fewer model calls",
        "runs": rows,
        "selected": selected,
        "limitations": [
            "Selection is internal benchmark evidence, not customer validation.",
            "Semantic judge is deliberately not used to choose among all six configurations.",
        ],
    }
    (output_root / "matrix.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if selected else 2


if __name__ == "__main__":
    raise SystemExit(main())
