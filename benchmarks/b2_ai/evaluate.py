#!/usr/bin/env python3
"""Score B2 structure exactly and emit a separate packet for semantic judging.

No keyword similarity or weighted aggregate is used. Meanings that cannot be
checked exactly are delegated to a calibrated human/LLM judge.
"""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def ratio(a: int, b: int):
    return None if b == 0 else a / b


def pair_set(groups):
    return {tuple(sorted(pair)) for group in groups for pair in itertools.combinations(sorted(group), 2)}


def normalized_npa_relation(relation, same_npa):
    """Negative subtypes are annotations, not distinct product decisions."""
    if same_npa is False:
        return "different"
    if relation == "same_stage_report":
        return "same_stage"
    return relation


def bcubed(expected_groups, predicted_groups, item_ids):
    expected_by_item = {item_id: group for group in expected_groups for item_id in group}
    predicted_by_item = {item_id: group for group in predicted_groups for item_id in group}
    precisions, recalls = [], []
    for item_id in item_ids:
        expected = expected_by_item.get(item_id, {item_id})
        predicted = predicted_by_item.get(item_id, {item_id})
        overlap = len(expected & predicted)
        precisions.append(overlap / len(predicted))
        recalls.append(overlap / len(expected))
    precision = ratio(sum(precisions), len(precisions))
    recall = ratio(sum(recalls), len(recalls))
    f1 = None if precision is None or recall is None or precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return {"precision": precision, "recall": recall, "f1": f1}


def classification_report(truth, predictions, field, classes):
    per_class, f1s = {}, []
    for value in classes:
        expected = {item_id for item_id, row in truth.items() if row[field] == value}
        predicted = {item_id for item_id, row in predictions.items() if row.get(field) == value}
        tp = len(expected & predicted)
        precision, recall = ratio(tp, len(predicted)), ratio(tp, len(expected))
        f1 = None if precision is None or recall is None or precision + recall == 0 else 2 * precision * recall / (precision + recall)
        if f1 is not None:
            f1s.append(f1)
        per_class[value] = {"support": len(expected), "predicted": len(predicted), "precision": precision, "recall": recall, "f1": f1}
    return {"classes": per_class, "macro_f1": ratio(sum(f1s), len(f1s))}


parser = argparse.ArgumentParser()
parser.add_argument("prediction", type=Path)
parser.add_argument("--version", default="v1")
parser.add_argument("--report", required=True, type=Path)
parser.add_argument("--judge-packet", required=True, type=Path)
args = parser.parse_args()

data = ROOT / "data" / args.version
prediction = load_json(args.prediction)
contract = load_json(data / "contract.json")
catalog = {row["id"]: row for row in load_jsonl(data / "catalog.jsonl")}
materials = {row["id"]: row for row in load_jsonl(data / "materials.jsonl")}
all_truth = {row["id"]: row for row in load_jsonl(data / "ground_truth.jsonl")}
split = prediction.get("split")
errors = []
if split not in {"all", "development", "validation", "holdout", "rat"}:
    errors.append("invalid split")
    split = "all"
visible = {item_id for item_id, row in catalog.items() if split == "all" or row["split"] == split}

if prediction.get("dataset_version") != contract["dataset_version"]:
    errors.append("dataset_version does not match the frozen contract")
for field in ("run_id", "configuration_id"):
    if not isinstance(prediction.get(field), str) or not prediction[field].strip():
        errors.append(f"{field} must be a non-empty string")
for field in ("material_predictions", "event_clusters", "npa_link_predictions", "npa_state_predictions"):
    if not isinstance(prediction.get(field), list):
        errors.append(f"{field} must be an array")
        prediction[field] = []

expected_material_ids = {item_id for item_id in visible if {"B2-F", "B2-R"} & set(catalog[item_id]["sets"])}
material_rows = [row for row in prediction["material_predictions"] if isinstance(row, dict) and isinstance(row.get("id"), str)]
material_predictions = {row["id"]: row for row in material_rows}
if len(material_predictions) != len(material_rows):
    errors.append("duplicate material prediction IDs")
if set(material_predictions) != expected_material_ids:
    errors.append(f"material coverage mismatch: missing={sorted(expected_material_ids - set(material_predictions))}, extra={sorted(set(material_predictions) - expected_material_ids)}")

