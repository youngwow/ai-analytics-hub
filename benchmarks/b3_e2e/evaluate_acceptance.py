#!/usr/bin/env python3
"""Validate evidence for the eight mandatory product operations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument("results", type=Path, help="JSON array: step_id, passed, evidence {artifact, observation}")
parser.add_argument("--report", required=True, type=Path)
args = parser.parse_args()
expected = {row["id"]: row for row in json.loads((ROOT / "data" / "v2" / "acceptance.json").read_text(encoding="utf-8"))["steps"]}
rows = json.loads(args.results.read_text(encoding="utf-8"))
errors = []
if not isinstance(rows, list):
    errors.append("results must be a JSON array")
    rows = []
by_id = {row.get("step_id"): row for row in rows if isinstance(row, dict) and row.get("step_id")}
if len(by_id) != len([row for row in rows if isinstance(row, dict) and row.get("step_id")]):
    errors.append("duplicate step_id")
unknown = sorted(set(by_id) - set(expected))
missing = sorted(set(expected) - set(by_id))
if unknown:
    errors.append(f"unknown steps: {unknown}")
if missing:
    errors.append(f"missing steps: {missing}")
checks = []
for step_id, spec in expected.items():
    row = by_id.get(step_id, {})
    evidence = row.get("evidence")
    artifact = evidence.get("artifact") if isinstance(evidence, dict) else None
    observation = evidence.get("observation") if isinstance(evidence, dict) else None
    artifact_exists = False
    if isinstance(artifact, str) and artifact.strip():
        artifact_exists = artifact.startswith(("https://", "http://")) or (args.results.parent / artifact).resolve().exists()
    passed = row.get("passed") is True and artifact_exists and isinstance(observation, str) and bool(observation.strip())
    checks.append({"step_id": step_id, "action": spec["action"], "passed": passed, "evidence": evidence, "artifact_exists_or_url": artifact_exists})
report = {"status": "PASS" if not errors and all(x["passed"] for x in checks) else "FAIL", "errors": errors, "checks": checks}
args.report.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(0 if report["status"] == "PASS" else 1)
