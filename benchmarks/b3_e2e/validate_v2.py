#!/usr/bin/env python3
"""Fail-fast integrity and leakage checks for B3 v2."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "v2"


def load_json(name: str):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def load_jsonl(name: str):
    return [json.loads(line) for line in (DATA / name).read_text(encoding="utf-8").splitlines() if line]


errors: list[str] = []
items = load_jsonl("timeline.jsonl")
truth = load_jsonl("ground_truth.jsonl")
objects = load_jsonl("object_truth.jsonl")
npas = load_jsonl("npa_truth.jsonl")
scenarios = load_json("scenarios.json")
states = load_json("initial_states.json")
baselines = load_json("baseline_packages.json")
transitions = load_json("expected_transitions.json")
coverage = load_json("coverage.json")

item_ids = [x.get("id") for x in items]
truth_ids = [x.get("id") for x in truth]
if len(item_ids) != len(set(item_ids)):
    errors.append("duplicate timeline IDs")
if set(item_ids) != set(truth_ids):
    errors.append("timeline and ground-truth IDs differ")

expected_scenarios = {"A": 22, "B": 22, "D1_critical": 3, "D2_event_boundary": 5, "D3_new_npa": 2, "D4_known_npa": 4}
actual_scenarios = Counter(x["flow_id"] for x in items)
if dict(actual_scenarios) != expected_scenarios:
    errors.append(f"scenario counts differ: {dict(actual_scenarios)}")
manifest_scenarios = scenarios.get("scenarios", {})
if set(manifest_scenarios) != set(expected_scenarios):
    errors.append("scenario manifest differs from required map")
for scenario_id, expected_count in expected_scenarios.items():
    ids = manifest_scenarios.get(scenario_id, {}).get("item_ids", [])
    actual_ids = [x["id"] for x in items if x["flow_id"] == scenario_id]
    if len(ids) != expected_count or set(ids) != set(actual_ids):
        errors.append(f"stale item list for {scenario_id}")
    if scenario_id not in states or scenario_id not in baselines or scenario_id not in transitions:
        errors.append(f"missing state/baseline/transition for {scenario_id}")
    elif len(states[scenario_id].get("known_events", [])) != 4:
        errors.append(f"{scenario_id} must start with four past events")
    else:
        state_events = {row["object_id"]: row for row in states[scenario_id]["known_events"]}
        for decision in states[scenario_id].get("human_decisions", []):
            target = state_events.get(decision.get("target_id"))
            if decision.get("action") == "archive" and (target is None or target.get("status") != "archived"):
                errors.append(f"{scenario_id} archive decision disagrees with current event state")

surface_counts = Counter(x["surface"] for x in items)
required_surfaces = {"rss": 18, "telegram": 18, "regulator_html": 13, "corporate_html": 4, "media_html": 1, "search_api": 4}
if dict(surface_counts) != required_surfaces:
    errors.append(f"surface counts differ: {dict(surface_counts)}")

length_bins = Counter()
for row in items:
    n = len(row["raw_text"])
    key = "lt200" if n < 200 else "200_499" if n < 500 else "500_999" if n < 1000 else "1000_2999" if n < 3000 else "3000_7999" if n < 8000 else "8000_plus"
    length_bins[key] += 1
    required = {"id", "flow_id", "available_at", "surface", "source_name", "source_url", "discoverability", "title", "raw_text", "transport_payload"}
    if not required.issubset(row):
        errors.append(f"incomplete timeline item {row.get('id')}")
    if "example.invalid" in row.get("source_url", ""):
        errors.append(f"legacy URL in {row.get('id')}")
    state_time = states.get(row.get("flow_id"), {}).get("virtual_now")
    if state_time and row.get("available_at", "") < state_time:
        errors.append(f"timeline item predates initial state: {row.get('id')}")
required_lengths = {"lt200": 5, "200_499": 12, "500_999": 13, "1000_2999": 21, "3000_7999": 5, "8000_plus": 2}
if dict(length_bins) != required_lengths:
    errors.append(f"length distribution differs: {dict(length_bins)}")

truth_by_id = {x["id"]: x for x in truth}
valid_roles = {"PR", "GR", "HEAD"}
for row in truth:
    if not set(row["roles"]).issubset(valid_roles):
        errors.append(f"invalid role in {row['id']}")
    if row["critical"] and row["importance"] != "critical":
        errors.append(f"critical item without critical importance: {row['id']}")
    if row["include_in_digest"] and not row["object_id"]:
        errors.append(f"digest item without object: {row['id']}")

member_use: Counter[str] = Counter()
for obj in objects:
    members = [truth_by_id[x] for x in obj["member_ids"] if x in truth_by_id]
    for member in obj["member_ids"]:
        member_use[member] += 1
        if member not in truth_by_id:
            errors.append(f"unknown member {member} in {obj['object_id']}")
        elif truth_by_id[member]["object_id"] != obj["object_id"]:
            errors.append(f"member/object disagreement for {member}")
    if members:
        if obj["critical"] != any(x["critical"] for x in members):
            errors.append(f"object critical flag disagrees with members: {obj['object_id']}")
        if obj["digest"] != any(x["include_in_digest"] for x in members):
            errors.append(f"object digest flag disagrees with members: {obj['object_id']}")
        if not set(obj["roles"]).issubset(set().union(*(set(x["roles"]) for x in members))):
            errors.append(f"object roles are unsupported by members: {obj['object_id']}")
if any(count > 1 for count in member_use.values()):
    errors.append("a timeline item belongs to multiple expected objects")

npa_ids = {x["object_id"] for x in npas}
object_npa_ids = {x["object_id"] for x in objects if x["type"] == "npa"}
if npa_ids != object_npa_ids:
    errors.append("NPA truth does not match NPA objects")
if any(x["object_id"] == "NPA-D3" for x in states["D3_new_npa"]["tracked_npas"]):
    errors.append("new NPA already present in D3 initial state")
if not any(x["object_id"] == "NPA-D4" for x in states["D4_known_npa"]["tracked_npas"]):
    errors.append("known NPA missing from D4 initial state")
if transitions["D4_known_npa"].get("expected_stage") != "adopted" or transitions["D4_known_npa"].get("expected_effective_from") != "2026-12-01":
    errors.append("D4 does not reach the adopted-with-effective-date lifecycle state")

for scenario_id in ("A", "B"):
    compound_telegram = [
        row for row in items
        if row["flow_id"] == scenario_id and row["surface"] == "telegram" and row["raw_text"].count("•") >= 3
    ]
    if not compound_telegram:
        errors.append(f"{scenario_id} lacks a compound Telegram digest")

for scenario_id, package in baselines.items():
    state = states[scenario_id]
    if package["tracked_npa_rows"] != state["tracked_npas"]:
        errors.append(f"baseline is not NPA-equivalent for {scenario_id}")
    if package["previous_release"] != state["last_release"]:
        errors.append(f"baseline is not release-equivalent for {scenario_id}")

if states.get("UI_ACCEPTANCE", {}).get("scenario_id") != "UI_ACCEPTANCE":
    errors.append("UI acceptance state has inconsistent scenario_id")

hidden_names = {"ground_truth", "object_truth", "npa_truth", "expected_transitions", "judge_prompt"}
for name in ["context_gs_labs.json", "initial_states.json", "scenarios.json"]:
    text = (DATA / name).read_text(encoding="utf-8")
    for hidden in hidden_names:
        if f'"{hidden}"' in text:
            errors.append(f"hidden field leaked into {name}: {hidden}")

checksum_rows = {}
for line in (DATA / "checksums.sha256").read_text(encoding="utf-8").splitlines():
    digest, name = line.split("  ", 1)
    checksum_rows[name] = digest
for name, expected in checksum_rows.items():
    actual = hashlib.sha256((DATA / name).read_bytes()).hexdigest()
    if actual != expected:
        errors.append(f"checksum mismatch: {name}")

expected_counts = coverage["counts"]
actual_counts = {
    "timeline_items": len(items), "flow_A": actual_scenarios["A"], "flow_B": actual_scenarios["B"],
    "diagnostic_items": len(items) - actual_scenarios["A"] - actual_scenarios["B"],
    "expected_objects": len(objects), "critical_items": sum(x["critical"] for x in truth),
    "irrelevant_items": sum(x["relevance"] == "irrelevant" for x in truth),
    "search_only_items": sum(x["discoverability"] == ["search"] for x in items),
}
if actual_counts != expected_counts:
    errors.append("coverage counts are stale")

result = {"status": "PASS" if not errors else "FAIL", "errors": errors, "counts": actual_counts, "scenarios": dict(actual_scenarios), "surfaces": dict(surface_counts), "lengths": dict(length_bins), "objects": {"all": len(objects), "npa": len(object_npa_ids)}, "initial_states": len(states)}
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(1 if errors else 0)