valid_relevance = {"relevant", "borderline", "irrelevant", "unknown"}
valid_importance = {"low", "medium", "high", "critical"}
valid_roles = {"PR", "GR", "HEAD"}
quote_checks = []
for item_id, row in material_predictions.items():
    if row.get("relevance") not in valid_relevance:
        errors.append(f"{item_id}: invalid relevance")
    if row.get("importance") not in valid_importance:
        errors.append(f"{item_id}: invalid importance")
    if not isinstance(row.get("critical_or_escalate"), bool):
        errors.append(f"{item_id}: critical_or_escalate must be boolean")
    if not isinstance(row.get("roles"), list) or not set(row.get("roles", [])).issubset(valid_roles):
        errors.append(f"{item_id}: invalid roles")
    if not isinstance(row.get("summary"), str):
        errors.append(f"{item_id}: summary must be a string")
    if not isinstance(row.get("impact"), str):
        errors.append(f"{item_id}: impact must be a string")
    if not isinstance(row.get("claims"), list):
        errors.append(f"{item_id}: claims must be an array")
        continue
    source_text = materials[item_id]["text"]
    for index, claim in enumerate(row["claims"]):
        valid = isinstance(claim, dict) and isinstance(claim.get("text"), str) and isinstance(claim.get("evidence_quote"), str)
        verbatim = valid and bool(claim["evidence_quote"].strip()) and claim["evidence_quote"] in source_text
        quote_checks.append({"id": item_id, "claim_index": index, "valid_shape": valid, "evidence_is_verbatim": verbatim})

truth = {item_id: all_truth[item_id] for item_id in expected_material_ids}
relevance = classification_report(truth, material_predictions, "relevance", ["relevant", "borderline", "irrelevant", "unknown"])
importance = classification_report(truth, material_predictions, "importance", ["low", "medium", "high", "critical"])
label_exact = {}
for field in ("relevance", "importance", "critical_or_escalate"):
    truth_field = "critical_or_escalate" if field == "critical_or_escalate" else field
    correct = sum(material_predictions.get(item_id, {}).get(field) == truth[item_id][truth_field] for item_id in truth)
    label_exact[field] = {"correct": correct, "total": len(truth), "accuracy": ratio(correct, len(truth))}
role_correct = sum(set(material_predictions.get(item_id, {}).get("roles", [])) == set(truth[item_id]["roles"]) for item_id in truth)
label_exact["roles"] = {"correct": role_correct, "total": len(truth), "exact_set_accuracy": ratio(role_correct, len(truth))}
role_labels = {}
for role in sorted(valid_roles):
    expected = {item_id for item_id, row in truth.items() if role in row["roles"]}
    predicted = {
        item_id
        for item_id, row in material_predictions.items()
        if role in row.get("roles", [])
    }
    tp = len(expected & predicted)
    role_labels[role] = {
        "support": len(expected),
        "predicted": len(predicted),
        "precision": ratio(tp, len(predicted)),
        "recall": ratio(tp, len(expected)),
    }
critical_ids = {item_id for item_id, row in truth.items() if row["critical_or_escalate"]}
critical_low = sorted(item_id for item_id in critical_ids if material_predictions.get(item_id, {}).get("importance") == "low")
review_rows_present = all(
    isinstance(row.get("review_required"), bool) for row in material_predictions.values()
)
review_ids = {
    item_id
    for item_id, row in material_predictions.items()
    if row.get("review_required") is True
}
label_error_ids = {
    item_id
    for item_id in truth
    if material_predictions[item_id].get("relevance") != truth[item_id]["relevance"]
    or material_predictions[item_id].get("importance") != truth[item_id]["importance"]
    or set(material_predictions[item_id].get("roles", [])) != set(truth[item_id]["roles"])
}

event_ids = {item_id for item_id in visible if "B2-E" in catalog[item_id]["sets"]}
expected_event_groups = []
for case in load_jsonl(data / "event_cases.jsonl"):
    for group in case["expected_clusters"]:
        subset = set(group) & event_ids
        if subset:
            expected_event_groups.append(subset)
predicted_event_groups, seen_event_members = [], []
for index, row in enumerate(prediction["event_clusters"]):
    if not isinstance(row, dict) or not isinstance(row.get("member_ids"), list) or not row.get("member_ids"):
        errors.append(f"event_clusters[{index}] malformed")
        continue
    members = set(row["member_ids"])
    if not members.issubset(event_ids):
        errors.append(f"event_clusters[{index}] contains non-B2-E IDs")
    predicted_event_groups.append(members & event_ids)
    seen_event_members.extend(members & event_ids)
if len(seen_event_members) != len(set(seen_event_members)):
    errors.append("event item occurs in more than one cluster")
if set(seen_event_members) != event_ids:
    errors.append(f"event coverage mismatch: missing={sorted(event_ids - set(seen_event_members))}")
gold_pairs, pred_pairs = pair_set(expected_event_groups), pair_set(predicted_event_groups)
tp, fp, fn = len(gold_pairs & pred_pairs), len(pred_pairs - gold_pairs), len(gold_pairs - pred_pairs)
pair_precision, pair_recall = ratio(tp, tp + fp), ratio(tp, tp + fn)
pair_f1 = None if pair_precision is None or pair_recall is None or pair_precision + pair_recall == 0 else 2 * pair_precision * pair_recall / (pair_precision + pair_recall)
b_cubed = bcubed(expected_event_groups, predicted_event_groups, event_ids)

