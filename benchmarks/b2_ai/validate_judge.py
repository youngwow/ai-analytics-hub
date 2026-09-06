#!/usr/bin/env python3
"""Reject incomplete B2 semantic-judge responses and summarize dimensions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def ratio(a, b):
    return None if b == 0 else a / b


parser = argparse.ArgumentParser()
parser.add_argument("response", type=Path)
parser.add_argument("--judge-packet", required=True, type=Path)
parser.add_argument("--report", required=True, type=Path)
args = parser.parse_args()
response, packet = load(args.response), load(args.judge_packet)
errors = []
if response.get("run_id") != packet.get("run_id") or response.get("configuration_id") != packet.get("configuration_id"):
    errors.append("judge response run/configuration does not match packet")
material_ids = {row["material"]["id"] for row in packet["materials"]}
event_ids = {row["truth"]["case_id"] for row in packet["event_cases"]}
material_rows = response.get("material_results")
event_rows = response.get("event_results")
if not isinstance(material_rows, list):
    errors.append("material_results must be an array")
    material_rows = []
if not isinstance(event_rows, list):
    errors.append("event_results must be an array")
    event_rows = []
material_map = {row.get("id"): row for row in material_rows if isinstance(row, dict)}
event_map = {row.get("case_id"): row for row in event_rows if isinstance(row, dict)}
if len(material_map) != len(material_rows) or set(material_map) != material_ids:
    errors.append("judge material coverage must exactly match packet")
if len(event_map) != len(event_rows) or set(event_map) != event_ids:
    errors.append("judge event coverage must exactly match packet")
for item_id, row in material_map.items():
    if not isinstance(row.get("must_fact_coverage"), (int, float)) or not 0 <= row["must_fact_coverage"] <= 1:
        errors.append(f"{item_id}: invalid must_fact_coverage")
    if not isinstance(row.get("unsupported_claims"), list):
        errors.append(f"{item_id}: unsupported_claims must be an array")
    if row.get("summary_faithfulness") not in {"pass", "fail"}:
        errors.append(f"{item_id}: invalid summary_faithfulness")
    if row.get("impact_justification") not in {"pass", "fail", "not_applicable"}:
        errors.append(f"{item_id}: invalid impact_justification")
for case_id, row in event_map.items():
    if row.get("preserved_unique_facts") not in {"pass", "fail"}:
        errors.append(f"{case_id}: invalid preserved_unique_facts")
    if row.get("preserved_independent_positions") not in {"pass", "fail", "not_applicable"}:
        errors.append(f"{case_id}: invalid preserved_independent_positions")
limitations = response.get("limitations")
if not isinstance(limitations, list) or not limitations or not all(isinstance(x, str) and x.strip() for x in limitations):
    errors.append("limitations must contain at least one non-empty string")

result = {
    "status": "PASS" if not errors else "FAIL",
    "errors": errors,
    "dimensions": {
        "mean_must_fact_coverage": ratio(sum(row.get("must_fact_coverage", 0) for row in material_map.values()), len(material_map)),
        "materials_with_unsupported_claims": sum(bool(row.get("unsupported_claims")) for row in material_map.values()),
        "faithful_summaries": sum(row.get("summary_faithfulness") == "pass" for row in material_map.values()),
        "events_preserving_unique_facts": sum(row.get("preserved_unique_facts") == "pass" for row in event_map.values()),
        "events_losing_independent_positions": sum(row.get("preserved_independent_positions") == "fail" for row in event_map.values()),
    },
    "rule": "Semantic dimensions remain separate and are not converted into one weighted score.",
}
args.report.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(0 if not errors else 1)
