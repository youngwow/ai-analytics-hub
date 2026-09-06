#!/usr/bin/env python3
"""Create a perfect-shape prediction to test the B3 scorer itself.

This is validator infrastructure, never an input to the product under test.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


parser = argparse.ArgumentParser()
parser.add_argument("--scenario", required=True)
parser.add_argument("--mode", default="hybrid", choices=["fixed", "search", "hybrid"])
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args()

data = ROOT / "data" / "v2"
scenarios = load_json(data / "scenarios.json")["scenarios"]
if args.scenario not in scenarios:
    parser.error(f"unknown scenario: {args.scenario}")
scenario_ids = set(scenarios[args.scenario]["item_ids"])
all_items = {x["id"]: x for x in load_jsonl(data / "timeline.jsonl")}
visible_ids = {item_id for item_id in scenario_ids if args.mode == "hybrid" or args.mode in all_items[item_id]["discoverability"]}
truth = {x["id"]: x for x in load_jsonl(data / "ground_truth.jsonl") if x["id"] in visible_ids}
objects = [x for x in load_jsonl(data / "object_truth.jsonl") if set(x["member_ids"]) & visible_ids]
npa_truth = {x["object_id"]: x for x in load_jsonl(data / "npa_truth.jsonl")}

decisions = [{
    "id": item_id,
    "relevance": truth[item_id]["relevance"],
    "importance": truth[item_id]["importance"],
    "critical": truth[item_id]["critical"],
    "roles": truth[item_id]["roles"],
    "risk_flag": truth[item_id]["requires_review"],
    "reason": "Oracle fixture used only to validate the scorer.",
} for item_id in sorted(visible_ids)]

predicted_objects = []
for expected in objects:
    members = [x for x in expected["member_ids"] if x in visible_ids]
    if not members:
        continue
    claims = []
    for index, member in enumerate(members, 1):
        source_text = all_items[member]["raw_text"]
        quote = source_text.split(".", 1)[0].strip() + ("." if "." in source_text else "")
        claims.append({"claim_id": f"{expected['object_id']}-C{index}", "text": quote, "evidence": [{"source_item_id": member, "quote": quote}]})
    obj = {
        "object_id": expected["object_id"], "type": expected["type"], "member_ids": members,
        "summary": expected["ideal_summary"], "impact_on_gs_labs": "Требует оценки по контексту компании.",
        "importance": expected["importance"], "critical": expected["critical"], "roles": expected["roles"], "claims": claims,
    }
    if expected["type"] == "npa" and expected["object_id"] in npa_truth:
        npa = npa_truth[expected["object_id"]]
        obj["npa_state"] = {"current_stage": npa["current_stage"], "current_version": npa["current_version"], "effective_from": npa.get("effective_from"), "history_ids": npa["history_ids"], "change_summary": npa["required_change"]}
    predicted_objects.append(obj)

deliveries = []
for role in ("PR", "GR", "HEAD"):
    for delivery_type in ("planned_digest", "urgent_alert", "npa_update"):
        selected = [x["object_id"] for x in objects if x["digest"] and role in x["roles"] and delivery_type in x["delivery_types"]]
        if selected:
            deliveries.append({"delivery_type": delivery_type, "recipient": role, "object_ids": selected, "digest_text": "Oracle delivery used only to test deterministic mappings."})

prediction = {"run_id": f"oracle-{args.scenario}-{args.mode}", "scenario_id": args.scenario, "mode": args.mode, "item_decisions": decisions, "objects": predicted_objects, "deliveries": deliveries}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(prediction, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"oracle: {len(decisions)} decisions, {len(predicted_objects)} objects -> {args.output}")
