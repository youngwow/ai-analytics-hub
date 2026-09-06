#!/usr/bin/env python3
"""Produce count-based H1/H2/H3/H4/H5 deterministic evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from selected_runs import RUNS

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "benchmarks/b4_contextual/data/v1"
OUT = ROOT / "artifacts/b4"
B3 = ROOT / "artifacts/b3"
B3_DATA = ROOT / "benchmarks/b3_e2e/data/v2"
SCENARIOS = ("A", "B", "D1_critical", "D2_event_boundary", "D3_new_npa", "D4_known_npa")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def verify_manifest() -> None:
    manifest = load(DATA / "manifest.json")
    errors = []
    for raw, expected in manifest["sources"].items():
        path = ROOT / raw
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            errors.append(raw)
    if errors:
        raise SystemExit("frozen inputs changed: " + ", ".join(errors))


def main() -> int:
    verify_manifest()
    scorecard = load(B3 / "B3_SCORECARD_V3.json")
    gold = {row["id"]: row for row in rows(B3_DATA / "ground_truth.jsonl")}
    object_gold = rows(B3_DATA / "object_truth.jsonl")
    predictions = {flow: load(ROOT / RUNS[flow]) for flow in SCENARIOS}

    # H1: retain the two windows separately; no average hides a failed window.
    h1 = scorecard["h1_discovery_comparison"]

    # H2: risk-only is useful only when every known dangerous item is caught.
    h2_flows = {}
    for flow, prediction in predictions.items():
        decisions = {row["id"]: row for row in prediction["item_decisions"]}
        expected = [
            item_id for item_id, truth in gold.items()
            if item_id in decisions and truth["requires_review"]
        ]
        flagged = [item_id for item_id, row in decisions.items() if row.get("risk_flag")]
        h2_flows[flow] = {
            "review_all_items": len(decisions),
            "risk_only_items": len(flagged),
            "gold_risk_items": len(expected),
            "gold_risk_caught": len(set(expected) & set(flagged)),
            "gold_risk_missed_ids": sorted(set(expected) - set(flagged)),
            "extra_review_ids": sorted(set(flagged) - set(expected)),
        }

    # H3: exact membership and pair errors are architecture-neutral invariants.
    h3_flows = {}
    for flow, prediction in predictions.items():
        truths = [row for row in object_gold if row["flow_id"] == flow and row["digest"]]
        predicted = prediction["objects"]
        exact = 0
        for truth in truths:
            if sum(set(obj["member_ids"]) == set(truth["member_ids"]) for obj in predicted) == 1:
                exact += 1
        flat_items = sum(len(row["member_ids"]) for row in truths)
        h3_flows[flow] = {
            "flat_digest_materials": flat_items,
            "expected_digest_objects": len(truths),
            "exact_object_membership": exact,
            "multi_source_objects": sum(len(row["member_ids"]) > 1 for row in truths),
        }

    # H4: evaluate PR and GR separately. HEAD is a recipient, not one of the two
    # working views in this hypothesis.
    h4_flows = {}
    for flow, prediction in predictions.items():
        decisions = {row["id"]: row for row in prediction["item_decisions"]}
        roles = {}
        for role in ("PR", "GR"):
            tp = fp = fn = 0
            for item_id, decision in decisions.items():
                expected = role in gold[item_id]["roles"]
                actual = role in decision.get("roles", [])
                tp += bool(actual and expected)
                fp += bool(actual and not expected)
                fn += bool(expected and not actual)
            roles[role] = {"correctly_visible": tp, "extra": fp, "missed": fn}
        critical_misses = []
        for item_id, decision in decisions.items():
            truth = gold[item_id]
            if truth["critical"]:
                expected = set(truth["roles"]) & {"PR", "GR"}
                missing = expected - set(decision.get("roles", []))
                if missing:
                    critical_misses.append({"id": item_id, "roles": sorted(missing)})
        h4_flows[flow] = {
            "general_stream_items": len(decisions),
            "roles": roles,
            "critical_role_misses": critical_misses,
        }

    # H5 deterministic boundary: semantic sufficiency is added by independent judges.
    packet = load(DATA / "judge_packet.json")
    h5 = {
        "working_stream_items": sum(len(p["item_decisions"]) for p in predictions.values()),
        "top_layer_expected_objects": len(packet["cases"]),
        "top_layer_exact_objects": sum(row["match_count"] == 1 for row in packet["cases"]),
        "critical_objects": sum(row["critical"] for row in packet["cases"]),
        "critical_objects_missing": [
            row["case_id"] for row in packet["cases"] if row["critical"] and row["match_count"] != 1
        ],
    }
    result = {
        "benchmark": "B4-contextual-v1",
        "interpretation": "Counts only; human time/usability/trust are not measured.",
        "H1": h1,
        "H2": h2_flows,
        "H3": h3_flows,
        "H4": h4_flows,
        "H5": h5,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "deterministic_scorecard.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
