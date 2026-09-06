#!/usr/bin/env python3
"""Score an isolated NPA contract run against the frozen B2 truth."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def normalized_relation(relation, same_npa):
    if same_npa is False:
        return "different"
    if relation == "same_stage_report":
        return "same_stage"
    return relation


def ratio(correct: int, total: int):
    return None if total == 0 else correct / total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prediction", type=Path)
    parser.add_argument("--version", default="v1")
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()

    prediction = load_json(args.prediction)
    data = ROOT / "data" / args.version
    catalog = {row["id"]: row for row in load_jsonl(data / "catalog.jsonl")}
    split = prediction["split"]
    visible = {
        item_id
        for item_id, row in catalog.items()
        if split == "all" or row["split"] == split
    }
    pair_truth = {
        row["case_id"]: row
        for row in load_jsonl(data / "npa_link_cases.jsonl")
        if row["left_id"] in visible and row["right_id"] in visible
    }
    pair_predictions = {
        row["case_id"]: row for row in prediction["npa_link_predictions"]
    }
    stage_truth = {}
    for trajectory in load_jsonl(data / "npa_trajectories.jsonl"):
        for item_id, stage in zip(
            trajectory["state_ids_in_order"], trajectory["expected_stages"], strict=True
        ):
            if item_id in visible:
                stage_truth[item_id] = stage
    stage_predictions = {
        row["id"]: row["stage"] for row in prediction["npa_state_predictions"]
    }

    errors = []
    if set(pair_predictions) != set(pair_truth):
        errors.append("NPA pair IDs do not exactly match the visible frozen cases")
    if set(stage_predictions) != set(stage_truth):
        errors.append("NPA state IDs do not exactly match the visible frozen states")
    same_correct = sum(
        pair_predictions.get(case_id, {}).get("same_npa") == truth["same_npa"]
        for case_id, truth in pair_truth.items()
    )
    relation_correct = sum(
        normalized_relation(
            pair_predictions.get(case_id, {}).get("relation"),
            pair_predictions.get(case_id, {}).get("same_npa"),
        )
        == normalized_relation(truth["relation"], truth["same_npa"])
        for case_id, truth in pair_truth.items()
    )
    stage_correct = sum(
        stage_predictions.get(item_id) == stage for item_id, stage in stage_truth.items()
    )
    report = {
        "benchmark": "B2-N isolated contract",
        "dataset_version": prediction["dataset_version"],
        "split": split,
        "contract_errors": errors,
        "usage": prediction.get("usage", {}),
        "npa": {
            "link_identity_accuracy": ratio(same_correct, len(pair_truth)),
            "relation_accuracy": ratio(relation_correct, len(pair_truth)),
            "stage_accuracy": ratio(stage_correct, len(stage_truth)),
        },
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
