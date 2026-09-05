from benchmarks.b1_collection.run import coverage_report, run_deterministic


def test_b1_deterministic_contract_passes():
    report = run_deterministic()
    assert report["passed"] == report["scenario_count"]
    assert report["recall_percent"] == 100.0
    assert report["required_field_accuracy_percent"] == 100.0
    assert all(report["guardrails"].values())
    assert all(value for value in report["operational"].values() if isinstance(value, bool))


def test_b1_coverage_keeps_material_gaps_visible():
    gaps = {item["method"] for item in coverage_report()["gaps"]}
    assert "arbitrary-json-api" in gaps
    assert "js-rendered-pages" in gaps
    assert "pdf-docx-attachments" in gaps
