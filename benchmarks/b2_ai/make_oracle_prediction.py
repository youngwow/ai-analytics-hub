#!/usr/bin/env python3
"""Create a perfect B2 prediction to test the scorer itself.

Never use this command as a product result: it intentionally reads hidden truth.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


parser = argparse.ArgumentParser()
parser.add_argument("--input", type=Path)
parser.add_argument("--output", type=Path)
args = parser.parse_args()
input_path = args.input or Path(os.environ["B2_INPUT"])
output_path = args.output or Path(os.environ["B2_OUTPUT"])
packet = load_json(input_path)
dataset_major = str(packet["dataset_version"]).split(".", 1)[0]
data = ROOT / "data" / dataset_major
if not data.is_dir():
    raise SystemExit(f"unsupported dataset version: {packet['dataset_version']}")
truth = {row["id"]: row for row in load_jsonl(data / "ground_truth.jsonl")}
materials = {row["id"]: row for row in load_jsonl(data / "materials.jsonl")}
visible = {row["id"] for row in packet["materials"]}
material_ids = set(packet["tasks"]["material_ids"]["B2-F"]) | set(packet["tasks"]["material_ids"]["B2-R"])
event_ids = set(packet["tasks"]["material_ids"]["B2-E"])
event_cases = load_jsonl(data / "event_cases.jsonl")
pair_truth = {row["case_id"]: row for row in load_jsonl(data / "npa_link_cases.jsonl")}
stage_truth = {
    item_id: stage
    for row in load_jsonl(data / "npa_trajectories.jsonl")
    for item_id, stage in zip(row["state_ids_in_order"], row["expected_stages"])
    if item_id in visible
}
prediction = {
    "run_id": "scorer-oracle",
    "configuration_id": "hidden-truth-self-test-only",
    "dataset_version": packet["dataset_version"],
    "split": packet["split"],
    "material_predictions": [{
        "id": item_id,
        "relevance": truth[item_id]["relevance"],
        "importance": truth[item_id]["importance"],
        "critical_or_escalate": truth[item_id]["critical_or_escalate"],
        "roles": truth[item_id]["roles"],
        "summary": truth[item_id]["must_facts"][0]["claim"] if truth[item_id]["must_facts"] else materials[item_id]["title"],
        "impact": truth[item_id]["impact_on_gs_labs"],
        "claims": [{"text": fact["claim"], "evidence_quote": fact["evidence"]} for fact in truth[item_id]["must_facts"]],
    } for item_id in sorted(material_ids)],
    "event_clusters": [
        {"cluster_id": f"oracle-{case['case_id']}-{index}", "member_ids": sorted(set(group) & event_ids)}
        for case in event_cases for index, group in enumerate(case["expected_clusters"])
        if set(group) & event_ids
    ],
    "npa_link_predictions": [{"case_id": row["case_id"], "same_npa": pair_truth[row["case_id"]]["same_npa"], "relation": pair_truth[row["case_id"]]["relation"]} for row in packet["tasks"]["npa_pairs"]],
    "npa_state_predictions": [{"id": item_id, "stage": stage} for item_id, stage in sorted(stage_truth.items())],
}
output_path.write_text(json.dumps(prediction, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
