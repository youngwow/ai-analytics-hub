#!/usr/bin/env python3
"""Evaluate a B3 prediction without keyword or weighted-score heuristics.

The script reports exact structured comparisons and prepares a semantic judge
packet. It deliberately does not collapse dimensions into one total score.
"""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "v1"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def safe_ratio(a: int, b: int):
    return None if b == 0 else a / b


def pairs(groups: list[set[str]]) -> set[tuple[str, str]]:
    result = set()
    for group in groups:
        result.update(tuple(sorted(pair)) for pair in itertools.combinations(group, 2))
    return result


parser = argparse.ArgumentParser()
parser.add_argument("prediction", type=Path)
parser.add_argument("--report", required=True, type=Path)
parser.add_argument("--judge-packet", required=True, type=Path)
args = parser.parse_args()

prediction = load_json(args.prediction)
scenarios = load_json(DATA / "scenarios.json")
if not isinstance(prediction, dict):
    parser.error("prediction must be a JSON object")
scenario_id = prediction.get("scenario_id")
mode = prediction.get("mode")
known_scenarios = set(scenarios["flows"]) | set(scenarios["diagnostics"])
if scenario_id not in known_scenarios:
    parser.error(f"unknown scenario_id: {scenario_id}")
if mode not in {"fixed", "search", "hybrid"}:
    parser.error(f"unknown mode: {mode}")
if scenario_id in scenarios["flows"]:
    scenario_ids = set(scenarios["flows"][scenario_id]["item_ids"])
else:
    scenario_ids = set(scenarios["diagnostics"][scenario_id]["item_ids"])

all_items = {x["id"]: x for x in load_jsonl(DATA / "timeline.jsonl")}
visible_ids = {
    item_id for item_id in scenario_ids
    if mode == "hybrid" or mode in all_items[item_id]["discoverability"]
}
item_truth = {x["id"]: x for x in load_jsonl(DATA / "ground_truth.jsonl") if x["id"] in visible_ids}
all_scenario_object_truth = [
    x for x in load_jsonl(DATA / "object_truth.jsonl")
    if set(x["member_ids"]) & scenario_ids
]
object_truth = [x for x in all_scenario_object_truth if set(x["member_ids"]) & visible_ids]
npa_truth = {
    x["object_id"]: x for x in load_jsonl(DATA / "npa_truth.jsonl")
    if set(x["history_ids"] + x["stale_or_secondary_ids"]) & visible_ids
}

errors = []
required_top = {"run_id", "scenario_id", "mode", "item_decisions", "objects", "deliveries"}
missing = sorted(required_top - prediction.keys())
if missing:
    errors.append(f"missing top-level fields: {missing}")
for field in ("item_decisions", "objects", "deliveries"):
    if field in prediction and not isinstance(prediction[field], list):
        errors.append(f"{field} must be an array")
        prediction[field] = []

for index, decision in enumerate(prediction.get("item_decisions", [])):
    required = {"id", "relevance", "importance", "critical", "roles", "risk_flag"}
    absent = sorted(required - decision.keys()) if isinstance(decision, dict) else sorted(required)
    if absent:
        errors.append(f"item_decisions[{index}] missing fields: {absent}")
        continue
    if decision["relevance"] not in {"relevant", "borderline", "irrelevant", "unknown"}:
        errors.append(f"item_decisions[{index}] invalid relevance")
    if decision["importance"] not in {"low", "medium", "high", "critical"}:
        errors.append(f"item_decisions[{index}] invalid importance")
    if not isinstance(decision["critical"], bool) or not isinstance(decision["risk_flag"], bool):
        errors.append(f"item_decisions[{index}] critical/risk_flag must be boolean")
    if not isinstance(decision["roles"], list) or not set(decision["roles"]).issubset({"PR", "GR", "HEAD"}):
        errors.append(f"item_decisions[{index}] invalid roles")

for index, obj in enumerate(prediction.get("objects", [])):
    required = {"object_id", "type", "member_ids", "summary", "importance", "critical", "roles", "claims"}
    absent = sorted(required - obj.keys()) if isinstance(obj, dict) else sorted(required)
    if absent:
        errors.append(f"objects[{index}] missing fields: {absent}")
        continue
    if obj["type"] not in {"publication", "event", "npa"}:
        errors.append(f"objects[{index}] invalid type")
    if obj["type"] == "npa" and not isinstance(obj.get("npa_state"), dict):
        errors.append(f"objects[{index}] NPA object missing npa_state")

for index, delivery in enumerate(prediction.get("deliveries", [])):
    required = {"delivery_type", "recipient", "object_ids", "digest_text"}
    absent = sorted(required - delivery.keys()) if isinstance(delivery, dict) else sorted(required)
    if absent:
        errors.append(f"deliveries[{index}] missing fields: {absent}")
        continue
    if delivery["delivery_type"] not in {"planned_digest", "urgent_alert", "npa_update"}:
        errors.append(f"deliveries[{index}] invalid delivery_type")
    if delivery["recipient"] not in {"PR", "GR", "HEAD"}:
        errors.append(f"deliveries[{index}] invalid recipient")

