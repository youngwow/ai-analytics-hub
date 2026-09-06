from __future__ import annotations

import json
from pathlib import Path

from benchmarks.b3_e2e.scale.build_scale import build
from benchmarks.b3_e2e.scale.validate_scale import validate


def test_scale_world_is_nested_and_valid(tmp_path: Path):
    root = tmp_path / "scale"
    build(root)

    report = validate(root)

    assert report["status"] == "PASS"
    assert report["objects"] == 10_000
    assert report["cases"] == 12
    assert report["coverage"]["100"]["cases"] == 4
    assert report["coverage"]["1000"]["cases"] == 8
    assert report["coverage"]["10000"]["cases"] == 12


def test_scale_cases_include_old_events_and_known_npas(tmp_path: Path):
    root = tmp_path / "scale"
    build(root)
    cases = json.loads((root / "cases.json").read_text(encoding="utf-8"))

    large = [case for case in cases if case["min_size"] == 10_000]
    assert {case["kind"] for case in large} == {"event", "npa"}
    assert any(case["expected_relation"] == "event_update" for case in large)
    assert any(case["expected_relation"] == "same_npa" for case in large)
    assert any(case["expected_object_id"] is None for case in large)
