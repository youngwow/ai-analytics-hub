#!/usr/bin/env python3
"""Evaluate a B3 prediction without keyword or weighted-score heuristics.

The script reports exact structured comparisons and prepares a semantic judge
packet. It deliberately does not collapse dimensions into one total score.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent


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


def version_generation(value) -> int | None:
    match = re.match(r"^v(\d+)(?:_|$)", str(value or ""))
    return int(match.group(1)) if match else None


parser = argparse.ArgumentParser()
parser.add_argument("prediction", type=Path)
parser.add_argument("--version", default="v2", choices=["v1", "v2"])
parser.add_argument("--report", required=True, type=Path)
parser.add_argument("--judge-packet", required=True, type=Path)
args = parser.parse_args()
DATA = ROOT / "data" / args.version

prediction = load_json(args.prediction)
scenarios = load_json(DATA / "scenarios.json")
if not isinstance(prediction, dict):
    parser.error("prediction must be a JSON object")
if not isinstance(prediction.get("run_id"), str) or not prediction.get("run_id", "").strip():
    parser.error("run_id must be a non-empty string")
scenario_id = prediction.get("scenario_id")
mode = prediction.get("mode")
scenario_map = scenarios.get("scenarios")
known_scenarios = set(scenario_map) if scenario_map is not None else set(scenarios["flows"]) | set(scenarios["diagnostics"])
if scenario_id not in known_scenarios:
    parser.error(f"unknown scenario_id: {scenario_id}")
if mode not in {"fixed", "search", "hybrid"}:
    parser.error(f"unknown mode: {mode}")
if scenario_map is not None:
    scenario_ids = set(scenario_map[scenario_id]["item_ids"])
elif scenario_id in scenarios["flows"]:
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
    if not isinstance(decision["roles"], list) or any(not isinstance(role, str) for role in decision["roles"]) or not set(decision["roles"]).issubset({"PR", "GR", "HEAD"}):
        errors.append(f"item_decisions[{index}] invalid roles")
        decision["roles"] = [role for role in decision.get("roles", []) if isinstance(role, str)] if isinstance(decision.get("roles"), list) else []

for index, obj in enumerate(prediction.get("objects", [])):
    required = {"object_id", "type", "member_ids", "summary", "importance", "critical", "roles", "claims"}
    absent = sorted(required - obj.keys()) if isinstance(obj, dict) else sorted(required)
    if absent:
        errors.append(f"objects[{index}] missing fields: {absent}")
        continue
    if obj["type"] not in {"publication", "event", "npa"}:
        errors.append(f"objects[{index}] invalid type")
    for field in ("member_ids", "roles", "claims"):
        if not isinstance(obj.get(field), list):
            errors.append(f"objects[{index}].{field} must be an array")
            obj[field] = []
    if not isinstance(obj.get("object_id"), str) or not obj.get("object_id", "").strip():
        errors.append(f"objects[{index}] invalid object_id")
    if not isinstance(obj.get("summary"), str) or not obj.get("summary", "").strip():
        errors.append(f"objects[{index}] summary must be a non-empty string")
    if obj.get("importance") not in {"low", "medium", "high", "critical"}:
        errors.append(f"objects[{index}] invalid importance")
    if not isinstance(obj.get("critical"), bool):
        errors.append(f"objects[{index}] critical must be boolean")
    if any(not isinstance(role, str) for role in obj.get("roles", [])) or not set(role for role in obj.get("roles", []) if isinstance(role, str)).issubset({"PR", "GR", "HEAD"}):
        errors.append(f"objects[{index}] invalid roles")
    if any(not isinstance(member, str) for member in obj.get("member_ids", [])):
        errors.append(f"objects[{index}] member_ids must contain strings")
        obj["member_ids"] = [member for member in obj.get("member_ids", []) if isinstance(member, str)]
    obj["roles"] = [role for role in obj.get("roles", []) if isinstance(role, str)]
    if len(obj.get("member_ids", [])) != len(set(obj.get("member_ids", []))):
        errors.append(f"objects[{index}] duplicate member_ids")
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
    if not isinstance(delivery.get("object_ids"), list):
        errors.append(f"deliveries[{index}].object_ids must be an array")
        delivery["object_ids"] = []
    elif any(not isinstance(object_id, str) for object_id in delivery["object_ids"]):
        errors.append(f"deliveries[{index}].object_ids must contain strings")
        delivery["object_ids"] = [object_id for object_id in delivery["object_ids"] if isinstance(object_id, str)]
    if not isinstance(delivery.get("digest_text"), str) or not delivery.get("digest_text", "").strip():
        errors.append(f"deliveries[{index}] digest_text must be a non-empty string")

valid_decision_rows = [x for x in prediction.get("item_decisions", []) if isinstance(x, dict) and x.get("id")]
decisions = {x.get("id"): x for x in valid_decision_rows}
if len(decisions) != len(valid_decision_rows):
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


def per_class_report(field: str, classes: list[str]) -> dict:
    rows = {}
    f1_values = []
    for value in classes:
        expected_ids = {item_id for item_id in visible_ids if item_truth[item_id][field] == value}
        predicted_ids = {item_id for item_id in visible_ids if item_id in decisions and decisions[item_id].get(field) == value}
        tp_value = len(expected_ids & predicted_ids)
        precision = safe_ratio(tp_value, len(predicted_ids))
        recall = safe_ratio(tp_value, len(expected_ids))
        f1 = None if precision is None or recall is None or precision + recall == 0 else 2 * precision * recall / (precision + recall)
        if f1 is not None:
            f1_values.append(f1)
        rows[value] = {"support": len(expected_ids), "predicted": len(predicted_ids), "precision": precision, "recall": recall, "f1": f1}
    return {"classes": rows, "macro_f1_over_present_classes": safe_ratio(sum(f1_values), len(f1_values))}


classification = {
    "relevance": per_class_report("relevance", ["relevant", "borderline", "irrelevant", "unknown"]),
    "importance": per_class_report("importance", ["low", "medium", "high", "critical"]),
}

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
valid_predicted_objects = [x for x in prediction.get("objects", []) if isinstance(x, dict)]
predicted_groups = [set(x.get("member_ids", [])) & visible_ids for x in valid_predicted_objects]
predicted_groups = [x for x in predicted_groups if x]
predicted_object_ids = [x.get("object_id") for x in valid_predicted_objects if x.get("object_id")]
if len(predicted_object_ids) != len(set(predicted_object_ids)):
    errors.append("duplicate predicted object IDs")
raw_predicted_members = [x for obj in valid_predicted_objects for x in obj.get("member_ids", [])]
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
exact_object_tp = len(expected_exact_sets & predicted_exact_sets)
unexpected_object_sets = predicted_exact_sets - expected_exact_sets
missing_object_sets = expected_exact_sets - predicted_exact_sets

# Delivery IDs are mapped only when the predicted object membership is exactly
# equal to an expected object. Partial semantic matching is left to the judge.
truth_by_members = {frozenset(set(x["member_ids"]) & visible_ids): x for x in object_truth if set(x["member_ids"]) & visible_ids}
predicted_to_truth = {}
predicted_objects_by_truth = {}
for obj in valid_predicted_objects:
    member_set = frozenset(set(obj.get("member_ids", [])) & visible_ids)
    if member_set in truth_by_members:
        truth_id = truth_by_members[member_set]["object_id"]
        predicted_to_truth[obj.get("object_id")] = truth_id
        predicted_objects_by_truth[truth_id] = obj

object_attribute_report = {}
for expected in object_truth:
    truth_id = expected["object_id"]
    actual = predicted_objects_by_truth.get(truth_id)
    object_attribute_report[truth_id] = {
        "present_as_exact_member_set": actual is not None,
        "type": actual is not None and actual.get("type") == expected["type"],
        "importance": actual is not None and actual.get("importance") == expected["importance"],
        "critical": actual is not None and actual.get("critical") == expected["critical"],
        "roles": actual is not None and set(actual.get("roles", [])) == set(expected["roles"]),
    }

expected_delivery = {
    role: {x["object_id"] for x in object_truth if x["digest"] and role in x["roles"]}
    for role in ("PR", "GR", "HEAD")
}
predicted_delivery = {role: set() for role in ("PR", "GR", "HEAD")}
delivery_occurrences = []
delivery_type_errors = []
expected_types = {x["object_id"]: set(x["delivery_types"]) for x in object_truth}
for delivery in [x for x in prediction.get("deliveries", []) if isinstance(x, dict)]:
    role = delivery.get("recipient")
    if role not in predicted_delivery:
        continue
    for predicted_id in delivery.get("object_ids", []):
        delivery_occurrences.append((role, predicted_id))
        if predicted_id in predicted_to_truth:
            truth_id = predicted_to_truth[predicted_id]
            predicted_delivery[role].add(truth_id)
            if delivery.get("delivery_type") not in expected_types[truth_id]:
                delivery_type_errors.append({"recipient": role, "object_id": truth_id, "actual": delivery.get("delivery_type"), "expected": sorted(expected_types[truth_id])})
        else:
            predicted_delivery[role].add(f"UNMAPPED:{predicted_id}")
duplicate_deliveries = sorted({
    (role, object_id)
    for role, object_id in delivery_occurrences
    if delivery_occurrences.count((role, object_id)) > 1
})
if duplicate_deliveries:
    errors.append("same object delivered more than once to one recipient")

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
for obj in valid_predicted_objects:
    claims = obj.get("claims", [])
    if not claims:
        errors.append(f"object without claims: {obj.get('object_id')}")
    claim_ids = [x.get("claim_id") for x in claims if isinstance(x, dict) and x.get("claim_id")]
    if len(claim_ids) != len(set(claim_ids)):
        errors.append(f"duplicate claim IDs in object: {obj.get('object_id')}")
    for claim in claims:
        if not isinstance(claim, dict):
            errors.append(f"non-object claim in object: {obj.get('object_id')}")
            continue
        claim_counts += 1
        if not claim.get("text"):
            errors.append(f"empty claim in object: {obj.get('object_id')}")
        evidence_rows = claim.get("evidence")
        if not isinstance(evidence_rows, list) or not evidence_rows:
            errors.append(f"claim without evidence: {claim.get('claim_id')}")
            evidence_rows = []
        for evidence in evidence_rows:
            if not isinstance(evidence, dict):
                errors.append(f"non-object evidence in claim: {claim.get('claim_id')}")
                quote_checks.append({"object_id": obj.get("object_id"), "claim_id": claim.get("claim_id"), "source_item_id": None, "verbatim_quote": False})
                continue
            source_id = evidence.get("source_item_id")
            quote = evidence.get("quote", "")
            valid = (
                source_id in visible_ids
                and source_id in obj.get("member_ids", [])
                and bool(quote)
                and (
                    quote in all_items[source_id]["raw_text"]
                    or quote in str(all_items[source_id].get("title") or "")
                )
            )
            quote_checks.append({"object_id": obj.get("object_id"), "claim_id": claim.get("claim_id"), "source_item_id": source_id, "verbatim_quote": valid})

npa_report = {
    object_id: {"present": False, "current_stage": False, "current_version": False, "current_version_exact_label": False, "effective_from": False, "history_ids": False, "history_ids_exact": False, "change_summary": "semantic_judge"}
    for object_id in npa_truth
}
for obj in valid_predicted_objects:
    predicted_truth_id = predicted_to_truth.get(obj.get("object_id"))
    if predicted_truth_id not in npa_truth:
        continue
    expected = npa_truth[predicted_truth_id]
    state = obj.get("npa_state") or {}
    actual_version = state.get("current_version")
    expected_version = expected["current_version"]
    actual_generation = version_generation(actual_version)
    expected_generation = version_generation(expected_version)
    expected_history = set(expected["history_ids"])
    actual_history = set(state.get("history_ids", []))
    npa_report[predicted_truth_id] = {
        "present": True,
        "current_stage": state.get("current_stage") == expected["current_stage"],
        "current_version": (
            actual_generation == expected_generation
            if expected_generation is not None
            else actual_version == expected_version
        ),
        "current_version_exact_label": actual_version == expected_version,
        "effective_from": state.get("effective_from") == expected.get("effective_from"),
        "history_ids": expected_history.issubset(actual_history),
        "history_ids_exact": actual_history == expected_history,
        "change_summary": "semantic_judge",
    }

critical_object_ids = {x["object_id"] for x in object_truth if x["critical"]}
critical_object_found = {
    truth_id for obj in valid_predicted_objects
    if (truth_id := predicted_to_truth.get(obj.get("object_id"))) in critical_object_ids and obj.get("critical") is True
}
critical_object_delivered = {
    truth_id for role_objects in predicted_delivery.values() for truth_id in role_objects if truth_id in critical_object_ids
}
expected_critical_recipient_pairs = {
    (role, object_id) for role, object_ids in expected_delivery.items() for object_id in object_ids if object_id in critical_object_ids
}
predicted_critical_recipient_pairs = {
    (role, object_id) for role, object_ids in predicted_delivery.items() for object_id in object_ids if object_id in critical_object_ids
}
missing_critical_recipient_pairs = expected_critical_recipient_pairs - predicted_critical_recipient_pairs

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
    "classification_by_class": classification,
    "safety": {
        "critical_recall": safe_ratio(len(critical_object_found), len(critical_object_ids)),
        "critical_item_recall": safe_ratio(len(critical_found), len(critical_ids)),
        "critical_object_recall": safe_ratio(len(critical_object_found), len(critical_object_ids)),
        "critical_delivery_recall": safe_ratio(len(critical_object_delivered), len(critical_object_ids)),
        "critical_recipient_recall": safe_ratio(len(expected_critical_recipient_pairs & predicted_critical_recipient_pairs), len(expected_critical_recipient_pairs)),
        "missing_critical_recipients": [{"recipient": role, "object_id": object_id} for role, object_id in sorted(missing_critical_recipient_pairs)],
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
        "exact_expected_objects": exact_object_tp,
        "expected_objects": len(expected_exact_sets),
        "predicted_objects": len(predicted_exact_sets),
        "exact_object_precision": safe_ratio(exact_object_tp, len(predicted_exact_sets)),
        "exact_object_recall": safe_ratio(exact_object_tp, len(expected_exact_sets)),
        "unexpected_member_sets": [sorted(x) for x in sorted(unexpected_object_sets, key=lambda value: sorted(value))],
        "missing_member_sets": [sorted(x) for x in sorted(missing_object_sets, key=lambda value: sorted(value))],
        "object_attributes": object_attribute_report,
    },
    "role_deliveries": delivery_report,
    "delivery_type_errors": delivery_type_errors,
    "duplicate_deliveries": [{"recipient": role, "object_id": object_id} for role, object_id in duplicate_deliveries],
    "npa_state": npa_report,
    "not_scored_by_rules": [
        "semantic groundedness beyond submitted evidence quotes",
        "summary sufficiency and preservation of meaning",
        "human task time and cognitive load",
        "product-value verdicts H2/H4/H5",
    ],
}

judge_objects = []
for expected in object_truth:
    visible_expected = dict(expected)
    visible_expected["member_ids"] = [item_id for item_id in expected["member_ids"] if item_id in visible_ids]
    visible_expected["unreachable_member_ids"] = [item_id for item_id in expected["member_ids"] if item_id not in visible_ids]
    if mode != "hybrid":
        # A fixed/search-only run must not be judged against meanings available
        # exclusively through the hidden mode. Visible item-level facts remain.
        visible_expected.pop("ideal_summary", None)
        visible_expected.pop("must_preserve", None)
    judge_objects.append(visible_expected)

judge_packet = {
    "instructions": (DATA / "judge_prompt.txt").read_text(encoding="utf-8"),
    "response_schema": load_json(DATA / "judge_response.schema.json"),
    "source_items": [all_items[x] for x in sorted(visible_ids)],
    "ground_truth": [item_truth[x] for x in sorted(item_truth)],
    "expected_objects": judge_objects,
    "npa_truth": list(npa_truth.values()),
    "prediction": prediction,
    "deterministic_report": report,
}

args.report.parent.mkdir(parents=True, exist_ok=True)
args.judge_packet.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
args.judge_packet.write_text(json.dumps(judge_packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
