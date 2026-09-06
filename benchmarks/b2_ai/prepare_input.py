#!/usr/bin/env python3
"""Build a ground-truth-free B2 input packet for any external AI pipeline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


parser = argparse.ArgumentParser()
parser.add_argument("--version", default="v1")
parser.add_argument("--split", default="all", choices=["all", "development", "validation", "holdout", "rat"])
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args()

data = ROOT / "data" / args.version
contract = load_json(data / "contract.json")
catalog = {row["id"]: row for row in load_jsonl(data / "catalog.jsonl")}
materials = load_jsonl(data / contract["input_file"])
visible = {
    item_id for item_id, row in catalog.items()
    if args.split == "all" or row["split"] == args.split
}
materials = [
    {key: row.get(key) for key in contract["model_input_allowlist"]}
    for row in materials if row["id"] in visible
]

task_ids = {
    task: sorted(item_id for item_id in visible if task in catalog[item_id]["sets"])
    for task in ("B2-F", "B2-R", "B2-E", "B2-N", "B2-H")
}
npa_pairs = [
    {"case_id": row["case_id"], "left_id": row["left_id"], "right_id": row["right_id"]}
    for row in load_jsonl(data / contract["npa_link_cases_file"])
    if row["left_id"] in visible and row["right_id"] in visible
]
npa_state_ids = sorted(
    item_id
    for row in load_jsonl(data / "npa_trajectories.jsonl")
    for item_id in row["state_ids_in_order"]
    if item_id in visible
)

packet = {
    "benchmark": "B2 AI",
    "dataset_version": contract["dataset_version"],
    "split": args.split,
    "context": load_json(data / "context_gs_labs.json"),
    "materials": materials,
    "tasks": {
        "material_ids": task_ids,
        "npa_pairs": npa_pairs,
        "npa_state_ids": npa_state_ids,
        "instructions": {
            "B2-F": "Return a summary and atomic claims with verbatim evidence quotes.",
            "B2-R": "Return relevance, importance, critical/escalation flag and recipient roles.",
            "B2-E": "Partition the listed publications into event clusters; singleton clusters are allowed.",
            "B2-N": "Classify the listed NPA pairs and return a stage for every listed NPA material.",
            "B2-H": "Reserved for the separate human RAT protocol; do not infer a human result.",
        },
    },
    "prediction_contract": load_json(ROOT / "prediction.schema.json"),
}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"status": "PASS", "split": args.split, "materials": len(materials), "output": str(args.output)}, ensure_ascii=False))