pair_truth = {row["case_id"]: row for row in load_jsonl(data / "npa_link_cases.jsonl") if row["left_id"] in visible and row["right_id"] in visible}
pair_rows = [row for row in prediction["npa_link_predictions"] if isinstance(row, dict) and isinstance(row.get("case_id"), str)]
pair_predictions = {row["case_id"]: row for row in pair_rows}
if len(pair_predictions) != len(pair_rows) or set(pair_predictions) != set(pair_truth):
    errors.append("NPA pair prediction IDs do not exactly match visible pair cases")
same_correct = sum(pair_predictions.get(case_id, {}).get("same_npa") == row["same_npa"] for case_id, row in pair_truth.items())
relation_correct = sum(
    normalized_npa_relation(
        pair_predictions.get(case_id, {}).get("relation"),
        pair_predictions.get(case_id, {}).get("same_npa"),
    )
    == normalized_npa_relation(row["relation"], row["same_npa"])
    for case_id, row in pair_truth.items()
)

stage_truth = {}
for trajectory in load_jsonl(data / "npa_trajectories.jsonl"):
    for item_id, stage in zip(trajectory["state_ids_in_order"], trajectory["expected_stages"]):
        if item_id in visible:
            stage_truth[item_id] = stage
stage_rows = [row for row in prediction["npa_state_predictions"] if isinstance(row, dict) and isinstance(row.get("id"), str)]
stage_predictions = {row["id"]: row for row in stage_rows}
if len(stage_predictions) != len(stage_rows) or set(stage_predictions) != set(stage_truth):
    errors.append("NPA state prediction IDs do not exactly match visible NPA states")
stage_correct = sum(stage_predictions.get(item_id, {}).get("stage") == stage for item_id, stage in stage_truth.items())

report = {
    "benchmark": "B2 AI",
    "dataset_version": contract["dataset_version"],
    "run_id": prediction.get("run_id"),
    "configuration_id": prediction.get("configuration_id"),
    "usage": prediction.get("usage"),
    "split": split,
    "contract_errors": errors,
    "material_labels": {
        "exact": label_exact,
        "relevance": relevance,
        "importance": importance,
        "roles_by_label": role_labels,
    },
    "safety": {
        "critical_total": len(critical_ids),
        "critical_in_low": critical_low,
        "submitted_claims": len(quote_checks),
        "verbatim_evidence_rate": ratio(sum(x["evidence_is_verbatim"] for x in quote_checks), len(quote_checks)),
        "all_submitted_evidence_quotes_verbatim": all(x["evidence_is_verbatim"] for x in quote_checks),
    },
    "review_policy": {
        "available": review_rows_present,
        "reviewed_items": len(review_ids) if review_rows_present else None,
        "workload_rate": ratio(len(review_ids), len(truth)) if review_rows_present else None,
        "label_error_recall": ratio(len(review_ids & label_error_ids), len(label_error_ids))
        if review_rows_present
        else None,
        "critical_recall": ratio(len(review_ids & critical_ids), len(critical_ids))
        if review_rows_present
        else None,
        "unreviewed_label_error_ids": sorted(label_error_ids - review_ids)
        if review_rows_present
        else [],
    },
    "event_grouping": {"pair_precision": pair_precision, "pair_recall": pair_recall, "pair_f1": pair_f1, "b_cubed": b_cubed, "false_merge_pairs": fp, "false_split_pairs": fn},
    "npa": {
        "link_identity_accuracy": ratio(same_correct, len(pair_truth)),
        "relation_accuracy": ratio(relation_correct, len(pair_truth)),
        "stage_accuracy": ratio(stage_correct, len(stage_truth)),
    },
    "semantic_status": "pending_independent_judge",
    "interpretation_rule": "Do not average dimensions into one score; any safety-gate failure remains visible.",
}

judge_packet = {
    "benchmark": "B2 AI semantic judge packet",
    "dataset_version": contract["dataset_version"],
    "run_id": prediction.get("run_id"),
    "configuration_id": prediction.get("configuration_id"),
    "rubric": {
        "required": ["must_fact_coverage", "unsupported_claims", "summary_faithfulness", "impact_justification", "preserved_independent_positions"],
        "rule": "Judge each item/case separately against the frozen truth; do not infer one weighted total.",
    },
    "materials": [
        {"material": materials[item_id], "truth": truth[item_id], "prediction": material_predictions.get(item_id)}
        for item_id in sorted(expected_material_ids)
    ],
    "event_cases": [
        {
            "truth": row,
            "materials": [
                materials[item_id]
                for item_id in row["member_ids"]
                if item_id in visible
            ],
            "material_predictions": [
                material_predictions[item_id]
                for item_id in row["member_ids"]
                if item_id in material_predictions
            ],
            "predicted_clusters": prediction["event_clusters"],
        }
        for row in load_jsonl(data / "event_cases.jsonl") if set(row["member_ids"]) & visible
    ],
}
args.report.parent.mkdir(parents=True, exist_ok=True)
args.judge_packet.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
args.judge_packet.write_text(json.dumps(judge_packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"status": "SCORED", "contract_errors": len(errors), "report": str(args.report), "judge_packet": str(args.judge_packet)}, ensure_ascii=False))
