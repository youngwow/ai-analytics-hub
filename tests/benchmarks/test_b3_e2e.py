from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
B3 = ROOT / "benchmarks" / "b3_e2e"
DATA = B3 / "data" / "v1"


def _jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_b3_dataset_and_evaluator_oracle_smoke(tmp_path: Path):
    validate = subprocess.run(
        [sys.executable, str(B3 / "validate_dataset.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(validate.stdout)["status"] == "PASS"

    item_ids = {"D201", "D202", "D203"}
    materials = {x["id"]: x for x in _jsonl(DATA / "timeline.jsonl") if x["id"] in item_ids}
    truth = {x["id"]: x for x in _jsonl(DATA / "ground_truth.jsonl") if x["id"] in item_ids}
    objects = [x for x in _jsonl(DATA / "object_truth.jsonl") if set(x["member_ids"]) & item_ids]

    prediction_objects = []
    for obj in objects:
        first_id = obj["member_ids"][0]
        prediction_objects.append(
            {
                "object_id": obj["object_id"],
                "type": obj["type"],
                "member_ids": obj["member_ids"],
                "summary": obj["ideal_summary"],
                "impact_on_gs_labs": "Тестовый эталонный ответ.",
                "importance": obj["importance"],
                "critical": obj["critical"],
                "roles": obj["roles"],
                "claims": [{"claim_id": f"{obj['object_id']}-c1", "text": obj["ideal_summary"], "evidence": [{"source_item_id": first_id, "quote": materials[first_id]["raw_text"]}]}],
                "npa_state": None,
            }
        )

    prediction = {
        "run_id": "oracle-smoke",
        "scenario_id": "D2_event_boundary",
        "mode": "hybrid",
        "item_decisions": [
            {
                "id": item_id,
                "relevance": row["relevance"],
                "importance": row["importance"],
                "critical": row["critical"],
                "roles": row["roles"],
                "risk_flag": row["requires_review"],
                "reason": "oracle smoke",
            }
            for item_id, row in truth.items()
        ],
        "objects": prediction_objects,
        "deliveries": [
            {
                "delivery_type": "planned_digest",
                "recipient": role,
                "object_ids": [x["object_id"] for x in objects if x["digest"] and role in x["roles"]],
                "digest_text": "Тестовый эталонный выпуск.",
            }
            for role in ("PR", "GR", "HEAD")
        ],
    }
    prediction_path = tmp_path / "prediction.json"
    report_path = tmp_path / "report.json"
    judge_path = tmp_path / "judge.json"
    prediction_path.write_text(json.dumps(prediction, ensure_ascii=False), encoding="utf-8")

    subprocess.run(
        [sys.executable, str(B3 / "evaluate.py"), str(prediction_path), "--report", str(report_path), "--judge-packet", str(judge_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["contract_errors"] == []
    assert report["grouping"]["pair_precision"] == 1.0
    assert report["grouping"]["pair_recall"] == 1.0
    assert report["item_labels"]["review_policy"]["accuracy"] == 1.0
    assert report["safety"]["all_submitted_evidence_quotes_verbatim"] is True


def test_hybrid_input_exposes_cross_channel_duplicates(tmp_path: Path):
    packet_path = tmp_path / "A-hybrid.json"
    subprocess.run(
        [sys.executable, str(B3 / "prepare_input.py"), "--scenario", "A", "--mode", "hybrid", "--view", "normalized", "--output", str(packet_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert len(packet["timeline"]) == 24
    observations = [x for x in packet["timeline"] if x["canonical_item_id"] == "A12"]
    assert {x["discovery_channel"] for x in observations} == {"fixed", "search"}