decisions = {x.get("id"): x for x in prediction.get("item_decisions", []) if x.get("id")}
if len(decisions) != len([x for x in prediction.get("item_decisions", []) if x.get("id")]):
    errors.append("duplicate item decision IDs")
unknown_decisions = sorted(set(decisions) - visible_ids)
missing_decisions = sorted(visible_ids - set(decisions))
if unknown_decisions:
    errors.append(f"decisions for invisible/unknown items: {unknown_decisions}")
if missing_decisions:
    errors.append(f"missing item decisions: {missing_decisions}")

labels = ["relevance", "importance", "critical"]
exact = {}
for label in labels:
    eligible = [item_id for item_id in visible_ids if item_id in decisions]
    correct = sum(decisions[item_id].get(label) == item_truth[item_id].get(label) for item_id in eligible)
    exact[label] = {"correct": correct, "total": len(eligible), "accuracy": safe_ratio(correct, len(eligible))}

role_eligible = [item_id for item_id in visible_ids if item_id in decisions]
role_correct = sum(set(decisions[x].get("roles", [])) == set(item_truth[x]["roles"]) for x in role_eligible)
exact["roles"] = {"correct": role_correct, "total": len(role_eligible), "exact_set_accuracy": safe_ratio(role_correct, len(role_eligible))}
review_correct = sum(decisions[x].get("risk_flag") == item_truth[x]["requires_review"] for x in role_eligible)
exact["review_policy"] = {"correct": review_correct, "total": len(role_eligible), "accuracy": safe_ratio(review_correct, len(role_eligible))}

critical_ids = {x for x in visible_ids if item_truth[x]["critical"]}
critical_found = {x for x in critical_ids if x in decisions and decisions[x].get("critical") is True}
critical_low = sorted(x for x in critical_ids if x in decisions and decisions[x].get("importance") == "low")

expected_groups = [set(x["member_ids"]) & visible_ids for x in object_truth]
expected_groups = [x for x in expected_groups if x]
predicted_groups = [set(x.get("member_ids", [])) & visible_ids for x in prediction.get("objects", [])]
predicted_groups = [x for x in predicted_groups if x]
predicted_object_ids = [x.get("object_id") for x in prediction.get("objects", []) if x.get("object_id")]
if len(predicted_object_ids) != len(set(predicted_object_ids)):
    errors.append("duplicate predicted object IDs")
raw_predicted_members = [x for obj in prediction.get("objects", []) for x in obj.get("member_ids", [])]
unknown_members = sorted(set(raw_predicted_members) - visible_ids)
if unknown_members:
    errors.append(f"object members outside visible input: {unknown_members}")
member_counts = {x: raw_predicted_members.count(x) for x in set(raw_predicted_members)}
reused_members = sorted(x for x, count in member_counts.items() if count > 1)
if reused_members:
    errors.append(f"items reused across predicted objects: {reused_members}")
gold_pairs = pairs(expected_groups)
pred_pairs = pairs(predicted_groups)
tp = len(gold_pairs & pred_pairs)
fp = len(pred_pairs - gold_pairs)
fn = len(gold_pairs - pred_pairs)

expected_exact_sets = {frozenset(x) for x in expected_groups}
predicted_exact_sets = {frozenset(x) for x in predicted_groups}

# Delivery IDs are mapped only when the predicted object membership is exactly
# equal to an expected object. Partial semantic matching is left to the judge.
truth_by_members = {frozenset(set(x["member_ids"]) & visible_ids): x for x in object_truth if set(x["member_ids"]) & visible_ids}
predicted_to_truth = {}
for obj in prediction.get("objects", []):
    member_set = frozenset(set(obj.get("member_ids", [])) & visible_ids)
    if member_set in truth_by_members:
        predicted_to_truth[obj.get("object_id")] = truth_by_members[member_set]["object_id"]

expected_delivery = {
    role: {x["object_id"] for x in object_truth if x["digest"] and role in x["roles"]}
    for role in ("PR", "GR", "HEAD")
}
predicted_delivery = {role: set() for role in ("PR", "GR", "HEAD")}
delivery_type_errors = []
expected_types = {x["object_id"]: set(x["delivery_types"]) for x in object_truth}
for delivery in prediction.get("deliveries", []):
    role = delivery.get("recipient")
    if role not in predicted_delivery:
        continue
    for predicted_id in delivery.get("object_ids", []):
        if predicted_id in predicted_to_truth:
            truth_id = predicted_to_truth[predicted_id]
            predicted_delivery[role].add(truth_id)
            if delivery.get("delivery_type") not in expected_types[truth_id]:
                delivery_type_errors.append({"recipient": role, "object_id": truth_id, "actual": delivery.get("delivery_type"), "expected": sorted(expected_types[truth_id])})
        else:
            predicted_delivery[role].add(f"UNMAPPED:{predicted_id}")

