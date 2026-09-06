#!/usr/bin/env python3
"""Validate the frozen B2 dataset and its separation from model input."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


parser = argparse.ArgumentParser()
parser.add_argument("--version", default="v1")
args = parser.parse_args()
DATA = ROOT / "data" / args.version

errors = []
contract = load_json(DATA / "contract.json")
materials = load_jsonl(DATA / "materials.jsonl")
catalog = load_jsonl(DATA / "catalog.jsonl")
truth = load_jsonl(DATA / "ground_truth.jsonl")
ids = [row["id"] for row in materials]
if len(ids) != len(set(ids)):
    errors.append("duplicate material IDs")
if set(ids) != {row["id"] for row in catalog} or set(ids) != {row["id"] for row in truth}:
    errors.append("materials/catalog/ground_truth ID sets differ")
material_map = {row["id"]: row for row in materials}
for row in truth:
    source = material_map[row["id"]]["text"]
    for fact in row.get("must_facts", []):
        if fact["evidence"] not in source:
            errors.append(f"{row['id']}: evidence is not verbatim")

event_members, trajectory_members = {}, {}
catalog_map = {row["id"]: row for row in catalog}
for case in load_jsonl(DATA / "event_cases.jsonl"):
    for item_id in case["member_ids"]:
        event_members[item_id] = case["case_id"]
    splits = {catalog_map[item_id]["split"] for item_id in case["member_ids"]}
    if len(splits) != 1:
        errors.append(f"{case['case_id']}: event crosses splits")
for trajectory in load_jsonl(DATA / "npa_trajectories.jsonl"):
    for item_id in trajectory["state_ids_in_order"]:
        trajectory_members[item_id] = trajectory["trajectory_id"]
    splits = {catalog_map[item_id]["split"] for item_id in trajectory["state_ids_in_order"]}
    if len(splits) != 1:
        errors.append(f"{trajectory['trajectory_id']}: trajectory crosses splits")

deny = set(contract["model_input_denylist"])
allow = set(contract["model_input_allowlist"])
if deny & allow:
    errors.append("input allowlist overlaps denylist")
if not allow.issubset(materials[0]):
    errors.append("input allowlist contains absent fields")

checksums = []
for path in sorted(DATA.iterdir()):
    if path.is_file() and path.name != "checksums.sha256":
        checksums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
actual = "\n".join(checksums) + "\n"
expected = (DATA / "checksums.sha256").read_text(encoding="utf-8")
if actual != expected:
    errors.append("checksums.sha256 does not match frozen files")

result = {
    "status": "PASS" if not errors else "FAIL",
    "dataset_version": contract["dataset_version"],
    "materials": len(materials),
    "event_cases": len(load_jsonl(DATA / "event_cases.jsonl")),
    "npa_trajectories": len(load_jsonl(DATA / "npa_trajectories.jsonl")),
    "errors": errors,
}
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(0 if not errors else 1)
