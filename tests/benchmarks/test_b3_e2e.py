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


def test_post_collection_value_scope_requires_only_analysis_and_packaging(tmp_path: Path):
    observations = []
    for workflow, scenario in (("manual_baseline", "A"), ("product", "B")):
        for task in ("analysis", "packaging"):
            observations.append(
                {
                    "run_id": "pilot-v1",
                    "participant_id": f"p-{workflow}",
                    "scenario_id": scenario,
                    "workflow": workflow,
                    "task": task,
                    "active_seconds": 60,
                    "corrections": 0,
                    "completed": True,
                }
            )
    source = tmp_path / "observations.jsonl"
    source.write_text("".join(json.dumps(row) + "\n" for row in observations), encoding="utf-8")

    full_report = tmp_path / "full.json"
    subprocess.run(
        [
            sys.executable,
            str(B3 / "evaluate_human.py"),
            str(source),
            "--report",
            str(full_report),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    post_report = tmp_path / "post.json"
    subprocess.run(
        [
            sys.executable,
            str(B3 / "evaluate_human.py"),
            str(source),
            "--value-scope",
            "post_collection",
            "--report",
            str(post_report),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )

    assert not json.loads(full_report.read_text())["comparisons"]["VALUE"][
        "ready_for_interpretation"
    ]
    post = json.loads(post_report.read_text())
    assert post["value_scope"] == "post_collection"
    assert post["comparisons"]["VALUE"]["ready_for_interpretation"]


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
                "claims": [
                    {
                        "claim_id": f"{obj['object_id']}-c1",
                        "text": obj["ideal_summary"],
                        "evidence": [
                            {"source_item_id": first_id, "quote": materials[first_id]["raw_text"]}
                        ],
                    }
                ],
                "npa_state": None,
            }
        )
    # A transport title is part of the visible original and is valid evidence,
    # even when normalized raw_text stores the body separately.
    first_member = prediction_objects[0]["member_ids"][0]
    prediction_objects[0]["claims"][0]["evidence"][0]["quote"] = materials[
        first_member
    ]["title"]

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
                "object_ids": [
                    x["object_id"] for x in objects if x["digest"] and role in x["roles"]
                ],
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
        [
            sys.executable,
            str(B3 / "evaluate.py"),
            str(prediction_path),
            "--version",
            "v1",
            "--report",
            str(report_path),
            "--judge-packet",
            str(judge_path),
        ],
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
        [
            sys.executable,
            str(B3 / "prepare_input.py"),
            "--version",
            "v1",
            "--scenario",
            "A",
            "--mode",
            "hybrid",
            "--view",
            "normalized",
            "--output",
            str(packet_path),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert len(packet["timeline"]) == 24
    observations = [x for x in packet["timeline"] if x["canonical_item_id"] == "A12"]
    assert {x["discovery_channel"] for x in observations} == {"fixed", "search"}


def test_b3_v2_stateful_environment_and_scorer(tmp_path: Path):
    build = subprocess.run(
        [sys.executable, str(B3 / "build_v2.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(build.stdout)["counts"]["timeline_items"] == 58
    validate = subprocess.run(
        [sys.executable, str(B3 / "validate_v2.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    validation = json.loads(validate.stdout)
    assert validation["status"] == "PASS"
    assert validation["scenarios"]["D3_new_npa"] == 2
    assert validation["scenarios"]["D4_known_npa"] == 4

    packet_path = tmp_path / "D4-input.json"
    subprocess.run(
        [
            sys.executable,
            str(B3 / "prepare_input.py"),
            "--version",
            "v2",
            "--scenario",
            "D4_known_npa",
            "--mode",
            "hybrid",
            "--output",
            str(packet_path),
        ],
        cwd=ROOT,
        check=True,
    )
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert any(x["object_id"] == "NPA-D4" for x in packet["initial_state"]["tracked_npas"])
    serialized = json.dumps(packet, ensure_ascii=False)
    for hidden in (
        "ground_truth",
        "object_truth",
        "npa_truth",
        "expected_transitions",
        "primary_hypotheses",
        "ideal_summary",
    ):
        assert hidden not in serialized

    prediction_path = tmp_path / "oracle.json"
    report_path = tmp_path / "report.json"
    judge_path = tmp_path / "judge.json"
    subprocess.run(
        [
            sys.executable,
            str(B3 / "make_oracle_prediction.py"),
            "--scenario",
            "D4_known_npa",
            "--mode",
            "hybrid",
            "--output",
            str(prediction_path),
        ],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            str(B3 / "evaluate.py"),
            str(prediction_path),
            "--version",
            "v2",
            "--report",
            str(report_path),
            "--judge-packet",
            str(judge_path),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["contract_errors"] == []
    assert report["grouping"]["pair_precision"] == 1.0
    assert report["npa_state"]["NPA-D4"]["current_stage"] is True


def test_b3_v2_scorer_rejects_extra_objects_and_missing_critical_recipient(tmp_path: Path):
    def oracle(scenario: str, name: str):
        path = tmp_path / f"{name}.json"
        subprocess.run(
            [
                sys.executable,
                str(B3 / "make_oracle_prediction.py"),
                "--scenario",
                scenario,
                "--mode",
                "hybrid",
                "--output",
                str(path),
            ],
            cwd=ROOT,
            check=True,
        )
        return path, json.loads(path.read_text(encoding="utf-8"))

    prediction_path, prediction = oracle("A", "extra-object")
    prediction["objects"].append(
        {
            "object_id": "FALSE-A01",
            "type": "publication",
            "member_ids": ["A01"],
            "summary": "Лишняя карточка.",
            "importance": "low",
            "critical": False,
            "roles": [],
            "claims": [
                {
                    "claim_id": "FALSE-C1",
                    "text": "Сеть начала пилот.",
                    "evidence": [
                        {
                            "source_item_id": "A01",
                            "quote": "Сеть магазинов одежды начала пилот электронных ценников в десяти торговых точках.",
                        }
                    ],
                }
            ],
        }
    )
    prediction_path.write_text(json.dumps(prediction, ensure_ascii=False), encoding="utf-8")
    report_path, judge_path = tmp_path / "extra-report.json", tmp_path / "extra-judge.json"
    subprocess.run(
        [
            sys.executable,
            str(B3 / "evaluate.py"),
            str(prediction_path),
            "--version",
            "v2",
            "--report",
            str(report_path),
            "--judge-packet",
            str(judge_path),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["grouping"]["exact_object_precision"] < 1.0
    assert report["grouping"]["unexpected_member_sets"] == [["A01"]]

    prediction_path, prediction = oracle("D1_critical", "missing-recipient")
    prediction["deliveries"] = [
        row for row in prediction["deliveries"] if row["recipient"] != "HEAD"
    ]
    prediction_path.write_text(json.dumps(prediction, ensure_ascii=False), encoding="utf-8")
    report_path, judge_path = tmp_path / "critical-report.json", tmp_path / "critical-judge.json"
    subprocess.run(
        [
            sys.executable,
            str(B3 / "evaluate.py"),
            str(prediction_path),
            "--version",
            "v2",
            "--report",
            str(report_path),
            "--judge-packet",
            str(judge_path),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["safety"]["critical_recipient_recall"] == 2 / 3
    assert report["safety"]["missing_critical_recipients"] == [
        {"recipient": "HEAD", "object_id": "EV-D1"}
    ]


def test_b3_v2_scorer_exposes_wrong_object_attributes_and_malformed_evidence(tmp_path: Path):
    prediction_path = tmp_path / "mutant.json"
    subprocess.run(
        [
            sys.executable,
            str(B3 / "make_oracle_prediction.py"),
            "--scenario",
            "D1_critical",
            "--mode",
            "hybrid",
            "--output",
            str(prediction_path),
        ],
        cwd=ROOT,
        check=True,
    )
    prediction = json.loads(prediction_path.read_text(encoding="utf-8"))
    prediction["objects"][0]["critical"] = False
    prediction["objects"][0]["roles"] = ["PR"]
    prediction["objects"][0]["claims"][0]["evidence"] = ["not-an-evidence-object"]
    prediction_path.write_text(json.dumps(prediction, ensure_ascii=False), encoding="utf-8")

    report_path, judge_path = tmp_path / "mutant-report.json", tmp_path / "mutant-judge.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(B3 / "evaluate.py"),
            str(prediction_path),
            "--version",
            "v2",
            "--report",
            str(report_path),
            "--judge-packet",
            str(judge_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    attributes = report["grouping"]["object_attributes"]["EV-D1"]
    assert attributes["critical"] is False
    assert attributes["roles"] is False
    assert any("non-object evidence" in error for error in report["contract_errors"])
    assert report["safety"]["all_submitted_evidence_quotes_verbatim"] is False


def test_b3_v2_judge_response_requires_complete_coverage(tmp_path: Path):
    prediction_path = tmp_path / "oracle.json"
    report_path, packet_path = tmp_path / "report.json", tmp_path / "packet.json"
    subprocess.run(
        [
            sys.executable,
            str(B3 / "make_oracle_prediction.py"),
            "--scenario",
            "D2_event_boundary",
            "--mode",
            "hybrid",
            "--output",
            str(prediction_path),
        ],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            str(B3 / "evaluate.py"),
            str(prediction_path),
            "--version",
            "v2",
            "--report",
            str(report_path),
            "--judge-packet",
            str(packet_path),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    response = {
        "object_results": [
            {
                "expected_object_id": row["object_id"],
                "inclusion": "pass",
                "membership": "pass",
                "required_meanings": "pass",
                "importance_critical_correctness": "pass",
                "unsupported_claims": [],
                "lost_positions": [],
                "role_correctness": "pass",
                "evidence_validity": "pass",
            }
            for row in packet["expected_objects"]
        ],
        "safety_gates": [
            {"gate": gate, "verdict": "pass", "item_ids": []}
            for gate in (
                "critical_recall",
                "no_critical_low",
                "no_unsupported_released_fact",
                "opinion_attribution",
                "npa_state",
                "critical_role_delivery",
            )
        ],
        "hypothesis_results": {
            key: "not_tested"
            for key in (
                "value_proxy",
                "H1",
                "H2_prerequisites",
                "H3",
                "H4_proxy",
                "H5_content_proxy",
            )
        },
        "limitations": ["Synthetic scenario."],
    }
    response_path, validation_path = (
        tmp_path / "judge-response.json",
        tmp_path / "judge-validation.json",
    )
    response_path.write_text(json.dumps(response), encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            str(B3 / "validate_judge.py"),
            str(response_path),
            "--judge-packet",
            str(packet_path),
            "--report",
            str(validation_path),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    assert json.loads(validation_path.read_text(encoding="utf-8"))["status"] == "PASS"

    response["object_results"].pop()
    response_path.write_text(json.dumps(response), encoding="utf-8")
    failed = subprocess.run(
        [
            sys.executable,
            str(B3 / "validate_judge.py"),
            str(response_path),
            "--judge-packet",
            str(packet_path),
            "--report",
            str(validation_path),
        ],
        cwd=ROOT,
        capture_output=True,
    )
    assert failed.returncode == 1


def test_b3_v2_acceptance_requires_real_evidence_artifact(tmp_path: Path):
    spec = json.loads((B3 / "data" / "v2" / "acceptance.json").read_text(encoding="utf-8"))
    evidence_file = tmp_path / "ui-test.log"
    evidence_file.write_text("recorded UI acceptance run", encoding="utf-8")
    rows = [
        {
            "step_id": row["id"],
            "passed": True,
            "evidence": {"artifact": evidence_file.name, "observation": row["expected"]},
        }
        for row in spec["steps"]
    ]
    results_path, report_path = tmp_path / "acceptance.json", tmp_path / "acceptance-report.json"
    results_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            str(B3 / "evaluate_acceptance.py"),
            str(results_path),
            "--report",
            str(report_path),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "PASS"

    rows[0]["evidence"]["artifact"] = "missing.png"
    results_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    failed = subprocess.run(
        [
            sys.executable,
            str(B3 / "evaluate_acceptance.py"),
            str(results_path),
            "--report",
            str(report_path),
        ],
        cwd=ROOT,
        capture_output=True,
    )
    assert failed.returncode == 1
