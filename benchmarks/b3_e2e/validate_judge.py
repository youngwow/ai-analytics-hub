#!/usr/bin/env python3
"""Reject incomplete or malformed semantic-judge output.

This validates coverage and shape only. It cannot prove that a semantic verdict
is correct; judge independence and manual spot checks remain part of protocol.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("response", type=Path)
parser.add_argument("--judge-packet", required=True, type=Path)
parser.add_argument("--report", required=True, type=Path)
args = parser.parse_args()

response = json.loads(args.response.read_text(encoding="utf-8"))
packet = json.loads(args.judge_packet.read_text(encoding="utf-8"))
errors: list[str] = []
if not isinstance(response, dict):
    errors.append("judge response must be an object")
    response = {}

expected_object_ids = {row["object_id"] for row in packet.get("expected_objects", [])}
object_rows = response.get("object_results")
if not isinstance(object_rows, list):
    errors.append("object_results must be an array")
    object_rows = []
object_ids = [row.get("expected_object_id") for row in object_rows if isinstance(row, dict)]
if len(object_ids) != len(set(object_ids)):
    errors.append("duplicate expected_object_id in object_results")
if set(object_ids) != expected_object_ids:
    errors.append(f"object result coverage differs: expected={sorted(expected_object_ids)} actual={sorted(set(object_ids))}")

object_fields = {"inclusion", "membership", "required_meanings", "importance_critical_correctness", "unsupported_claims", "lost_positions", "role_correctness", "evidence_validity"}
for index, row in enumerate(object_rows):
    if not isinstance(row, dict):
        errors.append(f"object_results[{index}] must be an object")
    elif not object_fields.issubset(row):
        errors.append(f"object_results[{index}] missing fields: {sorted(object_fields - set(row))}")

required_gates = {"critical_recall", "no_critical_low", "no_unsupported_released_fact", "opinion_attribution", "npa_state", "critical_role_delivery"}
gate_rows = response.get("safety_gates")
if not isinstance(gate_rows, list):
    errors.append("safety_gates must be an array")
    gate_rows = []
gate_ids = [row.get("gate") for row in gate_rows if isinstance(row, dict)]
if len(gate_ids) != len(set(gate_ids)):
    errors.append("duplicate gate in safety_gates")
if set(gate_ids) != required_gates:
    errors.append(f"safety gate coverage differs: expected={sorted(required_gates)} actual={sorted(set(gate_ids))}")

required_hypotheses = {"value_proxy", "H1", "H2_prerequisites", "H3", "H4_proxy", "H5_content_proxy"}
hypotheses = response.get("hypothesis_results")
if not isinstance(hypotheses, dict) or set(hypotheses) != required_hypotheses:
    errors.append("hypothesis_results must contain every declared proxy exactly once")
if not isinstance(response.get("limitations"), list):
    errors.append("limitations must be an array")

report = {"status": "PASS" if not errors else "FAIL", "errors": errors, "expected_objects": len(expected_object_ids), "required_safety_gates": len(required_gates)}
args.report.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(0 if not errors else 1)
