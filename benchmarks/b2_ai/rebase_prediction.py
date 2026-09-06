#!/usr/bin/env python3
"""Rebind a frozen prediction to audited labels after exact input verification."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

parser = argparse.ArgumentParser()
parser.add_argument("--input-packet", required=True, type=Path)
parser.add_argument("--prediction", required=True, type=Path)
parser.add_argument("--target-version", required=True)
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args()

packet = json.loads(args.input_packet.read_text(encoding="utf-8"))
prediction = json.loads(args.prediction.read_text(encoding="utf-8"))
target = HERE / "data" / args.target_version
contract = json.loads((target / "contract.json").read_text(encoding="utf-8"))
catalog = {
    row["id"]: row
    for row in (
        json.loads(line)
        for line in (target / "catalog.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    )
}
materials = {
    row["id"]: {key: row.get(key) for key in contract["model_input_allowlist"]}
    for row in (
        json.loads(line)
        for line in (target / "materials.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    )
}
old_materials = {row["id"]: row for row in packet["materials"]}
if any(materials.get(item_id) != row for item_id, row in old_materials.items()):
    raise SystemExit("target dataset inputs differ; prediction cannot be rebased")
if any(catalog[item_id]["split"] != packet["split"] for item_id in old_materials):
    raise SystemExit("target split differs; prediction cannot be rebased")

prediction["dataset_version"] = contract["dataset_version"]
prediction["configuration_id"] += f"__rescored-{args.target_version}"
prediction["rebase_provenance"] = {
    "source_dataset_version": packet["dataset_version"],
    "target_dataset_version": contract["dataset_version"],
    "input_equality_verified": True,
    "model_was_not_rerun": True,
}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(prediction, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