delivery_report = {}
for role in ("PR", "GR", "HEAD"):
    tp_role = len(expected_delivery[role] & predicted_delivery[role])
    delivery_report[role] = {
        "expected": sorted(expected_delivery[role]),
        "predicted_exactly_mapped": sorted(predicted_delivery[role]),
        "precision": safe_ratio(tp_role, len(predicted_delivery[role])),
        "recall": safe_ratio(tp_role, len(expected_delivery[role])),
    }

quote_checks = []
claim_counts = 0
for obj in prediction.get("objects", []):
    claims = obj.get("claims", [])
    if not claims:
        errors.append(f"object without claims: {obj.get('object_id')}")
    claim_ids = [x.get("claim_id") for x in claims if x.get("claim_id")]
    if len(claim_ids) != len(set(claim_ids)):
        errors.append(f"duplicate claim IDs in object: {obj.get('object_id')}")
    for claim in claims:
        claim_counts += 1
        if not claim.get("text"):
            errors.append(f"empty claim in object: {obj.get('object_id')}")
        if not claim.get("evidence"):
            errors.append(f"claim without evidence: {claim.get('claim_id')}")
        for evidence in claim.get("evidence", []):
            source_id = evidence.get("source_item_id")
            quote = evidence.get("quote", "")
            valid = (
                source_id in visible_ids
                and source_id in obj.get("member_ids", [])
                and bool(quote)
                and quote in all_items[source_id]["raw_text"]
            )
            quote_checks.append({"object_id": obj.get("object_id"), "claim_id": claim.get("claim_id"), "source_item_id": source_id, "verbatim_quote": valid})

npa_report = {}
for obj in prediction.get("objects", []):
    predicted_truth_id = predicted_to_truth.get(obj.get("object_id"))
    if predicted_truth_id not in npa_truth:
        continue
    expected = npa_truth[predicted_truth_id]
    state = obj.get("npa_state") or {}
    npa_report[predicted_truth_id] = {
        "current_stage": state.get("current_stage") == expected["current_stage"],
        "current_version": state.get("current_version") == expected["current_version"],
        "history_ids": set(state.get("history_ids", [])) == set(expected["history_ids"]),
        "change_summary": "semantic_judge",
    }

report = {
    "run_id": prediction.get("run_id"),
    "scenario_id": scenario_id,
    "mode": mode,
    "contract_errors": errors,
    "coverage": {"visible_items": len(visible_ids), "decisions": len(set(decisions) & visible_ids)},
    "discovery": {
        "all_digest_objects_in_scenario": sorted(x["object_id"] for x in all_scenario_object_truth if x["digest"]),
        "digest_objects_reachable_in_mode": sorted(x["object_id"] for x in object_truth if x["digest"]),
        "reachable_digest_coverage": safe_ratio(
            sum(x["digest"] for x in object_truth),
            sum(x["digest"] for x in all_scenario_object_truth),
        ),
        "irrelevant_items_exposed_by_mode": sum(item_truth[x]["relevance"] == "irrelevant" for x in visible_ids),
    },
    "item_labels": exact,
    "safety": {
        "critical_recall": safe_ratio(len(critical_found), len(critical_ids)),
        "critical_items_in_low": critical_low,
        "all_submitted_evidence_quotes_verbatim": all(x["verbatim_quote"] for x in quote_checks) if quote_checks else False,
        "submitted_claims": claim_counts,
        "claim_support_by_meaning": "requires_semantic_judge",
        "evidence_quote_checks": quote_checks,
    },
    "grouping": {
        "pair_true_positive": tp,
        "pair_false_positive": fp,
        "pair_false_negative": fn,
        "pair_precision": safe_ratio(tp, tp + fp),
        "pair_recall": safe_ratio(tp, tp + fn),
        "exact_expected_objects": len(expected_exact_sets & predicted_exact_sets),
        "expected_objects": len(expected_exact_sets),
    },
    "role_deliveries": delivery_report,
    "delivery_type_errors": delivery_type_errors,
    "npa_state": npa_report,
    "not_scored_by_rules": [
        "semantic groundedness beyond submitted evidence quotes",
        "summary sufficiency and preservation of meaning",
        "human task time and cognitive load",
        "product-value verdicts H2/H4/H5",
    ],
}

judge_packet = {
    "instructions": (DATA / "judge_prompt.txt").read_text(encoding="utf-8"),
    "response_schema": load_json(DATA / "judge_response.schema.json"),
    "source_items": [all_items[x] for x in sorted(visible_ids)],
    "ground_truth": [item_truth[x] for x in sorted(item_truth)],
    "expected_objects": object_truth,
    "npa_truth": list(npa_truth.values()),
    "prediction": prediction,
    "deterministic_report": report,
}

args.report.parent.mkdir(parents=True, exist_ok=True)
args.judge_packet.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
args.judge_packet.write_text(json.dumps(judge_packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
