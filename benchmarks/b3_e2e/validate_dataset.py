#!/usr/bin/env python3
"""Fail-fast structural audit for the frozen B3 dataset."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "v1"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


errors: list[str] = []
items = load_jsonl(DATA / "timeline.jsonl")
truth = load_jsonl(DATA / "ground_truth.jsonl")
objects = load_jsonl(DATA / "object_truth.jsonl")
npa_truth = load_jsonl(DATA / "npa_truth.jsonl")
scenarios = load_json(DATA / "scenarios.json")
coverage = load_json(DATA / "coverage.json")
acceptance = load_json(DATA / "acceptance.json")

item_ids = [x["id"] for x in items]
truth_ids = [x["id"] for x in truth]
if len(item_ids) != len(set(item_ids)):
    errors.append("duplicate timeline IDs")
if len(truth_ids) != len(set(truth_ids)):
    errors.append("duplicate ground-truth IDs")
if set(item_ids) != set(truth_ids):
    errors.append("timeline and ground-truth IDs differ")

by_id = {x["id"]: x for x in truth}
object_ids = [x["object_id"] for x in objects]
if len(object_ids) != len(set(object_ids)):
    errors.append("duplicate object IDs")
for obj in objects:
    members = obj["member_ids"]
    if not members or any(x not in by_id for x in members):
        errors.append(f"bad members in {obj['object_id']}")
        continue
    if any(by_id[x]["object_id"] != obj["object_id"] for x in members):
        errors.append(f"ground-truth object mismatch in {obj['object_id']}")
    if len({next(y["flow_id"] for y in items if y["id"] == x) for x in members}) != 1:
        errors.append(f"cross-flow object {obj['object_id']}")
if {x["object_id"] for x in npa_truth} != {x["object_id"] for x in objects if x["type"] == "npa"}:
    errors.append("NPA truth does not match NPA objects")

for flow_id in ("A", "B"):
    ids = scenarios["flows"][flow_id]["item_ids"]
    if len(ids) != 22 or len(set(ids)) != 22:
        errors.append(f"flow {flow_id} must contain 22 unique items")
    surfaces = {x["surface"] for x in items if x["id"] in ids}
    if not {"rss", "telegram", "regulator_html", "search_api"}.issubset(surfaces):
        errors.append(f"flow {flow_id} misses a required source surface")
    source_names = {x["source_name"] for x in items if x["id"] in ids}
    if len(source_names) < 7:
        errors.append(f"flow {flow_id} must expose at least seven distinct sources")
    flow_truth = [by_id[x] for x in ids]
    if sum(x["relevance"] == "irrelevant" for x in flow_truth) < 6:
        errors.append(f"flow {flow_id} has too little realistic noise")
    if sum(x["object_type"] == "event" for x in flow_truth) < 6:
        errors.append(f"flow {flow_id} has too little event coverage")
    if sum(x["object_type"] == "npa" for x in flow_truth) < 3:
        errors.append(f"flow {flow_id} has no NPA trajectory")
    if sum(next(y for y in items if y["id"] == x)["discoverability"] == ["search"] for x in ids) != 3:
        errors.append(f"flow {flow_id} must contain exactly three search-only items")
    if sum(set(next(y for y in items if y["id"] == x)["discoverability"]) == {"fixed", "search"} for x in ids) < 2:
        errors.append(f"flow {flow_id} must contain search/fixed overlap")

if not any(x["critical"] for x in truth):
    errors.append("no critical safety case")
if not any(x["relevance"] == "unknown" for x in truth):
    errors.append("no internal-context unknown case")
if not any("stale" in " ".join(x["risk_reasons"]) for x in truth):
    errors.append("no stale-version case")
if not any(x["requires_review"] for x in truth) or not any(not x["requires_review"] for x in truth):
    errors.append("review policy has no positive/negative contrast")
if len(acceptance.get("steps", [])) != 8:
    errors.append("mandatory acceptance path must contain eight operations")

for item in items:
    if not item["source_url"].endswith(f"/{item['id'].lower()}"):
        errors.append(f"unexpected URL mapping for {item['id']}")
    if "example.invalid" not in item["source_url"]:
        errors.append(f"non-fixture URL in {item['id']}")
    if not isinstance(item.get("transport_payload"), dict) or not item["transport_payload"].get("format"):
        errors.append(f"missing transport payload in {item['id']}")

checksums = {}
for line in (DATA / "checksums.sha256").read_text(encoding="utf-8").splitlines():
    digest, name = line.split("  ", 1)
    checksums[name] = digest
for name, expected in checksums.items():
    actual = hashlib.sha256((DATA / name).read_bytes()).hexdigest()
    if actual != expected:
        errors.append(f"checksum mismatch: {name}")

actual_counts = {
    "timeline_items": len(items),
    "flow_A": sum(x["flow_id"] == "A" for x in items),
    "flow_B": sum(x["flow_id"] == "B" for x in items),
    "diagnostic_items": sum(x["flow_id"] == "D" for x in items),
    "expected_objects": len(objects),
    "critical_items": sum(x["critical"] for x in truth),
    "irrelevant_items": sum(x["relevance"] == "irrelevant" for x in truth),
    "search_only_items": sum(x["discoverability"] == ["search"] for x in items),
}
if actual_counts != coverage["counts"]:
    errors.append("coverage counts are stale")

result = {
    "status": "PASS" if not errors else "FAIL",
    "errors": errors,
    "counts": actual_counts,
    "surfaces": dict(Counter(x["surface"] for x in items)),
    "roles": dict(Counter(role for x in truth for role in x["roles"])),
    "relevance": dict(Counter(x["relevance"] for x in truth)),
    "importance": dict(Counter(x["importance"] for x in truth)),
}
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(1 if errors else 0)
