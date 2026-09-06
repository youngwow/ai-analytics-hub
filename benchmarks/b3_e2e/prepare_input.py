#!/usr/bin/env python3
"""Materialise one leak-free B3 input packet for any agent or LLM system."""

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
parser.add_argument("--version", default="v2", choices=["v1", "v2"])
parser.add_argument("--scenario", required=True)
parser.add_argument("--mode", default="hybrid", choices=["fixed", "search", "hybrid"])
parser.add_argument("--view", default="normalized", choices=["normalized", "transport"])
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args()
DATA = ROOT / "data" / args.version

scenarios = load_json(DATA / "scenarios.json")
scenario_map = scenarios.get("scenarios")
if scenario_map is not None:
    if args.scenario not in scenario_map:
        parser.error(f"unknown scenario for {args.version}: {args.scenario}")
    scenario = scenario_map[args.scenario]
    ids = set(scenario["item_ids"])
elif args.scenario in scenarios["flows"]:
    ids = set(scenarios["flows"][args.scenario]["item_ids"])
    scenario = scenarios["flows"][args.scenario]
else:
    scenario = scenarios["diagnostics"][args.scenario]
    ids = set(scenario["item_ids"])

observations = []
canonical_ids = set()
for item in load_jsonl(DATA / "timeline.jsonl"):
    if item["id"] not in ids:
        continue
    if args.mode != "hybrid" and args.mode not in item["discoverability"]:
        continue
    channels = item["discoverability"] if args.mode == "hybrid" else [args.mode]
    canonical_ids.add(item["id"])
    for channel in channels:
        visible = {
            "observation_id": f"{item['id']}::{channel}",
            "canonical_item_id": item["id"],
            "discovery_channel": channel,
            "available_at": item["available_at"],
            "source_name": item["source_name"],
            "source_url": item["source_url"],
            "language": item["language"],
        }
        if args.view == "normalized":
            visible["title"] = item["title"]
            visible["raw_text"] = item["raw_text"]
        elif channel == "search":
            visible["transport_payload"] = {
                "format": "search_result",
                "result_id": visible["observation_id"],
                "title": item["title"],
                "content": item["raw_text"],
                "url": item["source_url"],
                "published_at": item["available_at"],
            }
        else:
            visible["transport_payload"] = item["transport_payload"]
        observations.append(visible)

visible_scenario = {key: value for key, value in scenario.items() if key not in {"primary_hypotheses"}}
packet = {
    "benchmark": "B3 E2E",
    "dataset_version": scenarios["dataset_version"],
    "scenario_id": args.scenario,
    "mode": args.mode,
    "view": args.view,
    "scenario": visible_scenario,
    "company_context": load_json(DATA / "context_gs_labs.json"),
    "initial_state": load_json(DATA / "initial_states.json")[args.scenario] if (DATA / "initial_states.json").exists() else None,
    "task": (DATA / "agent_task.txt").read_text(encoding="utf-8"),
    "response_schema": load_json(DATA / "prediction.schema.json"),
    "timeline": sorted(observations, key=lambda x: (x["available_at"], x["observation_id"])),
}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"{len(observations)} observations / {len(canonical_ids)} canonical items -> {args.output}")
