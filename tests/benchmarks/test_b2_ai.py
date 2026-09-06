from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from benchmarks.b2_ai.product_system import CountingProvider, npa_predictions
from src.processing.llm import LlmTemporaryError
from tests.support import FakeLLM

ROOT = Path(__file__).resolve().parents[2]
B2 = ROOT / "benchmarks" / "b2_ai"


def load_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_b2_input_has_no_ground_truth(tmp_path: Path):
    output = tmp_path / "input.json"
    subprocess.run([sys.executable, str(B2 / "prepare_input.py"), "--split", "validation", "--output", str(output)], cwd=ROOT, check=True)
    packet = json.loads(output.read_text(encoding="utf-8"))
    serialized = json.dumps(packet, ensure_ascii=False)
    for hidden in ("ground_truth", "diagnostic_risks", "expected_clusters", "expected_stages"):
        assert hidden not in serialized
    assert packet["materials"]
    assert all(set(row) == {"case_id", "left_id", "right_id"} for row in packet["tasks"]["npa_pairs"])


def test_b2_frozen_dataset_validates():
    completed = subprocess.run([sys.executable, str(B2 / "validate_dataset.py")], cwd=ROOT, check=True, capture_output=True, text=True)
    assert json.loads(completed.stdout)["status"] == "PASS"


def test_b2_oracle_scores_exact_dimensions(tmp_path: Path):
    data = B2 / "data" / "v1"
    materials = {x["id"]: x for x in load_jsonl(data / "materials.jsonl")}
    truth = {x["id"]: x for x in load_jsonl(data / "ground_truth.jsonl")}
    catalog = {x["id"]: x for x in load_jsonl(data / "catalog.jsonl")}
    visible = {item_id for item_id, row in catalog.items() if row["split"] == "validation"}
    event_cases = [x for x in load_jsonl(data / "event_cases.jsonl") if set(x["member_ids"]) & visible]
    pair_cases = [x for x in load_jsonl(data / "npa_link_cases.jsonl") if x["left_id"] in visible and x["right_id"] in visible]
    trajectories = load_jsonl(data / "npa_trajectories.jsonl")
    stage_truth = {item_id: stage for row in trajectories for item_id, stage in zip(row["state_ids_in_order"], row["expected_stages"]) if item_id in visible}
    material_ids = {item_id for item_id in visible if {"B2-F", "B2-R"} & set(catalog[item_id]["sets"])}
    prediction = {
        "run_id": "oracle-smoke",
        "configuration_id": "oracle",
        "dataset_version": "v1.0.0",
        "split": "validation",
        "material_predictions": [{
            "id": item_id,
            "relevance": truth[item_id]["relevance"],
            "importance": truth[item_id]["importance"],
            "critical_or_escalate": truth[item_id]["critical_or_escalate"],
                "roles": truth[item_id]["roles"],
                "summary": truth[item_id]["must_facts"][0]["claim"] if truth[item_id]["must_facts"] else materials[item_id]["title"],
                "impact": truth[item_id]["impact_on_gs_labs"],
                "claims": [{"text": fact["claim"], "evidence_quote": fact["evidence"]} for fact in truth[item_id]["must_facts"]],
        } for item_id in sorted(material_ids)],
        "event_clusters": [{"cluster_id": f"oracle-{index}", "member_ids": group} for index, case in enumerate(event_cases) for group in case["expected_clusters"]],
        "npa_link_predictions": [{"case_id": row["case_id"], "same_npa": row["same_npa"], "relation": row["relation"]} for row in pair_cases],
        "npa_state_predictions": [{"id": item_id, "stage": stage} for item_id, stage in sorted(stage_truth.items())],
    }
    prediction_path, report_path, judge_path = tmp_path / "prediction.json", tmp_path / "report.json", tmp_path / "judge.json"
    prediction_path.write_text(json.dumps(prediction, ensure_ascii=False), encoding="utf-8")
    subprocess.run([sys.executable, str(B2 / "evaluate.py"), str(prediction_path), "--report", str(report_path), "--judge-packet", str(judge_path)], cwd=ROOT, check=True)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["contract_errors"] == []
    assert report["material_labels"]["exact"]["relevance"]["accuracy"] == 1.0
    assert report["event_grouping"]["pair_f1"] == 1.0
    assert report["event_grouping"]["b_cubed"]["f1"] == 1.0
    assert report["npa"]["link_identity_accuracy"] == 1.0
    assert report["npa"]["stage_accuracy"] == 1.0


def test_b2_external_runner_smoke(tmp_path: Path):
    run_dir = tmp_path / "run"
    subprocess.run([
        sys.executable, str(B2 / "run_benchmark.py"), "--split", "validation", "--output-dir", str(run_dir), "--",
        sys.executable, str(B2 / "make_oracle_prediction.py"), "--input", "{input}", "--output", "{output}",
    ], cwd=ROOT, check=True, capture_output=True, text=True)
    report = json.loads((run_dir / "deterministic_report.json").read_text(encoding="utf-8"))
    assert report["contract_errors"] == []
    assert report["event_grouping"]["pair_f1"] == 1.0


def test_npa_predictions_use_bounded_independent_batches():
    documents = {f"n{i}": {"id": f"n{i}", "title": f"NPA {i}"} for i in range(13)}
    pairs = [
        {"case_id": f"p{i}", "left_id": f"n{i}", "right_id": f"n{i + 1}"}
        for i in range(12)
    ]
    ids = [f"n{i}" for i in range(9)]

    def answer(prompt: str):
        packet = json.loads(prompt)
        return {
            "pairs": [
                {"case_id": row["case_id"], "same_npa": True, "relation": "next_state"}
                for row in packet.get("pairs", [])
            ],
            "states": [
                {"id": row["id"], "stage": "draft"} for row in packet["materials"]
            ] if not packet.get("pairs") else [],
        }

    fake = FakeLLM(answer)
    counted = CountingProvider(fake)
    links, states = npa_predictions(counted, ids, pairs, documents)

    assert counted.attempted_calls == counted.calls == 15
    assert all(len(json.loads(prompt)["pairs"]) == 1 for prompt in fake.prompts[:12])
    assert all(len(json.loads(prompt)["materials"]) <= 3 for prompt in fake.prompts[12:])
    assert len(links) == 12 and all(row["same_npa"] for row in links)
    assert len(states) == 9 and all(row["stage"] == "draft" for row in states)


def test_npa_batch_failure_does_not_erase_successful_batches():
    documents = {f"n{i}": {"id": f"n{i}", "title": f"NPA {i}"} for i in range(7)}
    pairs = [
        {"case_id": f"p{i}", "left_id": f"n{i}", "right_id": f"n{i + 1}"}
        for i in range(6)
    ]
    ids = ["n0", "n1", "n2"]

    def successful(prompt: str):
        packet = json.loads(prompt)
        return {
            "pairs": [
                {"case_id": row["case_id"], "same_npa": True, "relation": "next_state"}
                for row in packet.get("pairs", [])
            ],
            "states": [
                {"id": row["id"], "stage": "draft"} for row in packet["materials"]
            ] if not packet.get("pairs") else [],
        }

    fake = FakeLLM([LlmTemporaryError("timeout"), successful, successful, successful])
    links, states = npa_predictions(CountingProvider(fake), ids, pairs, documents)

    assert links[0]["same_npa"] is False
    assert [row["same_npa"] for row in links[1:]] == [True] * 5
    assert all(row["stage"] == "draft" for row in states)
